"""短少、破损、超时、封签异常：隔离 + 调查 + 守恒。"""

from helpers import LATE_AT, RECEIVE_AT, approve, conservation, create_transfer, dispatch, locate


def _full_flow(client, qty=10, seal="SEAL-A"):
    number = create_transfer(client, qty=qty)
    approve(client, number)
    dispatch(client, number, seal=seal)
    return number


def _receive(client, number, lines, *, seal="SEAL-A", at=RECEIVE_AT):
    return client.post(
        f"/transfers/{number}/receive",
        json={"occurred_at": at, "seal_code": seal, "lines": lines},
        headers={"X-Actor-Id": "s2staff"},
    )


def test_shortage_quarantines_and_opens_investigation(client, seed):
    number = _full_flow(client, qty=10)
    resp = _receive(client, number, [
        {"product_code": "D1", "batch_number": "B2026-01", "received_qty": 8, "damaged_qty": 0}
    ])
    body = resp.get_json()
    assert resp.status_code == 200, body
    assert body["status"] == "QUARANTINED"
    assert "SHORTAGE" in body["reasons"]
    assert body["case_number"].startswith("INV-")

    # 8 在接收店，2 计 LOSS，承运人归零，总量守恒。
    by = {(h["holder_type"], h["holder_code"]): h["qty"] for h in locate(client)}
    assert by[("STORE", "S1")] == 90
    assert by[("STORE", "S2")] == 8
    assert by[("LOSS", "LOSS")] == 2
    assert ("CARRIER", "C1") not in by

    report = conservation(client)
    assert report["ok"], report["problems"]
    batch = next(b for b in report["batches"] if b["batch_number"] == "B2026-01")
    assert batch["active_qty"] == 98
    assert batch["loss_qty"] == 2


def test_damage_quarantines_damaged_only_and_releases_good(client, seed):
    number = _full_flow(client, qty=10)
    resp = _receive(client, number, [
        {"product_code": "D1", "batch_number": "B2026-01", "received_qty": 7, "damaged_qty": 3}
    ])
    body = resp.get_json()
    assert body["status"] == "QUARANTINED"
    assert body["reasons"] == ["DAMAGE"]

    by = {(h["holder_type"], h["holder_code"]): h["qty"] for h in locate(client)}
    assert by[("STORE", "S2")] == 7
    assert by[("QUARANTINE", "Q1")] == 3
    assert ("LOSS", "LOSS") not in by
    assert conservation(client)["ok"]


def test_timeout_quarantines_whole_shipment(client, seed):
    number = _full_flow(client, qty=10)
    late = LATE_AT
    resp = _receive(
        client, number,
        [{"product_code": "D1", "batch_number": "B2026-01", "received_qty": 10, "damaged_qty": 0}],
        at=late,
    )
    assert resp.get_json()["status"] == "QUARANTINED"
    assert "TIMEOUT" in resp.get_json()["reasons"]
    by = {(h["holder_type"], h["holder_code"]): h["qty"] for h in locate(client)}
    assert by[("QUARANTINE", "Q1")] == 10
    assert ("STORE", "S2") not in by


def test_seal_mismatch_quarantines_whole_shipment(client, seed):
    number = _full_flow(client, qty=10, seal="SEAL-A")
    resp = _receive(
        client, number,
        [{"product_code": "D1", "batch_number": "B2026-01", "received_qty": 10, "damaged_qty": 0}],
        seal="SEAL-FORGED",
    )
    assert resp.get_json()["status"] == "QUARANTINED"
    assert "SEAL_MISMATCH" in resp.get_json()["reasons"]
    by = {(h["holder_type"], h["holder_code"]): h["qty"] for h in locate(client)}
    assert by[("QUARANTINE", "Q1")] == 10


def test_investigation_release_to_receiver(client, seed):
    number = _full_flow(client, qty=10)
    case = _receive(client, number, [
        {"product_code": "D1", "batch_number": "B2026-01", "received_qty": 7, "damaged_qty": 3}
    ]).get_json()["case_number"]
    resp = client.post(
        f"/investigations/{case}/resolve",
        json={"outcome": "RELEASE_TO_RECEIVER", "note": "破损仅外包装，复检合格放行"},
        headers={"X-Actor-Id": "admin"},
    )
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["request_status"] == "DELIVERED"
    by = {(h["holder_type"], h["holder_code"]): h["qty"] for h in locate(client)}
    assert by[("STORE", "S2")] == 10
    assert ("QUARANTINE", "Q1") not in by
    assert conservation(client)["ok"]


def test_investigation_return_to_sender_restores_stock(client, seed):
    number = _full_flow(client, qty=10)
    case = _receive(client, number, [
        {"product_code": "D1", "batch_number": "B2026-01", "received_qty": 10, "damaged_qty": 0}
    ], at=LATE_AT).get_json()["case_number"]
    resp = client.post(
        f"/investigations/{case}/resolve",
        json={"outcome": "RETURN_TO_SENDER", "note": "超时退回"},
        headers={"X-Actor-Id": "admin"},
    )
    assert resp.get_json()["request_status"] == "RETURNED"
    by = {(h["holder_type"], h["holder_code"]): h["qty"] for h in locate(client)}
    # 补偿回发出店：100 全部回到 S1。
    assert by[("STORE", "S1")] == 100
    assert ("CARRIER", "C1") not in by
    assert ("QUARANTINE", "Q1") not in by
    assert conservation(client)["ok"]


def test_investigation_confirm_loss(client, seed):
    number = _full_flow(client, qty=10)
    case = _receive(client, number, [
        {"product_code": "D1", "batch_number": "B2026-01", "received_qty": 7, "damaged_qty": 3}
    ]).get_json()["case_number"]
    resp = client.post(
        f"/investigations/{case}/resolve",
        json={"outcome": "CONFIRM_LOSS", "note": "破损不可用，监销"},
        headers={"X-Actor-Id": "admin"},
    )
    assert resp.get_json()["request_status"] == "QUARANTINED"
    by = {(h["holder_type"], h["holder_code"]): h["qty"] for h in locate(client)}
    assert by[("LOSS", "LOSS")] == 3
    assert by[("STORE", "S2")] == 7
    assert conservation(client)["ok"]


def test_receive_more_than_in_transit_rejected(client, seed):
    number = _full_flow(client, qty=10)
    resp = _receive(client, number, [
        {"product_code": "D1", "batch_number": "B2026-01", "received_qty": 11, "damaged_qty": 0}
    ])
    assert resp.status_code == 422
    # 未产生任何签收：货仍在承运人手里。
    by = {(h["holder_type"], h["holder_code"]): h["qty"] for h in locate(client)}
    assert by[("CARRIER", "C1")] == 10
