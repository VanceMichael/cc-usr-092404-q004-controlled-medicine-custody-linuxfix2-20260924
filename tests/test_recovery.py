"""恢复运行后的超时补扫与 HTTP 幂等重放。"""

from datetime import timedelta

from pharmacy_identity import clock

from helpers import LATE_AT, approve, conservation, create_transfer, dispatch


def test_timeout_sweep_quarantines_overdue_in_transit(client, seed):
    overdue = create_transfer(client, qty=7)
    approve(client, overdue)
    dispatch(client, overdue, seal="SEAL-LATE")

    # 计划接收时刻在扫描时刻之后：不得误隔离。
    later = (clock.parse(LATE_AT) + timedelta(days=1)).isoformat()
    fresh = create_transfer(client, qty=1, dispatch_at=LATE_AT, receive_at=later)
    approve(client, fresh)
    dispatch(client, fresh, seal="SEAL-FRESH", at=LATE_AT)

    resp = client.post(
        "/system/timeout-sweep",
        json={"at": LATE_AT},
        headers={"X-Actor-Id": "admin"},
    )
    assert resp.status_code == 200, resp.get_json()
    swept = resp.get_json()["quarantined"]
    assert any(item["request_number"] == overdue and item["case_number"].startswith("INV-") for item in swept)
    assert not any(item["request_number"] == fresh for item in swept)

    detail = client.get(f"/transfers/{overdue}", headers={"X-Actor-Id": "aud1"}).get_json()
    assert detail["status"] == "QUARANTINED"
    assert detail["open_investigations"], "超时应自动创建调查"

    holders = client.get(
        "/batches/D1/B2026-01/location", headers={"X-Actor-Id": "admin"}
    ).get_json()["holders"]
    q = sum(h["qty"] for h in holders if h["holder_type"] == "QUARANTINE")
    assert q == 7
    assert conservation(client)["ok"]


def test_timeout_sweep_idempotent_when_run_again(client, seed):
    number = create_transfer(client, qty=4)
    approve(client, number)
    dispatch(client, number, seal="SEAL-SW")
    first = client.post("/system/timeout-sweep", json={"at": LATE_AT},
                        headers={"X-Actor-Id": "admin"}).get_json()
    assert any(item["request_number"] == number for item in first["quarantined"])
    second = client.post("/system/timeout-sweep", json={"at": LATE_AT},
                         headers={"X-Actor-Id": "admin"}).get_json()
    assert second["quarantined"] == []  # 已处理的不会重复隔离


def test_http_idempotency_key_replays_first_response(client, seed):
    body = {
        "sender_code": "S1", "receiver_code": "S2", "carrier_code": "C1",
        "planned_dispatched_at": "2026-12-31T00:00:00+00:00",
        "planned_received_at": "2026-12-31T05:00:00+00:00",
        "lines": [{"product_code": "D1", "batch_number": "B2026-01", "planned_qty": 6}],
    }
    headers = {"X-Actor-Id": "s1staff", "Idempotency-Key": "IDEM-001"}
    r1 = client.post("/transfers", json=body, headers=headers)
    r2 = client.post("/transfers", json=body, headers=headers)
    assert r1.status_code == r2.status_code == 200
    assert r1.get_json()["request_number"] == r2.get_json()["request_number"]

    listing = client.get("/transfers", headers={"X-Actor-Id": "aud1"}).get_json()
    assert len(listing) == 1  # 只创建了一单


def test_idempotency_key_rejects_different_payload(client, seed):
    headers = {"X-Actor-Id": "s1staff", "Idempotency-Key": "IDEM-002"}
    body1 = {
        "sender_code": "S1", "receiver_code": "S2", "carrier_code": "C1",
        "planned_dispatched_at": "2026-12-31T00:00:00+00:00",
        "planned_received_at": "2026-12-31T05:00:00+00:00",
        "lines": [{"product_code": "D1", "batch_number": "B2026-01", "planned_qty": 6}],
    }
    client.post("/transfers", json=body1, headers=headers)
    body2 = dict(body1)
    body2["lines"] = [{"product_code": "D1", "batch_number": "B2026-01", "planned_qty": 9}]
    resp = client.post("/transfers", json=body2, headers=headers)
    assert resp.status_code == 422
    assert resp.get_json()["code"] == "IDEMPOTENCY_CONFLICT"
