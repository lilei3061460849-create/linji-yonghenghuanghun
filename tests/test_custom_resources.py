"""自定义资源（v1）：声明 → 注册 → 真结算。

覆盖四类资源的正常路径、边界与错误输入，并证明「不改代码、只改数据」：
`data/custom_resources/examples/` 下的 5 份 JSON 覆盖三类道纹/遗物/法器/消耗品，
全部由同一个 `define_resource` 入口吃进去。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import custom_resources  # noqa: E402
from engine.api import GameEngine  # noqa: E402
from engine.daowen import DaoWenEngine  # noqa: E402
from engine.mechanisms import MECHANISMS  # noqa: E402
from tests.setup_support import finish_initial_daowen  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "data" / "custom_resources" / "examples"


def _engine(phase: str = "in_combat") -> GameEngine:
    engine = GameEngine(rng_seed=42)
    engine.execute_action("setup_attributes",
                          {"blood_points": 11, "speed_points": 6, "mana_points": 8})
    finish_initial_daowen(engine)
    engine.state.current_region = "罪孽都市"
    engine.state.relics = []
    engine.state.phase = phase
    if phase == "in_combat":
        engine.state.combat_subphase = "player_actions"
        engine.state.player.current_mana = 100
    return engine


def _enemy(engine, name: str = "靶怪", hp: int = 100):
    from engine.models import Entity
    monster = Entity(name=name, entity_type="怪物", blood_limit=hp, current_hp=hp,
                     attack_count=1, attack_power=5)
    engine.state.enemies.append(monster)
    return monster


@pytest.fixture(autouse=True)
def _clean_registry():
    custom_resources.clear()
    yield
    custom_resources.clear()


# ==================== 正常路径 ====================

def test_define_daowen_registers_and_resolves_with_x(monkeypatch):
    """道纹：声明即注册，X 直接进伤害；扣法力、占出手。"""
    engine = _engine()
    enemy = _enemy(engine)
    spec = json.loads((EXAMPLES / "dao_wen_shifeng.json").read_text(encoding="utf-8"))
    result = engine.execute_action("define_resource", {"spec": spec})
    assert result["success"], result
    assert "试锋" in engine.state.player.dao_wen

    before_mana = engine.state.player.current_mana
    before_actions = engine.state.player.actions_used_this_round
    hit = engine.execute_action("use_daowen", {
        "daowen_name": "试锋", "x": 3, "target_ref": "enemy:0"})
    assert hit["success"], hit
    assert enemy.current_hp == 100 - 9
    assert engine.state.player.current_mana == before_mana - 3
    assert engine.state.player.actions_used_this_round == before_actions + 1


def test_define_relic_triggers_passively_on_round_start():
    """遗物：编译成机制，回始自动触发，不占出手。"""
    engine = _engine()
    _enemy(engine)
    spec = json.loads((EXAMPLES / "relic_canzhu.json").read_text(encoding="utf-8"))
    assert engine.execute_action("define_resource", {"spec": spec})["success"]
    assert any(r.name == "残烛" for r in engine.state.relics)

    # 当前法力不得超过[法限]，故取低于上限的值观察增量。
    limit = engine.state.player.mana_limit
    engine.state.player.current_mana = max(0, limit - 2)
    engine.state.combat_subphase = "await_round_start"
    logs = engine.execute_action("round_start", {"relic_choices": {}})
    assert logs["success"], logs
    assert engine.state.player.current_mana == limit, logs
    assert engine.state.player.actions_used_this_round == 0


def test_define_artifact_activates_via_action():
    """法器：主动激活，默认不占出手。"""
    engine = _engine()
    _enemy(engine)
    spec = json.loads((EXAMPLES / "artifact_huixiang.json").read_text(encoding="utf-8"))
    assert engine.execute_action("define_resource", {"spec": spec})["success"]
    assert "回响铃" in engine.state.artifacts_owned

    before = engine.state.player.actions_used_this_round
    used = engine.execute_action("activate_resource", {"name": "回响铃"})
    assert used["success"], used
    assert engine.state.player.has_status("回响")
    assert engine.state.player.actions_used_this_round == before


def test_define_consumable_uses_declared_effects_and_durability():
    """消耗品：走 consume_item，按声明结算，扣耐久，不占出手。"""
    engine = _engine()
    _enemy(engine)
    engine.state.player.current_hp = 40
    spec = json.loads((EXAMPLES / "consumable_huichun.json").read_text(encoding="utf-8"))
    assert engine.execute_action("define_resource", {"spec": spec})["success"]

    item = next(c for c in engine.state.consumables if c.name == "回春散")
    before = engine.state.player.actions_used_this_round
    used = engine.execute_action("consume_item", {"name": "回春散"})
    assert used["success"], used
    assert engine.state.player.current_hp == 55
    assert item.current_uses == 1
    assert engine.state.player.actions_used_this_round == before


def test_examples_load_without_code_changes():
    """数据驱动：整个目录一次载入，四类资源各至少一件。"""
    registered = custom_resources.load_specs(EXAMPLES)
    categories = {resource.category for resource in registered}
    assert categories == {"道纹", "遗物", "法器", "消耗品"}
    assert len(registered) == 5


# ==================== 边界 ====================

def test_daowen_is_in_battle_only_unless_out_of_combat_true():
    """边界：默认局内；声明 out_of_combat=True 才允许局外发动。"""
    spec = json.loads((EXAMPLES / "dao_wen_shifeng.json").read_text(encoding="utf-8"))
    engine = _engine("pre_battle")
    assert engine.execute_action("define_resource", {"spec": spec})["success"]
    denied = engine.execute_action("use_daowen", {
        "daowen_name": "试锋", "x": 1, "target_ref": "enemy:0"})
    assert not denied["success"] and "局外发动须条文明示" in denied["error"]

    from engine.models import Entity
    engine.state.enemies.append(Entity(name="路人", entity_type="怪物",
                                       blood_limit=10, current_hp=10,
                                       attack_count=1, attack_power=1))
    spec_out = dict(spec, name="试锋", out_of_combat=True)
    custom_resources.clear()
    engine.state.player.dao_wen.pop("试锋", None)
    assert engine.execute_action("define_resource", {"spec": spec_out})["success"]
    allowed = engine.execute_action("use_daowen", {
        "daowen_name": "试锋", "x": 1, "target_ref": "enemy:0"})
    assert allowed["success"], allowed
    assert engine.state.player.actions_used_this_round == 0


def test_relic_with_x_is_rejected():
    """边界：遗物不得含 X —— 即使只是把 2 写成 2*X。"""
    spec = {
        "name": "违规遗物", "category": "遗物",
        "uniqueness": "测试用",
        "trigger": {"kind": "phase", "key": "round_start"},
        "effects": [{"verb": "mana", "target": "self", "amount": "X"}],
    }
    with pytest.raises(custom_resources.CustomResourceError):
        custom_resources.register(spec)


def test_triggered_consumable_cannot_be_used_manually():
    """边界：被动型消耗品只能由触发结算，手动使用应被拒绝且不扣耐久。"""
    engine = _engine()
    _enemy(engine)
    spec = {
        "name": "余烬粉", "category": "消耗品", "uses": 2,
        "summary": "受到伤害后获得 5 点格挡",
        "trigger": {"kind": "phase", "key": "round_end"},
        "effects": [{"verb": "shield", "target": "self", "amount": 5}],
    }
    assert engine.execute_action("define_resource", {"spec": spec})["success"]
    item = next(c for c in engine.state.consumables if c.name == "余烬粉")
    denied = engine.execute_action("consume_item", {"name": "余烬粉"})
    assert not denied["success"]
    assert "被动触发型" in denied["error"]
    assert item.current_uses == 2


# ==================== 错误输入 ====================

@pytest.mark.parametrize("bad_spec, keyword", [
    ({"name": "三字名", "category": "道纹", "uniqueness": "x",
      "cost": {"type": "法力", "amount": "X"},
      "effects": [{"verb": "damage", "amount": "X"}]}, "二字"),
    ({"name": "缺X", "category": "道纹", "uniqueness": "x",
      "cost": {"type": "法力", "amount": 1},
      "effects": [{"verb": "damage", "amount": 3}]}, "X"),
    ({"name": "怪词", "category": "道纹", "uniqueness": "x",
      "cost": {"type": "法力", "amount": "X"},
      "effects": [{"verb": "not_a_verb", "amount": "X"}]}, "未注册"),
    ({"name": "无时机", "category": "遗物", "uniqueness": "x",
      "effects": [{"verb": "mana", "target": "self", "amount": 2}]}, "触发时机"),
    ({"name": "无可比", "category": "法器",
      "effects": [{"verb": "mana", "target": "self", "amount": 2}]}, "uniqueness"),
    ({"name": "零耐久", "category": "消耗品", "uses": 0,
      "effects": [{"verb": "heal", "target": "self", "amount": 1}]}, "uses"),
])
def test_illegal_declarations_are_rejected(bad_spec, keyword):
    """错误：非法声明必须被校验器拒绝，不得静默降级。"""
    with pytest.raises(custom_resources.CustomResourceError) as excinfo:
        custom_resources.register(bad_spec)
    assert keyword in str(excinfo.value)


def test_builtin_name_collision_is_rejected():
    """错误：与内建资源重名必须拒绝（命名全宇宙唯一）。"""
    engine = _engine()
    spec = {"name": "杀伐", "category": "道纹", "uniqueness": "重名测试",
            "cost": {"type": "法力", "amount": "X"},
            "effects": [{"verb": "damage", "amount": "X"}]}
    result = engine.execute_action("define_resource", {"spec": spec})
    assert not result["success"] and "重名" in result["error"]


def test_expression_engine_rejects_code_execution():
    """错误：数值表达式只能算数，不能执行任意代码。"""
    for expression in ("__import__('os').system('true')", "open('/tmp/x')", "X.real"):
        with pytest.raises(custom_resources.CustomResourceError):
            custom_resources.evaluate(expression, 1)


def test_resources_survive_registry_rebuild():
    """注册表重建（register_all）后自定义道纹仍在——不需要改核心代码。"""
    spec = json.loads((EXAMPLES / "dao_wen_shifeng.json").read_text(encoding="utf-8"))
    custom_resources.register(spec)
    DaoWenEngine.register_all()
    assert "试锋" in DaoWenEngine._registry


def test_clear_unregisters_mechanisms():
    """回归：清理自定义资源必须同时摘掉机制，不能留下幽灵触发。"""
    spec = json.loads((EXAMPLES / "relic_canzhu.json").read_text(encoding="utf-8"))
    custom_resources.register(spec)
    assert any(name.startswith("自定义遗物·") for name in MECHANISMS.names())
    custom_resources.clear()
    assert not any(name.startswith("自定义遗物·") for name in MECHANISMS.names())
