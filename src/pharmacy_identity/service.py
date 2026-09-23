"""受控药品调拨保管领域服务。

不变量（全部由数据库约束 + 写事务串行化保证）：
1. 每个批号在任何时刻持仓非负、总量守恒（genesis 入链，written_off 凭调查结论出链）。
2. 出库是一条 released 移交事件：同一申请仅一条（部分唯一索引），出库后在途保管人唯一。
3. 双人复核：同一申请人不可复核自己的单，两位复核人必须不同。
4. 封签确认每申请每封签仅一行：两终端并发抢确认，数据库只接受一次。
5. 所有带幂等键的写操作重放返回原结果，不产生新事件。
6. 历史事件只追加：取消/拒收/退回/隔离/核销全部以新事件表达。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from . import chain as replay
from .models import (
    carriers,
    custody_events,
    drug_batches,
    investigations,
    request_reviews,
    risk_notes,
    scan_records,
    seal_confirmations,
    staff,
    stores,
    todo_tasks,
    transfer_requests,
)

TERMINAL_STATUSES = {
    "completed",
    "cancelled",
    "review_rejected",
    "rejected",
    "returned",
    "written_off",
}
UNRELEASED_STATUSES = {"pending_review", "approved"}
RESERVING_STATUSES = {"pending_review", "approved"}


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code = code
        self.status = status


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def store_custodian(store_id: int) -> str:
    return f"store:{store_id}"


def carrier_custodian(carrier_id: int) -> str:
    return f"carrier:{carrier_id}"


def quarantine_custodian(store_id: int) -> str:
    return f"quarantine:{store_id}"


class CustodyService:
    def __init__(self, engine, clock: Callable[[], str] = utcnow_iso):
        self.engine = engine
        self.clock = clock

    # ---------------------------------------------------------------- 基础登记

    def register_store(self, code: str, name: str, license_no: str, license_status: str = "active") -> dict:
        now = self.clock()
        with self.engine.begin() as conn:
            try:
                result = conn.execute(
                    stores.insert().values(
                        code=code,
                        name=name,
                        license_no=license_no,
                        license_status=license_status,
                        updated_at=now,
                    )
                )
            except IntegrityError:
                raise DomainError("store_exists", f"门店 {code} 已存在", 409)
            return {"id": result.inserted_primary_key[0], "code": code}

    def update_store_license(self, store_id: int, license_status: str, license_no: str | None = None) -> dict:
        now = self.clock()
        with self.engine.begin() as conn:
            row = conn.execute(select(stores).where(stores.c.id == store_id)).mappings().first()
            if row is None:
                raise DomainError("store_not_found", "门店不存在", 404)
            conn.execute(
                stores.update()
                .where(stores.c.id == store_id)
                .values(
                    license_status=license_status,
                    license_no=license_no or row["license_no"],
                    updated_at=now,
                )
            )
        return {"id": store_id, "license_status": license_status}

    def register_staff(self, user_code: str, store_id: int, role: str) -> dict:
        with self.engine.begin() as conn:
            if conn.execute(select(stores.c.id).where(stores.c.id == store_id)).first() is None:
                raise DomainError("store_not_found", "门店不存在", 404)
            try:
                result = conn.execute(
                    staff.insert().values(user_code=user_code, store_id=store_id, role=role, active=1)
                )
            except IntegrityError:
                raise DomainError("staff_exists", f"员工 {user_code} 已存在", 409)
            return {"id": result.inserted_primary_key[0]}

    def register_carrier(
        self,
        code: str,
        name: str,
        qualification_no: str,
        qualification_status: str = "active",
        qualified_until: str | None = None,
    ) -> dict:
        now = self.clock()
        with self.engine.begin() as conn:
            try:
                result = conn.execute(
                    carriers.insert().values(
                        code=code,
                        name=name,
                        qualification_no=qualification_no,
                        qualification_status=qualification_status,
                        qualified_until=qualified_until,
                        updated_at=now,
                    )
                )
            except IntegrityError:
                raise DomainError("carrier_exists", f"承运人 {code} 已存在", 409)
            return {"id": result.inserted_primary_key[0], "code": code}

    def update_carrier_qualification(
        self,
        carrier_id: int,
        qualification_status: str,
        qualified_until: str | None = None,
    ) -> dict:
        now = self.clock()
        with self.engine.begin() as conn:
            row = conn.execute(select(carriers).where(carriers.c.id == carrier_id)).mappings().first()
            if row is None:
                raise DomainError("carrier_not_found", "承运人不存在", 404)
            conn.execute(
                carriers.update()
                .where(carriers.c.id == carrier_id)
                .values(
                    qualification_status=qualification_status,
                    qualified_until=qualified_until if qualified_until is not None else row["qualified_until"],
                    updated_at=now,
                )
            )
        return {"id": carrier_id, "qualification_status": qualification_status}

    def register_batch(self, batch_no: str, drug_name: str) -> dict:
        with self.engine.begin() as conn:
            try:
                result = conn.execute(
                    drug_batches.insert().values(batch_no=batch_no, drug_name=drug_name, controlled=1)
                )
            except IntegrityError:
                raise DomainError("batch_exists", f"批号 {batch_no} 已存在", 409)
            return {"id": result.inserted_primary_key[0]}

    def genesis_stock(self, batch_no: str, store_id: int, quantity: int, actor: str) -> dict:
        """登记期初库存（批号入链的唯一入口之一）。"""
        if quantity <= 0:
            raise DomainError("bad_quantity", "期初数量必须为正")
        now = self.clock()
        with self.engine.begin() as conn:
            self._require_store(conn, store_id)
            if conn.execute(select(drug_batches.c.id).where(drug_batches.c.batch_no == batch_no)).first() is None:
                raise DomainError("batch_not_found", "批号未登记", 404)
            seq = self._next_seq(conn, None, batch_no)
            conn.execute(
                custody_events.insert().values(
                    request_id=None,
                    batch_no=batch_no,
                    seq=seq,
                    event_type="genesis",
                    from_custodian=None,
                    to_custodian=store_custodian(store_id),
                    quantity=quantity,
                    occurred_at=now,
                    recorded_at=now,
                    actor=actor,
                    source="online",
                    payload=json.dumps({"store_id": store_id}),
                )
            )
        return {"batch_no": batch_no, "store_id": store_id, "quantity": quantity}

    # ---------------------------------------------------------------- 读取视图

    def _load_events(self, conn, batch_no: str | None = None) -> list[dict]:
        stmt = select(custody_events)
        if batch_no is not None:
            stmt = stmt.where(custody_events.c.batch_no == batch_no)
        return [dict(r) for r in conn.execute(stmt.order_by(custody_events.c.occurred_at, custody_events.c.seq)).mappings()]

    def positions(self, at: str | None = None, batch_no: str | None = None) -> dict:
        with self.engine.connect() as conn:
            return replay.positions_at(self._load_events(conn, batch_no), at)

    def locate_batch(self, batch_no: str, at: str | None = None) -> dict:
        """夜班交接视图：某批号此刻在哪些保管人手中各多少。"""
        with self.engine.connect() as conn:
            holders = replay.positions_at(self._load_events(conn, batch_no), at).get(batch_no, {})
        return {
            "batch_no": batch_no,
            "at": at or self.clock(),
            "holders": [{"custodian": c, "quantity": q} for c, q in sorted(holders.items())],
        }

    # ---------------------------------------------------------------- 申请与复核

    def _freeze_snapshot(self, conn, from_store, to_store, carrier, planned_at: str, expected_by: str) -> dict:
        carrier_ok = (
            carrier["qualification_status"] == "active"
            and (carrier["qualified_until"] is None or carrier["qualified_until"] >= expected_by)
        )
        return {
            "planned_at": planned_at,
            "expected_by": expected_by,
            "from_store": {
                "id": from_store["id"],
                "code": from_store["code"],
                "license_no": from_store["license_no"],
                "license_status": from_store["license_status"],
            },
            "to_store": {
                "id": to_store["id"],
                "code": to_store["code"],
                "license_no": to_store["license_no"],
                "license_status": to_store["license_status"],
            },
            "carrier": {
                "id": carrier["id"],
                "code": carrier["code"],
                "qualification_no": carrier["qualification_no"],
                "qualification_status": carrier["qualification_status"],
                "qualified_until": carrier["qualified_until"],
                "qualified_for_planned_leg": carrier_ok,
            },
        }

    def create_request(
        self,
        request_no: str,
        batch_no: str,
        quantity: int,
        from_store_id: int,
        to_store_id: int,
        carrier_id: int,
        created_by: str,
        planned_at: str,
        expected_by: str,
    ) -> dict:
        if quantity <= 0:
            raise DomainError("bad_quantity", "调拨数量必须为正")
        if from_store_id == to_store_id:
            raise DomainError("same_store", "发出与接收门店不能相同")
        if expected_by < planned_at:
            raise DomainError("bad_schedule", "预计到达不得早于计划时刻")
        now = self.clock()
        with self.engine.begin() as conn:
            from_store = self._require_store(conn, from_store_id)
            to_store = self._require_store(conn, to_store_id)
            carrier = self._require_carrier(conn, carrier_id)
            batch_row = conn.execute(
                select(drug_batches).where(drug_batches.c.batch_no == batch_no)
            ).mappings().first()
            if batch_row is None:
                raise DomainError("batch_not_found", "批号未登记", 404)
            # 计划时刻冻结：许可/资质此刻必须有效
            if from_store["license_status"] != "active" or to_store["license_status"] != "active":
                raise DomainError("license_suspended", "计划时刻存在门店许可未激活，不得建单")
            if carrier["qualification_status"] != "active" or (
                carrier["qualified_until"] is not None and carrier["qualified_until"] < expected_by
            ):
                raise DomainError("carrier_unqualified", "计划时刻承运资质无效，不得建单")
            # 库存必须存在（预订数量在建单时即冻结，可用量减少但保管人不变）
            held = replay.positions_at(self._load_events(conn, batch_no)).get(batch_no, {})
            reserved = self._reserved_qty(conn, batch_no, from_store_id, exclude_request=None)
            if held.get(store_custodian(from_store_id), 0) - reserved < quantity:
                raise DomainError("insufficient_stock", "冻结数量超过门店可用库存")
            snapshot = self._freeze_snapshot(conn, from_store, to_store, carrier, planned_at, expected_by)
            try:
                result = conn.execute(
                    transfer_requests.insert().values(
                        request_no=request_no,
                        batch_no=batch_no,
                        drug_name=batch_row["drug_name"],
                        quantity=quantity,
                        from_store_id=from_store_id,
                        to_store_id=to_store_id,
                        carrier_id=carrier_id,
                        planned_at=planned_at,
                        expected_by=expected_by,
                        status="pending_review",
                        frozen_snapshot=json.dumps(snapshot, ensure_ascii=False),
                        created_by=created_by,
                        created_at=now,
                    )
                )
            except IntegrityError:
                raise DomainError("request_exists", f"申请 {request_no} 已存在", 409)
            request_id = result.inserted_primary_key[0]
            self._insert_event(
                conn,
                request_id=request_id,
                batch_no=batch_no,
                event_type="created",
                quantity=quantity,
                occurred_at=now,
                actor=created_by,
                payload={"snapshot": snapshot, "planned_at": planned_at, "expected_by": expected_by},
            )
        return self.get_request(request_no)

    def review_request(self, request_no: str, reviewer_user_code: str, decision: str, comment: str = "") -> dict:
        if decision not in ("approve", "reject"):
            raise DomainError("bad_decision", "decision 仅支持 approve/reject")
        now = self.clock()
        with self.engine.begin() as conn:
            req = self._require_request(conn, request_no)
            if req["status"] not in UNRELEASED_STATUSES:
                raise DomainError("not_reviewable", f"申请处于 {req['status']}，不可复核")
            reviewer = conn.execute(
                select(staff).where(staff.c.user_code == reviewer_user_code)
            ).mappings().first()
            if reviewer is None or not reviewer["active"]:
                raise DomainError("reviewer_unknown", "复核人不存在或已停用")
            if reviewer_user_code == req["created_by"]:
                raise DomainError("self_review", "申请人不得复核本人提交的调拨申请")
            approvals = conn.execute(
                select(request_reviews).where(
                    request_reviews.c.request_id == req["id"],
                    request_reviews.c.decision == "approve",
                )
            ).mappings().all()
            if any(r["reviewer_user_code"] == reviewer_user_code for r in approvals):
                raise DomainError("duplicate_review", "该复核人已复核过本申请", 409)
            if len(approvals) >= 2:
                raise DomainError("review_complete", "双人复核已完成")
            try:
                conn.execute(
                    request_reviews.insert().values(
                        request_id=req["id"],
                        reviewer_user_code=reviewer_user_code,
                        decision=decision,
                        comment=comment,
                        created_at=now,
                    )
                )
            except IntegrityError:
                raise DomainError("duplicate_review", "该复核人已复核过本申请", 409)
            self._insert_event(
                conn,
                request_id=req["id"],
                batch_no=req["batch_no"],
                event_type="review",
                actor=reviewer_user_code,
                occurred_at=now,
                payload={"decision": decision, "comment": comment},
            )
            if decision == "reject":
                conn.execute(
                    transfer_requests.update()
                    .where(transfer_requests.c.id == req["id"])
                    .values(status="review_rejected")
                )
            elif len(approvals) + 1 >= 2:
                conn.execute(
                    transfer_requests.update()
                    .where(transfer_requests.c.id == req["id"])
                    .values(status="approved")
                )
        return self.get_request(request_no)

    # ---------------------------------------------------------------- 出库

    def release_request(self, request_no: str, seal_no: str, actor: str, terminal_id: str,
                        occurred_at: str | None = None, offline: bool = False) -> dict:
        now = self.clock()
        happened = occurred_at or now
        if happened > now:
            raise DomainError("future_event", "出库时间不能晚于当前时间")
        with self.engine.begin() as conn:
            req = self._require_request(conn, request_no)
            if req["status"] != "approved":
                raise DomainError("not_approvable_release", f"申请状态 {req['status']}，须双人复核通过后出库")
            approvals = conn.execute(
                select(request_reviews.c.reviewer_user_code).where(
                    request_reviews.c.request_id == req["id"],
                    request_reviews.c.decision == "approve",
                )
            ).all()
            if len(approvals) < 2:
                raise DomainError("review_incomplete", "双人复核未完成，禁止出库")
            # 出库前按最新记录/最近重验快照再核验（冻结单内容不变，有效性重验结果另存事件）
            latest = self._latest_revalidation(conn, req["id"])
            if latest is not None and not latest.get("valid"):
                raise DomainError("revalidation_failed", "重验未通过，禁止出库；请先纠正或许可恢复后重验")
            from_store = self._require_store(conn, req["from_store_id"])
            carrier = self._require_carrier(conn, req["carrier_id"])
            if from_store["license_status"] != "active":
                raise DomainError("license_suspended", "发出门店许可已暂停，禁止出库")
            if carrier["qualification_status"] != "active" or (
                carrier["qualified_until"] is not None and carrier["qualified_until"] < now
            ):
                raise DomainError("carrier_unqualified", "承运资质已失效，禁止出库")
            held = replay.positions_at(self._load_events(conn, req["batch_no"])).get(req["batch_no"], {})
            reserved = self._reserved_qty(conn, req["batch_no"], req["from_store_id"], exclude_request=req["id"])
            if held.get(store_custodian(req["from_store_id"]), 0) - reserved < req["quantity"]:
                raise DomainError("insufficient_stock", "可用库存不足，禁止出库")
            # 唯一在途保管人：一条 released 移交事件，部分唯一索引兜底并发双出库
            self._insert_event(
                conn,
                request_id=req["id"],
                batch_no=req["batch_no"],
                event_type="released",
                from_custodian=store_custodian(req["from_store_id"]),
                to_custodian=carrier_custodian(req["carrier_id"]),
                quantity=req["quantity"],
                occurred_at=happened,
                actor=actor,
                terminal_id=terminal_id,
                source="offline" if offline else "online",
                payload={"seal_no": seal_no},
            )
            conn.execute(
                transfer_requests.update()
                .where(transfer_requests.c.id == req["id"])
                .values(status="in_transit", seal_no=seal_no, released_at=happened)
            )
            # 超时扫描待办：到达 expected_by 即应触发；系统停机期间由恢复任务补做
            conn.execute(
                todo_tasks.insert().values(
                    request_id=req["id"],
                    task_type="timeout_scan",
                    status="pending",
                    due_at=req["expected_by"],
                    payload=json.dumps({"seal_no": seal_no}),
                    created_at=now,
                )
            )
        return self.get_request(request_no)

    # ---------------------------------------------------------------- 封签与接收

    def confirm_seal(
        self,
        request_no: str,
        seal_no: str,
        actor: str,
        terminal_id: str,
        occurred_at: str | None = None,
        offline: bool = False,
    ) -> dict:
        """接收方对封签签字。同一封签仅可确认一次，重复/并发抢确认返回原结果。"""
        now = self.clock()
        happened = occurred_at or now
        if happened > now:
            raise DomainError("future_event", "封签确认时间不能晚于当前时间")
        idem = f"seal:{request_no}:{seal_no}"
        try:
            with self.engine.begin() as conn:
                req = self._require_request(conn, request_no)
                existing = conn.execute(
                    select(seal_confirmations).where(
                        seal_confirmations.c.request_id == req["id"],
                        seal_confirmations.c.seal_no == seal_no,
                    )
                ).mappings().first()
                if existing is not None:
                    return {"duplicated": True, "confirmation": self._seal_view(existing)}
                if req["status"] not in ("in_transit", "overdue"):
                    raise DomainError("not_in_transit", f"申请处于 {req['status']}，无法确认封签")
                seal_ok = seal_no == req["seal_no"]
                result = conn.execute(
                    seal_confirmations.insert().values(
                        request_id=req["id"],
                        seal_no=seal_no,
                        terminal_id=terminal_id,
                        confirmed_by=actor,
                        occurred_at=happened,
                        recorded_at=now,
                    )
                )
                confirmation_id = result.inserted_primary_key[0]
                self._insert_scan_events(
                    conn,
                    req=req,
                    scan_type="seal_confirm",
                    seal_no=seal_no,
                    actor=actor,
                    terminal_id=terminal_id,
                    occurred_at=happened,
                    now=now,
                    offline=offline,
                    result="seal_ok" if seal_ok else "seal_mismatch",
                    idempotency_key=idem,
                )
                if not seal_ok:
                    # 封签号不符/疑似拆封：不允许进入签收，直接转人工复核并立案
                    self._flag_manual_review(
                        conn, req, reason="bypass", detail=f"封签不符：扫描 {seal_no}，单据 {req['seal_no']}", now=now
                    )
                row = conn.execute(
                    select(seal_confirmations).where(seal_confirmations.c.id == confirmation_id)
                ).mappings().one()
                view = self._seal_view(row)
        except IntegrityError:
            # 并发终端抢先落库（BEGIN IMMEDIATE 下罕见，唯一约束兜底）：返回先成功的那一条
            with self.engine.connect() as conn:
                req = self._require_request(conn, request_no)
                winner = conn.execute(
                    select(seal_confirmations).where(
                        seal_confirmations.c.request_id == req["id"],
                        seal_confirmations.c.seal_no == seal_no,
                    )
                ).mappings().first()
            return {"duplicated": True, "confirmation": self._seal_view(winner), "lost_race": True}
        return {"duplicated": False, "confirmation": view, "seal_ok": seal_ok}

    def receive(
        self,
        request_no: str,
        actual_quantity: int,
        actor: str,
        terminal_id: str,
        damage_reported: bool = False,
        occurred_at: str | None = None,
        offline: bool = False,
    ) -> dict:
        """接收方对实收数量签字；短少/破损/超时直接隔离并立案。"""
        now = self.clock()
        happened = occurred_at or now
        if happened > now:
            raise DomainError("future_event", "签收时间不能晚于当前时间")
        if actual_quantity < 0:
            raise DomainError("bad_quantity", "实收数量不能为负")
        with self.engine.begin() as conn:
            req = self._require_request(conn, request_no)
            if req["status"] not in ("in_transit", "overdue"):
                raise DomainError("not_in_transit", f"申请处于 {req['status']}，无法签收")
            seal_row = conn.execute(
                select(seal_confirmations).where(
                    seal_confirmations.c.request_id == req["id"],
                    seal_confirmations.c.seal_no == req["seal_no"],
                )
            ).mappings().first()
            # 越过封签签署节点：不自动完成，转人工复核并留痕
            if seal_row is None:
                self._insert_scan_events(
                    conn,
                    req=req,
                    scan_type="receive_attempt",
                    seal_no=req["seal_no"],
                    actor=actor,
                    terminal_id=terminal_id,
                    occurred_at=happened,
                    now=now,
                    offline=offline,
                    result="blocked_missing_seal_signature",
                    idempotency_key=f"bypass:{request_no}:{terminal_id}:{happened}",
                )
                self._flag_manual_review(
                    conn, req, reason="bypass", detail="未完成封签签署即尝试数量签收", now=now
                )
                return {"blocked": True, "reason": "missing_seal_signature", "requires_manual_review": True}
            if actual_quantity > req["quantity"]:
                raise DomainError("over_shipment", "实收数量超过调拨数量，异常请走调查流程")

            timeout = happened > req["expected_by"] or req["status"] == "overdue"
            shortage = actual_quantity < req["quantity"]
            if shortage or damage_reported or timeout:
                reasons = []
                if shortage:
                    reasons.append("shortage")
                if damage_reported:
                    reasons.append("damage")
                if timeout:
                    reasons.append("timeout")
                reason = reasons[0]
                # 实到部分进接收门店隔离区；短少部分仍挂在承运人名下，由调查结论核销
                if actual_quantity > 0:
                    self._insert_event(
                        conn,
                        request_id=req["id"],
                        batch_no=req["batch_no"],
                        event_type="quarantined",
                        from_custodian=carrier_custodian(req["carrier_id"]),
                        to_custodian=quarantine_custodian(req["to_store_id"]),
                        quantity=actual_quantity,
                        occurred_at=happened,
                        actor=actor,
                        terminal_id=terminal_id,
                        source="offline" if offline else "online",
                        payload={"reasons": reasons},
                    )
                inv = conn.execute(
                    investigations.insert().values(
                        request_id=req["id"],
                        reason=reason,
                        qty_loss=req["quantity"] - actual_quantity if shortage else 0,
                        status="open",
                        detail=f"应收 {req['quantity']}，实收 {actual_quantity}；触发：{','.join(reasons)}",
                        created_at=happened,
                    )
                )
                self._insert_event(
                    conn,
                    request_id=req["id"],
                    batch_no=req["batch_no"],
                    event_type="quarantined",
                    from_custodian=None,
                    to_custodian=None,
                    quantity=0,
                    occurred_at=happened,
                    actor=actor,
                    terminal_id=terminal_id,
                    source="offline" if offline else "online",
                    payload={
                        "investigation_id": inv.inserted_primary_key[0],
                        "reasons": reasons,
                        "expected": req["quantity"],
                        "actual": actual_quantity,
                    },
                )
                conn.execute(
                    transfer_requests.update()
                    .where(transfer_requests.c.id == req["id"])
                    .values(status="quarantined", received_qty=actual_quantity, completed_at=happened)
                )
                self._complete_todos(conn, req["id"], "timeout_scan", now)
                return {
                    "quarantined": True,
                    "reasons": reasons,
                    "expected": req["quantity"],
                    "actual": actual_quantity,
                }

            # 正常交接：承运人 -> 接收门店，在途唯一保管人结束
            self._insert_event(
                conn,
                request_id=req["id"],
                batch_no=req["batch_no"],
                event_type="received",
                from_custodian=carrier_custodian(req["carrier_id"]),
                to_custodian=store_custodian(req["to_store_id"]),
                quantity=actual_quantity,
                occurred_at=happened,
                actor=actor,
                terminal_id=terminal_id,
                source="offline" if offline else "online",
                payload={"seal_no": req["seal_no"]},
            )
            conn.execute(
                transfer_requests.update()
                .where(transfer_requests.c.id == req["id"])
                .values(status="completed", received_qty=actual_quantity, completed_at=happened)
            )
            self._complete_todos(conn, req["id"], "timeout_scan", now)
        return self.get_request(request_no)

    # ---------------------------------------------------------------- 取消/拒收/退回

    def cancel_request(self, request_no: str, actor: str, reason: str) -> dict:
        """未出库取消：库存从未离店，补偿事件释放冻结数量（持仓自转移，净额为零）。"""
        now = self.clock()
        with self.engine.begin() as conn:
            req = self._require_request(conn, request_no)
            if req["status"] not in UNRELEASED_STATUSES:
                raise DomainError("not_cancellable", f"申请处于 {req['status']}，已出库不可取消")
            self._insert_event(
                conn,
                request_id=req["id"],
                batch_no=req["batch_no"],
                event_type="compensated",
                from_custodian=store_custodian(req["from_store_id"]),
                to_custodian=store_custodian(req["from_store_id"]),
                quantity=req["quantity"],
                occurred_at=now,
                actor=actor,
                payload={"action": "cancel", "reason": reason},
            )
            conn.execute(
                transfer_requests.update()
                .where(transfer_requests.c.id == req["id"])
                .values(status="cancelled")
            )
        return self.get_request(request_no)

    def reject_delivery(self, request_no: str, actor: str, reason: str) -> dict:
        """在途拒收：以补偿事件把货物从承运人恢复到发出门店库存。"""
        now = self.clock()
        with self.engine.begin() as conn:
            req = self._require_request(conn, request_no)
            if req["status"] not in ("in_transit", "overdue"):
                raise DomainError("not_rejectable", f"申请处于 {req['status']}，不可拒收")
            self._insert_event(
                conn,
                request_id=req["id"],
                batch_no=req["batch_no"],
                event_type="compensated",
                from_custodian=carrier_custodian(req["carrier_id"]),
                to_custodian=store_custodian(req["from_store_id"]),
                quantity=req["quantity"],
                occurred_at=now,
                actor=actor,
                payload={"action": "reject", "reason": reason},
            )
            conn.execute(
                transfer_requests.update()
                .where(transfer_requests.c.id == req["id"])
                .values(status="rejected", completed_at=now)
            )
            self._complete_todos(conn, req["id"], "timeout_scan", now)
        return self.get_request(request_no)

    def return_goods(self, request_no: str, actor: str, reason: str, quantity: int | None = None) -> dict:
        """完成交接后的退回：补偿事件把库存从接收门店移回发出门店。"""
        now = self.clock()
        with self.engine.begin() as conn:
            req = self._require_request(conn, request_no)
            if req["status"] != "completed":
                raise DomainError("not_returnable", f"申请处于 {req['status']}，仅已完成交接可退回")
            qty = quantity if quantity is not None else req["received_qty"] or req["quantity"]
            if qty <= 0 or qty > (req["received_qty"] or req["quantity"]):
                raise DomainError("bad_quantity", "退回数量非法")
            self._insert_event(
                conn,
                request_id=req["id"],
                batch_no=req["batch_no"],
                event_type="compensated",
                from_custodian=store_custodian(req["to_store_id"]),
                to_custodian=store_custodian(req["from_store_id"]),
                quantity=qty,
                occurred_at=now,
                actor=actor,
                payload={"action": "return", "reason": reason},
            )
            conn.execute(
                transfer_requests.update()
                .where(transfer_requests.c.id == req["id"])
                .values(status="returned", completed_at=now)
            )
        return self.get_request(request_no)

    # ---------------------------------------------------------------- 离线扫描与越序

    def offline_scan(
        self,
        request_no: str,
        scan_type: str,
        terminal_id: str,
        occurred_at: str,
        seal_no: str | None = None,
    ) -> dict:
        """设备离线扫描按真实发生时间入链；越序（未签封签先扫签收）转人工。"""
        now = self.clock()
        if occurred_at > now:
            raise DomainError("future_event", "扫描发生时间不能晚于当前时间")
        idem = f"scan:{request_no}:{terminal_id}:{scan_type}:{occurred_at}"
        with self.engine.begin() as conn:
            previous = conn.execute(
                select(scan_records).where(scan_records.c.idempotency_key == idem)
            ).mappings().first()
            if previous is not None:
                return {"duplicated": True, "scan": self._scan_view(previous)}
            req = self._require_request(conn, request_no)
            result = "recorded"
            bypass = False
            if scan_type in ("receive", "receive_attempt") and req["status"] in ("in_transit", "overdue"):
                sealed = conn.execute(
                    select(seal_confirmations.c.id).where(
                        seal_confirmations.c.request_id == req["id"]
                    )
                ).first()
                if sealed is None:
                    result = "blocked_out_of_order"
                    bypass = True
            self._insert_scan_events(
                conn,
                req=req,
                scan_type=scan_type,
                seal_no=seal_no,
                actor="offline_device",
                terminal_id=terminal_id,
                occurred_at=occurred_at,
                now=now,
                offline=True,
                result=result,
                idempotency_key=idem,
            )
            if bypass:
                self._flag_manual_review(
                    conn, req, reason="bypass", detail=f"离线扫描 {scan_type} 越过封签签署节点", now=now
                )
            row = conn.execute(
                select(scan_records).where(scan_records.c.idempotency_key == idem)
            ).mappings().one()
            return {"duplicated": False, "scan": self._scan_view(row), "requires_manual_review": bypass}

    # ---------------------------------------------------------------- 恢复/超时补做

    def run_due_tasks(self) -> dict:
        """系统恢复运行后补做：到期超时扫描 + 其他待办。事件按应发生时刻入链。"""
        now = self.clock()
        processed = []
        with self.engine.begin() as conn:
            dues = conn.execute(
                select(todo_tasks).where(
                    todo_tasks.c.status == "pending",
                    todo_tasks.c.due_at <= now,
                ).order_by(todo_tasks.c.due_at)
            ).mappings().all()
            for task in dues:
                req = None
                if task["request_id"] is not None:
                    req = conn.execute(
                        select(transfer_requests).where(transfer_requests.c.id == task["request_id"])
                    ).mappings().first()
                if task["task_type"] == "timeout_scan" and req is not None and req["status"] == "in_transit":
                    # 超时事实发生在 expected_by：按真实时刻补做扫描并立案待人工
                    existed = conn.execute(
                        select(investigations.c.id).where(
                            investigations.c.request_id == req["id"],
                            investigations.c.reason == "timeout",
                            investigations.c.status == "open",
                        )
                    ).first()
                    if existed is None:
                        conn.execute(
                            investigations.insert().values(
                                request_id=req["id"],
                                reason="timeout",
                                qty_loss=0,
                                status="open",
                                detail=f"超过 expected_by={req['expected_by']} 仍未签收，恢复后补做超时扫描",
                                created_at=task["due_at"],
                            )
                        )
                        self._insert_event(
                            conn,
                            request_id=req["id"],
                            batch_no=req["batch_no"],
                            event_type="scan",
                            occurred_at=task["due_at"],
                            actor="system-recovery",
                            source="recovery",
                            payload={"scan_type": "timeout_scan", "overdue_since": req["expected_by"]},
                        )
                        conn.execute(
                            transfer_requests.update()
                            .where(transfer_requests.c.id == req["id"])
                            .values(status="overdue")
                        )
                    processed.append({"task_id": task["id"], "result": "timeout_flagged"})
                else:
                    processed.append({"task_id": task["id"], "result": "kept_pending"})
                    continue
                conn.execute(
                    todo_tasks.update()
                    .where(todo_tasks.c.id == task["id"])
                    .values(status="done", handled_at=now)
                )
        return {"processed": processed, "at": now}

    # ---------------------------------------------------------------- 调查与人工复核

    def resolve_investigation(self, investigation_id: int, action: str, actor: str, note: str, quantity: int | None = None) -> dict:
        """调查处置：write_off / release_goods 为处置动作（可多次执行），close 结案。"""
        now = self.clock()
        with self.engine.begin() as conn:
            inv = conn.execute(
                select(investigations).where(investigations.c.id == investigation_id)
            ).mappings().first()
            if inv is None:
                raise DomainError("investigation_not_found", "调查不存在", 404)
            if inv["status"] != "open":
                raise DomainError("investigation_closed", "调查已结案")
            req = conn.execute(
                select(transfer_requests).where(transfer_requests.c.id == inv["request_id"])
            ).mappings().one()
            if action == "write_off":
                qty = quantity if quantity is not None else inv["qty_loss"]
                if qty <= 0:
                    raise DomainError("bad_quantity", "核销数量必须为正")
                # 短少：承运人名下余额核销；破损：隔离区余额核销。以调查结论作为守恒出链。
                source = (
                    carrier_custodian(req["carrier_id"])
                    if inv["reason"] == "shortage"
                    else quarantine_custodian(req["to_store_id"])
                )
                self._insert_event(
                    conn,
                    request_id=req["id"],
                    batch_no=req["batch_no"],
                    event_type="written_off",
                    from_custodian=source,
                    to_custodian=None,
                    quantity=qty,
                    occurred_at=now,
                    actor=actor,
                    payload={"investigation_id": investigation_id, "action": "write_off", "note": note},
                )
            elif action == "release_goods":
                # 隔离货物解除隔离，恢复为接收门店库存
                held = replay.positions_at(self._load_events(conn, req["batch_no"])).get(req["batch_no"], {})
                available = held.get(quarantine_custodian(req["to_store_id"]), 0)
                qty = quantity if quantity is not None else available
                if qty <= 0 or qty > available:
                    raise DomainError("bad_quantity", "解除隔离数量超过隔离区余额")
                self._insert_event(
                    conn,
                    request_id=req["id"],
                    batch_no=req["batch_no"],
                    event_type="compensated",
                    from_custodian=quarantine_custodian(req["to_store_id"]),
                    to_custodian=store_custodian(req["to_store_id"]),
                    quantity=qty,
                    occurred_at=now,
                    actor=actor,
                    payload={"action": "release_from_quarantine", "investigation_id": investigation_id, "note": note},
                )
            elif action == "close":
                conn.execute(
                    investigations.update()
                    .where(investigations.c.id == investigation_id)
                    .values(status="resolved", detail=inv["detail"] + f" | 结案：{note}", resolved_at=now)
                )
                conn.execute(
                    transfer_requests.update()
                    .where(transfer_requests.c.id == req["id"])
                    .values(requires_manual_review=0)
                )
                self._complete_todos(conn, req["id"], "manual_review", now)
                self._insert_event(
                    conn,
                    request_id=req["id"],
                    batch_no=req["batch_no"],
                    event_type="manual_review",
                    occurred_at=now,
                    actor=actor,
                    payload={"investigation_id": investigation_id, "action": "close", "note": note},
                )
                return {"investigation_id": investigation_id, "action": "close", "status": "resolved"}
            else:
                raise DomainError("bad_action", "action 仅支持 write_off / release_goods / close")
            conn.execute(
                investigations.update()
                .where(investigations.c.id == investigation_id)
                .values(detail=inv["detail"] + f" | 处置 {action}：{note}")
            )
        return {"investigation_id": investigation_id, "action": action, "status": "open"}

    # ---------------------------------------------------------------- 纠正与重验

    def revalidate_after_correction(self, author: str, store_id: int | None = None, carrier_id: int | None = None) -> dict:
        """关系/许可纠正后：只重验尚未出库的申请；已出库（含已完成）追加风险说明。"""
        now = self.clock()
        revalidated, risk_noted = [], []
        with self.engine.begin() as conn:
            stmt = select(transfer_requests)
            if store_id is not None:
                stmt = stmt.where(
                    (transfer_requests.c.from_store_id == store_id)
                    | (transfer_requests.c.to_store_id == store_id)
                )
            if carrier_id is not None:
                stmt = stmt.where(transfer_requests.c.carrier_id == carrier_id)
            requests = list(conn.execute(stmt).mappings())
            for req in requests:
                from_store = self._require_store(conn, req["from_store_id"])
                to_store = self._require_store(conn, req["to_store_id"])
                carrier = self._require_carrier(conn, req["carrier_id"])
                snapshot = self._freeze_snapshot(conn, from_store, to_store, carrier, req["planned_at"], req["expected_by"])
                valid = (
                    from_store["license_status"] == "active"
                    and to_store["license_status"] == "active"
                    and carrier["qualification_status"] == "active"
                    and snapshot["carrier"]["qualified_for_planned_leg"]
                )
                if req["status"] in UNRELEASED_STATUSES:
                    self._insert_event(
                        conn,
                        request_id=req["id"],
                        batch_no=req["batch_no"],
                        event_type="license_revalidated",
                        occurred_at=now,
                        actor=author,
                        payload={"valid": valid, "snapshot": snapshot},
                    )
                    revalidated.append({"request_no": req["request_no"], "valid": valid})
                else:
                    # 已出库/已完成：历史不可改，只追加风险说明
                    note = (
                        f"主体许可/资质在 {now} 被纠正后重验：{'通过' if valid else '仍异常'}；"
                        f"交接发生于旧记录，冻结快照见建单事件。"
                    )
                    conn.execute(
                        risk_notes.insert().values(
                            request_id=req["id"], note=note, author=author, created_at=now
                        )
                    )
                    self._insert_event(
                        conn,
                        request_id=req["id"],
                        batch_no=req["batch_no"],
                        event_type="risk_note",
                        occurred_at=now,
                        actor=author,
                        payload={"valid": valid, "note": note},
                    )
                    risk_noted.append(req["request_no"])
        return {"revalidated": revalidated, "risk_noted": risk_noted}

    # ---------------------------------------------------------------- 审计

    def audit(self, at: str | None = None) -> dict:
        """审计结论：每时刻唯一保管归属 + 批号数量守恒 + 单申请在途唯一性。"""
        with self.engine.connect() as conn:
            events = self._load_events(conn)
            request_rows = conn.execute(select(transfer_requests)).mappings().all()
            inv_rows = conn.execute(select(investigations).where(investigations.c.status == "open")).mappings().all()

        positions = replay.positions_at(events, at)
        conservation = replay.conservation_report(
            [e for e in events if at is None or e["occurred_at"] <= at]
        )
        written_off: dict[str, int] = {}
        for event in events:
            if event["event_type"] == "written_off" and (at is None or event["occurred_at"] <= at):
                written_off[event["batch_no"]] = written_off.get(event["batch_no"], 0) + event["quantity"]
        for item in conservation:
            item["written_off_qty"] = written_off.get(item["batch_no"], 0)
            item["conserved"] = item["held_qty"] + item["written_off_qty"] == item["genesis_qty"]
            item["holders"] = positions.get(item["batch_no"], {})
            batch_request_ids = {r["id"] for r in request_rows if r["batch_no"] == item["batch_no"]}
            item["open_investigations"] = [
                {"investigation_id": i["id"], "request_id": i["request_id"], "reason": i["reason"], "qty_loss": i["qty_loss"]}
                for i in inv_rows
                if i["request_id"] in batch_request_ids
            ]

        # 每个申请：出库量必须等于其在途保管链上的托管量（唯一保管人，无双边库存）
        request_checks = []
        by_request: dict[int, list] = {}
        for event in events:
            if event["request_id"] is not None and (at is None or event["occurred_at"] <= at):
                by_request.setdefault(event["request_id"], []).append(event)
        for req in request_rows:
            req_events = by_request.get(req["id"], [])
            handoffs = [e for e in req_events if e["event_type"] in replay.HANDOFF_TYPES]
            releases = [e for e in handoffs if e["event_type"] == "released"]
            unique_release = len(releases) <= 1
            request_checks.append(
                {
                    "request_no": req["request_no"],
                    "status": req["status"],
                    "unique_release_event": unique_release,
                    "single_inflight_custodian": unique_release,
                }
            )
        all_conserved = all(item["conserved"] for item in conservation)
        no_negatives = all(not item["negative_holders"] for item in conservation)
        all_release_unique = all(c["unique_release_event"] for c in request_checks)
        # 未结调查的短少量必须仍能在承运人/隔离区持仓中找到，否则说明事件缺口
        unexplained_loss = 0
        for item in conservation:
            pending = sum(i["qty_loss"] for i in item["open_investigations"])
            if pending:
                holders = item["holders"]
                residual = sum(
                    q for c, q in holders.items()
                    if c.startswith("carrier:") or c.startswith("quarantine:")
                )
                if pending > residual:
                    unexplained_loss += pending - residual
        return {
            "at": at or self.clock(),
            "quantity_conserved": all_conserved,
            "no_negative_holdings": no_negatives,
            "unique_inflight_custody": all_release_unique and replay.unique_inflight_custodian(events),
            "unexplained_loss_qty": unexplained_loss,
            "ok": all_conserved and no_negatives and all_release_unique and unexplained_loss == 0,
            "batches": conservation,
            "requests": request_checks,
            "open_investigation_count": len(inv_rows),
        }

    def event_chain(self, request_no: str) -> dict:
        with self.engine.connect() as conn:
            req = self._require_request(conn, request_no)
            rows = conn.execute(
                select(custody_events)
                .where(custody_events.c.request_id == req["id"])
                .order_by(custody_events.c.occurred_at, custody_events.c.seq)
            ).mappings().all()
            notes = conn.execute(
                select(risk_notes).where(risk_notes.c.request_id == req["id"]).order_by(risk_notes.c.created_at)
            ).mappings().all()
        return {
            "request_no": request_no,
            "events": [self._event_view(e) for e in rows],
            "risk_notes": [dict(n) for n in notes],
        }

    def get_request(self, request_no: str) -> dict:
        with self.engine.connect() as conn:
            req = self._require_request(conn, request_no)
            return self._request_view(conn, req)

    # ---------------------------------------------------------------- 角色视图

    def request_view_for_role(self, request_no: str, role: str) -> dict:
        full = self.get_request(request_no)
        if role == "audit":
            chain = self.event_chain(request_no)
            full["event_chain"] = chain["events"]
            full["risk_notes"] = chain["risk_notes"]
            return full
        if role == "carrier":
            return {
                "request_no": full["request_no"],
                "status": full["status"],
                "batch_no": full["batch_no"],
                "drug_name": full["drug_name"],
                "quantity": full["quantity"],
                "received_qty": full["received_qty"],
                "seal_no": full["seal_no"],
                "from_store": {"code": full["frozen"]["from_store"]["code"]},
                "to_store": {"code": full["frozen"]["to_store"]["code"]},
                "planned_at": full["planned_at"],
                "expected_by": full["expected_by"],
                "released_at": full["released_at"],
                "completed_at": full["completed_at"],
                "requires_manual_review": bool(full["requires_manual_review"]),
                # 履职所需：仅当前保管归属，不含门店许可证号
                "current_custodian": full["current_custodian"],
            }
        if role == "store":
            return {
                "request_no": full["request_no"],
                "status": full["status"],
                "batch_no": full["batch_no"],
                "drug_name": full["drug_name"],
                "quantity": full["quantity"],
                "received_qty": full["received_qty"],
                "seal_no": full["seal_no"],
                "counterpart_stores": [full["frozen"]["from_store"]["code"], full["frozen"]["to_store"]["code"]],
                "carrier_code": full["frozen"]["carrier"]["code"],
                "carrier_qualified": full["frozen"]["carrier"]["qualified_for_planned_leg"],
                "planned_at": full["planned_at"],
                "expected_by": full["expected_by"],
                "released_at": full["released_at"],
                "completed_at": full["completed_at"],
                "requires_manual_review": bool(full["requires_manual_review"]),
                "current_custodian": full["current_custodian"],
            }
        raise DomainError("bad_role", "角色仅支持 store|carrier|audit", 400)

    # ---------------------------------------------------------------- 内部工具

    def _insert_scan_events(self, conn, *, req, scan_type, seal_no, actor, terminal_id,
                            occurred_at, now, offline, result, idempotency_key):
        conn.execute(
            scan_records.insert().values(
                request_id=req["id"],
                seal_no=seal_no,
                scan_type=scan_type,
                terminal_id=terminal_id,
                occurred_at=occurred_at,
                recorded_at=now,
                offline=1 if offline else 0,
                result=result,
                idempotency_key=idempotency_key,
            )
        )
        self._insert_event(
            conn,
            request_id=req["id"],
            batch_no=req["batch_no"],
            event_type="scan",
            occurred_at=occurred_at,
            actor=actor,
            terminal_id=terminal_id,
            source="offline" if offline else "online",
            idempotency_key=idempotency_key,
            payload={"scan_type": scan_type, "seal_no": seal_no, "result": result},
        )

    def _flag_manual_review(self, conn, req, *, reason: str, detail: str, now: str) -> None:
        conn.execute(
            transfer_requests.update()
            .where(transfer_requests.c.id == req["id"])
            .values(requires_manual_review=1)
        )
        existing = conn.execute(
            select(investigations.c.id).where(
                investigations.c.request_id == req["id"],
                investigations.c.reason == reason,
                investigations.c.status == "open",
            )
        ).first()
        if existing is None:
            conn.execute(
                investigations.insert().values(
                    request_id=req["id"],
                    reason=reason,
                    qty_loss=0,
                    status="open",
                    detail=detail,
                    created_at=now,
                )
            )
        conn.execute(
            todo_tasks.insert().values(
                request_id=req["id"],
                task_type="manual_review",
                status="pending",
                due_at=now,
                payload=json.dumps({"reason": reason, "detail": detail}),
                created_at=now,
            )
        )
        self._insert_event(
            conn,
            request_id=req["id"],
            batch_no=req["batch_no"],
            event_type="manual_review",
            occurred_at=now,
            actor="system",
            payload={"reason": reason, "detail": detail},
        )

    def _complete_todos(self, conn, request_id: int, task_type: str, now: str, keep_other_open: bool = False) -> None:
        conn.execute(
            todo_tasks.update()
            .where(
                todo_tasks.c.request_id == request_id,
                todo_tasks.c.task_type == task_type,
                todo_tasks.c.status == "pending",
            )
            .values(status="done", handled_at=now)
        )

    def _latest_revalidation(self, conn, request_id: int) -> dict | None:
        rows = conn.execute(
            select(custody_events).where(
                custody_events.c.request_id == request_id,
                custody_events.c.event_type == "license_revalidated",
            ).order_by(custody_events.c.occurred_at, custody_events.c.seq)
        ).mappings().all()
        if not rows:
            return None
        return json.loads(rows[-1]["payload"])

    def _reserved_qty(self, conn, batch_no: str, store_id: int, exclude_request: int | None) -> int:
        stmt = select(transfer_requests.c.quantity).where(
            transfer_requests.c.batch_no == batch_no,
            transfer_requests.c.from_store_id == store_id,
            transfer_requests.c.status.in_(tuple(sorted(RESERVING_STATUSES))),
        )
        if exclude_request is not None:
            stmt = stmt.where(transfer_requests.c.id != exclude_request)
        return sum(r[0] for r in conn.execute(stmt))

    def _next_seq(self, conn, request_id, batch_no: str) -> int:
        stmt = select(custody_events.c.seq).where(custody_events.c.batch_no == batch_no)
        if request_id is None:
            stmt = stmt.where(custody_events.c.request_id.is_(None))
        else:
            stmt = stmt.where(custody_events.c.request_id == request_id)
        rows = [r[0] for r in conn.execute(stmt)]
        return (max(rows) + 1) if rows else 1

    def _insert_event(self, conn, **values):
        request_id = values.get("request_id")
        batch_no = values["batch_no"]
        values.setdefault("recorded_at", self.clock())
        values.setdefault("source", "online")
        values["seq"] = self._next_seq(conn, request_id, batch_no)
        if "payload" in values and not isinstance(values["payload"], str):
            values["payload"] = json.dumps(values["payload"], ensure_ascii=False)
        if "idempotency_key" in values:
            # 调用方自行处理幂等；唯一索引冲突要原样抛出以便捕获
            pass
        return conn.execute(custody_events.insert().values(**values))

    def _require_store(self, conn, store_id: int):
        row = conn.execute(select(stores).where(stores.c.id == store_id)).mappings().first()
        if row is None:
            raise DomainError("store_not_found", f"门店 {store_id} 不存在", 404)
        return row

    def _require_carrier(self, conn, carrier_id: int):
        row = conn.execute(select(carriers).where(carriers.c.id == carrier_id)).mappings().first()
        if row is None:
            raise DomainError("carrier_not_found", f"承运人 {carrier_id} 不存在", 404)
        return row

    def _require_request(self, conn, request_no: str):
        row = conn.execute(
            select(transfer_requests).where(transfer_requests.c.request_no == request_no)
        ).mappings().first()
        if row is None:
            raise DomainError("request_not_found", f"申请 {request_no} 不存在", 404)
        return row

    def _seal_view(self, row) -> dict:
        return {
            "seal_no": row["seal_no"],
            "confirmed_by": row["confirmed_by"],
            "terminal_id": row["terminal_id"],
            "occurred_at": row["occurred_at"],
            "recorded_at": row["recorded_at"],
        }

    def _scan_view(self, row) -> dict:
        return {
            "request_id": row["request_id"],
            "scan_type": row["scan_type"],
            "seal_no": row["seal_no"],
            "terminal_id": row["terminal_id"],
            "occurred_at": row["occurred_at"],
            "result": row["result"],
            "offline": bool(row["offline"]),
        }

    def _event_view(self, row) -> dict:
        data = dict(row)
        try:
            data["payload"] = json.loads(data["payload"])
        except (TypeError, ValueError):
            pass
        return data

    def _request_view(self, conn, req) -> dict:
        reviews = conn.execute(
            select(request_reviews).where(request_reviews.c.request_id == req["id"])
        ).mappings().all()
        events = conn.execute(
            select(custody_events)
            .where(custody_events.c.request_id == req["id"])
            .order_by(custody_events.c.occurred_at.desc(), custody_events.c.seq.desc())
        ).mappings().all()
        current = None
        for event in events:
            if event["event_type"] in replay.HANDOFF_TYPES and event["to_custodian"]:
                current = event["to_custodian"]
                break
        snapshot = json.loads(req["frozen_snapshot"])
        return {
            "id": req["id"],
            "request_no": req["request_no"],
            "batch_no": req["batch_no"],
            "drug_name": req["drug_name"],
            "quantity": req["quantity"],
            "from_store_id": req["from_store_id"],
            "to_store_id": req["to_store_id"],
            "carrier_id": req["carrier_id"],
            "planned_at": req["planned_at"],
            "expected_by": req["expected_by"],
            "status": req["status"],
            "seal_no": req["seal_no"],
            "received_qty": req["received_qty"],
            "requires_manual_review": req["requires_manual_review"],
            "created_by": req["created_by"],
            "created_at": req["created_at"],
            "released_at": req["released_at"],
            "completed_at": req["completed_at"],
            "frozen": snapshot,
            "reviews": [
                {
                    "reviewer": r["reviewer_user_code"],
                    "decision": r["decision"],
                    "comment": r["comment"],
                    "at": r["created_at"],
                }
                for r in reviews
            ],
            "current_custodian": current,
        }
