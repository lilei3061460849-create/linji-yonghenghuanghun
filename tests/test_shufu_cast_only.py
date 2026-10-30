"""【束缚】只允许施法（瞬发或自定义法术）的回归测试。"""

from engine.daowen import DaoWenEngine
from engine.models import Consumable, DaoWen, DaoWenInstance, Entity, StatusEffect
from tests.setup_support import (advance_to_player_turn, grant_extra_actions,
                                 hand_priority_to_enemy, opponent_passes)
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
    # 交替出手：优先权在轮回者侧时不提供 prepare_monster_phase（旧断言把它当成
    # 常驻可选项，是「怪物阶段可随时准备」的过期口径）。这里锁的是「束缚者只剩法术行动」。
    assert action_types == {"cast", "define_spell"}
    assert "prepare_monster_phase" not in action_types
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

    # 交替出手：施法后出手权归敌方，先按规则走过敌方【休整】/回终/回始，
    # 才能测到「束缚」本身对被禁行动的拦截。
    back = advance_to_player_turn(engine)
    assert back.get("success"), back
    assert engine.state.duel_turn == "player_side"

    for action_type, params in (
        ("prepare_attack", {"actor_ref": "player:0"}),
        ("use_daowen", {"daowen_name": "杀伐", "x": 1,
                         "target_ref": "enemy:0"}),
        ("focus", {}),
        ("consume_item", {"name": "不存在的物品"}),
    ):
        blocked = engine.execute_action(action_type, params)
        assert not blocked["success"], (action_type, blocked)
        assert "【束缚】" in blocked["error"], (action_type, blocked)

    # 《规则正文》第232-233行：禁止列表是「普攻、直接发动道纹、移除法术、使用消耗品、
    # 聚能、逃跑、指挥友方」；【跳过】属于「必要的回合推进」，不在禁止之列。
    # 旧断言把 rest 也算作被禁主动行动，与规则不符，故改为锁「跳过照常可用」。
    rest = engine.execute_action("rest", {})
    assert rest["success"], rest


def test_prepare_monster_phase_is_offered_only_when_enemy_holds_priority(tmp_path):
    """交替出手：怪物阶段的可选项跟着优先权走，不是常驻入口。"""
    engine, _player, _target = _bound_engine(tmp_path, "gate_offer")
    assert "prepare_monster_phase" not in {
        a["action_type"] for a in engine.get_available_actions()["actions"]}
    priority = hand_priority_to_enemy(engine)
    assert priority.get("success"), priority
    assert "prepare_monster_phase" in {
        a["action_type"] for a in engine.get_available_actions()["actions"]}


def test_custom_spell_actions_are_allowed_but_direct_daowen_is_blocked(tmp_path):
    engine, _player, _target = _bound_engine(tmp_path, "spell_actions")
    # 本用例连续定义/施展两次法术，按「阶数＝重数」各 2 次出手，先给足预算。
    grant_extra_actions(_player, 6)

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
    # 交替出手：两次主动行动之间必须经过敌方一手，否则第二次会被优先权闸门拦下。
    back = opponent_passes(engine)
    assert back.get("success"), back
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

    # 交替出手：这一步由靶尸行动，先把出手权交到敌方。
    priority = hand_priority_to_enemy(engine)
    assert priority.get("success"), priority
    cast = engine.execute_action("cast", {
        "actor_ref": "enemy:0", "target_ref": "player:0",
        "flow": "发动杀伐X于目标",
        "steps": [{"x": 1, "dodge": False, "trigger_spell_choices": {}}],
    })
    assert cast["success"], cast
