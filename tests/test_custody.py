"""保管链路领域不变量测试。"""

import threading

import pytest
from sqlalchemy import func, select

from pharmacy_identity.database import create_database_engine
from pharmacy_identity.models import custody_events, scan_records, seal_confirmations
from pharmacy_identity.service import CustodyService, DomainError
from pharmacy_identity.triggers import create_schema

from .conftest import happy_path


def test_happy_path_single_custodian_and_conservation(service, world):
    result = happy_path(service, world)
    assert result["status"] == "completed"

    located = service.locate_batch("B-001")
    holders = {h["custodian"]: h["quantity"] for h in located["holders"]}
    assert holders == {f"store:{world['s1']}": 90, f"store:{world['s2']}": 10}

    audit = service.audit()
    assert audit["ok"] is True
    assert audit["quantity_conserved"] is True
    assert audit["unique_inflight_custody"] is True
    batch = next(b for b in audit["batches"] if b["batch_no"] == "B-001")
    assert batch["genesis_qty"] == 100 and batch["held_qty"] == 100


def test_cross_midnight_unique_custody(service, world):
    # 跨午夜运输：23:50 出库，次日 00:20 签收（用过去日期，相对真实时钟恒为已发生）
    service.create_request(
        "TR-NIGHT", "B-001", 7, world["s1"], world["s2"], world["c1"],
        "u1", "2026-09-15T22:00:00+00:00", "2026-09-16T01:00:00+00:00",
    )
    service.review_request("TR-NIGHT", "u2", "approve")
    service.review_request("TR-NIGHT", "u3", "approve")
    service.release_request("TR-NIGHT", "SEAL-N", "u1", "T-A", occurred_at="2026-09-15T23:50:00+00:00")
    service.confirm_seal("TR-NIGHT", "SEAL-N", "u3", "T-B", occurred_at="2026-09-15T23:55:00+00:00")
    service.receive("TR-NIGHT", 7, "u3", "T-B", occurred_at="2026-09-16T00:20:00+00:00")

    before_midnight = service.locate_batch("B-001", at="2026-09-15T23:59:00+00:00")
    holders = {h["custodian"]: h["quantity"] for h in before_midnight["holders"]}
    # 午夜时这 7 件只能在承运人手里，两家门店都不能同时持有
    assert holders[f"carrier:{world['c1']}"] == 7
    assert f"store:{world['s2']}" not in holders


def test_request_freezes_snapshot_but_still_blocks_release_on_suspension(service, world):
    service.create_request(
        "TR-1", "B-001", 5, world["s1"], world["s2"], world["c1"],
        "u1", "2026-09-23T10:00:00+00:00", "2030-09-23T14:00:00+00:00",
    )
    service.review_request("TR-1", "u2", "approve")
    service.review_request("TR-1", "u3", "approve")
    req = service.get_request("TR-1")
    assert req["frozen"]["from_store"]["license_status"] == "active"

    # 出库前许可被暂停：冻结快照保留原样，但出库被现行状态拦截
    service.update_store_license(world["s1"], "suspended")
    with pytest.raises(DomainError) as exc:
        service.release_request("TR-1", "SEAL-1", "u1", "T-A")
    assert exc.value.code == "license_suspended"

    # 纠正后只重验未出库申请，通过即可继续
    service.update_store_license(world["s1"], "active")
    outcome = service.revalidate_after_correction("compliance", store_id=world["s1"])
    assert outcome["revalidated"] == [{"request_no": "TR-1", "valid": True}]
    service.release_request("TR-1", "SEAL-1", "u1", "T-A")
    assert service.get_request("TR-1")["status"] == "in_transit"


def test_two_person_review_rules(service, world):
    service.create_request(
        "TR-1", "B-001", 3, world["s1"], world["s2"], world["c1"],
        "u1", "2026-09-23T10:00:00+00:00", "2030-09-23T14:00:00+00:00",
    )
    with pytest.raises(DomainError) as exc:
        service.review_request("TR-1", "u1", "approve")
    assert exc.value.code == "self_review"
    service.review_request("TR-1", "u2", "approve")
    with pytest.raises(DomainError) as exc:
        service.review_request("TR-1", "u2", "approve")
    assert exc.value.code == "duplicate_review"
    # 仅一人复核不能出库
    with pytest.raises(DomainError) as exc:
        service.release_request("TR-1", "SEAL-1", "u1", "T-A")
    assert exc.value.code in ("review_incomplete", "not_approvable_release")
    service.review_request("TR-1", "u3", "approve")
    service.release_request("TR-1", "SEAL-1", "u1", "T-A")


def test_review_rejection_ends_request(service, world):
    service.create_request(
        "TR-1", "B-001", 3, world["s1"], world["s2"], world["c1"],
        "u1", "2026-09-23T10:00:00+00:00", "2030-09-23T14:00:00+00:00",
    )
    service.review_request("TR-1", "u2", "reject", comment="单据不齐")
    assert service.get_request("TR-1")["status"] == "review_rejected"
    with pytest.raises(DomainError) as exc:
        service.review_request("TR-1", "u3", "approve")
    assert exc.value.code == "not_reviewable"


def test_reservation_prevents_double_allocation(service, world):
    for no in ("TR-A", "TR-B"):
        service.create_request(
            no, "B-001", 40, world["s1"], world["s2"], world["c1"],
            "u1", "2026-09-23T10:00:00+00:00", "2030-09-23T14:00:00+00:00",
        )
    with pytest.raises(DomainError) as exc:
        service.create_request(
            "TR-C", "B-001", 40, world["s1"], world["s2"], world["c1"],
            "u1", "2026-09-23T10:01:00+00:00", "2030-09-23T14:00:00+00:00",
        )
    assert exc.value.code == "insufficient_stock"
    # 取消以补偿事件释放冻结量，之后可重新建单
    service.cancel_request("TR-A", "u1", "计划变更")
    service.create_request(
        "TR-C", "B-001", 40, world["s1"], world["s2"], world["c1"],
        "u1", "2026-09-23T10:02:00+00:00", "2030-09-23T14:00:00+00:00",
    )


def test_shortage_quarantine_investigation_then_writeoff_conserves(service, world):
    happy_path(service, world, request_no="TR-OK", qty=10)
    service.create_request(
        "TR-S", "B-001", 10, world["s1"], world["s2"], world["c1"],
        "u1", "2026-09-23T11:00:00+00:00", "2030-09-24T14:00:00+00:00",
    )
    service.review_request("TR-S", "u2", "approve")
    service.review_request("TR-S", "u3", "approve")
    service.release_request("TR-S", "SEAL-S", "u1", "T-A")
    service.confirm_seal("TR-S", "SEAL-S", "u3", "T-B")
    outcome = service.receive("TR-S", 7, "u3", "T-B")
    assert outcome["quarantined"] is True
    assert "shortage" in outcome["reasons"]
    assert service.get_request("TR-S")["status"] == "quarantined"

    # 短少 3 件仍挂在承运人名下待调查结论
    located = service.locate_batch("B-001")
    holders = {h["custodian"]: h["quantity"] for h in located["holders"]}
    assert holders[f"carrier:{world['c1']}"] == 3
    assert holders[f"quarantine:{world['s2']}"] == 7
    # 持仓仍守恒（短少悬置在承运名下），但审计必须暴露未结短少调查
    audit_before = service.audit()
    assert audit_before["quantity_conserved"] is True
    assert audit_before["open_investigation_count"] == 1
    batch = next(b for b in audit_before["batches"] if b["batch_no"] == "B-001")
    investigation_id = batch["open_investigations"][0]["investigation_id"]
    assert batch["open_investigations"][0]["qty_loss"] == 3

    # 调查处置：短少核销 + 隔离解除，最后结案
    service.resolve_investigation(investigation_id, "write_off", "auditor", "运输短少核销", quantity=3)
    service.resolve_investigation(investigation_id, "release_goods", "auditor", "封签完好解除隔离", quantity=7)
    service.resolve_investigation(investigation_id, "close", "auditor", "处置完成结案")

    audit = service.audit()
    assert audit["ok"] is True
    batch = next(b for b in audit["batches"] if b["batch_no"] == "B-001")
    assert batch["held_qty"] + batch["written_off_qty"] == batch["genesis_qty"] == 100
    assert batch["written_off_qty"] == 3


def test_damage_full_qty_quarantined(service, world):
    service.create_request(
        "TR-D", "B-001", 4, world["s1"], world["s2"], world["c1"],
        "u1", "2026-09-23T11:00:00+00:00", "2030-09-24T14:00:00+00:00",
    )
    service.review_request("TR-D", "u2", "approve")
    service.review_request("TR-D", "u3", "approve")
    service.release_request("TR-D", "SEAL-D", "u1", "T-A")
    service.confirm_seal("TR-D", "SEAL-D", "u3", "T-B")
    outcome = service.receive("TR-D", 4, "u3", "T-B", damage_reported=True)
    assert "damage" in outcome["reasons"]
    holders = {h["custodian"]: h["quantity"] for h in service.locate_batch("B-001")["holders"]}
    assert holders[f"quarantine:{world['s2']}"] == 4
    assert f"carrier:{world['c1']}" not in holders


def test_reject_and_return_are_compensation_events(service, world):
    happy_path(service, world, request_no="TR-1", qty=10)
    service.create_request(
        "TR-2", "B-001", 5, world["s1"], world["s2"], world["c1"],
        "u1", "2026-09-23T11:00:00+00:00", "2030-09-24T14:00:00+00:00",
    )
    service.review_request("TR-2", "u2", "approve")
    service.review_request("TR-2", "u3", "approve")
    service.release_request("TR-2", "SEAL-2", "u1", "T-A")
    # 在途拒收：承运人 -> 发出门店
    service.reject_delivery("TR-2", "u3", "门店闭店无法接收")
    holders = {h["custodian"]: h["quantity"] for h in service.locate_batch("B-001")["holders"]}
    assert holders[f"store:{world['s1']}"] == 90
    assert holders[f"store:{world['s2']}"] == 10

    # 已完成交接退回：接收门店 -> 发出门店
    service.return_goods("TR-1", "u3", "质量疑义退回", quantity=10)
    holders = {h["custodian"]: h["quantity"] for h in service.locate_batch("B-001")["holders"]}
    assert holders[f"store:{world['s1']}"] == 100
    assert f"store:{world['s2']}" not in holders
    assert service.audit()["ok"] is True


def test_history_append_only(service, world):
    happy_path(service, world)
    before = service.event_chain("TR-1")
    n_before = len(before["events"])
    service.return_goods("TR-1", "u3", "追加退回")
    after = service.event_chain("TR-1")
    assert len(after["events"]) == n_before + 1
    # 原节点原样保留，occurred_at/数量均未被改写
    assert [(e["event_type"], e["quantity"], e["occurred_at"]) for e in before["events"]] == [
        (e["event_type"], e["quantity"], e["occurred_at"]) for e in after["events"][:n_before]
    ]


def test_database_rejects_mutation_of_history(service, world):
    happy_path(service, world)
    from sqlalchemy.exc import IntegrityError
    from pharmacy_identity.models import custody_events as ce

    with pytest.raises(IntegrityError, match="append-only"):
        with service.engine.begin() as conn:
            conn.execute(ce.update().where(ce.c.event_type == "released").values(quantity=999))
    with pytest.raises(IntegrityError, match="append-only"):
        with service.engine.begin() as conn:
            conn.execute(ce.delete().where(ce.c.event_type == "review"))
    # 历史确实未受影响
    chain = service.event_chain("TR-1")
    released = next(e for e in chain["events"] if e["event_type"] == "released")
    assert released["quantity"] == 10


def test_revalidation_only_for_unreleased_risk_note_after_release(service, world):
    happy_path(service, world, request_no="TR-DONE", qty=5)
    service.create_request(
        "TR-PEND", "B-001", 5, world["s1"], world["s2"], world["c1"],
        "u1", "2026-09-23T11:00:00+00:00", "2030-09-24T14:00:00+00:00",
    )
    service.review_request("TR-PEND", "u2", "approve")
    service.review_request("TR-PEND", "u3", "approve")

    # 承运资质后来被纠正
    service.update_carrier_qualification(world["c1"], "suspended")
    service.update_carrier_qualification(world["c1"], "active", "2031-01-01T00:00:00+00:00")
    outcome = service.revalidate_after_correction("compliance", carrier_id=world["c1"])
    assert {"request_no": "TR-PEND", "valid": True} in outcome["revalidated"]
    assert "TR-DONE" in outcome["risk_noted"]

    chain = service.event_chain("TR-DONE")
    assert any(e["event_type"] == "risk_note" for e in chain["events"])
    assert chain["risk_notes"], "已完成交接只追加风险说明，不改历史"


def test_offline_scan_uses_real_occurred_time(service, world):
    service.create_request(
        "TR-1", "B-001", 6, world["s1"], world["s2"], world["c1"],
        "u1", "2026-09-15T10:00:00+00:00", "2030-09-24T14:00:00+00:00",
    )
    service.review_request("TR-1", "u2", "approve")
    service.review_request("TR-1", "u3", "approve")
    service.release_request("TR-1", "SEAL-1", "u1", "T-A", occurred_at="2026-09-15T14:00:00+00:00", offline=True)
    # 设备离线：签字与签收真实发生在下午，事后补传
    service.confirm_seal("TR-1", "SEAL-1", "u3", "T-B", occurred_at="2026-09-15T15:00:00+00:00", offline=True)
    service.receive("TR-1", 6, "u3", "T-B", occurred_at="2026-09-15T15:30:00+00:00", offline=True)

    at_1515 = service.locate_batch("B-001", at="2026-09-15T15:15:00+00:00")
    holders = {h["custodian"]: h["quantity"] for h in at_1515["holders"]}
    assert holders[f"carrier:{world['c1']}"] == 6
    at_1600 = service.locate_batch("B-001", at="2026-09-15T16:00:00+00:00")
    holders = {h["custodian"]: h["quantity"] for h in at_1600["holders"]}
    assert holders[f"store:{world['s2']}"] == 6

    with pytest.raises(DomainError) as exc:
        service.confirm_seal("TR-1", "SEAL-X", "u3", "T-B", occurred_at="2999-01-01T00:00:00+00:00")
    assert exc.value.code == "future_event"


def test_bypass_signature_routes_to_manual_review(service, world):
    service.create_request(
        "TR-1", "B-001", 6, world["s1"], world["s2"], world["c1"],
        "u1", "2026-09-23T10:00:00+00:00", "2030-09-24T14:00:00+00:00",
    )
    service.review_request("TR-1", "u2", "approve")
    service.review_request("TR-1", "u3", "approve")
    service.release_request("TR-1", "SEAL-1", "u1", "T-A")

    # 离线设备试图越过封签签署直接扫签收
    scan = service.offline_scan("TR-1", "receive", "T-B", "2026-09-23T15:00:00+00:00", seal_no="SEAL-1")
    assert scan["requires_manual_review"] is True
    assert scan["scan"]["result"] == "blocked_out_of_order"

    # 在线接口同样拦截，不产生移交
    outcome = service.receive("TR-1", 6, "u3", "T-B")
    assert outcome["blocked"] is True
    assert service.get_request("TR-1")["requires_manual_review"] == 1
    holders = {h["custodian"]: h["quantity"] for h in service.locate_batch("B-001")["holders"]}
    assert holders[f"carrier:{world['c1']}"] == 6


def test_duplicate_scan_returns_original_result(service, world):
    happy_path(service, world)
    first = service.offline_scan("TR-1", "location_check", "T-B", "2026-09-23T16:00:00+00:00")
    second = service.offline_scan("TR-1", "location_check", "T-B", "2026-09-23T16:00:00+00:00")
    assert first["duplicated"] is False
    assert second["duplicated"] is True
    assert second["scan"]["result"] == first["scan"]["result"]
    with service.engine.connect() as conn:
        count = conn.execute(
            select(func.count()).select_from(scan_records).where(
                scan_records.c.idempotency_key == "scan:TR-1:T-B:location_check:2026-09-23T16:00:00+00:00"
            )
        ).scalar_one()
    assert count == 1


def test_seal_race_two_terminals_only_one_wins(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path}/race.db")
    create_schema(engine)
    svc = CustodyService(engine)
    s1 = svc.register_store("S1", "城北店", "L1")["id"]
    s2 = svc.register_store("S2", "城南店", "L2")["id"]
    c1 = svc.register_carrier("C1", "安通", "Q1", "active", "2031-01-01T00:00:00+00:00")["id"]
    for u in ("u1", "u2", "u3"):
        svc.register_staff(u, s1 if u != "u3" else s2, "pharmacist")
    svc.register_batch("B1", "药")
    svc.genesis_stock("B1", s1, 50, "admin")
    svc.create_request("TR-1", "B1", 5, s1, s2, c1, "u1",
                       "2026-09-23T10:00:00+00:00", "2030-09-23T14:00:00+00:00")
    svc.review_request("TR-1", "u2", "approve")
    svc.review_request("TR-1", "u3", "approve")
    svc.release_request("TR-1", "SEAL-1", "u1", "T-A")

    results = []
    barrier = threading.Barrier(2)

    def confirm(terminal):
        barrier.wait()
        results.append(svc.confirm_seal("TR-1", "SEAL-1", "u3", terminal))

    t1 = threading.Thread(target=confirm, args=("T-X",))
    t2 = threading.Thread(target=confirm, args=("T-Y",))
    t1.start(); t2.start(); t1.join(); t2.join()

    winners = [r for r in results if not r["duplicated"]]
    losers = [r for r in results if r["duplicated"]]
    assert len(winners) == 1 and len(losers) == 1
    # 两个终端拿到的是同一条结果
    assert winners[0]["confirmation"] == losers[0]["confirmation"]
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(seal_confirmations)).scalar_one() == 1
    engine.dispose()


class ControllableClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


def test_recovery_does_overdue_scan_and_todos(tmp_path):
    engine = create_database_engine(f"sqlite:///{tmp_path}/clock.db")
    create_schema(engine)
    clock = ControllableClock("2026-09-23T08:00:00+00:00")
    svc = CustodyService(engine, clock=clock)
    s1 = svc.register_store("S1", "城北店", "L1")["id"]
    s2 = svc.register_store("S2", "城南店", "L2")["id"]
    c1 = svc.register_carrier("C1", "安通", "Q1")["id"]
    for u in ("u1", "u2", "u3"):
        svc.register_staff(u, s1 if u != "u3" else s2, "pharmacist")
    svc.register_batch("B1", "药")
    svc.genesis_stock("B1", s1, 50, "admin")
    svc.create_request("TR-1", "B1", 5, s1, s2, c1, "u1",
                       "2026-09-23T08:00:00+00:00", "2026-09-23T10:00:00+00:00")
    svc.review_request("TR-1", "u2", "approve")
    svc.review_request("TR-1", "u3", "approve")
    svc.release_request("TR-1", "SEAL-1", "u1", "T-A")

    # 系统在 09:00 宕机，12:00 才恢复：到期时不做事
    clock.value = "2026-09-23T09:00:00+00:00"
    assert svc.run_due_tasks()["processed"] == []
    clock.value = "2026-09-23T12:00:00+00:00"
    outcome = svc.run_due_tasks()
    assert outcome["processed"][0]["result"] == "timeout_flagged"
    assert svc.get_request("TR-1")["status"] == "overdue"

    # 超时扫描事件按应发生时刻（10:00）入链，而非恢复时刻
    chain = svc.event_chain("TR-1")
    recovered = [e for e in chain["events"] if e["event_type"] == "scan" and e["payload"].get("scan_type") == "timeout_scan"]
    assert len(recovered) == 1
    assert recovered[0]["occurred_at"] == "2026-09-23T10:00:00+00:00"
    assert recovered[0]["source"] == "recovery"
    audit = svc.audit()
    assert audit["open_investigation_count"] == 1
    engine.dispose()


def test_role_views_expose_only_fields_needed(service, world):
    happy_path(service, world)
    carrier_view = service.request_view_for_role("TR-1", "carrier")
    assert "license_no" not in carrier_view["frozen"] if "frozen" in carrier_view else True
    assert "frozen" not in carrier_view
    assert {"request_no", "status", "seal_no", "current_custodian", "from_store", "to_store"} <= set(carrier_view)

    store_view = service.request_view_for_role("TR-1", "store")
    assert "frozen" not in store_view
    assert store_view["carrier_qualified"] is True

    audit_view = service.request_view_for_role("TR-1", "audit")
    assert audit_view["event_chain"] and audit_view["reviews"]
    with pytest.raises(DomainError):
        service.request_view_for_role("TR-1", "regulator")
