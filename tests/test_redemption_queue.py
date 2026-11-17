"""救赎候选按稳定身份排队，并在选择之间重新验证。"""
from engine.api import GameEngine
from engine.models import Entity
from tests.test_redemption_and_setup import _engine, _ready_combat


def monster(name):
    return Entity(name=name, entity_type="怪物", blood_limit=100, current_hp=20,
                  attack_count=1, attack_power=1, mutation_count=49)


def test_candidates_are_unique_and_resolved_in_order():
    e = _ready_combat(_engine("queue_order"))
    a, b, c = monster("甲"), monster("乙"), monster("丙")
    e.state.enemies[:] = [a, b, c]
    assert e.combat.check_redemption(a)
    e.combat.check_redemption(c)
    e.combat.check_redemption(b)
    e.combat.check_redemption(c)
    assert [x["runtime_id"] for x in e.state.pending_redemption_candidates] == [c.runtime_id, b.runtime_id]
    assert e.execute_action("resolve_redemption", {"option": "终结"})["success"]
    assert e.state.pending_redemption["runtime_id"] == c.runtime_id
    assert a.departure_reason == "" and not a.is_alive
    assert e.execute_action("resolve_redemption", {"option": "终结"})["success"]
    assert e.state.pending_redemption["runtime_id"] == b.runtime_id
    assert e.execute_action("resolve_redemption", {"option": "终结"})["success"]
    assert not e.state.pending_redemption and not e.state.pending_redemption_candidates
    assert all(not m.is_alive and not m.is_departed for m in (a, b, c))


def test_candidates_rechecked_when_dead_or_ineligible():
    e = _ready_combat(_engine("queue_invalid"))
    a, b, c, d = (monster(name) for name in ("甲", "乙", "丙", "丁"))
    e.state.enemies[:] = [a, b, c, d]
    e.combat.check_redemption(a)
    for m in (b, c, d):
        e.combat.check_redemption(m)
    b.current_hp = 0
    b.is_alive = False
    c.mutation_count = 50
    assert e.execute_action("resolve_redemption", {"option": "终结"})["success"]
    assert e.state.pending_redemption["runtime_id"] == d.runtime_id
    assert not b.is_departed and c.is_alive and not c.is_departed
    assert not e.state.pending_redemption_candidates


def test_queue_roundtrip_and_identical_names(tmp_path):
    e = _ready_combat(_engine("queue_save"))
    e.save_dir = str(tmp_path)
    a, b = monster("同名"), monster("同名")
    e.state.enemies[:] = [a, b]
    e.combat.check_redemption(a)
    e.combat.check_redemption(b)
    assert e.save_game("queue")["success"]
    loaded = GameEngine(autosave=False)
    loaded.save_dir = str(tmp_path)
    assert loaded.load_game("queue")["success"]
    a2, b2 = loaded.state.enemies
    assert [c["runtime_id"] for c in loaded.state.pending_redemption_candidates] == [b.runtime_id]
    assert loaded.execute_action("resolve_redemption", {"option": "终结"})["success"]
    assert not a2.is_alive and not a2.is_departed
    assert b2.departure_reason == "救赎" and loaded.state.pending_redemption["runtime_id"] == b2.runtime_id
    assert loaded.execute_action("resolve_redemption", {"option": "终结"})["success"]
    assert not b2.is_alive and not b2.is_departed
    assert not loaded.state.pending_redemption and not loaded.state.pending_redemption_candidates


def test_sculpture_precedes_redemption_for_eligible_target():
    e = _ready_combat(_engine("queue_sculpture"))
    m = monster("雕塑优先")
    m.attack_count = m.attack_power = 0
    e.state.enemies[:] = [m]
    effect = e.combat.check_redemption(m)
    assert effect["type"] == "sculpture"
    assert m.is_sculptured and m.departure_reason == "雕塑"
    assert not e.state.pending_redemption


def test_cancel_invalid_active_candidate_without_fake_reward():
    e = _ready_combat(_engine("queue_active_invalid"))
    a, b = monster("甲"), monster("乙")
    e.state.enemies[:] = [a, b]
    e.combat.check_redemption(a)
    e.combat.check_redemption(b)
    shards = e.state.shards
    a.current_hp = 0
    a.is_departed = False
    a.departure_reason = ""
    result = e.execute_action("resolve_redemption", {"option": "终结"})
    assert result["success"] and result["action"] == "救赎·取消"
    assert e.state.shards == shards
    assert e.state.pending_redemption["runtime_id"] == b.runtime_id
    assert b.departure_reason == "救赎"


def test_queued_candidate_interrupts_later_conversion():
    from tests.test_task8_new_daowen import _player_with, monster as make_monster, cast
    e, combat, player = _player_with("病变")
    first = make_monster(e, hp=100, name="先")
    first.current_hp = 20
    first.attack_count = first.attack_power = 1
    second = make_monster(e, hp=100, name="后")
    second.current_hp = 20
    second.mutation_count = 50
    second.attack_count = second.attack_power = 1
    assert combat.check_redemption(first)
    result = cast(combat, player, "病变", 1, target=second,
                  from_kind="异变", to_kind="血限")
    assert second.mutation_count == 49 and second.blood_limit == 100
    assert any(effect["type"] == "cost_conversion_interrupted" for effect in result["effects"])
    assert e.state.pending_redemption_candidates[0]["runtime_id"] == second.runtime_id
    assert e.execute_action("resolve_redemption", {"option": "终结"})["success"]
    assert second.departure_reason == "救赎"


def test_one_area_effect_queues_every_eligible_monster():
    from tests.test_task8_new_daowen import _player_with, monster as make_monster, cast
    e, combat, player = _player_with("冲击")
    target = make_monster(e, hp=100, name="主目标")
    first = make_monster(e, hp=100, name="甲")
    second = make_monster(e, hp=100, name="乙")
    for m in (target, first, second):
        m.attack_count = m.attack_power = 1
    first.current_hp = second.current_hp = 21
    player_before = player.current_hp
    result = cast(combat, player, "冲击", 1, target=target)
    assert result["effects"]
    assert player.current_hp == player_before - 5
    assert first.current_hp == second.current_hp == 16
    assert first.departure_reason == "救赎" and second.is_alive
    assert e.state.pending_redemption["runtime_id"] == first.runtime_id
    assert [c["runtime_id"] for c in e.state.pending_redemption_candidates] == [second.runtime_id]
    assert e.execute_action("resolve_redemption", {"option": "终结"})["success"]
    assert second.departure_reason == "救赎"
    assert e.execute_action("resolve_redemption", {"option": "终结"})["success"]
    assert not e.state.pending_redemption and target.is_alive


def test_complete_blood_limit_change_checks_after_clamp_and_death():
    e = _ready_combat(_engine("queue_limit_change"))
    m = monster("血限增长")
    m.blood_limit = 95
    e.state.enemies[:] = [m]
    result = e.combat._apply_blood_limit_change(m, 1, "血限增长", "up")
    assert result["applied"] == 1 and result["redemption"]["type"] == "redemption"
    assert m.blood_limit == 96 and m.current_hp == 20 and m.departure_reason == "救赎"


def test_lethal_blood_limit_change_is_death_not_redemption():
    e = _ready_combat(_engine("queue_limit_lethal"))
    m = monster("血限归零")
    e.state.enemies[:] = [m]
    result = e.combat._apply_blood_limit_change(m, -100, "血限归零", "down")
    assert result["died"] and m.current_hp == 0 and not m.is_alive
    assert not m.is_departed and not e.state.pending_redemption


def test_chuyong_checks_redemption_after_both_values_are_synced():
    from engine.models import StatusEffect
    e = _ready_combat(_engine("queue_chuyong"))
    caster = e.state.player
    caster.blood_limit, caster.current_hp = 100, 20
    m = monster("初拥同步")
    m.blood_limit, m.current_hp = 80, 60
    e.state.enemies[:] = [m]
    m.add_status(StatusEffect(name="初拥", value=1, remaining_rounds=2,
                              effect_data={"caster_id": caster.runtime_id}))
    assert e.combat._sync_chuyong(m)
    assert (m.blood_limit, m.current_hp) == (100, 20)
    assert m.departure_reason == "救赎"
    assert e.state.pending_redemption["runtime_id"] == m.runtime_id


def test_mutation_verb_triggers_redemption_only_at_eligible_boundary():
    from engine.mechanisms.verbs import apply_verb
    e = _ready_combat(_engine("mutation_verb"))
    a, b = monster("甲"), monster("乙")
    a.mutation_count = 50
    b.current_hp = 21
    b.mutation_count = 50
    e.state.enemies[:] = [a, b]
    unchanged = apply_verb(e.combat, "mutation", {"target": b, "layers": -1})
    assert unchanged["mutation_total"] == 49 and b.is_alive
    assert not e.state.pending_redemption
    paid = apply_verb(e.combat, "mutation", {"target": a, "layers": -1})
    assert paid["mutation_total"] == 49 and paid["redemption"]["type"] == "redemption"
    assert a.departure_reason == "救赎" and b.is_alive


def test_custom_effect_stops_for_queued_target_but_not_other_target():
    from engine.custom_resources import apply_effects
    e = _ready_combat(_engine("mutation_compound"))
    first, queued, other = monster("先"), monster("待决"), monster("正常")
    queued.mutation_count = 50
    other.current_hp = 50
    other.mutation_count = 50
    e.state.enemies[:] = [first, queued, other]
    e.combat.check_redemption(first)
    effects = [{"verb": "mutation", "target": "all_enemies", "amount": -1},
               {"verb": "blood_limit", "target": "all_enemies", "amount": -5}]
    apply_effects(e.combat, effects, actor=e.state.player, target=queued, source="复合测试")
    assert queued.mutation_count == 49 and queued.blood_limit == 100
    assert other.mutation_count == 49 and other.blood_limit == 95
    assert [c["runtime_id"] for c in e.state.pending_redemption_candidates] == [queued.runtime_id]


def test_mutation_verb_honors_free_crystal_defer_and_sculpture_priority():
    from engine.mechanisms.verbs import apply_verb
    e = _ready_combat(_engine("mutation_deferred"))
    m = monster("延迟")
    m.mutation_count = 50
    e.state.enemies[:] = [m]
    e.combat._redemption_deferred.add(m.runtime_id)
    apply_verb(e.combat, "mutation", {"target": m, "layers": -1})
    assert m.is_alive and not e.state.pending_redemption
    e.combat._redemption_deferred.clear()
    m.attack_count = m.attack_power = 0
    event = e.combat.check_redemption(m, force=True)
    assert event["type"] == "sculpture" and m.is_sculptured


def test_mutation_raising_to_50_does_not_redeem_and_later_reduction_does():
    from engine.mechanisms.verbs import apply_verb
    e = _ready_combat(_engine("mutation_sequence"))
    m = monster("连续异变")
    e.state.enemies[:] = [m]
    up = apply_verb(e.combat, "mutation", {"target": m, "layers": 1})
    assert up["mutation_total"] == 50 and m.is_alive and not e.state.pending_redemption
    down = apply_verb(e.combat, "mutation", {"target": m, "layers": -1})
    assert down["mutation_total"] == 49 and m.departure_reason == "救赎"
    assert e.state.pending_redemption["runtime_id"] == m.runtime_id
    assert not e.state.pending_redemption_candidates


def test_bingbian_blood_relief_interrupts_before_mutation_cost():
    from tests.test_task8_new_daowen import _player_with, monster as make_monster, cast
    e, combat, player = _player_with("病变")
    m = make_monster(e, hp=100, name="血限转换")
    m.blood_limit, m.current_hp, m.mutation_count = 95, 20, 49
    m.attack_count = m.attack_power = 1
    m.reversible_cost_layers["血限"] = 1
    result = cast(combat, player, "病变", 1, target=m, from_kind="血限", to_kind="异变")
    assert m.blood_limit == 96 and m.mutation_count == 49
    assert m.reversible_cost_layers["血限"] == 0
    assert m.departure_reason == "救赎"
    assert any(effect["type"] == "cost_conversion_interrupted" for effect in result["effects"])


def test_reflect_life_loss_redeems_only_after_nonlethal_loss():
    from engine.effect_context import make_context
    e = _ready_combat(_engine("reflect_redemption"))
    m = monster("反噬怪")
    m.current_hp = 24
    e.state.enemies[:] = [m]
    ctx = make_context(timing="combat", source="攻击", source_type="attack",
                       actor=m, target=e.state.player, mechanic="damage")
    result = e.combat._resolve_reflect_aftermath(m, 5, ctx, e.state.player)
    assert result["reflected"] == 5 and m.current_hp == 19
    assert result["redemption"]["type"] == "redemption"
    assert m.departure_reason == "救赎"


def test_combined_blood_and_hp_reduction_checks_after_both_values():
    from tests.test_task8_new_daowen import _player_with, monster as make_monster
    e, combat, player = _player_with("断脉")
    m = make_monster(e, hp=100, name="组合降血")
    m.current_hp, m.attack_count, m.attack_power = 25, 1, 1
    result = combat.apply_daowen_effect("测试血限变化", {
        "x": 1, "blood_limit_reduction": 4, "hp_reduction": 5,
    }, player, m)
    assert (m.blood_limit, m.current_hp) == (96, 20)
    assert m.departure_reason == "救赎"
    assert any(effect["type"] == "redemption" for effect in result["effects"])


def test_spell_skips_redemption_candidate_without_affecting_other_target():
    from engine.spell_execution import SpellCastRequest, StepRequest
    from tests.test_spell_priority_windows import _engine as spell_engine
    e, player, first = spell_engine()
    first.blood_limit, first.current_hp, first.mutation_count = 100, 20, 49
    pending = monster("待决")
    e.state.enemies.append(pending)
    e.combat.check_redemption(first)
    e.combat.check_redemption(pending)
    execution = e.combat.build_instant_execution(player, "发动杀伐X于目标", pending,
                                                [{"x": 1}], e.combat._combat_entity_refs())
    execution.run_all()
    assert execution.results[0].status.value == "skipped"
    assert pending.current_hp == 20 and pending.is_alive
    assert first.departure_reason == "救赎"


def test_atomic_blood_increase_effect_queues_each_monster():
    from tests.test_task8_new_daowen import _player_with, monster as make_monster
    e, combat, player = _player_with("庇护")
    target = make_monster(e, hp=100, name="增血限")
    target.blood_limit, target.current_hp = 95, 20
    target.attack_count = target.attack_power = 1
    result = combat.apply_daowen_effect("测试", {"x": 1, "blood_limit_increase": 1}, player, target)
    assert target.blood_limit == 96 and target.departure_reason == "救赎"
    assert any(effect["type"] == "redemption" for effect in result["effects"])


def test_paused_spell_and_redemption_queue_round_trip_no_repeated_effect(tmp_path):
    from tests.test_spell_priority_windows import _engine as spell_engine, _cast, _skip
    e, player, first = spell_engine(player_extra=2)
    first.blood_limit, first.current_hp, first.mutation_count = 100, 21, 49
    second = monster("候选")
    blocker = Entity("守场", "怪物", blood_limit=100, current_hp=100,
                     attack_count=1, attack_power=1, mana_limit=10, current_mana=10)
    e.state.enemies.extend([second, blocker])
    flow = "发动杀伐X于目标→发动庇护X于自身"
    result = _cast(e, "player:0", flow, "enemy:0", [1, 1])
    assert result["success"] and first.current_hp == 20
    assert e.state.pending_redemption["runtime_id"] == first.runtime_id
    e.combat.check_redemption(second)
    assert len(e.combat._spell_stack) == 1
    e.save_dir = str(tmp_path)
    assert e.save_game("spell_and_queue")["success"]
    loaded = GameEngine(autosave=False)
    loaded.save_dir = str(tmp_path)
    assert loaded.load_game("spell_and_queue")["success"]
    first2, second2, _ = loaded.state.enemies
    assert first2.current_hp == 20 and loaded.state.player.actions_used_this_round == 1
    assert len(loaded.combat._spell_stack[-1]["execution"].results) == 1
    assert loaded.execute_action("resolve_redemption", {"option": "终结"})["success"]
    assert second2.departure_reason == "救赎"
    assert loaded.execute_action("resolve_redemption", {"option": "终结"})["success"]
    assert first2.current_hp == 20 and loaded.state.player.actions_used_this_round == 1
    execution = loaded.combat._spell_stack[-1]["execution"]
    assert _skip(loaded, "enemy:2")["success"]
    assert [r.daowen for r in execution.results] == ["杀伐", "庇护"]
    assert first2.current_hp == 20 and loaded.state.player.actions_used_this_round == 2


def test_restored_borrowed_corpse_snapshot_checks_after_all_fields():
    e = _ready_combat(_engine("jieshi_redemption"))
    m = monster("还原")
    m.blood_limit = 95
    m._jieshi_snapshot = {"blood_limit": 100, "mana_limit": 4, "speed_limit": 2,
                          "attack_count": 1, "attack_power": 1}
    e.state.enemies[:] = [m]
    effects = e.combat._settle_daowen_status_expiry(m, ["借尸"])
    assert (m.blood_limit, m.current_hp, m.mana_limit, m.speed_limit) == (100, 20, 4, 2)
    assert m.departure_reason == "救赎"
    assert [effect["type"] for effect in effects] == ["jieshi_expired", "redemption"]


def test_mutation_verb_never_redeems_zero_hp_entity():
    from engine.mechanisms.verbs import apply_verb
    e = _ready_combat(_engine("mutation_zero_hp"))
    m = monster("命零")
    m.current_hp, m.mutation_count = 0, 50
    e.state.enemies[:] = [m]
    apply_verb(e.combat, "mutation", {"target": m, "layers": -1})
    assert m.current_hp == 0 and not e.state.pending_redemption


def test_infinite_blood_limit_removal_checks_after_clamping():
    from engine.models import StatusEffect
    e = _ready_combat(_engine("removed_infinite_redemption"))
    m = Entity("过限", "怪物", blood_limit=1, current_hp=1,
               attack_count=1, attack_power=1, mutation_count=49)
    e.state.enemies[:] = [m]
    status = StatusEffect(name="无限血限", value=1, remaining_rounds=1)
    m.add_status(status)
    m.current_hp = 2
    m.status_effects.remove(status)
    e.combat._settle_removed_statuses(m, [status])
    assert m.current_hp == 1 and m.departure_reason == "救赎"
    assert e.state.pending_redemption["runtime_id"] == m.runtime_id


def test_mutation_payment_completes_before_redemption():
    e = _ready_combat(_engine("cost_mutation"))
    m = monster("支付异变")
    m.mutation_count = 40
    e.state.enemies[:] = [m]
    payment = e.combat.pay_numeric_cost(m, "异变", 1)
    assert payment["owner"]["paid"] == 1 and m.mutation_count == 41
    assert payment["owner"]["redemption"]["type"] == "redemption"
    assert m.departure_reason == "救赎"


def test_mutation_payment_ineligible_and_sculpture_priority():
    e = _ready_combat(_engine("cost_mutation_ineligible"))
    m = monster("未满足")
    m.mutation_count = 49
    e.state.enemies[:] = [m]
    payment = e.combat.pay_numeric_cost(m, "异变", 1)
    assert payment["owner"]["paid"] == 1 and m.mutation_count == 50
    assert m.is_alive and not e.state.pending_redemption
    m.mutation_count = 40
    m.attack_count = m.attack_power = 0
    payment = e.combat.pay_numeric_cost(m, "异变", 1)
    assert payment["owner"]["redemption"]["type"] == "sculpture"
    assert m.departure_reason == "雕塑" and not e.state.pending_redemption


def test_mutation_payment_callback_occurs_once_and_redeems_its_target():
    e = _ready_combat(_engine("cost_callback"))
    caster = e.state.player
    victim = monster("钉目标")
    victim.current_hp = 25
    e.state.enemies[:] = [victim]
    e.state.event_modifiers["brand_nail_target_ref"] = "enemy:0"
    before = caster.mutation_count
    payment = e.combat.pay_numeric_cost(caster, "异变", 1)
    assert caster.mutation_count == before + 1
    assert victim.current_hp == 15 and victim.departure_reason == "救赎"
    assert payment["owner"]["brand_nail"]["actual_damage"] == 10
    assert e.state.pending_redemption["runtime_id"] == victim.runtime_id
    assert not e.state.pending_redemption_candidates


def test_mutation_payment_other_entity_types_do_not_redeem():
    e = _ready_combat(_engine("cost_other_types"))
    player = e.state.player
    player.current_hp = 1
    ally = Entity("同伴", "朋友", blood_limit=100, current_hp=20,
                  attack_count=1, attack_power=1, mutation_count=40)
    e.state.friends.append(ally)
    before = player.mutation_count
    for entity in (player, ally):
        result = e.combat.pay_numeric_cost(entity, "异变", 1)
        assert result["owner"]["paid"] == 1 and "redemption" not in result["owner"]
    assert player.mutation_count == before + 1 and ally.mutation_count == 41
    assert player.is_alive and ally.is_alive and not e.state.pending_redemption


def test_compound_mutation_redemption_blocks_later_target_effects():
    e = _ready_combat(_engine("compound_mutation"))
    m = monster("复合目标")
    m.mutation_count = 50
    e.state.enemies[:] = [m]
    result = e.combat.apply_daowen_effect(
        "探针", {"x": 1, "mutation_reduction": 1, "blood_limit_increase": 10,
                 "attack_boost": 5, "target_shield": 3}, e.state.player, m)
    assert m.departure_reason == "救赎" and m.mutation_count == 49
    assert (m.blood_limit, m.attack_power, m.shield) == (100, 1, 0)
    assert any(effect.get("type") == "redemption" for effect in result["effects"])


def test_compound_wave_isolates_queued_candidate_and_keeps_other_shares():
    from engine.models import StatusEffect

    e = _ready_combat(_engine("compound_wave"))
    first, queued, other = monster("已有待决"), monster("候选"), monster("其他")
    queued.mutation_count = 50
    other.current_hp = 80
    other.mutation_count = 50
    e.state.enemies[:] = [first, queued, other]
    e.combat.check_redemption(first)
    other.add_status(StatusEffect("波及", remaining_rounds=-1, source=e.state.player.name))
    e.combat.apply_daowen_effect(
        "探针", {"x": 2, "mutation_reduction": 2, "blood_limit_increase": 10,
                 "attack_boost": 4}, e.state.player, queued)
    assert e.combat.is_redemption_pending(queued)
    assert queued.mutation_count == 49 and queued.blood_limit == 100 and queued.attack_power == 1
    assert other.mutation_count == 49 and other.blood_limit == 105 and other.attack_power == 3


def test_evolution_mutation_cost_checks_before_borrowing(monkeypatch):
    from engine.models import DaoWen, DaoWenInstance

    e = _ready_combat(_engine("evolution_cost_redemption"))
    m = monster("进化怪")
    m.mutation_count = 40
    e.state.enemies[:] = [m]
    e.state.player.dao_wen["加害"] = DaoWenInstance(
        DaoWen(name="加害", formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    monkeypatch.setattr(e.combat, "check_monster_difficulty", lambda _: {"signals": []})
    outcome = e.combat.execute_evolution(m, "加害", 1)
    assert outcome["success"] and outcome["interrupted_by_redemption"]
    assert m.mutation_count == 45 and m.departure_reason == "救赎"
    assert "加害" not in m.dao_wen and id(m) in e.combat._monster_evolved


def test_compound_named_status_does_not_affect_redeemed_target():
    e = _ready_combat(_engine("compound_named_status"))
    m = monster("目标")
    m.mutation_count = 50
    e.state.enemies[:] = [m]
    e.combat.apply_daowen_effect(
        "怜悯", {"x": 1, "mutation_reduction": 1, "blocked_payment_type": "生命"},
        e.state.player, m)
    assert m.departure_reason == "救赎" and not m.has_status("怜悯")


def test_compound_wave_named_status_keeps_other_target():
    from engine.models import StatusEffect

    e = _ready_combat(_engine("compound_wave_named_status"))
    m, other = monster("目标"), monster("其他")
    m.mutation_count = 50
    other.current_hp = 80
    other.add_status(StatusEffect("波及", remaining_rounds=-1, source=e.state.player.name))
    e.state.enemies[:] = [m, other]
    e.combat.apply_daowen_effect(
        "冥气", {"x": 2, "mutation_reduction": 2, "speed_loss_speed_limit": 1, "duration": 2},
        e.state.player, m)
    assert m.departure_reason == "救赎" and not m.has_status("冥气")
    assert other.has_status("冥气")


def test_ordinary_consumable_mutation_triggers_redemption():
    from engine.models import Consumable
    from engine.npc_consumables import consume_ordinary

    e = _ready_combat(_engine("consumable_mutation"))
    m = monster("用药怪")
    m.mutation_count = 40
    e.state.enemies[:] = [m]
    item = Consumable("异变药", "获得异变1", 1, 1)
    outcome = consume_ordinary(e.combat, item, m)
    assert outcome["result"]["redemption"]["type"] == "redemption"
    assert m.mutation_count == 41 and item.current_uses == 0


def test_grenade_does_not_debuff_redeemed_target():
    from engine.models import Consumable
    from engine.npc_consumables import consume_tool

    e = _ready_combat(_engine("grenade_redemption"))
    m = monster("手雷目标")
    m.current_hp = 30
    e.state.enemies[:] = [m]
    item = Consumable("高爆手雷", "", 1, 1)
    outcome = consume_tool(e.combat, item, {"target_ref": "enemy:0"}, e.state.player, [m])
    assert m.departure_reason == "救赎" and not m.has_status("手雷减攻")
    assert outcome["result"]["nade_minus"] == 0 and item.current_uses == 0


def test_water_gun_removes_infinite_blood_limit_before_redemption():
    from engine.models import Consumable, StatusEffect
    from engine.npc_consumables import consume_tool

    e = _ready_combat(_engine("watergun_redemption"))
    m = monster("水枪目标")
    m.blood_limit, m.current_hp = 1, 2
    m.add_status(StatusEffect("无限血限", remaining_rounds=2))
    e.state.enemies[:] = [m]
    item = Consumable("高压水枪", "", 1, 1)
    outcome = consume_tool(e.combat, item, {}, e.state.player, [m])
    assert outcome["success"] and m.current_hp == 1 and m.departure_reason == "救赎"


def test_merge_creation_checks_after_all_panels_and_departures():
    from engine.models import StatusEffect

    e = _ready_combat(_engine("merge_redemption"))
    m = monster("合体怪")
    m.current_hp = 21
    m.add_status(StatusEffect("化尸", remaining_rounds=2))
    creation = Entity("待合并", "造物", blood_limit=100, current_hp=1,
                      attack_count=2, attack_power=3)
    caster = Entity("施法怪", "怪物", blood_limit=100, current_hp=100,
                    attack_count=1, attack_power=1)
    e.state.enemies[:] = [caster, m, creation]
    result = e.combat.apply_daowen_effect(
        "合体", {"x": 2, "merge_creations": 2,
                 "merge_creation_ids": [m.runtime_id, creation.runtime_id]}, caster, m)
    assert (m.blood_limit, m.current_hp, m.attack_power, m.attack_count) == (200, 22, 4, 3)
    assert creation.departure_reason == "合体" and m.departure_reason == "救赎"
    assert any(item.get("type") == "redemption" for item in result["effects"])


def test_heal_verb_finishes_heal_ledger_before_redemption(monkeypatch):
    from engine.mechanisms.verbs import apply_verb
    e = _ready_combat(_engine("heal_verb_ledger"))
    m = monster("回复目标")
    m.current_hp = 10
    e.state.enemies[:] = [m]
    original = e.combat.check_redemption
    seen = []
    def check(entity, **kwargs):
        seen.append((entity.current_hp, entity.total_healed, len(entity._heal_events)))
        return original(entity, **kwargs)
    monkeypatch.setattr(e.combat, "check_redemption", check)
    result = apply_verb(e.combat, "heal", {"target": m, "amount": 1})
    assert seen == [(11, 1, 1)]
    assert result["actual_heal"] == 1 and result["redemption"]["type"] == "redemption"
    assert m.departure_reason == "救赎"


def test_blood_limit_cost_completes_callback_and_ledger_before_redemption(monkeypatch):
    e = _ready_combat(_engine("blood_cost_ledger"))
    m = monster("血限代价")
    m.current_hp = 10
    e.state.enemies[:] = [m]
    seen = []
    monkeypatch.setattr(e.combat, "_on_cost_paid", lambda who, ctx: seen.append(
        (who.blood_limit, who.current_hp, who.reversible_cost_layers["血限"],
         bool(e.state.pending_redemption))))
    result = e.combat.pay_numeric_cost(m, "血限", 1)
    assert seen == [(99, 10, 1, False)]
    assert result["owner"]["paid"] == 1 and result["owner"]["redemption"]["type"] == "redemption"
    assert m.departure_reason == "救赎"


def test_shared_life_cost_completes_all_parts_before_redemption(monkeypatch):
    from engine.models import StatusEffect
    e = _ready_combat(_engine("shared_cost_order"))
    a, b = monster("共罪甲"), monster("共罪乙")
    a.current_hp = b.current_hp = 21
    e.state.enemies[:] = [a, b]
    ids = [a.runtime_id, b.runtime_id]
    for entity in (a, b):
        entity.add_status(StatusEffect("共罪", remaining_rounds=2, effect_data={"group_ids": ids}))
    callbacks = []
    monkeypatch.setattr(e.combat, "_on_cost_paid", lambda who, ctx: callbacks.append(
        (who.name, a.current_hp, b.current_hp, bool(e.state.pending_redemption))))
    result = e.combat.pay_numeric_cost(a, "生命", 2)
    assert callbacks == [("共罪甲", 20, 21, False), ("共罪乙", 20, 20, False)]
    assert result["actual_paid"] == 2
    assert a.departure_reason == "救赎" and e.combat.is_redemption_pending(b)
    assert [event["type"] for event in result["redemptions"]] == ["redemption", "redemption_pending"]
    assert e.state.pending_redemption["runtime_id"] == ids[0]
    assert e.state.pending_redemption_candidates[0]["runtime_id"] == ids[1]


def test_heal_verb_cancer_precedes_redemption():
    from engine.mechanisms.verbs import apply_verb
    e = _ready_combat(_engine("heal_cancer_priority"))
    m = monster("癌变优先")
    m.current_hp = 10
    m.total_healed = 199
    e.state.enemies[:] = [m]
    result = apply_verb(e.combat, "heal", {"target": m, "amount": 1})
    assert result["actual_heal"] == 1 and result["cancer"]
    assert m.is_proliferated and not e.state.pending_redemption
    assert "redemption" not in result


def test_lethal_blood_limit_payment_has_death_priority():
    e = _ready_combat(_engine("lethal_blood_cost"))
    m = monster("致死血限")
    e.state.enemies[:] = [m]
    result = e.combat.pay_numeric_cost(m, "血限", 100)
    assert result["owner"]["paid"] == 100 and m.current_hp == 0 and not m.is_alive
    assert not m.is_departed and not e.state.pending_redemption


def test_compound_redemption_does_not_burn_shards_or_disable_relic():
    from engine.models import Relic
    e = _ready_combat(_engine("compound_resources_isolation"))
    m = monster("待决资源")
    m.mutation_count = 50
    m.shards = 12
    m.relics.append(Relic("测试遗物", ""))
    e.state.enemies[:] = [m]
    e.combat.apply_daowen_effect(
        "探针", {"x": 1, "mutation_reduction": 1, "shard_burn": 5,
                 "relic_disable": 1, "relic_names": ["测试遗物"]}, e.state.player, m)
    assert m.departure_reason == "救赎" and m.shards == 12
    assert m.relics[0].enabled


def test_merge_does_not_modify_or_replace_redeemed_participant():
    from engine.models import StatusEffect
    e = _ready_combat(_engine("merge_isolation"))
    caster = Entity("施法怪", "怪物", blood_limit=100, current_hp=100,
                    attack_count=1, attack_power=1)
    m = monster("合体候选")
    m.mutation_count = 50
    m.add_status(StatusEffect("化尸", remaining_rounds=2))
    creation = Entity("其他造物", "造物", blood_limit=100, current_hp=100,
                      attack_count=2, attack_power=3)
    e.state.enemies[:] = [caster, m, creation]
    result = e.combat.apply_daowen_effect(
        "合体", {"x": 2, "merge_creations": 2, "mutation_reduction": 1}, caster, m)
    assert m.departure_reason == "救赎" and m.blood_limit == 100
    assert creation.is_alive and creation.blood_limit == 100
    assert any(effect["type"] == "merge_interrupted" for effect in result["effects"])


def test_diaoling_skips_queued_target_but_executes_other_targets():
    e = _ready_combat(_engine("diaoling_queue_isolation"))
    first, queued, other = monster("先"), monster("候选"), monster("正常")
    e.state.enemies[:] = [first, queued, other]
    e.combat.check_redemption(first)
    e.combat.check_redemption(queued)
    effects = e.combat._execute_diaoling(e.state.player, [e.state.player], 20)
    assert queued.is_alive and e.combat.is_redemption_pending(queued)
    assert not other.is_alive and not other.is_departed
    assert any(effect.get("target") == other.name for effect in effects)


def test_bingbian_relief_updates_ledger_before_redemption(monkeypatch):
    from tests.test_task8_new_daowen import _player_with, monster as make_monster, cast
    e, combat, player = _player_with("病变")
    m = make_monster(e, hp=100, name="转换账本")
    m.blood_limit, m.current_hp, m.mutation_count = 95, 20, 49
    m.attack_count = m.attack_power = 1
    m.reversible_cost_layers["血限"] = 1
    real_check = combat.check_redemption
    observed = []
    def check(entity, **kwargs):
        result = real_check(entity, **kwargs)
        if result:
            observed.append((entity.reversible_cost_layers["血限"], entity.mutation_count))
        return result
    monkeypatch.setattr(combat, "check_redemption", check)
    cast(combat, player, "病变", 1, target=m, from_kind="血限", to_kind="异变")
    assert observed == [(0, 49)] and m.departure_reason == "救赎"


def test_compound_heal_redeems_before_following_mutation_and_shield():
    e = _ready_combat(_engine("compound_heal_boundary"))
    m = monster("回复后")
    m.current_hp = 10
    e.state.enemies[:] = [m]
    result = e.combat.apply_daowen_effect(
        "探针", {"x": 1, "target_heal": 1, "mutation_reduction": 1,
                 "target_shield": 5}, e.state.player, m)
    assert (m.current_hp, m.mutation_count, m.shield) == (11, 49, 0)
    assert m.departure_reason == "救赎"
    assert [effect["type"] for effect in result["effects"]][:2] == ["heal", "redemption"]


def test_immortality_converted_damage_heal_checks_redemption():
    from engine.models import StatusEffect
    e = _ready_combat(_engine("immortal_heal_redemption"))
    m = monster("永生回复")
    m.current_hp = 10
    m.add_status(StatusEffect("永生", remaining_rounds=2))
    e.state.enemies[:] = [m]
    result = e.combat._apply_hostile_damage(m, 1, source=e.state.player)
    assert result["converted_to_heal"] and result["heal"]["actual_heal"] == 1
    assert result["redemption"]["type"] == "redemption" and m.current_hp == 11


def test_consumable_heal_redeems_before_following_mutation():
    from engine.models import Consumable
    from engine.npc_consumables import consume_ordinary
    e = _ready_combat(_engine("item_heal_redemption"))
    m = monster("回复异变药")
    m.current_hp = 10
    e.state.enemies[:] = [m]
    item = Consumable("复合药", "恢复1生命，获得异变1", 1, 1)
    result = consume_ordinary(e.combat, item, m)
    assert result["result"]["redemption"]["type"] == "redemption"
    assert m.current_hp == 11 and m.mutation_count == 49 and item.current_uses == 0


def test_blood_pump_heal_finishes_shield_before_redemption():
    from engine.models import Consumable
    from engine.npc_consumables import consume_tool
    e = _ready_combat(_engine("blood_pump_heal"))
    m = monster("血泵怪")
    m.blood_limit, m.current_hp = 1000, 10
    e.state.enemies[:] = [m]
    item = Consumable("备用血泵", "", 1, 1)
    result = consume_tool(e.combat, item, {}, m, [e.state.player])
    assert m.current_hp == 30 and m.shield == 30 and m.departure_reason == "救赎"
    assert result["result"]["redemption"]["type"] == "redemption"


def test_chuyong_synchronizing_zero_hp_resolves_death_first():
    from engine.models import StatusEffect
    e = _ready_combat(_engine("chuyong_zero_hp"))
    caster = e.state.player
    caster.current_hp = 0
    m = monster("初拥命零")
    e.state.enemies[:] = [m]
    m.add_status(StatusEffect(name="初拥", remaining_rounds=2,
                              effect_data={"caster_id": caster.runtime_id}))
    assert e.combat._sync_chuyong(m)
    assert m.current_hp == 0 and not m.is_alive and not m.is_departed
    assert not e.state.pending_redemption


def test_relic_after_damage_heals_other_monster_then_redeems_it():
    from engine.models import Relic
    e = _ready_combat(_engine("relic_heal_redemption"))
    healer, victim = monster("血囊持有者"), monster("受伤者")
    healer.current_hp = 10
    healer.relics.append(Relic("噬伤血囊", ""))
    victim.current_hp, victim.mutation_count = 100, 50
    e.state.enemies[:] = [healer, victim]
    e.combat._apply_hostile_damage(victim, 5, source=e.state.player)
    assert healer.current_hp == 11 and healer.departure_reason == "救赎"
    assert victim.current_hp == 95 and victim.is_alive
    assert e.state.pending_redemption["runtime_id"] == healer.runtime_id


def test_corpse_borrow_one_blood_checks_after_status():
    e = _ready_combat(_engine("corpse_borrow_one_blood"))
    m = monster("借尸者")
    m.current_hp = 21
    corpse = Entity("尸体", "怪物", blood_limit=1, current_hp=0,
                    attack_count=1, attack_power=1, is_alive=False)
    e.state.dead_monsters = [corpse]
    e.state.enemies[:] = [m]
    result = e.combat.apply_daowen_effect("借尸", {"x": 1, "duration": 2,
        "borrow_corpse_name": "尸体"}, e.state.player, m)
    assert (m.current_hp, m.blood_limit) == (1, 1) and m.has_status("借尸")
    assert m.departure_reason == "救赎"
    assert any(effect.get("type") == "redemption" for effect in result["effects"])


def test_round_start_mutation_isolates_pending_target_and_continues_others():
    from engine.models import MONSTER_MANA_RELIC, Relic
    e = _ready_combat(_engine("round_start_queue_isolation"))
    a, b, c = monster("回始甲"), monster("回始乙"), monster("回始丙")
    c.current_hp = 80
    for m in (a, b, c):
        m.mana_limit = 1
        m.current_mana = 0
        m.mutation_count = 40
        m.relics.append(Relic(MONSTER_MANA_RELIC, ""))
        m.hp_lost_this_round = 7
        m.blood_oath_used_this_round = True
    e.state.enemies[:] = [a, b, c]
    e.combat.round_start()
    assert a.departure_reason == "救赎" and e.combat.is_redemption_pending(b)
    assert a.mutation_count == b.mutation_count == c.mutation_count == 41
    assert b.hp_lost_this_round == 7 and b.blood_oath_used_this_round
    assert not c.blood_oath_used_this_round
    assert c.is_alive
