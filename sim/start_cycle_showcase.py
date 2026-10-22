#!/usr/bin/env python3
"""开始一次轮回：真实引擎开局 + 真实战斗结算，逐时点展示本轮实现的『新反应法术』实战触发。

与“只解析 DSL”不同，这里用 GameEngine 真实开局并装备本轮实现的反应法术，随后在真实
CombatEngine 里逐时点触发。每一步都用引擎真实返回（spell_logs / reaction_logs /
_hp_loss_events）佐证触发，而非手写台词：
  ① 敌方【非普攻道纹伤害】→ 失去生命后（自动反应）
  ② 持有者自付【流血】代价      → 失去生命后（自动反应，连击类不耗法力）
  ③ 敌方【普攻】命中            → resolve_attack 反应窗口（受到伤害前 / 失去生命前 /
                                     受到伤害后 / 失去生命后）
  ④ 持有者【闪避】成功          → 「闪避时」（EXTRA_TRIGGERS 开放时点）

用法：python3 sim/start_cycle_showcase.py [--seed N]
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance, Spell
from tests.setup_support import finish_initial_daowen
from sim.optional_actions import start_battle, start_round

REACTION_DAOWEN = ("杀伐", "庇护", "再生", "连击", "坠落")
# (name, required_daowen, 触发时点, 效果流, 是否内置)
REACTION_SPELLS: list[tuple[str, list[str], str, str, bool]] = [
    ("先发制人", ["杀伐"], "受到伤害前", "发动杀伐 X于攻击者", True),
    ("后发制人", ["庇护"], "受到伤害前", "发动庇护 X于自身", True),
    ("亡语", ["杀伐"], "失去生命前", "发动杀伐 X于攻击者", False),
    ("护佑", ["庇护"], "受到伤害后", "发动庇护 X于自身", False),
    ("生生不息", ["再生"], "失去生命后", "发动再生 X于自身", True),
    ("以牙还牙", ["再生", "杀伐"], "失去生命后", "发动杀伐 X于攻击者", True),
    ("借力打力", ["庇护", "杀伐"], "受到伤害前", "发动庇护 X于自身", True),
    ("不死不休", ["连击"], "失去生命后", "发动连击 X于攻击者", True),
    ("千刀万剐", ["再生", "连击"], "失去生命后", "发动再生 X于自身", True),
    ("咎由自取", ["坠落", "杀伐", "连击"], "目标发动道纹前", "发动杀伐 X于攻击者", False),
]


def _grant_daowen(p) -> None:
    for name in REACTION_DAOWEN:
        p.dao_wen[name] = DaoWenInstance(
            DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""),
            x_value=0)


def _set_spells(p, names) -> None:
    p.spells = [Spell(name=name, required_daowen=list(req), trigger_condition=trig, effect_flow=flow)
                for name, req, trig, flow, _ in REACTION_SPELLS if name in names]


def _hdr(s: str) -> None:
    print(f"\n── {s} " + "─" * max(0, 40 - len(s)))


def _dump(logs, indent="      ") -> None:
    for lg in logs or []:
        if lg.get("execution"):
            print(f"{indent}⚡ {lg['spell']}（{lg.get('daowen')} → {lg.get('target')}, X={lg.get('x')}）")


def _ev_logs(p) -> list:
    return [lg for ev in (getattr(p, "_hp_loss_events", []) or []) for lg in (ev.get("reaction_logs") or [])]


def _setup_engine(seed: int):
    engine = GameEngine(db_path=tempfile.mktemp(suffix=".db"), rng_seed=seed,
                        sealed_candidate_path="/tmp/cycle_showcase_seal.json")
    engine.execute_action("setup_attributes", {"name": "玄夜", "blood_points": 10,
                                                "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(engine, prefer="杀伐")
    engine.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = engine.execute_action("setup_choose_region", {"region": "扭曲都市"})
    if setup.get("success") and setup.get("result", {}).get("relic_choices"):
        engine.execute_action("choose_discovered_relic",
                              {"relic_name": setup["result"]["relic_choices"][0]})
    p = engine.state.player
    _grant_daowen(p)
    p.blood_limit = 120
    p.current_hp = 120
    p.mana_limit = 80
    p.current_mana = 80
    while engine.state.energy > 0:
        r = engine.execute_action("pre_battle_action", {"sub_action": "修行", "tier": 1})
        if not r.get("success"):
            break
    return engine, p


def _step_cycle(trg, x) -> dict:
    """构造一条显式结算步。"""
    return {"x": x, "target_ref": trg, "dodge": False}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    engine, p = _setup_engine(args.seed)
    combat = engine.combat

    print(f"『第 1 次轮回 · 战局』（真实引擎结算）——{p.name}（{p.entity_type}）")
    _hdr("本轮实现的反应法术（可在后续对战里被持有者装备）")
    for name, req, note, _, _ in REACTION_SPELLS:
        print(f"  · {name}（{'/'.join(req)}）—— {note}")
    print(f"\n持有者面板：血 {p.current_hp}/{p.blood_limit} · 法 {p.current_mana} · 速 {p.current_speed}")

    bs, _ = start_battle(engine)
    if not bs.get("success"):
        print("战始失败:", bs.get("error")); return
    rs, _ = start_round(engine)
    if not rs.get("success"):
        print("回始失败:", rs.get("error")); return

    enemy = engine.state.enemies[0]
    enemy.attack_power = 12
    enemy.speed_limit = enemy.current_speed = 3
    print(f"对手：【{enemy.name}】血 {enemy.current_hp} · 攻 {enemy.attack_power}")
    refs = combat._combat_entity_refs()

    def dctx(actor, source="杀伐", st="daowen", mechanic="damage", sub="daowen", amount=0):
        return {"timing": "monster_action", "source": source, "source_type": st,
                "actor": actor, "target": p, "mechanic": mechanic, "subtype": sub,
                "amount": amount, "tags": {st, "showcase"}}

    # ① 非攻击道纹伤害 → 失去生命后（自动反应）
    _hdr("① 敌方【道纹·连击X=10】命中持有者（非普攻伤害 → 自动反应）")
    _set_spells(p, ["生生不息"])
    p.current_mana = 30
    before = p.current_hp
    detail = combat._apply_hostile_damage(p, 10, source=enemy, ctx=dctx(enemy, "连击", "daowen", "damage", "daowen", 10))
    print(f"    扣血 {detail.get('actual_damage')}（{before}→{p.current_hp}）→ 失去生命后触发：")
    _dump(detail.get("reaction_logs"))
    _dump(_ev_logs(p))

    _hdr("①b 敌方【道纹】命中 → 失去生命后（连击类不耗法力，0法力也触发）")
    _set_spells(p, ["不死不休"])
    p.current_mana = 0
    before = p.current_hp = 100
    combat._apply_hostile_damage(p, 10, source=enemy, ctx=dctx(enemy, "杀伐", "daowen", "damage", "daowen", 10))
    print(f"    扣血 10（100→{p.current_hp}）→ 触发：")
    _dump(_ev_logs(p))
    print(f"    敌被反击：{enemy.current_hp}（原 {enemy.blood_limit}）")

    # ② 自付流血代价 → 失去生命后
    _hdr("② 持有者自付【流血X=5】代价（连击类）")
    _set_spells(p, ["不死不休"])
    before = p.current_hp
    combat.pay_numeric_cost(p, "流血", 5, cost_context=dctx(p, "连击", "daowen", "cost", "bleed", 5))
    print(f"    扣血 {before - p.current_hp}（{before}→{p.current_hp}）→ 触发：")
    _dump(_ev_logs(p))

    # ③ 敌方普攻命中 → resolve_attack 四窗口显式提交
    _hdr("③ 敌方普攻命中持有者（resolve_attack 反应窗口）")
    _set_spells(p, ["先发制人", "亡语", "护佑", "生生不息"])
    p.current_hp = 90
    p.current_mana = 200
    before = p.current_hp
    sc = {"before": {}, "life_before": {}, "damage_after": {}, "after": {}}
    sc["before"]["先发制人"] = {"use": True, "steps": [_step_cycle("enemy:0", 4)], "max_iterations": 1}
    sc["life_before"]["亡语"] = {"use": True, "steps": [_step_cycle("enemy:0", 2)], "max_iterations": 1}
    sc["damage_after"]["护佑"] = {"use": True, "steps": [_step_cycle("player:0", 15)], "max_iterations": 1}
    sc["after"]["生生不息"] = {"use": True, "steps": [_step_cycle("player:0", 25)], "max_iterations": 1}
    res = combat.resolve_attack(enemy, p, dodge=False, spell_choices=sc, entity_refs=refs)
    print(f"    命中扣血 {res.get('hp_lost')}（{before}→{p.current_hp}）盾={p.shield} → 窗口触发：")
    _dump(res.get("spell_logs"))

    # ④ 成功闪避 → 「闪避时」（EXTRA_TRIGGERS 开放时点）
    _hdr("④ 持有者成功闪避 → 「闪避时」（EXTRA_TRIGGERS 开放时点）")
    p.spells = [Spell(name="护佑·庇护", required_daowen=["庇护"],
                      trigger_condition="闪避时", effect_flow="发动庇护 X于自身")]
    p.current_speed = 2
    p.speed_limit = 2
    p.current_mana = 10
    dod = combat.resolve_attack(enemy, p, dodge=True, spell_choices={"before": {}, "after": {}},
                                entity_refs=refs)
    print(f"    闪避成功 speed_after={dod.get('speed_after_dodge')} 盾={p.shield} → 触发：")
    _dump(dod.get("spell_logs"))

    print(f"\n[回合结束] 玄夜 hp={p.current_hp}/{p.blood_limit} 法={p.current_mana} 盾={p.shield}"
          f" | 敌={[(m.name, m.current_hp) for m in engine.state.enemies if m.is_alive]}")


if __name__ == "__main__":
    main()
