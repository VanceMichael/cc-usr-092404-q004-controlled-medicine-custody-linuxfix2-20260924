"""设备离线扫码、越节点转人工、重复幂等与双终端封签竞争。"""

import threading

from sqlalchemy import select

from helpers import MIDNIGHT_TRANSIT, approve, create_transfer, dispatch
from pharmacy_identity.schema import custody_events, manual_reviews, transfer_requests


def _scan(client, number, *, key="SCAN-1", at=MIDNIGHT_TRANSIT, seal=None, device="DEV-1"):
    return client.post(
        f"/transfers/{number}/scans",
        json={
            "device_id": device,
            "idempotency_key": key,
            "occurred_at": at,
            "location": "京哈检查站",
            "seal_code": seal,
        },
        headers={"X-Actor-Id": "car1"},
    )


def test_offline_scan_uses_real_occurrence_time(client, seed, app):
    number = create_transfer(client)
    approve(client, number)
    dispatch(client, number)

    # 设备离线时的检查点扫描，恢复后补传，携带真实发生时间。
    resp = _scan(client, number)
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["result"] == "SCAN"

    eng = app.extensions["test_engine"]
    with eng.connect() as conn:
        row = conn.execute(
            select(custody_events.c.occurred_at, custody_events.c.recorded_at)
            .where(custody_events.c.event_type == "SCAN")
        ).first()
    assert row[0] == MIDNIGHT_TRANSIT
    assert row[0] != row[1]  # 发生时间 != 入库时间


def test_scan_before_dispatch_is_fact_but_manual_review(client, seed, app):
    number = create_transfer(client)
    approve(client, number)
    # 未出库先扫码：事实入链（不丢），但自动流转冻结，202 + 人工复核单。
    resp = _scan(client, number, at=MIDNIGHT_TRANSIT)
    assert resp.status_code == 202, resp.get_json()
    body = resp.get_json()
    assert body["code"] == "MANUAL_REVIEW_REQUIRED"
    review_id = body["manual_review_id"]

    eng = app.extensions["test_engine"]
    with eng.connect() as conn:
        scan = conn.execute(
            select(custody_events.c.id).where(custody_events.c.event_type == "SCAN")
        ).first()
        assert scan is not None  # 事实已落库
        review = conn.execute(
            select(manual_reviews).where(manual_reviews.c.id == review_id)
        ).mappings().one()
        assert review["status"] == "PENDING"
        assert review["reason"] == "NODE_SKIP"

    # 越节点期间不能直接出库以外的自动推进；人工处理后可继续。
    resolved = client.post(
        f"/manual-reviews/{review_id}/resolve",
        json={"resolution": "RISK_ACCEPTED", "note": "时钟错误，实际已出库"},
        headers={"X-Actor-Id": "admin"},
    )
    assert resolved.status_code == 200


def test_seal_mismatch_scan_goes_manual(client, seed):
    number = create_transfer(client)
    approve(client, number)
    dispatch(client, number, seal="SEAL-1")
    resp = _scan(client, number, seal="SEAL-OTHER")
    assert resp.status_code == 202
    assert resp.get_json()["manual_review_id"]


def test_duplicate_scan_returns_original_result(client, seed):
    number = create_transfer(client)
    approve(client, number)
    dispatch(client, number)
    first = _scan(client, number, key="DUP-1")
    assert first.status_code == 200
    first_id = first.get_json()["event_id"]

    # 同一设备同一幂等键重复扫码：不产生新事件，返回原结果。
    second = _scan(client, number, key="DUP-1")
    assert second.status_code == 200
    assert second.get_json()["duplicate"] is True
    assert second.get_json()["event_id"] == first_id

    third = _scan(client, number, key="DUP-1", at="2026-09-24T01:00:00+00:00")
    assert third.get_json()["event_id"] == first_id


def test_different_devices_same_key_are_distinct_scans(client, seed):
    number = create_transfer(client)
    approve(client, number)
    dispatch(client, number)
    r1 = _scan(client, number, key="K", device="DEV-A")
    r2 = _scan(client, number, key="K", device="DEV-B")
    assert r1.status_code == r2.status_code == 200
    assert r1.get_json()["event_id"] != r2.get_json()["event_id"]


def test_reuse_seal_on_dispatch_rejected(client, seed, app):
    """出库封签全局唯一：第二单不得复用同一封签。"""
    first = create_transfer(client, qty=2)
    approve(client, first)
    assert dispatch(client, first, seal="SHARED-SEAL")["status"] == "DISPATCHED"

    second = create_transfer(client, qty=3)
    approve(client, second)
    resp = client.post(
        f"/transfers/{second}/dispatch",
        json={"seal_code": "SHARED-SEAL"},
        headers={"X-Actor-Id": "s1staff"},
    )
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "SEAL_ALREADY_DISPATCHED"
    eng = app.extensions["test_engine"]
    with eng.connect() as conn:
        assert conn.execute(
            select(transfer_requests.c.status).where(
                transfer_requests.c.request_number == second
            )
        ).scalar_one() == "PLANNED"


def test_concurrent_receive_same_seal_one_wins(tmp_path):
    """清洁签收占用封签：并发双终端只有一次签收成功（文件库 + WAL）。"""
    from sqlalchemy import create_engine

    from pharmacy_identity import create_app
    from pharmacy_identity.schema import append_only_triggers, metadata
    from sqlalchemy import text as sa_text
    from helpers import seed_database

    db = tmp_path / "race.sqlite3"
    eng = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})
    with eng.begin() as conn:
        metadata.create_all(conn)
        for ddl in append_only_triggers():
            conn.execute(sa_text(ddl))
        seed_database(conn)
    application = create_app(eng)
    flask_client = application.test_client()

    number = create_transfer(flask_client, qty=4)
    approve(flask_client, number)
    dispatch(flask_client, number, seal="RACE-SEAL")

    results: list = []
    barrier = threading.Barrier(2)

    def confirm():
        c = application.test_client()
        barrier.wait()
        resp = c.post(
            f"/transfers/{number}/receive",
            json={
                "seal_code": "RACE-SEAL",
                "lines": [{"product_code": "D1", "batch_number": "B2026-01",
                           "received_qty": 4, "damaged_qty": 0}],
            },
            headers={"X-Actor-Id": "s2staff"},
        )
        results.append(resp.status_code)

    t1 = threading.Thread(target=confirm)
    t2 = threading.Thread(target=confirm)
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(results) == [200, 409], results
    holders = flask_client.get(
        "/batches/D1/B2026-01/location", headers={"X-Actor-Id": "admin"}
    ).get_json()["holders"]
    received = sum(h["qty"] for h in holders if h["holder_code"] == "S2")
    assert received == 4
