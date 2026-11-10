"""生产路径的「失去生命」钩子：建号/战始/回始/读档都必须重绑轮回者。

`CombatEngine.__init__` 早于 `setup_attributes`，当时 `state.player` 还是 None；
战始快照、读档与沙盒深拷贝又会剔除 Entity 上的 `_hp_engine_ref` 弱引用。
不重绑的话【脆弱】【庇护】【血契】以及「失去生命后」反应在**真实游戏**里
对轮回者静默失效，只有显式调用 `_bind_existing_hp_hooks()` 的测试夹具能过。
本文件全程走 `GameEngine.execute_action`，不手工绑定钩子。
"""
from __future__ import annotations

import pytest

from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance, Entity

from tests.setup_support import begin_battle, finish_initial_daowen


@pytest.fixture()
def engine(tmp_path):
    return GameEngine(db_path=str(tmp_path / "hooks.db"),
                      save_dir=str(tmp_path / "saves"),
                      autosave=False, rng_seed=11)


def _setup_to_battle(e: GameEngine) -> None:
    e.execute_action("setup_attributes", {
        "name": "钩验者", "blood_points": 11, "speed_points": 6, "mana_points": 8})
    finish_initial_daowen(e, prefer="杀伐")
    e.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    e.execute_action("setup_choose_region", {"region": "扭曲都市"})


def _assert_bound(e: GameEngine, where: str) -> None:
    p = e.state.player
    assert p is not None, where
    ref = p.__dict__.get("_hp_intercept_ref")
    assert ref is not None, f"{where}: 轮回者未绑定失血拦截钩子"
    assert ref() is e.combat, where
    # 正式流程刻意不绑完整引擎：那会改变属性封顶与「失去生命后」反应窗口。
    assert p.__dict__.get("_hp_engine_ref") is None, where


def test_setup_binds_player_hook(engine):
    engine.execute_action("setup_attributes", {
        "name": "钩验者", "blood_points": 11, "speed_points": 6, "mana_points": 8})
    _assert_bound(engine, "建号后")


def test_battle_start_and_round_start_keep_hook(engine):
    _setup_to_battle(engine)
    assert begin_battle(engine)["success"]
    _assert_bound(engine, "战始后")
    assert engine.execute_action("round_start", {"relic_choices": {}})["success"]
    _assert_bound(engine, "回始后")


def test_fragile_repeats_player_hp_loss_without_manual_hook_binding(engine):
    """3 点伤害 + 【脆弱2】应重复 3 次（共 9 点）；不绑定钩子只会掉 3 点。"""
    _setup_to_battle(engine)
    assert begin_battle(engine)["success"]
    assert engine.execute_action("round_start", {"relic_choices": {}})["success"]
    p = engine.state.player
    foe = Entity("靶怪", "怪物", blood_limit=80, current_hp=80,
                 attack_count=1, attack_power=1)
    engine.state.enemies[:] = [foe]
    p.dao_wen["脆弱"] = DaoWenInstance(
        DaoWen(name="脆弱", formula="", cost_type="消耗",
               cost_formula="2X", effect_formula=""), x_value=0, x_free=True)
    p.current_mana = 50
    cast = engine.execute_action("use_daowen", {
        "daowen_name": "脆弱", "x": 2, "target": p.name, "dodge": False})
    assert cast["success"], cast.get("error")
    hp0 = p.current_hp
    engine.combat._apply_hostile_damage(p, 3, source=foe)
    assert p.current_hp == hp0 - 9, "脆弱未按生产路径重复失血"
    assert not any(s.name == "脆弱" for s in p.status_effects)


def test_save_and_load_rebinds_hook(engine, tmp_path):
    _setup_to_battle(engine)
    assert begin_battle(engine)["success"]
    assert engine.execute_action("round_start", {"relic_choices": {}})["success"]
    slot = "hook_check"
    saved = engine.save_game(slot)
    assert saved["success"], saved
    loaded = GameEngine(db_path=str(tmp_path / "loaded.db"),
                        save_dir=str(tmp_path / "saves"),
                        autosave=False, rng_seed=11)
    assert loaded.load_game(slot)["success"]
    _assert_bound(loaded, "读档后")
