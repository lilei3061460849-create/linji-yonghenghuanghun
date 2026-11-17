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
