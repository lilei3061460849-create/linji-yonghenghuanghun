"""自定义资源 v2：随机目标、失忆代价、存档持久化（Issue #95）。

v1 已交付的注册/结算路径不在此重复测试，这里只覆盖 v2 新增能力及其边界。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import custom_resources  # noqa: E402
from engine.api import GameEngine  # noqa: E402
from engine.models import Entity  # noqa: E402
from tests.setup_support import finish_initial_daowen  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _engine(seed: int = 42) -> GameEngine:
    engine = GameEngine(rng_seed=seed, autosave=False)
    engine.execute_action("setup_attributes",
                          {"blood_points": 11, "speed_points": 6, "mana_points": 8})
    finish_initial_daowen(engine)
    engine.state.current_region = "罪孽都市"
    engine.state.relics = []
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "player_actions"
    engine.state.player.current_mana = 100
    return engine


def _enemy(engine, name: str, hp: int = 100) -> Entity:
    monster = Entity(name=name, entity_type="怪物", blood_limit=hp, current_hp=hp,
                     attack_count=1, attack_power=5)
    engine.state.enemies.append(monster)
    return monster


@pytest.fixture(autouse=True)
def _clean_registry():
    custom_resources.clear()
    yield
    custom_resources.clear()


ARTIFACT_RANDOM = {
    "name": "乱星盘",
    "category": "法器",
    "uniqueness": "只有本器能在不指定目标的情况下随机指定一个敌对角色并造成固定伤害。",
    "summary": "随机一名敌人受到4点伤害",
    "costs_action": False,
    "effects": [{"verb": "damage", "target": "random_enemy", "amount": 4}],
}

DAOWEN_AMNESIA = {
    "name": "刻名",
    "category": "道纹",
    "uniqueness": "以失去指定道纹为代价发动，现有资源没有可选择的失忆代价通道。",
    "summary": "失忆1：对[目标]造成2X点伤害",
    "cost": {"type": "失忆", "amount": 1},
    "effects": [{"verb": "damage", "target": "target", "amount": "2*X"}],
}


# ==================== 随机目标 ====================

def test_random_enemy_target_picks_exactly_one_and_records_roll():
    engine = _engine()
    custom_resources.register(ARTIFACT_RANDOM)
    engine._grant_custom_resource(custom_resources.get("乱星盘"))
    foes = [_enemy(engine, "靶甲"), _enemy(engine, "靶乙"), _enemy(engine, "靶丙")]

    result = engine.execute_action("activate_resource", {"name": "乱星盘"})

    assert result["success"], result
    logs = result["result"]["effects"]
    assert len(logs) == 1
    roll = logs[0]["random_roll"]
    assert roll["pool_size"] == 3
    assert roll["selected_index"] in (0, 1, 2)
    assert roll["selected_name"] == logs[0]["target"]
    assert roll["seed"] == 42
    hurt = [foe for foe in foes if foe.current_hp < foe.blood_limit]
    assert len(hurt) == 1 and hurt[0].name == logs[0]["target"]


def test_random_target_is_reproducible_with_same_seed():
    rolls = []
    for _ in range(2):
        engine = _engine(seed=7)
        custom_resources.clear()
        custom_resources.register(ARTIFACT_RANDOM)
        engine._grant_custom_resource(custom_resources.get("乱星盘"))
        for name in ("靶甲", "靶乙", "靶丙"):
            _enemy(engine, name)
        result = engine.execute_action("activate_resource", {"name": "乱星盘"})
        rolls.append(result["result"]["effects"][0]["random_roll"])
    assert rolls[0] == rolls[1]


def test_random_target_with_no_candidate_is_skipped_without_roll():
    engine = _engine()
    custom_resources.register(ARTIFACT_RANDOM)
    engine._grant_custom_resource(custom_resources.get("乱星盘"))
    result = engine.execute_action("activate_resource", {"name": "乱星盘"})
    assert result["success"], result
    entry = result["result"]["effects"][0]
    assert entry["skipped"] == "没有合法目标"
    assert "random_roll" not in entry


# ==================== 失忆代价 ====================

def test_amnesia_cost_requires_explicit_forget_choice():
    engine = _engine()
    custom_resources.register(DAOWEN_AMNESIA)
    engine._grant_custom_resource(custom_resources.get("刻名"))
    player = engine.state.player
    assert "杀伐" in player.dao_wen
    foe = _enemy(engine, "靶甲")
    mana_before = player.current_mana

    rejected = engine.execute_action("use_daowen", {
        "daowen_name": "刻名", "x": 1, "target": foe.name,
        "dodge": False, "blood_shadow": False,
    })
    assert not rejected["success"]
    assert "forget_daowen" in rejected["error"]
    assert "杀伐" in player.dao_wen
    assert player.current_mana == mana_before
    assert player.actions_used_this_round == 0
    assert foe.current_hp == foe.blood_limit


def test_amnesia_cost_rejects_unknown_name_and_keeps_state():
    engine = _engine()
    custom_resources.register(DAOWEN_AMNESIA)
    engine._grant_custom_resource(custom_resources.get("刻名"))
    player = engine.state.player
    foe = _enemy(engine, "靶甲")

    rejected = engine.execute_action("use_daowen", {
        "daowen_name": "刻名", "x": 1, "target": foe.name,
        "forget_daowen": "不存在的道纹", "dodge": False, "blood_shadow": False,
    })
    assert not rejected["success"]
    assert "不在当前持有道纹中" in rejected["error"]
    assert foe.current_hp == foe.blood_limit
    assert player.actions_used_this_round == 0


def test_amnesia_cost_removes_the_chosen_daowen_and_settles_effect():
    engine = _engine()
    custom_resources.register(DAOWEN_AMNESIA)
    engine._grant_custom_resource(custom_resources.get("刻名"))
    player = engine.state.player
    foe = _enemy(engine, "靶甲")

    accepted = engine.execute_action("use_daowen", {
        "daowen_name": "刻名", "x": 2, "target": foe.name,
        "forget_daowen": "杀伐", "dodge": False, "blood_shadow": False,
    })
    assert accepted["success"], accepted
    assert "杀伐" not in player.dao_wen
    assert [inst.dao_wen.name for inst in player.forgotten_cost_daowen] == ["杀伐"]
    assert foe.current_hp == 96  # 2X = 4


# ==================== 存档持久化 ====================

def test_custom_resource_specs_survive_save_and_load(tmp_path):
    engine = _engine()
    engine.save_dir = str(tmp_path)
    custom_resources.register(ARTIFACT_RANDOM)
    saved = engine.save_game("v2")
    assert saved["success"], saved

    custom_resources.clear()  # 模拟跨会话：注册表为空
    assert custom_resources.get("乱星盘") is None

    loaded = engine.load_game("v2")
    assert loaded["success"], loaded
    assert "乱星盘" in loaded["custom_resources"]["restored"]
    assert custom_resources.get("乱星盘") is not None
    assert custom_resources.get("乱星盘").spec["effects"][0]["target"] == "random_enemy"


def test_restore_skips_already_registered_and_reports_bad_spec():
    custom_resources.register(ARTIFACT_RANDOM)
    report = custom_resources.restore_specs([
        dict(ARTIFACT_RANDOM),          # 已注册 → 跳过
        {"name": "", "category": "法器"},
    ])
    assert report["skipped"] == ["乱星盘"]
    assert report["restored"] == []
    assert report["errors"], "空名声明必须被登记为错误而不是静默丢弃"


DAOWEN_TOO_EXPENSIVE = {
    "name": "灼骨",
    "category": "道纹",
    "uniqueness": "以自损血限为代价造成固定伤害，现有资源没有可缩放的等价血限支付通道。",
    "summary": "血限5X：对[目标]造成3X点伤害",
    "cost": {"type": "血限", "amount": "5*X"},
    "effects": [{"verb": "damage", "target": "target", "amount": "3*X"}],
}


def test_custom_daowen_cost_failure_rolls_back_everything():
    """失败回滚：代价付不起时不得扣资源、不得消耗出手、不得结算效果。"""
    engine = _engine()
    custom_resources.register(DAOWEN_TOO_EXPENSIVE)
    engine._grant_custom_resource(custom_resources.get("灼骨"))
    player = engine.state.player
    foe = _enemy(engine, "靶甲")
    player.blood_limit = 3
    player.current_hp = 3

    rejected = engine.execute_action("use_daowen", {
        "daowen_name": "灼骨", "x": 2, "target": foe.name,
        "dodge": False, "blood_shadow": False,
    })
    assert not rejected["success"], rejected
    assert player.blood_limit == 3
    assert player.actions_used_this_round == 0
    assert foe.current_hp == foe.blood_limit
    assert "血限" not in player.reversible_cost_layers
