"""申请创建期的校验：许可、资质、关系、批号数量缺一不可。"""

from helpers import DISPATCH_AT, RECEIVE_AT


def _post(client, **overrides):
    body = {
        "sender_code": "S1",
        "receiver_code": "S2",
        "carrier_code": "C1",
        "planned_dispatched_at": DISPATCH_AT,
        "planned_received_at": RECEIVE_AT,
        "lines": [{"product_code": "D1", "batch_number": "B2026-01", "planned_qty": 10}],
    }
    body.update(overrides)
    return client.post("/transfers", json=body, headers={"X-Actor-Id": "s1staff"})


def test_unknown_batch_rejected(client, seed):
    resp = _post(client, lines=[{"product_code": "D1", "batch_number": "NOPE", "planned_qty": 1}])
    assert resp.status_code == 422
    assert "批号" in resp.get_json()["message"]


def test_over_reservation_rejected(client, seed):
    assert _post(client, lines=[{"product_code": "D1", "batch_number": "B2026-01", "planned_qty": 60}]).status_code == 200
    resp = _post(client, lines=[{"product_code": "D1", "batch_number": "B2026-01", "planned_qty": 60}])
    assert resp.status_code == 422
    assert "可冻结数量不足" in resp.get_json()["message"]


def test_unqualified_carrier_rejected(client, seed):
    resp = _post(client, carrier_code="S2")  # S2 是门店，不是承运人
    assert resp.status_code == 422
    assert "CARRIER" in resp.get_json()["message"]


def test_other_store_cannot_create_transfer(client, seed):
    # 新建无关系门店 S3 店员，不能代 S1 发起。
    client.post("/admin/parties", json={"code": "S3", "name": "郊县", "type": "STORE", "jurisdiction": "TJ"},
                headers={"X-Actor-Id": "admin"})
    client.post("/admin/actors", json={"actor_id": "s3", "display_name": "x", "role": "STORE_STAFF", "party_code": "S3"},
                headers={"X-Actor-Id": "admin"})
    resp = client.post("/transfers", json={
        "sender_code": "S1", "receiver_code": "S2", "carrier_code": "C1",
        "planned_dispatched_at": DISPATCH_AT, "planned_received_at": RECEIVE_AT,
        "lines": [{"product_code": "D1", "batch_number": "B2026-01", "planned_qty": 1}],
    }, headers={"X-Actor-Id": "s3"})
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "FORBIDDEN"


def test_non_positive_qty_rejected(client, seed):
    resp = _post(client, lines=[{"product_code": "D1", "batch_number": "B2026-01", "planned_qty": 0}])
    assert resp.status_code == 422


def test_expired_planned_window_rejected(client, seed):
    resp = _post(
        client,
        planned_dispatched_at="2001-01-01T00:00:00+00:00",
        planned_received_at="2001-01-01T03:00:00+00:00",
    )
    assert resp.status_code == 422
    assert "许可" in resp.get_json()["message"] or "关系" in resp.get_json()["message"]
