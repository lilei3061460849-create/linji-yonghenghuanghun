"""【束缚】只允许施法（瞬发或自定义法术）的回归测试。"""

from engine.daowen import DaoWenEngine
from engine.models import Consumable, DaoWen, DaoWenInstance, Entity, StatusEffect
from tests.test_reincarnator_basic_attack import _engine


def _bound_engine(tmp_path, suffix="cast_only"):
    engine = _engine(
        f"shufu_{tmp_path.name}_{suffix}",
        blood_points=1, speed_points=8, mana_points=16,
    )
    player = engine.state.player
    player.add_status(StatusEffect(
        name="束缚", remaining_rounds=2, value=1, source="测试束缚"))
    target = Entity(
        name="靶尸", entity_type="怪物", blood_limit=200, current_hp=200,
        speed_limit=0, current_speed=0, mana_limit=1, current_mana=1,
    )
    engine.state.enemies[:] = [target]
    return engine, player, target


def test_shufu_calculation_says_cast_only_and_keeps_x_round_duration():
    target = Entity(name="目标", entity_type="轮回者")
    calc = DaoWenEngine.calculate_shufu(3, target=target)

    assert calc["duration"] == 3
    assert "只能施法" in calc["effect"]
    assert "其余主动行动不可用" in calc["summary"]
    assert "持续3回合" in calc["summary"]


def test_bound_player_can_cast_but_cannot_use_other_active_actions(tmp_path):
    engine, player, target = _bound_engine(tmp_path)
    assert not engine.combat.can_act(player)
    assert engine.combat.can_act(player, action_type="cast")
    assert engine.combat.can_act(player, action_type="define_spell")

    available = engine.get_available_actions()
    action_types = {action["action_type"] for action in available["actions"]}
    assert action_types == {"cast", "define_spell", "prepare_monster_phase"}
    assert "cast(flow=...)" in available["binding_restriction"]
    assert "define_spell/cast(kind=spell)" in available["binding_restriction"]

    result = engine.execute_action("cast", {
        "flow": "发动杀伐X于目标",
        "steps": [{"x": 1, "dodge": False, "trigger_spell_choices": {}}],
        "target_ref": "enemy:0",
    })
    assert result["success"], result
    assert target.current_hp < 200
    assert player.has_status("束缚")

    for action_type, params in (
        ("prepare_attack", {"actor_ref": "player:0"}),
        ("use_daowen", {"daowen_name": "杀伐", "x": 1,
                         "target_ref": "enemy:0"}),
        ("focus", {}),
        ("rest", {}),
        ("consume_item", {"name": "不存在的物品"}),
    ):
        blocked = engine.execute_action(action_type, params)
        assert not blocked["success"], (action_type, blocked)
        assert "【束缚】" in blocked["error"], (action_type, blocked)


def test_custom_spell_actions_are_allowed_but_direct_daowen_is_blocked(tmp_path):
    engine, _player, _target = _bound_engine(tmp_path, "spell_actions")

    blocked = engine.execute_action("cast", {
        "kind": "daowen", "daowen_name": "杀伐", "x": 1,
        "target_ref": "enemy:0",
    })
    assert not blocked["success"]
    assert "只能进行法术行动" in blocked["error"]

    def spell(name):
        return {
            "name": name,
            "required_daowen": ["杀伐"],
            "trigger_condition": "受到伤害前",
            "effect_flow": "发动杀伐X于攻击者",
        }

    via_cast = engine.execute_action("cast", {"kind": "spell", "spell": spell("受缚反击")})
    assert via_cast["success"], via_cast
    direct = engine.execute_action("define_spell", {"spell": spell("受缚守护")})
    assert direct["success"], direct

    cannot_remove = engine.execute_action("undefine_spell", {"spell_name": "受缚反击"})
    assert not cannot_remove["success"]
    assert "只能进行法术行动" in cannot_remove["error"]


def test_shackling_item_status_uses_the_same_cast_only_gate(tmp_path):
    engine, player, target = _bound_engine(tmp_path, "item_status")
    player.status_effects.clear()
    target.entity_type = "轮回者"
    target.dao_wen["杀伐"] = DaoWenInstance(DaoWen(
        name="杀伐", formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    engine.state.consumables = [Consumable("镇魂铁钉", "施加束缚2", 3, 3)]

    applied = engine.execute_action("consume_item", {
        "name": "镇魂铁钉", "target_ref": "enemy:0"})
    assert applied["success"], applied
    assert target.has_status("束缚")
    assert not engine.combat.can_act(target)
    assert engine.combat.can_act(target, action_type="cast")

    cast = engine.execute_action("cast", {
        "actor_ref": "enemy:0", "target_ref": "player:0",
        "flow": "发动杀伐X于目标",
        "steps": [{"x": 1, "dodge": False, "trigger_spell_choices": {}}],
    })
    assert cast["success"], cast
