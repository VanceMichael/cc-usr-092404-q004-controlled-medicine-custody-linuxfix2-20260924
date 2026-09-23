"""HTTP 边界测试：角色视图裁剪、错误码、夜班定位与恢复入口。"""


def _setup_world(client):
    s1 = client.post("/admin/stores", json={"code": "S1", "name": "城北店", "license_no": "LIC-S1"}).get_json()["id"]
    s2 = client.post("/admin/stores", json={"code": "S2", "name": "城南店", "license_no": "LIC-S2"}).get_json()["id"]
    c1 = client.post("/admin/carriers", json={
        "code": "C1", "name": "安通", "qualification_no": "Q1",
        "qualified_until": "2031-01-01T00:00:00+00:00",
    }).get_json()["id"]
    client.post("/admin/staff", json={"user_code": "u1", "store_id": s1, "role": "pharmacist"})
    client.post("/admin/staff", json={"user_code": "u2", "store_id": s1, "role": "pharmacist"})
    client.post("/admin/staff", json={"user_code": "u3", "store_id": s2, "role": "pharmacist"})
    client.post("/admin/batches", json={"batch_no": "B-001", "drug_name": "芬太尼贴剂"})
    client.post("/admin/genesis", json={"batch_no": "B-001", "store_id": s1, "quantity": 100, "actor": "admin"})
    return s1, s2, c1


def _happy_path(client, s1, s2, c1, qty=10):
    resp = client.post("/requests", json={
        "request_no": "TR-1", "batch_no": "B-001", "quantity": qty,
        "from_store_id": s1, "to_store_id": s2, "carrier_id": c1,
        "created_by": "u1",
        "planned_at": "2026-09-15T10:00:00+00:00",
        "expected_by": "2030-09-23T14:00:00+00:00",
    })
    assert resp.status_code == 201, resp.get_json()
    client.post("/requests/TR-1/reviews", json={"reviewer": "u2", "decision": "approve"})
    client.post("/requests/TR-1/reviews", json={"reviewer": "u3", "decision": "approve"})
    client.post("/requests/TR-1/release", json={"seal_no": "SEAL-1", "actor": "u1", "terminal_id": "T-A"})
    client.post("/requests/TR-1/seal", json={"seal_no": "SEAL-1", "actor": "u3", "terminal_id": "T-B"})
    resp = client.post("/requests/TR-1/receive", json={
        "actual_quantity": qty, "actor": "u3", "terminal_id": "T-B",
    })
    assert resp.status_code == 200, resp.get_json()


def test_full_flow_and_locate(client):
    s1, s2, c1 = _setup_world(client)
    _happy_path(client, s1, s2, c1)

    located = client.get("/batches/B-001/location").get_json()
    holders = {h["custodian"]: h["quantity"] for h in located["holders"]}
    assert holders == {f"store:{s1}": 90, f"store:{s2}": 10}


def test_role_views_field_redaction(client):
    s1, s2, c1 = _setup_world(client)
    _happy_path(client, s1, s2, c1)

    carrier = client.get("/requests/TR-1?role=carrier").get_json()
    assert "frozen" not in carrier  # 承运看不到门店许可证快照
    assert carrier["current_custodian"] == f"store:{s2}"

    store_view = client.get("/requests/TR-1?role=store").get_json()
    assert "frozen" not in store_view
    assert store_view["carrier_qualified"] is True

    audit_view = client.get("/requests/TR-1?role=audit").get_json()
    assert audit_view["frozen"]["from_store"]["license_no"] == "LIC-S1"
    assert audit_view["event_chain"][0]["event_type"] == "created"

    assert client.get("/requests/TR-1").status_code == 400
    assert client.get("/requests/TR-1?role=regulator").status_code == 400


def test_chain_and_audit_require_audit_role(client):
    s1, s2, c1 = _setup_world(client)
    _happy_path(client, s1, s2, c1)
    assert client.get("/requests/TR-1/chain?role=store").status_code == 403
    assert client.get("/audit?role=carrier").status_code == 403

    report = client.get("/audit?role=audit").get_json()
    assert report["ok"] is True
    assert report["quantity_conserved"] is True
    assert report["unique_inflight_custody"] is True

    chain = client.get("/requests/TR-1/chain?role=audit").get_json()
    types_ = [e["event_type"] for e in chain["events"]]
    assert types_[0] == "created"
    assert "released" in types_ and "received" in types_


def test_duplicate_seal_confirmation_returns_original(client):
    s1, s2, c1 = _setup_world(client)
    client.post("/requests", json={
        "request_no": "TR-1", "batch_no": "B-001", "quantity": 5,
        "from_store_id": s1, "to_store_id": s2, "carrier_id": c1,
        "created_by": "u1",
        "planned_at": "2026-09-15T10:00:00+00:00",
        "expected_by": "2030-09-23T14:00:00+00:00",
    })
    client.post("/requests/TR-1/reviews", json={"reviewer": "u2", "decision": "approve"})
    client.post("/requests/TR-1/reviews", json={"reviewer": "u3", "decision": "approve"})
    client.post("/requests/TR-1/release", json={"seal_no": "SEAL-1", "actor": "u1", "terminal_id": "T-A"})

    first = client.post("/requests/TR-1/seal", json={"seal_no": "SEAL-1", "actor": "u3", "terminal_id": "T-X"}).get_json()
    second = client.post("/requests/TR-1/seal", json={"seal_no": "SEAL-1", "actor": "u3", "terminal_id": "T-Y"}).get_json()
    assert first["duplicated"] is False
    assert second["duplicated"] is True
    assert first["confirmation"] == second["confirmation"]


def test_bypass_and_recovery_endpoints(client):
    s1, s2, c1 = _setup_world(client)
    client.post("/requests", json={
        "request_no": "TR-1", "batch_no": "B-001", "quantity": 5,
        "from_store_id": s1, "to_store_id": s2, "carrier_id": c1,
        "created_by": "u1",
        "planned_at": "2026-09-15T08:00:00+00:00",
        "expected_by": "2026-09-15T10:00:00+00:00",
    })
    client.post("/requests/TR-1/reviews", json={"reviewer": "u2", "decision": "approve"})
    client.post("/requests/TR-1/reviews", json={"reviewer": "u3", "decision": "approve"})
    client.post("/requests/TR-1/release", json={"seal_no": "SEAL-1", "actor": "u1", "terminal_id": "T-A"})

    # 离线设备越过签署节点
    scan = client.post("/scans/offline", json={
        "request_no": "TR-1", "scan_type": "receive", "terminal_id": "T-B",
        "occurred_at": "2026-09-15T11:00:00+00:00", "seal_no": "SEAL-1",
    }).get_json()
    assert scan["requires_manual_review"] is True

    recovered = client.post("/system/recover").get_json()
    assert any(item["result"] == "timeout_flagged" for item in recovered["processed"])
    assert client.get("/requests/TR-1?role=store").get_json()["status"] == "overdue"
    assert client.get("/audit?role=audit").get_json()["open_investigation_count"] >= 2


def test_domain_error_serialized(client):
    s1, s2, c1 = _setup_world(client)
    resp = client.post("/requests", json={"request_no": "TR-X", "batch_no": "B-404"})
    assert resp.status_code == 400  # 缺字段
    resp = client.post("/admin/genesis", json={
        "batch_no": "B-404", "store_id": s1, "quantity": 1, "actor": "a",
    })
    assert resp.status_code == 404
    assert resp.get_json()["error"]["code"] == "batch_not_found"
    resp = client.get("/requests/NOPE?role=store")
    assert resp.status_code == 404
    assert resp.get_json()["error"]["code"] == "request_not_found"
