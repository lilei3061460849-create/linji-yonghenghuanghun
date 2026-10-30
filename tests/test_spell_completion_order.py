"""同条件法术按定义完成先来后到，而非按阵营或提交顺序。"""
from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import DaoWen, DaoWenInstance, Entity, GameState, Spell


def _combat():
    state = GameState(phase="in_combat", combat_subphase="player_actions", current_round=1)
    player = Entity("轮回者", "轮回者", blood_limit=100, current_hp=50,
                    mana_limit=50, current_mana=50)
    enemy = Entity("靶怪", "怪物", blood_limit=100, current_hp=50,
                   mana_limit=50, current_mana=50)
    state.player = player
    state.enemies = [enemy]
    for entity in (player, enemy):
        entity.dao_wen["再生"] = DaoWenInstance(DaoWen(
            name="再生", formula="", cost_type="消耗", cost_formula="X", effect_formula=""),
            x_value=0, x_free=True)
    return CombatEngine(state, DiceEngine()), player, enemy


def _complete(combat, holder, name, trigger):
    spell = Spell(name=name, required_daowen=["再生"], trigger_condition=trigger,
                  effect_flow="发动再生X于自身")
    combat.complete_spell(holder, spell)
    return spell


def _decision(target_ref):
    return {"use": True, "steps": [{"x": 1, "target_ref": target_ref, "dodge": False}]}


def _fired_names(logs):
    return [entry["spell"] for entry in logs if entry.get("execution")]


def test_same_holder_damage_reactions_follow_completion_not_submission_order():
    combat, player, enemy = _combat()
    a = _complete(combat, player, "A", "受到伤害后")
    b = _complete(combat, player, "B", "受到伤害后")
    assert (a.completion_order, b.completion_order) == (1, 2)

    # 故意按B、A提交；实际仍须A先完成所以A先触发。
    logs = combat._resolve_spell_reactions(
        "受到伤害后", player, enemy,
        {"B": _decision("player:0"), "A": _decision("player:0")},
        combat._combat_entity_refs())
    assert _fired_names(logs) == ["A", "B"]


def test_reversing_completion_order_reverses_damage_reaction_order():
    combat, player, enemy = _combat()
    _complete(combat, player, "A", "受到伤害后")
    _complete(combat, player, "B", "受到伤害后")
    # 用另一组状态让B先完成，避免同名定义只改列表而不改完成事实。
    combat2, player2, enemy2 = _combat()
    _complete(combat2, player2, "B", "受到伤害后")
    _complete(combat2, player2, "A", "受到伤害后")
    logs = combat2._resolve_spell_reactions(
        "受到伤害后", player2, enemy2,
        {"A": _decision("player:0"), "B": _decision("player:0")},
        combat2._combat_entity_refs())
    assert _fired_names(logs) == ["B", "A"]


def test_same_condition_across_characters_uses_global_completion_order():
    combat, player, enemy = _combat()
    # B在敌方身上但先完成；A在玩家身上后完成。提交列表故意把A放前面。
    b = _complete(combat, enemy, "B", "回终")
    a = _complete(combat, player, "A", "回终")
    assert b.completion_order < a.completion_order
    refs = combat._combat_entity_refs()
    logs = combat.resolve_global_trigger_spells(
        "回终", {"player:0": {"A": _decision("player:0")},
                  "enemy:0": {"B": _decision("enemy:0")}}, refs)
    assert _fired_names(logs) == ["B", "A"]
    assert a.to_dict()["completion_order"] == a.completion_order
