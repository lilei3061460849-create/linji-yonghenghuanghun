"""怪物阶段事务一致性回归（2026-08-19 P0）。

目标：任何非法 resolve 输入不得留下任何战斗副作用。
覆盖：非法 hits / 非法 attack schema / 非法 dodge / 非法·重复使用本回合道纹；
合法提交行为完全不变。非法提交前后 round_used、道纹层数、资源、HP、事件流逐项一致。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.combat import CombatEngine
from engine.combat_events import CombatEventType
from engine.dice import DiceEngine
from engine.models import DaoWen, DaoWenInstance, Entity
from tests.setup_support import finish_initial_daowen, hand_priority_to_enemy
from tests.monster_phase_support import resolve_monster_phase as advance_round1


def _arena(seed: int = 1):
    """罪孽都市（烁金是罪孽专属）开战并推进到第 2 回合，怪物持有 烁金。"""
    import tempfile
    save_dir = tempfile.mkdtemp(prefix="txn")
    e = GameEngine(db_path=os.path.join(save_dir, "g.db"), rng_seed=seed,
                   save_dir=save_dir)
    e.execute_action("setup_attributes", {"name": "L", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "罪孽都市"})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    e.state.energy = 0
    assert e.execute_action("battle_start", {"relic_choices": {}})["success"]
    # 2026-10-12 出怪下界 1→2：第1场现在可能抽出 N=2。本文件测的是**事务回滚**，
    # 需要一个可控的单怪舞台（快照 _snapshot 只盯 enemies[0]，事件流断言也按单怪写），
    # 因此把多余的怪与增援队列清掉。多怪提交/结算的覆盖在别的文件里
    # （test_arena_ffa / test_action_budget / monster_phase_support 等），本文件不重复。
    e.state.enemies[:] = e.state.enemies[:1]
    e.state.monster_reinforcements = []
    m = e.state.enemies[0]
    # 2026-10-13：遗物池删2件后首怪漂移（狙击手atk1→赌鬼atk2），本文件“2次命中非法”的
    # 前提被打破；事务回滚与具体怪无关，钉死 attack_count=1 恢复非法前提。
    m.attack_count = 1
    m.shards = 100
    # 第 1 回合先让怪物空手（无道纹则 prepare 不列选项、daowen 提交 null），
    # 免得首回合被迫发动道纹污染 activated 基线；烁金在第 1 回合结束后再加。
    m.dao_wen.clear()
    # 第 1 回合走完整两阶段 API：2026-09-15 删除白板后，首回合怪物也要交一个道纹，
    # 这里选一个非【烁金】的选项（保住烁金 X=2，供第 2 回合断言吸金 10X=20）。
    assert e.execute_action("round_start", {"relic_choices": {}})["success"]
    hand_priority_to_enemy(e)  # 交替出手：声明轮回者本回合已交出优先权
    prep1 = e.execute_action("prepare_monster_phase", {})
    assert prep1["success"], prep1
    from tests.monster_phase_support import _decline_spells as _ds
    choices1 = []
    for a in prep1["result"]["actors"]:
        tgt = a["attack_target_options"][0]["ref"]
        to = next(t for t in a["attack_target_options"] if t["ref"] == tgt)
        hits = [{"target_ref": tgt, "dodge": False, "blood_shadow": False,
                 "spell_choices": _ds(to)} for _ in range(a["base_hits_per_attack"])]
        assert a["daowen_required"] is False, "第1回合怪物无道纹，不得要求提交"
        choices1.append({"actor_ref": a["actor_ref"], "daowen": None,
                         "attack_actions": [{"hits": hits}
                                            for _ in range(a["base_attack_actions"])]})
    assert e.execute_action("resolve_monster_phase",
                            {"token": prep1["result"]["token"],
                             "choices": choices1})["success"]
    assert e.execute_action("round_end", {})["success"]
    # 第 2 回合：怪物获得烁金，可发动道纹
    m.dao_wen["烁金"] = DaoWenInstance(
        DaoWen(name="烁金", formula="", cost_type="", cost_formula="", effect_formula=""),
        x_value=2)
    assert e.execute_action("round_start", {"relic_choices": {}})["success"]
    hand_priority_to_enemy(e)
    prep = e.execute_action("prepare_monster_phase", {})
    assert prep["success"], prep
    return e, m, prep


def _snapshot(e):
    """记录可对比的战斗状态快照（事件流只保留类型序列，timestamp 忽略）。

    注意：失败回滚后实体对象会被快照副本替换，必须从 e.state 重读，
    不能持有失败前的旧引用。
    """
    m = e.state.enemies[0]
    return {
        "round_used": sorted(e.combat._monster_round_used(m)),
        "activated": sorted(e.combat._monster_activated.get(id(m), set())),
        "x": {k: v.x_value for k, v in m.dao_wen.items()},
        "monster_shards": m.shards,
        "player_shards": e.state.shards,
        "player_hp": e.state.player.current_hp,
        "player_mana": e.state.player.current_mana,
        "player_speed": e.state.player.current_speed,
        "player_shield": e.state.player.shield,
        "monster_actions_used": m.actions_used_this_round,
        "events": [(ev.event_type.value, ev.actor_name, ev.target_name)
                   for ev in e.state.combat_events],
    }


def _actors(prep):
    return {a["actor_ref"]: a for a in prep["result"]["actors"]}


def _decline_spells(option):
    return {
        timing: {sp["spell_name"]: {"use": False}
                 for sp in option.get("spell_options", {}).get(timing, [])}
        for timing in ("before", "after")
    }


def _legal_hits(engine, prep, actor):
    """按 prepare 快照构造合法攻击（hits 数 = 道纹执行后 attack_count，变形除外）。"""
    a = _actors(prep)[actor]
    hits_per = max(0, engine.state.enemies[int(actor.split(":", 1)[1])].attack_count
                   - engine.state.enemies[int(actor.split(":", 1)[1])].get_status_value("手雷减攻"))
    target_ref = a["attack_target_options"][0]["ref"]
    target_option = next(t for t in a["attack_target_options"] if t["ref"] == target_ref)
    return [{"hits": [{"target_ref": target_ref, "dodge": False, "blood_shadow": False,
                       "spell_choices": _decline_spells(target_option)}
                      for _ in range(hits_per)]}
            for _ in range(a["base_attack_actions"])]


def test_illegal_hits_count_rolls_back_all_side_effects():
    """非法 hits（数量不足）：道纹已执行（烁金夺碎片）后动态校验失败 → 必须整体回滚。"""
    e, m, prep = _arena()
    actor = next(iter(_actors(prep)))
    before = _snapshot(e)

    # 合法道纹 + 攻击只提交 1 次命中（schema 要求 2 次以上）→ 执行阶段动态失败
    choice = {
        "actor_ref": actor,
        "daowen": {"name": "烁金", "dodge": False, "blood_shadow": False,
                   "trigger_spell_choices": {}, "target_ref": "player:0"},
        "attack_actions": [{"hits": [{"target_ref": "player:0", "dodge": False,
                                      "blood_shadow": False,
                                      "spell_choices": {"before": {}, "after": {}}},
                                     {"target_ref": "player:0", "dodge": False,
                                      "blood_shadow": False,
                                      "spell_choices": {"before": {}, "after": {}}}]}],
    }
    with pytest.raises(ValueError):
        e.combat.resolve_monster_phase([choice], prep["result"])

    after = _snapshot(e)
    assert after == before, f"非法 hits 必须零副作用:\n{before}\n{after}"


def test_illegal_attack_actions_count_no_side_effects():
    """非法 attack schema（attack_actions 数量错误）：静态校验执行前拦截，道纹不得执行。"""
    e, m, prep = _arena()
    actor = next(iter(_actors(prep)))
    before = _snapshot(e)

    legal_hits = _legal_hits(e, prep, actor)
    # 故意少提交一个 attack_action（合法数量 +1）
    choice = {
        "actor_ref": actor,
        "daowen": {"name": "烁金", "dodge": False, "blood_shadow": False,
                   "trigger_spell_choices": {}, "target_ref": "player:0"},
        "attack_actions": legal_hits + legal_hits[:1],
    }
    with pytest.raises(ValueError):
        e.combat.resolve_monster_phase([choice], prep["result"])

    after = _snapshot(e)
    assert after == before, "非法 attack_actions 数量必须零副作用"


def test_illegal_dodge_no_speed_rolls_back():
    """非法 dodge（目标速度 0 仍提交闪避）：执行阶段动态校验失败 → 整体回滚。"""
    e, m, prep = _arena()
    actor = next(iter(_actors(prep)))
    m.dao_wen.pop("必中", None)   # 移除必中，让 dodge 的速度校验真正生效
    assert e.combat.bizhong_remaining(m) == 0
    e.state.player.current_speed = 0
    before = _snapshot(e)

    legal_hits = _legal_hits(e, prep, actor)
    # 把第一个 hit 改成 dodge=True（速度 0 → 非法）
    legal_hits[0]["hits"][0]["dodge"] = True
    choice = {
        "actor_ref": actor,
        "daowen": {"name": "烁金", "dodge": False, "blood_shadow": False,
                   "trigger_spell_choices": {}, "target_ref": "player:0"},
        "attack_actions": legal_hits,
    }
    with pytest.raises(ValueError):
        e.combat.resolve_monster_phase([choice], prep["result"])

    after = _snapshot(e)
    assert after == before, "非法 dodge 必须零副作用（含速度被回滚为 0）"


def test_illegal_reused_daowen_this_round_no_side_effects():
    """非法/重复使用本回合道纹：round_used 已含该道纹仍提交 → 静态校验拦截，零副作用。"""
    e, m, prep = _arena()
    actor = next(iter(_actors(prep)))
    # 模拟本回合已用过 烁金（引擎记录 round_used）
    e.combat._monster_round_used(m).add("烁金")
    before = _snapshot(e)

    choice = {
        "actor_ref": actor,
        "daowen": {"name": "烁金", "dodge": False, "blood_shadow": False,
                   "trigger_spell_choices": {}, "target_ref": "player:0"},
        "attack_actions": _legal_hits(e, prep, actor),
    }
    with pytest.raises(ValueError):
        e.combat.resolve_monster_phase([choice], prep["result"])

    after = _snapshot(e)
    assert after == before, "重复使用本回合道纹必须零副作用"


def test_api_level_failure_leaves_no_side_effects():
    """API 层（execute_action）非法提交：返回 success=False，战斗状态零副作用。"""
    e, m, prep = _arena()
    actor = next(iter(_actors(prep)))
    before = _snapshot(e)

    choice = {
        "actor_ref": actor,
        "daowen": {"name": "烁金", "dodge": False, "blood_shadow": False,
                   "trigger_spell_choices": {}, "target_ref": "player:0"},
        "attack_actions": [{"hits": [{"target_ref": "player:0", "dodge": False,
                                      "blood_shadow": False,
                                      "spell_choices": {"before": {}, "after": {}}},
                                     {"target_ref": "player:0", "dodge": False,
                                      "blood_shadow": False,
                                      "spell_choices": {"before": {}, "after": {}}}]}],
    }
    res = e.execute_action("resolve_monster_phase",
                           {"token": prep["result"]["token"], "choices": [choice]})
    assert res["success"] is False, "非法 hits 必须被拒绝"

    after = _snapshot(e)
    assert after == before, "API 层非法提交必须零副作用"


def test_legal_submission_behavior_unchanged():
    """合法提交行为完全不变：道纹生效、攻击结算、事件流正常。"""
    e, m, prep = _arena()
    actor = next(iter(_actors(prep)))
    e.state.shards = 100
    m.shards = 100

    # 交替出手：一次提交只承载一个行动，道纹与攻击必须分两次提交。
    # 本用例要观察两份结算，故按通用规则用【蓄锐·增】把怪物出手预算补到 2。
    from tests.setup_support import grant_extra_actions
    grant_extra_actions(m, 1)

    dao_choice = {
        "actor_ref": actor,
        "daowen": {"name": "烁金", "dodge": False, "blood_shadow": False,
                   "trigger_spell_choices": {}, "target_ref": "player:0"},
        "attack_actions": [],
    }
    e.combat.resolve_monster_phase([dao_choice], prep["result"])

    # 道纹已激活：round_used 含 烁金，X 递增（+2×副本阶级）
    assert "烁金" in e.combat._monster_round_used(m)
    # 烁金X=2 → 烧 10X=20 碎片：玩家 100→80，怪物碎片不变（销毁，不入账）
    assert m.shards == 100, f"烁金只烧目标碎片，施法者不入账: {m.shards}"
    assert e.state.shards == 80

    # 第二手：重新 prepare 后只提交攻击组
    prep2 = e.combat.prepare_monster_phase()
    attack_choice = {"actor_ref": actor, "daowen": None,
                     "attack_actions": _legal_hits(e, {"result": prep2}, actor)}
    results = e.combat.resolve_monster_phase([attack_choice], prep2)
    attack_rows = [r for r in results if r.get("attacker") == m.name
                   and "damage_dealt" in r]
    assert attack_rows, "合法提交必须结算攻击"
    # 事件流有伤害事件
    assert any(ev.event_type == CombatEventType.DAMAGE_APPLIED
               for ev in e.state.combat_events)
