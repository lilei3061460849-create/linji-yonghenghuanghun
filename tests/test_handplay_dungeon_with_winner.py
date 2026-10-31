"""Regression tests for real-winner tier-two handplay setup and rest contracts."""
from types import SimpleNamespace

import pytest

from engine.ai_player import PlaceholderBackend
from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance, Entity
from sim import handplay_dungeon_with_winner as handplay
from sim.handplay_dungeon_with_winner import (
    _choose_rest_plan,
    _prepare_before_battle,
    _resolve_monster_turn_hand,
    manual_player_turn,
    play_dungeon,
)


def _wave_phase_engine(tmp_path):
    engine = GameEngine(
        db_path=str(tmp_path / "wave-handplay.db"),
        save_dir=str(tmp_path / "saves"),
        sealed_candidate_path=str(tmp_path / "sealed.json"),
        death_book_path=str(tmp_path / "death.md"),
        rng_seed=7,
    )
    player = Entity("测试轮回者", "轮回者", blood_limit=10, current_hp=10,
                    mana_limit=10, current_mana=10, speed_limit=0, current_speed=0)
    killer = Entity("先手杀手", "怪物", blood_limit=100, current_hp=100,
                    attack_count=1, attack_power=50)
    wave_monster = Entity("血僵", "怪物", blood_limit=100, current_hp=100,
                          attack_count=1, attack_power=10)
    wave_monster.dao_wen["波及"] = DaoWenInstance(
        DaoWen(name="波及", formula="", cost_type="消耗",
               cost_formula="X", effect_formula=""),
        x_value=2,
    )
    engine.state.player = player
    engine.state.friends = []
    engine.state.employees = []
    engine.state.enemies = [killer, wave_monster]
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "player_actions"
    engine.state.pending_monster_phase = {}
    engine.state.current_round = 2
    return engine


def test_handplay_resolves_wave_before_an_earlier_actor_can_kill_its_targets(
        tmp_path, monkeypatch):
    engine = _wave_phase_engine(tmp_path)
    monkeypatch.setattr(handplay, "choose_dodge", lambda *args, **kwargs: False)

    log = []
    result = _resolve_monster_turn_hand(engine, log)

    assert result["success"], result
    # 交替出手：一步只结算一个行动者，波及必须先手落地（其标记目标仍全部存活）。
    details = result["result"]["details"]
    wave = next(entry for entry in details if entry.get("daowen_activated") == "波及")
    assert wave["execution"]["wave_marked"] == ["测试轮回者", "先手杀手"]
    assert details.index(wave) == 0
    assert not any(entry.get("attacker") == "先手杀手" for entry in details)

    # 轮回者跳过优先权，敌方再拿走下一个行动者：先手杀手正常击杀（合法战败）。
    assert engine.execute_action("skip", {})["success"]
    second = _resolve_monster_turn_hand(engine, log)
    assert second["success"], second
    assert second["result"]["player_dead"] is True


def test_placeholder_ai_orders_wave_before_target_killing_actor(tmp_path):
    engine = _wave_phase_engine(tmp_path)
    prepared = engine.execute_action("prepare_monster_phase", {})
    assert prepared["success"], prepared

    decision = PlaceholderBackend().decide(
        {"state": engine.state.to_dict()},
        {"action_type": "resolve_monster_phase", **prepared["result"]},
    )
    choices = decision.params["choices"]
    # 交替出手：每次只提交1个行动者；波及使用者排在最前。
    assert [choice["actor_ref"] for choice in choices] == ["enemy:1"]
    assert len(choices[0]["daowen"]["dodge_targets"]) == 2
    assert choices[0]["attack_actions"] == []  # 用道纹就不能再普攻
    result = engine.execute_action(decision.action_type, decision.params)
    assert result["success"], result
    assert result["result"]["player_dead"] is False

    # 轮回者跳过 → 敌方下一个行动者（先手杀手）击杀轮回者，合法战败。
    assert engine.execute_action("skip", {})["success"]
    prepared = engine.execute_action("prepare_monster_phase", {})
    assert prepared["success"], prepared
    decision = PlaceholderBackend().decide(
        {"state": engine.state.to_dict()},
        {"action_type": "resolve_monster_phase", **prepared["result"]},
    )
    assert [c["actor_ref"] for c in decision.params["choices"]] == ["enemy:0"]
    result = engine.execute_action(decision.action_type, decision.params)
    assert result["success"], result
    assert result["result"]["player_dead"] is True


def test_handplay_fallback_resubmits_wave_dodge_targets(tmp_path, monkeypatch):
    engine = _wave_phase_engine(tmp_path)
    monkeypatch.setattr(handplay, "choose_dodge", lambda *args, **kwargs: False)
    original_execute = engine.execute_action
    resolve_submissions = []

    def reject_first_resolve(action, params):
        if action == "resolve_monster_phase":
            resolve_submissions.append(params)
            if len(resolve_submissions) == 1:
                return {"success": False, "error": "deliberate first-submit rejection"}
        return original_execute(action, params)

    monkeypatch.setattr(engine, "execute_action", reject_first_resolve)
    log = []
    result = _resolve_monster_turn_hand(engine, log)

    assert result["success"], result
    assert len(resolve_submissions) == 2
    retry_choices = resolve_submissions[1]["choices"]
    assert retry_choices[0]["actor_ref"] == "enemy:1"  # 波及在先手行动，避免快照目标先死亡
    wave_dao = retry_choices[0]["daowen"]
    assert len(wave_dao["dodge_targets"]) == 2
    assert retry_choices[0]["attack_actions"] == []
    assert "首提被拒" in log[0]


def test_rest_plan_matches_current_blood_limit_percentages_and_budget():
    # A real generated winner is blood-limit 66, HP 18, with 22 shards. Tier 3
    # would heal 40 but costs 25, so two affordable tier-2 rests fill the 48-HP gap.
    assert _choose_rest_plan(blood_limit=66, current_hp=18, shards=22) == (2, 27)
    assert _choose_rest_plan(blood_limit=66, current_hp=45, shards=12) == (2, 27)
    # Free tier 1 remains legal even while in shard debt.
    assert _choose_rest_plan(blood_limit=66, current_hp=58, shards=-5) == (1, 14)
    assert _choose_rest_plan(blood_limit=66, current_hp=30, shards=35) == (3, 40)


def test_rejected_rest_fails_fast_without_replaying_the_same_action():
    player = SimpleNamespace(blood_limit=66, current_hp=18, dao_wen={})
    state = SimpleNamespace(player=player, energy=3, shards=22)

    class RejectingEngine:
        def __init__(self):
            self.state = state
            self.calls = []

        def execute_action(self, action, params):
            self.calls.append((action, params))
            return {"success": False, "error": "deliberate test rejection"}

    engine = RejectingEngine()
    with pytest.raises(RuntimeError, match="局外休整2档失败"):
        _prepare_before_battle(engine)

    assert len(engine.calls) == 1
    assert engine.calls[0][1]["heal_allocations"] == [
        {"target_ref": "player:0", "amount": 27}
    ]


def test_real_winner_without_kill_daowen_uses_two_phase_basic_attack():
    player = SimpleNamespace(
        is_alive=True, action_count=1, current_hp=60, blood_limit=60,
        current_mana=4, shield=0, dao_wen={},
        effective_attack_power=lambda: 4,
    )
    target = SimpleNamespace(
        is_alive=True, name="test target", current_hp=100,
        attack_count=0, attack_power=0,
    )
    target_option = {
        "ref": "enemy:0", "spell_options": {"before": [], "after": []},
    }

    class BasicAttackEngine:
        def __init__(self):
            self.state = SimpleNamespace(player=player, enemies=[target])
            self.combat = SimpleNamespace(
                _combat_entity_refs=lambda: {"player:0": player, "enemy:0": target}
            )

        def execute_action(self, action, params):
            if action == "prepare_attack":
                return {"success": True, "result": {
                    "token": "attack-token", "hit_count": 2,
                    "target_options": [target_option],
                }}
            assert action == "resolve_attack"
            assert params["token"] == "attack-token"
            assert [hit["damage"] for hit in params["hits"]] == [4, 4]
            assert all(hit["target_ref"] == "enemy:0" for hit in params["hits"])
            target.current_hp -= 8
            return {"success": True, "result": {
                "hits": [{"damage_dealt": 4}, {"damage_dealt": 4}],
            }}

    log = []
    result = manual_player_turn(BasicAttackEngine(), log)

    assert len(result) == 1
    assert target.current_hp == 92
    assert any("普攻2击" in line for line in log)


def test_tier_two_loader_rejects_legacy_candidates_wrapper_before_setup(tmp_path):
    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_text('{"candidates": {"2": []}}', encoding="utf-8")

    with pytest.raises(ValueError, match="candidates wrapper"):
        play_dungeon(str(legacy_path), battles=1, seed=7)
