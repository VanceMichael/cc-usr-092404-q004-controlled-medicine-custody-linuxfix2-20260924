"""端到端工作流辅助。"""

from datetime import datetime, timedelta, timezone

from pharmacy_identity import directory


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


# 全部时刻相对运行时刻生成，保证“基线登记 → 出库 → 在途 → 签收”因果有序。
_NOW = datetime.now(timezone.utc).replace(microsecond=0)
DISPATCH_AT = _iso(_NOW + timedelta(hours=1))
MIDNIGHT_TRANSIT = _iso(_NOW + timedelta(hours=3))
RECEIVE_AT = _iso(_NOW + timedelta(hours=5))
LATE_AT = _iso(_NOW + timedelta(days=2))


def seed_database(conn):
    """与 tests/conftest.py 中 seed 相同的标准主数据，供独立数据库测试复用。"""
    directory.register_party(conn, "S1", "城南门店", "STORE", "BJ")
    directory.register_party(conn, "S2", "城北门店", "STORE", "BJ")
    directory.register_party(conn, "C1", "城际承运", "CARRIER", "BJ")
    directory.register_quarantine(conn, "Q1", "市级隔离库", "BJ")
    directory.register_actor(conn, "admin", "管理员", "ADMIN")
    directory.register_actor(conn, "s1mgr", "城南店长", "STORE_STAFF", "S1", can_review=True)
    directory.register_actor(conn, "s2mgr", "城北店长", "STORE_STAFF", "S2", can_review=True)
    directory.register_actor(conn, "s1staff", "城南店员", "STORE_STAFF", "S1")
    directory.register_actor(conn, "s2staff", "城北店员", "STORE_STAFF", "S2")
    directory.register_actor(conn, "car1", "承运司机", "CARRIER_STAFF", "C1")
    directory.register_actor(conn, "aud1", "合规审计", "AUDITOR")
    directory.register_drug(conn, "D1", "盐酸哌甲酯片", "第一类")
    directory.register_batch(conn, "D1", "B2026-01", 100, "S1", expiry_date="2027-01-01")
    directory.register_batch(conn, "D1", "B2026-02", 20, "S1", expiry_date="2027-06-01")
    for party, ltype, number in (
        ("S1", "STORE_CONTROLLED_DRUG", "LIC-S1"),
        ("S2", "STORE_CONTROLLED_DRUG", "LIC-S2"),
        ("C1", "CARRIER_QUALIFICATION", "LIC-C1"),
    ):
        directory.add_license(
            conn, party, ltype, number,
            "2020-01-01T00:00:00+00:00", "2030-01-01T00:00:00+00:00",
        )
    directory.add_relationship(
        conn, "S1", "C1", "STORE_CARRIER",
        "2020-01-01T00:00:00+00:00", "2030-01-01T00:00:00+00:00",
    )
    directory.add_relationship(
        conn, "S1", "S2", "SENDER_RECEIVER",
        "2020-01-01T00:00:00+00:00", "2030-01-01T00:00:00+00:00",
    )


def create_transfer(
    client, actor="s1staff", *, qty=10, batch="B2026-01",
    dispatch_at=DISPATCH_AT, receive_at=RECEIVE_AT,
    sender="S1", receiver="S2", carrier="C1",
):
    resp = client.post(
        "/transfers",
        json={
            "sender_code": sender,
            "receiver_code": receiver,
            "carrier_code": carrier,
            "planned_dispatched_at": dispatch_at,
            "planned_received_at": receive_at,
            "lines": [{"product_code": "D1", "batch_number": batch, "planned_qty": qty}],
        },
        headers={"X-Actor-Id": actor},
    )
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["request_number"]


def approve(client, number, first="s1mgr", second="s2mgr"):
    r1 = client.post(
        f"/transfers/{number}/reviews",
        json={"review_role": "FIRST", "decision": "APPROVE"},
        headers={"X-Actor-Id": first},
    )
    assert r1.status_code == 200, r1.get_json()
    r2 = client.post(
        f"/transfers/{number}/reviews",
        json={"review_role": "SECOND", "decision": "APPROVE"},
        headers={"X-Actor-Id": second},
    )
    assert r2.status_code == 200, r2.get_json()


def dispatch(client, number, *, actor="s1staff", seal="SEAL-1", at=DISPATCH_AT, **extra):
    resp = client.post(
        f"/transfers/{number}/dispatch",
        json={"seal_code": seal, "occurred_at": at, "driver_name": "李司机", "vehicle_no": "京A12345", **extra},
        headers={"X-Actor-Id": actor},
    )
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def clean_receive(client, number, *, qty=10, batch="B2026-01", actor="s2staff", at=RECEIVE_AT, seal="SEAL-1"):
    resp = client.post(
        f"/transfers/{number}/receive",
        json={
            "occurred_at": at,
            "seal_code": seal,
            "lines": [{"product_code": "D1", "batch_number": batch, "received_qty": qty, "damaged_qty": 0}],
        },
        headers={"X-Actor-Id": actor},
    )
    return resp


def conservation(client, actor="admin"):
    resp = client.get("/audit/conservation", headers={"X-Actor-Id": actor})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def locate(client, product="D1", batch="B2026-01", at=None, actor="admin"):
    url = f"/batches/{product}/{batch}/location"
    if at:
        from urllib.parse import quote

        url += f"?at={quote(at, safe='')}"
    resp = client.get(url, headers={"X-Actor-Id": actor})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["holders"]
