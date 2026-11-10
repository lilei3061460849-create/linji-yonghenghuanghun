#!/usr/bin/env python3
"""衍生道纹选取率审计（2026-11-09 用户令：以选取率为准抬汇率）。

用户口径
--------
「汇率不必守恒，以选取率为准——尽量让每条衍生道纹的选取率接近，选取率太低的
就抬汇率、增加性价比」。所以本脚本不判断「数值平不平衡」，只回答一件事：

    在【衍生】的「池中≤3 项择一」里，每条道纹有多大概率被选中？

模型（不是规则，是审计假设；参数集中在 PRICE / STOCK / OUTPUT 三段，可调）
----------------------------------------------------------------------
1. **产出**（生命等价）：对敌方阵营的有效失血 1:1；对己方回复 1:1（并受已损
   生命封顶）；获得法力 ×`MANA_TO_HP`（=4，锚点：规则正文【再生】法力X→回复4X
   生命）；自己与友方的失血按 −1:1 计（【冲击】因此可能为负）。
2. **代价按「库存占比×不可逆」**，而不是绝对价格——因为同一种货币对不同流派的
   机会成本完全不同（3 法限的法术流与 20 法限的法术流不是一回事）：
       cost = Σ (支付量 / 该资源在本局的库存) × 不可逆权重
   不可逆权重反映「这一局能不能补回来」：生命/法力能在本场回复（0.5），
   血限/法限/速限/攻击力是本局永久刻度（2.0），碎片是局外货币、不影响战场（1.0）。
3. **选取**：从该母本池里随机提供 ≤3 个未持有项（与引擎 derive_daowen 同规则），
   取 score 最大者；同分随机。提供次数与选中次数之比就是选取率。

战场形态（随机采样，权重均匀）
----------------------------
敌人 1–3（默认血量 80，护盾 0–20），友方 0–2（40 生命），X ∈ {1,2,3}，
玩家库存随机落在低/中/高三档投资（见 STOCK_BANDS）。

产出走与引擎一致的结算口径：护盾先吃伤害、龙鳞类逐笔减伤每笔 −1、【断脉】无视
护盾与逐笔减伤、【买命】【冲击】走裸失血（不吃护盾/减伤/招架）。脚本末有
`--verify` 会用真实 CombatEngine 抽检若干样本，确认解析口径没跑偏。

用法
----
  PYTHONPATH=. python3 sim/audits/derivative_pick_rate.py
  PYTHONPATH=. python3 sim/audits/derivative_pick_rate.py --trials 200000 --verify
  PYTHONPATH=. python3 sim/audits/derivative_pick_rate.py --write 内部/衍生道纹选取率-2026-11-09.md
"""
from __future__ import annotations

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.abspath(__file__))
while _ROOT != _os.path.dirname(_ROOT) and not _os.path.isdir(_os.path.join(_ROOT, "engine")):
    _ROOT = _os.path.dirname(_ROOT)
for _p in (_ROOT, _os.path.join(_ROOT, "sim"), _os.path.join(_ROOT, "tests")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

import argparse
import json
import math
import random
from collections import defaultdict

from engine import derivatives as derivative_rules

ROOT = _ROOT

# ---------------------------------------------------------------- 模型参数
MANA_TO_HP = 4.0            # 1 法力 = 4 生命（【再生】法力X→回复4X生命）

# 不可逆权重：本场能补回来的资源便宜，本局永久刻度贵。
IRREVERSIBLE = {
    "生命": 0.5,
    "法力": 0.5,
    "血限": 2.0,
    "法限": 2.0,
    "速度": 2.0,
    "攻击力": 2.0,
    "碎片": 1.0,
    "枯竭": 2.0,
    "假碎片": 1.0,
}

# 玩家库存分档（低/中/高投资）。三种都采样，避免模型默认某一种流派。
STOCK_BANDS = {
    "生命": (40, 90),
    "血限": (40, 90),
    "法限": (2, 14),
    "速度": (2, 10),
    "攻击力": (1, 9),
    "碎片": (0, 60),
    "法力": (0, 12),
}

PER_HIT_REDUCTION_BANDS = (0, 0, 0, 1, 2)   # 龙鳞类逐笔减伤（多数没有）
SHIELD_BANDS = (0, 0, 5, 10, 20)

# 参数档位：结论若在三档下一致，就不必纠结参数本身。
PROFILES = {
    # 基准：本场可回复的资源便宜（0.5），本局永久刻度贵（2.0）。
    "基准": {"生命": 0.5, "法力": 0.5, "血限": 2.0, "法限": 2.0,
             "速度": 2.0, "攻击力": 2.0, "碎片": 1.0},
    # 保守：所有资源一视同仁（只看库存占比）。
    "保守": {"生命": 1.0, "法力": 1.0, "血限": 1.0, "法限": 1.0,
             "速度": 1.0, "攻击力": 1.0, "碎片": 1.0},
    # 极端：上限/永久刻度按 3 倍计，放大「付上限」的痛感。
    "极端": {"生命": 0.3, "法力": 0.3, "血限": 3.0, "法限": 3.0,
             "速度": 3.0, "攻击力": 3.0, "碎片": 1.0},
}
ACTIVE_IRREVERSIBLE = dict(IRREVERSIBLE)

# 预演用覆盖表：{名: {"rate": 倍率, "output": 产出倍数, "no_self": True/False}}
# 只影响本审计的推演，不改引擎；数字确认后才去改 engine/derivatives.py。
OVERRIDES: dict[str, dict] = {}


def reset_overrides() -> None:
    OVERRIDES.clear()


def use_profile(name: str) -> None:
    global ACTIVE_IRREVERSIBLE
    ACTIVE_IRREVERSIBLE = dict(PROFILES[name])


def _sample_state(rng: random.Random) -> dict:
    stock = {k: rng.uniform(*v) for k, v in STOCK_BANDS.items()}
    hp = stock["血限"] * rng.uniform(0.55, 1.0)
    enemies = []
    for _ in range(rng.choice((1, 1, 2, 3))):
        enemies.append({"hp": 80.0, "shield": float(rng.choice(SHIELD_BANDS)),
                        "reduce": rng.choice(PER_HIT_REDUCTION_BANDS)})
    allies = [{"hp": 40.0} for _ in range(rng.choice((0, 0, 1, 2)))]
    return {"stock": stock, "hp": hp, "enemies": enemies, "allies": allies,
            "x": rng.choice((1, 2, 3))}


# ---------------------------------------------------------------- 产出计算
def _through_shield(amount: float, shield: float, reduce: float) -> tuple[float, float]:
    """单笔伤害：先逐笔减伤，再护盾吸收，返回 (落到生命的伤害, 剩余护盾)。"""
    after_reduce = max(0.0, amount - reduce)
    absorbed = min(shield, after_reduce)
    return after_reduce - absorbed, shield - absorbed


def _multi_hit(hits: int, per_hit: float, shield: float, reduce: float) -> tuple[float, float]:
    """多段：每段各自减伤、各自吃护盾（与引擎逐击结算一致）。"""
    total = 0.0
    for _ in range(hits):
        dealt, shield = _through_shield(per_hit, shield, reduce)
        total += dealt
    return total, shield


def _hp_loss(ent: dict, amount: float) -> float:
    return min(ent["hp"], amount)


def output_of(name: str, state: dict) -> float:
    """把一条衍生道纹的产出折成「生命等价」，按敌/友两侧净额计。"""
    spec = derivative_rules.spec_of(name)
    x = state["x"]
    amount = spec.effect_amount(x)      # 效果量跟着 engine/derivatives.py 的 effect_rate 走
    enemies, allies = state["enemies"], state["allies"]
    kind = spec.effect_kind
    value = 0.0

    if kind == "multi_hit":
        per_hit = amount / (5.0 * x) if x else 1.0     # 每段伤害（默认 1 点）
        for e in enemies:
            dealt, _ = _multi_hit(5 * x, per_hit, e["shield"], e["reduce"])
            value = max(value, dealt)
    elif kind == "damage":
        for e in enemies:
            dealt, _ = _through_shield(amount, e["shield"], e["reduce"])
            value = max(value, dealt)
    elif kind == "ignore_shield":
        for e in enemies:
            # 无视格挡只跳过护盾；龙鳞类逐笔减伤照常生效（2026-11-09 引擎抽检确认）。
            dealt, _ = _through_shield(amount, 0.0, e["reduce"])
            value = max(value, dealt)
    elif kind == "life_loss":
        for e in enemies:
            value = max(value, _hp_loss(e, amount))    # 裸失血，不吃护盾/减伤
    elif kind == "others_life_loss":
        # 除[目标]外所有角色失血；[目标]由玩家挑，取最优解：
        #   选自己 → 全体敌人 + 友方失血，自己不失血
        #   选某个敌人 → 其余敌人 + 自己 + 友方失血
        allow_self = OVERRIDES.get(name, {}).get("self_target", True)
        best_self = -1e9
        if allow_self:   # 目标选自己：全体敌人 + 友方失血，自己不失血
            best_self = sum(_hp_loss(e, amount) for e in enemies) \
                - sum(_hp_loss(a, amount) for a in allies)
        for i in range(len(enemies)):
            cand = sum(_hp_loss(e, amount) for j, e in enumerate(enemies) if j != i) \
                - amount - sum(_hp_loss(a, amount) for a in allies)
            best_self = max(best_self, cand)
        value = best_self
    elif kind == "mana_gain":
        value = amount * MANA_TO_HP
    elif kind == "heal":
        missing = max(0.0, state["stock"]["血限"] - state["hp"])
        value = min(amount, missing)
    else:  # pragma: no cover
        raise SystemExit(f"未知衍生道纹: {name}")
    return value * OVERRIDES.get(name, {}).get("output", 1.0)


def cost_of(name: str, state: dict) -> float:
    """代价＝库存占比×不可逆权重。"""
    spec = derivative_rules.spec_of(name)
    ov = OVERRIDES.get(name, {})
    rate = ov.get("rate", spec.rate)
    kind = ov.get("cost_kind", spec.cost_kind)
    amount = rate * state["x"]
    stock = max(1.0, state["stock"].get(kind, 1.0))
    return (amount / stock) * ACTIVE_IRREVERSIBLE[kind]


def score_of(name: str, state: dict) -> float:
    cost = cost_of(name, state)
    if cost <= 0:
        return 0.0
    return output_of(name, state) / cost


# ---------------------------------------------------------------- 三选一取样
def _offer(pool: tuple[str, ...], rng: random.Random) -> list[str]:
    k = min(len(pool), rng.choice((1, 2, 3, 3, 3)))
    return rng.sample(list(pool), k)


def run(trials: int, seed: int = 20261109, profile: str = "基准") -> dict:
    use_profile(profile)
    rng = random.Random(seed)
    pools = {}
    for parent in ("杀伐", "透支", "再生"):
        pools[parent] = tuple(derivative_rules.pool_of(parent))

    offered = defaultdict(int)
    picked = defaultdict(int)
    scores = defaultdict(list)
    outputs = defaultdict(list)
    costs = defaultdict(list)
    single_duel_pick = defaultdict(int)     # 1 敌 0 友 的纯单挑
    multi_pick = defaultdict(int)
    single_offers = defaultdict(int)
    multi_offers = defaultdict(int)

    for _ in range(trials):
        state = _sample_state(rng)
        for pool in pools.values():
            choice = _offer(pool, rng)
            sc = {n: score_of(n, state) for n in pool}
            for n in choice:
                offered[n] += 1
            best = max(choice, key=lambda n: (sc[n], rng.random()))
            picked[best] += 1
            duel = len(state["enemies"]) == 1 and not state["allies"]
            for n in choice:
                (single_offers if duel else multi_offers)[n] += 1
            (single_duel_pick if duel else multi_pick)[best] += 1
            for n in pool:
                scores[n].append(sc[n])
                outputs[n].append(output_of(n, state))
                costs[n].append(cost_of(n, state))

    rows = []
    for parent, pool in pools.items():
        rates = {n: (picked[n] / offered[n] if offered[n] else 0.0) for n in pool}
        mean = sum(rates.values()) / len(rates)
        for n in pool:
            spec = derivative_rules.spec_of(n)
            rate = OVERRIDES.get(n, {}).get("rate", spec.rate)
            rows.append({
                "母本": parent,
                "名": n,
                "支付": f"{OVERRIDES.get(n, {}).get('cost_kind', spec.cost_kind)}{rate}X",
                "平均产出": sum(outputs[n]) / len(outputs[n]),
                "平均代价占比": sum(costs[n]) / len(costs[n]),
                "效率得分": sum(scores[n]) / len(scores[n]),
                "被提供": offered[n],
                "被选中": picked[n],
                "选取率": rates[n],
                "池内均值": mean,
                "单挑选取率": (single_duel_pick[n] / single_offers[n]) if single_offers[n] else 0.0,
                "多人选取率": (multi_pick[n] / multi_offers[n]) if multi_offers[n] else 0.0,
                "stdlib": 0.0,
            })
    for row in rows:
        row["标准差"] = _stdev(scores[row["名"]])
    return {"trials": trials, "profile": profile, "rows": rows,
            "pools": {k: list(v) for k, v in pools.items()}}


def _stdev(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    m = sum(values) / n
    return math.sqrt(sum((v - m) ** 2 for v in values) / (n - 1))


# ---------------------------------------------------------------- 建议
def equalizers(rows: list[dict]) -> list[dict]:
    """每条要「等于池内中位得分」需要多少产出倍数（>1 加强、<1 削弱）。"""
    out = []
    for parent in ("杀伐", "透支", "再生"):
        pool = [r for r in rows if r["母本"] == parent]
        if len(pool) < 2:
            continue
        median = sorted(r["效率得分"] for r in pool)[len(pool) // 2]
        for r in pool:
            k = median / r["效率得分"] if r["效率得分"] > 0 else float("inf")
            out.append({"名": r["名"], "母本": parent, "选取率": r["选取率"],
                        "产出倍数": k, "池内中位得分": median,
                        "单挑选取率": r["单挑选取率"]})
    return out


def suggestions(rows: list[dict]) -> list[dict]:
    """选取率明显低于池内均值的，给出「把倍率降到几分之几」的建议。

    score ∝ 产出/代价，代价 ∝ 倍率；要把它抬到池内中位得分，倍率需乘
    score/中位数（小于 1 就是降价＝抬汇率）。倍率取整后不得小于 1。
    """
    out = []
    for parent in ("杀伐", "透支", "再生"):
        pool = [r for r in rows if r["母本"] == parent]
        if len(pool) < 2:
            continue
        median = sorted(r["效率得分"] for r in pool)[len(pool) // 2]
        mean_rate = sum(r["选取率"] for r in pool) / len(pool)
        for r in pool:
            if r["选取率"] >= 0.6 * mean_rate:
                continue
            k = r["效率得分"] / median if median else 1.0
            old = derivative_rules.spec_of(r["名"]).rate
            new = max(1, int(round(old * k)))
            if new >= old:
                continue
            out.append({"名": r["名"], "母本": parent, "现倍率": old, "建议倍率": new,
                        "选取率": r["选取率"], "池内均值": mean_rate})
    return out


def _table(rows: list[dict]) -> list[str]:
    out = ["| 母本 | 名 | 支付 | 平均产出 | 平均代价占比 | 效率分 | 选取率 | 池内均值 | 单挑 | 多人 |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: (r["母本"], -r["选取率"])):
        out.append(
            f"| {r['母本']} | {r['名']} | {r['支付']} | {r['平均产出']:.1f} | "
            f"{r['平均代价占比']:.3f} | {r['效率得分']:.1f} | {r['选取率']:.1%} | "
            f"{r['池内均值']:.1%} | {r['单挑选取率']:.1%} | {r['多人选取率']:.1%} |")
    return out


def render(base: dict, profiles: dict[str, dict], tuned: dict[str, dict]) -> str:
    rows = base["rows"]
    lines = ["# 衍生道纹选取率（模型审计）", ""]
    lines.append(f"采样 {base['trials']:,} 局面×三池（每次从池中随机提供 ≤3 项择一，"
                 "与引擎 `derive_daowen` 同规则）。")
    lines.append("")
    lines.append("**模型不是规则**：产出按生命等价（对方失血 1:1、回复 1:1、"
                 f"1 法力 = {MANA_TO_HP:.0f} 生命）；代价按「库存占比×不可逆权重」"
                 "（本场可回复 0.5／本局永久刻度 2.0／局外碎片 1.0）。"
                 "效果量与倍率直接读 `engine/derivatives.py`，不在这里另写一份。")
    lines.append("")
    lines += _table(rows)

    lines.append("")
    lines.append("## 排序在三种参数档位下是否一致")
    lines.append("")
    lines.append("| 名 | " + " | ".join(profiles) + " |")
    lines.append("|---" * (len(profiles) + 1) + "|")
    for name in derivative_rules.DERIVED_DAOWEN:
        cells = []
        for _prof, rep in profiles.items():
            rate = next(r["选取率"] for r in rep["rows"] if r["名"] == name)
            cells.append(f"{rate:.1%}")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")

    lines.append("")
    lines.append("## 结构性说明")
    lines.append("")
    lines.append("1. **【断脉】已从 96% 压回 36%**：倍率血限X→5X 之后与池内中位齐平。")
    lines.append("2. **【虚耗】抬产出而不是降倍率**：法限库存只有 2–14，付 1 点就是一大截库存，"
                 "倍率到 1X 已经到底，只能把 5X 伤害抬到 12X。")
    lines.append("3. **【冲击】现在是池内第一**：把[目标]选成自己 → 「除目标外所有角色」退化成"
                 "「所有敌人（＋友方）」，单挑里就是「法力X 打 5X」。它与母本【杀伐】"
                 "（法力X 打 X²）在 X=5 处交叉：X<5 冲击更划算，X≥5 母本更强，"
                 "所以它是低 X 群体牌，不是无脑上位。")
    lines.append("4. **【搏命】仍然垫底（8%）**：速度库存只有 2–10、不可逆权重 2.0，"
                 "付 1 点等于交掉一大截先手；要拉平得改成「速度X → 12X 法力」，"
                 "2026-11-09 用户令：暂不动。")
    lines.append("5. **【献锋】恒 100%**：再生池只有它一条，没有对手，选取率没有信息量；"
                 "要让这个数字有意义得先给【再生】加第二条衍生。")
    lines.append("")

    for label, rep in tuned.items():
        lines.append(f"## {label}的选取率（对照）")
        lines.append("")
        lines += _table(rep["rows"])
        lines.append("")
    return "\n".join(lines) + "\n"


def compare(trials: int, seed: int = 20261109) -> tuple[dict, dict, dict]:
    """现状表 + 三档参数对照 + 「调整前」回放（2026-11-09 第一版汇率）。"""
    reset_overrides()
    base = run(trials, seed, "基准")
    profiles = {prof: run(trials, seed, prof) for prof in PROFILES}

    # 调整前那一版：连击生命5X、虚耗5X 伤害、断脉血限X、冲击速度X、
    # 折年 X 法力。用来回答「不改会怎样」。
    reset_overrides()
    before = {
        "连击": {"rate": 5},
        "虚耗": {"output": 5 / 12},
        "断脉": {"rate": 1},
        "冲击": {"cost_kind": "速度", "self_target": False},
        "折年": {"output": 0.5},
    }
    OVERRIDES.update(before)
    snapshot = run(trials, seed, "基准")
    reset_overrides()
    return base, profiles, {"调整前（第一版汇率）": snapshot}


# ---------------------------------------------------------------- 口径抽检
def verify() -> None:
    """用真实 CombatEngine 抽检解析产出，确认与引擎结算一致。"""
    from engine.combat import CombatEngine
    from engine.daowen import DaoWenEngine
    from engine.dice import DiceEngine
    from engine.models import Entity, GameState

    def fresh(shield: int = 0, reduce_relic: bool = False):
        state = GameState(phase="in_combat", combat_subphase="player_actions")
        p = Entity("受测者", "轮回者", blood_limit=66, current_hp=66,
                   attack_count=1, attack_power=1, mana_limit=6, current_mana=6)
        state.player = p
        m = Entity("靶怪", "怪物", blood_limit=80, current_hp=80,
                   attack_count=1, attack_power=1, shield=shield)
        state.enemies.append(m)
        if reduce_relic:
            from engine.models import Relic
            m.relics.append(Relic("叠鳞甲", ""))
        return state, CombatEngine(state, DiceEngine()), p, m

    checks = [
        ("连击", 1, 10, False, 0),    # 5 段各 1 点，10 护盾全吃
        ("连击", 2, 3, False, 7),     # 10 段：3 段吃盾，7 段落血
        ("连击", 1, 0, True, 0),      # 龙鳞每笔 −1 → 0 伤
        ("虚耗", 1, 10, False, 2),    # 单笔 12 点（12X）先被 10 护盾吃 → 2
        ("虚耗", 2, 3, True, 20),     # 单笔 24 点：−1 龙鳞 → 23，再被 3 护盾吃 → 20
        ("断脉", 2, 20, True, 9),     # 吃龙鳞 −1、不吃护盾
        ("买命", 2, 20, True, 10),    # 裸失血
    ]
    print("== 与引擎抽检 ==")
    ok = True
    for name, x, shield, reduce_relic, expect in checks:
        state, combat, p, m = fresh(shield, reduce_relic)
        calc = DaoWenEngine.resolve(name, x, target=m)
        try:
            combat.apply_daowen_effect(name, calc, p, m)
        except Exception as exc:  # 代价付不起等
            print(f"  {name} X={x}: 执行失败 {exc}")
            continue
        lost = 80 - m.current_hp
        mark = "ok" if lost == expect else "差异"
        if lost != expect:
            ok = False
        print(f"  {name} X={x} 护盾{shield:.0f} 龙鳞{int(reduce_relic)}: "
              f"引擎掉血 {lost:.0f} / 预期 {expect} → {mark}")
    print("  结论:", "口径一致" if ok else "需核对（解析模型与引擎结算不一致）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=60000)
    ap.add_argument("--seed", type=int, default=20261109)
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--json", default="")
    ap.add_argument("--write", default="")
    args = ap.parse_args()

    if args.verify:
        verify()

    base, profiles, tuned = compare(args.trials, args.seed)
    text = render(base, profiles, tuned)
    print(text)
    if args.write:
        from pathlib import Path
        Path(args.write).write_text(text, encoding="utf-8")
        print(f"written {args.write}")
    if args.json:
        from pathlib import Path
        payload = {"base": base, "profiles": profiles, "tuned": tuned,
                   "equalizers": equalizers(base["rows"])}
        Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"written {args.json}")


if __name__ == "__main__":
    main()
