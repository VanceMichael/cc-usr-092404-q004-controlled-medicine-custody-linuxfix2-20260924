"""许可/关系纠正：只重验未出库申请，对已完成交接追加风险说明。"""

from sqlalchemy import select

from helpers import approve, clean_receive, conservation, create_transfer, dispatch
from pharmacy_identity.schema import (
    licenses,
    relationships,
    risk_notes,
    transfer_requests,
)


def _license_id(app, party_code, license_type="STORE_CONTROLLED_DRUG"):
    eng = app.extensions["test_engine"]
    with eng.connect() as conn:
        return conn.execute(
            select(licenses.c.id)
            .where(licenses.c.party_code == party_code)
            .where(licenses.c.license_type == license_type)
            .where(licenses.c.status == "ACTIVE")
        ).scalar_one()


def _rel_id(app, party="S1", related="C1"):
    eng = app.extensions["test_engine"]
    with eng.connect() as conn:
        return conn.execute(
            select(relationships.c.id)
            .where(relationships.c.party_code == party)
            .where(relationships.c.related_party_code == related)
        ).scalar_one()


def _risk_notes(app, number):
    eng = app.extensions["test_engine"]
    with eng.connect() as conn:
        rid = conn.execute(
            select(transfer_requests.c.id).where(
                transfer_requests.c.request_number == number
            )
        ).scalar_one()
        return conn.execute(
            select(risk_notes.c.category, risk_notes.c.note)
            .where(risk_notes.c.request_id == rid)
        ).all()


def test_license_correction_revalidates_planned_and_risknotes_completed(client, seed, app):
    planned_no = create_transfer(client, qty=2)   # 仍未出库
    approve(client, planned_no)
    done_no = create_transfer(client, qty=3)
    approve(client, done_no)
    dispatch(client, done_no, seal="SEAL-DONE")
    assert clean_receive(client, done_no, qty=3, seal="SEAL-DONE").status_code == 200

    lid = _license_id(app, "C1", "CARRIER_QUALIFICATION")
    # 把承运资质的有效期纠正到调拨之后：未出库申请失效；已完成只加风险说明。
    resp = client.post(
        f"/admin/licenses/{lid}/correct",
        json={
            "license_number": "LIC-C1-NEW",
            "valid_from": "2030-01-01T00:00:00+00:00",
            "valid_to": "2031-01-01T00:00:00+00:00",
            "note": "资质续期登记错误，实际 2030 年生效",
        },
        headers={"X-Actor-Id": "admin"},
    )
    assert resp.status_code == 200, resp.get_json()

    eng = app.extensions["test_engine"]
    with eng.connect() as conn:
        validity = {
            n: conn.execute(
                select(transfer_requests.c.snapshot_valid).where(
                    transfer_requests.c.request_number == n
                )
            ).scalar_one()
            for n in (planned_no, done_no)
        }
    assert validity[planned_no] == 0
    assert validity[done_no] == 1  # 已完成不重写

    # 未出库申请被冻结，出库被拒。
    blocked = client.post(
        f"/transfers/{planned_no}/dispatch",
        json={"seal_code": "SEAL-X"},
        headers={"X-Actor-Id": "s1staff"},
    )
    assert blocked.status_code == 409
    assert blocked.get_json()["code"] == "SNAPSHOT_INVALID"

    notes = _risk_notes(app, done_no)
    assert any(n[0] == "LICENSE_CORRECTED" for n in notes)

    # 历史守恒不被纠正动作破坏。
    assert conservation(client)["ok"]


def test_old_license_version_preserved_as_corrected(client, seed, app):
    lid = _license_id(app, "S1")
    client.post(
        f"/admin/licenses/{lid}/correct",
        json={
            "license_number": "LIC-S1-FIX",
            "valid_from": "2021-01-01T00:00:00+00:00",
            "valid_to": "2031-01-01T00:00:00+00:00",
            "note": "编号更正",
        },
        headers={"X-Actor-Id": "admin"},
    )
    eng = app.extensions["test_engine"]
    with eng.connect() as conn:
        old = conn.execute(select(licenses).where(licenses.c.id == lid)).mappings().one()
        versions = conn.execute(
            select(licenses.c.status, licenses.c.license_number)
            .where(licenses.c.party_code == "S1")
        ).all()
    assert old["status"] == "CORRECTED"
    assert old["license_number"] == "LIC-S1"  # 旧行原样保留
    assert ("ACTIVE", "LIC-S1-FIX") in versions


def test_relationship_correction_only_affects_unshipped(client, seed, app):
    planned_no = create_transfer(client, qty=1)
    approve(client, planned_no)

    rid = _rel_id(app, "S1", "S2")
    resp = client.post(
        f"/admin/relationships/{rid}/correct",
        json={
            "related_party_code": "S2",
            "valid_from": "2030-01-01T00:00:00+00:00",
            "valid_to": "2031-01-01T00:00:00+00:00",
            "note": "收发货关系生效日登记错误",
        },
        headers={"X-Actor-Id": "admin"},
    )
    assert resp.status_code == 200
    blocked = client.post(
        f"/transfers/{planned_no}/dispatch",
        json={"seal_code": "SEAL-Y"},
        headers={"X-Actor-Id": "s1staff"},
    )
    assert blocked.status_code == 409
    assert blocked.get_json()["code"] == "SNAPSHOT_INVALID"
    assert conservation(client)["ok"]


def test_risk_note_added_when_correction_hits_in_transit(client, seed, app):
    number = create_transfer(client, qty=4)
    approve(client, number)
    dispatch(client, number, seal="SEAL-IT")  # 在途

    lid = _license_id(app, "C1", "CARRIER_QUALIFICATION")
    client.post(
        f"/admin/licenses/{lid}/correct",
        json={
            "license_number": "LIC-C1-2",
            "valid_from": "2030-01-01T00:00:00+00:00",
            "valid_to": "2031-01-01T00:00:00+00:00",
            "note": "在途期间纠正资质",
        },
        headers={"X-Actor-Id": "admin"},
    )
    notes = _risk_notes(app, number)
    assert any("资质" in n[1] for n in notes)
    # 在途状态不被后台纠正打断。
    resp = client.get(f"/transfers/{number}", headers={"X-Actor-Id": "aud1"})
    assert resp.get_json()["status"] == "DISPATCHED"
