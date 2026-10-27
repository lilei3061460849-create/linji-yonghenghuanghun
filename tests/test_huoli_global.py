"""【疯狂】遗物化回归（2026-10-25 用户令）。

口径：原原始怪物道纹【疯狂】退出道纹体系，改为通用遗物——
「所有角色每回合出手次数+1」。判定走全场遗物查询，不再挂状态。

- 正常：任一场上角色持有【疯狂】，双方每人每回合出手 +1。
- 边界：没有该遗物时无加成；同一件只加一次（多人持有不叠加）；
  遗物被停用（enabled=False）时不生效。
- 错误：遗物化后它不再是可发动道纹，道纹注册表里查不到，提交发动必须被拒。
"""
import os
import sys

import pytest

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.daowen import DaoWenEngine
from engine.models import Entity, Relic


def _engine(suffix):
    os.makedirs("/tmp/linji_tests", exist_ok=True)
    engine = GameEngine(db_path=f"/tmp/linji_tests/test_kuangkuang_relic_{suffix}.db", rng_seed=7)
    engine.execute_action("setup_attributes", {
        "name": "试者", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": "罪孽都市"})
    engine.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    engine.state.phase = "in_combat"
    engine.state.current_round = 2
    return engine


def _monster(engine, name="疯狂怪", hp=100):
    m = Entity(name=name, entity_type="怪物", blood_limit=hp, current_hp=hp,
               attack_count=1, attack_power=1)
    engine.state.enemies.append(m)
    return m


def _hold(engine, entity, name="疯狂", enabled=True):
    """玩家侧遗物挂 state.relics（归属查询读那里），其余角色挂自身 relics。"""
    target = engine.state.relics if entity.entity_type == "轮回者" else entity.relics
    target.append(Relic(name=name, effect="所有角色每回合出手次数+1", enabled=enabled))


def test_normal_relic_boosts_every_character():
    """正常：玩家持有【疯狂】，双方出手预算都 +1。"""
    engine = _engine("normal")
    player = engine.state.player
    monster = _monster(engine)
    base_player = engine.combat.single_round_action_count(player)
    base_monster = engine.combat.single_round_action_count(monster)

    _hold(engine, player)

    assert engine.combat.single_round_action_count(player) == base_player + 1
    assert engine.combat.single_round_action_count(monster) == base_monster + 1, \
        "遗物写的是「所有角色」，敌方同样 +1"


def test_boundary_monster_holder_helps_player_and_disabled_does_not():
    """边界：怪物持有同样全场 +1；遗物停用或不存在时不加；多人持有只加一次。"""
    engine = _engine("boundary")
    player = engine.state.player
    monster = _monster(engine)
    base_player = engine.combat.single_round_action_count(player)

    _hold(engine, player, enabled=False)
    assert engine.combat.single_round_action_count(player) == base_player, "停用的遗物不生效"

    _hold(engine, monster)
    assert engine.combat.single_round_action_count(player) == base_player + 1, \
        "怪物持有的【疯狂】对玩家同样生效"
    assert engine.combat.single_round_action_count(monster) == 3

    _hold(engine, player)  # 两人各持一件，仍然只 +1
    assert engine.combat.single_round_action_count(player) == base_player + 1


def test_error_relicized_daowen_cannot_be_activated():
    """错误输入：【疯狂】已遗物化，注册表里没有它，发动必须失败。"""
    DaoWenEngine.register_all()
    assert "疯狂" not in DaoWenEngine._registry
    engine = _engine("error")
    player = engine.state.player
    with pytest.raises(Exception):
        DaoWenEngine.resolve("疯狂", 1, target=player, caster=player)
