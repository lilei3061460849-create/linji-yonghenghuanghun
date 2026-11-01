from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import Consumable, Entity, GameState, Relic, StatusEffect


def _state_with_player(*relics: Relic) -> tuple[GameState, CombatEngine, Entity]:
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100, mana_limit=20, current_mana=20,
                    speed_limit=10, current_speed=10)
    state.player = player
    state.relics = list(relics)
    state.enemies = [Entity("M", "怪物", blood_limit=100, current_hp=100, shield=10)]
    return state, CombatEngine(state, DiceEngine()), player


def _bleed_once(timing: str, tags: set[str] | None = None) -> tuple[Entity, dict]:
    _, combat, player = _state_with_player(Relic("血誓戒", ""))
    detail = combat.pay_numeric_cost(player, "流血", 4, cost_context={
        "timing": timing, "source": f"src-{timing}", "source_type": "matrix",
        "actor": player, "target": player, "mechanic": "cost", "subtype": "bleed",
        "amount": 4, "tags": tags or {"active_payment"},
    })
    return player, detail


def test_bleed_source_matrix_for_blood_oath():
    """同为流血4：血誓戒只认回始/玩家行动/反应的主动支付上下文。"""
    for timing in ("pre_battle_event", "battle_start", "round_end"):
        player, detail = _bleed_once(timing)
        assert player.current_hp == 96
        assert player.shield == 0
        assert player.blood_oath_used_this_round is False
        assert "blood_oath" not in detail["owner"]["detail"]

    for timing in ("round_start", "player_action", "reaction"):
        player, detail = _bleed_once(timing)
        assert player.current_hp == 96
        assert player.shield == 4
        assert player.blood_oath_used_this_round is True
        assert detail["owner"]["detail"]["blood_oath"]["type"] == "shield"

    player, detail = _bleed_once("player_action", tags={"automatic"})
    assert player.current_hp == 96
    assert player.shield == 0
    assert "blood_oath" not in detail["owner"]["detail"]


def test_damage_source_matrix_context_and_pierce():
    """同为造成8伤害：攻击/道纹/遗物来源都带damage ctx；贯穿统一在伤害入口生效。"""
    _, combat, player = _state_with_player()
    target = combat.state.enemies[0]
    player.add_status(StatusEffect("贯穿", value=1, remaining_rounds=1, source="test"))

    rows = [
        ("普通攻击", "attack", {"attack"}),
        ("杀伐", "daowen", {"daowen"}),
        ("烙痕钉", "relic", {"relic"}),
    ]
    for source, source_type, tags in rows:
        target.current_hp = target.blood_limit = 100
        target.shield = 10
        detail = combat._apply_hostile_damage(target, 8, source=player, ctx={
            "timing": "player_action", "source": source, "source_type": source_type,
            "actor": player, "target": target, "mechanic": "damage", "subtype": source_type,
            "amount": 8, "tags": tags,
        })
        assert detail["actual_damage"] == 8
        assert detail["shield_absorbed"] == 0
        assert target.shield == 10
        assert detail["damage_type"] == "无视格挡"
        assert detail["ctx"]["source"] == source
        assert detail["ctx"]["source_type"] == source_type
        assert detail["hp_loss_ctx"]["parent_event_id"] == detail["ctx"]["event_id"]


def test_heal_source_matrix_contexts_and_bottle_storage():
    """同为回复：道纹/寄生/负岳索/血族血脉/龙血瓶均记录heal ctx。"""
    state, combat, player = _state_with_player(Relic("血族血脉", "", tags=["血族"]))
    player.current_hp = 80
    direct = state.apply_heal(player, 5, ctx={
        "timing": "player_action", "source": "再生", "source_type": "daowen",
        "actor": player, "target": player, "mechanic": "heal", "subtype": "daowen",
        "amount": 5, "tags": {"daowen"},
    })
    assert direct["heal_ctx"]["source"] == "再生"

    enemy = state.enemies[0]
    enemy.shield = 0
    enemy.add_status(StatusEffect("寄生", value=1, remaining_rounds=-1, source=player.name))
    player.current_hp = 80
    parasite = combat._apply_hostile_damage(enemy, 10, source=player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": enemy, "mechanic": "damage", "subtype": "daowen", "amount": 10,
    })["jisheng_heal"]
    assert parasite["heal_ctx"]["source"] == "寄生"

    friend = Entity("F", "朋友", blood_limit=30, current_hp=30)
    friend.add_status(StatusEffect("负岳索", value=1, remaining_rounds=-1, source="负岳索"))
    state.friends = [friend]
    player.current_hp = 80
    fuyue = combat._apply_hostile_damage(friend, 6, source=enemy, ctx={
        "timing": "monster_action", "source": "普通攻击", "source_type": "attack",
        "actor": enemy, "target": friend, "mechanic": "damage", "subtype": "attack", "amount": 6,
    })["fuyuesuo_heal"]
    assert fuyue["heal_ctx"]["source"] == "负岳索"

    player.current_hp = 80
    player.damage_dealt_this_round = 7
    state.combat_subphase = "await_round_end"
    lineage = next(e for e in combat.round_end()["effects"] if e.get("type") == "blood_lineage_heal")
    assert lineage["heal_ctx"]["source"] == "血族血脉"

    bottle = Consumable("龙血瓶", "", current_uses=10, max_uses=10)
    state.consumables = [bottle]
    player.current_hp = 99
    stored = state.apply_heal(player, 5, ctx={
        "timing": "player_action", "source": "急救箱", "source_type": "consumable",
        "actor": player, "target": player, "mechanic": "heal", "subtype": "consumable", "amount": 5,
    })
    assert stored["dragon_blood_bottle_stored"] == 4
    assert stored["dragon_blood_bottle_ctx"]["parent_event_id"] == stored["heal_ctx"]["event_id"]


def test_speed_source_matrix_contexts():
    """同为当前速度变化：疲惫、闪避、道纹增速、焦黑发丝均记录speed_change。"""
    state, combat, player = _state_with_player(Relic("回锋刀", ""), Relic("焦黑发丝", ""))
    enemy = state.enemies[0]

    combat.pay_numeric_cost(player, "疲惫", 2, cost_context={
        "timing": "battle_start", "source": "苍白之花", "source_type": "relic",
        "actor": player, "target": player, "mechanic": "cost", "subtype": "fatigue",
        "amount": 2, "tags": {"active_payment"}, "event_id": "cost-fatigue-matrix",
    })
    assert player._speed_change_events[-1]["source"] == "苍白之花"
    assert player._speed_change_events[-1]["amount"] == -2

    combat._spend_dodge_speed(player, "enemy:0")
    assert player._speed_change_events[-1]["source"] == "闪避"
    assert player._speed_change_events[-1]["amount"] == -1

    combat.apply_daowen_effect("超频", {"x": 3, "speed_boost": 3}, player, player)
    assert player._speed_change_events[-1]["source"] == "超频"
    assert player._speed_change_events[-1]["amount"] == 3

    enemy.current_hp = 0
    enemy.is_alive = False
    # 2026-09-13 全局上限：满速时焦黑发丝的 +2 会被吃掉而不记事件；
    # 本例验的是"来源归因写对了"，先腾出上限空间。
    player.current_speed = max(0, player.speed_limit - 2)
    combat._on_entity_death(enemy, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": enemy, "mechanic": "death", "subtype": "hp_zero",
        "event_id": "death-speed-matrix", "tags": {"death"},
    })
    assert player._speed_change_events[-1]["source"] == "焦黑发丝"
    assert player._speed_change_events[-1]["parent_event_id"] == enemy._death_ctx["event_id"]


def test_death_vs_leave_resolution_matrix(tmp_path):
    """命零给碎片；离场不给碎片，二者战终ctx明确区分。"""
    from engine.api import GameEngine

    engine = GameEngine(db_path=str(tmp_path / "matrix_resolution.db"), rng_seed=1)
    player = Entity("P", "轮回者", blood_limit=60, current_hp=60)
    dead = Entity("Dead", "怪物", blood_limit=100, current_hp=0)
    dead.is_alive = False
    dead.battle_start_blood_limit = 100
    dead._death_ctx = {
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "mechanic": "death", "subtype": "hp_zero", "event_id": "death-matrix",
        "tags": [], "parent_event_id": None, "actor": "P", "target": "Dead", "owner": None, "amount": 0,
    }
    left = Entity("Left", "怪物", blood_limit=100, current_hp=50)
    left.battle_start_blood_limit = 100
    engine.state.player = player
    engine.state.enemies = [dead, left]
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "await_round_end"
    engine.state.current_battle = 1
    engine.combat.state = engine.state
    engine.combat._remove_from_combat(left, "癌变", ctx={
        "timing": "player_action", "source": "癌变", "source_type": "system",
        "target": left, "mechanic": "leave", "subtype": "cancer", "event_id": "leave-matrix",
        "tags": {"leave", "no_shards"},
    })

    result = engine.execute_action("battle_end", {})
    assert result["success"], result
    assert result["result"]["shard_reward"] == 7  # 2 + 出生自带透支×5
    assert result["result"]["death_shard_rewards"][0]["name"] == "Dead"
    assert result["result"]["death_shard_rewards"][0]["ctx"]["subtype"] == "death_shard_reward"
    assert result["result"]["removed_via_alt_path"][0]["name"] == "Left"
    assert result["result"]["removed_via_alt_path"][0]["ctx"]["subtype"] == "leave_no_shards"
