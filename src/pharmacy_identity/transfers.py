"""受控药品调拨工作流：冻结申请、双人复核、唯一在途保管、签收/隔离/补偿。

状态机：
    PLANNED ──双人 APPROVE──▶ DISPATCHED ──清洁签收──▶ DELIVERED
       │                         │
       │                     ┌───┴────────────────────┐
    CANCELLED          REJECTED（拒收，运回）    QUARANTINED（短少/破损/超时/封签）
                                                 └─调查裁决─▶ DELIVERED / RETURNED / LOSS
    DELIVERED/QUARANTINED ──退回（两程：承运人接走、送回发出店）──▶ RETURNED
"""

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from . import clock, directory, ledger
from .errors import Conflict, ManualReviewRequired, NotFound, ValidationFailed
from .events import append_event, find_scan, seal_taken
from .schema import (
    actors,
    custody_events,
    investigations,
    manual_reviews,
    reviews,
    risk_notes,
    transfer_lines,
    transfer_requests,
    quarantine_locations,
)


# --- 小工具 -----------------------------------------------------------------


def _holder(kind: str, code: str) -> dict[str, str]:
    return {"holder_type": kind, "holder_code": code}


def _mv(product_code: str, batch_number: str, qty: int, src: dict | None, dst: dict | None) -> dict:
    return {
        "product_code": product_code,
        "batch_number": batch_number,
        "qty": qty,
        "from": src,
        "to": dst,
    }


def _actor(conn: Connection, actor_id: str):
    row = conn.execute(select(actors).where(actors.c.actor_id == actor_id)).mappings().first()
    if not row or not row["active"]:
        raise NotFound(f"经办人 {actor_id} 不存在或已停用")
    return row


def _request(conn: Connection, number: str):
    req = conn.execute(
        select(transfer_requests).where(transfer_requests.c.request_number == number)
    ).mappings().first()
    if not req:
        raise NotFound(f"调拨申请 {number} 不存在")
    return req


def _lines(conn: Connection, request_id: int):
    return conn.execute(
        select(transfer_lines).where(transfer_lines.c.request_id == request_id)
    ).mappings().all()


def _open_investigation(conn, req, reason: str, note: str, actor_id: str, occurred_at: str) -> str:
    case_number = directory.new_case_number()
    conn.execute(
        investigations.insert().values(
            case_number=case_number,
            request_id=req["id"],
            reason=reason,
            status="OPEN",
            opened_by=actor_id,
            opened_at=occurred_at,
            note=note,
        )
    )
    return case_number


def _manual(conn, req_id: int | None, reason: str, note: str, payload: dict, actor_id: str | None) -> int:
    result = conn.execute(
        manual_reviews.insert().values(
            request_id=req_id,
            reason=reason,
            status="PENDING",
            payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            created_by=actor_id,
            created_at=clock.now(),
        )
    )
    return result.inserted_primary_key[0]


def _quarantine_for(conn: Connection, jurisdiction_hint: str | None) -> str:
    row = None
    if jurisdiction_hint:
        row = conn.execute(
            select(quarantine_locations.c.code)
            .where(quarantine_locations.c.jurisdiction == jurisdiction_hint)
            .limit(1)
        ).first()
    if row is None:
        row = conn.execute(select(quarantine_locations.c.code).limit(1)).first()
    if row is None:
        raise ValidationFailed("系统中没有可用隔离区")
    return row[0]


def _approved_by_two(conn: Connection, request_id: int) -> bool:
    approvers = conn.execute(
        select(reviews.c.actor_id)
        .where(reviews.c.request_id == request_id)
        .where(reviews.c.decision == "APPROVE")
    ).all()
    return len({a[0] for a in approvers}) >= 2


def _pending_manual_review(conn: Connection, request_id: int) -> bool:
    return conn.execute(
        select(func.count())
        .select_from(manual_reviews)
        .where(manual_reviews.c.request_id == request_id)
        .where(manual_reviews.c.status == "PENDING")
    ).scalar_one() > 0


def _causal_occurred(conn: Connection, request_id: int, provided: str | None) -> str:
    """处置动作的发生时间不得早于该申请最近一次保管移动。

    调查裁决、退回等管理动作在因果上只能发生在出库/签收/隔离之后；
    设备补录或时钟偏差不能让处置排到被处置事实之前。
    """
    base = clock.normalize(provided) if provided else clock.now()
    row = conn.execute(
        select(func.max(custody_events.c.occurred_at))
        .where(custody_events.c.request_id == request_id)
        .where(
            custody_events.c.event_type.in_(
                ("DISPATCHED", "RECEIVED", "QUARANTINED", "RETURNED")
            )
        )
    ).scalar()
    if row and clock.parse(row) > clock.parse(base):
        return row
    return base


# --- 申请与冻结 --------------------------------------------------------------


def create_request(conn: Connection, body: dict[str, Any], actor_id: str) -> dict[str, Any]:
    actor = _actor(conn, actor_id)
    sender_code = body["sender_code"]
    receiver_code = body["receiver_code"]
    carrier_code = body["carrier_code"]
    if actor["role"] != "ADMIN" and actor["party_code"] not in (sender_code, receiver_code):
        raise Conflict("FORBIDDEN", "只能由本门店或管理员发起调拨")

    planned_dispatch = clock.normalize(body["planned_dispatched_at"])
    planned_receive = clock.normalize(body["planned_received_at"])
    raw_lines = body.get("lines") or []
    lines = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_lines:
        key = (raw["product_code"], raw["batch_number"])
        if key in seen:
            raise ValidationFailed(f"批号 {key[1]} 在同一申请中重复")
        seen.add(key)
        lines.append(
            {"product_code": key[0], "batch_number": key[1], "planned_qty": int(raw["planned_qty"])}
        )
    if not lines:
        raise ValidationFailed("调拨明细不能为空")

    # 可用余额来自台账重放：只有发出店 STORE 持有的批次数量才能被冻结。
    balances = ledger.current_balances(conn)
    available: dict[tuple[str, str], int] = {}
    for key, holders in balances.items():
        available[key] = holders.get(("STORE", sender_code), 0)
    reserved = directory.reserved_quantities(conn)

    violations = directory.validate_live(
        conn,
        sender_code,
        receiver_code,
        carrier_code,
        planned_dispatch,
        planned_receive,
        lines,
        available,
        reserved,
    )
    if violations:
        raise ValidationFailed("；".join(violations))

    snapshot = directory.build_snapshot(
        conn, sender_code, receiver_code, carrier_code,
        planned_dispatch, planned_receive, lines,
    )
    number = directory.new_request_number()
    result = conn.execute(
        transfer_requests.insert().values(
            request_number=number,
            sender_code=sender_code,
            receiver_code=receiver_code,
            carrier_code=carrier_code,
            planned_dispatched_at=planned_dispatch,
            planned_received_at=planned_receive,
            status="PLANNED",
            snapshot_json=json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
            snapshot_valid=True,
            created_by=actor_id,
            created_at=clock.now(),
        )
    )
    request_id = result.inserted_primary_key[0]
    for line in lines:
        conn.execute(transfer_lines.insert().values(request_id=request_id, **line))
    append_event(
        conn,
        request_id=request_id,
        event_type="REQUESTED",
        holder_type="STORE",
        holder_code=sender_code,
        actor_id=actor_id,
        items=[{"product_code": l["product_code"], "batch_number": l["batch_number"], "qty": l["planned_qty"]} for l in lines],
        payload={"lines": lines, "frozen_licenses": [l["id"] for l in snapshot["licenses"]]},
    )
    return {"request_number": number, "status": "PLANNED", "frozen": True}


# --- 双人复核 ----------------------------------------------------------------


def add_review(conn: Connection, number: str, body: dict[str, Any], actor_id: str) -> dict[str, Any]:
    actor = _actor(conn, actor_id)
    if not actor["can_review"]:
        raise Conflict("FORBIDDEN", "该经办人没有复核权限")
    req = _request(conn, number)
    if req["status"] != "PLANNED":
        raise Conflict("REVIEW_CLOSED", f"申请处于 {req['status']}，不能再复核")

    role_slot = body["review_role"]
    if role_slot not in ("FIRST", "SECOND"):
        raise ValidationFailed("review_role 必须为 FIRST 或 SECOND")
    decision = body["decision"]
    if decision not in ("APPROVE", "REJECT"):
        raise ValidationFailed("decision 必须为 APPROVE 或 REJECT")

    existing = conn.execute(
        select(reviews).where(reviews.c.request_id == req["id"])
    ).mappings().all()
    by_role = {r["review_role"]: r for r in existing}
    if role_slot in by_role:
        raise Conflict("REVIEW_EXISTS", f"{role_slot} 复核已完成，结论不可更改")
    if role_slot == "SECOND":
        first = by_role.get("FIRST")
        if not first:
            raise Conflict("FIRST_REVIEW_REQUIRED", "必须先完成初审")
        if first["actor_id"] == actor_id:
            raise Conflict("DUAL_CONTROL", "双人复核必须由两名不同经办人完成")
        if first["decision"] == "REJECT":
            raise Conflict("FIRST_REVIEW_REJECTED", "初审已驳回，不能进入复审")

    at = clock.now()
    conn.execute(
        reviews.insert().values(
            request_id=req["id"],
            review_role=role_slot,
            actor_id=actor_id,
            decision=decision,
            note=body.get("note"),
            created_at=at,
        )
    )
    append_event(
        conn,
        request_id=req["id"],
        event_type="REVIEWED",
        holder_type="STORE",
        holder_code=req["sender_code"],
        actor_id=actor_id,
        occurred_at=at,
        payload={"review_role": role_slot, "decision": decision, "note": body.get("note")},
    )
    if decision == "REJECT":
        conn.execute(
            transfer_requests.update()
            .where(transfer_requests.c.id == req["id"])
            .values(status="CANCELLED", cancelled_at=at, reject_reason=f"复核驳回（{role_slot}）：{body.get('note') or ''}")
        )
        return {"request_number": number, "status": "CANCELLED"}
    return {"request_number": number, "status": "PLANNED", "review_role": role_slot, "approved": True}


# --- 出库：唯一在途保管人 -----------------------------------------------------


def dispatch(conn: Connection, number: str, body: dict[str, Any], actor_id: str) -> dict[str, Any]:
    actor = _actor(conn, actor_id)
    req = _request(conn, number)
    if req["status"] != "PLANNED":
        raise Conflict("NOT_PLANNED", f"申请处于 {req['status']}，不能出库")
    if not req["snapshot_valid"]:
        raise Conflict("SNAPSHOT_INVALID", "冻结资质复核未通过，禁止出库")
    if actor["role"] != "ADMIN" and actor["party_code"] != req["sender_code"]:
        raise Conflict("FORBIDDEN", "只有发出门店可以确认出库")
    if not _approved_by_two(conn, req["id"]):
        raise Conflict("DUAL_CONTROL", "出库前必须完成两名不同经办人的复核批准")
    if _pending_manual_review(conn, req["id"]):
        raise Conflict("MANUAL_REVIEW_OPEN", "存在未结的人工复核单，出库被冻结，需先处理")

    seal_code = body.get("seal_code")
    if not seal_code:
        raise ValidationFailed("出库必须施封并记录封签号")
    if seal_taken(conn, seal_code, "DISPATCHED"):
        raise Conflict("SEAL_ALREADY_DISPATCHED", "该封签已用于另一次出库")
    occurred = clock.normalize(body["occurred_at"]) if body.get("occurred_at") else clock.now()

    # 出库前再按冻结版本核对一次现状（许可可能在复核后被暂停）。
    violations = directory.revalidate(conn, req)
    if violations:
        conn.execute(
            transfer_requests.update()
            .where(transfer_requests.c.id == req["id"])
            .values(snapshot_valid=False, revalidated_at=clock.now())
        )
        raise Conflict("SNAPSHOT_INVALID", "；".join(violations))

    lines = _lines(conn, req["id"])
    movements = [
        _mv(
            l["product_code"], l["batch_number"], l["planned_qty"],
            _holder("STORE", req["sender_code"]),
            _holder("CARRIER", req["carrier_code"]),
        )
        for l in lines
    ]
    event_id = append_event(
        conn,
        request_id=req["id"],
        event_type="DISPATCHED",
        holder_type="CARRIER",
        holder_code=req["carrier_code"],
        actor_id=actor_id,
        occurred_at=occurred,
        seal_code=seal_code,
        location=body.get("location"),
        payload={
            "movements": movements,
            "driver_name": body.get("driver_name"),
            "vehicle_no": body.get("vehicle_no"),
        },
        items=[{"product_code": l["product_code"], "batch_number": l["batch_number"], "qty": l["planned_qty"]} for l in lines],
    )
    conn.execute(
        transfer_requests.update()
        .where(transfer_requests.c.id == req["id"])
        .values(
            status="DISPATCHED",
            dispatched_at=occurred,
            seal_code=seal_code,
            driver_name=body.get("driver_name"),
            vehicle_no=body.get("vehicle_no"),
        )
    )
    return {"request_number": number, "status": "DISPATCHED", "seal_code": seal_code, "event_id": event_id}


# --- 离线/在途扫码 ------------------------------------------------------------


def record_scan(conn: Connection, number: str, body: dict[str, Any], actor_id: str | None) -> dict[str, Any]:
    device_id = body.get("device_id")
    idem = body.get("idempotency_key")
    if not device_id or not idem:
        raise ValidationFailed("扫码必须携带 device_id 与 idempotency_key")
    occurred = clock.normalize(body["occurred_at"]) if body.get("occurred_at") else clock.now()

    # 重复扫码：不产生新节点，直接返回原结果。
    existing = find_scan(conn, device_id, idem)
    if existing:
        return {"duplicate": True, "event_id": existing["id"], "result": existing["event_type"]}

    req = _request(conn, number)
    payload = {"location": body.get("location"), "seal_seen": body.get("seal_code")}
    manual_reason: str | None = None
    note: str | None = None

    if req["status"] == "PLANNED":
        # 尚未完成出库签署节点就扫码：事实入链，自动流转冻结，转人工复核。
        manual_reason = "NODE_SKIP"
        note = "申请尚未出库即出现扫码，疑似越过签署节点"
    elif req["status"] != "DISPATCHED":
        raise Conflict("NOT_IN_TRANSIT", f"申请处于 {req['status']}，不接受在途扫码")
    elif body.get("seal_code") and body["seal_code"] != req["seal_code"]:
        manual_reason = "SEAL_MISMATCH"
        note = f"扫码封签 {body['seal_code']} 与出库封签 {req['seal_code']} 不一致"
    elif req["dispatched_at"] and clock.parse(occurred) < clock.parse(req["dispatched_at"]):
        manual_reason = "CHAIN_ORDER"
        note = "扫码发生时间早于出库时间，链路顺序异常"

    event_id = append_event(
        conn,
        request_id=req["id"],
        event_type="SCAN",
        holder_type="CARRIER",
        holder_code=req["carrier_code"],
        actor_id=actor_id,
        device_id=device_id,
        occurred_at=occurred,
        location=body.get("location"),
        seal_code=body.get("seal_code"),
        idempotency_key=idem,
        note=note,
        payload=payload,
    )
    if manual_reason:
        review_id = _manual(
            conn, req["id"], manual_reason, note or "",
            {"scan_event_id": event_id, **payload}, actor_id
        )
        raise ManualReviewRequired(note or "已转人工复核", review_id)
    return {"duplicate": False, "event_id": event_id, "result": "SCAN"}


# --- 接收签收 / 隔离 / 调查 ---------------------------------------------------


def _request_holdings(
    conn: Connection, request_id: int, holder_type: str | None = None
) -> dict[tuple[str, str], dict[tuple[str, str], int]]:
    """重放该申请相关移动，返回 {完整保管人: {批号: 数量}}（只计正余额）。"""
    data = ledger.replay(conn)
    held: dict[tuple[str, str], dict[tuple[str, str], int]] = {}
    for mv in data["timeline"]:
        if mv["request_id"] != request_id:
            continue
        key = (mv["product_code"], mv["batch_number"])
        for side, sign in (("from", -1), ("to", 1)):
            if mv.get(side):
                holder = (mv[side]["holder_type"], mv[side]["holder_code"])
                if holder_type and holder[0] != holder_type:
                    continue
                bucket = held.setdefault(holder, {})
                bucket[key] = bucket.get(key, 0) + sign * mv["qty"]
    return {
        holder: {k: q for k, q in batches.items() if q > 0}
        for holder, batches in held.items()
        if any(q > 0 for q in batches.values())
    }


def receive(conn: Connection, number: str, body: dict[str, Any], actor_id: str) -> dict[str, Any]:
    actor = _actor(conn, actor_id)
    req = _request(conn, number)
    if req["status"] != "DISPATCHED":
        raise Conflict("NOT_DISPATCHED", f"申请处于 {req['status']}，不能签收")
    if actor["role"] != "ADMIN" and actor["party_code"] != req["receiver_code"]:
        raise Conflict("FORBIDDEN", "只有接收门店可以签收")

    occurred = _causal_occurred(
        conn, req["id"], body.get("occurred_at") and clock.normalize(body["occurred_at"])
    )
    seal_seen = body.get("seal_code")
    seal_ok = seal_seen == req["seal_code"]
    timeout = clock.parse(occurred) > clock.parse(req["planned_received_at"])

    # 双终端抢先确认同一封签：第二个在任何数量处理之前即被数据库裁决拒绝。
    if seal_ok and seal_taken(conn, req["seal_code"], "RECEIVED"):
        raise Conflict("SEAL_ALREADY_CONFIRMED", "该封签已被签收，重复确认被拒绝")

    planned = {(l["product_code"], l["batch_number"]): l for l in _lines(conn, req["id"])}
    carrier_holdings = _request_holdings(conn, req["id"], "CARRIER").get(
        ("CARRIER", req["carrier_code"]), {}
    )
    actual: dict[tuple[str, str], dict[str, int]] = {}
    for raw in body.get("lines", []):
        key = (raw["product_code"], raw["batch_number"])
        if key not in planned:
            raise ValidationFailed(f"实收明细出现申请外批号 {key[1]}")
        good, damaged = int(raw.get("received_qty", 0)), int(raw.get("damaged_qty", 0))
        if good < 0 or damaged < 0:
            raise ValidationFailed("实收数量不能为负")
        if good + damaged > carrier_holdings.get(key, 0):
            raise ValidationFailed(
                f"批号 {key[1]} 实收 {good + damaged} 超出承运人在途数量 {carrier_holdings.get(key, 0)}，请转人工核查"
            )
        actual[key] = {"good": good, "damaged": damaged}
    missing = set(planned) - set(actual)
    if missing:
        raise ValidationFailed(f"缺少批号的实收上报：{sorted(b for _, b in missing)}")

    total_shortage = sum(
        planned[k]["planned_qty"] - actual[k]["good"] - actual[k]["damaged"] for k in planned
    )
    total_damaged = sum(actual[k]["damaged"] for k in planned)
    clean = (
        seal_ok
        and not timeout
        and total_shortage == 0
        and total_damaged == 0
        and all(actual[k]["good"] == planned[k]["planned_qty"] for k in planned)
    )

    # 并发窗口复核：数量读取可能发生在另一终端签收提交前后，落单者在此被挡下。
    if clean and seal_taken(conn, req["seal_code"], "RECEIVED"):
        raise Conflict("SEAL_ALREADY_CONFIRMED", "该封签已被签收，重复确认被拒绝")

    carrier = _holder("CARRIER", req["carrier_code"])
    receiver = _holder("STORE", req["receiver_code"])
    sign_items = [
        {"product_code": k[0], "batch_number": k[1], "qty": planned[k]["planned_qty"]}
        for k in planned
    ]
    signed_qty = {f"{k[0]}|{k[1]}": actual[k] for k in planned}

    if clean:
        # 清洁签收：承运人把全部批号一次性交给接收门店；封签确认占用唯一索引。
        movements = [
            _mv(k[0], k[1], planned[k]["planned_qty"], carrier, receiver) for k in planned
        ]
        try:
            append_event(
                conn,
                request_id=req["id"],
                event_type="RECEIVED",
                holder_type="STORE",
                holder_code=req["receiver_code"],
                actor_id=actor_id,
                occurred_at=occurred,
                seal_code=req["seal_code"],
                location=body.get("location"),
                items=sign_items,
                payload={"movements": movements, "seal_intact": True, "signed_qty": signed_qty},
            )
        except Conflict as exc:
            if exc.code == "SEAL_ALREADY_CONFIRMED":
                raise
            raise
        except Exception as exc:  # 并发下部分唯一索引裁决
            if "seal_code" in str(exc):
                raise Conflict("SEAL_ALREADY_CONFIRMED", "该封签已被签收，重复确认被拒绝") from exc
            raise
        conn.execute(
            transfer_requests.update()
            .where(transfer_requests.c.id == req["id"])
            .values(status="DELIVERED", received_at=occurred)
        )
        return {"request_number": number, "status": "DELIVERED"}

    # 异常签收。封签不符或超时：整单隔离（实物全在，只是不得放行）；
    # 封签完好且未超时：好货正常入库，破损进隔离，短少计 LOSS。
    quarantine_all = not seal_ok or timeout
    q_code = _quarantine_for(conn, body.get("quarantine_code"))
    quarantine = _holder("QUARANTINE", q_code)
    movements: list[dict[str, Any]] = []
    reasons = sorted(
        set(
            (["SEAL_MISMATCH"] if not seal_ok else [])
            + (["TIMEOUT"] if timeout else [])
            + (["DAMAGE"] if total_damaged else [])
            + (["SHORTAGE"] if total_shortage > 0 else [])
        )
    )
    for key, line in planned.items():
        good, damaged = actual[key]["good"], actual[key]["damaged"]
        shortage = line["planned_qty"] - good - damaged
        physical = good + damaged
        if quarantine_all:
            # 封签不符或超时：实物一律隔离（好货也不放行），短少计损失。
            if physical:
                movements.append(_mv(key[0], key[1], physical, carrier, quarantine))
        else:
            if good:
                movements.append(_mv(key[0], key[1], good, carrier, receiver))
            if damaged:
                movements.append(_mv(key[0], key[1], damaged, carrier, quarantine))
        if shortage > 0:
            movements.append(_mv(key[0], key[1], shortage, carrier, _holder("LOSS", "LOSS")))
        conn.execute(
            transfer_lines.update().where(transfer_lines.c.id == line["id"]).values(
                received_qty=0 if quarantine_all else good,
                damaged_qty=damaged,
                shortage_qty=max(shortage, 0),
                quarantined_qty=physical if quarantine_all else damaged,
            )
        )

    # 异常签收不占用封签“唯一成功确认”：seal_code 留空，所见封签记入负载。
    append_event(
        conn,
        request_id=req["id"],
        event_type="RECEIVED",
        holder_type="CARRIER" if quarantine_all else "STORE",
        holder_code=req["carrier_code"] if quarantine_all else req["receiver_code"],
        actor_id=actor_id,
        occurred_at=occurred,
        location=body.get("location"),
        items=sign_items,
        note="异常签收，接收人已对封签与实收数量签字，转入隔离调查",
        payload={
            "movements": movements,
            "seal_intact": seal_ok,
            "seal_seen": seal_seen,
            "expected_seal": req["seal_code"],
            "timeout": timeout,
            "signed_qty": signed_qty,
        },
    )
    append_event(
        conn,
        request_id=req["id"],
        event_type="QUARANTINED",
        holder_type="QUARANTINE",
        holder_code=q_code,
        actor_id=actor_id,
        occurred_at=occurred,
        reason=",".join(reasons),
        items=sign_items,
        payload={"reasons": reasons, "quarantine_all": quarantine_all},
    )
    priority = ("SEAL_MISMATCH", "TIMEOUT", "SHORTAGE", "DAMAGE")
    primary = next((r for r in priority if r in reasons), "SHORTAGE")
    case_number = _open_investigation(
        conn, req, primary,
        f"封签{'相符' if seal_ok else '不符'}；短少 {max(total_shortage, 0)}；破损 {total_damaged}；超时 {timeout}",
        actor_id, occurred,
    )
    conn.execute(
        transfer_requests.update()
        .where(transfer_requests.c.id == req["id"])
        .values(status="QUARANTINED", received_at=occurred)
    )
    return {
        "request_number": number,
        "status": "QUARANTINED",
        "case_number": case_number,
        "reasons": reasons,
    }


# --- 取消 / 拒收 / 退回（补偿事件）-------------------------------------------


def cancel(conn: Connection, number: str, body: dict[str, Any], actor_id: str) -> dict[str, Any]:
    _actor(conn, actor_id)
    req = _request(conn, number)
    if req["status"] != "PLANNED":
        raise Conflict("NOT_CANCELLABLE", f"申请已出库（{req['status']}），不能取消，应走拒收或退回")
    at = clock.now()
    append_event(
        conn,
        request_id=req["id"],
        event_type="CANCELLED",
        holder_type="STORE",
        holder_code=req["sender_code"],
        actor_id=actor_id,
        occurred_at=at,
        reason=body.get("reason"),
        payload={"compensation": "RESERVATION_RELEASED"},
    )
    conn.execute(
        transfer_requests.update()
        .where(transfer_requests.c.id == req["id"])
        .values(status="CANCELLED", cancelled_at=at, reject_reason=body.get("reason"))
    )
    return {"request_number": number, "status": "CANCELLED"}


def reject(conn: Connection, number: str, body: dict[str, Any], actor_id: str) -> dict[str, Any]:
    """车门签收时拒收：承运人仍在保管，货物原车退回发出店。"""
    actor = _actor(conn, actor_id)
    req = _request(conn, number)
    if req["status"] != "DISPATCHED":
        raise Conflict("NOT_IN_TRANSIT", f"申请处于 {req['status']}，不能拒收")
    if actor["role"] != "ADMIN" and actor["party_code"] != req["receiver_code"]:
        raise Conflict("FORBIDDEN", "只有接收门店可以拒收")
    occurred = _causal_occurred(
        conn, req["id"], body.get("occurred_at") and clock.normalize(body["occurred_at"])
    )
    lines = _lines(conn, req["id"])

    if body.get("seal_code") and body["seal_code"] != req["seal_code"]:
        # 封签不符：先隔离并开调查，不得原车退回。
        q_code = _quarantine_for(conn, body.get("quarantine_code"))
        movements = [
            _mv(l["product_code"], l["batch_number"], l["planned_qty"],
                _holder("CARRIER", req["carrier_code"]), _holder("QUARANTINE", q_code))
            for l in lines
        ]
        append_event(
            conn, request_id=req["id"], event_type="REJECTED",
            holder_type="QUARANTINE", holder_code=q_code, actor_id=actor_id,
            occurred_at=occurred, seal_code=body.get("seal_code"),
            reason="SEAL_MISMATCH", items=[{"product_code": l["product_code"], "batch_number": l["batch_number"], "qty": l["planned_qty"]} for l in lines],
            payload={"movements": movements},
        )
        case_number = _open_investigation(conn, req, "SEAL_MISMATCH", body.get("reason") or "拒收时封签不符", actor_id, occurred)
        conn.execute(transfer_requests.update().where(transfer_requests.c.id == req["id"]).values(status="QUARANTINED", received_at=occurred))
        return {"request_number": number, "status": "QUARANTINED", "case_number": case_number}

    movements = [
        _mv(l["product_code"], l["batch_number"], l["planned_qty"],
            _holder("CARRIER", req["carrier_code"]), _holder("STORE", req["sender_code"]))
        for l in lines
    ]
    append_event(
        conn, request_id=req["id"], event_type="REJECTED",
        holder_type="CARRIER", holder_code=req["carrier_code"], actor_id=actor_id,
        occurred_at=occurred, seal_code=req["seal_code"], reason=body.get("reason"),
        items=[{"product_code": l["product_code"], "batch_number": l["batch_number"], "qty": l["planned_qty"]} for l in lines],
        payload={"movements": []},
    )
    append_event(
        conn, request_id=req["id"], event_type="RETURNED",
        holder_type="STORE", holder_code=req["sender_code"], actor_id=actor_id,
        occurred_at=occurred, reason="REJECT_RETURN",
        payload={"movements": movements, "compensation": "REJECTED"},
    )
    conn.execute(
        transfer_requests.update()
        .where(transfer_requests.c.id == req["id"])
        .values(status="REJECTED", rejected_at=occurred, reject_reason=body.get("reason"), returned_at=occurred)
    )
    return {"request_number": number, "status": "REJECTED"}


def return_start(conn: Connection, number: str, body: dict[str, Any], actor_id: str) -> dict[str, Any]:
    """退回第一程：接收门店/隔离区把货交给唯一承运人。"""
    _actor(conn, actor_id)
    req = _request(conn, number)
    if req["status"] not in ("DELIVERED", "QUARANTINED"):
        raise Conflict("NOT_RETURNABLE", f"申请处于 {req['status']}，不能发起退回")
    if req["status"] == "QUARANTINED":
        open_case = conn.execute(
            select(func.count()).select_from(investigations)
            .where(investigations.c.request_id == req["id"])
            .where(investigations.c.status == "OPEN")
        ).scalar_one()
        if open_case:
            raise Conflict("INVESTIGATION_OPEN", "调查未结案前不能退回隔离货物")
    occurred = _causal_occurred(
        conn, req["id"], body.get("occurred_at") and clock.normalize(body["occurred_at"])
    )
    if req["status"] == "QUARANTINED":
        holdings = _request_holdings(conn, req["id"], "QUARANTINE")
    else:
        holdings = {
            ("STORE", req["receiver_code"]): _request_holdings(conn, req["id"], "STORE").get(
                ("STORE", req["receiver_code"]), {}
            )
        }
        # 接收门店可能已把批次再调出：退回数量以全局台账实际余额封顶。
        global_balances = ledger.current_balances(conn)
        capped: dict[tuple[str, str], int] = {}
        for key, qty in holdings[("STORE", req["receiver_code"])].items():
            actual_have = global_balances.get(key, {}).get(("STORE", req["receiver_code"]), 0)
            allowed = min(qty, actual_have)
            if allowed > 0:
                capped[key] = allowed
        holdings[("STORE", req["receiver_code"])] = capped
    movements = [
        _mv(key[0], key[1], qty, _holder(holder[0], holder[1]),
            _holder("CARRIER", req["carrier_code"]))
        for holder, batches in sorted(holdings.items())
        for key, qty in sorted(batches.items())
    ]
    if not movements:
        raise Conflict("NOTHING_TO_RETURN", "该申请名下没有可退回的在账数量")
    items = [
        {"product_code": m["product_code"], "batch_number": m["batch_number"], "qty": m["qty"]}
        for m in movements
    ]
    append_event(
        conn, request_id=req["id"], event_type="RETURNED",
        holder_type="CARRIER", holder_code=req["carrier_code"], actor_id=actor_id,
        occurred_at=occurred, seal_code=body.get("seal_code"),
        items=items,
        payload={"movements": movements, "leg": "RETURN_PICKUP"},
    )
    conn.execute(
        transfer_requests.update()
        .where(transfer_requests.c.id == req["id"]).values(status="RETURNED")
    )
    return {"request_number": number, "status": "RETURNED", "leg": "PICKUP"}


def return_complete(conn: Connection, number: str, body: dict[str, Any], actor_id: str) -> dict[str, Any]:
    """退回第二程：承运人把货送回发出门店，库存以补偿事件恢复。"""
    _actor(conn, actor_id)
    req = _request(conn, number)
    if req["status"] != "RETURNED" or req["returned_at"] is not None:
        raise Conflict("RETURN_NOT_OPEN", "退回不在途中或已完成")
    occurred = _causal_occurred(
        conn, req["id"], body.get("occurred_at") and clock.normalize(body["occurred_at"])
    )
    held = _request_holdings(conn, req["id"], "CARRIER").get(
        ("CARRIER", req["carrier_code"]), {}
    )
    movements = [
        _mv(k[0], k[1], qty, _holder("CARRIER", req["carrier_code"]), _holder("STORE", req["sender_code"]))
        for k, qty in sorted(held.items())
    ]
    if not movements:
        raise Conflict("NOTHING_TO_RETURN", "承运人手中没有该申请的在途货物")
    returned_by_batch = {k: m["qty"] for k, m in (((m["product_code"], m["batch_number"]), m) for m in movements)}
    append_event(
        conn, request_id=req["id"], event_type="RETURNED",
        holder_type="STORE", holder_code=req["sender_code"], actor_id=actor_id,
        occurred_at=occurred, payload={"movements": movements, "leg": "RETURN_DELIVERY", "compensation": "RETURNED"},
    )
    for line in _lines(conn, req["id"]):
        key = (line["product_code"], line["batch_number"])
        if key in returned_by_batch:
            conn.execute(
                transfer_lines.update().where(transfer_lines.c.id == line["id"])
                .values(returned_qty=returned_by_batch[key])
            )
    conn.execute(
        transfer_requests.update().where(transfer_requests.c.id == req["id"])
        .values(returned_at=occurred)
    )
    return {"request_number": number, "status": "RETURNED", "leg": "DELIVERED"}


# --- 调查裁决 ----------------------------------------------------------------


def resolve_investigation(conn: Connection, case_number: str, body: dict[str, Any], actor_id: str) -> dict[str, Any]:
    _actor(conn, actor_id)
    case = conn.execute(
        select(investigations).where(investigations.c.case_number == case_number)
    ).mappings().first()
    if not case:
        raise NotFound(f"调查 {case_number} 不存在")
    if case["status"] != "OPEN":
        raise Conflict("CASE_CLOSED", "调查已结案")
    req = conn.execute(
        select(transfer_requests).where(transfer_requests.c.id == case["request_id"])
    ).mappings().first()
    outcome = body["outcome"]
    if outcome not in ("RELEASE_TO_RECEIVER", "RETURN_TO_SENDER", "CONFIRM_LOSS"):
        raise ValidationFailed("未知裁决结果")
    occurred = _causal_occurred(
        conn, req["id"], body.get("occurred_at") and clock.normalize(body["occurred_at"])
    )

    quarantine_holdings = _request_holdings(conn, req["id"], "QUARANTINE")
    held: dict[tuple[str, str], int] = {}
    for (_, q_code), batches in quarantine_holdings.items():
        for key, qty in batches.items():
            held[(q_code, key[0], key[1])] = qty
    if body.get("quantities"):
        wanted = {(q["product_code"], q["batch_number"]): int(q["qty"]) for q in body["quantities"]}
        held = {
            (q_code, p, b): min(qty, wanted.get((p, b), 0))
            for (q_code, p, b), qty in held.items()
            if wanted.get((p, b))
        }
    if not held and outcome != "CONFIRM_LOSS":
        raise Conflict("NOTHING_IN_QUARANTINE", "隔离区已无该申请货物")

    def _by_holder(target_kind: str, target_code: str, *, from_quarantine=True):
        out = []
        for q_code, product_code, batch_number in sorted(held):
            qty = held[(q_code, product_code, batch_number)]
            src = _holder("QUARANTINE", q_code) if from_quarantine else None
            out.append(_mv(product_code, batch_number, qty, src, _holder(target_kind, target_code)))
        return out

    quantities_payload = {
        f"{p}|{b}": held[(q_code, p, b)] for q_code, p, b in sorted(held)
    }
    new_status: str
    received_at = None
    if outcome == "RELEASE_TO_RECEIVER":
        movements = _by_holder("STORE", req["receiver_code"])
        append_event(conn, request_id=req["id"], event_type="QUARANTINED", holder_type="STORE",
                     holder_code=req["receiver_code"], actor_id=actor_id, occurred_at=occurred,
                     reason="INVESTIGATION_RELEASE",
                     payload={"movements": movements}, note="调查放行至接收门店")
        new_status, received_at = "DELIVERED", occurred
    elif outcome == "RETURN_TO_SENDER":
        pickup = _by_holder("CARRIER", req["carrier_code"])
        delivery = [
            _mv(p, b, m["qty"], _holder("CARRIER", req["carrier_code"]),
                _holder("STORE", req["sender_code"]))
            for m, (_, p, b) in zip(pickup, sorted(held))
        ]
        append_event(conn, request_id=req["id"], event_type="RETURNED", holder_type="CARRIER",
                     holder_code=req["carrier_code"], actor_id=actor_id, occurred_at=occurred,
                     reason="INVESTIGATION_RETURN",
                     payload={"movements": pickup, "leg": "RETURN_PICKUP"})
        append_event(conn, request_id=req["id"], event_type="RETURNED", holder_type="STORE",
                     holder_code=req["sender_code"], actor_id=actor_id, occurred_at=occurred,
                     reason="INVESTIGATION_RETURN",
                     payload={"movements": delivery, "leg": "RETURN_DELIVERY",
                              "compensation": "INVESTIGATION_RETURN"})
        new_status = "RETURNED"
    else:  # CONFIRM_LOSS
        movements = []
        for q_code, product_code, batch_number in sorted(held):
            qty = held[(q_code, product_code, batch_number)]
            movements.append(
                _mv(product_code, batch_number, qty,
                    _holder("QUARANTINE", q_code), _holder("LOSS", "LOSS"))
            )
        append_event(conn, request_id=req["id"], event_type="QUARANTINED", holder_type="LOSS",
                     holder_code="LOSS", actor_id=actor_id, occurred_at=occurred,
                     reason="INVESTIGATION_LOSS",
                     payload={"movements": movements}, note="调查确认损失")
        new_status = "QUARANTINED"

    append_event(
        conn, request_id=req["id"], event_type="INVESTIGATION_RESOLVED",
        holder_type="STORE", holder_code=req["sender_code"], actor_id=actor_id, occurred_at=occurred,
        payload={"case_number": case_number, "outcome": outcome, "quantities": quantities_payload},
    )
    conn.execute(
        investigations.update().where(investigations.c.id == case["id"])
        .values(status="RESOLVED", resolved_at=clock.now(), outcome=outcome,
                note=(case["note"] or "") + f"｜裁决：{body.get('note') or ''}")
    )
    update_values: dict[str, Any] = {"status": new_status}
    if received_at:
        update_values["received_at"] = received_at
    if outcome == "RETURN_TO_SENDER":
        update_values["returned_at"] = occurred
    conn.execute(transfer_requests.update().where(transfer_requests.c.id == req["id"]).values(**update_values))
    return {"case_number": case_number, "status": "RESOLVED", "outcome": outcome, "request_status": new_status}


def resolve_manual_review(conn: Connection, review_id: int, body: dict[str, Any], actor_id: str) -> dict[str, Any]:
    _actor(conn, actor_id)
    row = conn.execute(select(manual_reviews).where(manual_reviews.c.id == review_id)).mappings().first()
    if not row:
        raise NotFound("人工复核单不存在")
    if row["status"] != "PENDING":
        raise Conflict("REVIEW_CLOSED", "该人工复核已处理")
    resolution = body["resolution"]
    conn.execute(
        manual_reviews.update().where(manual_reviews.c.id == review_id).values(
            status="RESOLVED", resolved_at=clock.now(), resolution=resolution, resolved_by=actor_id
        )
    )
    if row["request_id"] is not None and resolution.startswith("RISK"):
        conn.execute(
            risk_notes.insert().values(
                request_id=row["request_id"], category="OTHER",
                note=f"人工复核 {review_id}（{row['reason']}）结论：{resolution}；{body.get('note') or ''}",
                actor_id=actor_id, created_at=clock.now(),
            )
        )
    return {"review_id": review_id, "status": "RESOLVED", "resolution": resolution}


# --- 恢复运行：超时扫描与待办补做 ----------------------------------------------


def run_timeout_sweep(conn: Connection, at: str | None = None) -> dict[str, Any]:
    """对超过计划接收时刻仍在途的申请，按超时隔离并立案。

    服务恢复后调用；以当前时刻为裁决时间，保证宕机窗口也会被补齐。
    """
    moment = clock.normalize(at) if at else clock.now()
    pending = conn.execute(
        select(transfer_requests).where(transfer_requests.c.status == "DISPATCHED")
    ).mappings().all()
    quarantined: list[dict[str, str]] = []
    for req in pending:
        if clock.parse(req["planned_received_at"]) >= clock.parse(moment):
            continue
        lines = _lines(conn, req["id"])
        q_code = _quarantine_for(conn, None)
        movements = [
            _mv(l["product_code"], l["batch_number"], l["planned_qty"],
                _holder("CARRIER", req["carrier_code"]), _holder("QUARANTINE", q_code))
            for l in lines
        ]
        append_event(
            conn, request_id=req["id"], event_type="QUARANTINED",
            holder_type="QUARANTINE", holder_code=q_code, actor_id="SYSTEM",
            occurred_at=moment, reason="TIMEOUT_SWEEP",
            items=[{"product_code": l["product_code"], "batch_number": l["batch_number"], "qty": l["planned_qty"]} for l in lines],
            payload={"movements": movements, "reasons": ["TIMEOUT"], "note": "恢复运行后补做的超时扫描"},
        )
        case_number = _open_investigation(
            conn, req, "TIMEOUT",
            f"超过计划接收时刻 {req['planned_received_at']} 仍未签收，系统补扫隔离", "SYSTEM", moment,
        )
        for l in lines:
            conn.execute(
                transfer_lines.update().where(transfer_lines.c.id == l["id"])
                .values(quarantined_qty=l["planned_qty"])
            )
        conn.execute(
            transfer_requests.update().where(transfer_requests.c.id == req["id"])
            .values(status="QUARANTINED")
        )
        quarantined.append({"request_number": req["request_number"], "case_number": case_number})
    return {"at": moment, "quarantined": quarantined}
