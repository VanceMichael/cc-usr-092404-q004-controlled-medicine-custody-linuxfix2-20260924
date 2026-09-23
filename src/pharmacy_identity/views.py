"""角色视图：门店、承运、审计只能看到履职所需字段。

投影在读取层完成：同一事实按角色裁剪，承运资质号、冻结快照、哈希链、
风险说明等只对审计角色开放。
"""

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection

from .schema import (
    custody_events,
    custody_items,
    investigations,
    parties,
    reviews,
    risk_notes,
    transfer_lines,
    transfer_requests,
)

# 各角色可见的申请字段（最小必要）。
_FIELDS_BY_ROLE = {
    "STORE_STAFF": {
        "request_number", "status", "sender_code", "receiver_code", "carrier_name",
        "planned_dispatched_at", "planned_received_at", "dispatched_at", "received_at",
        "cancelled_at", "rejected_at", "returned_at", "seal_code", "lines",
        "open_investigations", "my_role",
    },
    "CARRIER_STAFF": {
        "request_number", "status", "sender_code", "receiver_code",
        "planned_dispatched_at", "planned_received_at", "dispatched_at", "received_at",
        "seal_code", "driver_name", "vehicle_no", "lines", "my_role",
    },
    "AUDITOR": {"__all__"},
    "ADMIN": {"__all__"},
}

_LINE_FIELDS = {
    "STORE_STAFF": ("product_code", "batch_number", "planned_qty", "received_qty",
                    "damaged_qty", "shortage_qty", "quarantined_qty", "returned_qty"),
    "CARRIER_STAFF": ("product_code", "batch_number", "planned_qty"),
    "AUDITOR": ("__all__",),
    "ADMIN": ("__all__",),
}

_EVENT_FIELDS = {
    "STORE_STAFF": ("seq", "event_type", "occurred_at", "holder_type", "holder_code",
                    "seal_code", "location", "reason"),
    "CARRIER_STAFF": ("seq", "event_type", "occurred_at", "holder_type", "holder_code",
                      "seal_code", "location", "device_id"),
    "AUDITOR": ("__all__",),
    "ADMIN": ("__all__",),
}


def _party_name(conn: Connection, code: str | None) -> str | None:
    if not code:
        return None
    row = conn.execute(select(parties.c.name).where(parties.c.code == code)).first()
    return row[0] if row else None


def full_request(conn: Connection, req, lines: list[dict]) -> dict[str, Any]:
    """组装完整申请事实（仅服务内部使用，输出前必须经 project_request）。"""
    rid = req["id"]
    case_rows = conn.execute(
        select(
            investigations.c.case_number,
            investigations.c.reason,
            investigations.c.status,
            investigations.c.opened_at,
            investigations.c.resolved_at,
            investigations.c.outcome,
        ).where(investigations.c.request_id == rid)
    ).mappings().all()
    review_rows = conn.execute(
        select(
            reviews.c.review_role, reviews.c.actor_id, reviews.c.decision,
            reviews.c.note, reviews.c.created_at,
        ).where(reviews.c.request_id == rid)
    ).mappings().all()
    risk_rows = conn.execute(
        select(
            risk_notes.c.category, risk_notes.c.note,
            risk_notes.c.actor_id, risk_notes.c.created_at,
        ).where(risk_notes.c.request_id == rid)
    ).mappings().all()
    snapshot = json.loads(req["snapshot_json"]) if req["snapshot_json"] else None
    return {
        "id": rid,
        "request_number": req["request_number"],
        "status": req["status"],
        "sender_code": req["sender_code"],
        "receiver_code": req["receiver_code"],
        "carrier_code": req["carrier_code"],
        "carrier_name": _party_name(conn, req["carrier_code"]),
        "sender_name": _party_name(conn, req["sender_code"]),
        "receiver_name": _party_name(conn, req["receiver_code"]),
        "planned_dispatched_at": req["planned_dispatched_at"],
        "planned_received_at": req["planned_received_at"],
        "created_at": req["created_at"],
        "dispatched_at": req["dispatched_at"],
        "received_at": req["received_at"],
        "cancelled_at": req["cancelled_at"],
        "rejected_at": req["rejected_at"],
        "returned_at": req["returned_at"],
        "reject_reason": req["reject_reason"],
        "seal_code": req["seal_code"],
        "driver_name": req["driver_name"],
        "vehicle_no": req["vehicle_no"],
        "snapshot_valid": req["snapshot_valid"],
        "revalidated_at": req["revalidated_at"],
        "lines": [dict(l) for l in lines],
        "reviews": [dict(r) for r in review_rows],
        "investigations": [dict(r) for r in case_rows],
        "open_investigations": [r["case_number"] for r in case_rows if r["status"] == "OPEN"],
        "risk_notes": [dict(r) for r in risk_rows],
        "snapshot": snapshot,
    }


def _scope_allowed(actor, full: dict[str, Any]) -> bool:
    if actor["role"] in ("ADMIN", "AUDITOR"):
        return True
    if actor["role"] == "STORE_STAFF":
        return actor["party_code"] in (full["sender_code"], full["receiver_code"])
    if actor["role"] == "CARRIER_STAFF":
        return actor["party_code"] == full["carrier_code"]
    return False


def _filter_fields(data: dict[str, Any], allowed) -> dict[str, Any]:
    if "__all__" in allowed:
        return data
    return {k: v for k, v in data.items() if k in allowed}


def project_request(conn: Connection, actor, req, lines: list[dict]) -> dict[str, Any]:
    full = full_request(conn, req, lines)
    if not _scope_allowed(actor, full):
        from .errors import Conflict

        raise Conflict("FORBIDDEN", "该申请不在你的履职范围内")
    role = actor["role"]
    full["my_role"] = (
        "SENDER" if actor["party_code"] == full["sender_code"]
        else "RECEIVER" if actor["party_code"] == full["receiver_code"]
        else "CARRIER" if actor["party_code"] == full["carrier_code"]
        else role
    )
    line_allowed = _LINE_FIELDS[role]
    full["lines"] = [_filter_fields(l, line_allowed) for l in full["lines"]]
    return _filter_fields(full, _FIELDS_BY_ROLE[role])


def project_chain(conn: Connection, actor, request_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        select(custody_events)
        .where(custody_events.c.request_id == request_id)
        .order_by(custody_events.c.occurred_at, custody_events.c.id)
    ).mappings().all()
    allowed = _EVENT_FIELDS[actor["role"]]
    result = []
    for row in rows:
        event = dict(row)
        if "__all__" not in allowed:
            event = {k: v for k, v in event.items() if k in allowed}
        if "__all__" in allowed:
            event["items"] = [
                dict(r)
                for r in conn.execute(
                    select(custody_items).where(custody_items.c.event_id == row["id"])
                ).mappings().all()
            ]
        result.append(event)
    return result


def list_requests(conn: Connection, actor) -> list[dict[str, Any]]:
    stmt = select(transfer_requests)
    if actor["role"] == "STORE_STAFF":
        stmt = stmt.where(
            (transfer_requests.c.sender_code == actor["party_code"])
            | (transfer_requests.c.receiver_code == actor["party_code"])
        )
    elif actor["role"] == "CARRIER_STAFF":
        stmt = stmt.where(transfer_requests.c.carrier_code == actor["party_code"])
    reqs = conn.execute(stmt.order_by(transfer_requests.c.id.desc())).mappings().all()
    out = []
    allowed = _FIELDS_BY_ROLE[actor["role"]]
    for req in reqs:
        lines = conn.execute(
            select(transfer_lines).where(transfer_lines.c.request_id == req["id"])
        ).mappings().all()
        projected = project_request(conn, actor, req, lines)
        out.append(_filter_fields(projected, allowed))
    return out
