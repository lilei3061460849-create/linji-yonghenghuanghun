"""交替出手的优先权闸门：普通战斗严格拒绝，养蛊场（观众不参战）不受限。

本文件锁住两条契约，防止为了修夹具而放宽生产校验：

1. 普通战斗里，轮回者持有优先权时 `prepare_monster_phase` 必须被拒；
   多人提交（一次提交多个行动者）在任何模式下都必须被拒。
2. 养蛊场（`state.arena_ffa`）玩家侧是观众、不产生优先权，故怪物阶段可以直接
   准备；这不是「放宽闸门」，而是该模式下没有对手出手可交替。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import Entity
from tests.setup_support import finish_initial_daowen, hand_priority_to_enemy


def _battle_engine(tmp_path) -> GameEngine:
    engine = GameEngine(
        db_path=str(tmp_path / "gate.db"),
        save_dir=str(tmp_path / "saves"),
        sealed_candidate_path=str(tmp_path / "sealed.json"),
        death_book_path=str(tmp_path / "death.md"),
        rng_seed=3,
    )
    assert engine.execute_action("setup_attributes", {
        "name": "闸门测试", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })["success"]
    assert finish_initial_daowen(engine)["success"]
    assert engine.execute_action("setup_choose_resonance",
                                 {"resonance_type": "反转"})["success"]
    assert engine.execute_action("setup_choose_region", {"region": "龙心谷"})["success"]
    return engine


def _single_monster(engine: GameEngine) -> Entity:
    engine.state.player = Entity("轮回者", "轮回者", blood_limit=60, current_hp=60,
                                 mana_limit=10, current_mana=10, speed_limit=3,
                                 current_speed=3, attack_count=1, attack_power=3)
    engine.state.friends = []
    engine.state.employees = []
    monster = Entity("靶怪", "怪物", blood_limit=40, current_hp=40,
                     attack_count=1, attack_power=2)
    engine.state.enemies = [monster]
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "player_actions"
    engine.state.pending_monster_phase = {}
    engine.state.current_round = 2
    engine.state.duel_turn = "player_side"
    return monster


def test_prepare_monster_phase_requires_enemy_priority(tmp_path):
    """轮回者持有优先权时准备怪物行动必须被拒（生产校验不得为了让测试通过而放宽）。"""
    engine = _battle_engine(tmp_path)
    _single_monster(engine)
    rejected = engine.execute_action("prepare_monster_phase", {})
    assert rejected["success"] is False
    assert "交替出手" in rejected["error"], rejected
    assert engine.state.pending_monster_phase == {}

    # 交出优先权后才允许准备——用规则内的优先级声明，不绕过校验
    hand_priority_to_enemy(engine)
    accepted = engine.execute_action("prepare_monster_phase", {})
    assert accepted["success"], accepted


def test_multi_actor_submission_is_rejected_in_normal_battle(tmp_path):
    """一次提交多个行动者在普通战斗里同样被拒（与死斗/养蛊场同一契约）。"""
    engine = _battle_engine(tmp_path)
    _single_monster(engine)
    engine.state.enemies.append(Entity("另一只怪", "怪物", blood_limit=40, current_hp=40,
                                       attack_count=1, attack_power=2))
    hand_priority_to_enemy(engine)
    prepared = engine.execute_action("prepare_monster_phase", {})
    assert prepared["success"], prepared

    choices = []
    for actor in prepared["result"]["actors"]:
        target_ref = actor["attack_target_options"][0]["ref"]
        target_option = next(t for t in actor["attack_target_options"]
                             if t["ref"] == target_ref)
        hits = [{"target_ref": target_ref, "dodge": False, "blood_shadow": False,
                 "spell_choices": {timing: {sp["spell_name"]: {"use": False}
                                            for sp in target_option.get("spell_options", {}).get(timing, [])}
                                   for timing in ("before", "after")}}
                for _ in range(actor["base_hits_per_attack"])]
        choices.append({"actor_ref": actor["actor_ref"], "daowen": None,
                        "attack_actions": [{"hits": hits} for _ in range(actor["base_attack_actions"])]})
    result = engine.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"], "choices": choices})
    assert result["success"] is False
    assert "只能提交1个" in result["error"], result


def test_ffa_spectator_holds_no_priority():
    """养蛊场：玩家侧是观众，不产生优先权；怪物阶段可以直接准备。"""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "sim"))
    import monster_arena as arena
    from tests.test_arena_ffa import _lineup

    engine = arena.build_arena(_lineup(2))
    assert engine.state.arena_ffa is True
    assert engine.combat.side_can_act("player_side") is False, "观众不该产生优先权"
    assert engine.combat.side_can_act("opponent_side") is True
    prepared = engine.execute_action("prepare_monster_phase", {})
    assert prepared["success"], prepared
