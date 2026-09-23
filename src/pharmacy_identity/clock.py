"""时钟工具：所有时刻统一为带时区的 UTC ISO-8601 字符串。

离线扫描携带真实发生时间，服务端不允许用记录时间替代发生时间；
传入的时间字符串会被解析、归一化，跨午夜运输由此得到正确归属。
"""

from datetime import datetime, timezone
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize(value: Any) -> str:
    """把任意 ISO-8601 输入归一化为 UTC；裸时间视为 UTC。"""
    if value is None:
        raise ValueError("时间不能为空")
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    return parse(str(value)).isoformat()


def parse(value: str) -> datetime:
    import re

    text = value
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # 容忍查询串中未编码的 “+HH:MM”（HTTP 会把 + 解成空格）。
    text = re.sub(r" (\d{2}:\d{2})$", r"+\1", text)
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
