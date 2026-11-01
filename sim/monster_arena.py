#!/usr/bin/env python3
"""养蛊场：让怪物互斗，用演化把「通用怪物 AI」的权重练出来。

用法（仓库根目录）：
    python sim/monster_arena.py --eval                 # 用现有权重跑一轮评测（打印战绩）
    python sim/monster_arena.py --train                # 演化训练（默认代数/规模见下方常量）
    python sim/monster_arena.py --train --gens 6 --pop 8 --matches 4 --monsters 2
    python sim/monster_arena.py --train --no-cull --save   # 消融：不淘汰失败品
    python sim/monster_arena.py --compare 777 --matches 12 --monsters 3   # holdout 对比

设计（与用户 2026-10-03 的要求逐条对应）：
  · 怪物之间互相攻击——不靠模式开关，靠基础规则：
    敌对＝阵营（`Entity.camp`）不同。养蛊场给每只怪物填唯一 camp（自成一营），
    于是怪物阶段的合法目标按通用规则自然变成「其它参战者」；观众用通用属性
    `Entity.in_combat = False` 表达「不在场上」，既不占出手权也不进合法目标。
    每次提交仍只结算 1 个行动者的 1 个行动（严格交替，避免先手方一次结算全场）。
  · 赢了加分——存活到最后 +1；每击杀 +0.3。
  · 自己行动导致自己死亡＝失败品——两条路径都记为 self_kill 并在报告里单列：
    ① 自己出手之后自己没了（自伤/反噬/自爆）；② 回合结算时被【凡庸】一类"自己折腾出来的"
    结算掉（判据：回合开始时还活着、回合结束时没了、且对手在这一回合没打掉它多少生命）。
    命中即适应度 0。`--no-cull` 是消融开关：训练时不判失败品，用来对照这条规则的作用。
  · 算法——候选动作由生产引擎 prepare 枚举，逐个用 `ActionPreview` 真预演，
    按可调权重打分取最高。权重向量用 (μ+λ) 演化策略在固定对手/固定种子下搜索。

口径与诚实边界：
  · 预演走 `engine/ai_preview.py`，结算与真实执行同一份实现，不存在第二套公式。
  · 训练对手是同一套规则下的其它怪物实例，不是人类玩家；本训练只优化
    「在养蛊场里活下去并杀敌」的通用打分权重，不改变任何道纹/代价/伤害规则。
  · 权重只是打分函数的系数：不外挂脚本、不硬编码道纹名、不看隐藏信息。
"""
from __future__ import annotations
import argparse
import copy
import json
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "sim"))

from engine.ai_preview import ActionPreview          # noqa: E402
from engine.api import GameEngine                    # noqa: E402
from engine.daowen import DaoWenEngine               # noqa: E402
from engine.models import Entity                            # noqa: E402
from engine.monsters import make_monster_entity, parse_monster_pool  # noqa: E402
from tests.setup_support import begin_battle, begin_round, finish_initial_daowen  # noqa: E402

WEIGHTS_PATH = ROOT / "reports" / "monster_ai_weights.json"
POOL_INDEX = ROOT / "副本索引.md"

# 打分权重：这就是被训练出来的「通用怪物 AI」本体（默认值＝人工先验，训练会改动它）
DEFAULT_WEIGHTS = {
    "dmg": 100.0,      # 对其它怪物造成的生命伤害（按目标血限归一）
    "kill": 60.0,      # 造成击杀
    "hp": 80.0,        # 自己失去的生命（按自己血限归一，负项）
    "tempo": 25.0,     # 本回合是否让对手掉血（凡庸线：连续 5 回合不掉血就自爆）
    "mana": 20.0,      # 自己花掉的法力（按法限归一，负项）
    "speed": 20.0,     # 自己失去的当前速度（负项）
    "mut": 30.0,       # 自己累积的异变（负项，50 层崩解）
    "debuff": 15.0,    # 给对手挂上的减益状态数
    "overkill": 8.0,   # 伤害溢出惩罚（打在尸体/超量伤害上）
}

WEIGHT_KEYS = tuple(DEFAULT_WEIGHTS)
# 训练中允许探索的范围（软边界，避免演化出极端系数）
WEIGHT_BOUNDS = {k: (0.0, 400.0) for k in WEIGHT_KEYS}

DEFAULTS = dict(gens=8, pop=8, matches=4, monsters=2, rounds=12, seed=20261003)
MAX_CANDIDATES = 24        # 每个 actor 每回合最多预演多少个候选（性能闸门）


# ---------------------------------------------------------------------------
# 0. 沙盒
# ---------------------------------------------------------------------------

def monster_pool(region: str = "扭曲都市") -> list[dict]:
    pools = parse_monster_pool(POOL_INDEX)
    return pools.get(region) or next(iter(pools.values()))


def build_arena(lineup: list[dict], *, seed: int = 0, rng_seed: int = 7,
                hp_scale: float = 0.25, free_actions: bool = False):
    """建一座养蛊场：玩家侧只当观众（不参战），enemies 全是互斗的怪物。"""
    tmp = Path(tempfile.mkdtemp())
    e = GameEngine(db_path=str(tmp / "arena.db"), save_dir=str(tmp),
                   sealed_candidate_path=str(tmp / "x.json"), rng_seed=rng_seed + seed)
    assert e.execute_action("setup_attributes", {
        "name": "观众", "blood_points": 11, "speed_points": 8, "mana_points": 6})["success"]
    assert finish_initial_daowen(e)["success"]
    assert e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})["success"]
    assert e.execute_action("setup_choose_region", {"region": "扭曲都市"})["success"]
    assert begin_battle(e)["success"]
    assert begin_round(e)["success"]
    e.state.player.blood_limit = e.state.player.current_hp = 10_000    # 观众：不参与，也不该被打
    monsters = []
    for defn in lineup:
        m = make_monster_entity(dict(defn))
        # 养蛊场场景参数（不是规则改动）：把血限按 hp_scale 缩到角斗场尺度，
        # 让比赛在十几回合内分出胜负，训练循环才跑得动。伤害/代价/道纹一律照原规则。
        m.blood_limit = max(30, int(m.blood_limit * hp_scale))
        m.current_hp = m.blood_limit
        monsters.append(m)
    e.state.enemies = monsters
    # 用通用属性表达这件事，引擎里没有任何「养蛊场模式」分支：
    #   · 观众 `in_combat=False` —— 不占出手权、不进任何合法目标；
    #   · 每只怪物自成一营（camp 唯一）—— 敌对＝阵营不同，于是它们互相攻击，
    #     而不会去打观众（观众本来也不在场上）。
    e.state.player.in_combat = False
    for index, monster in enumerate(monsters):
        monster.camp = f"arena:{index}"
    # 「行动自由化 2 槽」已随统一交替出手废止（规则：每次只提交一个行动），
    # 因此这里不再有任何开关；`free_actions` 参数保留只为兼容旧命令行调用。
    return e


def _side_resources(ent) -> dict:
    return {"hp": ent.current_hp, "mana": ent.current_mana, "speed": ent.current_speed,
            "mut": getattr(ent, "mutation_count", 0)}


# ---------------------------------------------------------------------------
# 1. 候选动作 + 打分（策略＝权重向量）
# ---------------------------------------------------------------------------

def _candidates(actor: dict, rivals: list[dict]) -> list[dict]:
    """把 prepare 给出的合法选项展开成候选提交（确定性顺序，性能闸门截断）。

    契约：一次提交＝一个真实行动（攻击组或一个道纹，不打包）。
    free_actions 参数仅为兼容旧命令行保留（引擎里已无自由行动开关），
    两槽（攻击×2／道纹×2／一攻一道纹）的旧候选族
    已随该裁定废止。
    """
    out: list[dict] = []
    free = bool(actor.get("free_actions"))
    daowen_opts = actor.get("daowen_options") or []
    hit = {"dodge": False, "blood_shadow": False, "spell_choices": {"before": {}, "after": {}}}
    groups = max(1, int(actor.get("base_attack_actions") or 1))
    base_hits = max(1, actor.get("base_hits_per_attack", 1))

    def attacks(times: int) -> list[dict]:
        return [{"hits": [dict(hit, target_ref=rival["ref"]) for _ in range(base_hits)]}
                for _ in range(groups * times)]

    if free:
        # 2026-10-30：交替出手统一后，引擎一次提交只接受一个真实行动
        # （《规则正文·战斗行动》第 7 条／怪物条目：攻击与道纹不得打包成同一次提交；
        #  combat_parts/monster_phase.py 的 attack_slots + dao_slots != 1 校验）。
        # 因此候选只保留「单槽」：一组攻击，或一个道纹——每回合能有几次由通用出手
        # 预算（Entity.action_count）决定，不再有「攻击×2／道纹×2」的候选。
        dao_objs: list[dict] = []
        for opt in daowen_opts:
            min_x = max(1, int(opt.get("min_x") or 1))
            wave_refs = ([t["ref"] for t in (opt.get("dodge_target_options") or [])]
                         or [r["ref"] for r in rivals]) if opt["name"] == "波及" else []
            if opt["name"] == "波及" and len(wave_refs) < min_x:
                continue
            xs = ([min_x, max(min_x, (opt.get("max_x") or 1) // 2), opt.get("max_x") or 1]
                  if opt.get("x_free") else [opt.get("x") or 1])
            targets = ([t["ref"] for t in (opt.get("target_options") or [])]
                       if opt.get("requires_target") else [None])
            for x in sorted({int(v) for v in xs if int(v) >= min_x}):
                if opt["name"] == "波及" and x > len(wave_refs):
                    continue
                for tref in targets:
                    cand = {"name": opt["name"], "x": x, "dodge": False, "blood_shadow": False}
                    if tref:
                        cand["target_ref"] = tref
                    if opt["name"] == "波及":
                        cand["dodge_targets"] = [{"target_ref": r, "dodge": False,
                                                  "blood_shadow": False}
                                                 for r in wave_refs[:x]]
                    dao_objs.append(cand)
        can_atk = bool(rivals) and groups > 0
        attacks_out: list[dict] = []
        if can_atk:
            for rival in rivals:
                attacks_out.append({"actor_ref": actor["actor_ref"], "daowen": None,
                                    "attack_actions": attacks(1)})
        dao_out = [{"actor_ref": actor["actor_ref"], "daowen": d, "attack_actions": []}
                   for d in dao_objs]
        # 交替取前 MAX_CANDIDATES 个（避免攻击候选把道纹候选挤出配额）
        out = []
        i = 0
        while len(out) < MAX_CANDIDATES and (i < len(attacks_out) or i < len(dao_out)):
            if i < len(attacks_out):
                out.append(attacks_out[i])
            if len(out) < MAX_CANDIDATES and i < len(dao_out):
                out.append(dao_out[i])
            i += 1
        if not out:
            out = [{"actor_ref": actor["actor_ref"], "daowen": None, "attack_actions": []}]
        return out

    has_daowen = bool(daowen_opts)
    dao_cands: list[dict | None] = []
    for opt in daowen_opts:
        # X 下限恒为 1（2026-10-09 用户令 repealed 了波及 X≥2；prepare 已不再暴露
        # min_x 字段，这里兜底 1）。波及 X=1 合法——单目标全值。
        min_x = max(1, int(opt.get("min_x") or 1))
        wave_refs = ([t["ref"] for t in (opt.get("dodge_target_options") or [])]
                     or [r["ref"] for r in rivals]) if opt["name"] == "波及" else []
        if opt["name"] == "波及" and len(wave_refs) < min_x:
            continue
        xs = ([min_x, max(min_x, (opt.get("max_x") or 1) // 2), opt.get("max_x") or 1]
              if opt.get("x_free") else [opt.get("x") or 1])
        targets = ([t["ref"] for t in (opt.get("target_options") or [])]
                   if opt.get("requires_target") else [None])
        for x in sorted({int(v) for v in xs if int(v) >= min_x}):
            if opt["name"] == "波及" and x > len(wave_refs):
                continue
            for tref in targets:
                cand = {"name": opt["name"], "x": x, "dodge": False, "blood_shadow": False}
                if tref:
                    cand["target_ref"] = tref
                if opt["name"] == "波及":
                    cand["dodge_targets"] = [{"target_ref": r, "dodge": False,
                                              "blood_shadow": False}
                                             for r in wave_refs[:x]]
                dao_cands.append(cand)
    if not dao_cands and not has_daowen:
        dao_cands = [None]                       # 无合法道纹时才允许 null
    for cand in dao_cands:
        for rival in rivals:
            out.append({"actor_ref": actor["actor_ref"], "daowen": cand,
                        "attack_actions": attacks(1)})
    return out[:MAX_CANDIDATES]


def _features(diff: dict, me_index: int) -> dict:
    """把预演 diff 归纳成打分用的特征（只读生产引擎给出的后果）。"""
    foes, own = [], None
    for i, ent in enumerate(diff.get("enemies", []) or []):
        if i == me_index:
            own = ent
        else:
            foes.append(ent)
    def _norm(v, hi):
        return (v / hi) if hi else 0.0
    dmg = sum(max(0, f.get("hp_before", 0) - f.get("hp_after", 0)) for f in foes)
    kills = sum(1 for f in foes if f.get("alive_before") and f.get("dead"))
    overkill = sum(max(0, -(f.get("hp_after", 0))) for f in foes)
    own_hp_loss = max(0, own.get("hp_before", 0) - own.get("hp_after", 0)) if own else 0
    own_hp_max = max(1, own.get("hp_before", 1)) if own else 1
    own_mana = max(0, own.get("mana_before", 0) - own.get("mana_after", 0)) if own else 0
    own_mana_max = max(1, own.get("mana_before", 1)) if own else 1
    own_speed = max(0, own.get("speed_before", 0) - own.get("speed_after", 0)) if own else 0
    own_mut = max(0, (own.get("mut_after", 0) - own.get("mut_before", 0))) if own else 0
    debuffs = sum(1 for ev in diff.get("events", [])
                  if ev.get("type") == "status_applied" and ev.get("target") not in (None, "")
                  and ev.get("actor") == (own or {}).get("name"))
    return {"dmg": _norm(dmg, max(1, sum(f.get("hp_before", 1) for f in foes) or 1)),
            "kill": float(kills), "overkill": _norm(overkill, own_hp_max),
            "hp": _norm(own_hp_loss, own_hp_max),
            "mana": _norm(own_mana, own_mana_max),
            "speed": _norm(own_speed, max(1, own.get("speed_before", 1) if own else 1)),
            "mut": _norm(own_mut, 50.0),
            "tempo": 1.0 if dmg > 0 else 0.0,
            "debuff": float(debuffs)}


def score_features(f: dict, w: dict) -> float:
    return (w["dmg"] * f["dmg"] + w["kill"] * f["kill"] + w["tempo"] * f["tempo"]
            + w["debuff"] * f["debuff"]
            - w["hp"] * f["hp"] - w["mana"] * f["mana"] - w["speed"] * f["speed"]
            - w["mut"] * f["mut"] - w["overkill"] * f["overkill"])


def choose_action(e, token: str, actor: dict, me_index: int, weights: dict,
                  rivals: list[dict], preview: ActionPreview, *,
                  mode: str = "best", rng: random.Random | None = None
                  ) -> tuple[dict, dict, dict]:
    """按权重给候选打分，返回 (提交, 分数表, 最佳候选特征)。

    mode="random" 是**对照用**的随机决策（等概率挑一个合法候选），
    用来量「这套沙盒里『选得聪明』到底值多少分」；训练与评测一律用 "best"。
    mode 取 "attack2"/"mixed"/"dao2" 时：把候选限制在对应行动构成里再按权重取最高
    （该构成没有候选时退回全部候选）——用于「固定构成」对照实验，例如
    「怪物还会不会用道纹」。
    """
    scored = []
    for cand in _candidates(actor, rivals):
        out = preview.preview("resolve_monster_phase", {"token": token, "choices": [cand]})
        res = out.get("result") or {}
        if not res.get("success"):
            continue
        feats = _features(out.get("diff") or {}, me_index)
        scored.append((score_features(feats, weights), cand, feats))
    if not scored:
        # 全部候选都被拒（极端情况）：退回「不发动 + 打第一个对手」，保证流程不死锁
        hit = {"dodge": False, "blood_shadow": False, "spell_choices": {"before": {}, "after": {}}}
        cand = {"actor_ref": actor["actor_ref"], "daowen": None,
                "attack_actions": [{"hits": [dict(hit, target_ref=rivals[0]["ref"])
                                             for _ in range(max(1, actor.get("base_hits_per_attack", 1)))]}
                                   for _ in range(max(1, actor.get("base_attack_actions", 1)))]}
        return cand, {}, {}
    if mode == "random":
        best_score, best_cand, best_feats = (rng or random).choice(scored)
        return best_cand, {"#random": round(best_score, 2)}, best_feats
    if mode in ("attack2", "mixed", "dao2"):
        base_groups = max(1, int(actor.get("base_attack_actions") or 1))

        def _kind(cand: dict) -> str:
            casts = sum(1 for key in ("daowen", "daowen_2") if isinstance(cand.get(key), dict))
            atk = len(cand.get("attack_actions") or []) // base_groups
            if casts and atk:
                return "mixed"
            if casts >= 2:
                return "dao2"
            if atk >= 2:
                return "attack2"
            return "other"

        only = [t for t in scored if _kind(t[1]) == mode]
        if only:
            scored = only
    scored.sort(key=lambda t: (-t[0], t[1]["daowen"]["name"] if t[1]["daowen"] else ""))
    best_score, best_cand, best_feats = scored[0]
    return best_cand, {f"#{i}": round(s, 2) for i, (s, _, _) in enumerate(scored[:3])}, best_feats


# ---------------------------------------------------------------------------
# 2. 一场比赛
# ---------------------------------------------------------------------------

def run_match(lineup: list[dict], weights: dict, *, seed: int = 0, max_rounds: int = 12,
              weight_sets: list[dict] | None = None, hp_scale: float = 0.25,
              setup=None, choice_log: list | None = None,
              choice_modes: list[str] | None = None, free_actions: bool = False) -> dict:
    """lineup 里的怪物互斗；weight_sets 可以为每只怪物指定不同的权重（否则共用）。

    setup(engine) 是可选钩子，用来在开打前摆好特定场面（例如把某只怪物推到崩解线附近），
    只改场景参数，不改规则。
    choice_log 传入列表时，逐次决策记一行（回合/怪物/道纹/X/攻击目标），
    供「两套权重到底有没有改变行为」这类对照探针使用。
    choice_modes 可以为每只怪物指定 "best"（默认，按权重取最高）或 "random"（对照）。
    free_actions=True 打开「怪物行动自由化」（规则实验，默认关）。
    """
    e = build_arena(lineup, seed=seed, hp_scale=hp_scale, free_actions=free_actions)
    if setup is not None:
        setup(e)
    sets = weight_sets or [weights] * len(lineup)
    modes = choice_modes or ["best"] * len(lineup)
    chooser_rng = random.Random(seed * 7919 + 13)
    stats = {i: {"dmg_out": 0, "dmg_taken": 0, "kills": 0, "deaths": 0, "self_kill": False,
                 "casts": 0, "attack_groups": 0, "rounds_acted": 0, "zero_damage_rounds": 0}
             for i in range(len(lineup))}
    log: list[dict] = []
    composition = {"攻击×2": 0, "道纹×2": 0, "一攻一道纹": 0, "单槽/无动作": 0}
    rounds_played = 0
    for rnd in range(1, max_rounds + 1):
        alive = [i for i, m in enumerate(e.state.enemies) if m.is_alive]
        if len(alive) <= 1:
            break
        rounds_played = rnd
        round_start_hp = {i: e.state.enemies[i].current_hp for i in range(len(lineup))}
        from_others: dict[int, int] = {}
        # 每回合轮转先手：避免固定 0 号怪物永远先动
        order = alive[rnd % len(alive):] + alive[:rnd % len(alive)]
        for me_index in order:
            monster = e.state.enemies[me_index]
            if not monster.is_alive:
                continue
            prep = e.execute_action("prepare_monster_phase", {})
            if not prep.get("success"):
                log.append({"round": rnd, "monster": monster.name, "error": prep.get("error", "")})
                break
            res = prep["result"]
            token = res["token"]
            actor = next((a for a in res["actors"] if a["actor_ref"] == f"enemy:{me_index}"), None)
            if actor is None:
                continue
            rivals = [{"ref": f"enemy:{j}", "name": e.state.enemies[j].name}
                      for j, m in enumerate(e.state.enemies) if m.is_alive and j != me_index]
            if not rivals:
                break
            before_hp = {j: m.current_hp for j, m in enumerate(e.state.enemies)}
            preview = ActionPreview(e)
            cand, scores, _ = choose_action(e, token, actor, me_index, sets[me_index],
                                            rivals, preview, mode=modes[me_index],
                                            rng=chooser_rng)
            if choice_log is not None:
                dw = cand.get("daowen") or {}
                tgt = [h.get("target_ref") for act in cand.get("attack_actions", [])
                       for h in act.get("hits", [])]
                choice_log.append({"round": rnd, "monster": monster.name, "actor": me_index,
                                   "daowen": dw.get("name"), "x": dw.get("x"), "targets": tgt})
            out = e.execute_action("resolve_monster_phase",
                                   {"token": token, "choices": [cand]})
            if not out.get("success"):
                log.append({"round": rnd, "monster": monster.name, "error": out.get("error", "")})
                continue
            stats[me_index]["rounds_acted"] += 1
            stats[me_index]["casts"] += sum(
                1 for key in ("daowen", "daowen_2") if isinstance(cand.get(key), dict))
            groups = max(1, int(actor.get("base_attack_actions") or 1))
            atk_groups = len(cand.get("attack_actions") or []) // groups
            stats[me_index]["attack_groups"] += atk_groups
            if free_actions:
                key = (("攻击×2" if atk_groups >= 2 else "单槽/无动作")
                       if not (cand.get("daowen") or cand.get("daowen_2")) else
                       ("道纹×2" if (cand.get("daowen") and cand.get("daowen_2")) else
                        ("一攻一道纹" if atk_groups >= 1 else "单槽/无动作")))
                composition[key] += 1
            dealt = 0
            for j, m in enumerate(e.state.enemies):
                delta = before_hp[j] - m.current_hp
                if delta > 0:
                    if j == me_index:
                        stats[me_index]["dmg_taken"] += delta
                    else:
                        stats[me_index]["dmg_out"] += delta
                        dealt += delta
                        from_others[j] = from_others.get(j, 0) + delta
                if j != me_index and not m.is_alive and before_hp[j] > 0:
                    stats[me_index]["kills"] += 1
            if dealt <= 0:
                stats[me_index]["zero_damage_rounds"] += 1
            # 「自己行动导致自己死亡」之一：自己这一步之后自己没了（自伤/反噬/自爆）
            if not e.state.enemies[me_index].is_alive and before_hp[me_index] > 0:
                stats[me_index]["deaths"] += 1
                stats[me_index]["self_kill"] = True
                log.append({"round": rnd, "monster": monster.name, "self_kill": True,
                            "how": "自己出手后自己命零",
                            "daowen": (cand.get("daowen") or {}).get("name", ""),
                            "scores": scores})
        if e.state.combat_subphase == "await_round_end" and sum(
                1 for m in e.state.enemies if m.is_alive) > 1:
            e.execute_action("round_end", {})
            # 「自己行动导致自己死亡」之二：回合里的行为（如空转喂出【凡庸】）在回终把自己结算掉。
            # 判据：本回合开始时还活着、回合结束时没了，且**没有被别人打掉那么多生命**——
            # 也就是这条命不是对手拿走的，是自己折腾没的。
            for j, m in enumerate(e.state.enemies):
                if round_start_hp.get(j, 0) > 0 and not m.is_alive and not stats[j]["self_kill"]:
                    if from_others.get(j, 0) < round_start_hp[j]:
                        stats[j]["self_kill"] = True
                        stats[j]["deaths"] += 1
                        log.append({"round": rnd, "monster": m.name, "self_kill": True,
                                    "how": "回合内自己把自己结算掉（如【凡庸】空转自爆）",
                                    "taken_from_others": from_others.get(j, 0),
                                    "hp_at_round_start": round_start_hp[j]})
            e.execute_action("round_start", {"relic_choices": {}})
    survivors = [i for i, m in enumerate(e.state.enemies) if m.is_alive]
    return {"lineup": [d["name"] for d in lineup], "rounds": rounds_played,
            "survivors": survivors, "stats": stats, "log": log[-8:],
            "composition": composition}


def fitness(result: dict, me_index: int, weights: dict, *, cull_self_kill: bool = True) -> float:
    """赢了加分；自己行动导致自己死亡＝失败品（0 分，不看其它战果）。

    `cull_self_kill=False` 只用于消融实验（对照「不淘汰失败品会训练出什么」）：
    此时不再把死亡/自爆直接判 0，只看存活、击杀与输出。
    """
    s = result["stats"][me_index]
    if cull_self_kill and (s["self_kill"] or s["deaths"]):
        return 0.0
    last_alive = len(result["survivors"]) == 1 and result["survivors"][0] == me_index
    base = (1.0 if last_alive else 0.0) + 0.3 * s["kills"]
    base -= 0.05 * s["zero_damage_rounds"]          # 空转（凡庸压力）轻罚
    base += 0.05 * (s["dmg_out"] / 100.0)           # 输出效率小项
    return max(0.0, base)


def demo_self_kill(max_rounds: int = 7) -> dict:
    """演示「自己行动导致自己死亡＝失败品」这条判据怎么落地。

    摆一个确定性的场面：0 号怪物的法力上限被摆成 0（[攻击力]＝[当前法力]），于是它每次普攻
    都是 0 伤、连续五回合没能让对手掉血 →【凡庸】把它直接结算掉；它血量够厚，这条命不是对手
    拿走的。判据因此记为 self_kill、适应度 0＝失败品——这正是本局 7 只怪物自己炸死自己的那条路径。
    """
    pool = {d["name"]: d for d in monster_pool()}
    weights = dict(DEFAULT_WEIGHTS)
    first, second = list(pool.values())[:2]
    lineup = [dict(first), dict(second)]

    def _setup(e):
        idle = e.state.enemies[0]
        idle.mana_limit = 0                      # 攻击力=当前法力=0 → 每回合 0 伤
        idle.current_mana = 0
        # 空转要空满五回合：给它足够生命撑住对手的普攻（自己仍然一点伤害都打不出）
        idle.blood_limit = idle.current_hp = 5_000

    res = run_match(lineup, weights, seed=0, max_rounds=max_rounds, setup=_setup)
    s0 = res["stats"][0]
    return {"lineup": [d["name"] for d in lineup],
            "note": "0 号法力上限 0（每回合普攻 0 伤、撑满 5000 生命）",
            "rounds": res["rounds"], "self_kill": bool(s0["self_kill"]),
            "zero_damage_rounds": s0["zero_damage_rounds"], "dmg_out": s0["dmg_out"],
            "dmg_taken": s0["dmg_taken"],
            "fitness": round(fitness(res, 0, weights), 3),
            "survivors": res["survivors"], "log": res["log"][:4]}


# ---------------------------------------------------------------------------
# 3. 评测 / 演化
# ---------------------------------------------------------------------------

def _lineups(pool: list[dict], n_monsters: int, count: int, seed: int) -> list[list[dict]]:
    rng = random.Random(seed)
    names = [d["name"] for d in pool]
    out = []
    for k in range(count):
        pick = rng.sample(range(len(pool)), min(n_monsters, len(pool)))
        out.append([dict(pool[i]) for i in pick])
    return out


def evaluate(weights: dict, *, matches: int, monsters: int, seed: int, rounds: int,
             lineups: list[list[dict]] | None = None, hp_scale: float = 0.25,
             cull_self_kill: bool = True, free_actions: bool = False) -> dict:
    pool = monster_pool()
    lineups = lineups or _lineups(pool, monsters, matches, seed)
    wins = kills = self_kills = 0
    casts = attack_groups = 0
    composition = {"攻击×2": 0, "道纹×2": 0, "一攻一道纹": 0, "单槽/无动作": 0}
    dmgs, rounds_played = [], []
    per_seat = []
    for k, lineup in enumerate(lineups):
        res = run_match(lineup, weights, seed=seed + k, max_rounds=rounds, hp_scale=hp_scale,
                        free_actions=free_actions)
        for key, val in (res.get("composition") or {}).items():
            composition[key] = composition.get(key, 0) + val
        casts += sum(v["casts"] for v in res["stats"].values())
        attack_groups += sum(v["attack_groups"] for v in res["stats"].values())
        seats = [fitness(res, i, weights, cull_self_kill=cull_self_kill)
                 for i in range(len(lineup))]
        per_seat.append({"lineup": res["lineup"], "fitness": [round(f, 3) for f in seats],
                         "survivors": res["survivors"], "rounds": res["rounds"],
                         "self_kills": [i for i, s in res["stats"].items() if s["self_kill"]]})
        wins += sum(1 for i, s in enumerate(seats) if s >= 1.0)
        kills += sum(v["kills"] for v in res["stats"].values())
        self_kills += sum(1 for v in res["stats"].values() if v["self_kill"])
        dmgs.append(sum(v["dmg_out"] for v in res["stats"].values()))
        rounds_played.append(res["rounds"])
    n = len(lineups) * monsters
    return {"matches": len(lineups), "monsters": monsters, "seats": n,
            "mean_fitness": (sum(sum(p["fitness"]) for p in per_seat) / n) if n else 0.0,
            "casts": casts, "attack_groups": attack_groups, "composition": composition,
            "win_rate": (wins / n) if n else 0.0,
            "kills": kills, "self_kill_rate": (self_kills / n) if n else 0.0,
            "avg_damage_per_match": (sum(dmgs) / len(dmgs)) if dmgs else 0.0,
            "avg_rounds": (sum(rounds_played) / len(rounds_played)) if rounds_played else 0.0,
            "detail": per_seat}


def _mutate(w: dict, rng: random.Random, scale: float) -> dict:
    out = dict(w)
    for k in WEIGHT_KEYS:
        if rng.random() < 0.5:
            lo, hi = WEIGHT_BOUNDS[k]
            out[k] = min(hi, max(lo, w[k] + rng.gauss(0, scale * max(1.0, w[k]))))
    return out


def train(*, gens: int, pop: int, matches: int, monsters: int, seed: int, rounds: int,
          generations_log: list | None = None, hp_scale: float = 0.25,
          cull_self_kill: bool = True, free_actions: bool = False) -> dict:
    """(μ+λ) 演化：每代用同一批阵容/种子评测全部个体，取前 1/4 变异繁殖。"""
    rng = random.Random(seed)
    lineups = _lineups(monster_pool(), monsters, matches, seed)   # 固定评测集＝公平比较
    population = [dict(DEFAULT_WEIGHTS)] + [
        _mutate(DEFAULT_WEIGHTS, rng, 0.35) for _ in range(max(0, pop - 1))]
    history = []
    best = {"weights": dict(DEFAULT_WEIGHTS), "score": -1.0, "metrics": {}}
    for gen in range(1, gens + 1):
        scored = []
        for w in population:
            m = evaluate(w, matches=matches, monsters=monsters, seed=seed, rounds=rounds,
                         lineups=lineups, hp_scale=hp_scale, cull_self_kill=cull_self_kill,
                         free_actions=free_actions)
            # 目标＝全席平均适应度：fitness 本身已经写着「存活+1／击杀+0.3／自己作死死＝0」
            # （2026-10-03 用户令），直接用它的均值当选择压力，比只看赢率（二值、噪声大）
            # 更稳；再给输出效率一个小权重把平局拉开。
            score = m["mean_fitness"] + 0.001 * m["avg_damage_per_match"]
            scored.append((score, w, m))
        scored.sort(key=lambda t: -t[0])
        if scored[0][0] > best["score"]:
            best = {"weights": dict(scored[0][1]), "score": round(scored[0][0], 4),
                    "metrics": {k: v for k, v in scored[0][2].items() if k != "detail"}}
        history.append({"gen": gen, "best": round(scored[0][0], 4),
                        "mean": round(statistics.mean(s for s, _, _ in scored), 4),
                        "best_mean_fitness": round(scored[0][2]["mean_fitness"], 4),
                        "best_win_rate": round(scored[0][2]["win_rate"], 4),
                        "best_self_kill_rate": round(scored[0][2]["self_kill_rate"], 4),
                        "weights": {k: round(v, 2) for k, v in scored[0][1].items()}})
        if generations_log is not None:
            generations_log.append(history[-1])
        elite = [dict(w) for _, w, _ in scored[:max(1, len(scored) // 4)]]
        population = elite + [_mutate(rng.choice(elite), rng, 0.2)
                              for _ in range(pop - len(elite))]
    return {"best": best, "history": history}


def load_weights(path: Path = WEIGHTS_PATH) -> dict:
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        return {k: float(data.get("weights", data).get(k, DEFAULT_WEIGHTS[k])) for k in WEIGHT_KEYS}
    return dict(DEFAULT_WEIGHTS)


def save_weights(weights: dict, metrics: dict, path: Path = WEIGHTS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"weights": {k: round(v, 4) for k, v in weights.items()},
                                "metrics": metrics, "source": "sim/monster_arena.py",
                                "note": "养蛊场演化出的通用怪物打分权重；默认不接入生产，"
                                        "需显式传 --weights 或调用方指定才会使用。"},
                               ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------------------
# 4. CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval", action="store_true", help="用现有/默认权重评测")
    ap.add_argument("--train", action="store_true", help="演化训练")
    ap.add_argument("--gens", type=int, default=DEFAULTS["gens"])
    ap.add_argument("--pop", type=int, default=DEFAULTS["pop"])
    ap.add_argument("--matches", type=int, default=DEFAULTS["matches"])
    ap.add_argument("--monsters", type=int, default=DEFAULTS["monsters"])
    ap.add_argument("--rounds", type=int, default=DEFAULTS["rounds"])
    ap.add_argument("--seed", type=int, default=DEFAULTS["seed"])
    ap.add_argument("--hp-scale", type=float, default=0.25, dest="hp_scale")
    ap.add_argument("--self-kill-demo", action="store_true", dest="self_kill_demo",
                    help="演示「自己行动导致自己死亡＝失败品」判据")
    ap.add_argument("--pool", type=str, default=None, metavar="SEED,SEED,...",
                    help="多个 holdout 种子上汇总对比：默认权重 vs 已保存权重（样本更大、更不易被单一种子带偏）")
    ap.add_argument("--compare", type=int, default=None, metavar="HOLDOUT_SEED",
                    help="在 holdout 种子的同一批阵容上，对比默认权重与已保存权重")
    ap.add_argument("--weights", type=Path, default=None)
    ap.add_argument("--save", action="store_true", help="训练后保存权重到 reports/monster_ai_weights.json")
    ap.add_argument("--free-actions", action="store_true", dest="free_actions",
                    help="规则实验（2026-10-03 用户裁定）：怪物每回合 2 槽、不再强制发动道纹")
    ap.add_argument("--no-cull", action="store_false", dest="cull_self_kill",
                    help="消融：训练时不把「自己行动导致自己死亡」判为失败品（权重另存 _nocull 文件）")
    args = ap.parse_args()

    if args.self_kill_demo:
        d = demo_self_kill()
        print(json.dumps(d, ensure_ascii=False, indent=1)[:1200] if d else "未复现自爆：需要人工复核")
        return
    if args.pool is not None:
        seeds = [int(x) for x in args.pool.split(",") if x.strip()]
        w_base, w_new = dict(DEFAULT_WEIGHTS), load_weights(args.weights or WEIGHTS_PATH)
        table = {}
        for tag, w in (("默认权重（人工先验）", w_base), ("养蛊场训练权重", w_new)):
            agg = {"seeds": len(seeds), "matches": 0, "seats": 0, "wins": 0.0,
                   "self_kills": 0.0, "kills": 0, "damage": 0.0, "rounds": 0.0}
            for sd in seeds:
                lineups = _lineups(monster_pool(), args.monsters, args.matches, sd)
                m = evaluate(w, matches=args.matches, monsters=args.monsters, seed=sd,
                             rounds=args.rounds, lineups=lineups, hp_scale=args.hp_scale)
                agg["matches"] += m["matches"]; agg["seats"] += m["seats"]
                agg["wins"] += m["win_rate"] * m["seats"]
                agg["self_kills"] += m["self_kill_rate"] * m["seats"]
                agg["kills"] += m["kills"]
                agg["damage"] += m["avg_damage_per_match"] * m["matches"]
                agg["rounds"] += m["avg_rounds"] * m["matches"]
            n = agg["seats"] or 1
            agg["win_rate"] = round(agg["wins"] / n, 4)
            agg["self_kill_rate"] = round(agg["self_kills"] / n, 4)
            agg["avg_damage_per_match"] = round(agg["damage"] / (agg["matches"] or 1), 1)
            agg["avg_rounds"] = round(agg["rounds"] / (agg["matches"] or 1), 1)
            table[tag] = agg
            print(f"{tag}｜汇总 {agg['seeds']} 个 holdout 种子 × {agg['matches']//agg['seeds']} 场"
                  f"（{agg['seats']} 席）：赢率 {agg['win_rate']:.3f}"
                  f"（{agg['wins']:.0f}/{agg['seats']} 席）｜自爆率 {agg['self_kill_rate']:.3f}"
                  f"｜总击杀 {agg['kills']}｜场均输出 {agg['avg_damage_per_match']:.1f}"
                  f"｜平均回合 {agg['avg_rounds']:.1f}")
        print(json.dumps(table, ensure_ascii=False, indent=1))
        return
    if args.compare is not None:
        pool = monster_pool()
        lineups = _lineups(pool, args.monsters, args.matches, args.compare)
        w_base, w_new = dict(DEFAULT_WEIGHTS), load_weights(args.weights or WEIGHTS_PATH)
        rows = []
        for tag, w in (("默认权重（人工先验）", w_base), ("养蛊场训练权重", w_new)):
            m = evaluate(w, matches=args.matches, monsters=args.monsters, seed=args.compare,
                         rounds=args.rounds, lineups=lineups, hp_scale=args.hp_scale)
            rows.append((tag, m))
            print(f"{tag}：赢率 {m['win_rate']:.3f}｜自爆率 {m['self_kill_rate']:.3f}｜"
                  f"总击杀 {m['kills']}｜场均输出 {m['avg_damage_per_match']:.1f}｜"
                  f"平均回合 {m['avg_rounds']:.1f}")
        print(json.dumps({tag: {k: v for k, v in m.items() if k != "detail"} for tag, m in rows},
                         ensure_ascii=False, indent=1))
        return
    if not (args.eval or args.train):
        args.eval = True
    if args.train:
        t0 = time.time()
        log: list = []
        out = train(gens=args.gens, pop=args.pop, matches=args.matches,
                    monsters=args.monsters, seed=args.seed, rounds=args.rounds,
                    generations_log=log, hp_scale=args.hp_scale,
                    cull_self_kill=args.cull_self_kill,
                    free_actions=args.free_actions)
        print(f"训练完成 {time.time() - t0:.1f}s｜最优适应度 {out['best']['score']}"
              f"｜规则：{'自由行动（2槽，道纹非强制）' if args.free_actions else '原契约（强制道纹）'}"
              f"｜评测构成 {out['best']['metrics'].get('composition')}")
        for row in out["history"]:
            print(f"  第{row['gen']:>2}代  最优 {row['best']:>7.4f}  均值 {row['mean']:>7.4f}  "
                  f"赢率 {row['best_win_rate']:.3f}  自爆率 {row['best_self_kill_rate']:.3f}  "
                  f"席均适应度 {row.get('best_mean_fitness', 0):.3f}")
        print("最优权重:", json.dumps({k: round(v, 2) for k, v in out["best"]["weights"].items()},
                                     ensure_ascii=False))
        if args.save:
            # 不同规则/口径各存各的，避免互相覆盖：自由行动组单独一份
            if args.free_actions:
                path = WEIGHTS_PATH.with_name("monster_ai_weights_free.json")
            else:
                path = WEIGHTS_PATH if args.cull_self_kill else \
                    WEIGHTS_PATH.with_name("monster_ai_weights_nocull.json")
            save_weights(out["best"]["weights"], out["best"]["metrics"], path)
            print("已保存:", path)
    if args.eval:
        w = load_weights(args.weights) if args.weights else load_weights()
        t0 = time.time()
        m = evaluate(w, matches=args.matches, monsters=args.monsters, seed=args.seed,
                     rounds=args.rounds, hp_scale=args.hp_scale, free_actions=args.free_actions)
        print(f"评测 {m['matches']} 场×{m['monsters']} 只｜用时 {time.time() - t0:.1f}s"
              f"｜怪物自由行动 {'开' if args.free_actions else '关'}")
        if getattr(m, "get", None) and m.get("casts") is not None:
            print(f"  行动构成：{m['composition']}｜道纹总数 {m['casts']}｜攻击组总数 {m['attack_groups']}")
        print(f"  赢率 {m['win_rate']:.3f}｜自爆率 {m['self_kill_rate']:.3f}｜"
              f"总击杀 {m['kills']}｜场均输出 {m['avg_damage_per_match']:.1f}｜平均回合 {m['avg_rounds']:.1f}")
        for row in m["detail"]:
            print("  ", row)


if __name__ == "__main__":
    main()
