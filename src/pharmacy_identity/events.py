"""保管事件追加器：序号、哈希链与幂等裁决集中于此。"""

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection

from . import clock
from .errors import Conflict
from .hashing import event_hash
from .schema import custody_events, custody_items

# 会引起批次数量移动的事件类型；移动方向在 ledger 中解释。
MOVEMENT_EVENTS = {
    "BASELINE",
    "DISPATCHED",
    "RECEIVED",
    "QUARANTINED",
    "RETURNED",
    "INVESTIGATION_RESOLVED",
}


def append_event(
    conn: Connection,
    *,
    event_type: str,
    holder_type: str,
    holder_code: str,
    request_id: int | None = None,
    actor_id: str | None = None,
    device_id: str | None = None,
    occurred_at: str | None = None,
    seal_code: str | None = None,
    location: str | None = None,
    idempotency_key: str | None = None,
    reason: str | None = None,
    note: str | None = None,
    payload: dict[str, Any] | None = None,
    items: list[dict[str, Any]] | None = None,
) -> int:
    """追加一个保管事件。调用方必须已在写事务中。

    序号按申请内递增；哈希为全局链：覆盖业务字段、明细与上一事件哈希。
    封签唯一性冲突由数据库部分唯一索引裁决（双终端只有一个成功）。
    """
    occurred = clock.normalize(occurred_at) if occurred_at else clock.now()
    recorded = clock.now()

    seq: int | None = None
    if request_id is not None:
        row = conn.execute(
            select(custody_events.c.seq)
            .where(custody_events.c.request_id == request_id)
            .order_by(custody_events.c.seq.desc())
            .limit(1)
        ).first()
        seq = (row[0] or 0) + 1 if row else 1

    prev_row = conn.execute(
        select(custody_events.c.event_hash).order_by(custody_events.c.id.desc()).limit(1)
    ).first()
    prev_hash = prev_row[0] if prev_row else None

    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True) if payload else None
    digest = event_hash(
        prev_hash,
        {
            "request_id": request_id,
            "seq": seq,
            "event_type": event_type,
            "actor_id": actor_id,
            "device_id": device_id,
            "occurred_at": occurred,
            "holder_type": holder_type,
            "holder_code": holder_code,
            "seal_code": seal_code,
            "location": location,
            "reason": reason,
            "note": note,
            "payload": payload,
            "items": sorted(
                [
                    [i["product_code"], i["batch_number"], i["qty"]]
                    for i in (items or [])
                ]
            ),
        },
    )

    try:
        result = conn.execute(
            custody_events.insert().values(
                request_id=request_id,
                seq=seq,
                event_type=event_type,
                actor_id=actor_id,
                device_id=device_id,
                occurred_at=occurred,
                recorded_at=recorded,
                holder_type=holder_type,
                holder_code=holder_code,
                seal_code=seal_code,
                location=location,
                idempotency_key=idempotency_key,
                reason=reason,
                note=note,
                payload_json=payload_json,
                prev_hash=prev_hash,
                event_hash=digest,
            )
        )
    except Exception as exc:  # SQLite IntegrityError 的统一转译
        message = str(exc)
        if "ux_events_received_seal" in message:
            raise Conflict("SEAL_ALREADY_CONFIRMED", "该封签已被签收，重复确认被拒绝") from exc
        if "ux_events_dispatch_seal" in message:
            raise Conflict("SEAL_ALREADY_DISPATCHED", "该封签已用于另一次出库") from exc
        if "ux_events_scan_idem" in message:
            raise Conflict("DUPLICATE_SCAN", "重复扫码") from exc
        raise

    event_id = result.inserted_primary_key[0]
    for item in items or []:
        conn.execute(
            custody_items.insert().values(
                event_id=event_id,
                product_code=item["product_code"],
                batch_number=item["batch_number"],
                qty=item["qty"],
            )
        )
    return event_id


def find_scan(conn: Connection, device_id: str, idempotency_key: str) -> dict[str, Any] | None:
    """重复扫码：按设备与幂等键找回原事件。"""
    row = conn.execute(
        select(custody_events)
        .where(custody_events.c.device_id == device_id)
        .where(custody_events.c.idempotency_key == idempotency_key)
        .limit(1)
    ).mappings().first()
    return dict(row) if row else None


def seal_taken(conn: Connection, seal_code: str, event_type: str) -> bool:
    """封签是否已被指定类型的事件占用。唯一索引是最终裁决，这里给出友好错误。"""
    return conn.execute(
        select(custody_events.c.id)
        .where(custody_events.c.seal_code == seal_code)
        .where(custody_events.c.event_type == event_type)
        .limit(1)
    ).first() is not None
