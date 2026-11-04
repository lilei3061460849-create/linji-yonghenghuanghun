"""自保时钟实验：132 局前后对照 + 「回复打在谁身上」归因审计。

背景（DM 裁定 2026-08-31）：要让《死者之书》里那句「不伤害他人，一味治疗，异变缠身
都会死亡哦」真的拦住自爆，得把引擎自己的三条自爆时钟接进候选打分
（`engine/ai_tactics.py::_self_preserve_bias`）。该改动属 **AI 决策口径**，默认关闭，
置 `LJ_SELF_PRESERVE=1` 打开，须以前后数据裁定是否常开。

用法：
    # ① 前后对照（各 132 局，seed=1；两次都跑约 16 分钟）
    python3 sim/audits/self_preserve_experiment.py matrix --tag BASE      # 改前
    LJ_SELF_PRESERVE=1 python3 sim/audits/self_preserve_experiment.py matrix --tag SP

    # ② 归因审计：每一笔真实回复落在谁身上 + 候选生成时各道纹的 kind/目标
    python3 sim/audits/self_preserve_experiment.py audit --pairs 12
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


import argparse
import collections
import json
import os
import sys


from sim import duel_records as dr          # noqa: E402


# ---------------------------------------------------------------------------
# ① 132 局矩阵：跑完直接给聚合（判定/死因/回合），不必再解析实录文本
# ---------------------------------------------------------------------------

def run_matrix(tag: str, seed: int) -> None:
    from sim.duel_records import WINNER_DIR
    names = sorted(f for f in os.listdir(WINNER_DIR) if f.endswith(".json"))
    rows = []
    for ch in names:
        for df in names:
            if ch == df:
                continue
            rec = dr.run_and_record(ch, df, seed)
            v = rec.get("判定") or {}
            book = rec.get("终局账面") or {}
            dead = [b for b in book.values() if not b["存活"]]
            cause = (dead[0]["死因source"] if dead else "") or "未死"
            rows.append({"挑战档": ch[:-5], "守擂档": df[:-5],
                         "winner": v.get("winner"), "rounds": v.get("rounds") or 0,
                         "cause": cause})
    verdict = collections.Counter(r["winner"] for r in rows)
    cause = collections.Counter(r["cause"] for r in rows)
    rounds = [r["rounds"] for r in rows]
    flag = os.environ.get("LJ_SELF_PRESERVE", "")
    print(f"\n==== 矩阵 {tag}（LJ_SELF_PRESERVE={flag or '未设置'}）：{len(rows)} 局 seed={seed}")
    print(f"判定：{dict(verdict)}")
    print(f"死因：{dict(cause)}")
    print(f"回合：平均 {sum(rounds) / len(rounds):.2f}  区间 {min(rounds)}–{max(rounds)}")
    out = f"/tmp/self_preserve_{tag}.json"
    json.dump(rows, open(out, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"逐局明细已写入 {out}")


def compare(a: str, b: str) -> None:
    ra = json.load(open(f"/tmp/self_preserve_{a}.json", encoding="utf-8"))
    rb = json.load(open(f"/tmp/self_preserve_{b}.json", encoding="utf-8"))
    flip = sum(1 for x, y in zip(ra, rb) if x["winner"] != y["winner"])
    same = sum(1 for x, y in zip(ra, rb) if x["cause"] == y["cause"])
    print(f"== {a} → {b}：胜方改变 {flip}/{len(ra)} 局；死因不变 {same}/{len(ra)} 局")
    for tag, rows in ((a, ra), (b, rb)):
        print(f"   {tag}: 判定 {dict(collections.Counter(r['winner'] for r in rows))}"
              f" 死因 {dict(collections.Counter(r['cause'] for r in rows))}")


# ---------------------------------------------------------------------------
# ② 归因审计：真实回复落在谁身上（打点 Entity.heal，预演期间静音）
# ---------------------------------------------------------------------------

def run_audit(pairs: int, seed: int) -> None:
    from engine.models import Entity
    import engine.ai_preview as ap
    import engine.api as api
    from engine.ai_tactics import TacticalAI

    st = {"preview": False, "params": None, "side": None}
    heals: list = []
    kinds: collections.Counter = collections.Counter()

    orig_heal = Entity.heal

    def heal(self, amount, *a, **kw):
        r = orig_heal(self, amount, *a, **kw)
        if amount > 0 and not st["preview"]:
            heals.append({"side": st["side"], "params": dict(st["params"] or {}),
                          "受益者": self.name, "量": amount,
                          "累计回复": self.total_healed})
        return r

    orig_prev = ap.ActionPreview.preview

    def prev(self, *a, **kw):
        st["preview"] = True
        try:
            return orig_prev(self, *a, **kw)
        finally:
            st["preview"] = False

    orig_exec = api.GameEngine.execute_action

    def exec_spy(self, action, params):
        if action in ("use_daowen", "use_resonance"):
            st["params"] = {k: params.get(k) for k in
                            ("daowen_name", "x", "target", "actor_ref")}
        return orig_exec(self, action, params)

    orig_cand = TacticalAI._daowen_candidates

    def cand(self):
        out = orig_cand(self)
        me = self.player
        for c in out:
            kinds[(getattr(me, "name", "?"), c["label"].split("X=")[0],
                   str(c["kind"]), c["target"] == getattr(me, "name", None))] += 1
        return out

    orig_ta = TacticalAI.take_action

    def ta(self):
        st["side"] = "守擂" if self._actor_ref else "挑战"
        return orig_ta(self)

    Entity.heal = heal
    ap.ActionPreview.preview = prev
    api.GameEngine.execute_action = exec_spy
    TacticalAI._daowen_candidates = cand
    TacticalAI.take_action = ta

    from sim.duel_records import WINNER_DIR
    names = sorted(f for f in os.listdir(WINNER_DIR) if f.endswith(".json"))
    tried, results = 0, []
    for ch in names:
        for df in names:
            if ch == df or tried >= pairs:
                continue
            tried += 1
            heals.clear()
            rec = dr.run_and_record(ch, df, seed)
            v = rec.get("判定") or {}
            book = rec.get("终局账面") or {}
            seat_of = {b["名字"]: seat for seat, b in book.items()}
            own = foe = 0
            for h in heals:
                # 受益者所在席位 vs 执行侧：不同席 = 在给对手回血
                if seat_of.get(h["受益者"]) == h["side"]:
                    own += h["量"]
                else:
                    foe += h["量"]
            dead = [b for b in book.values() if not b["存活"]]
            results.append({"局": f"{ch[:-5]} vs {df[:-5]}",
                            "winner": v.get("winner"), "rounds": v.get("rounds"),
                            "死因": (dead[0]["死因source"] if dead else "") or "未死",
                            "回复给自己": own, "回复给对手": foe,
                            "回复笔数": len(heals)})
            print(f"  [{tried}/{pairs}] {results[-1]['局']} → {results[-1]['winner']}"
                  f" 第{results[-1]['rounds']}回合 死因={results[-1]['死因']}"
                  f" 回复：自己{own} / 对手{foe}（{len(heals)} 笔）")
            if tried >= pairs:
                break
        if tried >= pairs:
            break

    tot_own = sum(r["回复给自己"] for r in results)
    tot_foe = sum(r["回复给对手"] for r in results)
    print(f"\n== 审计 {len(results)} 局：回复总量 {tot_own + tot_foe}"
          f"（落在自己身上 {tot_own} / 落在对手身上 {tot_foe}）")
    print("== 候选生成时的 kind/目标（施法者 / 道纹 / kind / 目标是否自己）:")
    for k, n in kinds.most_common(16):
        print(f"   {k[0]} {k[1]:<6} kind={k[2]:<10} 目标是自己={k[3]}  ×{n}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)
    m = sub.add_parser("matrix")
    m.add_argument("--tag", required=True)
    m.add_argument("--seed", type=int, default=1)
    c = sub.add_parser("compare")
    c.add_argument("a")
    c.add_argument("b")
    a = sub.add_parser("audit")
    a.add_argument("--pairs", type=int, default=12)
    a.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()
    if args.mode == "matrix":
        run_matrix(args.tag, args.seed)
    elif args.mode == "compare":
        compare(args.a, args.b)
    else:
        run_audit(args.pairs, args.seed)


if __name__ == "__main__":
    main()