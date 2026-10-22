#!/usr/bin/env python3
"""『养蛊』：用当前引擎真实跑一阶 7 场，抓出通关胜者，再用两胜者做第 8 场死斗。

背景（2026-08-30 更正）：旧 `data/real_winners/winner_0X.json` 是旧引擎/旧平衡产物，
当前代码下无法复现（重放只到 cleared=2..5），不该再当"真实胜者"用。本脚本用**当前引擎**
重新养蛊：
  1) 逐种子跑 `build_learner._play`(battles=7)，抓 cleared==7 的局；从封存槽
     `sealed_candidate_path` 里读出【真实跑完 7 场】的快照（含面板/道纹/法术/遗物），
     另存为 `data/breed_winners/won_XX.json`。
  2) 挑两个不同构建的通关胜者（挑战者/守擂者），直接由 `sim.duel_pvp.run_duel_pvp` 驱动对称 PvP 死斗
     （双侧都按轮回者规则：法力制、出手次数按 action_count（当前固定2次）、自由控 X、逐出手交替）。

用法：
    python3 sim/breed_and_duel.py --breed 5          # 养 5 个通关胜者(最多)
    python3 sim/breed_and_duel.py --duel 2            # 用已养胜者做 2 场死斗
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim.build_learner import _play
from sim.duel_pvp import run_duel_pvp

BREED_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "breed_winners")

# 养蛊构建集（starter / 学习序列 / 加点）。速度=关键属性(见实测)：由基准 8 起，
# 用不同构建横向比较，而非都堆速度。
# 修行分配（DM裁定 2026-09-10）：2属性点=1[速限]=1[法限]。
# 档位点数与新计价不匹配——tier3 是 3 点，只能买 1 档并浪费 1 点（如实回报 wasted_points）。
# 兑换点数必须全偶数（2属性点一档）；档位多出来的奇数点留在池里，下次一起兑。
XIUXING_MANA = {"tier3": {"speed_points": 0, "mana_points": 2},
                "tier2": {"speed_points": 0, "mana_points": 2}}
XIUXING_SPEED = {"tier3": {"speed_points": 2, "mana_points": 0},
                 "tier2": {"speed_points": 2, "mana_points": 0}}

BUILDS = {
    # DM裁定 2026-09-10：攻次=当前速度、攻力=当前法力（换算仅限轮回者），
    # 攻次/攻力不再是可购买面板；2属性点=1速限=1法限，1点=6血限。
    "杀伐法攻": {"starter": "杀伐", "learn": ["庇护", "再生"],
                   "attrs": {"blood_points": 7, "speed_points": 6, "mana_points": 12},
                   "xiuxing": XIUXING_MANA},
    "普攻武斗": {"starter": "杀伐", "learn": ["庇护", "再生"],
                   "attrs": {"blood_points": 3, "speed_points": 10, "mana_points": 12},
                   "xiuxing": XIUXING_SPEED},
    "封印控制": {"starter": "封印", "learn": ["杀伐", "再生", "庇护"],
                   "attrs": {"blood_points": 5, "speed_points": 8, "mana_points": 12},
                   "xiuxing": XIUXING_MANA},
    "速战速决": {"starter": "杀伐", "learn": ["庇护", "再生"],
                   "attrs": {"blood_points": 3, "speed_points": 14, "mana_points": 8},
                   "xiuxing": XIUXING_SPEED},
    "血厚耐打": {"starter": "杀伐", "learn": ["庇护", "再生"],
                   "attrs": {"blood_points": 13, "speed_points": 6, "mana_points": 6},
                   "xiuxing": XIUXING_SPEED},
}


def __play_result_name():
    # `_play` 返回里 sealed 字段在 cleared==7+outcome=='sealed' 时才 True；
    # 但 cleared==7 且走 duel 时 sealed 为 None。统一以 cleared==7 作为"通关"基准。
    pass


def _snapshot_from_seal(seal_path: str) -> dict | None:
    """从封存槽文件读出通关胜者快照（candidates['1'][0]）。输入被 _play 消费后可能残留多档。"""
    if not os.path.exists(seal_path):
        return None
    with open(seal_path, encoding="utf-8") as f:
        try:
            data = json.load(f)
        except (ValueError, OSError):
            return None
    for tier, queue in (data.get("candidates") or {}).items():
        for snap in (queue or []):
            if isinstance(snap, dict) and snap.get("player"):
                return snap
    return None


def _breed_one(build_name: str, cfg: dict, seed: int, out_dir: str) -> dict | None:
    seal_path = tempfile.mktemp(suffix=".json")
    db = tempfile.mktemp(suffix=".db")
    from sim.legacy_mentor import LegacyAwareAI
    from sim.win_only_ai import win_only_cls
    # 用户裁定 2026-09-10：一切打分=胜负唯一计分。遗言桥降级为提案器——
    # 遗言偏见仍影响「试什么」（候选裁剪），「选什么」只认整局推演的 ±1。
    bridge = win_only_cls(LegacyAwareAI)
    r = _play(cfg["starter"], cfg["learn"], "扭曲都市", seed=seed, battles=7,
              attrs=cfg["attrs"],
              # DM裁定 2026-09-09：打开养蛊期的碎片支出（含修行），角色才有机会
              # 把碎片换成攻次/攻力——否则普攻恒为 1×1，法力一池制下没有稳定输出。
              spend_shards=True, xiuxing=cfg.get("xiuxing"),
              # DM裁定 2026-09-10：角色可以参考《死者之书》遗言但不百分百照做——
              # 战斗 AI 走遗言桥（评分偏见 ±35 上限 + 逐回合掷签信从度 0.35~0.85）。
              ai_cls=bridge,
              # 养蛊读**正典**《死者之书》：写入必须经 DM submit_ruling 审核，
              # sim 管线不存在自动写路径（engine/api.py:966），只读安全。
              lab_paths={"sealed_path": seal_path, "db_path": db,
                         "death_book_path": os.path.join(
                             os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "死者之书.md")})
    cleared = r.get("cleared") or 0
    if cleared >= 7:
        snap = _snapshot_from_seal(seal_path)
        if snap is None:
            return {"build": build_name, "seed": seed, "cleared": cleared,
                    "ok": False, "reason": "cleared=7 但封存槽无快照"}
        os.makedirs(out_dir, exist_ok=True)
        # 追溯源：构建 + 种子 + 通关场次
        snap["origin"] = {"build": build_name, "seed": seed, "cleared": cleared}
        out = os.path.join(out_dir, f"{build_name}_{seed}.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
        p = snap.get("player") or {}
        return {"build": build_name, "seed": seed, "cleared": cleared,
                "ok": True, "file": out,
                "name": p.get("name"), "血": p.get("blood_limit"), "法": p.get("mana_limit"),
                "速": p.get("speed_limit"),
                "道纹": sorted(p.get("dao_wen", {})),
                "法术": [s.get("name") for s in p.get("spells", [])]}
    return {"build": build_name, "seed": seed, "cleared": cleared, "ok": False}


def breed(args) -> None:
    os.makedirs(BREED_DIR, exist_ok=True)
    # 先清旧
    for fn in os.listdir(BREED_DIR):
        os.remove(os.path.join(BREED_DIR, fn))
    print(f"养蛊目录：{BREED_DIR}\n")
    wins = []
    t0 = time.time()
    for bname, cfg in BUILDS.items():
        got = 0
        for seed in range(args.seed_start, args.seed_start + args.max_per_build):
            if got >= args.per_build:
                break
            res = _breed_one(bname, cfg, seed, BREED_DIR)
            if res.get("ok"):
                got += 1
                wins.append(res)
                p = res
                print(f"  ✅ {bname} seed={seed} → {p['name']} 血{p['血']} 法{p['法']} 速{p['速']} "
                      f"道纹={p['道纹']} 法术={p['法术']}")
            else:
                # 只打印少量失败信息，避免刷屏
                if seed < args.seed_start + 8:
                    print(f"     {bname} seed={seed} cleared={res.get('cleared')}")
        if wins and len(wins) >= args.breed:
            break
    print(f"\n养蛊完成：{len(wins)} 个通关胜者，耗时 {time.time()-t0:.0f}s")
    for w in wins:
        print(f"  · {w['file']}")
    return wins


def _make_breeder_act(e, log):
    """扭曲都市通用的挑战者策略：按【实际持有】的道纹选行动（通用，不硬编码单一道纹）。

    PvP 关键约束：守擂常持【先发制人】（受到伤害前用杀伐反打），挑战者一上来就杀伐
    会被瞬间反杀（满血也扛不住）。所以这里先做**防御/长袖**动作（庇护加盾、封印/束缚/
    减速削弱守擂），再输出；输出用代价型(连击，不耗法)或杀伐的 mana_budget 均摊，
    避免一发打光法力后整回合空转。None 才返回 False（让守擂接管）。"""
    p = e.state.player
    # 2026-10-11 用户令：【衰败】已删除，防守牌位改用仍存的【减速】
    defense = ["庇护", "再生", "封印", "束缚", "减速", "弱化", "蒙蔽", "僵化"]
    attack_off = ["连击", "乱神", "杀伐"]
    # 新回回合：先补一次防御
    state = {"last_round": -1, "buffed": False}

    def act_once():
        if not p or not p.is_alive:
            return False
        enemies = [x for x in e.state.enemies if x.is_alive]
        if not enemies:
            return False
        lord = next((x for x in enemies if x.entity_type == "轮回者"), enemies[0])
        tgt = f"enemy:{e.state.enemies.index(lord)}"
        if e.state.current_round != state["last_round"]:
            state["last_round"] = e.state.current_round
            state["buffed"] = False
        # 每回合第一步：挑一个防御/长袖道纹对自己或压守擂，避免被先发制人一招反杀
        if not state["buffed"]:
            for name in defense:
                if name not in p.dao_wen:
                    continue
                if p.current_mana < 1 and name not in ("束缚", "封印", "蒙蔽", "僵化"):
                    continue
                # 自身增益(庇护/再生)以自身为目标；削弱类(封印/束缚/减速)指向守擂
                target_ref = "player:0" if name in ("庇护", "再生") else tgt
                r = e.execute_action("use_daowen", {"daowen_name": name, "x": 2,
                                                   "target_ref": target_ref,
                                                   "trigger_spell_choices": {}})
                if r.get("success"):
                    state["buffed"] = True
                    log.append(f"  {name}X=2(自己)" if target_ref == "player:0" else f"  {name}X=2 压{lord.name}")
                    return True
            state["buffed"] = True  # 无防御道纹可放也标记，防止反复尝试
        # 输出：代价型优先(不耗法)，杀伐用 mana_budget 均摊
        remain_actions = max(1, p.action_count - p.actions_used_this_round)
        mana_budget = max(1, p.current_mana // remain_actions)
        for name in attack_off:
            if name not in p.dao_wen:
                continue
            if p.current_mana < 1 and name not in ("连击", "乱神"):
                continue
            if name in ("连击", "乱神"):
                x = 2
            else:
                x = max(1, min(mana_budget, p.current_mana))
            r = e.execute_action("use_daowen", {"daowen_name": name, "x": x,
                                               "target_ref": tgt,
                                               "trigger_spell_choices": {}})
            if r.get("success"):
                dmg = sum(ef.get("actual_damage", 0) for ef in (r.get("execution", {}).get("effects") or []))
                log.append(f"  {name}X={x} 打{lord.name} → {dmg}伤")
                return True
        return False

    return act_once


def run_breeder_duel(challenger_path, defender_path, seed, cn, dn, db):
    """用扭曲都市(tier=1)搭建死斗：挑战者/守擂者都是真实通关胜者快照，
    触发 final_crown → duel_start 后经 run_duel_pvp 做对称 PvP。
    返回 (挑战者胜?, 回合数, 日志)。"""
    import shutil
    from engine.api import GameEngine
    from tests.setup_support import finish_initial_daowen
    from sim.handplay_dungeon_with_winner import load_winner
    from sim.optional_actions import start_battle
    from sim.guard_full_run import settle_wages
    # 守擂快照复制到临时封存槽：final_crown 会弹出/消耗该槽文件，直接复用源文件
    # 批次多次死斗会把 data/breed_winners 里的胜者慢慢删光。这里用临时副本，
    # 既保留「守擂=真实胜者快照」的语义，又不破坏胜者数据。
    _tmp_sealed = tempfile.mktemp(suffix=".json")
    shutil.copy(defender_path, _tmp_sealed)
    e = GameEngine(db_path=db, rng_seed=seed, sealed_candidate_path=_tmp_sealed)
    try:
        return _run_breeder_duel_inner(e, challenger_path, defender_path, seed, cn, dn, db)
    finally:
        if os.path.exists(_tmp_sealed):
            os.remove(_tmp_sealed)


def _run_breeder_duel_inner(e, challenger_path, defender_path, seed, cn, dn, db):
    """run_breeder_duel 的实际死斗主体（守擂快照已复制到临时封存槽）。"""
    from tests.setup_support import finish_initial_daowen
    from sim.handplay_dungeon_with_winner import load_winner
    from sim.optional_actions import start_battle
    from sim.guard_full_run import settle_wages
    from sim.legacy_mentor import LegacyAwareAI  # DM裁定 2026-09-10：死斗双方参考遗言
    with open(challenger_path, encoding="utf-8") as f:
        snap = json.load(f)
    p0 = snap["player"]
    e.execute_action("setup_attributes", {"name": p0["name"], "blood_points": 10,
                                          "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "扭曲都市"})
    e.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    load_winner(e, snap)
    # 第 7 场仪式战：挑战者已通关扭曲都市，直接清场 → final_crown → duel_start
    e.state.energy = 0
    e.state.current_battle = 6
    bs, _ = start_battle(e)
    if not bs.get("success"):
        return None, 0, [f"开始第7战失败: {str(bs.get('error',''))[:80]}"]
    for m in e.state.enemies:
        m.is_alive = False
    e.state.monster_reinforcements = []  # 波次：仪式战脚手架直接清空增援队列
    be = e.execute_action("battle_end", {})
    guard = 0
    while be.get("success") and be.get("completed") is False and be.get("pending_wage_decisions"):
        settle_wages(e, [])
        be = e.execute_action("battle_end", {})
        guard += 1
        if guard > 5:
            break
    crown = (be.get("result") or {}).get("final_crown", {})
    if crown.get("outcome") != "duel_start":
        return None, 0, [f"未进入死斗: {crown.get('outcome')} {str(be.get('error',''))[:80]}"]
    # 双轮回者各有其名（随机、互不相同）：在实录头部前先分配，使标题/对白/最终面板都用新名字。
    # run_duel_pvp 内部也会执行（幂等，同 seed 同结果）；这里先跑，让头部即刻反映角色名。
    from sim.duel_pvp import _assign_duelist_names
    _assign_duelist_names(e, seed=getattr(e.dice, "_seed", 0) or 0)
    logs = [f"⚔ 第8场死斗：{cn}(挑战者) vs {dn}(守擂)"]
    # 死斗继承第7场战终真实损耗：打印 当前血/血限，而非只看 blood_limit（更直观）
    logs.append(f"  守擂方：{[(x.name, f'{x.current_hp}/{x.blood_limit}') for x in e.state.enemies]}")
    logs.append(f"  挑战方：{e.state.player.name}({e.state.player.current_hp}/{e.state.player.blood_limit})")
    log_buf = []
    # 对白收集器：双方经性格系统渲染台词（角色不再哑火）
    import types
    对话 = types.SimpleNamespace(buf=[], events=0, next_line_round=1)
    # 挑战者/守擂者都是轮回者，共用 TacticalAI（残韵+性格+变数）+ 对白渲染
    result = run_duel_pvp(e, None, max_rounds=30, max_steps=400, log=log_buf,
                          use_tactical=True, 对话=对话,
                          # DM裁定 2026-09-10：死斗双方同班遗言桥（对称，都可参考遗言）
                          ai_cls=LegacyAwareAI)
    # 把对白（按下限保留）插入实录，与动作日志混排
    for line in 对话.buf:
        logs.append(f"  💬 {line}")
    for line in log_buf[:18]:
        logs.append(f"  {line}")
    p2 = e.state.player
    logs.append(f"  最终：挑战者 hp={p2.current_hp if p2 else 0}/{p2.blood_limit if p2 else 0} "
                f"守擂={[(x.name, x.current_hp) for x in e.state.enemies if x.is_alive]}")
    logs.append(f"  判定：{result.get('winner')}（{result.get('reason')}，第{result.get('rounds')}回合）")
    return result.get("winner") == "challenger", result.get("rounds", 0), logs


def duel(args) -> None:
    files = sorted(fn for fn in os.listdir(BREED_DIR) if fn.endswith(".json"))
    if len(files) < 2:
        print(f"胜者不足（{len(files)}），请先 --breed"); return
    for i in range(args.duel):
        # 交替换边：偶=file[0]挑战 file[1]守擂；奇=file[1]挑战 file[0]守擂
        if i % 2 == 0:
            cs, ds = os.path.join(BREED_DIR, files[0]), os.path.join(BREED_DIR, files[1])
            cn, dn = files[0], files[1]
        else:
            cs, ds = os.path.join(BREED_DIR, files[1]), os.path.join(BREED_DIR, files[0])
            cn, dn = files[1], files[0]
        # 每局用 fresh 副本（final_crown 会删封存槽文件）
        cs_tmp = tempfile.mktemp(suffix=".json")
        ds_tmp = tempfile.mktemp(suffix=".json")
        import shutil
        shutil.copy(cs, cs_tmp); shutil.copy(ds, ds_tmp)
        db = tempfile.mktemp(suffix=".db")
        seed = args.seed_start + i * 97
        print(f"\n===== 死斗第{i+1}局 seed={seed} =====")
        won, rnd, logs = run_breeder_duel(cs_tmp, ds_tmp, seed, cn, dn, db)
        for line in logs or []:
            print(f"  {line}")
        print(f"  → {'挑战者胜' if won else '守擂方胜'}（第{rnd}回合）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--breed", type=int, default=0, help="养 0 个则跳过")
    ap.add_argument("--duel", type=int, default=0, help="死斗场数")
    ap.add_argument("--per-build", type=int, default=1, help="每构建抓几个通关胜者")
    ap.add_argument("--max-per-build", type=int, default=120, help="每构建最多扫多少种子")
    ap.add_argument("--seed-start", type=int, default=1)
    args = ap.parse_args()
    if args.breed:
        breed(args)
    if args.duel:
        duel(args)
    if not args.breed and not args.duel:
        ap.print_help()


if __name__ == "__main__":
    main()
