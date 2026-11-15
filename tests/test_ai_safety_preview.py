"""TacticalAI 行动预演安全层回归（2026-08-19；2026-10-03 改用现役机制）。

原用例以「怪物持【爆裂】反噬致轮回者命零」为危险源；删除【爆裂】
（效果由通用遗物【千荆甲】承接，但千荆甲只能由轮回者持有，怪物不再有反噬），故本文件
曾改用【无神】（使[目标]选择目标时强制改为自身）作为危险源；**删除【无神】【借力】【衰败】**，轮回者再没有任何"道纹出手打自己"的路径，该危险源
随之消失。现改用第三类仍存的自灭源：**异变迷失**（消耗品叠加异变层数越过阈值）——
它同样是"玩家自己的一个动作把自己打死"，且与道纹无关，正好证明安全层不是道纹特判。
断言结构不变：预演必须判定死亡、TacticalAI 不得选择该行动、预演零副作用。

> 覆盖缺口（如实记录）：原"单体自灭 vs AOE 自灭都要被拦截"这一条已无法构造——
> 现役道纹里没有能让轮回者打自己的路径。安全层的**通用性**改由
> `test_risk_classify_is_action_agnostic` 从另一侧钉住（同一个分类器、不看动作名/道纹名）。

覆盖：预演后果提取（HP/命零/事件链）、预演零副作用、AOE 自灭拒绝、
单体自灭拒绝、安全动作行为不变。
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.ai_preview import ActionPreview
from engine.ai_tactics import TacticalAI
from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance, Entity, StatusEffect
from tests.setup_support import finish_initial_daowen


def _arena(region="扭曲都市"):
    e = GameEngine(db_path=os.path.join(tempfile.mkdtemp(prefix="prev"), "g.db"),
                   rng_seed=1, save_dir=tempfile.mkdtemp(prefix="prev2"))
    e.execute_action("setup_attributes", {"name": "顾衡", "blood_points": 7,
                                          "speed_points": 8, "mana_points": 10})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": region})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    e.state.energy = 0
    return e


def _give(e, name, x=0):
    e.state.player.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X",
               effect_formula=""), x_value=x)


def _enemy(name, hp, bl=None, atk=1, ap=5, baolie=False):
    """敌方怪物。baolie 参数已废弃（2026-10-03 删【爆裂】），保留仅为调用签名兼容。"""
    return Entity(name, "怪物", blood_limit=bl if bl is not None else hp,
                  current_hp=hp, attack_count=atk, attack_power=ap)


def _lethal_consumable(e, mutation=46):
    """2026-10-11 起的安全层危险源：异变 46 + 残骸（异变+10 → 56 ≥ 50 迷失）→ 自灭。

    【无神】删除后，轮回者已无"道纹出手打自己"的路径；改用一个与道纹完全无关的
    自灭源，正好证明安全层不是某一道纹的特判。
    """
    from engine.models import Consumable
    e.state.player.mutation_count = mutation
    e.state.consumables = [Consumable(name="残骸", effect="恢复20生命并获得异变10",
                                      current_uses=1, max_uses=1)]
    return e


def _ai_ready(e, player_hp, player_mana, daowen, enemies):
    p = e.state.player
    p.current_hp = player_hp
    p.blood_limit = 100
    p.current_mana = player_mana
    p.mana_limit = player_mana
    p.current_speed = 8
    p.speed_limit = 8
    for name, x in daowen.items():
        _give(e, name, x)
    e.state.enemies = enemies
    e.state.phase = "in_combat"
    e.state.combat_subphase = "player_actions"
    e.state.current_round = 2
    return TacticalAI(e)


# ============ 异变迷失案例：自己的动作打死自己 → 预演必须判死 ============

def test_guheng_case_preview_reports_self_attack_death():
    """预演必须判定：异变46 的轮回者吃下残骸（+10 → 56 ≥ 50 迷失）→ 命零。"""
    e = _arena()
    ai = _ai_ready(
        e, player_hp=39, player_mana=36,
        daowen={"杀伐": 24},
        enemies=[
            _enemy("脑蜘蛛", 204, atk=2, ap=11),
            _enemy("人头气球", 222),
            _enemy("血肉巨囊", 258, atk=1, ap=8),
        ],
    )
    _lethal_consumable(e)
    before_hp = e.state.player.current_hp
    before_mut = e.state.player.mutation_count

    pv = ai.previewer.preview("consume_item", {"name": "残骸"})
    diff = pv["diff"]
    assert pv["result"] is not None and pv["result"].get("success"), pv["result"]
    assert diff["player_dead"] is True, "预演必须判定轮回者命零"
    assert diff["player"]["hp_after"] == 0
    assert diff["player"]["mutation_after"] == 56, diff["player"]
    # 效果链必须包含自灭相关的伤害/死亡事件
    reflect_events = [ev for ev in diff["events"]
                      if ev["type"] in ("damage_applied", "entity_died")]
    assert reflect_events, "效果链必须包含自灭的伤害/死亡事件"
    # 预演零副作用（restore 会替换实体对象，必须从 state 重读玩家）
    assert e.state.player.current_hp == before_hp, "预演不得改变真实战斗状态"
    assert e.state.player.mutation_count == before_mut, "预演不得改变真实异变层数"


def test_guheng_case_tactical_ai_rejects_self_lethal_aoe():
    """TacticalAI 不得选择致死行动：try_consumable 经预演拒绝自灭的残骸。"""
    e = _arena()
    ai = _ai_ready(
        e, player_hp=39, player_mana=36,
        daowen={"杀伐": 24},
        enemies=[
            _enemy("脑蜘蛛", 204, atk=2, ap=11),
            _enemy("人头气球", 222),
            _enemy("血肉巨囊", 258, atk=1, ap=8),
        ],
    )
    _lethal_consumable(e)
    r = ai.try_consumable()
    assert r is None, "致死消耗品必须被安全过滤拒绝"
    assert ai.preview_rejected, "安全过滤应记录被淘汰候选"
    assert any("残骸" in entry for entry in ai.preview_rejected), ai.preview_rejected
    # 玩家状态未被改动（预演+过滤全程零副作用）
    assert e.state.player.current_hp == 39
    assert e.state.player.mutation_count == 46


# ========== 安全层的通用性：同一个分类器，不看动作名也不看道纹名 ==========

def test_risk_classify_is_action_agnostic():
    """安全层不是道纹特判：`risk_classify` 只读后果 diff，与动作名/道纹名无关。

    原用例是"单体自灭也要被拦截"；【无神】删除后轮回者已无自灭道纹路径，
    故改从这一侧钉住同一条不变量：同一份致死后果，无论来自哪类动作，
    分类结果一致。
    """
    e = _arena()
    ai = _ai_ready(e, player_hp=39, player_mana=36, daowen={"杀伐": 24},
                   enemies=[_enemy("脑蜘蛛", 204, atk=2, ap=11)])
    p = e.state.player
    lethal_diff = {"player": {"hp_after": 0, "hp_before": 39, "mana_after": 36,
                              "mana_before": 36, "speed_after": 8, "speed_before": 8,
                              "mutation_delta": 0, "mutation_after": 0},
                   "player_dead": True, "events": []}
    safe_diff = {"player": {"hp_after": 39, "hp_before": 39, "mana_after": 30,
                            "mana_before": 36, "speed_after": 8, "speed_before": 8,
                            "mutation_delta": 0, "mutation_after": 0},
                 "player_dead": False, "events": []}
    assert ActionPreview.risk_classify(lethal_diff, p)[0] == "LETHAL"
    assert ActionPreview.risk_classify(safe_diff, p)[0] != "LETHAL"
    # 同一个玩家、同一份后果，换成任意动作名/道纹名都不得改变结论
    p2 = e.state.player
    assert ActionPreview.risk_classify(dict(lethal_diff), p2)[0] == "LETHAL"


def test_safe_attack_still_allowed():
    """安全动作行为不变：未中【无神】时，正常攻击仍可执行并造成伤害。"""
    e = _arena()
    ai = _ai_ready(
        e, player_hp=60, player_mana=30,
        daowen={"杀伐": 5},
        enemies=[_enemy("普通怪", 100, atk=1, ap=5)],
    )
    pv = ai.previewer.preview("use_daowen", {
        "daowen_name": "杀伐", "x": 3, "target": "普通怪",
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    })
    assert pv["diff"]["player_dead"] is False, "未中无神不应自灭"
    assert pv["diff"]["enemies"][0]["hp_after"] < pv["diff"]["enemies"][0]["hp_before"]

    r = ai._cast("杀伐", 3, "普通怪")
    assert r is not None and r.get("success"), "安全攻击必须正常执行"
    assert not ai.preview_rejected


def test_preview_restores_all_state():
    """预演零副作用：执行前后 玩家HP/法力/速度/碎片、怪物HP、事件流 完全一致。"""
    e = _arena()
    ai = _ai_ready(
        e, player_hp=39, player_mana=36,
        daowen={"冲击": 4},
        enemies=[
            _enemy("脑蜘蛛", 204, atk=2, ap=11),
            _enemy("人头气球", 222),
            _enemy("血肉巨囊", 258, atk=1, ap=8),
        ],
    )
    def snap():
        # restore 会替换实体对象：必须每次从 state 重读，不得持有旧引用
        pp = e.state.player
        return {
            "hp": pp.current_hp, "mana": pp.current_mana, "speed": pp.current_speed,
            "shards": e.state.shards,
            "enemy_hp": [m.current_hp for m in e.state.enemies],
            "events": len(e.state.combat_events),
            "round_used": e.combat._monster_round_used(e.state.enemies[0]),
        }

    before = snap()
    ai.previewer.preview("use_daowen", {
        "daowen_name": "冲击", "x": 4, "dodge": False, "blood_shadow": False,
        "trigger_spell_choices": {},
    })
    after = snap()
    assert after == before, f"预演必须零副作用:\n{before}\n{after}"
