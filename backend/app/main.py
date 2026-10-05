import json
from datetime import date, datetime, timezone
from fastapi import FastAPI, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import connect
from app.engines.alerting import build_alerts, get_warn_days
from app.engines.fefo import build_plan, verify_plan, plan_qty, expire_lots

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

@app.get("/api/alerts")
def alerts():
    # 顶条永远按当前 warn_days 与今天现算，结果只含在架正余量批次
    c = connect(); out = build_alerts(c); c.close(); return out

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

def _on_shelf_lots(c, item_id: int) -> list[dict]:
    return [dict(r) for r in c.execute(
        "SELECT * FROM lots WHERE item_id=? AND status='on_shelf' AND qty_remain>0", (item_id,))]

@app.post("/api/consume/preview")
def consume_preview(body: ConsumeIn):
    """预演：按当前 warn_days/今天给出 FEFO 明细，level 与顶条同一套分类。"""
    c = connect()
    warn_days = get_warn_days(c)
    today = date.today().isoformat()
    plan = build_plan(_on_shelf_lots(c, body.item_id), body.qty, warn_days, today)
    c.close()
    if not plan["ok"] and plan["reason"] == "qty_non_positive":
        raise HTTPException(400, plan["reason"])
    if not plan["ok"]:
        raise HTTPException(409, plan)
    plan["item_id"] = body.item_id
    plan["note"] = body.note
    return plan

class ConsumeCommitIn(BaseModel):
    item_id: int
    note: str = ""
    plan: dict

@app.post("/api/consume/commit")
def consume_commit(body: ConsumeCommitIn):
    """确认提交：事务内用数据库当前事实重算校验预演，不一致整单 409 且零扣减。"""
    c = connect()
    try:
        warn_days = get_warn_days(c)
        today = date.today().isoformat()
        lots = _on_shelf_lots(c, body.item_id)
        ok, reason = verify_plan(body.plan, lots, warn_days, today)
        if not ok:
            # 预演后设置/日期/批次任一变化（如 warn_days 已改）：预演资格、顶条、全层
            # 不能各过一套 —— 整单拒绝，不写入任何扣减。
            raise HTTPException(409, {"ok": False, "reason": reason})
        # 校验通过后才在同一事务内落库
        for d in body.plan["deductions"]:
            c.execute("UPDATE lots SET qty_remain = qty_remain - ? WHERE id=? AND status='on_shelf'",
                      (d["take"], d["lot_id"]))
            rem = c.execute("SELECT qty_remain FROM lots WHERE id=?", (d["lot_id"],)).fetchone()["qty_remain"]
            if rem <= 0:
                c.execute("UPDATE lots SET status='consumed', qty_remain=0 WHERE id=?", (d["lot_id"],))
        c.execute("INSERT INTO consumptions(note,result_json,created_at) VALUES (?,?,?)",
                  (body.note, json.dumps(body.plan), datetime.now(timezone.utc).isoformat()))
        c.commit()
    finally:
        c.close()
    return {"ok": True, "qty": plan_qty(body.plan), "deductions": body.plan["deductions"]}

@app.post("/api/consume")
def consume(body: ConsumeIn):
    """即时扣减（内部同样走预演→即时校验→提交，不留陈旧窗口）。"""
    c = connect()
    try:
        warn_days = get_warn_days(c)
        today = date.today().isoformat()
        lots = _on_shelf_lots(c, body.item_id)
        plan = build_plan(lots, body.qty, warn_days, today)
        if not plan["ok"] and plan["reason"] == "qty_non_positive":
            raise HTTPException(400, plan["reason"])
        if not plan["ok"]:
            raise HTTPException(409, plan)
        for d in plan["deductions"]:
            c.execute("UPDATE lots SET qty_remain = qty_remain - ? WHERE id=?", (d["take"], d["lot_id"]))
            rem = c.execute("SELECT qty_remain FROM lots WHERE id=?", (d["lot_id"],)).fetchone()["qty_remain"]
            if rem <= 0:
                c.execute("UPDATE lots SET status='consumed', qty_remain=0 WHERE id=?", (d["lot_id"],))
        c.execute("INSERT INTO consumptions(note,result_json,created_at) VALUES (?,?,?)",
                  (body.note, json.dumps(plan), datetime.now(timezone.utc).isoformat()))
        c.commit()
    finally:
        c.close()
    return plan

@app.post("/api/expire-sweep")
def expire_sweep():
    """过期下架：状态翻转与下架履历同一事务，同时成立或同时回滚。

    只把 on_shelf 改为 expired（永不反向），因此调 warn_days 不可能让已下架批次回架。
    """
    c = connect()
    try:
        today = date.today().isoformat()
        lots = [dict(r) for r in c.execute("SELECT * FROM lots WHERE status='on_shelf'")]
        ids = expire_lots(lots, today)
        now = datetime.now(timezone.utc).isoformat()
        for lot in lots:
            if lot["id"] in ids:
                c.execute("UPDATE lots SET status='expired' WHERE id=? AND status='on_shelf'", (lot["id"],))
                c.execute(
                    """INSERT INTO expiry_ledger(lot_id,item_id,qty_remain,expiry,reason,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (lot["id"], lot["item_id"], lot["qty_remain"], lot["expiry"], "expired", now))
        c.commit()
    finally:
        c.close()
    # 顶条与全层都以同一 status 为准：下架成功后两边都不再含这些批
    return {"expired_ids": ids}

@app.get("/api/settings")
def settings_get():
    c = connect(); rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return rows

def _parse_positive_int(raw) -> int:
    """严格正整数：拒绝浮点（含 3.0）、布尔、字符串、0、负数。"""
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise HTTPException(400, {"ok": False, "reason": "warn_days_must_be_positive_int"})
    return raw

@app.post("/api/settings")
def settings_update(body: dict = Body(...)):
    """改 warn_days：非法值 400，库与顶条口径都保持改前；合法才落库。

    只写 settings 一行，绝不更新 lots：全层批次 status、余量、到期日字符串均不变。
    """
    if "warn_days" not in body:
        raise HTTPException(400, {"ok": False, "reason": "warn_days_required"})
    warn_days = _parse_positive_int(body["warn_days"])
    c = connect()
    try:
        c.execute("UPDATE settings SET value=? WHERE key='warn_days'", (str(warn_days),))
        c.commit()
        rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}
    finally:
        c.close()
    # /alerts 下一次请求即按新值现算 soon/expired
    return rows
