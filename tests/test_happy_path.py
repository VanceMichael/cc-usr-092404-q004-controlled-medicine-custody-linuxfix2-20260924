"""完整正常路径：冻结 → 双人复核 → 出库（唯一在途保管人）→ 跨午夜 → 清洁签收。"""

from helpers import (
    DISPATCH_AT,
    MIDNIGHT_TRANSIT,
    RECEIVE_AT,
    approve,
    clean_receive,
    conservation,
    create_transfer,
    dispatch,
    locate,
)


def test_happy_path_unique_custody_across_midnight(client, seed):
    number = create_transfer(client, qty=10)
    approve(client, number)
    dispatch(client, number)

    # 出库后：发出店 90、承运人 10，两边不同时持有同一单位。
    holders = locate(client)
    assert {"holder_type": "STORE", "holder_code": "S1", "qty": 90} in holders
    assert {"holder_type": "CARRIER", "holder_code": "C1", "qty": 10} in holders
    assert sum(h["qty"] for h in holders) == 100

    # 午夜时刻（跨天）在途归属仍然唯一且守恒。
    midnight = locate(client, at=MIDNIGHT_TRANSIT)
    assert midnight == [
        {"holder_type": "CARRIER", "holder_code": "C1", "qty": 10},
        {"holder_type": "STORE", "holder_code": "S1", "qty": 90},
    ]

    resp = clean_receive(client, number)
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["status"] == "DELIVERED"

    # 签收后：承运人归零，接收店持有 10。
    holders = locate(client)
    by = {(h["holder_type"], h["holder_code"]): h["qty"] for h in holders}
    assert by[("STORE", "S2")] == 10
    assert by[("STORE", "S1")] == 90
    assert ("CARRIER", "C1") not in by

    report = conservation(client)
    assert report["ok"], report["problems"]
    batch = next(b for b in report["batches"] if b["batch_number"] == "B2026-01")
    assert batch["baseline_qty"] == 100
    assert batch["active_qty"] == 100
    assert batch["loss_qty"] == 0
    assert batch["conserved"] is True


def test_custody_intervals_are_continuous(client, seed):
    number = create_transfer(client, qty=3)
    approve(client, number)
    dispatch(client, number, seal="SEAL-X")
    assert clean_receive(client, number, qty=3, seal="SEAL-X").status_code == 200

    resp = client.get(
        f"/transfers/{number}/custody-intervals", headers={"X-Actor-Id": "admin"}
    )
    assert resp.status_code == 200
    intervals = resp.get_json()["intervals"]
    # 承运人区间与接收店区间首尾相接。
    carrier = next(i for i in intervals if i["holder"] == ["CARRIER", "C1"])
    receiver = next(i for i in intervals if i["holder"] == ["STORE", "S2"])
    assert carrier["ended_at"] == receiver["started_at"]
    assert carrier["started_at"] == DISPATCH_AT
    assert receiver["ended_at"] is None


def test_request_freezes_license_and_batches(client, seed, app):
    number = create_transfer(client, qty=5)
    approve(client, number)

    import json
    from sqlalchemy import select
    from pharmacy_identity.schema import transfer_requests

    eng = app.extensions["test_engine"]
    with eng.connect() as conn:
        row = conn.execute(
            select(transfer_requests.c.snapshot_json)
            .where(transfer_requests.c.request_number == number)
        ).first()
    snapshot = json.loads(row[0])
    assert len(snapshot["licenses"]) == 3  # 两店许可 + 承运资质
    assert snapshot["lines"][0]["planned_qty"] == 5
    assert snapshot["planned_dispatched_at"] == DISPATCH_AT
    assert snapshot["planned_received_at"] == RECEIVE_AT
