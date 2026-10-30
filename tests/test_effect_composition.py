"""组合压力测试（Phase 7）：不是 A、B、C 各自单独跑，而是把它们的**组合**跑起来。

需求原话：「A+B、A+C、B+C、A+B+C、A+B+C+D 都要覆盖，断言不崩、不死循环、
不污染、不留非法实体、不留悬垂引用、不出现指向不存在实体的 entity-id 运行态、
不出现无法解释的状态变化」。

本文件的结构：

* `PIECES`：A/B/C/D… 与实际道纹的固定映射（A=杀伐 伤害，B=再生 治疗，C=庇护 护盾，
  D=连击 失去生命，E=分裂，F=坠落）；
* `assert_battle_invariants()`：把上面那串「不许」写成一处可复用的断言；
* 组合矩阵：两两、三连、四连（顺序按字母，X=1，给足资源保证效果真的落地）；
* 语义组合：伤害+治疗 / 伤害+护盾 / 死亡+复活 / 死亡+分裂 / 失去生命+再生 /
  失去生命+连击 / 连击+再生 / 进化+触发 / 分裂+触发 / 多个触发连续；
* 预演组合：整段组合预演一遍，再正式执行，与对照组逐字段比对。

注意：本文件是**测试**，不含任何规则改动；为了让组合真的发生，harness 会给足
法力/出手（`_refill`），这属于测试前提，不是引擎行为。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine                                       # noqa: E402
from engine.combat_events import CombatEventType                        # noqa: E402
from engine.mechanisms import Mechanism, Trigger                        # noqa: E402
from engine.models import DaoWen, DaoWenInstance, Entity                # noqa: E402
from engine.resolution import KIND_DEATH, KIND_EVOLVE, KIND_HEAL, KIND_SPLIT  # noqa: E402
from engine.pollution_guard import (                                    # noqa: E402
    assert_runtime_unchanged, snapshot_runtime_state,
)
from tests.preview_support import build_battle_engine                   # noqa: E402

#: 组合用的「积木」：字母 → 道纹名（与需求里的 A/B/C/D 一一对应）。
PIECES = {
    "A": "杀伐",   # 伤害
    "B": "再生",   # 治疗
    "C": "庇护",   # 护盾
    "D": "连击",   # 失去生命
    "E": "分裂",   # 分裂
    "F": "坠落",   # 压制飞行（打断类效果）
}
_TARGET_SELF = {"B", "C"}          # 只对自己生效的积木
_VOLATILE = {"runtime_id", "game_id", "token", "timestamp", "event_id",
             "parent_event_id"}


# ---------------------------------------------------------------- harness

def _grant(engine, *names):
    """直接授予道纹（测试前提）：与存档读取路径同构，不改任何规则数值。"""
    for name in names:
        if name not in engine.state.player.dao_wen:
            engine.state.player.dao_wen[name] = DaoWenInstance(
                DaoWen(name=name, formula=f"{name}X", cost_type="消耗",
                       cost_formula="X", effect_formula=""))


def _refill(engine):
    """给足资源，保证组合里的每一步都能真的发生（并保证出手预算不成为噪声）。"""
    player = engine.state.player
    player.current_hp = max(player.current_hp, player.blood_limit)
    player.current_mana = max(player.current_mana, player.mana_limit, 10)
    player.current_speed = max(player.current_speed, player.speed_limit)
    engine.state.energy = max(engine.state.energy, 3)


def _target_for(engine, piece: str):
    if piece in _TARGET_SELF:
        return engine.state.player.name
    return next((e.name for e in engine.state.enemies if e.is_alive), None)


def _cast(engine, piece: str, x: int = 1):
    name = PIECES[piece]
    _grant(engine, name)
    target = _target_for(engine, piece)
    params = {"daowen_name": name, "x": x}
    if target:
        params["target"] = target
    result = engine.execute_action("use_daowen", params)
    if result.get("success") is False and "出手" in str(result.get("error", "")):
        # 出手预算用完：开一个新回合（回合是规则的一部分，不是绕过）
        engine.combat.round_start({})
        _refill(engine)
        result = engine.execute_action("use_daowen", params)
    return result


def _run_sequence(engine, sequence):
    results = []
    for piece in sequence:
        _refill(engine)
        results.append((piece, _cast(engine, piece)))
    return results


def assert_battle_invariants(engine, *, context: str = ""):
    """所有组合之后都必须成立的硬约束（需求里的「不许」清单）。"""
    tag = f"[{context}] " if context else ""
    state, combat = engine.state, engine.combat
    entities = [state.player] if state.player else []
    entities += list(state.enemies) + list(getattr(state, "temp_friends", []) or [])

    for entity in entities:
        assert isinstance(entity, Entity), f"{tag}非法实体：{entity!r}"
        assert entity.current_hp >= 0, f"{tag}{entity.name} 出现负血 {entity.current_hp}"
        assert entity.shield >= 0, f"{tag}{entity.name} 出现负护盾 {entity.shield}"
        if entity.is_alive:
            assert entity.current_hp > 0 or entity.is_departed, \
                f"{tag}{entity.name} 活着却 0 血"
        assert entity.name, f"{tag}存在无名实体"
        assert entity.dao_wen is not None and entity.status_effects is not None

    ids = [id(e) for e in entities]
    assert len(ids) == len(set(ids)), f"{tag}同一实体被登记了多次"

    # entity-id 运行态：键必须指向当前战场上的实体（不留指向已消失实体的脏键）
    known = set(ids)
    for attr in ("_resonance_rewrites",):
        mapping = getattr(combat, attr, None) or {}
        for key in mapping:
            assert key in known, f"{tag}{attr} 指向不存在的实体 id={key}"

    # 结算上下文：不留深度、不留终止原因
    assert combat.resolution.depth == 0, f"{tag}结算帧未退出"
    assert combat.resolution.trip_reason == "", f"{tag}保险丝被触发：{combat.resolution.trip_reason}"
    assert combat._effect_chain_depth == 0, f"{tag}历史深度别名未归零"

    # 事件流是纯追加的观测数据，不应被改成非列表/被清空
    assert isinstance(state.combat_events, list), f"{tag}事件流被替换成非列表"


def _norm(obj):
    if isinstance(obj, dict):
        return {k: ("<volatile>" if k in _VOLATILE else _norm(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_norm(v) for v in obj]
    return obj


def _fingerprint(engine) -> str:
    return json.dumps(_norm(engine.state.to_dict()), ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------- 组合矩阵

MATRIX = [
    ("A", "B"),
    ("A", "C"),
    ("B", "C"),
    ("A", "D"),
    ("B", "D"),
    ("C", "D"),
    ("A", "B", "C"),
    ("A", "B", "D"),
    ("A", "C", "D"),
    ("B", "C", "D"),
    ("A", "B", "C", "D"),
]


@pytest.mark.parametrize("sequence", MATRIX, ids=lambda s: "+".join(s))
def test_composition_matrix_is_stable(tmp_path, sequence):
    engine = build_battle_engine(tmp_path, name="comb_" + "".join(sequence))
    results = _run_sequence(engine, sequence)
    failed = [(p, r.get("error")) for p, r in results if r.get("success") is False]
    assert not failed, f"组合 {'+'.join(sequence)} 中这些积木没能结算：{failed}"
    assert_battle_invariants(engine, context="+".join(sequence))


# ---------------------------------------------------------------- 语义组合

def test_damage_plus_heal_moves_hp_both_ways(tmp_path):
    engine = build_battle_engine(tmp_path, name="combo_dh")
    player = engine.state.player
    enemy = next(e for e in engine.state.enemies if e.is_alive)
    _refill(engine)
    hp_before = enemy.current_hp
    _cast(engine, "A")                                     # 伤害
    assert enemy.current_hp < hp_before, "伤害没有落地"
    player.current_hp = max(1, player.current_hp - 5)
    hurt = player.current_hp
    _cast(engine, "B")                                     # 治疗
    assert player.current_hp >= hurt, "治疗没有落地"
    assert_battle_invariants(engine, context="伤害+治疗")


def test_damage_plus_shield_keeps_shield_legal(tmp_path):
    engine = build_battle_engine(tmp_path, name="combo_ds")
    _refill(engine)
    _cast(engine, "C")                                     # 护盾
    shield_after = engine.state.player.shield
    assert shield_after >= 0
    enemy = next(e for e in engine.state.enemies if e.is_alive)
    hp = enemy.current_hp
    _cast(engine, "A")                                     # 伤害
    assert enemy.current_hp <= hp, "伤害不该加血"
    assert_battle_invariants(engine, context="伤害+护盾")


def test_death_plus_revive_seal_reentry(tmp_path):
    """死亡+复活：同一场里一边死人（造物被击杀），一边有怪物回场（封印）。

    「复活」在本作里不是无代价起死回生，而是【封印】的延迟回场：目标必须
    活着且在场（`daowen_effect.py` 的公开路径有校验），暂离后按 X 回合再入场。
    因此这个组合用**两只**怪来构造：一只走死亡，一只走暂离→回场。
    """
    engine = build_battle_engine(tmp_path, name="combo_death_revive")
    combat = engine.combat
    ctx = combat.resolution
    ctx.set_tracing(True)
    monster = next(e for e in engine.state.enemies if e.is_alive)
    creation = combat._spawn_fenlie_creations(monster, 1)[0]
    combat.round_start({})
    assert_battle_invariants(engine, context="分裂后")

    # 死亡：击杀造物
    combat._apply_hostile_damage(creation, creation.current_hp + 1,
                                 source=engine.state.player)
    assert not creation.is_alive
    assert KIND_DEATH in [f.kind for f in ctx.history], "命零没有走结算帧"

    # 复活（回场）：把本体的下一次入场推迟 1 回合，再由回合始带回战场
    combat._delay_monster_reentry(monster, 1)
    result = combat.round_start({})
    assert any(e.get("type") == "seal_reentry" for e in result.get("effects", [])), \
        "封印回场没有发生"
    assert monster.is_alive and monster.current_hp > 0, "回场后应当是活的"
    assert monster in engine.state.enemies, "回场后必须重新在场"
    assert_battle_invariants(engine, context="死亡+复活")


def test_death_plus_split_keeps_creations_legal(tmp_path):
    """死亡+分裂：一次创造两个造物，击杀其中一个不影响另一个。"""
    engine = build_battle_engine(tmp_path, name="combo_split")
    combat = engine.combat
    combat.resolution.set_tracing(True)
    monster = next(e for e in engine.state.enemies if e.is_alive)
    creations = combat._spawn_fenlie_creations(monster, 2)
    assert KIND_SPLIT in [f.kind for f in combat.resolution.history], "分裂没有记账"
    assert len(creations) == 2
    combat.round_start({})
    combat._apply_hostile_damage(creations[0], creations[0].current_hp + 1,
                                 source=engine.state.player)
    assert not creations[0].is_alive and creations[1].is_alive
    assert_battle_invariants(engine, context="死亡+分裂")


def test_life_loss_plus_regen_and_blood_debt(tmp_path):
    """失去生命+再生、失去生命+连击、连击+再生：三条都以「失去生命」为起点。"""
    engine = build_battle_engine(tmp_path, name="combo_life")
    player = engine.state.player
    _grant(engine, "连击", "再生")
    _refill(engine)
    engine.combat.round_start({})
    _refill(engine)

    hp = player.current_hp
    assert _cast(engine, "D").get("success") is True      # 连击：失去生命
    assert player.current_hp < hp, "连击没有失去生命"
    assert_battle_invariants(engine, context="失去生命")

    _cast(engine, "B")                                     # 再生
    assert player.current_hp >= hp - 1
    assert_battle_invariants(engine, context="失去生命+再生")

    _refill(engine)
    hp2 = player.current_hp
    _cast(engine, "D")                                     # 再次失去生命
    _cast(engine, "B")                                     # 再接再生
    assert_battle_invariants(engine, context="连击+再生")


def test_evolution_plus_trigger(tmp_path):
    """进化+触发：怪物困境 → 进化（原初X）→ 其后的触发照常结算。"""
    engine = build_battle_engine(tmp_path, name="combo_evolve")
    combat = engine.combat
    combat.resolution.set_tracing(True)
    monster = next(e for e in engine.state.enemies if e.is_alive)
    _grant(engine, "杀伐")
    monster.current_hp = max(1, int(monster.blood_limit * 0.2))   # 制造「困境」
    assert combat.check_monster_difficulty(monster), "没有进入困境，测试前提不成立"
    result = combat.execute_evolution(monster, "杀伐", 1)
    assert isinstance(result, dict)
    assert result.get("success") is True, result.get("error")
    assert KIND_EVOLVE in [f.kind for f in combat.resolution.history], \
        "进化没有走结算帧"
    # 进化之后照常触发一次伤害，链必须完整退出
    combat._apply_hostile_damage(monster, 1, source=engine.state.player)
    assert_battle_invariants(engine, context="进化+触发")


def test_split_plus_trigger_fires_for_creations(tmp_path):
    """分裂+触发：新生成的造物必须能正常参与触发分发。"""
    engine = build_battle_engine(tmp_path, name="combo_split_trigger")
    combat = engine.combat
    fired: list[str] = []

    def on_damage(trigger_ctx, targets):
        fired.append(getattr(trigger_ctx.target, "name", "?"))
        return {"ok": True}

    mechanism = Mechanism(name="测试·分裂触发", when=Trigger.event(CombatEventType.DAMAGE_APPLIED),
                          effect=on_damage)
    combat.mechanism_bus.register(mechanism)
    try:
        monster = next(e for e in engine.state.enemies if e.is_alive)
        creations = combat._spawn_fenlie_creations(monster, 2)
        for creation in creations:
            combat._apply_hostile_damage(creation, 1, source=engine.state.player)
    finally:
        combat.mechanism_bus.unregister(mechanism)
    assert len(fired) >= len(creations), f"造物的伤害没有触发分发：{fired}"
    assert_battle_invariants(engine, context="分裂+触发")


def test_multiple_triggers_fire_in_priority_order(tmp_path):
    """多个 trigger 连续：全部触发，且顺序严格等于 priority 顺序（规则顺序）。"""
    engine = build_battle_engine(tmp_path, name="combo_triggers")
    combat = engine.combat
    order: list[str] = []

    def make(name, priority):
        return Mechanism(name=name, when=Trigger.event(CombatEventType.DAMAGE_APPLIED),
                         priority=priority,
                         effect=lambda trigger_ctx, targets, _n=name: order.append(_n) or {})

    mechanisms = [make("测试·C", 30), make("测试·A", 10), make("测试·B", 20)]
    for mechanism in mechanisms:
        combat.mechanism_bus.register(mechanism)
    try:
        enemy = next(e for e in engine.state.enemies if e.is_alive)
        combat._apply_hostile_damage(enemy, 1, source=engine.state.player)
    finally:
        for mechanism in mechanisms:
            combat.mechanism_bus.unregister(mechanism)
    assert order == ["测试·A", "测试·B", "测试·C"], order
    assert_battle_invariants(engine, context="多触发")


# ---------------------------------------------------------------- 预演 + 组合

def test_preview_of_full_combination_is_side_effect_free(tmp_path):
    """整段组合预演一遍：真实状态、战斗运行态、事件流都不许变。"""
    from engine.ai_preview import ActionPreview
    engine = build_battle_engine(tmp_path, name="combo_preview")
    _grant(engine, *PIECES.values())
    _refill(engine)
    before = snapshot_runtime_state(engine, "before_preview")
    for piece in ("A", "B", "C", "D"):
        target = _target_for(engine, piece)
        params = {"daowen_name": PIECES[piece], "x": 1}
        if target:
            params["target"] = target
        ActionPreview(engine).preview("use_daowen", params)
    assert_runtime_unchanged(before, snapshot_runtime_state(engine, "after_preview"),
                             context="组合预演")
    assert engine.combat.resolution.depth == 0 and engine.combat.resolution.trip_reason == ""


def test_preview_then_execute_combination_equals_control(tmp_path):
    """预演过整段组合之后，正式执行必须与「从没预演过」的对照组逐字段一致。"""
    from engine.ai_preview import ActionPreview
    a = build_battle_engine(tmp_path, name="combo_a")
    b = build_battle_engine(tmp_path, name="combo_b")
    for engine in (a, b):
        _grant(engine, *PIECES.values())
        _refill(engine)
    assert _fingerprint(a) == _fingerprint(b), "对照前提不成立：同种子状态不同"

    sequence = ("A", "B", "C", "D")
    for piece in sequence:                       # A 先预演整段
        target = _target_for(a, piece)
        params = {"daowen_name": PIECES[piece], "x": 1}
        if target:
            params["target"] = target
        ActionPreview(a).preview("use_daowen", params)

    _run_sequence(a, sequence)                   # 两边正式执行同一段组合
    _run_sequence(b, sequence)
    assert _fingerprint(a) == _fingerprint(b), "预演改变了正式执行的结果"
    assert_battle_invariants(a, context="预演+组合 A")
    assert_battle_invariants(b, context="预演+组合 B")


def test_engine_stays_usable_after_a_tripped_fuse_in_a_combination(tmp_path, monkeypatch):
    """组合里触发保险丝之后，引擎必须还能继续用（不留半死不活的状态）。"""
    from engine.resolution import ResolutionContext
    engine = build_battle_engine(tmp_path, name="combo_fuse")
    _grant(engine, *PIECES.values())
    _refill(engine)
    monkeypatch.setattr(ResolutionContext, "MAX_DEPTH", 2)
    enemy = next(e for e in engine.state.enemies if e.is_alive)
    result = engine.execute_action("use_daowen",
                                   {"daowen_name": "杀伐", "x": 1, "target": enemy.name})
    assert result.get("success") is False
    monkeypatch.setattr(ResolutionContext, "MAX_DEPTH", 64)
    engine.combat.round_start({})
    _refill(engine)
    assert engine.execute_action("use_daowen",
                                 {"daowen_name": "杀伐", "x": 1,
                                  "target": enemy.name}).get("success") is True
    assert_battle_invariants(engine, context="保险丝后恢复")
