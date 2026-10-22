#!/usr/bin/env python3
"""真正的 PvP 死斗驱动（守擂方按轮回者规则行动）。

背景（用户指出，已核实）：此前死斗把守擂者整体塞进 state.enemies、用怪物阶段
（prepare_monster_phase/resolve_monster_phase）驱动，守擂者被迫遵守怪物规则：
  - 出手次数 = 1+疯狂X+狂暴1（与速限无关），而非轮回者的 速限/3
  - 不持有法力，发动道纹不付蓝
  - 道纹 X = 实例 x_value（封存快照 x=0 → 道纹全废）
  - 首回合白板（怪不出道纹）
根本没有专门的 PvP 程序执行 PvP 规则。

本驱动让守擂方走与挑战者相同的玩家侧接口（use_daowen / prepare_attack /
resolve_attack），引擎对 in_final_duel 的 opponent 侧已放行，且自动走：
  - 法力制（发动消耗道纹扣守擂主将法力）
  - 轮回者出手次数按 action_count（当前固定2次）；朋友/员工 = 攻击次数/3
  - 道纹 X 自由控（由本驱动决策，与挑战者同策略）
  - 每次行动后 _advance_duel_turn 对称换边
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.enums import CombatSubphase
from engine.ai_tactics import daowen_text_kind
from engine.combat import MEDIOCRITY_ROUNDS

# 顺序与定性（2026-08-31 DM 裁定）：【凡庸】是**规则**层的反乌龟机制，
# 死锁防护只是 sim 层防卡死的**程序兜底**。兜底必须让凡庸先走满它所需的连续
# 回合数；且兜底命中时**不判胜负**，只报错。
# 阈值 = 凡庸阈值 + 1：保证凡庸先拿满 MEDIOCRITY_ROUNDS 个回合-end tick
# 仍未能终结战斗，才轮到兜底说话。
DEADLOCK_MIN_ROUNDS = MEDIOCRITY_ROUNDS + 1
# 对白渲染（角色=轮回者，共用 personality）：让 PvP 有台词、有性格差异
try:
    from sim.duel_dialogue import render_line, peek_personality
except Exception:  # 兜底：对白渲染失败不阻塞死斗
    render_line = lambda actor, event, personality=None, rng=None: f"{getattr(actor,'name','??')}: …"
    peek_personality = lambda engine, entity: None

def _default_ai_cls():
    """死斗默认战术 AI（用户裁定 2026-09-10：胜负唯一计分）。延迟导入防循环。
    LJ_WIN_ONLY=0 退回启发式 TacticalAI（千局级扫描用）。"""
    if os.environ.get("LJ_WIN_ONLY", "1") == "0":
        return TacticalAI
    from sim.win_only_ai import WinOnlyAI
    return WinOnlyAI


# 战场公开频道（报告.md 硬伤3）：台词发布到 state.battle_channel，双方+观战者可见。
# 独立随机源：绝**不**消耗全局 random / 引擎 RNG，避免台词影响 AI 与结算
# （红线 E：不碰 AI —— 连随机数消耗都不能串味）。
try:
    from engine.dialogue import utter as _utter
    _DIALOGUE_RNG = random.Random(20260830)
except Exception:      # 兜底：频道不可用不阻塞死斗
    _utter = None
    _DIALOGUE_RNG = None


def _publish_line(engine, actor, personality=None, log: list = None) -> None:
    """把一句话发布到战场公开频道（时机自由：出手前后、任何时候都能说）。

    只写字符串、不碰任何数值、不进 AI 决策链。发布失败一律静默——台词
    永远不该让死斗跑不下去。
    """
    if _utter is None or actor is None:
        return
    try:
        entry = _utter(engine.state, actor, rng=_DIALOGUE_RNG, personality=personality)
        if log is not None:      # 实录：把这句话按发生顺序并进动作日志
            log.append(f"  [{actor.name}] 说（{entry['posture']}）：{entry['text']}")
    except Exception:
        pass

# 注意：不在模块顶层导入 build_learner（循环导入——build_learner 在死斗时局部
# import 本模块，若本模块顶部又导入 build_learner 会拿到半加载的模块，
# round_start_relic_choices 尚未定义 → NameError）。改为函数内延迟导入。


def _defender_resonance_candidates(e, ref, ent, previewer, cache):
    """守擂者（同为轮回者）的残韵候选：用自己的残韵库存转化**挑战者**的道纹，
    并让自己获得转化后的道纹。与挑战者侧 TacticalAI._resonance_candidates 同一
    哲学（对存在变化路径的敌方道纹打分，无固定表）。返回 [(score, params, desc)]。"""
    from engine.daowen import ResonanceEngine
    stock = getattr(ent, "resonance", None) or {}
    if not any(v > 0 for v in stock.values()):
        return []
    p = e.state.player
    if not p or not p.is_alive:
        return []
    out = []
    # 挑战者威胁构成（可见信息）：越强的道纹越值得转化
    for dw, inst in sorted(p.dao_wen.items()):
        if inst is None:
            continue
        text_kind = daowen_text_kind(inst)
        weight = {"damage": 1.0, "control": 0.7, "debuff": 0.6}.get(text_kind, 0.4)
        for path in ResonanceEngine.get_available_resonance(dw):
            rtype = path.get("resonance_type")
            if not rtype or stock.get(rtype, 0) <= 0:
                continue
            params = {"actor_ref": ref, "source_daowen": dw, "resonance_type": rtype,
                      "target_ref": "player:0"}
            pv = previewer.preview("use_resonance", params)
            res = pv.get("result") or {}
            if not res.get("success"):
                continue
            diff = pv.get("diff", {})
            dp = diff.get("player", {})
            # 转化挑战者强道纹即可观收益：夺其威胁 + 施法者获得新道纹的期权
            score = 4.0 * (0.5 + weight)
            score -= 0.5 * max(0, dp.get("hp_before", 0) - dp.get("hp_after", 0))  # 不产生直接伤害，避免与输出竞争时被压制
            out.append((score, params,
                        f"残韵·{rtype}→{dw}@{p.name}（守擂{ent.name}转化）"))
    out.sort(key=lambda t: -t[0])
    return out[:3]


def _resolve_opponent_one(e, log=None, 对话=None):
    """守擂方一步：通用预演决策（2026-08-26 修复"木桩守擂"）。

    旧版硬编码 杀伐/低血庇护/普攻 三板斧——不持有杀伐的封存构筑（如
    畸变/连击流）会整场一动不动,死斗沦为打木桩(用户判定为无效数据)。
    现改为与挑战者 TacticalAI 同一哲学:枚举守擂主将**实际持有**的可用道纹
    × X 档 × 目标,经 ActionPreview 在引擎副本上真实预演,按后果打分:
    挑战者掉血/阵亡 > 自身回血 > 压制挑战者(状态事件) > 法力节约;
    全部被引擎拒绝才退回普攻,普攻也不行才让行。
    每次仍只行动一步,保持"逐出手交替"的 PvP 语义。
    """
    from sim.build_learner import _decline_spells
    if log is None:            # 不得用 `log = log or []`:空列表是 falsy,会静默丢弃调用方的日志缓冲(2026-08-26 修复守擂日志丢失)
        log = []
    refs = e.combat._combat_entity_refs()
    candidates = []
    for ref, ent in refs.items():
        if not ref.startswith("enemy:"):
            continue
        if not ent.is_alive or ent.has_retreated:
            continue
        if ent.actions_used_this_round >= ent.action_count:
            continue
        candidates.append((ref, ent))
    if not candidates:
        return False
    lord = next(((r, x) for r, x in candidates if x.entity_type == "轮回者"), None)
    ref, ent = lord or candidates[0]
    p = e.state.player

    best = None   # (score, params, desc)
    if ent.entity_type == "轮回者" and p and p.is_alive:
        from engine.ai_preview import ActionPreview
        previewer = ActionPreview(e)
        actions_left = max(1, ent.action_count - ent.actions_used_this_round)
        mana_budget = max(1, ent.current_mana // actions_left)
        cache = getattr(e, "_duel_probe_cache", None)
        if cache is None:
            cache = {}
            e._duel_probe_cache = cache
        for name, inst in sorted(ent.dao_wen.items()):
            if inst is None or not inst.can_use():
                continue
            probe = cache.get(name)
            if probe is None:   # X=1 探针:法力单价 + 方向(自身/敌向)
                pv = previewer.preview("use_daowen", {
                    "actor_ref": ref, "daowen_name": name, "x": 1,
                    "target_ref": "player:0", "dodge": False, "blood_shadow": False})
                cost = ((pv.get("result") or {}).get("calculation") or {}).get("cost")
                ok = bool((pv.get("result") or {}).get("success"))
                probe = {"cost": cost if isinstance(cost, int) else 1, "ok_on_player": ok}
                cache[name] = probe
            cost = probe["cost"]
            cap = max(1, ent.current_mana // cost) if cost > 0 else 2
            xs = sorted({1, min(cap, max(1, mana_budget // cost)) if cost > 0 else 1, cap})[-3:]
            for target_ref in ("player:0", ref):
                for x in xs:
                    params = {"actor_ref": ref, "daowen_name": name, "x": x,
                              "target_ref": target_ref, "dodge": False,
                              "blood_shadow": False, "trigger_spell_choices": {}}
                    pv = previewer.preview("use_daowen", params)
                    res = pv.get("result") or {}
                    if not res.get("success"):
                        continue
                    diff = pv.get("diff", {})
                    dp = diff.get("player", {})
                    score = 0.0
                    dmg = max(0, dp.get("hp_before", 0) - dp.get("hp_after", 0))
                    score += 2.2 * dmg
                    if dp.get("dead"):
                        score += 100.0
                    score -= 0.5 * max(0, dp.get("shield_after", 0) - dp.get("shield_before", 0))
                    lord_name = ent.name
                    for ev_row in diff.get("enemies", []):
                        if ev_row.get("name") == lord_name:
                            score += 1.4 * max(0, ev_row.get("hp_after", 0) - ev_row.get("hp_before", 0))
                    for ev in diff.get("events", []):
                        if ev.get("type") == "status_applied" and ev.get("target") == (p.name if p else ""):
                            score += 2.5   # 压制挑战者
                    score -= 0.12 * (res.get("calculation", {}).get("cost") or 0)
                    desc = f"守擂{ent.name} {name}X={x}" + ("(自身)" if target_ref == ref else "→挑战者")
                    if best is None or score > best[0]:
                        best = (score, params, desc)
        # 守擂残韵：与挑战者共用一套残韵机制（本步骤不消耗出手，可任意时刻插队）
        for score, rparams, rdesc in _defender_resonance_candidates(e, ref, ent, previewer, cache):
            if best is None or score > best[0]:
                best = (score, rparams, rdesc)
        if best is not None and best[0] > 0:
            params = dict(best[1])
            # 闪避中继(2026-08-26):攻方提交时代目标声明闪避(与怪物阶段解析器同构)。
            # 挑战者是否闪避由 choose_dodge 按伤害阈值/速度预算决定——修复 PvP 双方
            # 从不闪避的问题(用户指出:把把死斗第一回合结束)。
            from engine.ai_tactics import choose_dodge
            pv_check = previewer.preview("use_daowen", params)
            exp_dmg = max(0, ((pv_check.get("diff", {}).get("player", {}) or {})
                              .get("hp_before", 0))
                          - ((pv_check.get("diff", {}).get("player", {}) or {})
                             .get("hp_after", 0)))
            params["dodge"] = choose_dodge(e, exp_dmg) if params.get("target_ref") == "player:0" else False
            r = e.execute_action("use_daowen" if best[1].get("daowen_name") else "use_resonance", params)
            if r.get("success"):
                dodge_note = "(挑战者闪避)" if params.get("dodge") else ""
                log.append(f"  {best[2]}{dodge_note}")
                _defender_line(e, 对话, ent)
                return True
    # 普攻兜底（prepare_attack/resolve_attack，走玩家侧攻击接口；逐击闪避由
    # choose_dodge 按每击伤害与速度预算决定——挑战者侧的闪避终于存在）
    prep = e.execute_action("prepare_attack", {"actor_ref": ref})
    if not prep.get("success"):
        return False
    target_ref = "player:0"
    option = next((o for o in prep["result"]["target_options"] if o["ref"] == target_ref),
                  prep["result"]["target_options"][0])
    from engine.ai_tactics import choose_dodge
    # DM裁定 2026-09-10：轮回者攻力=当前法力，面板 attack_power 恒为 0。
    # 这里过去直读面板，守擂是轮回者时每击伤害被当成 1 点，永远够不到闪避阈值，
    # PvP 的闪避中继等于失效。必须走 effective_attack_power()。
    per_hit_damage = ent.effective_attack_power() or 1
    hits = [{"target_ref": option["ref"],
             "dodge": choose_dodge(e, per_hit_damage),
             "blood_shadow": False,
             "spell_choices": _decline_spells(option)}
            for _ in range(prep["result"]["hit_count"])]
    res = e.execute_action("resolve_attack", {"token": prep["result"]["token"], "hits": hits})
    if res.get("success"):
        n_dodge = sum(1 for h in hits if h["dodge"])
        log.append(f"  守擂{ent.name} 普攻（{prep['result']['hit_count']}击"
                   + (f",{n_dodge}击被闪避" if n_dodge else "") + "）")
        _defender_line(e, 对话, ent)
        return True
    return False


# 性格维度：给死斗双方各seed一个**确定、彼此不同**的人格画像（由名字哈希导出）。
# 这使「挑战者 vs 守擂者」的性格差异真实可观察（TacticalAI 性格调制 + 对白渲染），
# 而不是两人都退化为无性格的纯局势效用。人格由 engine.personality 权威记录，
# _refresh_personality/_w 与 render_line 均读同一份数据。
_TRAIT_DIMS = [
    "risk_preference",     # +冒险 / -求稳
    "exploration_desire",  # +探索 / -守成
    "expression_style",    # +直言 / -内敛
    "reaction_pattern",    # +从容 / -慌乱
    "emotional_stability", # +沉稳 / -易波动
    "decision_habit",      # +先观察后行动 / -冲动
    "moral_baseline",      # +守义 / -利己
    "resource_view",       # +节约 / -挥霍
]


def _seed_duelist_personality(e, entity) -> None:
    """给一名轮回者写入一套确定性人格（名字哈希决定各维方向与强度）。

    每条维度记 4 次同向证据（weight=1），使 EMA 收敛到方向、置信度累积到可读阈值，
    score×confidence 足够让 _w() 产生真实调制，也让 render_line 能挑到性格台词。
    """
    if entity is None or not getattr(entity, "is_alive", False):
        return
    import hashlib
    digest = hashlib.sha256(entity.name.encode("utf-8")).hexdigest()
    for i, dim in enumerate(_TRAIT_DIMS):
        byte = int(digest[i % len(digest):i % len(digest) + 2], 16)
        direction = 1 if byte % 2 == 0 else -1
        strength = 0.55 + (byte % 5) / 10.0  # 0.55~0.95，避免所有角色一致
        for _ in range(4):
            try:
                e.update_personality(
                    entity, dim, direction,
                    evidence=f"死斗性格映射：{entity.name}在{_TRAIT_DIMS[i]}维度"
                             f"表现出{'积极' if direction>0 else '消极'}倾向",
                    weight=strength,
                )
            except Exception:
                break  # 实体中途离场等偶发情况：跳过即可，不阻塞死斗


# 引擎内部死因 subtype → 读得懂的名字。缺的按原样显示，宁可露内部名也不要静默吞掉。
_DEATH_CAUSE_LABELS = {
    "mediocrity": "凡庸",
    "collapse": "崩解",
    "cancer": "癌变",
    "proliferation": "癌变",
    "sculpture": "化雕塑",
}


def death_attribution_note(entity, side_label: str) -> str:
    """命零归因（报告.md 硬伤1 改法2，DM 裁定 2026-08-30）。

    「守擂主将阵亡」这句话默认暗示是对手打死的。实测守擂者靠【连击X】的
    「流血X」代价把自己流到 0——没人打它，是它自己付的代价（死亡上下文里
    actor == 死者本人且带 active_payment 标签）。胜负仍按「主将命零＝守擂
    失守」判定（挑战者胜），但归因必须如实写清楚，不让挑战者白捡一个击杀。
    """
    if entity is None:
        return f"{side_label}阵亡"
    ctx = getattr(entity, "_death_ctx", None) or {}
    if not ctx:
        return f"{side_label}阵亡"
    # 具名死因优先：DM 要求死因必须写明，不许把【癌变】这类特殊死因
    # 兜底成含糊的「自伤命零」（2026-08-31：癌变就是这样被吞掉的）。
    cause = ctx.get("subtype") or ""
    if cause == "cancer":
        return f"{side_label}阵亡（累计恢复量达血限×2 → 因【癌变】命零，非对手击杀）"
    actor = ctx.get("actor")
    if actor in (None, "", getattr(entity, "name", None)):
        source = ctx.get("source") or "代价"
        if "active_payment" in (ctx.get("tags") or []):
            return f"{side_label}阵亡（自付【{source}】代价命零，非对手击杀）"
        if cause and cause != "hp_zero":
            # 内部 subtype 是英文名，直接吐出来读不懂（实测出现过 "mediocrity"）
            label = _DEATH_CAUSE_LABELS.get(cause, cause)
            return f"{side_label}阵亡（{label}，非对手击杀）"
        return f"{side_label}阵亡（自伤命零，非对手击杀）"
    return f"{side_label}阵亡"


def _drain_ai_log(ai, prefix: str, log: list, seen: dict) -> None:
    """把 AI 自 `seen` 之后新增的日志行全部追加进 log（保序）。

    原先只取 `ai.log[-1]`，verbose 下每次出手可能有多条（[读到…] + [实时决策]…），
    只取最后一条会把"读到对手台词后的判断"整条丢掉，对白的影响就看不见了。
    """
    start = seen.get(id(ai), 0)
    for line in ai.log[start:]:
        log.append(f"  {prefix} {line}")
    seen[id(ai)] = len(ai.log)


def _clean_ai_label(line: str) -> str:
    """把 TacticalAI 日志行压缩为可读动作标签，如
    '[实时决策] 残韵·曲解→再生@贾凡（对手）（得分 1.83）' → '残韵·曲解→再生@贾凡（对手）'。"""
    label = line.strip()
    label = label.split("[实时决策]", 1)[-1].strip()
    label = label.split("（得分", 1)[0].strip()
    return label


# 死斗角色名池：给两名轮回者随机分配**互不相同**的名字（显示层）。养蛊胜者快照里
# 的名字是生成时统一写死的「贾凡」，直接拿来死斗就会出现两个一模一样的人名。
# 每次死斗从池里**确定性**抽两枚（同 seed 可复现），保证「各有其名、绝不相同」。
_DUELIST_NAME_POOL = [
    "玄夜", "青梧", "林渊", "凌霜", "萧晨", "君墨", "苏挽", "沈孤",
    "裴青", "顾昭", "白芷", "闻人", "祝融", "洛璃", "祁连", "段云",
    "秦九", "阮烟", "南宫", "花袭", "司空", "雪榭", "江晚", "寒彻",
]


def _assign_duelist_names(e, seed: int) -> None:
    """给挑战者与守擂主将各分配一个互不相同的名字（确定性，同 seed 可复现）。

    只改显示层名字（Entity.name），不碰数值/道纹/机制。人格 seed 以名字为输入，
    故名字先定，再 seed 性格，避免「两人同名导致性格恰好相同」。若一方没有存活
    的轮回者主将（如守擂全灭），则只给挑战者改。

    **配对改为挑"性格反差最大的一对"**（2026-08-30）：人格由名字哈希决定，
    纯随机抽名经常抽到两个信念都接近 0 的木头（实测 seed=1 的「司空/闻人」
    信念各约 +0.06 / −0.29），双方谁也不信谁也不疑，对白就退化成垃圾话。
    这里在名字池里**穷举**所有两两组合，挑「对同一句示弱的判断差距最大」的一对
    ——名字是显示层，数值/道纹/机制一个都不动，但心理博弈立刻看得见了。
    seed 相同仍可复现；池内无有效组合时回退到原随机抽法。
    """
    import random as _random
    pool = list(_DUELIST_NAME_POOL)
    chosen = _pick_contrasting_names(pool, seed)
    rng = _random.Random(seed)
    if chosen is None:                     # 回退：原随机抽法
        rng.shuffle(pool)
        names = iter(pool)
        challenger_name = next(names)
        lord_name = next(names)
    else:
        challenger_name, lord_name = chosen
    taken = set()
    if e.state.player is not None:
        e.state.player.name = challenger_name
        taken.add(challenger_name)
    lord = next((x for x in e.state.enemies
                 if x.entity_type == "轮回者" and x.is_alive), None)
    if lord is not None:
        name = lord_name if lord_name not in taken else None
        if name is None:
            names = iter([n for n in pool if n not in taken])
            name = next(names)
        lord.name = name
        # 与玩家侧同名的其他实体（朋友/员工）此前已被 _uniquify 加「（对手）」，
        # 改名后再兜底一次避免冲突。
        for other in e.state.get_all_player_side():
            if other is not e.state.player and other.name == lord.name:
                other.name = f"{other.name}（对手）"


def _pick_contrasting_names(pool: list, seed: int):
    """在名字池里挑「对同一句示弱的判断反差最大」的一对（确定性，同 seed 可复现）。

    人格由名字哈希决定，纯随机抽名常常抽到两个"谁也不信谁也不疑"的木头。
    返回 (挑战者名, 守擂名)；算不出来返回 None（调用方回退到随机抽法）。
    """
    try:
        import hashlib
        from engine.dialogue import belief_from_traits
        dims = list(_TRAIT_DIMS)

        def traits(name: str) -> dict:
            d = hashlib.sha256(name.encode("utf-8")).hexdigest()
            return {dim: (1 if int(d[i % len(d):i % len(d) + 2], 16) % 2 == 0 else -1)
                    * (0.55 + (int(d[i % len(d):i % len(d) + 2], 16) % 5) / 10.0)
                    for i, dim in enumerate(dims)}

        prof = {n: traits(n) for n in pool}
        best, best_gap = None, 0.0
        for i, a in enumerate(pool):
            for b in pool[i + 1:]:
                ga = belief_from_traits(prof[a], "weak")
                gb = belief_from_traits(prof[b], "weak")
                gap = abs(ga - gb)
                if gap > best_gap + 1e-9:
                    best, best_gap = (a, b) if ga >= gb else (b, a), gap
        if best is None:
            return None
        import random as _r
        if _r.Random(seed).random() < 0.5:      # 谁当挑战者由 seed 定，仍可复现
            best = (best[1], best[0])
        return best
    except Exception:
        return None


def _defender_line(e, 对话, ent) -> None:
    """守擂者动作后触发一句性格对白（不阻塞死斗）。"""
    if 对话 is None:
        return
    对话.events += 1
    if 对话.events in (3, 10, 18, 26):
        pers = peek_personality(e, ent)
        line = render_line(ent, "damage_out" if 对话.events % 2 else "damage_in", pers)
        对话.buf.append(line)
        _publish_line(e, ent, pers)


def _duel_state_sizes(e, top=6):
    """诊断：按字段统计 deepcopy 成本（ms），用于超时样本归因。"""
    import copy, dataclasses, time
    rows = []
    st = e.state
    for f in dataclasses.fields(st):
        try:
            t = time.perf_counter()
            copy.deepcopy(getattr(st, f.name))
            rows.append(((time.perf_counter() - t) * 1000, f.name))
        except Exception:
            pass
    rows.sort(reverse=True)
    return [(nm, round(ms, 2)) for ms, nm in rows[:top]]


def run_duel_pvp(e, player_act=None, max_rounds=60, max_steps=400, log=None,
                 max_wall_seconds=30.0, use_tactical=True, 对话=None,
                 ai_cls=None, resume=False):
    """PvP 对称交替死斗：双方都按轮回者规则行动。

    ai_cls: 双方共用的战术 AI 类。**默认 WinOnlyAI（用户裁定 2026-09-10：
        胜负唯一计分——胜+1/败-1，无论什么手段什么战术）**；显式传 TacticalAI
        或 LegacyAwareAI 可回到启发式/遗言桥口径（旧报告实录均为该口径）。

    resume=True：从**进行中的死斗状态**续跑（供整局推演 WinOnlyAI 用）——跳过
        设名/读书/性格播种与首个 round_start，直接从当前 duel_turn 接管。
        常规调用（resume=False）行为完全不变。

    player_act(): 挑战者侧行动1次（成功返回 True，引擎已换边；无行动返回 False）。
        当 use_tactical=True 时忽略 player_act，改由 TacticalAI（含残韵候选 +
        性格调制 + 变数决策）驱动挑战者；player_act 保留为向后兼容的落后路径。
    守擂侧由本驱动用玩家侧接口行动（法力制/出手次数/自由控X + 残韵候选 + 对白）。
    对话: 可选 SimpleNamespace(buf, events, next_line_round)，用于收集死斗对白。

    max_wall_seconds: 墙钟守护（2026-08-22）。死斗在战斗7之后进行，实体/遗物/
        事件累积使 ai_preview 的整状态 deepcopy 预演成本暴涨，实测单场死斗
        100% CPU 空转 >5 分钟（批次13 gen1619 卡死事件）。这是资源超时，
        只返回 timeout_kind=wall_clock，不作胜负判定、不计玩法失败。
    返回 dict: {'winner': 'challenger'|'defender', 'rounds': n, 'reason': str}
    """
    import time as _time
    from sim.build_learner import round_start_relic_choices
    if log is None:   # 空列表是 falsy,`log or []` 会静默丢弃调用方缓冲(2026-08-26 同源修复)
        log = []
    # 双轮回者各有其名（随机生成、互不相同）：先定名，再 seed 性格（性格按名字哈希）。
    if not resume:
        _assign_duelist_names(e, seed=getattr(e.dice, "_seed", 0) or 0)
        # 死斗开始前，双方轮回者各自翻阅《死者之书》——前人怎么死的是唯一的历史教训。
        # 纯读取：不消耗精力、不掷骰、不改任何数值（engine/api.py::_action_read_death_book）。
        try:
            _book = e.execute_action("read_death_book", {})
        except Exception:  # 读取失败不得影响死斗本身
            _book = {}
        for _l in (_book.get("legacies") or []):
            log.append(f"  [死者之书] {_l.get('title', '')}｜{_l.get('text', '')}")
        # 双方都是轮回者：各自 seed 一套确定、可区分的性格画像 → 性格调制 + 对白差异。
        _seed_duelist_personality(e, e.state.player)
        for foe in e.state.enemies:
            if foe.entity_type == "轮回者":
                _seed_duelist_personality(e, foe)
    # 残韵：挑战者沿用 State.resonance（load_winner 已从快照还原其真实准备量）；
    # 守擂者在 _trigger_final_crown 也已从快照还原其真实准备量（无则 0）。
    # 一律**只使用真实准备的残韵**，绝不凭空充能——没有就是没有。
    # 挑战者用 TacticalAI 驱动（残韵+性格+变数）
    from engine.ai_tactics import TacticalAI
    # 默认 = WinOnlyAI（用户裁定 2026-09-10：胜负唯一计分，死斗默认用它——
    # 「不然出的再快看一群傻子打架有什么意思」）。启发式 TacticalAI 降级为
    # 推演内环/显式指定时的实现。延迟导入防循环。
    _ai_cls = ai_cls if ai_cls is not None else _default_ai_cls()
    _def_tai = None
    if use_tactical:
        _tai = _ai_cls(e, verbose=True)
        _seen: dict = {}
        def player_act():
            acted = _tai.take_action()
            # 动作实录：挑战者侧执行的动作（含残韵/道纹）→ 报告可见。
            # 排空而非只取最后一条：verbose 下的"[读到…]"自述也要留住；
            # 且**未出手时也要排空**，否则日志顺序会失真（攒到下次一起倒出来）。
            _drain_ai_log(_tai, "挑战者", log, _seen)
            if not acted:
                return False
            # 对白：挑战者侧动作事件（首句开场后按节奏插台词）。
            # 注意：**只在真的出手之后**才说——AI 空转一次就说一句会刷屏。
            if 对话 is not None:
                对话.events += 1
                pers = peek_personality(e, e.state.player)
                line = render_line(e.state.player, "opening", pers)
                if 对话.events in (1, 2, 8, 15):
                    对话.buf.append(line)
                _publish_line(e, e.state.player, pers, log)
            return True
        # 守擂者用同一套 TacticalAI，视角重定向为守擂（双方共享机制）
        lord = next((x for x in e.state.enemies
                     if x.entity_type == "轮回者" and x.is_alive), None)
        if lord is not None:
            refs = e.combat._combat_entity_refs()
            lord_ref = next((r for r, ent in refs.items() if ent is lord), "player:0")
            foes = [e.state.player] + [f for f in e.state.friends if f.is_alive] \
                   + [emp for emp in e.state.employees if emp.is_alive and emp.is_deployed]
            foes = [f for f in foes if f is not None and f.is_alive]
            # verbose=True：让守擂者动作（含残韵）写入 _def_tai.log，报告才能如实呈现。
            # 否则守擂者一切行动（含残韵）都静默执行、对客席不可见，报告会误判"守擂无残韵"。
            _def_tai = _ai_cls(e, verbose=True, actor=lord, enemies=foes, actor_ref=lord_ref)
    deadline = _time.monotonic() + max_wall_seconds

    def _over_time():
        return _time.monotonic() > deadline

    def _lord():
        return next((x for x in e.state.enemies if x.entity_type == "轮回者"), None)

    def _duelist_out(ent) -> bool:
        """判负出场=命零**或**化雕塑（is_sculptured/is_departed 离场不是命零，
        但死斗只允许一名轮回者离开——化雕塑同样判负，2026-09-10 用户裁定：
        判定须区分 被击杀/化雕塑/凡庸/崩解，不许一律写「阵亡」）。"""
        return bool(ent is None or not ent.is_alive
                    or getattr(ent, "is_sculptured", False)
                    or getattr(ent, "is_departed", False))

    def _lord_alive():
        return not any(_duelist_out(x) for x in e.state.enemies
                       if x.entity_type == "轮回者")

    def _challenger_alive():
        return not _duelist_out(e.state.player)

    def challenger_death_reason() -> str:
        p = e.state.player
        if p is not None and (getattr(p, "is_sculptured", False)
                              or getattr(p, "is_departed", False)):
            return "挑战者化雕塑（攻次与攻力双0离场，判负；非被击杀）"
        return death_attribution_note(p, "挑战者")

    def lord_death_reason() -> str:
        lord = _lord()
        if lord is not None and (getattr(lord, "is_sculptured", False)
                                 or getattr(lord, "is_departed", False)):
            return "守擂主将化雕塑（攻次与攻力双0离场，判负；非被击杀）"
        return death_attribution_note(lord, "守擂主将")

    # 「连续 N 回合双方面板零净变化」= 真死锁(互瞪):法力每回合已回填,仍无任何
    # 可造成伤害/回血的动作(如 1 血 0 牌对峙)。这与「法力枯竭、回填后还能打」
    # 的 PvP 对耗区分开——后者该继续,而非判死锁。
    #
    # 但判定时机受两条约束(2026-08-31 DM 裁定):
    #   1) 阈值必须 > MEDIOCRITY_ROUNDS,让【凡庸】先炸;
    #   2) 命中时不判胜负,只报错(兜底不得擅定擂主卫冕)。
    hp_frozen_rounds = 0
    for rnd in range(1, max_rounds + 1):
        if not _challenger_alive():
            return {"winner": "defender", "rounds": rnd, "reason": challenger_death_reason()}
        if not _lord_alive():
            return {"winner": "challenger", "rounds": rnd, "reason": lord_death_reason()}
        from sim.optional_actions import start_round
        # 记录本回合开局双方 hp，用于回合末判「真死锁」。
        hp_before = (e.state.player.current_hp if e.state.player else None,
                     tuple(x.current_hp for x in e.state.enemies if x.entity_type == "轮回者"))
        if resume and rnd == 1:
            # 续跑模式：第一轮跳过 round_start（状态本来就在回合中段），后续轮照常。
            rs = {"success": True}
        else:
            rs, _rsart = start_round(e)
        if not rs.get("success"):
            # 回始失败不允许吞掉:法力不会回填,双方将永久空转(2026-08-26 死斗三
            # "对手血契"校验事故)。显式报告提交错误并留因，不把程序错误算作规则败北。
            return {"winner": None, "rounds": rnd, "error": True, "error_kind": "contract",
                    "reason": f"回始提交失败: {str(rs.get('error', ''))[:80]}"}
        # 死锁防护:连续 STALL_GUARD_STEPS 步双方面板零变化 → 判卫冕(双有效行动
        # 枯竭的残局,如 1 血 0 法互瞪;不做 400 步 × 60 回合的无意义空转)。
        last_panel, panel_stall = None, 0
        for _ in range(max_steps):
            if _over_time():
                # 同死锁：墙钟超时是程序兜底，不得判胜负。
                return {"winner": None, "rounds": rnd, "error": True,
                        "reason": (f"死斗超时(>{max_wall_seconds:g}s)："
                                   "程序兜底报错，不作胜负判定"),
                        "timeout": True, "timeout_kind": "wall_clock",
                        "diag_state_sizes": _duel_state_sizes(e)}
            if not _challenger_alive():
                return {"winner": "defender", "rounds": rnd, "reason": challenger_death_reason()}
            if not _lord_alive():
                return {"winner": "challenger", "rounds": rnd, "reason": lord_death_reason()}
            panel = (
                (e.state.player.current_hp, e.state.player.shield, e.state.player.current_mana,
                 e.state.player.current_speed, e.state.current_round) if e.state.player else None,
                tuple((x.current_hp, x.shield, x.current_mana, x.current_speed)
                      for x in e.state.enemies if x.entity_type == "轮回者"),
            )
            panel_stall = panel_stall + 1 if panel == last_panel else 0
            last_panel = panel
            if panel_stall >= 50:
                # 本回合双方已无有效动作（法力/出手/可打出的牌耗尽）。这通常是
                # 「法力枯竭」而非死斗终局——先结束本回合让回始回填法力，下一回合
                # 继续对耗。真正的死锁（回填后仍无净变化）由回合末 hp_frozen_rounds 判定。
                break
            if e.state.duel_turn == "player_side":
                acted = player_act()
                if not acted:
                    ra = e.execute_action("resolve_ally_phases", {})
                    if not (ra.get("result", {}) or {}).get("acted_count", 0):
                        e.state.duel_turn = "opponent_side"  # 挑战者无行动 → 让守擂
            else:
                if _def_tai is not None:
                    acted = _def_tai.take_action()
                    _drain_ai_log(_def_tai, "守擂", log, _seen)
                    if acted and 对话 is not None:
                        对话.events += 1
                        pers = peek_personality(e, _def_tai.player)
                        line = render_line(_def_tai.player, "damage_out", pers)
                        if 对话.events in (4, 12, 20, 28):
                            对话.buf.append(line)
                        _publish_line(e, _def_tai.player, pers, log)
                else:
                    acted = _resolve_opponent_one(e, log, 对话)
                if not acted:
                    e.state.duel_turn = "player_side"  # 守擂无行动 → 让回挑战者
        # 本回合双方在死斗里都经"玩家侧接口"驱动（use_daowen/resolve_attack/普攻），
        # 这些 action 只在双方都不能行动时才把 subphase 推到 await_round_end；但守擂侧
        # 由 _resolve_opponent_one 驱动、不触发该收尾，subphase 会停在 player_actions，
        # 使次回合 round_start 撞 guard 失败 → 法力永不回填 → 死锁卫冕。
        # 这里显式收尾到 await_round_end，再调 round_end。
        e.state.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value
        re = e.execute_action("round_end", {})
        if not re.get("success"):
            return {"winner": None, "rounds": rnd, "error": True, "error_kind": "contract",
                    "reason": f"回终提交失败: {str(re.get('error',''))[:80]}"}
        # 回始：重置双方 AI 的回合记账（凡庸压力/本回合伤害标记/已控制目标）
        if use_tactical:
            _tai.new_round()
            if _def_tai is not None:
                _def_tai.new_round()
        # 回合进度记录（供诊断/报告展示战局推进）
        log.append(f"  ─ 第{rnd}回合结束：挑战者hp={e.state.player.current_hp if e.state.player else 0}"
                   f" 守擂hp={[(x.name, x.current_hp) for x in e.state.enemies if x.entity_type=='轮回者']}")
        # 真死锁判定:回始已回满法力、双方仍无任何净变化(伤害/回血),连续两回合零
        # 净变化即互瞪死锁——法力回填都救不了,判卫冕而非空转满 max_rounds。
        hp_after = (e.state.player.current_hp if e.state.player else None,
                    tuple(x.current_hp for x in e.state.enemies if x.entity_type == "轮回者"))
        frozen = hp_before == hp_after
        hp_frozen_rounds = hp_frozen_rounds + 1 if frozen else 0
        if hp_frozen_rounds >= DEADLOCK_MIN_ROUNDS:
            # 不判胜负：凡庸已让满 MEDIOCRITY_ROUNDS 回合仍僵持，属程序级异常，
            # 只报错交由上层处置，不得替规则宣布擂主卫冕。
            return {"winner": None, "rounds": rnd, "error": True, "timeout": False,
                    "error_kind": "deadlock",
                    "reason": (f"死斗死锁：回始回填后双方面板连续 {hp_frozen_rounds} 回合"
                               f"零净变化，达兜底阈值 {DEADLOCK_MIN_ROUNDS} 回合"
                               f"（须大于规则层反僵持的 {MEDIOCRITY_ROUNDS} 回合，"
                               "以保证凡庸先有机会结算）。"
                               "此为程序兜底报错，不作胜负判定。")}
    return {"winner": "defender", "rounds": max_rounds, "reason": "回合上限"}
