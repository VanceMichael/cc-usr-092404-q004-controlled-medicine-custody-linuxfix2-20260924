"""角色字段最小化：门店、承运、审计各看履职所需。"""

from helpers import approve, clean_receive, create_transfer, dispatch


def _delivered(client, seal="SEAL-V"):
    number = create_transfer(client, qty=5)
    approve(client, number)
    dispatch(client, number, seal=seal)
    assert clean_receive(client, number, qty=5, seal=seal).status_code == 200
    return number


def test_store_staff_sees_own_request_only(client, seed):
    number = _delivered(client)
    resp = client.get(f"/transfers/{number}", headers={"X-Actor-Id": "s1staff"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["my_role"] == "SENDER"
    assert "snapshot" not in body
    assert "reviews" not in body
    assert "risk_notes" not in body
    assert "carrier_name" in body
    # 门店可见批号数量与签收结果。
    line = body["lines"][0]
    assert "planned_qty" in line and "received_qty" in line
    assert "id" not in line


def test_unrelated_party_forbidden(client, seed):
    # 新建一家与本调拨无任何关系的门店及其店员。
    assert client.post(
        "/admin/parties",
        json={"code": "S3", "name": "郊县门店", "type": "STORE", "jurisdiction": "TJ"},
        headers={"X-Actor-Id": "admin"},
    ).status_code == 200
    assert client.post(
        "/admin/actors",
        json={"actor_id": "s3staff", "display_name": "郊县店员", "role": "STORE_STAFF", "party_code": "S3"},
        headers={"X-Actor-Id": "admin"},
    ).status_code == 200

    number = _delivered(client)
    resp = client.get(f"/transfers/{number}", headers={"X-Actor-Id": "s3staff"})
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "FORBIDDEN"


def test_carrier_sees_operational_fields_only(client, seed):
    number = create_transfer(client)
    approve(client, number)
    dispatch(client, number, seal="SEAL-CV")

    listing = client.get("/transfers", headers={"X-Actor-Id": "car1"})
    assert listing.status_code == 200
    assert len(listing.get_json()) == 1
    view = listing.get_json()[0]
    assert view["request_number"] == number
    assert "snapshot" not in view and "snapshot_valid" not in view
    assert view["seal_code"] == "SEAL-CV"
    assert view["lines"][0] == {"product_code": "D1", "batch_number": "B2026-01", "planned_qty": 10}

    chain = client.get(f"/transfers/{number}/chain", headers={"X-Actor-Id": "car1"})
    assert chain.status_code == 200
    for event in chain.get_json():
        assert "prev_hash" not in event
        assert "payload_json" not in event
        assert "items" not in event


def test_auditor_sees_everything_including_snapshot_and_hashes(client, seed):
    number = create_transfer(client)
    approve(client, number)
    dispatch(client, number, seal="SEAL-AV")

    resp = client.get(f"/transfers/{number}", headers={"X-Actor-Id": "aud1"})
    body = resp.get_json()
    assert "snapshot" in body
    assert "reviews" in body
    assert body["snapshot"]["licenses"]

    chain = client.get(f"/transfers/{number}/chain", headers={"X-Actor-Id": "aud1"})
    events = chain.get_json()
    dispatch_event = next(e for e in events if e["event_type"] == "DISPATCHED")
    assert dispatch_event["event_hash"]
    assert dispatch_event["items"]


def test_auditor_can_list_all_requests(client, seed):
    _delivered(client, "SEAL-1")
    _delivered(client, "SEAL-2")
    resp = client.get("/transfers", headers={"X-Actor-Id": "aud1"})
    assert len(resp.get_json()) == 2


def test_store_listing_scoped_to_party(client, seed):
    _delivered(client, "SEAL-1")
    s1 = client.get("/transfers", headers={"X-Actor-Id": "s1staff"})
    s2 = client.get("/transfers", headers={"X-Actor-Id": "s2staff"})
    assert len(s1.get_json()) == len(s2.get_json()) == 1
    assert s1.get_json()[0]["my_role"] == "SENDER"
    assert s2.get_json()[0]["my_role"] == "RECEIVER"


def test_anonymous_rejected(client, seed):
    assert client.get("/transfers").status_code == 401


def test_store_can_locate_in_transit_batch_but_carrier_only_when_holding(client, seed):
    number = create_transfer(client, qty=8)
    approve(client, number)
    dispatch(client, number, seal="SEAL-LOC")

    # 在途：发出店虽不再持有，但作为参与方可查看该批号位置。
    sender = client.get("/batches/D1/B2026-01/location", headers={"X-Actor-Id": "s1staff"})
    assert sender.status_code == 200
    holders = {(h["holder_type"], h["holder_code"]): h["qty"] for h in sender.get_json()["holders"]}
    assert holders[("CARRIER", "C1")] == 8

    # 与该单无关的门店 S3 看不到。
    client.post("/admin/parties", json={"code": "S3", "name": "郊县", "type": "STORE", "jurisdiction": "TJ"},
                headers={"X-Actor-Id": "admin"})
    client.post("/admin/actors", json={"actor_id": "s3", "display_name": "x", "role": "STORE_STAFF", "party_code": "S3"},
                headers={"X-Actor-Id": "admin"})
    denied = client.get("/batches/D1/B2026-01/location", headers={"X-Actor-Id": "s3"})
    assert denied.status_code == 403
