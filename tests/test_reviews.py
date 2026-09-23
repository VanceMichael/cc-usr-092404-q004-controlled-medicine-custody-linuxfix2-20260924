"""双人复核与出库门槛。"""

from helpers import approve, create_transfer, dispatch


def test_second_review_requires_first(client, seed):
    number = create_transfer(client)
    resp = client.post(
        f"/transfers/{number}/reviews",
        json={"review_role": "SECOND", "decision": "APPROVE"},
        headers={"X-Actor-Id": "s2mgr"},
    )
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "FIRST_REVIEW_REQUIRED"


def test_dual_control_rejects_same_reviewer(client, seed):
    number = create_transfer(client)
    client.post(
        f"/transfers/{number}/reviews",
        json={"review_role": "FIRST", "decision": "APPROVE"},
        headers={"X-Actor-Id": "s1mgr"},
    )
    resp = client.post(
        f"/transfers/{number}/reviews",
        json={"review_role": "SECOND", "decision": "APPROVE"},
        headers={"X-Actor-Id": "s1mgr"},
    )
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "DUAL_CONTROL"


def test_review_slot_cannot_be_changed(client, seed):
    number = create_transfer(client)
    client.post(
        f"/transfers/{number}/reviews",
        json={"review_role": "FIRST", "decision": "APPROVE"},
        headers={"X-Actor-Id": "s1mgr"},
    )
    resp = client.post(
        f"/transfers/{number}/reviews",
        json={"review_role": "FIRST", "decision": "REJECT"},
        headers={"X-Actor-Id": "s2mgr"},
    )
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "REVIEW_EXISTS"


def test_dispatch_blocked_without_two_approvals(client, seed):
    number = create_transfer(client)
    resp = client.post(
        f"/transfers/{number}/dispatch",
        json={"seal_code": "SEAL-1"},
        headers={"X-Actor-Id": "s1staff"},
    )
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "DUAL_CONTROL"


def test_first_reject_cancels_and_blocks_second(client, seed):
    number = create_transfer(client)
    resp = client.post(
        f"/transfers/{number}/reviews",
        json={"review_role": "FIRST", "decision": "REJECT", "note": "批号存疑"},
        headers={"X-Actor-Id": "s1mgr"},
    )
    assert resp.get_json()["status"] == "CANCELLED"
    resp = client.post(
        f"/transfers/{number}/reviews",
        json={"review_role": "SECOND", "decision": "APPROVE"},
        headers={"X-Actor-Id": "s2mgr"},
    )
    assert resp.status_code == 409


def test_reviewer_without_permission_rejected(client, seed):
    number = create_transfer(client)
    resp = client.post(
        f"/transfers/{number}/reviews",
        json={"review_role": "FIRST", "decision": "APPROVE"},
        headers={"X-Actor-Id": "s1staff"},  # can_review=False
    )
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "FORBIDDEN"


def test_suspended_license_blocks_dispatch_then_revalidate_after_restore(client, seed, app):
    number = create_transfer(client)
    approve(client, number)

    # 暂停发出店许可：未出库申请立即重验失效。
    eng = app.extensions["test_engine"]
    from sqlalchemy import select
    from pharmacy_identity.schema import licenses

    with eng.begin() as conn:
        lid = conn.execute(
            select(licenses.c.id).where(licenses.c.party_code == "S1")
        ).scalar_one()
    resp = client.post(
        f"/admin/licenses/{lid}/status",
        json={"status": "SUSPENDED", "note": "飞行检查"},
        headers={"X-Actor-Id": "admin"},
    )
    assert resp.status_code == 200
    blocked = client.post(
        f"/transfers/{number}/dispatch",
        json={"seal_code": "SEAL-1"},
        headers={"X-Actor-Id": "s1staff"},
    )
    assert blocked.status_code == 409
    assert blocked.get_json()["code"] == "SNAPSHOT_INVALID"

    # 恢复许可：同一未出库申请重验通过，可以出库。
    client.post(
        f"/admin/licenses/{lid}/status",
        json={"status": "ACTIVE", "note": "整改完成"},
        headers={"X-Actor-Id": "admin"},
    )
    assert dispatch(client, number)["status"] == "DISPATCHED"
