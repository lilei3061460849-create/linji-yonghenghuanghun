"""重构1（2026-11-01）：遗物脱离死者之页，改为「已启用件数 ≤ ceil(血限/30)」。

三组契约：
1. 容量脱钩——遗物不占页，交易/豪夺/世界事件获得遗物永不触发页数回滚；
2. 启用槽——常规获得/战前配装/单件切换严格受限，满槽时新遗物停用入库；
3. 战斗突破（方案A）——赦免/交易/豪夺在战斗内可直接启用，战终压回上限。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.death_pages import bind_resources
from engine.models import DaoWen, DaoWenInstance, Entity, Relic
from tests.setup_support import finish_initial_daowen

import tempfile


def _engine():
    e = GameEngine(db_path=os.path.join(tempfile.mkdtemp(prefix="rel"), "g.db"),
                   save_dir=tempfile.mkdtemp(prefix="rel2"), rng_seed=11, autosave=False)
    e.execute_action("setup_attributes", {"name": "持物者", "blood_points": 11,
                                          "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "龙心谷"})
    if setup.get("result", {}).get("relic_choices"):
        e.execute_action("choose_discovered_relic",
                         {"relic_name": setup["result"]["relic_choices"][0]})
    return e


def _grant_daowen(entity, name):
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))


# ---------------- 1. 容量脱钩 ----------------

def test_relics_do_not_consume_death_pages():
    e = _engine()
    p = e.state.player
    e.state.relics = []
    bind_resources(e.state)
    before = p.pages_used
    e.state.relics = [Relic(f"遗物{i}", "") for i in range(12)]
    bind_resources(e.state)
    assert p.pages_used == before, "12 件遗物不得占用任何死者之页"


def test_world_pickup_of_relic_ignores_full_pages():
    """满十页（道纹+法器）时地图上的遗物照样能被拾取。"""
    e = _engine()
    p = e.state.player
    p.dao_wen.clear()
    for i in range(1):
        _grant_daowen(p, f"纹{i}")
    e.state.artifacts_owned = [f"法器{i}" for i in range(9)]
    bind_resources(e.state)
    assert p.pages_used == p.load_capacity == 10

    e.state.relics = []
    relic = Relic("地图遗物", "")
    e.state.relics.append(relic)          # 拾取入口的实际动作
    e.state.note_relic_gained(relic)
    assert any(r.name == "地图遗物" for r in e.state.relics)
    assert p.pages_used == 10, "遗物入库不涨页"


# ---------------- 2. 启用槽 ----------------

def test_enabled_limit_is_ceil_blood_over_30():
    p = Entity("T", "轮回者", blood_limit=1)
    assert p.enabled_relic_limit == 1              # 1~30 → 1
    p.blood_limit = 29
    assert p.enabled_relic_limit == 1
    p.blood_limit = 30
    assert p.enabled_relic_limit == 1
    p.blood_limit = 31
    assert p.enabled_relic_limit == 2              # 31~60 → 2
    p.blood_limit = 60
    assert p.enabled_relic_limit == 2
    p.blood_limit = 61
    assert p.enabled_relic_limit == 3
    p.blood_limit = 0
    assert p.enabled_relic_limit == 0
    # 旧属性 relic_capacity 现在指向同一条槽位口径
    p.blood_limit = 90
    assert p.relic_capacity == p.enabled_relic_limit == 3


def test_regular_gain_beyond_quota_is_stored_disabled():
    e = _engine()
    p = e.state.player
    p.blood_limit = 30                     # 1 个启用槽
    e.state.relics = []
    e.state.phase = "pre_battle"

    first = Relic("甲", "")
    e.state.relics.append(first)
    e.state.note_relic_gained(first)
    second = Relic("乙", "")
    e.state.relics.append(second)
    e.state.note_relic_gained(second)

    assert first.enabled is True
    assert second.enabled is False, "启用槽满 → 停用入库"
    assert len(e.state.relics) == 2, "持有不限量，不得丢弃"


def test_set_active_relics_and_toggle_enforce_quota():
    e = _engine()
    p = e.state.player
    p.blood_limit = 60                     # 2 个启用槽
    e.state.relics = [Relic(n, "") for n in ("甲", "乙", "丙")]

    ok, err = e.state.set_active_relics(["甲", "乙"])
    assert ok, err
    ok, err = e.state.set_active_relics(["甲", "乙", "丙"])
    assert ok is False and "不得超过2件" in err, err

    e.state.set_active_relics(["甲"])
    ok, err = e.state.toggle_relic("丙")
    assert ok is True, err
    # 槽位已满（甲+丙）→ 第三件必须被拒，不会悄悄突破战前配装上限
    ok, err = e.state.toggle_relic("乙")
    assert ok is False and "达到上限2件" in err, err
    assert e.state.enabled_relic_names() == {"甲", "丙"}


def test_toggle_refuses_to_exceed_quota():
    e = _engine()
    p = e.state.player
    p.blood_limit = 30
    e.state.relics = [Relic("甲", ""), Relic("乙", "")]
    e.state.set_active_relics(["甲"])

    ok, err = e.state.toggle_relic("乙")
    assert ok is False and "达到上限1件" in err, err
    assert e.state.enabled_relic_names() == {"甲"}


# ---------------- 3. 战斗中临时突破 + 战终压回 ----------------

def test_in_combat_gain_bypasses_quota():
    e = _engine()
    p = e.state.player
    p.blood_limit = 30                     # 1 槽
    e.state.relics = [Relic("甲", "")]
    e.state.note_relic_gained(e.state.relics[0])
    e.state.phase = "in_combat"

    traded = Relic("交易来的", "", tags=["本场"])
    e.state.relics.append(traded)
    e.state.note_relic_gained(traded)

    assert traded.enabled is True, "战斗中的临时遗物直接生效（方案A）"
    assert len(e.state.enabled_relic_names()) == 2


def test_battle_end_trim_keeps_first_n_in_hold_order():
    e = _engine()
    p = e.state.player
    p.blood_limit = 60                     # 2 槽
    e.state.relics = [Relic("甲", ""), Relic("乙", ""), Relic("丙", ""), Relic("丁", "")]
    for relic in e.state.relics:            # 模拟战斗内全部启用（临时突破）
        relic.enabled = True
    assert len(e.state.enabled_relic_names()) == 4

    disabled = e.state.enforce_enabled_relic_limit()

    assert disabled == ["丙", "丁"], "按持有顺序保留前 N 件"
    assert e.state.enabled_relic_names() == {"甲", "乙"}
    assert len(e.state.relics) == 4, "超出的只是停用，不丢弃"


def test_battle_end_reports_trimmed_relics():
    """战终结算里必须真的调用压回（结果字段可核对）。"""
    import inspect
    from engine.api import GameEngine as _GE
    src = inspect.getsource(_GE._action_battle_end)
    assert "enforce_enabled_relic_limit" in src
    assert "relics_disabled_over_limit" in src


def test_haoduo_steal_does_not_touch_pages():
    """豪夺与到期归还都不再经页数体系：pages_used 全程不变。"""
    e = _engine()
    p = e.state.player
    bind_resources(e.state)
    before = p.pages_used
    e.state.relics = []
    e.state.note_relic_gained(Relic("被夺来的", "")) if False else None
    steal_target = Entity("靶怪", "怪物", blood_limit=60, current_hp=60)
    steal_target.relics = [Relic("战利品", "")]
    e.state.enemies = [steal_target]
    stolen = e.combat._steal_one_relic(p, steal_target, 1, "战利品")
    assert stolen == "战利品" and any(r.name == "战利品" for r in e.state.relics)
    assert p.pages_used == before
    e.combat._tick_stolen_relics(p)
    assert any(r.name == "战利品" for r in steal_target.relics), "到期归还"
    assert p.pages_used == before
