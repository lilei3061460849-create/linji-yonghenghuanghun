"""2026-10-13 裁定：偏爱回始获得法限等量法力与异变，怪物不触发迷失。"""
from pathlib import Path

import pytest

from engine import battle_report as BR
from engine.combat import CombatEngine
from engine.combat_events import CombatEventType
from engine.dice import DiceEngine
from engine.mechanisms import apply_verb
from engine.models import Entity, GameState, MONSTER_MANA_RELIC, Relic, StatusEffect
from engine.rules_source import lethal_events

ROOT = Path(__file__).resolve().parents[1]
EFFECT = "[回始]获得等同[法限]的法力和【异变】"


def _entity(name="怪物", entity_type="怪物", mana_limit=10, current_mana=3):
    return Entity(name, entity_type, blood_limit=100, current_hp=100,
                  mana_limit=mana_limit, current_mana=current_mana,
                  speed_limit=3, current_speed=3)


def _arena(monster=None):
    player = _entity("轮回者", "轮回者")
    monster = monster if monster is not None else _entity()
    state = GameState(player=player, enemies=[monster], phase="in_combat",
                      combat_subphase="await_round_start")
    return state, CombatEngine(state, DiceEngine(seed=13)), player, monster


def _relic_effects(result):
    return [effect for effect in result["effects"]
            if effect.get("relic") == MONSTER_MANA_RELIC]


def test_default_relic_description_matches_new_rule():
    monster = _entity()
    relics = [relic for relic in monster.relics if relic.name == MONSTER_MANA_RELIC]
    assert len(relics) == 1
    assert relics[0].effect == EFFECT
    text = (ROOT / "物品索引.md").read_text(encoding="utf-8")
    entry = text.split(f"### {MONSTER_MANA_RELIC}\n", 1)[1].split("\n---", 1)[0]
    assert "［回始］获得等同［法限］的法力和【异变】" in entry


def test_existing_relic_description_is_upgraded_without_duplicating_it():
    monster = Entity("旧档怪物", "怪物", blood_limit=100, current_hp=100,
                     relics=[Relic(MONSTER_MANA_RELIC, "[回始]法力恢复至上限")])
    assert len(monster.relics) == 1
    assert monster.relics[0].effect == EFFECT


def test_round_start_grants_mana_and_full_limit_mutation_each_round():
    state, combat, player, monster = _arena()
    monster.mutation_count = 45
    for expected_mutation in (55, 65, 75):
        result = combat.round_start()
        assert monster.current_mana == 10
        assert monster.mutation_count == expected_mutation
        assert monster.is_alive and monster.current_hp == 100
        assert player.current_mana == 3 and player.mutation_count == 0
        effects = _relic_effects(result)
        assert len(effects) == 1
        assert effects[0]["mana_requested"] == 10
        assert effects[0]["mutation_added"] == 10
        assert effects[0]["mutation_total"] == expected_mutation
    assert state.current_round == 3


def test_mana_cap_does_not_reduce_the_mutation_grant():
    _, combat, _, monster = _arena(_entity(current_mana=8))
    result = combat.round_start()
    assert monster.current_mana == 10
    assert monster.mutation_count == 10
    assert _relic_effects(result)[0]["mana_gained"] == 2


def test_gouhun_blocks_only_mana_not_mutation():
    _, combat, _, monster = _arena()
    monster.add_status(StatusEffect("勾魂", remaining_rounds=2, value=1))
    result = combat.round_start()
    assert monster.current_mana == 3
    assert monster.mutation_count == 10
    assert _relic_effects(result)[0]["mana_gained"] == 0


def test_grant_reads_current_mana_limit_not_the_original_panel():
    _, combat, _, monster = _arena()
    # 「枯竭」是下一次[战始]当前法力-X（延迟扣款），压法限要用「法限」。
    combat.pay_numeric_cost(monster, "法限", 6)
    assert monster.mana_limit == 4
    combat.round_start()
    assert monster.current_mana == 4 and monster.mutation_count == 4


def test_zero_mana_limit_grants_nothing():
    _, combat, _, monster = _arena(_entity(mana_limit=0, current_mana=0))
    result = combat.round_start()
    assert monster.current_mana == 0 and monster.mutation_count == 0
    assert not _relic_effects(result)


def test_removed_relic_stops_both_grants_but_not_monster_immunity():
    _, combat, _, monster = _arena()
    monster.relics.clear()
    monster.mutation_count = 75
    result = combat.round_start()
    assert monster.current_mana == 3 and monster.mutation_count == 75
    assert monster.is_alive and not _relic_effects(result)
    assert monster.add_mutation(10)["lost"] is False


def test_dead_monster_receives_neither_grant():
    _, combat, _, monster = _arena()
    monster.is_alive = False
    monster.current_hp = 0
    result = combat.round_start()
    assert monster.current_mana == 3 and monster.mutation_count == 0
    assert not _relic_effects(result)


@pytest.mark.parametrize("storage", ["entity", "player_side"])
def test_non_monster_holder_receives_both_grants(storage):
    state, combat, player, _ = _arena()
    relic = Relic(MONSTER_MANA_RELIC, EFFECT)
    (player.relics if storage == "entity" else state.relics).append(relic)
    combat.round_start()
    assert player.current_mana == 10 and player.mutation_count == 10


@pytest.mark.parametrize("storage", ["entity", "player_side"])
def test_sealed_player_relic_does_not_trigger(storage):
    state, combat, player, _ = _arena()
    relic = Relic(MONSTER_MANA_RELIC, EFFECT)
    (player.relics if storage == "entity" else state.relics).append(relic)
    state.sealed_relics[MONSTER_MANA_RELIC] = 1
    combat.round_start()
    assert player.current_mana == 3 and player.mutation_count == 0


def test_opponent_reincarnator_holder_uses_its_own_relics():
    opponent = _entity("死斗对手", "轮回者")
    state, combat, player, _ = _arena(opponent)
    state.opponent_relics.append(Relic(MONSTER_MANA_RELIC, EFFECT))
    combat.round_start()
    assert opponent.current_mana == 10 and opponent.mutation_count == 10
    assert player.current_mana == 3 and player.mutation_count == 0


def test_inherited_relic_does_not_make_non_monster_immune():
    state, combat, player, monster = _arena()
    monster.relics.clear()
    state.relics.append(Relic(MONSTER_MANA_RELIC, EFFECT))
    player.mutation_count = 45
    combat.round_start()
    assert player.mutation_count == 55
    assert not player.is_alive and player.current_hp == 0
    assert player._death_ctx["source"] == "迷失"


def test_friend_holder_keeps_existing_lost_transformation_behavior():
    state, combat, _, _ = _arena()
    friend = _entity("朋友", "朋友", mana_limit=5, current_mana=2)
    friend.relics.append(Relic(MONSTER_MANA_RELIC, EFFECT))
    friend.mutation_count = 46
    state.friends.append(friend)
    combat.round_start()
    assert friend.mutation_count == 51
    assert friend.is_alive and friend.entity_type == "怪物"
    assert friend not in state.friends and friend in state.enemies
    combat.round_start()
    assert friend.mutation_count == 56 and friend.is_alive


def test_same_holder_in_two_side_lists_triggers_only_once():
    monster = _entity()
    state, combat, _, _ = _arena(monster)
    state.player = monster  # 兼容迷失变身后仍保留的 player 引用
    combat.round_start()
    assert monster.mutation_count == 10


@pytest.mark.parametrize("source", ["model", "cost", "verb"])
def test_monster_never_triggers_lost_from_any_mutation_source(source):
    _, combat, _, monster = _arena()
    monster.mutation_count = 49
    if source == "model":
        result = monster.add_mutation(100)
    elif source == "cost":
        result = combat.pay_numeric_cost(monster, "异变", 100)["owner"]["mutation"]
    else:
        result = apply_verb(combat, "mutation", {"target": monster, "layers": 100})
    assert result["mutation_total"] == 149
    assert not result["lost"] and not result["collapsed"]
    assert monster.is_alive and monster.current_hp == 100
    assert not [event for event in combat.event_stream
                if event.event_type == CombatEventType.ENTITY_DIED]


def test_direct_lost_resolver_cannot_kill_or_transform_a_monster():
    state, combat, _, monster = _arena()
    monster.mutation_count = 150
    result = combat._resolve_mutation_lost(monster)
    assert result["lost"] is False
    assert monster.is_alive and monster.current_hp == 100
    assert state.enemies == [monster]


def test_monster_payload_does_not_advertise_a_fake_lost_clock():
    monster = _entity()
    monster.mutation_count = 150
    assert "迷失" not in monster.lethal_counters()
    assert all("迷失" not in item for item in monster.lethal_progress())
    data = monster.to_dict()
    assert data["mutation_count"] == 150
    assert "迷失" not in data["lethal_counters"]
    assert "迷失" not in BR.resource_line(monster)


@pytest.mark.parametrize("entity_type", ["轮回者", "朋友", "员工", "造物", "赤族"])
def test_non_monster_lost_threshold_is_unchanged(entity_type):
    entity = _entity(entity_type, entity_type)
    entity.add_mutation(49)
    result = entity.add_mutation(1)
    assert result["lost"] is True and result["collapsed"] is False
    assert entity.is_alive  # 模型层仍交由战斗层处理非怪物迷失
    assert entity.lethal_counters()["迷失"] == (50, 50)


def test_lost_type_exclusion_is_driven_by_the_structured_source(monkeypatch):
    entry = lethal_events.by_id("mishi")
    assert entry["excluded_entity_types"] == ["怪物"]
    monster = _entity()
    assert monster.can_trigger_mutation_lost() is False
    monkeypatch.setitem(entry, "excluded_entity_types", [])
    assert monster.can_trigger_mutation_lost() is True
    assert "迷失" in monster.lethal_counters()
    assert monster.add_mutation(50)["lost"] is True


def test_risk_preview_has_no_fake_monster_lost_warning():
    from engine.ai_preview import ActionPreview

    monster = _entity(current_mana=10)
    monster.mutation_count = 150
    diff = {"player_dead": False, "player": {
        "hp_after": 100, "mana_after": 10, "speed_after": 3,
        "mutation_delta": 20, "mutation_after": 170,
    }}
    level, reasons = ActionPreview.risk_classify(diff, monster)
    assert level in {"LOW", "SAFE"}, reasons
    assert not any("迷失" in reason for reason in reasons)


def test_self_preservation_has_no_monster_mutation_veto():
    from types import SimpleNamespace
    from engine.ai_tactics import TacticalAI

    _, combat, _, monster = _arena()
    monster.mutation_count = 150
    ai = SimpleNamespace(player=monster, engine=SimpleNamespace(combat=combat),
                         _rounds_since_damage=0,
                         SELF_PRESERVE_MARGIN=TacticalAI.SELF_PRESERVE_MARGIN)
    bias, veto = TacticalAI._self_preserve_bias(
        ai, {"player": {"mutation_delta": 20, "mutation_after": 170}}, enemy_hp_loss=0)
    assert bias == 0 and veto is None


def test_ally_x_helper_respects_monster_immunity_but_preserves_non_monster_budget():
    from engine.ai_rules import pick_ally_daowen_x

    monster = _entity()
    monster.mutation_count = 150
    assert pick_ally_daowen_x(monster, "全力", monster) >= 1
    friend = _entity("朋友", "朋友")
    friend.mutation_count = 49
    assert pick_ally_daowen_x(friend, "全力", friend) == 0


def test_relic_constructor_upgrades_old_inherited_descriptions():
    relic = Relic(MONSTER_MANA_RELIC, "[回始]法力恢复至上限", tags=["继承"])
    assert relic.effect == EFFECT and relic.tags == ["继承"]


def test_real_entity_load_upgrades_old_descriptions_and_applies_the_new_effect(tmp_path):
    from engine.api import GameEngine

    engine = GameEngine(db_path=str(tmp_path / "legacy.db"),
                        save_dir=str(tmp_path / "saves"), rng_seed=13)
    snapshot = engine._serialize_entity_full(_entity())
    snapshot["mutation_count"] = 49
    for relic in snapshot["relics"]:
        relic["effect"] = "[回始]法力恢复至上限"
    restored = engine._deserialize_entity_full(snapshot)
    assert restored.relics and all(relic.effect == EFFECT for relic in restored.relics)
    _, combat, _, restored = _arena(restored)
    combat.round_start()
    assert restored.current_mana == 10 and restored.mutation_count == 59
    assert restored.is_alive and restored.current_hp == 100
