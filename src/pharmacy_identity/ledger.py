"""台账重放：从追加型保管事件重建任意时刻的保管归属。

审计三项证明均由此给出：
1. 批号数量守恒：任一时刻所有保管人（含 LOSS）持有量之和恒等于基线数量；
2. 唯一保管归属：每次移动把数量从一个保管人原子地交给下一个，重放中
   任何保管人余额不得为负（不可能出现两边同时持有同一单位）；
3. 保管区间连续：单个申请的保管区间首尾相接、互不重叠。
"""

import json
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection

from . import clock
from .hashing import event_hash
from .schema import custody_events, custody_items

BatchKey = tuple[str, str]
Holder = tuple[str, str]  # (holder_type, holder_code)


def _movements(row) -> list[dict[str, Any]]:
    if row.payload_json:
        payload = json.loads(row.payload_json)
        return payload.get("movements", [])
    return []


def replay(conn: Connection, as_of: str | None = None) -> dict[str, Any]:
    """按真实发生时间（occurred_at）重放全部事件。

    离线补传的事件发生在过去，因此按 occurred_at 排序而非写入顺序；
    同一时刻以 id 作为因果次序。
    """
    rows = conn.execute(
        select(custody_events).order_by(custody_events.c.occurred_at, custody_events.c.id)
    ).mappings().all()
    if as_of is not None:
        as_of_dt = clock.parse(as_of)
        rows = [r for r in rows if clock.parse(r["occurred_at"]) <= as_of_dt]

    balances: dict[BatchKey, dict[Holder, int]] = defaultdict(lambda: defaultdict(int))
    initial: dict[BatchKey, int] = {}
    timeline: list[dict[str, Any]] = []
    negative: list[str] = []

    for row in rows:
        for mv in _movements(row):
            key = (mv["product_code"], mv["batch_number"])
            qty = mv["qty"]
            source = (mv["from"]["holder_type"], mv["from"]["holder_code"]) if mv.get("from") else None
            target = (mv["to"]["holder_type"], mv["to"]["holder_code"]) if mv.get("to") else None
            if row.event_type == "BASELINE":
                initial[key] = initial.get(key, 0) + qty
            if source is not None:
                balances[key][source] -= qty
                if balances[key][source] < 0:
                    negative.append(
                        f"事件 {row.id} 使 {key} 在 {source} 的余额为 {balances[key][source]}"
                    )
            if target is not None:
                balances[key][target] += qty
            timeline.append(
                {
                    "event_id": row.id,
                    "request_id": row.request_id,
                    "event_type": row.event_type,
                    "occurred_at": row.occurred_at,
                    **mv,
                }
            )

    return {
        "balances": {k: dict(v) for k, v in balances.items()},
        "initial": initial,
        "timeline": timeline,
        "negative": negative,
        "events": [dict(r) for r in rows],
    }


def current_balances(conn: Connection) -> dict[BatchKey, dict[Holder, int]]:
    return replay(conn)["balances"]


def locate_batch(conn: Connection, product_code: str, batch_number: str, at: str | None = None) -> list[dict[str, Any]]:
    """合规主管的夜班问题：这批货此刻在谁手里。"""
    data = replay(conn, as_of=at)
    holders = data["balances"].get((product_code, batch_number), {})
    return [
        {"holder_type": h[0], "holder_code": h[1], "qty": qty}
        for h, qty in sorted(holders.items())
        if qty != 0
    ]


def holder_intervals(conn: Connection, request_id: int) -> list[dict[str, Any]]:
    """重建单个申请连续、不重叠的保管区间。"""
    rows = conn.execute(
        select(custody_events)
        .where(custody_events.c.request_id == request_id)
        .order_by(custody_events.c.occurred_at, custody_events.c.id)
    ).mappings().all()
    intervals: list[dict[str, Any]] = []
    open_interval: dict[str, Any] | None = None
    for row in rows:
        movements = _movements(row)
        if not movements:
            # SCAN/REVIEWED 等观察类事件不切换保管人。
            continue
        for mv in movements:
            if mv.get("from"):
                source = (mv["from"]["holder_type"], mv["from"]["holder_code"])
                if open_interval and open_interval["holder"] == list(source):
                    open_interval["end_event_id"] = row.id
                    open_interval["ended_at"] = row.occurred_at
                    intervals.append(open_interval)
                    open_interval = None
            if mv.get("to"):
                target = (mv["to"]["holder_type"], mv["to"]["holder_code"])
                open_interval = {
                    "holder": list(target),
                    "started_at": row.occurred_at,
                    "start_event_id": row.id,
                    "end_event_id": None,
                    "ended_at": None,
                }
    if open_interval:
        intervals.append(open_interval)
    return intervals


def verify_conservation(conn: Connection) -> dict[str, Any]:
    """全量守恒证明：初始量 = 各保管人持有量之和；无负余额。"""
    data = replay(conn)
    problems = list(data["negative"])
    batches_report: list[dict[str, Any]] = []
    all_keys = set(data["initial"]) | set(data["balances"])
    for key in sorted(all_keys):
        holders = {h: q for h, q in data["balances"].get(key, {}).items() if q != 0}
        total = sum(holders.values())
        baseline = data["initial"].get(key, 0)
        active = sum(q for (ht, _), q in holders.items() if ht != "LOSS")
        lost = sum(q for (ht, _), q in holders.items() if ht == "LOSS")
        ok = total == baseline and all(q >= 0 for q in holders.values())
        if not ok:
            problems.append(f"{key} 不守恒：基线 {baseline}，当前合计 {total}")
        batches_report.append(
            {
                "product_code": key[0],
                "batch_number": key[1],
                "baseline_qty": baseline,
                "active_qty": active,
                "loss_qty": lost,
                "holders": [
                    {"holder_type": h[0], "holder_code": h[1], "qty": q}
                    for h, q in sorted(holders.items())
                ],
                "conserved": ok,
            }
        )
    return {
        "ok": not problems,
        "problems": problems,
        "batches": batches_report,
    }


def verify_hash_chain(conn: Connection) -> dict[str, Any]:
    """重算哈希链，证明历史节点未被删除或篡改。"""
    rows = conn.execute(
        select(
            custody_events.c.id,
            custody_events.c.request_id,
            custody_events.c.seq,
            custody_events.c.event_type,
            custody_events.c.actor_id,
            custody_events.c.device_id,
            custody_events.c.occurred_at,
            custody_events.c.holder_type,
            custody_events.c.holder_code,
            custody_events.c.seal_code,
            custody_events.c.location,
            custody_events.c.reason,
            custody_events.c.note,
            custody_events.c.payload_json,
            custody_events.c.prev_hash,
            custody_events.c.event_hash,
        ).order_by(custody_events.c.id)
    ).all()
    prev: str | None = None
    problems: list[str] = []
    for (
        eid, request_id, seq, event_type, actor_id, device_id, occurred_at,
        holder_type, holder_code, seal_code, location, reason, note,
        payload_json, stored_prev, stored_hash,
    ) in rows:
        if stored_prev != prev:
            problems.append(f"事件 {eid} 前驱哈希断裂")
        payload = json.loads(payload_json) if payload_json else None
        items = [
            [r[0], r[1], r[2]]
            for r in conn.execute(
                select(
                    custody_items.c.product_code,
                    custody_items.c.batch_number,
                    custody_items.c.qty,
                )
                .where(custody_items.c.event_id == eid)
                .order_by(custody_items.c.product_code, custody_items.c.batch_number)
            ).all()
        ]
        digest = event_hash(
            prev,
            {
                "request_id": request_id,
                "seq": seq,
                "event_type": event_type,
                "actor_id": actor_id,
                "device_id": device_id,
                "occurred_at": occurred_at,
                "holder_type": holder_type,
                "holder_code": holder_code,
                "seal_code": seal_code,
                "location": location,
                "reason": reason,
                "note": note,
                "payload": payload,
                "items": sorted(items),
            },
        )
        if digest != stored_hash:
            problems.append(f"事件 {eid} 哈希不匹配，内容被改动")
        prev = stored_hash
    return {"ok": not problems, "problems": problems, "event_count": len(rows)}
