"""不变量测试：warn_days 迭代下，预演资格 / 顶条现算 / 全层真源 / 下架履历必须一致。

直接调用路由函数与引擎（不起 HTTP 服务），每个用例使用独立临时库。
"""

import os
from datetime import date, timedelta

import pytest
from fastapi import HTTPException

from app import seed
from app import main
from app.db import connect
from app.engines.alerting import classify, build_alerts, get_warn_days
from app.engines.fefo import build_plan, verify_plan, expire_lots


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seed.init_db()
    c = connect()
    today = date.today()
    # 清掉种子批次，只用受控数据；settings.warn_days 保留种子值 '3'
    c.execute("DELETE FROM lots")
    c.execute("INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
              (1, 2, 2, (today - timedelta(days=1)).isoformat(), "on_shelf", "clean"))  # A 已过期
    c.execute("INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
              (1, 2, 2, (today + timedelta(days=2)).isoformat(), "on_shelf", "clean"))  # B 2 天后
    c.execute("INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
              (1, 2, 2, (today + timedelta(days=10)).isoformat(), "on_shelf", "clean"))  # C 10 天后
    c.execute("INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
              (1, 1, 1, None, "on_shelf", "clean"))  # D 无到期日
    c.commit()
    lot_ids = [r["id"] for r in c.execute("SELECT id FROM lots WHERE item_id=1 ORDER BY id")]
    c.close()
    return {"A": lot_ids[0], "B": lot_ids[1], "C": lot_ids[2], "D": lot_ids[3]}


def _lot_snapshot(c):
    return {r["id"]: (r["status"], r["qty_remain"], r["expiry"])
            for r in c.execute("SELECT id,status,qty_remain,expiry FROM lots ORDER BY id")}


def _alert_ids():
    return {a["id"]: a["level"] for a in main.alerts()}


def _fridge_ids():
    return {r["id"] for r in main.fridge()}


# ---------- 分类器单一规则 ----------

def test_classify_boundaries():
    today = "2026-10-05"
    assert classify("2026-10-04", today, 3) == ("expired", None)
    assert classify("2026-10-05", today, 3) == ("soon", 0)       # 当天到期仍算 soon，不算 expired
    assert classify("2026-10-08", today, 3) == ("soon", 3)
    assert classify("2026-10-09", today, 3) == (None, 4)
    assert classify(None, today, 3) == (None, None)
    # expired 与 warn_days 无关：调多大都不回在架/不消失
    assert classify("2026-10-04", today, 100000) == ("expired", None)


def test_expire_lots_matches_classify():
    today = "2026-10-05"
    lots = [
        {"id": 1, "qty_remain": 1, "expiry": "2026-10-04", "status": "on_shelf"},
        {"id": 2, "qty_remain": 1, "expiry": "2026-10-05", "status": "on_shelf"},
        {"id": 3, "qty_remain": 0, "expiry": "2026-10-01", "status": "on_shelf"},
    ]
    assert expire_lots(lots, today) == [1]


# ---------- warn_days 校验：设置与顶条都回到改前 ----------

@pytest.mark.parametrize("bad", [0, -1, 3.5, "3", True, False, None])
def test_invalid_warn_days_rejected(db, bad):
    before_alerts = _alert_ids()
    c = connect(); before = get_warn_days(c); c.close()
    with pytest.raises(HTTPException) as ei:
        main.settings_update({"warn_days": bad})
    assert ei.value.status_code == 400
    c = connect(); assert get_warn_days(c) == before; c.close()
    assert _alert_ids() == before_alerts


def test_missing_warn_days_rejected(db):
    with pytest.raises(HTTPException) as ei:
        main.settings_update({})
    assert ei.value.status_code == 400


# ---------- 改天数：顶条按新值现算，全层批次事实不变 ----------

def test_change_warn_days_only_moves_soon_band(db):
    # 默认 warn_days=3：A expired、B soon、C 不预警
    assert _alert_ids() == {db["A"]: "expired", db["B"]: "soon"}

    c = connect(); before = _lot_snapshot(c); c.close()
    main.settings_update({"warn_days": 20})

    c = connect(); after = _lot_snapshot(c); c.close()
    # 全层批次 status / 余量 / 到期日字符串逐项不变
    assert before == after
    # 顶条按新天数现算：C 进入 soon；A 仍是 expired（与天数无关）
    assert _alert_ids() == {db["A"]: "expired", db["B"]: "soon", db["C"]: "soon"}

    main.settings_update({"warn_days": 1})
    assert _alert_ids() == {db["A"]: "expired"}  # 收窄后 B(2天)、C 都离开顶条
    c = connect(); assert _lot_snapshot(c) == before; c.close()


def test_alerts_always_subset_of_fridge(db):
    # 顶条报紧急的批必须同时在全层在架 —— 任意 warn_days 下都成立
    for w in (1, 2, 10, 100):
        main.settings_update({"warn_days": w})
        assert set(_alert_ids()) <= _fridge_ids()


# ---------- 过期下架：履历 / 顶条 / 全层同事务一致，改天数不可逆 ----------

def test_expire_sweep_atomic_and_irreversible_by_warn_days(db):
    main.settings_update({"warn_days": 3})
    sweep = main.expire_sweep()
    assert sweep["expired_ids"] == [db["A"]]

    c = connect()
    row = c.execute("SELECT * FROM expiry_ledger WHERE lot_id=?", (db["A"],)).fetchone()
    assert row is not None and row["reason"] == "expired"
    assert c.execute("SELECT status FROM lots WHERE id=?", (db["A"],)).fetchone()["status"] == "expired"
    assert c.execute("SELECT COUNT(*) n FROM expiry_ledger").fetchone()["n"] == 1
    c.close()

    # 顶条与全层同时不再含该批
    assert db["A"] not in _alert_ids()
    assert db["A"] not in _fridge_ids()

    # 无论把 warn_days 调到多大，已下架批都不回在架、不进顶条、履历不重复
    main.settings_update({"warn_days": 100000})
    assert db["A"] not in _fridge_ids()
    assert db["A"] not in _alert_ids()
    c = connect()
    assert c.execute("SELECT status FROM lots WHERE id=?", (db["A"],)).fetchone()["status"] == "expired"
    assert c.execute("SELECT COUNT(*) n FROM expiry_ledger").fetchone()["n"] == 1
    c.close()

    # 再扫一次幂等：不新增履历
    main.expire_sweep()
    c = connect()
    assert c.execute("SELECT COUNT(*) n FROM expiry_ledger").fetchone()["n"] == 1
    c.close()


# ---------- 消费预演：预演资格 / 顶条现算 / 全层真源不许各过一套 ----------

def test_preview_levels_share_single_truth(db):
    c = connect(); warn = get_warn_days(c)
    today = date.today().isoformat()
    lots = [dict(r) for r in c.execute(
        "SELECT * FROM lots WHERE item_id=1 AND status='on_shelf' AND qty_remain>0")]
    c.close()
    plan = build_plan(lots, 3, warn, today)
    by_id = {d["lot_id"]: d["level"] for d in plan["deductions"]}
    # FEFO 顺序：A(过期)→B(2天,soon)→部分 C
    assert by_id[db["A"]] == "expired"
    assert by_id[db["B"]] == "soon"
    assert plan["deductions"][0]["lot_id"] == db["A"]
    assert plan["plan_hash"]


def test_commit_rejected_after_warn_days_change_with_zero_deductions(db):
    plan = main.consume_preview(main.ConsumeIn(item_id=1, qty=2))
    assert plan["ok"]

    c = connect(); before = _lot_snapshot(c); c.close()
    # 预演按旧 warn_days=3 挑批；提交前设置被改成 20
    main.settings_update({"warn_days": 20})

    with pytest.raises(HTTPException) as ei:
        main.consume_commit(main.ConsumeCommitIn(item_id=1, plan=plan))
    assert ei.value.status_code == 409
    detail = ei.value.detail
    assert isinstance(detail, dict) and detail["reason"] == "stale_preview"

    # 整单拒绝：零扣减、无消费记录
    c = connect()
    assert _lot_snapshot(c) == before
    assert c.execute("SELECT COUNT(*) n FROM consumptions").fetchone()["n"] == 0
    c.close()

    # 用新口径重新预演后提交成功
    plan2 = main.consume_preview(main.ConsumeIn(item_id=1, qty=2))
    assert plan2["warn_days"] == 20
    r = main.consume_commit(main.ConsumeCommitIn(item_id=1, plan=plan2))
    assert r["ok"]
    c = connect()
    # 最早批 A 被扣 2 后耗尽 → consumed
    assert c.execute("SELECT status,qty_remain FROM lots WHERE id=?", (db["A"],)).fetchone()["status"] == "consumed"
    assert c.execute("SELECT COUNT(*) n FROM consumptions").fetchone()["n"] == 1
    c.close()
    # 顶条随之现算且仍是全层子集
    assert db["A"] not in _alert_ids()
    assert set(_alert_ids()) <= _fridge_ids()


def test_commit_rejected_after_sweep_between_preview_and_commit(db):
    plan = main.consume_preview(main.ConsumeIn(item_id=1, qty=3))
    c = connect(); before_qty = {r["id"]: r["qty_remain"]
                                 for r in c.execute("SELECT id,qty_remain FROM lots")}; c.close()
    # 预演后、确认前：过期批次被下架（A 状态 on_shelf→expired）
    main.expire_sweep()
    with pytest.raises(HTTPException) as ei:
        main.consume_commit(main.ConsumeCommitIn(item_id=1, plan=plan))
    assert ei.value.status_code == 409
    c = connect()
    # 消费整单未发生：所有余量不变、无消费记录；A 只承受了下架这一笔事实
    assert {r["id"]: r["qty_remain"] for r in c.execute("SELECT id,qty_remain FROM lots")} == before_qty
    assert c.execute("SELECT status FROM lots WHERE id=?", (db["A"],)).fetchone()["status"] == "expired"
    assert c.execute("SELECT COUNT(*) n FROM consumptions").fetchone()["n"] == 0
    c.close()


def test_tampered_plan_rejected(db):
    plan = main.consume_preview(main.ConsumeIn(item_id=1, qty=1))
    plan["deductions"][0]["take"] = 999  # 篡改扣减量
    with pytest.raises(HTTPException) as ei:
        main.consume_commit(main.ConsumeCommitIn(item_id=1, plan=plan))
    assert ei.value.status_code == 409
    c = connect()
    assert c.execute("SELECT qty_remain FROM lots WHERE id=?", (db["A"],)).fetchone()["qty_remain"] == 2
    c.close()
