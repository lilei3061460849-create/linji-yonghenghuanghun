"""法术生命周期（Part 6）：instant / battle / permanent 是**行为**，不是标签。

规则（2026-10-02 定稿）：
  instant   —— cast(flow=...) 一次执行完即弃，不建立任何绑定；
  battle    —— 战斗中自创法术的默认作用域：本场战斗有效，[战终]由引擎清除，
               不会以"跨战斗绑定"的形式活过战终（含存档/读档往返）；
  permanent —— 显式声明的永久法术：正常存档语义，跨战斗保留。

持续法术的事实源只有 entity.spells；本文件同时锁定旧存档兼容：旧存档里的
Spell 没有 lifecycle 实例属性时按 permanent 处理（类属性默认值）。
"""
import os
import sys

from tests.setup_support import begin_battle, begin_round, finish_initial_daowen

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance, Entity, Spell
from engine.spell_execution import Lifecycle


def _engine(suffix: str) -> GameEngine:
    e = GameEngine(db_path=f"/tmp/test_spell_lifecycle_{suffix}.db", rng_seed=1,
                   sealed_candidate_path=f"/tmp/test_spell_lifecycle_{suffix}.json")
    e.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = e.execute_action("setup_choose_region", {"region": "龙心谷"})
    e.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    return e


def _start_battle(e: GameEngine) -> None:
    e.state.enemies.clear()
    e.state.monster_reinforcements.clear()
    e.state.enemies.append(Entity("靶怪", "怪物", blood_limit=999, current_hp=999,
                                  attack_count=1, attack_power=1))
    started = begin_battle(e)
    assert started["success"], started
    assert begin_round(e)["success"]
    # 夹具只需要这一场「进行中的战斗」：清掉待进场增援，否则战终被判阻塞。
    e.state.monster_reinforcements.clear()


def _give_daowen(e: GameEngine, *names: str) -> None:
    for name in names:
        e.state.player.dao_wen[name] = DaoWenInstance(
            DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))


def _define(e: GameEngine, name: str, **extra):
    definition = {"name": name, "required_daowen": ["再生"],
                  "trigger_condition": "失去生命后", "effect_flow": "发动再生X于自身"}
    definition.update(extra)
    e.state.player.actions_used_this_round = 0
    return e.execute_action("define_spell", {"spell": definition})


def _end_battle(e: GameEngine):
    for m in list(e.state.enemies):
        m.is_alive = False
    return e.execute_action("battle_end", {})


def test_define_spell_in_combat_is_battle_scoped_by_default():
    e = _engine("default_battle")
    _start_battle(e)
    _give_daowen(e, "再生")
    result = _define(e, "急救")
    assert result["success"], result
    spell = next(s for s in e.state.player.spells if s.name == "急救")
    assert spell.lifecycle == Lifecycle.BATTLE.value
    bindings = e.combat.spell_bindings(e.state.player)
    assert any(b["name"] == "急救" and b["lifecycle"] == "battle" for b in bindings)


def test_explicit_permanent_and_invalid_lifecycle():
    e = _engine("explicit")
    _start_battle(e)
    _give_daowen(e, "再生")
    ok = _define(e, "常驻", lifecycle="permanent")
    assert ok["success"], ok
    assert next(s for s in e.state.player.spells if s.name == "常驻").lifecycle == "permanent"

    bad = _define(e, "非法", lifecycle="instant")
    assert not bad["success"]
    assert "lifecycle" in bad["error"]
    assert all(s.name != "非法" for s in e.state.player.spells)


def test_battle_end_removes_battle_spells_and_keeps_permanent():
    e = _engine("battle_end")
    _start_battle(e)
    _give_daowen(e, "再生")
    assert _define(e, "急救").get("success")
    assert _define(e, "常驻", lifecycle="permanent").get("success")

    ended = _end_battle(e)
    assert ended["success"], ended
    removed = ended["result"]["spell_bindings_removed"]
    assert {"holder": e.state.player.name, "spell": "急救", "lifecycle": "battle"} in removed
    names = [s.name for s in e.state.player.spells]
    assert "急救" not in names and "常驻" in names


def test_undefine_spell_removes_custom_definition_without_action_cost():
    e = _engine("undefine")
    _start_battle(e)
    _give_daowen(e, "再生")
    assert _define(e, "急救").get("success")
    e.state.player.actions_used_this_round = 0
    result = e.execute_action("undefine_spell", {"spell_name": "急救"})
    assert result["success"], result
    assert result["result"]["removed"]["custom"] is True
    assert not e.state.player.spells
    assert e.state.player.actions_used_this_round == 0


def test_battle_spell_is_battle_local_and_never_survives_battle_end_via_save(tmp_path):
    e = _engine("save_roundtrip")
    _start_battle(e)
    _give_daowen(e, "再生")
    assert _define(e, "急救").get("success")

    saved = e.save_game("midbattle")
    assert saved["success"], saved
    loaded = e.load_game("midbattle")
    assert loaded["success"], loaded
    # 同一场战斗内的存档往返：battle 法术仍在（战斗还没结束）
    assert "急救" in [s.name for s in e.state.player.spells]

    ended = _end_battle(e)
    assert ended["success"], ended
    assert "急救" not in [s.name for s in e.state.player.spells]

    saved2 = e.save_game("afterbattle")
    assert saved2["success"], saved2
    assert e.load_game("afterbattle")["success"]
    assert "急救" not in [s.name for s in e.state.player.spells]
    assert e.state.phase == "pre_battle"


def test_legacy_spell_without_lifecycle_attribute_is_permanent():
    """old-save 兼容：旧 Spell 对象没有 lifecycle 实例属性 → 按 permanent 处理。"""
    spell = Spell(name="旧", required_daowen=["再生"], trigger_condition="失去生命后",
                  effect_flow="发动再生X于自身")
    assert spell.lifecycle == Lifecycle.PERMANENT.value
    spell.__dict__.pop("lifecycle", None)
    assert getattr(spell, "lifecycle", "permanent") == "permanent"

    e = _engine("legacy_attribute")
    _start_battle(e)
    _give_daowen(e, "再生")
    legacy = Spell(name="旧法术", required_daowen=["再生"], trigger_condition="失去生命后",
                   effect_flow="发动再生X于自身")
    legacy.__dict__.pop("lifecycle", None)
    e.state.player.spells.append(legacy)
    ended = _end_battle(e)
    assert ended["success"], ended
    assert any(s.name == "旧法术" for s in e.state.player.spells), \
        "旧存档法术没有 lifecycle 标记，必须按 permanent 保留而非被战终清掉"
