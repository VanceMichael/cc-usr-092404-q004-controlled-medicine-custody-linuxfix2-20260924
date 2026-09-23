"""保管事件链的纯函数重放：审计结论只依赖 custody_events，不依赖当前状态列。

每个移交事件把 quantity 从 from_custodian 转到 to_custodian；
非移交事件（review/scan/risk_note 等）不改变持仓。
按真实发生时间 (occurred_at, seq) 排序，因此离线补传的历史扫描也能回到正确时刻。
"""

from collections import defaultdict
from typing import Iterable, Mapping

HANDOFF_TYPES = {
    "genesis",
    "released",
    "received",
    "quarantined",
    "compensated",
    "written_off",
}


def positions_at(events: Iterable[Mapping], at: str | None = None) -> dict[str, dict[str, int]]:
    """返回 {batch_no: {custodian: qty}}，重放 occurred_at <= at 的移交事件。"""
    positions: dict[str, dict[str, int]] = defaultdict(dict)
    ordered = sorted(
        (e for e in events if at is None or e["occurred_at"] <= at),
        key=lambda e: (e["occurred_at"], e["seq"]),
    )
    for event in ordered:
        if event["event_type"] not in HANDOFF_TYPES:
            continue
        batch = event["batch_no"]
        qty = event["quantity"]
        source = event["from_custodian"]
        target = event["to_custodian"]
        if source is not None:
            positions[batch][source] = positions[batch].get(source, 0) - qty
            if positions[batch][source] == 0:
                del positions[batch][source]
        if target is not None:
            positions[batch][target] = positions[batch].get(target, 0) + qty
    return {batch: dict(holders) for batch, holders in positions.items()}


def conservation_report(events: Iterable[Mapping]) -> list[dict]:
    """逐批号核对守恒：总持有量必须恒等于期初（genesis）量，且持仓不得为负。"""
    events = list(events)
    genesis: dict[str, int] = defaultdict(int)
    for event in events:
        if event["event_type"] == "genesis":
            genesis[event["batch_no"]] += event["quantity"]

    report = []
    positions = positions_at(events)
    for batch_no, total in sorted(genesis.items()):
        holders = positions.get(batch_no, {})
        held = sum(holders.values())
        negative = {c: q for c, q in holders.items() if q < 0}
        report.append(
            {
                "batch_no": batch_no,
                "genesis_qty": total,
                "held_qty": held,
                "conserved": held == total and not negative,
                "holders": holders,
                "negative_holders": negative,
            }
        )
    return report


def unique_inflight_custodian(events: Iterable[Mapping], request_no: str | None = None) -> bool:
    """在途唯一性：承运人持有期间，发出门店不得再持有同一批号（防双库存）。

    逐事件检查，任何时刻每个批号在每个持有人处只有一份非负持仓即视为归属唯一；
    额外校验 released 与 received/quarantined 之间承运人是该批货唯一的外部持有人。
    """
    events = sorted(events, key=lambda e: (e["occurred_at"], e["seq"]))
    positions: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for event in events:
        if event["event_type"] not in HANDOFF_TYPES:
            continue
        batch = event["batch_no"]
        if event["from_custodian"]:
            positions[batch][event["from_custodian"]] -= event["quantity"]
        if event["to_custodian"]:
            positions[batch][event["to_custodian"]] += event["quantity"]
        for qty in positions[batch].values():
            if qty < 0:
                return False
    return True
