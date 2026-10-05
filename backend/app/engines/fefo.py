"""FEFO consume: earliest expiry first among positive remaining lots.

消费预演（build_plan）与顶条共用 app.engines.alerting.classify：
预演里标注的 soon/expired 资格，与提交时刻重算、顶条现算永远是同一套规则。
"""

import hashlib
import json
from datetime import date

from app.engines.alerting import classify


def sort_lots_fefo(lots: list[dict]) -> list[dict]:
    return sorted(
        [l for l in lots if float(l.get("qty_remain", 0)) > 0],
        key=lambda l: (l.get("expiry") or "9999-12-31", l.get("id") or 0),
    )


def consume_fefo(lots: list[dict], qty: float) -> dict:
    """Return deductions list and leftover demand. Mutates copies only."""
    need = float(qty)
    if need <= 0:
        return {"ok": False, "reason": "qty_non_positive", "deductions": [], "short": 0.0}
    ordered = sort_lots_fefo(lots)
    deductions = []
    for lot in ordered:
        if need <= 0:
            break
        avail = float(lot["qty_remain"])
        take = min(avail, need)
        deductions.append({"lot_id": lot["id"], "take": take, "expiry": lot.get("expiry")})
        need -= take
    if need > 1e-9:
        return {"ok": False, "reason": "short", "deductions": deductions, "short": round(need, 3)}
    return {"ok": True, "reason": "", "deductions": deductions, "short": 0.0}


def _basis_fingerprint(qty: float, lots: list[dict], warn_days: int, today: str) -> str:
    """预演所依赖事实的指纹：需求数量、在架正余量批次（余量/到期日）、当天、warn_days。

    任一项在提交时变化（设置改过、日期翻转、批次被消费或下架、入库了更早到期的批）
    都会导致指纹不符 → 拒绝提交。
    """
    basis = [
        [l["id"], round(float(l["qty_remain"]), 6), l.get("expiry"), l.get("status")]
        for l in sorted(lots, key=lambda l: l["id"])
    ]
    payload = json.dumps(
        {"qty": round(float(qty), 6), "lots": basis, "warn_days": warn_days, "today": today},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_plan(lots: list[dict], qty: float, warn_days: int, today: str | None = None) -> dict:
    """预演：FEFO 扣减明细 + 每条按当前 warn_days/今日标注 level（单一真源）。

    返回 ok=True 时带 plan_hash，提交必须原样回传；ok=False（数量非正/不足）无 plan_hash。
    level 仅来自 alerting.classify，预演资格不自建规则。
    """
    today = today or date.today().isoformat()
    result = consume_fefo(lots, qty)
    plan = {
        **result,
        "warn_days": warn_days,
        "today": today,
        "basis": [
            _lot_basis(l, today, warn_days)
            for l in sort_lots_fefo(lots)
        ],
    }
    for d in plan["deductions"]:
        level, days_left = classify(d.get("expiry"), today, warn_days)
        d["level"] = level
        if days_left is not None:
            d["days_left"] = days_left
    if result["ok"]:
        plan["plan_hash"] = _basis_fingerprint(qty, sort_lots_fefo(lots), warn_days, today)
    return plan


def _lot_basis(lot: dict, today: str, warn_days: int) -> dict:
    level, days_left = classify(lot.get("expiry"), today, warn_days)
    return {
        "lot_id": lot["id"],
        "qty_remain": float(lot["qty_remain"]),
        "expiry": lot.get("expiry"),
        "level": level,
        "days_left": days_left,
    }


def verify_plan(plan: dict, lots: list[dict], warn_days: int, today: str) -> tuple[bool, str]:
    """提交校验：用数据库当前事实重算指纹，与预演指纹必须一致。

    这保证「预演资格 / 顶条现算 / 全层真源」提交时是同一份 warn_days 与同一天：
    预演后改过设置、批次状态或余量变化，一律整单拒绝，不做任何扣减。
    """
    if not plan or not plan.get("ok") or not plan.get("plan_hash"):
        return False, "invalid_plan"
    if int(plan.get("warn_days", -1)) != warn_days or plan.get("today") != today:
        return False, "stale_preview"
    current = sort_lots_fefo(lots)
    if _basis_fingerprint(plan_qty(plan), current, warn_days, today) != plan["plan_hash"]:
        return False, "stale_preview"
    # 指纹已覆盖，再逐笔确认批次仍在架且余量足够，防止异常数据落库
    remain = {l["id"]: float(l["qty_remain"]) for l in current if l.get("status") == "on_shelf"}
    for d in plan.get("deductions", []):
        avail = remain.get(d["lot_id"])
        if avail is None or avail + 1e-9 < float(d["take"]):
            return False, "stale_preview"
    return True, ""


def plan_qty(plan: dict) -> float:
    return round(sum(float(d["take"]) for d in plan.get("deductions", [])), 6)


def expire_lots(lots: list[dict], today: str) -> list[int]:
    """Ids that should leave shelf: remaining>0 and expiry < today.

    与 alerting.classify 的 expired 边界一致（严格小于今天，当天到期仍算在架/soon）。
    """
    out = []
    for l in lots:
        exp = l.get("expiry")
        if exp and exp < today and float(l.get("qty_remain", 0)) > 0:
            out.append(l["id"])
    return out
