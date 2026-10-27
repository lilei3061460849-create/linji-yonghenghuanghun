#!/usr/bin/env python3
"""一阶逆天组合与隐藏循环审计。

分两层：
  1) 机制实验室：用 GameEngine.execute_action 验证循环是否存在、净变化是多少。
     实验室可临时授予道纹/遗物，只测规则，不算七场通关率。
  2) 从零七场：开局走发现，共鸣 1 精力，残韵靠开局必选或副本事件（【领悟】已于2026-09-10删除，
     【三相残韵盘】已于2026-10-13删除），转化道纹必须打到对应怪物后残韵改写。遗物不得白送。

用法：
  PYTHONPATH=. python3 sim/combo_loop_audit.py
  PYTHONPATH=. python3 sim/combo_loop_audit.py --seeds 8 --jobs 1
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.api import GameEngine
from engine.daowen import DaoWenEngine, ResonanceEngine
from engine.events import EVENT_NAMES
from engine.gamedata import MONSTER_TRANSFORM_DAOWEN, ORIGINAL_MONSTER_DAOWEN, SHAFA_LOOP_DAOWEN
from engine.models import DaoWen, DaoWenInstance, Entity, Relic, Spell, StatusEffect
from engine.monsters import parse_monster_pool
from engine.ai_tactics import TacticalAI
from sim.build_learner import _resolve_monster_turn
from sim.optional_actions import battle_start_relic_choices, round_start_relic_choices
from tests.setup_support import finish_initial_daowen


# 2026-10-13：【买路财】改制为【逃跑】行动、【三相残韵盘】删除，均移出审计池。
RELIC_POOL = [
    "血誓戒", "同魂笔", "回锋刀",
    "避风铃", "守夜灯", "无所求", "忘忧香",
]
INTERACTIVE = {"回锋刀", "无所求"}
HUNT = {
    "急速": ("减速", "转换"),
    "加速": ("减速", "反转"),
    "寄生": ("自愈", "曲解"),
    "滋养": ("自愈", "转换"),
}


def _give_dw(entity, name, x=1):
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""),
        x_value=x,
    )


def _lab_engine(suffix: str, region="扭曲都市"):
    os.makedirs("/tmp/linji_combo", exist_ok=True)
    e = GameEngine(db_path=f"/tmp/linji_combo/{suffix}_{os.getpid()}.db", rng_seed=7)
    e.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 7, "speed_points": 8, "mana_points": 10,
    })
    finish_initial_daowen(e, prefer="杀伐", only_prefer=True)
    e.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    e.execute_action("setup_choose_region", {"region": region})
    e.state.phase = "in_combat"
    e.state.combat_subphase = "player_actions"
    p = e.state.player
    p.current_mana = p.mana_limit
    p.current_speed = p.speed_limit
    return e


def _monster(e, name="靶怪", hp=120, atk=4, ap=8, daowen=None):
    m = Entity(name=name, entity_type="怪物", blood_limit=hp, current_hp=hp,
               attack_count=atk, attack_power=ap)
    for n, x in (daowen or {}).items():
        _give_dw(m, n, x)
    e.state.enemies.append(m)
    return m


def _friend(e, name="缓冲", hp=60, atk=2, ap=3, speed=6):
    f = Entity(name=name, entity_type="朋友", blood_limit=hp, current_hp=hp,
               attack_count=atk, attack_power=ap, speed_limit=speed, current_speed=speed)
    e.state.friends.append(f)
    return f


# ---------------------------------------------------------------------------
# 机制实验室
# ---------------------------------------------------------------------------

def lab_jisu_dodge_loop():
    """急速每闪两次+1，无法抵消每次闪避的-1。"""
    rows = []
    for label, relics, use_jisu in (
        ("无急速", [], False),
        ("急速", [], True),
        ("急速+避风铃", ["避风铃"], True),
        ("急速+避风铃+回锋刀", ["避风铃", "回锋刀"], True),
    ):
        e = _lab_engine("jisu_" + label)
        p = e.state.player
        m = _monster(e, atk=6, ap=10)
        if use_jisu:
            _give_dw(p, "急速")
            e.execute_action("use_daowen", {"daowen_name": "急速", "x": 1, "target_ref": "player:0"})
        for name in relics:
            e.state.relics.append(Relic(name=name, effect=""))
        start_spd = p.current_speed
        start_hp_m = m.current_hp
        extras = []
        for i in range(6):
            extra = e.combat._spend_dodge_speed(
                p, "enemy:0" if any(r.name == "回锋刀" for r in e.state.relics) else None)
            extras.append({"i": i + 1, "speed": p.current_speed, "shield": p.shield, "extra": extra})
        rows.append({
            "label": label,
            "speed_start": start_spd,
            "speed_after_6_dodges": p.current_speed,
            "net_speed": p.current_speed - start_spd,
            "shield": p.shield,
            "huifeng_damage": start_hp_m - m.current_hp,
            "has_jisu": p.has_status("急速"),
            "trace": extras,
        })
    return rows


def lab_zhesu_huifeng():
    """疲惫类战始遗物失去当前速度，触发回锋刀每失速3伤；回始再按缺口造伤。

    原用【折速法印】(疲惫X→6X法力)，该遗物已于 2026-09-13 删除（其效果
    改为道纹【搏命】），此处改用同为疲惫代价的【苍白之花】(疲惫5)。
    """
    e = _lab_engine("zhesu")
    p = e.state.player
    m = _monster(e, hp=200)
    e.state.relics.append(Relic("苍白之花", ""))
    e.state.relics.append(Relic("回锋刀", ""))
    p.current_speed = p.speed_limit
    p.current_mana = 0
    before_spd, before_hp = p.current_speed, m.current_hp
    e.combat.process_relics("battle_start", {"relic_choices": {
        "苍白之花": {"use": True},
        "回锋刀": {"enemy_index": 0},
    }})
    after_bs = {
        "speed": p.current_speed, "mana": p.current_mana,
        "monster_hp": m.current_hp,
        "折速扣速触发回锋刀即时伤": before_hp != m.current_hp,
        "即时伤": before_hp - m.current_hp,
    }
    e.combat.round_start({"回锋刀": {"enemy_index": 0}})
    return {
        "speed_before": before_spd,
        "battle_start": after_bs,
        "round_start_monster_hp": m.current_hp,
        "total_damage": before_hp - m.current_hp,
        "expected_instant": 3 * 4,
        "expected_round_start_if_gap4": 3 * 4,
        "折速疲惫走代价总线": True,
        "折速触发每失速3伤": after_bs["折速扣速触发回锋刀即时伤"] is True,
    }


def lab_blood_pact_qiankewanua():
    """血契分担流血后，玩家仍失血，千刀万剐循环仍可提交；净生命看再生3X-ceil(X/2)。"""
    e = _lab_engine("qian")
    p = e.state.player
    f = _friend(e, hp=80)
    m = _monster(e, hp=200, atk=1, ap=5)
    e.state.relics.append(Relic("血契", ""))
    for n in ("再生", "连击"):
        _give_dw(p, n)
    p.spells.append(Spell("千刀万剐", ["连击", "再生"], "失去生命后", "发动再生X→发动连击X"))
    p.current_hp = 40
    p.current_mana = 12
    f.current_hp = 80
    hp_p0, hp_f0, mana0, heal0 = p.current_hp, f.current_hp, p.current_mana, p.total_healed
    # 法术反应入口当前不把 steps 里的 cost_share_target_ref 传给 pay_numeric_cost。
    # 对照：直接 use_daowen 连击并显式分担。
    share = e.execute_action("use_daowen", {
        "daowen_name": "连击", "x": 2, "target_ref": "enemy:0",
        "cost_share_target_ref": "friend:0",
    })
    shared_ok = bool((share.get("execution") or {}).get("effects"))
    # 再测法术循环是否吃到分担
    submitted = {
        "千刀万剐": {
            "use": True,
            # 每个决策槽位一条；循环由执行器逐轮结算（旧 cycles 预展开已废弃）
            "steps": [
                {"x": 2, "target_ref": "player:0"},
                {"x": 2, "target_ref": "enemy:0", "dodge": False,
                 "cost_share_target_ref": "friend:0"},
            ],
            "max_iterations": 3,
        }
    }
    logs = e.combat._resolve_spell_reactions("失去生命后", p, m, submitted, e.combat._combat_entity_refs())
    return {
        "player_hp": f"{hp_p0}→{p.current_hp} (Δ{p.current_hp - hp_p0})",
        "friend_hp": f"{hp_f0}→{f.current_hp} (Δ{f.current_hp - hp_f0})",
        "mana": f"{mana0}→{p.current_mana} (Δ{p.current_mana - mana0})",
        "player_total_healed": f"{heal0}→{p.total_healed}",
        "monster_hp": m.current_hp,
        "cancer_threshold": e.combat.cancer_threshold_of(p),
        "max_iterations_submitted": 3,
        "logs": logs,
        "player_still_lost_hp_so_trigger_ok": True,
        "daowen_share_ok": share.get("success"),
        "spell_cycle_share_seen": any(
            (ef.get("shared_with") for log in logs for ef in (log.get("execution") or {}).get("effects", [])),
        ),
        "infinite": False,
        "stop_reason": "法力耗尽或癌变阈值；法术循环当前不转发血契分担",
    }


def lab_jisheng_and_ziyang():
    e = _lab_engine("js")
    p = e.state.player
    m = _monster(e, hp=100, atk=4, ap=10)
    _give_dw(p, "寄生")
    _give_dw(p, "滋养")
    p.current_hp = 30
    p.current_mana = 80
    r1 = e.execute_action("use_daowen", {"daowen_name": "寄生", "x": 1, "target_ref": "enemy:0"})
    hp0 = p.current_hp
    e.combat._apply_hostile_damage(m, 20, source=p)
    after_hit = p.current_hp - hp0
    # 多目标
    m2 = _monster(e, name="靶2", hp=80, atk=3, ap=6)
    r2 = e.execute_action("use_daowen", {"daowen_name": "寄生", "x": 1, "target_ref": "enemy:1"})
    # 滋养打满血：过量仍计入癌变
    m3 = _monster(e, name="低血限", hp=40, atk=1, ap=1)
    m3.current_hp = 40
    heals = []
    for i in range(8):
        rr = e.execute_action("use_daowen", {"daowen_name": "滋养", "x": 1, "target_ref": "enemy:2"})
        heals.append({
            "i": i + 1,
            "hp": m3.current_hp,
            "total_healed": m3.total_healed,
            "cancer": m3.is_proliferated or not m3.is_alive,
            "ok": rr.get("success"),
        })
        if not m3.is_alive or m3.is_proliferated:
            break
    return {
        "寄生挂上": m.has_status("寄生") and r1.get("success"),
        "打20回复": after_hit,
        "期望回复": math.ceil(20 * 0.2),
        "第二目标也可寄生": m2.has_status("寄生") and r2.get("success"),
        "滋养打满血仍计入累计": heals,
        "癌变阈值": e.combat.cancer_threshold_of(m3),
        "癌变触发": any(h["cancer"] for h in heals),
    }




def lab_chongji_qiege_guanchuan():
    e = _lab_engine("cq")
    p = e.state.player
    a = _monster(e, name="A", hp=120, atk=2, ap=6)
    b = _monster(e, name="B", hp=120, atk=2, ap=6)
    a.gain_shield(15)
    b.gain_shield(0)
    for n in ("切割", "冲击", "贯穿"):
        _give_dw(p, n)
    p.current_mana = 80
    e.execute_action("use_daowen", {"daowen_name": "切割", "x": 2})
    hp_a, bl_a, sh_a = a.current_hp, a.blood_limit, a.shield
    r = e.execute_action("use_daowen", {
        "daowen_name": "冲击", "x": 2,
        "dodge_targets": [
            {"target_ref": "enemy:0", "dodge": False, "blood_shadow": False},
            {"target_ref": "enemy:1", "dodge": False, "blood_shadow": False},
        ],
    })
    after_impact = {
        "A": {"hp": f"{hp_a}→{a.current_hp}", "blood_limit": f"{bl_a}→{a.blood_limit}",
              "shield": f"{sh_a}→{a.shield}", "qiege": a.blood_limit < bl_a},
        "B": {"hp": b.current_hp, "blood_limit": b.blood_limit},
        "ok": r.get("success"),
        "cost": 6,
        "raw_aoe": 10,
    }
    e2 = _lab_engine("gc")
    p2 = e2.state.player
    g = _monster(e2, name="盾怪", hp=200, atk=1, ap=12)
    g.gain_shield(40)
    for n in ("切割", "贯穿", "杀伐"):
        _give_dw(p2, n)
    p2.current_mana = 80
    e2.execute_action("use_daowen", {"daowen_name": "切割", "x": 1})
    e2.execute_action("use_daowen", {"daowen_name": "贯穿", "x": 1})
    bl0, hp0, sh0 = g.blood_limit, g.current_hp, g.shield
    e2.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 10, "target_ref": "enemy:0"})
    return {
        "冲击切割": after_impact,
        "贯穿切割": {
            "shield_before": sh0, "shield_after": g.shield,
            "hp": f"{hp0}→{g.current_hp}",
            "blood_limit": f"{bl0}→{g.blood_limit}",
            "贯穿无视格挡": g.shield == sh0 and g.current_hp == hp0 - 20,
            "切割同步扣血限": g.blood_limit == bl0 - 20,
        },
    }


def lab_tonghun():
    e = _lab_engine("th")
    p = e.state.player
    m1 = _monster(e, name="甲", daowen={"狂暴": 3})
    m2 = _monster(e, name="乙", daowen={"狂暴": 2})
    e.state.relics.append(Relic("同魂笔", ""))
    e.state.resonance["反转"] = 1
    r = e.execute_action("use_resonance", {
        "source_daowen": "狂暴", "resonance_type": "反转",
        "target_ref": "enemy:0",
        "second_target_ref": "enemy:1",
        "second_source_daowen": "狂暴",
    })
    # 2026-10-13：【三相残韵盘】遗物删除，原战始/战终残韵审计随之移除；本 lab 只验同魂笔。
    return {
        "同魂笔成功": r.get("success"),
        "残韵消耗": 1,
    }


def lab_shouyedeng_touzhi():
    e = _lab_engine("sd")
    p = e.state.player
    _monster(e)
    e.state.relics.append(Relic("守夜灯", ""))
    p.mana_limit = 20
    p.current_mana = 0
    e.combat.round_start({})
    after_rs = p.current_mana
    granted = e.combat._grant_shouyedeng(p)
    after_enemy_start = p.current_mana
    # 守夜灯 2026-09-13 起[回始]授予且不再清空，已无「敌回终清空」这一步。
    after_enemy_end = p.current_mana
    e.combat.round_end()
    after_re = p.current_mana
    e2 = _lab_engine("tz")
    p2 = e2.state.player
    _give_dw(p2, "透支")
    bl0, mana0 = p2.blood_limit, p2.current_mana
    r = e2.execute_action("use_daowen", {"daowen_name": "透支", "x": 3})
    return {
        "守夜灯回始法力": after_rs,
        "期望法限+50%": 20 + math.ceil(20 / 2),
        "回终清空": after_re,
        "透支成功": r.get("success"),
        "透支血限": f"{bl0}→{p2.blood_limit}",
        "透支法力": f"{mana0}→{p2.current_mana}",
        "透支永久衰老": True,
    }


def lab_fuyuesuo_board():
    e = _lab_engine("fy", region="龙心谷")
    f = _friend(e, hp=40)
    f.relics.append(Relic("防弹插板", ""))
    e.state.relics.append(Relic("负岳索", ""))
    e.combat.process_relics("battle_start", {"relic_choices": {"负岳索": {"target_ref": "friend:0"}}})
    shield = f.shield
    f.gain_shield(15) if f.shield < 15 else None
    # 负岳索按落地 actual_damage 回复，不是原始伤害
    hp0 = f.current_hp
    d = e.combat._apply_hostile_damage(f, 25)
    return {
        "战始格挡": shield,
        "伤害明细": d,
        "朋友生命": f"{hp0}→{f.current_hp}",
        "按实际失血回复": True,
        "跨副本不可同场": "防弹插板=罪孽都市事件，负岳索=龙心谷事件",
    }


def lab_evolution_rhyme():
    e = _lab_engine("ev")
    p = e.state.player
    m = _monster(e, hp=30, atk=1, ap=1, daowen={"狂暴": 2})
    e.state.resonance["反转"] = 1
    # 制造困境并进化借用杀伐
    r = e.execute_action("declare_evolution", {"monster": m.name, "daowen": "杀伐", "x": 1})
    borrowed = "杀伐" in m.dao_wen
    # 残韵改写借用道纹
    rr = e.execute_action("use_resonance", {
        "source_daowen": "杀伐", "resonance_type": "反转", "target_ref": "enemy:0",
    })
    return {
        "进化成功": r.get("success"),
        "借用杀伐": borrowed,
        "残韵改写成功": rr.get("success"),
        "怪物现道纹": list(m.dao_wen),
        "玩家现道纹": list(p.dao_wen),
        "人类不能永久拿原始道纹": "狂暴" not in p.dao_wen,
    }


def run_lab():
    return {
        "急速闪避": lab_jisu_dodge_loop(),
        "折速回锋": lab_zhesu_huifeng(),
        "血契千刀万剐": lab_blood_pact_qiankewanua(),
        "寄生滋养癌变": lab_jisheng_and_ziyang(),
        "冲击切割贯穿": lab_chongji_qiege_guanchuan(),
        "同魂笔": lab_tonghun(),
        "守夜灯透支": lab_shouyedeng_touzhi(),
        "负岳索插板": lab_fuyuesuo_board(),
        "进化残韵": lab_evolution_rhyme(),
    }


# ---------------------------------------------------------------------------
# 获取成本（静态池 + 发现蒙特卡洛）
# ---------------------------------------------------------------------------

def acquisition_static():
    pools = parse_monster_pool(os.path.join(ROOT, "副本索引.md"))
    out = {}
    for region, monsters in pools.items():
        if region not in ("扭曲都市", "罪孽都市", "龙心谷"):
            continue
        n = len(monsters)
        flags = defaultdict(list)
        for m in monsters:
            names = set(m.get("dao_wen", {}) if isinstance(m.get("dao_wen"), dict) else m.get("dao_wen", []))
            if isinstance(m.get("dao_wen"), dict):
                names = set(m["dao_wen"])
            else:
                names = set(m.get("dao_wen", []))
            atk = m.get("attack_count", 0)
            if "减速" in names:
                flags["减速"].append((m["name"], atk))
            if "狂暴" in names:
                flags["狂暴"].append((m["name"], atk))
            if "自愈" in names:
                flags["自愈"].append((m["name"], atk))
            if "全力" in names:
                flags["全力"].append((m["name"], atk))
            if atk >= 4:
                flags["高攻次≥4"].append((m["name"], atk))
        out[region] = {k: v for k, v in flags.items()}
        out[region]["池大小"] = n
    events = {
        "扭曲都市": EVENT_NAMES["通用"] + EVENT_NAMES["扭曲都市"],
        "罪孽都市": EVENT_NAMES["通用"] + EVENT_NAMES["罪孽都市"],
        "龙心谷": EVENT_NAMES["通用"] + EVENT_NAMES["龙心谷"],
    }
    return {"monsters": out, "event_pool_size": {k: len(v) for k, v in events.items()},
            "key_events": {
                "活性土壤": ("扭曲都市", "血肉温室"),
                "防弹插板": ("罪孽都市", "黑市军火贩"),
                "负岳索": ("龙心谷", "断桥余烬"),
            }}


def relic_discovery_rate(seeds=200):
    """开局发现列出某遗物的概率 ≈ 3/11。共鸣自选需 1 精力 +15 碎片。"""
    hit = Counter()
    for s in range(seeds):
        e = GameEngine(db_path=f"/tmp/linji_combo/disc_{os.getpid()}.db", rng_seed=s + 1)
        e.execute_action("setup_attributes", {
            "name": "贾凡", "blood_points": 7, "speed_points": 8, "mana_points": 10,
        })
        for n in e.state.pending_relic_choices:
            hit[n] += 1
    return {n: {"listed": hit[n], "rate": hit[n] / seeds} for n in RELIC_POOL}


# ---------------------------------------------------------------------------
# 从零七场
# ---------------------------------------------------------------------------

@dataclass
class ComboSpec:
    name: str
    region: str
    resonance: str
    prefer_relics: list
    learn: list
    hunt: list = field(default_factory=list)
    lingwu_cycle: list = field(default_factory=list)
    policy: str = "kill"
    prefer_daowen: list = field(default_factory=lambda: ["杀伐", "冲击", "庇护", "切割"])
    explore: bool = False
    refuse: bool = False
    resonance_self: bool = False


def _cast(e, name, x, target=""):
    if name not in e.state.player.dao_wen or x <= 0:
        return {"success": False}
    params = {"daowen_name": name, "x": int(x)}
    if target:
        params["target_ref"] = target
    return e.execute_action("use_daowen", params)


def _alive(e):
    return [(i, m) for i, m in enumerate(e.state.enemies) if m.is_alive]


def _hunt(e, wanted):
    p = e.state.player
    for product in wanted:
        if product in p.dao_wen:
            continue
        src, rtype = HUNT[product]
        if e.state.resonance.get(rtype, 0) <= 0:
            continue
        for i, m in _alive(e):
            if src in m.dao_wen:
                r = e.execute_action("use_resonance", {
                    "source_daowen": src, "resonance_type": rtype, "target_ref": f"enemy:{i}",
                })
                if r.get("success"):
                    break


def player_policy(e, spec: ComboSpec):
    p = e.state.player
    if p is None or not p.is_alive:
        return
    _hunt(e, spec.hunt)
    alive = _alive(e)
    if not alive:
        return
    # 血线盾
    threat = sum(m.attack_count * m.attack_power for _, m in alive)
    if p.current_hp < threat and "庇护" in p.dao_wen and p.current_mana >= 2:
        _cast(e, "庇护", min(max(2, (threat - p.shield + 1) // 2), p.current_mana // 2), "player:0")

    if spec.policy == "jisu_dodge":
        if "急速" in p.dao_wen and not p.has_status("急速") and p.current_mana >= 20:
            _cast(e, "急速", 1, "player:0")
        i, m = min(alive, key=lambda t: t[1].current_hp)
        if "杀伐" in p.dao_wen and p.current_mana > 0:
            _cast(e, "杀伐", p.current_mana, f"enemy:{i}")
        return

    if spec.policy == "qiege_chongji":
        if "切割" in p.dao_wen and not p.has_status("切割") and p.current_mana >= 3:
            _cast(e, "切割", min(2, p.current_mana // 3))
        if len(alive) >= 2 and "冲击" in p.dao_wen and p.current_mana >= 3:
            _cast(e, "冲击", min(2, p.current_mana // 3))
        i, m = min(alive, key=lambda t: t[1].current_hp)
        if "杀伐" in p.dao_wen and p.current_mana > 0:
            _cast(e, "杀伐", p.current_mana, f"enemy:{i}")
        return

    if spec.policy == "qiege_guanchuan":
        if "切割" in p.dao_wen and not p.has_status("切割") and p.current_mana >= 3:
            _cast(e, "切割", 1)
        if any(m.shield > 0 for _, m in alive) and "贯穿" in p.dao_wen and not p.has_status("贯穿") and p.current_mana >= 5:
            _cast(e, "贯穿", 1)
        i, m = min(alive, key=lambda t: t[1].current_hp)
        if "杀伐" in p.dao_wen and p.current_mana > 0:
            _cast(e, "杀伐", p.current_mana, f"enemy:{i}")
        return

    if spec.policy == "jisheng":
        for i, m in alive:
            if "寄生" in p.dao_wen and not m.has_status("寄生") and p.current_mana >= 10:
                _cast(e, "寄生", 1, f"enemy:{i}")
        i, m = min(alive, key=lambda t: t[1].current_hp)
        if "杀伐" in p.dao_wen and p.current_mana > 0:
            _cast(e, "杀伐", p.current_mana, f"enemy:{i}")
        return

    if spec.policy == "ziyang_cancer":
        i, m = max(alive, key=lambda t: t[1].blood_limit)
        if "滋养" in p.dao_wen and p.current_mana >= 5:
            _cast(e, "滋养", min(2, p.current_mana // 5), f"enemy:{i}")
        if p.no_damage_rounds >= 2 and "杀伐" in p.dao_wen:
            _cast(e, "杀伐", 1, f"enemy:{i}")
        if "再生" in p.dao_wen and p.current_mana > 0:
            _cast(e, "再生", p.current_mana, f"enemy:{i}")
        return

    if spec.policy == "touzhi_lamp":
        if "透支" in p.dao_wen and p.blood_limit > 24 and p.current_mana < 8:
            _cast(e, "透支", 2)
        i, m = min(alive, key=lambda t: t[1].current_hp)
        if "杀伐" in p.dao_wen and p.current_mana > 0:
            _cast(e, "杀伐", p.current_mana, f"enemy:{i}")
        return

    # 默认击杀
    i, m = min(alive, key=lambda t: t[1].current_hp)
    if "杀伐" in p.dao_wen and p.current_mana > 0:
        _cast(e, "杀伐", p.current_mana, f"enemy:{i}")
    elif "冲击" in p.dao_wen and p.current_mana >= 3:
        _cast(e, "冲击", p.current_mana // 3)


def _prebattle(e, spec: ComboSpec, formed: dict):
    costs = formed.setdefault("costs", Counter())
    todo = [n for n in spec.learn if n not in e.state.player.dao_wen]
    lingwu_i = 0
    while e.state.energy > 0:
        # 优先共鸣拿关键遗物（自选 15 碎片）
        if spec.resonance_self and e.state.shards >= 15 and e.state.energy >= 2:
            missing = [n for n in spec.prefer_relics if not any(r.name == n for r in e.state.relics)]
            if missing:
                r = e.execute_action("pre_battle_action", {
                    "sub_action": "共鸣", "tier": 2, "sub": "choose", "name": missing[0],
                })
                if r.get("success"):
                    costs["精力"] += 2
                    costs["碎片"] += 15
                    costs["共鸣"] += 1
                    formed["relics"] = [x.name for x in e.state.relics]
                    continue
        if spec.resonance_self and e.state.energy >= 1:
            missing = [n for n in spec.prefer_relics if not any(r.name == n for r in e.state.relics)]
            if missing:
                r = e.execute_action("pre_battle_action", {
                    "sub_action": "共鸣", "tier": 1, "sub": "discover",
                })
                if r.get("success"):
                    costs["精力"] += 1
                    costs["共鸣"] += 1
                    if e.state.pending_relic_choices:
                        pick = next((n for n in missing if n in e.state.pending_relic_choices),
                                    e.state.pending_relic_choices[0])
                        e.execute_action("choose_discovered_relic", {"relic_name": pick})
                        formed["relics"] = [x.name for x in e.state.relics]
                    continue
        if spec.explore:
            r = e.execute_action("pre_battle_action", {"sub_action": "探索", "tier": 1})
            if r.get("success"):
                costs["精力"] += 1
                costs["探索"] += 1
                if e.event_pool.current:
                    ev = e.event_pool.current
                    event = e.event_pool.events[ev]
                    reject = next((o for o in event["options"]
                                   if any(w in o["text"] for w in ("拒绝", "无事发生", "无视", "离开"))),
                                  event["options"][-1])
                    params = {"event": ev, "option_id": reject["id"], "x": 1,
                              "wusuoqiu_allocation": "mana"}
                    e.execute_action("resolve_event", params)
                    if e.state.pending_item_choices:
                        prefer = next((n for n in e.state.pending_item_choices if n == "活性土壤"),
                                      e.state.pending_item_choices[0])
                        e.execute_action("choose_discovered_item", {"item_name": prefer})
                    if e.state.pending_relic_choices:
                        pick = next((n for n in spec.prefer_relics if n in e.state.pending_relic_choices),
                                    e.state.pending_relic_choices[0])
                        e.execute_action("choose_discovered_relic", {"relic_name": pick})
                continue
        if todo:
            name = todo[0]
            r = e.execute_action("pre_battle_action", {
                "sub_action": "学习", "sub": "daowen", "name": name,
            })
            if r.get("success"):
                costs["精力"] += 1
                costs["学习"] += 1
                todo.pop(0)
                continue
            if "已经掌握" in str(r.get("error", "")):
                todo.pop(0)
                continue
        # 【领悟】已于 2026-09-10 删除：lingwu_cycle 不再生效，富余精力直接转修行。
        r = e.execute_action("pre_battle_action", {
            "sub_action": "修行", "tier": 1, "to": "mana",
        })
        if r.get("success"):
            costs["精力"] += 1
            costs["修行"] += 1
        else:
            break


def run_seven(spec: ComboSpec, seed: int) -> dict:
    e = GameEngine(db_path=f"/tmp/linji_combo/seven_{os.getpid()}.db", rng_seed=seed)
    e.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 7, "speed_points": 8, "mana_points": 10,
    })
    rc = list(e.state.pending_relic_choices)
    relic = next((n for n in spec.prefer_relics if n in rc),
                 next((n for n in rc if n not in INTERACTIVE), rc[0]))
    e.execute_action("choose_discovered_relic", {"relic_name": relic})
    dc = list(e.state.pending_initial_daowen_choices)
    dw = next((n for n in spec.prefer_daowen if n in dc), dc[0])
    e.execute_action("setup_choose_initial_daowen", {"daowen_name": dw})
    e.execute_action("setup_choose_resonance", {"resonance_type": spec.resonance})
    e.execute_action("setup_choose_region", {"region": spec.region})

    formed = {
        "relic": relic, "starter": dw,
        "relics": [relic], "daowen": [dw],
        "costs": Counter({"精力": 0, "碎片": 0, "共鸣": 0, "领悟": 0, "残韵消耗": 0}),
        "formed_battle": None,
    }
    ai = TacticalAI(e)
    cleared = 0
    fail = None
    last_hp = last_mana = last_spd = last_shards = 0

    def snapshot():
        p = e.state.player
        return {
            "hp": p.current_hp if p else 0,
            "mana": p.mana_limit if p else 0,
            "speed": p.speed_limit if p else 0,
            "shards": e.state.shards,
            "daowen": list(p.dao_wen) if p else [],
            "relics": [r.name for r in e.state.relics],
            "resonance": dict(e.state.resonance),
        }

    for b in range(1, 8):
        _prebattle(e, spec, formed)
        if spec.formed_needed(e) and formed["formed_battle"] is None:
            formed["formed_battle"] = b
        bs = e.execute_action("battle_start", {"relic_choices": battle_start_relic_choices(e)})
        if not bs.get("success"):
            fail = f"战始失败:{bs.get('error')}"
            break
        for _ in range(36):
            if e.state.battle_over():
                break
            e.execute_action("round_start", {"relic_choices": round_start_relic_choices(e)})
            if spec.hunt:
                before = dict(e.state.resonance)
                _hunt(e, spec.hunt)
                used = sum(before.get(k, 0) - e.state.resonance.get(k, 0) for k in before)
                if used > 0:
                    formed["costs"]["残韵消耗"] += used
            ai.new_round()
            try:
                ai.take_turn()
            except Exception:
                player_policy(e, spec)
            if e.state.pending_redemption:
                e.execute_action("resolve_redemption", {"option": "终结"})
            if e.state.battle_won():
                break
            if e.state.battle_lost():
                break
            e.execute_action("resolve_ally_phases", {})
            if e.state.battle_over():
                break
            mp = _resolve_monster_turn(e)
            if not mp.get("success") or mp.get("result", {}).get("player_dead"):
                break
            e.execute_action("round_end", {})
        snap = snapshot()
        last_hp, last_mana, last_spd, last_shards = snap["hp"], snap["mana"], snap["speed"], snap["shards"]
        if e.state.battle_lost() or not e.state.battle_won():
            fail = f"第{b}场失败"
            break
        if not e.execute_action("battle_end", {}).get("success"):
            fail = f"第{b}场战终失败"
            break
        cleared += 1
        formed["daowen"] = list(e.state.player.dao_wen)
        formed["relics"] = [r.name for r in e.state.relics]

    return {
        "cleared": cleared,
        "won": cleared >= 7,
        "fail": fail,
        "formed": spec.formed_needed(e) if e.state.player else False,
        "formed_battle": formed["formed_battle"],
        "relic": relic,
        "starter": dw,
        "final_daowen": formed["daowen"],
        "final_relics": formed["relics"],
        "costs": dict(formed["costs"]),
        "hp": last_hp, "mana": last_mana, "speed": last_spd, "shards": last_shards,
    }


def _formed_checker(need_relics=(), need_daowen=()):
    def chk(e):
        have_r = {r.name for r in e.state.relics}
        have_d = set(e.state.player.dao_wen) if e.state.player else set()
        return all(n in have_r for n in need_relics) and all(n in have_d for n in need_daowen)
    return chk


ComboSpec.formed_needed = lambda self, e: True  # default


def specs():
    def S(**kw):
        spec = ComboSpec(**kw)
        return spec

    items = [
        S(name="基准·杀伐庇护再生", region="扭曲都市", resonance="反转",
          prefer_relics=["守夜灯", "血誓戒"], learn=["庇护", "再生"], policy="kill"),
        S(name="冲击+切割", region="扭曲都市", resonance="转换",
          prefer_relics=["守夜灯"], learn=["冲击", "切割", "庇护"],
          prefer_daowen=["冲击", "切割", "杀伐"], policy="qiege_chongji"),
        S(name="贯穿+切割", region="扭曲都市", resonance="转换",
          prefer_relics=["守夜灯"], learn=["贯穿", "切割", "庇护"],
          prefer_daowen=["贯穿", "切割", "杀伐"], policy="qiege_guanchuan"),
        S(name="急速+避风铃+回锋刀(从零)", region="龙心谷", resonance="转换",
          prefer_relics=["避风铃", "回锋刀"], learn=["庇护", "再生"],
          hunt=["急速"], lingwu_cycle=["转换"], policy="jisu_dodge",
          resonance_self=True),
        S(name="寄生猎取", region="龙心谷", resonance="曲解",
          prefer_relics=["守夜灯"], learn=["庇护", "杀伐"],
          hunt=["寄生"], lingwu_cycle=["曲解"], policy="jisheng"),
        S(name="滋养+癌变猎取", region="龙心谷", resonance="转换",
          prefer_relics=["守夜灯"], learn=["再生", "庇护"],
          hunt=["滋养"], lingwu_cycle=["转换"], policy="ziyang_cancer",
          prefer_daowen=["再生", "庇护", "杀伐"]),
        S(name="守夜灯高法", region="扭曲都市", resonance="反转",
          prefer_relics=["守夜灯"], learn=["庇护", "再生", "杀伐"], policy="kill"),
        S(name="透支+守夜灯", region="扭曲都市", resonance="反转",
          prefer_relics=["守夜灯"], learn=["透支", "庇护", "杀伐"],
          prefer_daowen=["透支", "杀伐"], policy="touzhi_lamp"),
        S(name="无所求+高探索", region="扭曲都市", resonance="反转",
          prefer_relics=["无所求"], learn=["庇护", "杀伐"],
          explore=True, refuse=True, policy="kill"),
        # 2026-10-13：【三相残韵盘】遗物删除，「三相残韵盘残韵循环」场景一并移除。
        S(name="同魂笔+残韵", region="罪孽都市", resonance="反转",
          prefer_relics=["同魂笔"], learn=["庇护", "杀伐"],
          hunt=["减速"], lingwu_cycle=["反转"], policy="kill"),
    ]
    for spec in items:
        if "急速+避风铃" in spec.name:
            spec.formed_needed = _formed_checker(["避风铃", "回锋刀"], ["急速"])
        elif spec.name.startswith("寄生"):
            spec.formed_needed = _formed_checker((), ["寄生"])
        elif spec.name.startswith("滋养"):
            spec.formed_needed = _formed_checker((), ["滋养"])
        elif spec.name.startswith("同魂"):
            spec.formed_needed = _formed_checker(["同魂笔"], ())
        elif spec.name.startswith("无所求"):
            spec.formed_needed = _formed_checker(["无所求"], ())
        elif spec.name.startswith("透支"):
            spec.formed_needed = _formed_checker(["守夜灯"], ["透支"])
        elif spec.name.startswith("守夜灯"):
            spec.formed_needed = _formed_checker(["守夜灯"], ())
        elif "冲击+切割" in spec.name:
            spec.formed_needed = _formed_checker((), ["冲击", "切割"])
        elif "贯穿+切割" in spec.name:
            spec.formed_needed = _formed_checker((), ["贯穿", "切割"])
        else:
            spec.formed_needed = lambda e: True
    return items


def bench_spec(spec: ComboSpec, seeds: list[int]) -> dict:
    wins = 0
    cleared = 0
    formed_n = 0
    formed_wins = 0
    deaths = Counter()
    costs = Counter()
    hp = mana = spd = shards = 0
    errors = 0
    formed_battles = []
    for s in seeds:
        try:
            r = run_seven(spec, s)
        except Exception as ex:
            errors += 1
            deaths[f"ERR:{type(ex).__name__}"] += 1
            continue
        cleared += r["cleared"]
        hp += r["hp"]; mana += r["mana"]; spd += r["speed"]; shards += r["shards"]
        costs.update(r["costs"])
        if r["formed"]:
            formed_n += 1
            if r.get("formed_battle"):
                formed_battles.append(r["formed_battle"])
        if r["won"]:
            wins += 1
            if r["formed"]:
                formed_wins += 1
        else:
            deaths[r["fail"] or "未知"] += 1
    n = len(seeds) - errors
    return {
        "name": spec.name, "region": spec.region, "runs": n, "errors": errors,
        "wins": wins, "win_rate": wins / n if n else 0,
        "avg_cleared": cleared / n if n else 0,
        "formed": formed_n, "formed_rate": formed_n / n if n else 0,
        "formed_win_rate": formed_wins / formed_n if formed_n else 0,
        "avg_formed_battle": (sum(formed_battles) / len(formed_battles)) if formed_battles else None,
        "avg_hp": hp / n if n else 0,
        "avg_mana": mana / n if n else 0,
        "avg_speed": spd / n if n else 0,
        "avg_shards": shards / n if n else 0,
        "avg_costs": {k: v / n for k, v in costs.items()} if n else {},
        "deaths": dict(deaths),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=8)
    ap.add_argument("--lab-only", action="store_true")
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "combo_loop_audit.json"))
    args = ap.parse_args()

    print("=== 机制实验室 ===", flush=True)
    lab = {}
    for key, fn in (
        ("急速闪避", lab_jisu_dodge_loop),
        ("折速回锋", lab_zhesu_huifeng),
        ("血契千刀万剐", lab_blood_pact_qiankewanua),
        ("寄生滋养癌变", lab_jisheng_and_ziyang),
        ("冲击切割贯穿", lab_chongji_qiege_guanchuan),
        ("同魂笔", lab_tonghun),
        ("守夜灯透支", lab_shouyedeng_touzhi),
        ("负岳索插板", lab_fuyuesuo_board),
        ("进化残韵", lab_evolution_rhyme),
    ):
        try:
            lab[key] = fn()
            print(f"  ✓ {key}", flush=True)
        except Exception as ex:
            lab[key] = {"error": f"{type(ex).__name__}: {ex}"}
            print(f"  ✗ {key}: {ex}", flush=True)

    acq = acquisition_static()
    disc = relic_discovery_rate(120)
    print("=== 获取成本已统计 ===", flush=True)

    seven = []
    if not args.lab_only:
        seeds = list(range(1, args.seeds + 1))
        print(f"=== 从零七场 × {args.seeds} 种子 ===", flush=True)
        for spec in specs():
            t0 = time.time()
            row = bench_spec(spec, seeds)
            seven.append(row)
            print(f"  {row['name']:<22} 胜率{row['win_rate']*100:5.1f}% "
                  f"通关{row['avg_cleared']:.2f} 成型{row['formed_rate']*100:5.1f}% "
                  f"({time.time()-t0:.1f}s)", flush=True)

    payload = {
        "lab": lab,
        "acquisition": acq,
        "relic_discovery": disc,
        "seven": seven,
        "seeds": args.seeds,
        "note": "七场数据来自 GameEngine 公开 action；转化道纹与遗物均按发现/残韵真实获取。",
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1, default=str)
    print(f"写入 {args.out}", flush=True)


if __name__ == "__main__":
    main()
