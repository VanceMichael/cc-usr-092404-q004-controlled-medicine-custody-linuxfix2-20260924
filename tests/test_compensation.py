"""取消、拒收、退回：以补偿事件恢复库存，历史节点保留。"""

from sqlalchemy import select, text

from helpers import (
    DISPATCH_AT,
    RECEIVE_AT,
    approve,
    clean_receive,
    conservation,
    create_transfer,
    dispatch,
    locate,
)


def _events(app, number):
    from pharmacy_identity.schema import custody_events, transfer_requests

    eng = app.extensions["test_engine"]
    with eng.connect() as conn:
        rid = conn.execute(
            select(transfer_requests.c.id).where(
                transfer_requests.c.request_number == number
            )
        ).scalar_one()
        return conn.execute(
            select(custody_events.c.event_type)
            .where(custody_events.c.request_id == rid)
            .order_by(custody_events.c.id)
        ).all()


def test_cancel_releases_reservation_and_keeps_history(client, seed, app):
    # 第一单冻结全部 100；未取消前第二单冻结 100 必须失败。
    big = create_transfer(client, qty=100)
    resp = client.post(
        "/transfers",
        json={
            "sender_code": "S1", "receiver_code": "S2", "carrier_code": "C1",
            "planned_dispatched_at": DISPATCH_AT, "planned_received_at": RECEIVE_AT,
            "lines": [{"product_code": "D1", "batch_number": "B2026-01", "planned_qty": 100}],
        },
        headers={"X-Actor-Id": "s1staff"},
    )
    assert resp.status_code == 422

    cancelled = client.post(
        f"/transfers/{big}/cancel",
        json={"reason": "计划变更"},
        headers={"X-Actor-Id": "s1staff"},
    )
    assert cancelled.status_code == 200
    assert cancelled.get_json()["status"] == "CANCELLED"

    # 预留释放：第二单现在可以创建。
    again = create_transfer(client, qty=100)
    assert again

    # 库存仍全部在发出店；历史事件（含 CANCELLED）保留。
    by = {(h["holder_type"], h["holder_code"]): h["qty"] for h in locate(client)}
    assert by == {("STORE", "S1"): 100}
    types = [r[0] for r in _events(app, big)]
    assert "REQUESTED" in types and "CANCELLED" in types


def test_dispatch_after_cancel_rejected(client, seed):
    number = create_transfer(client)
    client.post(f"/transfers/{number}/cancel", json={}, headers={"X-Actor-Id": "s1staff"})
    resp = client.post(
        f"/transfers/{number}/dispatch",
        json={"seal_code": "S1"},
        headers={"X-Actor-Id": "s1staff"},
    )
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "NOT_PLANNED"


def test_rejection_returns_goods_to_sender_with_compensation(client, seed, app):
    number = create_transfer(client, qty=10)
    approve(client, number)
    dispatch(client, number, seal="SEAL-R")
    resp = client.post(
        f"/transfers/{number}/reject",
        json={"occurred_at": RECEIVE_AT, "seal_code": "SEAL-R", "reason": "订单已撤销"},
        headers={"X-Actor-Id": "s2staff"},
    )
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["status"] == "REJECTED"

    by = {(h["holder_type"], h["holder_code"]): h["qty"] for h in locate(client)}
    assert by == {("STORE", "S1"): 100}
    assert conservation(client)["ok"]

    types = [r[0] for r in _events(app, number)]
    assert types.count("RETURNED") >= 1
    # 拒收事件仍在链上，历史不删。
    assert "REJECTED" in types


def test_reject_with_broken_seal_goes_quarantine(client, seed):
    number = create_transfer(client, qty=10)
    approve(client, number)
    dispatch(client, number, seal="SEAL-R")
    resp = client.post(
        f"/transfers/{number}/reject",
        json={"occurred_at": RECEIVE_AT, "seal_code": "SEAL-BROKEN", "reason": "封签被拆"},
        headers={"X-Actor-Id": "s2staff"},
    )
    assert resp.get_json()["status"] == "QUARANTINED"
    assert resp.get_json()["case_number"]
    by = {(h["holder_type"], h["holder_code"]): h["qty"] for h in locate(client)}
    assert by[("QUARANTINE", "Q1")] == 10
    assert conservation(client)["ok"]


def test_return_after_delivery_is_two_legs(client, seed):
    number = create_transfer(client, qty=10)
    approve(client, number)
    dispatch(client, number, seal="SEAL-T")
    assert clean_receive(client, number, seal="SEAL-T").status_code == 200

    pickup = client.post(
        f"/transfers/{number}/return",
        json={"leg": "pickup", "seal_code": "SEAL-T2"},
        headers={"X-Actor-Id": "s2staff"},
    )
    assert pickup.status_code == 200
    assert pickup.get_json()["leg"] == "PICKUP"
    # 第一程后：承运人是唯一保管人。
    by = {(h["holder_type"], h["holder_code"]): h["qty"] for h in locate(client)}
    assert by[("CARRIER", "C1")] == 10
    assert by[("STORE", "S1")] == 90

    delivery = client.post(
        f"/transfers/{number}/return",
        json={"leg": "complete"},
        headers={"X-Actor-Id": "s1staff"},
    )
    assert delivery.status_code == 200
    assert delivery.get_json()["leg"] == "DELIVERED"
    by = {(h["holder_type"], h["holder_code"]): h["qty"] for h in locate(client)}
    assert by == {("STORE", "S1"): 100}
    assert conservation(client)["ok"]


def test_append_only_tables_reject_update_and_delete(client, seed, app):
    number = create_transfer(client)
    approve(client, number)
    dispatch(client, number, seal="SEAL-IMM")
    clean_receive(client, number, seal="SEAL-IMM")

    eng = app.extensions["test_engine"]
    import pytest

    with eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO risk_notes(request_id, category, note, actor_id, created_at) "
            "VALUES(1,'OTHER','测试追加','admin','t')"
        ))

    for statement in (
        "UPDATE custody_events SET note='tampered'",
        "DELETE FROM custody_items",
        "UPDATE reviews SET decision='REJECT'",
        "DELETE FROM risk_notes",
    ):
        with eng.connect() as conn:
            with pytest.raises(Exception) as exc:
                conn.execute(text(statement))
            assert "追加型" in str(exc.value)
