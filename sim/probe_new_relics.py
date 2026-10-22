#!/usr/bin/env python3
"""探针：5 件新遗物的实测表现（2026-10-03 用户令）。

覆盖：千荆甲 / 万钧印 / 癫狂之脑 / 活血衣 / 增生药剂（全部走生产引擎实跑）。
每条用例只**观察**引擎结果并打印，不预设结论；不可判定处标 UNVERIFIED。

跑法：
    python3 sim/probe_new_relics.py                 # 人类可读
    python3 sim/probe_new_relics.py --json /tmp/verify/new_relics.json
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, "sim")

from engine.daowen import DaoWenEngine                 # noqa: E402
from engine.models import Entity, Relic, StatusEffect  # noqa: E402
from engine.api import GameEngine                      # noqa: E402
from tests.setup_support import begin_battle, begin_round, finish_initial_daowen  # noqa: E402


def _sandbox(tmp: Path, relics: list[str], region: str = "扭曲都市") -> GameEngine:
    e = GameEngine(db_path=str(tmp / "d.db"), save_dir=str(tmp),
                   sealed_candidate_path=str(tmp / "x.json"), rng_seed=7)
    assert e.execute_action("setup_attributes", {
        "name": "甲", "blood_points": 11, "speed_points": 8, "mana_points": 6})["success"]
    assert finish_initial_daowen(e)["success"]
    assert e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})["success"]
    assert e.execute_action("setup_choose_region", {"region": region})["success"]
    assert begin_battle(e)["success"]
    assert begin_round(e)["success"]
    e.state.relics = [Relic(name=n, effect="") for n in relics]
    p = e.state.player
    p.blood_limit, p.current_hp = 400, 200
    p.mana_limit, p.current_mana = 60, 55
    p.speed_limit, p.current_speed = 5, 5
    p.shield = 0
    return e


def _monster(e: GameEngine, atk: int = 3, ap: int = 10, hp: int = 500) -> Entity:
    m = e.state.enemies[0]
    m.blood_limit, m.current_hp = hp, hp
    m.speed_limit, m.current_speed = 3, 3
    m.mana_limit, m.current_mana = 20, 20
    m.shield = 0
    m.dao_wen = {}
    m.attack_count, m.attack_power = atk, ap
    return m


def _attack(e: GameEngine, attacker: Entity, target: Entity, **kw) -> dict:
    return e.combat.resolve_attack(attacker, target, **kw)


# ==================== 1. 千荆甲 ====================

def case_qianjingjia(tmp: Path) -> dict:
    e = _sandbox(tmp, ["千荆甲"])
    p, m = e.state.player, _monster(e)
    m.current_hp = m.blood_limit = 45
    r = _attack(e, m, p)
    out = {
        "case": "千荆甲：怪物攻击玩家",
        "attacker_hp_before": 45, "attacker_hp_after": m.current_hp,
        "player_hp_before": 200, "player_hp_after": p.current_hp,
        "damage_to_player": r.get("damage_dealt"),
        "attacker_died": not m.is_alive,
        "reflect_ledger": [ev for ev in getattr(m, "_hp_loss_events", [])][-1:],
    }
    # 反噬致死 → 玩家这一击不落地
    e2 = _sandbox(tmp, ["千荆甲"])
    p2, m2 = e2.state.player, _monster(e2)
    m2.current_hp = m2.blood_limit = 8   # 攻击 30 点 → 反噬 30 → 必死
    r2 = _attack(e2, m2, p2)
    out["lethal_case"] = {
        "attacker_died": not m2.is_alive,
        "player_hp_after": p2.current_hp,
        "damage_dealt_to_player": r2.get("damage_dealt", 0),
        "suppress_flag": r2.get("qianjingjia_suppress", False),
    }
    # 非攻击伤害（道纹）不触发
    e3 = _sandbox(tmp, ["千荆甲"])
    p3, m3 = e3.state.player, _monster(e3)
    m3.dao_wen = {}
    e3._grant_named_daowen(m3, "杀伐")
    hp_before = m3.current_hp
    calc = DaoWenEngine.resolve("杀伐", 5)
    e3.combat.apply_daowen_effect("杀伐", calc, m3, p3)
    out["non_attack_no_reflect"] = {"attacker_hp_unchanged": m3.current_hp == hp_before}
    return out


# ==================== 2. 万钧印 ====================

def case_wanjunyin(tmp: Path) -> dict:
    e = _sandbox(tmp, ["万钧印"])
    p, m = e.state.player, _monster(e)
    hp_m, hp_p = m.current_hp, p.current_hp
    r = _attack(e, m, p)
    out = {
        "case": "万钧印：怪物攻击玩家 → 改为怪物自攻",
        "wanjunyin": r.get("wanjunyin"),
        "attacker_hp_before": hp_m, "attacker_hp_after": m.current_hp,
        "player_hp_before": hp_p, "player_hp_after": p.current_hp,
        "damage_dealt_field": r.get("damage_dealt", 0),
    }
    # 拒绝发动
    e2 = _sandbox(tmp, ["万钧印"])
    p2, m2 = e2.state.player, _monster(e2)
    hp_m2, hp_p2 = m2.current_hp, p2.current_hp
    r2 = _attack(e2, m2, p2, wanjunyin=False)
    out["refused"] = {"attacker_hp_after": m2.current_hp, "player_hp_after": p2.current_hp,
                      "attacker_unchanged": m2.current_hp == hp_m2,
                      "player_took_damage": p2.current_hp < hp_p2,
                      "wanjunyin": r2.get("wanjunyin")}
    # 无遗物对照组
    e3 = _sandbox(tmp, [])
    p3, m3 = e3.state.player, _monster(e3)
    _attack(e3, m3, p3)
    out["control_no_relic"] = {"player_hp_after": p3.current_hp}
    return out


# ==================== 3. 癫狂之脑 ====================

def case_diankuang(tmp: Path) -> dict:
    e = _sandbox(tmp, ["癫狂之脑"])
    p, m = e.state.player, _monster(e)
    budget = e._action_budget_of(p)
    # 攻击伤害翻倍
    hp_before = p.current_hp
    r = _attack(e, m, p)
    dmg = hp_before - p.current_hp
    # 对照：无遗物
    e2 = _sandbox(tmp, [])
    p2, m2 = e2.state.player, _monster(e2)
    hp2 = p2.current_hp
    _attack(e2, m2, p2)
    dmg2 = hp2 - p2.current_hp
    # 非攻击伤害不翻倍（道纹直伤）
    e3 = _sandbox(tmp, ["癫狂之脑"])
    p3, m3 = e3.state.player, _monster(e3)
    e3._grant_named_daowen(m3, "杀伐")
    hp3 = p3.current_hp
    e3.combat.apply_daowen_effect("杀伐", DaoWenEngine.resolve("杀伐", 5), m3, p3)
    dmg3 = hp3 - p3.current_hp
    e4 = _sandbox(tmp, [])
    p4, m4 = e4.state.player, _monster(e4)
    e4._grant_named_daowen(m4, "杀伐")
    hp4 = p4.current_hp
    e4.combat.apply_daowen_effect("杀伐", DaoWenEngine.resolve("杀伐", 5), m4, p4)
    dmg4 = hp4 - p4.current_hp
    return {
        "case": "癫狂之脑：出手+1 / 攻击伤害翻倍",
        "action_budget_with_relic": budget,
        "attack_damage_with_relic": dmg,
        "attack_damage_without_relic": dmg2,
        "attack_doubled": dmg == 2 * dmg2,
        "daowen_damage_with_relic": dmg3,
        "daowen_damage_without_relic": dmg4,
        "daowen_not_doubled": dmg3 == dmg4,
    }


# ==================== 4. 活血衣 ====================

def case_huoxueyi(tmp: Path) -> dict:
    e = _sandbox(tmp, ["活血衣"])
    p, m = e.state.player, _monster(e, atk=2, ap=10)
    p.current_hp = 100
    healed_before = p.total_healed
    r = _attack(e, m, p)
    dmg = r.get("damage_dealt", 0)
    heal = p.total_healed - healed_before
    out = {
        "case": "活血衣：受到攻击伤害后回复其一半",
        "damage_taken": dmg,
        "heal": heal,
        "hp_after": p.current_hp,
        "hp_expected_if_half": 100 - dmg + -(-dmg // 2),
    }
    # 非攻击伤害（道纹）不回血
    e2 = _sandbox(tmp, ["活血衣"])
    p2, m2 = e2.state.player, _monster(e2)
    e2._grant_named_daowen(m2, "杀伐")
    p2.current_hp = 100
    e2.combat.apply_daowen_effect("杀伐", DaoWenEngine.resolve("杀伐", 5), m2, p2)
    out["daowen_heal"] = None
    out["hp_after_daowen"] = p2.current_hp
    # 与千荆甲同时持有：反噬伤害不计活血（反噬不是"受到攻击造成的伤害"）
    e3 = _sandbox(tmp, ["活血衣", "千荆甲"])
    p3, m3 = e3.state.player, _monster(e3)
    p3.current_hp = 100
    m3.current_hp = m3.blood_limit = 20
    healed_before3 = p3.total_healed
    r3 = _attack(e3, m3, p3)
    out["with_qianjingjia"] = {"damage_to_player": r3.get("damage_dealt", 0),
                               "heal": p3.total_healed - healed_before3,
                               "attacker_hp_after": m3.current_hp,
                               "player_hp_after": p3.current_hp}
    return out


# ==================== 5. 增生药剂 ====================

def case_zengsheng(tmp: Path) -> dict:
    e = _sandbox(tmp, ["增生药剂"])
    p = e.state.player
    e._grant_named_daowen(p, "连击")
    p.current_hp = 100
    hp_before = p.current_hp
    e.combat.apply_daowen_effect("连击", DaoWenEngine.resolve("连击", 3), p, p)
    out = {
        "case": "增生药剂：玩家对自己造成伤害（连击3打自己）",
        "hp_before": hp_before, "hp_after": p.current_hp,
        "self_damage": 3, "relic_heal_expected": 30,
        "heal_records": [ev.get("subtype") for ev in getattr(p, "_hp_loss_events", [])],
    }
    # 对照组：无遗物
    e2 = _sandbox(tmp, [])
    p2 = e2.state.player
    e2._grant_named_daowen(p2, "连击")
    p2.current_hp = 100
    e2.combat.apply_daowen_effect("连击", DaoWenEngine.resolve("连击", 3), p2, p2)
    out["hp_after_without_relic"] = p2.current_hp
    out["delta_with_relic"] = p.current_hp - hp_before
    out["delta_without_relic"] = p2.current_hp - 100
    # 受到攻击不触发（攻击者≠自己）
    e3 = _sandbox(tmp, ["增生药剂"])
    p3, m3 = e3.state.player, _monster(e3, atk=1, ap=10)
    p3.current_hp = 100
    r3 = _attack(e3, m3, p3)
    out["attack_no_trigger"] = {"hp_after": p3.current_hp,
                                "heal": r3.get("zengsheng_heal")}
    return out


# ==================== 6. 组合：千荆甲 + 癫狂之脑 ====================

def case_combo(tmp: Path) -> dict:
    e = _sandbox(tmp, ["千荆甲", "癫狂之脑"])
    p, m = e.state.player, _monster(e, atk=1, ap=10)
    m.current_hp = m.blood_limit = 500
    p.current_hp = 300
    r = _attack(e, m, p)
    return {
        "case": "组合：千荆甲+癫狂之脑（反噬按翻倍后数值等量反射）",
        "actual_damage": r.get("actual_damage"),
        "attacker_hp_after": m.current_hp,
        "player_hp_after": p.current_hp,
    }


CASES = [case_qianjingjia, case_wanjunyin, case_diankuang,
         case_huoxueyi, case_zengsheng, case_combo]


def main() -> int:
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--json", default="")
    args = ap_.parse_args()
    results = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for fn in CASES:
            try:
                results.append(fn(tmp))
            except Exception as exc:  # noqa: BLE001 - 探针要如实报告异常
                results.append({"case": fn.__name__, "ERROR": repr(exc)})
    for r in results:
        print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str),
                                   encoding="utf-8")
        print(f"\n[写出] {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
