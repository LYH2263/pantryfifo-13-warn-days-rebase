import json
from datetime import date, datetime, timezone
from typing import Any
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import connect
from app.engines.fefo import consume_fefo, expire_lots
from app.engines.alerts import (
    EXPIRED, SOON, classify, get_warn_days, parse_warn_days,
)

app = FastAPI(title="Pantryfifo", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def _startup(): seed.init_db()

@app.get("/api/health")
def health(): return {"ok": True, "project": "pantryfifo"}

@app.get("/api/items")
def items():
    c = connect(); rows = [dict(r) for r in c.execute("SELECT * FROM items")]; c.close(); return rows

@app.get("/api/fridge")
def fridge(layer: str | None = None):
    c = connect()
    q = """SELECT lots.*, items.name, items.layer, items.unit FROM lots
           JOIN items ON items.id=lots.item_id WHERE lots.status='on_shelf'"""
    args = []
    if layer:
        q += " AND items.layer=?"; args.append(layer)
    rows = [dict(r) for r in c.execute(q, args)]; c.close(); return rows

def _on_shelf_lots(c, item_id: int | None = None, with_expiry: bool = False) -> list[dict]:
    """全层/顶条/预演共用的在架批次读取：status='on_shelf' 且余量>0。

    已 expired 下架（sweep）或 consumed 的批在此即不可见，改 warn_days 不会
    改变任何行的 status，因此不可能因改设置复活。with_expiry 供顶条使用。
    """
    q = """SELECT lots.*, items.name, items.layer FROM lots
           JOIN items ON items.id=lots.item_id
           WHERE lots.status='on_shelf' AND lots.qty_remain>0"""
    args: list = []
    if with_expiry:
        q += " AND lots.expiry IS NOT NULL"
    if item_id is not None:
        q += " AND lots.item_id=?"; args.append(item_id)
    return [dict(r) for r in c.execute(q, args)]

@app.get("/api/alerts")
def alerts():
    c = connect()
    warn = get_warn_days(c)
    today = date.today()
    rows = _on_shelf_lots(c)
    c.close()
    out = []
    for r in rows:
        level = classify(r["expiry"], today, warn)
        # 顶条只报 soon / expired；fresh 与无到期日不上顶条
        if level in (SOON, EXPIRED):
            r["level"] = level
            if level == SOON:
                r["days_left"] = (date.fromisoformat(r["expiry"]) - today).days
            out.append(r)
    return out

class LotIn(BaseModel):
    item_id: int
    qty: float
    expiry: str

@app.post("/api/lots")
def inbound(body: LotIn):
    c = connect()
    item = c.execute("SELECT id FROM items WHERE id=?", (body.item_id,)).fetchone()
    if not item: c.close(); raise HTTPException(404, "item")
    cur = c.execute(
        "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
        (body.item_id, body.qty, body.qty, body.expiry, "on_shelf", "clean"))
    c.commit(); lid = cur.lastrowid; c.close(); return {"id": lid}

class ConsumeIn(BaseModel):
    item_id: int
    qty: float
    note: str = ""

def _fefo_with_levels(lots: list[dict], qty: float, warn: int, today: date) -> dict:
    """FEFO 只看过期日与余量（与 warn_days 无关）；level 用当前 warn_days 现算，
    让预演能显式标出哪些批按当前设置属于 soon/expired。"""
    result = consume_fefo(lots, qty)
    levels = {l["id"]: classify(l.get("expiry"), today, warn) for l in lots}
    for d in result["deductions"]:
        d["level"] = levels.get(d["lot_id"])
    return result

@app.post("/api/consume/preview")
def consume_preview(body: ConsumeIn):
    """消费预演：返回扣减计划 + 提交时必须回传的快照。

    预演与提交之间若 warn_days、批次 status/余量/到期日发生变化，提交整单拒绝，
    杜绝「预演按旧天数、顶条按新天数、全层又是另一套」。"""
    c = connect()
    item = c.execute("SELECT id FROM items WHERE id=?", (body.item_id,)).fetchone()
    if not item: c.close(); raise HTTPException(404, "item")
    warn = get_warn_days(c)
    lots = _on_shelf_lots(c, body.item_id)
    c.close()
    today = date.today()
    result = _fefo_with_levels(lots, body.qty, warn, today)
    result["warn_days"] = warn
    result["snapshot"] = {
        "item_id": body.item_id,
        "qty": body.qty,
        "warn_days": warn,
        "lots": [
            {"lot_id": l["id"], "qty_remain": float(l["qty_remain"]), "expiry": l["expiry"]}
            for l in lots
        ],
    }
    return result

class SnapshotLot(BaseModel):
    lot_id: int
    qty_remain: float
    expiry: str | None

class ConsumeCommitIn(BaseModel):
    item_id: int
    qty: float
    note: str = ""
    warn_days: int | None = None
    snapshot: list[SnapshotLot] | None = None

def _snapshot_stale(c, body: ConsumeCommitIn, warn: int) -> str | None:
    """返回拒绝原因；None 表示预演后资格真源未漂移。"""
    if body.snapshot is None or body.warn_days is None:
        return "snapshot_missing"
    try:
        snap_warn = parse_warn_days(body.warn_days)
    except ValueError:
        return "snapshot_malformed"
    if snap_warn != warn:
        return "settings_changed"
    current = {l["id"]: (float(l["qty_remain"]), l["expiry"])
               for l in _on_shelf_lots(c, body.item_id)}
    snap = {s.lot_id: (float(s.qty_remain), s.expiry) for s in body.snapshot}
    # 批集合或任一批余量/到期日不一致（含被 sweep 下架、被消费、新批次入库）即漂移
    if set(current) != set(snap):
        return "lots_changed"
    for lid, vals in snap.items():
        cur_rem, cur_exp = current[lid]
        if abs(cur_rem - vals[0]) > 1e-9 or cur_exp != vals[1]:
            return "lots_changed"
    return None

@app.post("/api/consume")
def consume(body: ConsumeCommitIn):
    c = connect()
    item = c.execute("SELECT id FROM items WHERE id=?", (body.item_id,)).fetchone()
    if not item: c.close(); raise HTTPException(404, "item")
    warn = get_warn_days(c)
    stale = _snapshot_stale(c, body, warn)
    if stale == "snapshot_malformed":
        c.close(); raise HTTPException(400, stale)
    if stale:
        # 设置或批次已漂移：整单拒绝，不做任何扣减
        c.close()
        raise HTTPException(409, {"ok": False, "reason": stale})
    lots = _on_shelf_lots(c, body.item_id)
    result = _fefo_with_levels(lots, body.qty, warn, date.today())
    if not result["ok"] and result["reason"] == "qty_non_positive":
        c.close(); raise HTTPException(400, result["reason"])
    if not result["ok"]:
        c.close(); raise HTTPException(409, result)
    # 校验通过后一次性落库：任一步失败整体回滚，不留半单
    try:
        for d in result["deductions"]:
            c.execute("UPDATE lots SET qty_remain = qty_remain - ? WHERE id=?", (d["take"], d["lot_id"]))
            rem = c.execute("SELECT qty_remain FROM lots WHERE id=?", (d["lot_id"],)).fetchone()["qty_remain"]
            if rem <= 0:
                c.execute("UPDATE lots SET status='consumed', qty_remain=0 WHERE id=?", (d["lot_id"],))
        c.execute("INSERT INTO consumptions(note,result_json,created_at) VALUES (?,?,?)",
                  (body.note, json.dumps(result), datetime.now(timezone.utc).isoformat()))
        c.commit()
    except Exception:
        c.rollback(); c.close(); raise
    c.close(); return result

@app.post("/api/expire-sweep")
def expire_sweep():
    c = connect()
    lots = [dict(r) for r in c.execute("SELECT * FROM lots WHERE status='on_shelf'")]
    ids = expire_lots(lots, date.today().isoformat())
    now = datetime.now(timezone.utc).isoformat()
    # 状态翻转与下架履历同一事务：履历有账 ⇔ 该批已不在架，不可能出现
    # 「履历记下架、顶条却还报紧急」或反之的中间态。
    for i in ids:
        c.execute("UPDATE lots SET status='expired' WHERE id=?", (i,))
        c.execute("INSERT INTO shelf_events(lot_id,event,created_at) VALUES (?,?,?)",
                  (i, "expired", now))
    c.commit(); c.close(); return {"expired_ids": ids}

@app.get("/api/shelf-events")
def shelf_events():
    c = connect()
    rows = [dict(r) for r in c.execute("SELECT * FROM shelf_events ORDER BY id")]
    c.close(); return rows

@app.get("/api/settings")
def settings():
    c = connect(); rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return rows

class WarnDaysIn(BaseModel):
    # 用 Any 自行严格校验：拒绝 3.5 / true / "abc" / 0 / 负数
    warn_days: Any

@app.put("/api/settings/warn-days")
def update_warn_days(body: WarnDaysIn):
    """改预警天数。

    非法（非正整数）→ 400，settings 与顶条一律维持改前值；
    合法 → 仅改 settings；不触碰任何 lot 的 status/qty_remain/expiry，
    顶条下次读取即按新天数现算 soon/expired。"""
    try:
        v = parse_warn_days(body.warn_days)
    except ValueError:
        raise HTTPException(400, "warn_days must be a positive integer")
    c = connect()
    c.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        ("warn_days", str(v)))
    c.commit(); c.close()
    return {"warn_days": v}
