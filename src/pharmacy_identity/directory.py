"""身份目录：主体、版本化许可与关系、药品批次与调拨快照。

许可证/关系纠正不改写旧行：旧版本置 CORRECTED 并新增版本。调拨申请在
创建时刻冻结当时的许可、承运资质、批号与数量；纠正发生时只重验尚未出库
的申请，对已有出库/完成交接的申请追加风险说明。
"""

import json
import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.engine import Connection

from . import clock
from .errors import Conflict, NotFound
from .events import append_event
from .schema import (
    actors,
    batches,
    drugs,
    licenses,
    parties,
    quarantine_locations,
    relationships,
    risk_notes,
    transfer_lines,
    transfer_requests,
)

LICENSE_STORE = "STORE_CONTROLLED_DRUG"
LICENSE_CARRIER = "CARRIER_QUALIFICATION"


# --- 基础登记 ---------------------------------------------------------------


def register_party(conn: Connection, code: str, name: str, type_: str, jurisdiction: str) -> None:
    if conn.execute(select(parties.c.code).where(parties.c.code == code)).first():
        raise Conflict("PARTY_EXISTS", f"主体 {code} 已存在")
    conn.execute(
        parties.insert().values(
            code=code, name=name, type=type_, jurisdiction=jurisdiction, created_at=clock.now()
        )
    )


def register_actor(
    conn: Connection,
    actor_id: str,
    display_name: str,
    role: str,
    party_code: str | None = None,
    can_review: bool = False,
) -> None:
    conn.execute(
        actors.insert().values(
            actor_id=actor_id,
            display_name=display_name,
            role=role,
            party_code=party_code,
            can_review=can_review,
            active=True,
            created_at=clock.now(),
        )
    )


def register_quarantine(conn: Connection, code: str, name: str, jurisdiction: str) -> None:
    if conn.execute(
        select(quarantine_locations.c.code).where(quarantine_locations.c.code == code)
    ).first():
        raise Conflict("QUARANTINE_EXISTS", f"隔离区 {code} 已存在")
    conn.execute(
        quarantine_locations.insert().values(
            code=code, name=name, jurisdiction=jurisdiction, created_at=clock.now()
        )
    )


def register_drug(conn: Connection, product_code: str, name: str, controlled_class: str) -> None:
    conn.execute(
        drugs.insert().values(
            product_code=product_code,
            name=name,
            controlled_class=controlled_class,
            created_at=clock.now(),
        )
    )


def register_batch(
    conn: Connection,
    product_code: str,
    batch_number: str,
    initial_qty: int,
    holder_code: str,
    expiry_date: str | None = None,
) -> None:
    """登记批次并写入 BASELINE 保管事件：链路从源头即守恒。"""
    conn.execute(
        batches.insert().values(
            product_code=product_code,
            batch_number=batch_number,
            initial_qty=initial_qty,
            expiry_date=expiry_date,
            holder_party_code=holder_code,
            registered_at=clock.now(),
        )
    )
    append_event(
        conn,
        event_type="BASELINE",
        holder_type="STORE",
        holder_code=holder_code,
        items=[{"product_code": product_code, "batch_number": batch_number, "qty": initial_qty}],
        payload={
            "movements": [
                {
                    "product_code": product_code,
                    "batch_number": batch_number,
                    "qty": initial_qty,
                    "from": None,
                    "to": {"holder_type": "STORE", "holder_code": holder_code},
                }
            ]
        },
    )


# --- 许可证与关系（版本化）---------------------------------------------------


def add_license(
    conn: Connection,
    party_code: str,
    license_type: str,
    license_number: str,
    valid_from: str,
    valid_to: str,
) -> int:
    result = conn.execute(
        licenses.insert().values(
            party_code=party_code,
            license_type=license_type,
            license_number=license_number,
            valid_from=clock.normalize(valid_from),
            valid_to=clock.normalize(valid_to),
            status="ACTIVE",
            corrected_at=None,
            created_at=clock.now(),
        )
    )
    return result.inserted_primary_key[0]


def add_relationship(
    conn: Connection,
    party_code: str,
    related_party_code: str,
    relation_type: str,
    valid_from: str,
    valid_to: str,
) -> int:
    result = conn.execute(
        relationships.insert().values(
            party_code=party_code,
            related_party_code=related_party_code,
            relation_type=relation_type,
            valid_from=clock.normalize(valid_from),
            valid_to=clock.normalize(valid_to),
            status="ACTIVE",
            corrected_at=None,
            created_at=clock.now(),
        )
    )
    return result.inserted_primary_key[0]


def change_license_status(
    conn: Connection, license_id: int, new_status: str, note: str, actor_id: str | None = None
) -> None:
    """暂停/吊销/恢复许可，并扫描在途与计划申请。"""
    row = conn.execute(select(licenses).where(licenses.c.id == license_id)).mappings().first()
    if not row:
        raise NotFound("许可证不存在")
    if row["status"] == "CORRECTED":
        raise Conflict("LICENSE_VERSION_CLOSED", "已纠正版本不能再变更状态")
    conn.execute(licenses.update().where(licenses.c.id == license_id).values(status=new_status))
    _sweep_requests(
        conn,
        _requests_for_party(conn, row["party_code"]),
        "LICENSE_CORRECTED" if new_status == "CORRECTED" else "OTHER",
        f"许可状态变更为 {new_status}：{note}",
        actor_id=actor_id,
    )


def correct_license(
    conn: Connection,
    license_id: int,
    *,
    license_number: str,
    valid_from: str,
    valid_to: str,
    actor_id: str,
    note: str,
) -> int:
    """以新版本纠正许可证：旧版本置 CORRECTED，原记录原样保留。"""
    row = conn.execute(select(licenses).where(licenses.c.id == license_id)).mappings().first()
    if not row:
        raise NotFound("许可证不存在")
    conn.execute(
        licenses.update()
        .where(licenses.c.id == license_id)
        .values(status="CORRECTED", corrected_at=clock.now())
    )
    new_id = add_license(
        conn, row["party_code"], row["license_type"], license_number, valid_from, valid_to
    )
    _sweep_requests(
        conn,
        _requests_for_party(conn, row["party_code"]),
        "LICENSE_CORRECTED",
        f"许可证 {row['license_type']} 已纠正：{note}",
        actor_id=actor_id,
    )
    return new_id


def correct_relationship(
    conn: Connection,
    relationship_id: int,
    *,
    related_party_code: str,
    valid_from: str,
    valid_to: str,
    actor_id: str,
    note: str,
) -> int:
    row = conn.execute(
        select(relationships).where(relationships.c.id == relationship_id)
    ).mappings().first()
    if not row:
        raise NotFound("关系不存在")
    conn.execute(
        relationships.update()
        .where(relationships.c.id == relationship_id)
        .values(status="CORRECTED", corrected_at=clock.now())
    )
    new_id = add_relationship(
        conn,
        row["party_code"],
        related_party_code,
        row["relation_type"],
        valid_from,
        valid_to,
    )
    request_ids: set[int] = set()
    for code in (row["party_code"], row["related_party_code"], related_party_code):
        request_ids.update(_requests_for_party(conn, code))
    _sweep_requests(
        conn,
        sorted(request_ids),
        "RELATIONSHIP_CORRECTED",
        f"关系 {row['relation_type']} 已纠正：{note}",
        actor_id=actor_id,
    )
    return new_id


def _requests_for_party(conn: Connection, party_code: str) -> list[int]:
    rows = conn.execute(
        select(transfer_requests.c.id).where(
            or_(
                transfer_requests.c.sender_code == party_code,
                transfer_requests.c.receiver_code == party_code,
                transfer_requests.c.carrier_code == party_code,
            )
        )
    ).all()
    return [r[0] for r in rows]


def _sweep_requests(
    conn: Connection,
    request_ids: list[int],
    category: str,
    note: str,
    actor_id: str | None = None,
) -> int:
    """许可/关系变化后的影响面。

    未出库（PLANNED）申请立即按现状重验并冻结结论；已出库或已交接的
    申请历史不可改写，只追加风险说明。
    """
    changed = 0
    for rid in request_ids:
        req = conn.execute(
            select(transfer_requests).where(transfer_requests.c.id == rid)
        ).mappings().first()
        if not req or req["status"] == "CANCELLED":
            continue
        if req["status"] == "PLANNED":
            violations = revalidate(conn, req)
            conn.execute(
                transfer_requests.update()
                .where(transfer_requests.c.id == rid)
                .values(snapshot_valid=not violations, revalidated_at=clock.now())
            )
        else:
            # 已出库/已完成：历史不可动，仅追加风险说明（追加型表）。
            current_violations = revalidate(conn, req)
            detail = f"；重验发现：{'；'.join(current_violations)}" if current_violations else ""
            conn.execute(
                risk_notes.insert().values(
                    request_id=rid,
                    category=category,
                    note=note + detail,
                    actor_id=actor_id or "SYSTEM",
                    created_at=clock.now(),
                )
            )
        changed += 1
    return changed


# --- 快照冻结与校验 ----------------------------------------------------------


def _active_license_versions(conn: Connection, party_code: str, license_type: str) -> list[dict]:
    rows = conn.execute(
        select(licenses)
        .where(licenses.c.party_code == party_code)
        .where(licenses.c.license_type == license_type)
        .where(licenses.c.status == "ACTIVE")
    ).mappings().all()
    return [dict(r) for r in rows]


def build_snapshot(
    conn: Connection,
    sender_code: str,
    receiver_code: str,
    carrier_code: str,
    planned_dispatched_at: str,
    planned_received_at: str,
    lines: list[dict[str, Any]],
) -> dict[str, Any]:
    """冻结计划时刻所见的许可、承运资质、关系版本与批号数量。"""
    wanted_pairs = (
        {sender_code, carrier_code},
        {sender_code, receiver_code},
    )
    rel_rows = [
        dict(r)
        for r in conn.execute(select(relationships).where(relationships.c.status == "ACTIVE")).mappings()
        if {r["party_code"], r["related_party_code"]} in wanted_pairs
    ]
    return {
        "frozen_at": clock.now(),
        "planned_dispatched_at": planned_dispatched_at,
        "planned_received_at": planned_received_at,
        "licenses": [
            dict(r)
            for code, ltype in (
                (sender_code, LICENSE_STORE),
                (receiver_code, LICENSE_STORE),
                (carrier_code, LICENSE_CARRIER),
            )
            for r in _active_license_versions(conn, code, ltype)
        ],
        "relationships": rel_rows,
        "batches": [dict(r) for r in conn.execute(select(batches)).mappings().all()],
        "lines": lines,
    }


def _covers(valid_from: str, valid_to: str, moment: str) -> bool:
    return clock.parse(valid_from) <= clock.parse(moment) <= clock.parse(valid_to)


def validate_live(
    conn: Connection,
    sender_code: str,
    receiver_code: str,
    carrier_code: str,
    planned_dispatched_at: str,
    planned_received_at: str,
    lines: list[dict[str, Any]],
    available_balance: dict[tuple[str, str], int],
    reserved: dict[tuple[str, str], int],
) -> list[str]:
    """创建/出库前按当前事实校验。available_balance 来自台账重放。"""
    violations: list[str] = []

    party_rows = {
        r[0]: r[1]
        for r in conn.execute(select(parties.c.code, parties.c.type)).all()
    }
    for code, expect in (
        (sender_code, "STORE"),
        (receiver_code, "STORE"),
        (carrier_code, "CARRIER"),
    ):
        if party_rows.get(code) != expect:
            violations.append(f"主体 {code} 不存在或类型不是 {expect}")

    def _license_ok(code: str, ltype: str, moment: str) -> None:
        rows = _active_license_versions(conn, code, ltype)
        if not rows:
            violations.append(f"{code} 缺少有效的 {ltype}")
        elif not any(_covers(r["valid_from"], r["valid_to"], moment) for r in rows):
            violations.append(f"{code} 的 {ltype} 未覆盖计划时刻（暂停/过期）")

    _license_ok(sender_code, LICENSE_STORE, planned_dispatched_at)
    _license_ok(receiver_code, LICENSE_STORE, planned_received_at)
    _license_ok(carrier_code, LICENSE_CARRIER, planned_dispatched_at)

    rel_rows = conn.execute(select(relationships).where(relationships.c.status == "ACTIVE")).mappings()
    pairs = {
        tuple(sorted((sender_code, carrier_code))): "STORE_CARRIER",
        tuple(sorted((sender_code, receiver_code))): "SENDER_RECEIVER",
    }
    covered: set[tuple[str, str]] = set()
    for rel in rel_rows:
        pair = tuple(sorted((rel["party_code"], rel["related_party_code"])))
        if pair in pairs and rel["relation_type"] == pairs[pair]:
            if _covers(rel["valid_from"], rel["valid_to"], planned_dispatched_at) or _covers(
                rel["valid_from"], rel["valid_to"], planned_received_at
            ):
                covered.add(pair)
    for pair, rel_type in pairs.items():
        if pair not in covered:
            violations.append(f"{pair[0]} 与 {pair[1]} 缺少覆盖计划时段的 {rel_type} 关系")

    batch_index = {
        (b[0], b[1]): b
        for b in conn.execute(
            select(
                batches.c.product_code,
                batches.c.batch_number,
            )
        ).all()
    }
    seen: set[tuple[str, str]] = set()
    for line in lines:
        key = (line["product_code"], line["batch_number"])
        if key in seen:
            violations.append(f"批号 {key[1]} 在同一申请中重复")
            continue
        seen.add(key)
        batch = batch_index.get(key)
        if not batch:
            violations.append(f"批号 {key[1]}（{key[0]}）不存在")
            continue
        qty = line["planned_qty"]
        if not isinstance(qty, int) or qty <= 0:
            violations.append(f"批号 {key[1]} 数量必须为正整数")
            continue
        free = available_balance.get(key, 0) - reserved.get(key, 0)
        if qty > free:
            violations.append(f"批号 {key[1]} 可冻结数量不足：申请 {qty}，可用 {max(free, 0)}")

    if clock.parse(planned_received_at) < clock.parse(planned_dispatched_at):
        violations.append("计划接收时刻不得早于计划出库时刻")
    return violations


def revalidate(conn: Connection, req) -> list[str]:
    """许可/关系纠正后重验：按冻结快照中的版本 ID 回到数据库核对现状。"""
    snapshot = json.loads(req["snapshot_json"])
    violations: list[str] = []
    for frozen in snapshot["licenses"]:
        row = conn.execute(
            select(licenses.c.status, licenses.c.valid_from, licenses.c.valid_to).where(
                licenses.c.id == frozen["id"]
            )
        ).first()
        if row is None:
            violations.append(f"冻结许可证版本 {frozen['id']} 已不存在")
            continue
        status, valid_from, valid_to = row
        if status != "ACTIVE":
            violations.append(
                f"{frozen['party_code']} 的 {frozen['license_type']} 现为 {status}"
            )
        elif not (
            _covers(valid_from, valid_to, snapshot["planned_dispatched_at"])
            or _covers(valid_from, valid_to, snapshot["planned_received_at"])
        ):
            violations.append(f"{frozen['party_code']} 的 {frozen['license_type']} 不再覆盖计划时段")

    for frozen in snapshot.get("relationships", []):
        row = conn.execute(
            select(relationships.c.status).where(relationships.c.id == frozen["id"])
        ).first()
        if row is None or row[0] != "ACTIVE":
            violations.append(
                f"冻结关系版本 {frozen['id']}（{frozen['party_code']}↔{frozen['related_party_code']}）已失效"
            )
    return violations


def reserved_quantities(conn: Connection, exclude_request_id: int | None = None) -> dict[tuple[str, str], int]:
    """已被其他 PLANNED 申请冻结、尚未出库的数量。"""
    query = (
        select(
            transfer_lines.c.product_code,
            transfer_lines.c.batch_number,
            transfer_lines.c.planned_qty,
        )
        .select_from(transfer_lines.join(transfer_requests, transfer_lines.c.request_id == transfer_requests.c.id))
        .where(transfer_requests.c.status == "PLANNED")
        .where(transfer_requests.c.snapshot_valid.is_(True))
    )
    if exclude_request_id is not None:
        query = query.where(transfer_requests.c.id != exclude_request_id)
    reserved: dict[tuple[str, str], int] = {}
    for product_code, batch_number, qty in conn.execute(query).all():
        reserved[(product_code, batch_number)] = reserved.get((product_code, batch_number), 0) + qty
    return reserved


def new_request_number() -> str:
    return f"TR-{uuid.uuid4().hex[:12].upper()}"


def new_case_number() -> str:
    return f"INV-{uuid.uuid4().hex[:12].upper()}"
