"""保管事件哈希链。

每个事件的哈希覆盖其全部业务字段与上一事件哈希；任何篡改（删除、改数、
改时刻）都会让链尾重算值与库存值不一致，审计据此发现历史被改动。
"""

import hashlib
import json
from typing import Any

GENESIS = "0" * 64


def payload_digest(payload: Any) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def event_hash(prev_hash: str | None, fields: dict[str, Any]) -> str:
    material = {"prev": prev_hash or GENESIS, **fields}
    return payload_digest(material)
