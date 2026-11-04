"""胜利路径锦标赛（2026-08-22 DM裁定：把各种胜利方法的利弊和胜率都列出来）。

同种子配对（种子1..N），每行一条清场路径，全部走完7场战斗：
  指标 = 均通关场数 / 第1战死亡率 / 进终局率 / 死斗触发·胜率 / 完整轮回率 / 清场构成。
清场构成由 build_learner._alt_victory_scan 统计（命零类走事件流，离场类走旗标差集）。

收割技巧（本届新增知识）：副本专属道纹与杀伐闭环互不连通，唯一入口是
对【怪物持有的专属道纹】施加残韵——持有人变化的同时玩家获得成果道纹
（api.py use_resonance → _grant_transformed_daowen），随后学习门禁解锁
（api.py:1638）。还债流/雕塑流 AI 即按此打造。
"""
from __future__ import annotations

# --- 2026-11-04：脚本放进子目录后，不再靠"往上数几层"猜仓库根 ---
# 向上找到同时含 engine/ 与 sim/ 的目录就是仓库根；再附带 sim/ 让老式裸模块名
# import（如 `import balance_sim`）继续可用。搬动本文件不需要再改这几行。
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.abspath(__file__))
while _ROOT != _os.path.dirname(_ROOT) and not _os.path.isdir(_os.path.join(_ROOT, "engine")):
    _ROOT = _os.path.dirname(_ROOT)
for _p in (_ROOT, _os.path.join(_ROOT, "sim"), _os.path.join(_ROOT, "tests")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)


import json
import sys
import time


from engine.ai_tactics import TacticalAI, monster_threat          # noqa: E402
from sim.build_learner import _skip_resource_timeout, play, DEFAULT_POLICY                # noqa: E402

SEEDS = range(1, 121)
BATTLES = 7


# --------------------------------------------------------------------------
# 路径专用 AI
# --------------------------------------------------------------------------
class HarvestMixin:
    """收割：对怪物持有的源道纹施加残韵，玩家获得目标道纹（跨环入口）。"""

    def try_harvest(self, plans):
        stock = {k: v for k, v in self.engine.state.resonance.items() if v > 0}
        if not stock or not self.player:
            return None
        held = set(self.player.dao_wen)
        for enemy in self.alive_enemies():
            if enemy.entity_type != "怪物" or not enemy.is_alive:
                continue
            for src, rtype, dest in plans:
                if dest in held or src not in enemy.dao_wen:
                    continue
                if stock.get(rtype, 0) <= 0:
                    continue
                r = self.engine.execute_action("use_resonance", {
                    "source_daowen": src, "resonance_type": rtype,
                    "target": enemy.name})
                if r.get("success"):
                    self.used[f"收割·{dest}"] = self.used.get(f"收割·{dest}", 0) + 1
                    self.resolve_pending_redemption()
                    return r
        return None


class StallNoDmgAI(TacticalAI):
    """纯憋流：永不伤害（对照组——记录玩家侧凡庸自爆陷阱）。"""

    STRATEGIES = ("try_artifact", "try_survive", "try_buff", "try_resonance",
                  "try_control", "try_ramp", "try_reroll", "try_consumable")


class PokeStallAI(TacticalAI):
    """凡庸混合：苟活+控制优先，每轮仅蹭一刀 X=1 重置自身凡庸计数，绝不爆发。"""

    def try_poke(self):
        enemies = self.alive_enemies()
        if not enemies:
            return None
        for name in self.owned("nuke"):
            r = self._cast(name, 1, enemies[0].name)
            if r:
                return r
        return None

    STRATEGIES = ("try_artifact", "try_survive", "try_buff", "try_resonance",
                  "try_control", "try_debuff", "try_poke", "try_ramp",
                  "try_reroll", "try_consumable")


class SealAI(TacticalAI):
    """封印手术流：异变预算 34（崩解50留16余量），仅对致命威胁先波及标记再封印。"""

    MUTATION_BUDGET = 34

    def _held(self, name):
        return self.player is not None and name in self.player.dao_wen

    def _mark(self, threat):
        refs = self.engine.combat._combat_entity_refs()
        ref = next((r for r, ent in refs.items() if ent is threat), None)
        if ref is None:
            return None
        p = {"daowen_name": "波及", "x": 1, "dodge": False, "blood_shadow": False,
             "dodge_targets": [{"target_ref": ref, "dodge": False, "blood_shadow": False}],
             "target_ref": ref, "target": threat.name}
        r = self.engine.execute_action("use_daowen", p)
        return r if r.get("success") else None

    def try_seal_surgical(self):
        p = self.player
        if not p or not self._held("封印") or p.mutation_count > self.MUTATION_BUDGET:
            return None
        enemies = [en for en in self.alive_enemies() if en.entity_type == "怪物"]
        if not enemies:
            return None
        threat = max(enemies, key=monster_threat)
        lethal = (self.incoming_damage() > p.current_hp * 0.6
                  or threat.attack_power * max(1, threat.attack_count) >= p.blood_limit * 0.5)
        if not lethal:
            return None
        if self._held("波及") and self.mana() >= 3 and not any(
                s.name == "波及" and s.source == p.name and not s.is_expired
                for s in threat.status_effects):
            if self._mark(threat):
                return {"success": True}
        return self._cast("封印", 1, threat.name)

    STRATEGIES = ("try_artifact", "try_survive", "try_seal_surgical", "try_buff",
                  "try_resonance", "try_finish", "try_remove", "try_control",
                  "try_aoe", "try_debuff", "try_pressure", "try_ramp",
                  "try_reroll", "try_consumable")


class FeedAI(TacticalAI):
    """癌变供养流：给血限最低的怪物灌再生，累计恢复达血限×2 即癌变被吸收。"""

    def try_feed(self):
        if not self.player or "再生" not in self.player.dao_wen:
            return None
        enemies = [en for en in self.alive_enemies() if en.entity_type == "怪物"]
        if not enemies:
            return None
        tgt = min(enemies, key=lambda en: (en.total_healed * 1.0 / max(1, en.blood_limit), en.blood_limit))
        # 喂最接近癌变线的；都不近就喂血限最低的
        tgt = min(enemies, key=lambda en: en.blood_limit * 2 - en.total_healed)
        need = tgt.blood_limit * 2 - tgt.total_healed
        if need <= 0:
            return None
        x = min(self.mana(), max(1, -(-need // 3)))   # heal_per_x=3
        if x < 1:
            return None
        return self._cast("再生", x, tgt.name)

    def try_poke(self):                                   # 其余怪用最小输出清掉
        enemies = [en for en in self.alive_enemies() if en.entity_type == "怪物"]
        feed_targets = [en for en in enemies if en.total_healed * 2 < en.blood_limit]
        others = [en for en in enemies if en not in feed_targets]
        pool = others or []
        if not pool:
            return None
        for name in self.owned("nuke"):
            r = self._cast(name, 1, pool[0].name)
            if r:
                return r
        return None

    STRATEGIES = ("try_artifact", "try_survive", "try_buff", "try_resonance",
                  "try_feed", "try_control", "try_poke", "try_ramp",
                  "try_reroll", "try_consumable")


class DebtAI(HarvestMixin, TacticalAI):
    """还债流：收割赎金(清算+反转)抽干怪物碎片。

    2026-10-13：【逼债】删除，「挂永久催缴逼到碎片≤-10」的负债驱动已不存在；
    本流派退化为赎金碎片压制（还债仍可由怪物碎片代价透支触发，见 test_debt_overdraft）。
    """

    PLANS = [("清算", "反转", "赎金")]

    def try_harvest_debt(self):
        return self.try_harvest(self.PLANS)

    def try_debt(self):
        if not self.player:
            return None
        held = set(self.player.dao_wen)
        enemies = [en for en in self.alive_enemies() if en.entity_type == "怪物"]
        if not enemies:
            return None
        if "赎金" in held and self.mana() >= 10:
            tgt = min(enemies, key=lambda en: en.shards)
            if tgt.shards <= 5:                       # 已有去碎片趋势再加速
                return self._cast("赎金", 1, tgt.name)
        return None

    # 注意：收割类 AI 不能保留通用 try_resonance——残韵库存是全局最稀缺资源，
    # 通用残韵会把库存烧在高价值敌方道纹上，导致收割永远等不到目标类型（v1 教训）。
    STRATEGIES = ("try_artifact", "try_harvest_debt", "try_survive", "try_buff",
                  "try_debt", "try_finish", "try_remove",
                  "try_control", "try_aoe", "try_debuff", "try_pressure",
                  "try_ramp", "try_reroll", "try_consumable")


class SculptAI(HarvestMixin, TacticalAI):
    """雕塑流：收割 无力(疯狂+反转)，把头号威胁出手次数归零→回终化雕塑(耐久=血限5%)。"""

    PLANS = [("疯狂", "反转", "无力")]

    def try_harvest_sculpt(self):
        return self.try_harvest(self.PLANS)

    def try_sculpt(self):
        if not self.player or "无力" not in self.player.dao_wen:
            return None
        enemies = [en for en in self.alive_enemies() if en.entity_type == "怪物"
                   and en.attack_count > 0]
        if not enemies:
            return None
        threat = max(enemies, key=monster_threat)
        x = min(threat.attack_count, self.mana() // 10)
        if x < 1:
            return None
        return self._cast("无力", x, threat.name)

    STRATEGIES = ("try_artifact", "try_harvest_sculpt", "try_survive", "try_buff",
                  "try_sculpt", "try_finish", "try_remove",
                  "try_control", "try_aoe", "try_debuff", "try_pressure",
                  "try_ramp", "try_reroll", "try_consumable")


class RedeemAI(TacticalAI):
    """救赎雇员流：先剥怪物原始道纹（残韵），残血压到≤血限10%触发救赎并接纳为员工。"""

    ORIGINALS = ("狂暴", "全力", "疯狂", "减速", "必中", "自愈", "飞行")
    ALLY_CAP = 2

    def try_strip(self):
        stock = {k: v for k, v in self.engine.state.resonance.items() if v > 0}
        if not stock:
            return None
        for enemy in self.alive_enemies():
            if enemy.entity_type != "怪物":
                continue
            for src in self.ORIGINALS:
                if src not in enemy.dao_wen:
                    continue
                from engine.daowen import ResonanceEngine
                for path in ResonanceEngine.get_available_resonance(src):
                    rt = path.get("resonance_type")
                    if rt and stock.get(rt, 0) > 0:
                        r = self.engine.execute_action("use_resonance", {
                            "source_daowen": src, "resonance_type": rt,
                            "target": enemy.name})
                        if r.get("success"):
                            return r
        return None

    def resolve_pending_redemption(self, option: str = "终结"):
        st = self.engine.state
        n_allies = len([f for f in st.friends + st.employees if f.is_alive])
        if n_allies < self.ALLY_CAP:
            option = "接纳"
        return super().resolve_pending_redemption(option)

    STRATEGIES = ("try_artifact", "try_strip", "try_survive", "try_buff",
                  "try_resonance", "try_finish", "try_remove", "try_control",
                  "try_aoe", "try_debuff", "try_pressure", "try_ramp",
                  "try_reroll", "try_consumable")


# --------------------------------------------------------------------------
# 锦标赛配置
# --------------------------------------------------------------------------
ROWS = [
    ("伤害基准@扭曲",   "扭曲都市", "庇护", ["杀伐", "再生", "贯穿"], TacticalAI,  {}),
    ("凡庸混合@扭曲",   "扭曲都市", "庇护", ["束缚", "杀伐", "再生"], PokeStallAI, {}),
    ("纯憋流@扭曲",     "扭曲都市", "庇护", ["束缚", "再生", "定型"], StallNoDmgAI, {}),
    ("封印手术@扭曲",   "扭曲都市", "庇护", ["封印", "波及", "再生"], SealAI,      {}),
    ("癌变供养@扭曲",   "扭曲都市", "庇护", ["再生", "杀伐", "透支"], FeedAI,      {}),
    ("还债流@罪孽",     "罪孽都市", "庇护", ["束缚", "杀伐", "再生"], DebtAI,
     {"relic_policy": "prefer_optional", "resonance": "反转",
      "policy": {**DEFAULT_POLICY, "共鸣": 30}}),   # 原 "领悟":30，【领悟】已于2026-09-10删除
    ("雕塑流@罪孽",     "罪孽都市", "庇护", ["束缚", "杀伐", "再生"], SculptAI,
     {"relic_policy": "prefer_optional", "resonance": "反转",
      "policy": {**DEFAULT_POLICY, "雇佣": 30, "共鸣": 25}}),   # 原含 "领悟":25，已删除
    ("救赎朋友流@扭曲", "扭曲都市", "庇护", ["杀伐", "再生", "束缚"], RedeemAI,    {}),
    ("伤害基准@罪孽",   "罪孽都市", "庇护", ["杀伐", "再生", "贯穿"], TacticalAI,  {}),
]


def run_row(label, region, starter, learn, ai_cls, kw):
    tot = {"n": 0, "invalid": 0, "cleared": 0.0, "won": 0, "b1_death": 0,
           "reach7": 0, "duel_fought": 0, "duel_won": 0, "sealed7": 0}
    alt = {}
    t0 = time.time()
    for seed in SEEDS:
        telemetry = {}
        r = play(starter, list(learn), region, seed=seed, battles=BATTLES,
                 telemetry=telemetry, ai_cls=ai_cls, **kw)
        if _skip_resource_timeout(r, context=__name__):
            continue
        if r.get("invalid"):
            tot["invalid"] += 1
            continue
        tot["n"] += 1
        tot["cleared"] += r["cleared"]
        if r["cleared"] == 0:
            tot["b1_death"] += 1
        if r["cleared"] >= BATTLES - 1:
            tot["reach7"] += 1
        if r.get("won"):
            tot["won"] += 1
        duels = (telemetry.get("duels") or {})
        tot["duel_fought"] += duels.get("fought", 0)
        tot["duel_won"] += duels.get("won", 0)
        tot["sealed7"] += duels.get("sealed_no_duel", 0)
        for k, v in (telemetry.get("alt_victory") or {}).items():
            alt[k] = alt.get(k, 0) + v
    n = max(1, tot["n"])
    return {
        "label": label, "region": region, "ai": ai_cls.__name__,
        "valid": tot["n"], "invalid": tot["invalid"],
        "avg_cleared": round(tot["cleared"] / n, 2),
        "b1_death_pct": round(100 * tot["b1_death"] / n, 1),
        "reach7": tot["reach7"],
        "duel_fought": tot["duel_fought"], "duel_won": tot["duel_won"],
        "sealed7": tot["sealed7"],
        "cycle_pct": round(100 * tot["duel_won"] / n, 1),
        "alt_victory": dict(sorted(alt.items(), key=lambda kv: -kv[1])),
        "seconds": round(time.time() - t0, 1),
    }


def main():
    import os
    only = os.environ.get("VT_ROWS", "")
    out_file = os.environ.get("VT_OUT", "/tmp/vt_results.json")
    out = []
    for label, region, starter, learn, ai_cls, kw in ROWS:
        if only and label not in only:
            continue
        row = run_row(label, region, starter, learn, ai_cls, kw)
        out.append(row)
        print(f"[done] {label}: avg={row['avg_cleared']} b1={row['b1_death_pct']}% "
              f"duel={row['duel_won']}/{row['duel_fought']} alt={row['alt_victory']} "
              f"({row['seconds']}s)", flush=True)
    json.dump(out, open(out_file, "w"), ensure_ascii=False, indent=1)
    print("\n===== 胜利路径锦标赛（同种子配对 n=" + str(len(list(SEEDS))) + "）=====")
    hdr = f"{'路径':<14}{'均通关':>7}{'1战死%':>8}{'进终局':>7}{'死斗':>10}{'轮回率%':>8}  清场构成"
    print(hdr)
    for r in out:
        alt = " ".join(f"{k}×{v}" for k, v in r["alt_victory"].items())
        print(f"{r['label']:<14}{r['avg_cleared']:>7}{r['b1_death_pct']:>8}"
              f"{r['reach7']:>7}{str(r['duel_won'])+'/'+str(r['duel_fought']):>10}"
              f"{r['cycle_pct']:>8}  {alt}  [无效{r['invalid']}]")


if __name__ == "__main__":
    main()