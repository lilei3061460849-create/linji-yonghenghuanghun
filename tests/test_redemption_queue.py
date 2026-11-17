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
