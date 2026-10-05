"""预警分类的唯一真源。

不变量：
- warn_days 只决定派生的 level（soon 窗口）；批次 status / qty_remain / expiry
  永远以 lots 表为准，改设置不写 lots，到期日字符串始终是入库时的值。
- 顶条（alerts）、消费预演（preview）、全层（fridge）共用 is_on_shelf 谓词与
  classify 分类，禁止三处各写一套；由此保证「顶条报紧急 ⇒ 全层必有该批」。
- 已 expired 下架（status='expired'）的批对 warn_days 不可见，改天数不会复活。
"""
from __future__ import annotations

from datetime import date

EXPIRED = "expired"
SOON = "soon"
FRESH = "fresh"

WARN_DAYS_KEY = "warn_days"


def parse_warn_days(raw) -> int:
    """严格正整数；bool、小数、非数字串、<=0 一律 ValueError。"""
    if isinstance(raw, bool):
        raise ValueError("warn_days must be a positive integer")
    if isinstance(raw, int):
        v = raw
    elif isinstance(raw, str) and raw.strip().isdigit():
        v = int(raw.strip())
    else:
        raise ValueError("warn_days must be a positive integer")
    if v < 1:
        raise ValueError("warn_days must be a positive integer")
    return v


def get_warn_days(c) -> int:
    """库内存的始终是合法正整数（写入端已校验）。"""
    row = c.execute(
        "SELECT value FROM settings WHERE key=?", (WARN_DAYS_KEY,)
    ).fetchone()
    return parse_warn_days(row["value"] if row else None)


def classify(expiry: str | None, today: date, warn_days: int) -> str | None:
    """expired: expiry<=today；soon: 剩余天数<=warn_days；fresh；无到期日 None。"""
    if not expiry:
        return None
    d = date.fromisoformat(expiry)
    if d <= today:
        return EXPIRED
    return SOON if (d - today).days <= warn_days else FRESH
