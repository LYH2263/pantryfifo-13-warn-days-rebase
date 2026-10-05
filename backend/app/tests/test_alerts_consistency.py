"""顶条/全层/下架履历/预演提交四方一致性 + warn_days 设置规则。"""
import os
import tempfile
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    tmp = tempfile.mkdtemp()
    os.environ["DATA_DIR"] = tmp
    # seed 在建库时写死了日期，先建库再清成相对今天的受控数据
    from app.db import connect
    from app import seed
    seed.init_db()
    c = connect()
    c.execute("DELETE FROM lots")
    c.execute("DELETE FROM shelf_events")
    c.execute("DELETE FROM sqlite_sequence WHERE name IN ('lots','shelf_events')")
    c.execute("UPDATE settings SET value='3' WHERE key='warn_days'")
    today = date.today()
    c.executemany(
        "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
        [
            # 1: 昨天到期（expired，在架未 sweep）  2: 前天到期 expired
            (1, 2, 2, (today - timedelta(days=1)).isoformat(), "on_shelf", "clean"),
            (1, 1, 1, (today - timedelta(days=2)).isoformat(), "on_shelf", "clean"),
            # 3: warn=3 时 soon（剩2天），warn=1 时 fresh
            (1, 4, 4, (today + timedelta(days=2)).isoformat(), "on_shelf", "clean"),
            # 4: 远期 fresh
            (1, 5, 5, (today + timedelta(days=30)).isoformat(), "on_shelf", "clean"),
        ],
    )
    c.commit(); c.close()
    from app.main import app
    with TestClient(app) as cl:
        yield cl


# ---------- 设置校验 ----------

@pytest.mark.parametrize("bad", [0, -1, 3.5, "3.5", "abc", "", True, False, None])
def test_invalid_warn_days_rejected_and_unchanged(client, bad):
    before = client.get("/api/settings").json()["warn_days"]
    r = client.put("/api/settings/warn-days", json={"warn_days": bad})
    assert r.status_code == 400
    after = client.get("/api/settings").json()["warn_days"]
    assert after == before == "3"

def test_valid_warn_days_persists(client):
    assert client.put("/api/settings/warn-days", json={"warn_days": 10}).json() == {"warn_days": 10}
    assert client.get("/api/settings").json()["warn_days"] == "10"
    assert client.put("/api/settings/warn-days", json={"warn_days": "7"}).json() == {"warn_days": 7}

# ---------- 改天数：顶条现算，全层批次不变，到期日不变 ----------

def test_changing_warn_days_recomputes_bar_but_not_lots(client):
    before_lots = client.get("/api/fridge").json()
    before_db = {(l["id"]): (l["qty_remain"], l["status"], l["expiry"]) for l in before_lots}

    bar3 = {(a["id"], a["level"]) for a in client.get("/api/alerts").json()}
    # lot3（剩2天）在 warn=3 时 soon
    assert (3, "soon") in bar3
    assert {(lid, lv) for lid, lv in bar3 if lv == "expired"} == {(1, "expired"), (2, "expired")}

    assert client.put("/api/settings/warn-days", json={"warn_days": 1}).status_code == 200
    bar1 = client.get("/api/alerts").json()
    # 只剩2天 > 1：lot3 立即下顶条；expired 仍在
    assert 3 not in {a["id"] for a in bar1}
    assert {a["id"] for a in bar1 if a["level"] == "expired"} == {1, 2}

    after_lots = client.get("/api/fridge").json()
    after_db = {(l["id"]): (l["qty_remain"], l["status"], l["expiry"]) for l in after_lots}
    # 全层批次 status、余量、到期日字符串都不因改设置而动
    assert before_db == after_db

def test_topbar_subset_of_fridge(client):
    """顶条报紧急的每一批，全层必须同时可见。"""
    for w in (1, 3, 100):
        client.put("/api/settings/warn-days", json={"warn_days": w})
        fridge_ids = {l["id"] for l in client.get("/api/fridge").json()}
        alert_ids = {a["id"] for a in client.get("/api/alerts").json()}
        assert alert_ids <= fridge_ids

# ---------- sweep 下架后改天数不复活：履历/顶条/全层同进同退 ----------

def test_swept_lots_never_revived_by_warn_days(client):
    r = client.post("/api/expire-sweep")
    assert set(r.json()["expired_ids"]) == {1, 2}
    events = client.get("/api/shelf-events").json()
    assert {e["lot_id"] for e in events if e["event"] == "expired"} == {1, 2}

    for w in (1, 3, 1000):
        client.put("/api/settings/warn-days", json={"warn_days": w})
        fridge_ids = {l["id"] for l in client.get("/api/fridge").json()}
        alert_ids = {a["id"] for a in client.get("/api/alerts").json()}
        assert 1 not in fridge_ids and 2 not in fridge_ids
        assert 1 not in alert_ids and 2 not in alert_ids
        assert alert_ids <= fridge_ids
        # 下架履历仍在
        assert {e["lot_id"] for e in client.get("/api/shelf-events").json()} >= {1, 2}

# ---------- 预演 → 提交：设置漂移整单拒绝，不留半单 ----------

def test_preview_commit_happy_path(client):
    pv = client.post("/api/consume/preview", json={"item_id": 1, "qty": 3}).json()
    assert pv["ok"] and pv["warn_days"] == 3
    # FEFO：先吃最早到期批 lot2(前天,1) 再 lot1(昨天,2)
    assert [d["lot_id"] for d in pv["deductions"]] == [2, 1]
    assert {d["lot_id"]: d["level"] for d in pv["deductions"]} == {1: "expired", 2: "expired"}

    commit = {"item_id": 1, "qty": 3, "warn_days": pv["warn_days"], "snapshot": pv["snapshot"]["lots"]}
    r = client.post("/api/consume", json=commit)
    assert r.status_code == 200 and r.json()["ok"]
    # lot1/lot2 吃完转 consumed，不留在架
    fridge = {l["id"]: l["status"] for l in client.get("/api/fridge").json()}
    assert 1 not in fridge and 2 not in fridge

def test_commit_rejected_after_settings_change_no_partial(client):
    pv = client.post("/api/consume/preview", json={"item_id": 1, "qty": 2}).json()
    client.put("/api/settings/warn-days", json={"warn_days": 10})

    commit = {"item_id": 1, "qty": 2, "warn_days": pv["warn_days"], "snapshot": pv["snapshot"]["lots"]}
    r = client.post("/api/consume", json=commit)
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "settings_changed"

    # 整单未落库：余量、状态都和预演前一致
    rows = {l["id"]: l["qty_remain"] for l in client.get("/api/fridge").json()}
    assert rows[1] == 2 and rows[2] == 1

def test_commit_rejected_after_sweep_no_partial(client):
    pv = client.post("/api/consume/preview", json={"item_id": 1, "qty": 2}).json()
    client.post("/api/expire-sweep")  # lot1/lot2 下架
    commit = {"item_id": 1, "qty": 2, "warn_days": pv["warn_days"], "snapshot": pv["snapshot"]["lots"]}
    r = client.post("/api/consume", json=commit)
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "lots_changed"
    # 没有任何扣减
    assert {l["id"]: l["qty_remain"] for l in client.get("/api/fridge").json()} == {3: 4, 4: 5}

def test_commit_without_snapshot_rejected(client):
    r = client.post("/api/consume", json={"item_id": 1, "qty": 1})
    assert r.status_code == 409

def test_preview_levels_follow_current_warn_days(client):
    client.put("/api/settings/warn-days", json={"warn_days": 1})
    pv = client.post("/api/consume/preview", json={"item_id": 1, "qty": 8}).json()
    levels = {d["lot_id"]: d["level"] for d in pv["deductions"]}
    assert levels[3] == "fresh" and levels[1] == "expired"
