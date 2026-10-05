"""预警单一真源：顶条、消费预演、全层分类共用同一规则。

规则只依赖三类输入：
- 批次自身事实：status / qty_remain / expiry（入库时写入，改 warn_days 永不变更）
- 当天日期
- 设置 warn_days（仅影响 soon 带的宽窄）

expired 是日历事实（expiry < today），与 warn_days 无关：
过期下架的批次不会因为调大 warn_days 回到在架。
"""

from datetime import date


def get_warn_days(conn) -> int:
    return int(conn.execute("SELECT value FROM settings WHERE key='warn_days'").fetchone()["value"])


def classify(expiry: str | None, today: str, warn_days: int) -> tuple[str | None, int | None]:
    """返回 (level, days_left)。level ∈ {'expired','soon',None}。

    顶条、预演、任何需要 soon/expired 判断的地方都只能走这里，
    禁止各自再写一套比较。
    """
    if not expiry:
        return None, None
    if expiry < today:
        return "expired", None
    days_left = (date.fromisoformat(expiry) - date.fromisoformat(today)).days
    if days_left <= warn_days:
        return "soon", days_left
    return None, days_left


def build_alerts(conn, today: str | None = None) -> list[dict]:
    """按当前 warn_days 现算顶条（只看在架、有余量、有到期日）。

    返回的每一条都必然同时能在全层（on_shelf 且余量>0）找到，
    因此不会出现「顶条报紧急而全层没有该批」。
    """
    today = today or date.today().isoformat()
    warn_days = get_warn_days(conn)
    rows = [dict(r) for r in conn.execute(
        """SELECT lots.*, items.name, items.layer FROM lots
           JOIN items ON items.id=lots.item_id
           WHERE status='on_shelf' AND qty_remain>0 AND expiry IS NOT NULL""")]
    out = []
    for r in rows:
        level, days_left = classify(r["expiry"], today, warn_days)
        if level:
            r["level"] = level
            if days_left is not None:
                r["days_left"] = days_left
            out.append(r)
    return out
