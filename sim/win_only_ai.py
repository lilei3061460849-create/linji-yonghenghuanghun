"""胜负唯一计分 AI（用户裁定 2026-09-10：胜 +1 / 败 −1，无论什么手段什么战术；
二审：**一切打分机制都用这个**，不只死斗——PvE 战斗、死斗、养蛊链全默认）。

与 TacticalAI 的区别只在**选择**：启发式效用分不再做最终裁决，只用来把
候选裁剪到前 N 个提案（省算力）；每个提案在引擎世界的深拷贝上真实打出：
  · 死斗（in_final_duel）：run_duel_pvp(resume=True) 推演到底 → ±1；
  · PvE 战斗：推演本场打到终局（清场 +1 / 阵亡 −1 / 上限未决 0）。
平局/未决=0；全部同分时按提案器首选出（胜负看不出差别时，听提案器的）。

世界切换镜像 engine/ai_preview.py::preview_sequence 的口径（state+dice 换入
engine 与 combat 双侧，外加 combat 的全部可变记账字典），推演完原样换回——
原世界的实体对象一个字节都不动。

用法：`run_duel_pvp(...)` 默认即本类；PvE 经 `build_learner._play(ai_cls=默认)`。
`win_only_cls(base)` 可以给任意提案器（如遗言桥 LegacyAwareAI）配上 ±1 裁决——
提案口径（含遗言偏见）留在裁剪阶段，胜负裁决权归推演。
代价：每手 ≤N 候选×整局推演，秒级/手；千局级扫描可 LJ_WIN_ONLY=0 或显式传基类。
"""
from __future__ import annotations

import contextlib
import copy
import os

from engine.ai_tactics import TacticalAI, MAX_CANDIDATE_PREVIEWS


def _enabled() -> bool:
    """总开关（默认开）。LJ_WIN_ONLY=0 可整体退回启发式口径（千局级扫描用）。"""
    return os.environ.get("LJ_WIN_ONLY", "1") != "0"


_PLAYOUT_DEPTH = [0]          # 推演世界嵌套深度（_swap_world 维护，诊断工具读取）


def playout_depth() -> int:
    """当前是否处于推演世界（>0=是）。诊断工具据此过滤实录。"""
    return _PLAYOUT_DEPTH[0]


class WinOnlyAI(TacticalAI):
    """提案=基类候选生成；裁决=整局推演的胜负 ±1。"""

    PLAYOUT_TOP_N = 4          # 每手推演的候选上限（启发式排序取前 N）
    PLAYOUT_MAX_ROUNDS = 30    # 死斗推演回合上限（与死斗扫描同口径）
    PLAYOUT_MAX_ROUNDS_PVE = 10 # PvE 推演回合上限（要装得下「爬梯5跳+透支爆发」的完整计划）
    PLAYOUT_ACTION_CAP = 40    # 推演内单轮玩家出手上限（防拒绝循环）

    # ---------- 世界切换（镜像 ai_preview 的换世界口径，整场推演后丢弃副本） ----------

    def _swap_world(self):
        """context manager：把引擎换到深拷贝世界；退出时原样换回。

        进入/退出维护模块级 `_PLAYOUT_DEPTH`（可重入）：诊断工具（duel_diff_trace
        等）据此把推演世界的 execute_action 排除出「真实结算」实录——修复前
        推演世界的出手被当 [真实] 打印，是「68 盾/法力 51」类假账证据的来源。
        """

        @contextlib.contextmanager
        def _cm():
            _PLAYOUT_DEPTH[0] += 1
            eng = self.engine
            combat = eng.combat
            real = {
                "state": eng.state, "dice": eng.dice,
                "combat_state": combat.state, "combat_dice": combat.dice,
                "pending": eng._pending_interrupts,
                "hist_len": len(eng._action_history),
                "last": eng._last_result,
                "activated": combat._monster_activated,
                "round_used": combat._monster_daowen_round_used,
                "rewrites": combat._resonance_rewrites,
                "split": getattr(combat, "_split_clones_spawned", 0),
                "evolved": combat._monster_evolved,
                "depth": combat._effect_chain_depth,
            }
            eng.state = copy.deepcopy(real["state"])
            combat.state = eng.state
            eng.dice = copy.deepcopy(real["dice"])
            combat.dice = eng.dice
            combat._monster_activated = copy.deepcopy(real["activated"])
            combat._monster_daowen_round_used = copy.deepcopy(real["round_used"])
            combat._resonance_rewrites = copy.deepcopy(real["rewrites"])
            combat._split_clones_spawned = copy.deepcopy(real["split"])
            combat._monster_evolved = copy.deepcopy(real["evolved"])
            combat._effect_chain_depth = copy.deepcopy(real["depth"])
            eng._pending_interrupts = copy.deepcopy(real["pending"])
            try:
                yield
            finally:
                eng.state = real["state"]
                combat.state = real["combat_state"]
                eng.dice = real["dice"]
                combat.dice = real["combat_dice"]
                combat._monster_activated = real["activated"]
                combat._monster_daowen_round_used = real["round_used"]
                combat._resonance_rewrites = real["rewrites"]
                combat._split_clones_spawned = real["split"]
                combat._monster_evolved = real["evolved"]
                combat._effect_chain_depth = real["depth"]
                eng._pending_interrupts = real["pending"]
                del eng._action_history[real["hist_len"]:]
                eng._last_result = real["last"]
                _PLAYOUT_DEPTH[0] -= 1
        return _cm()

    # ---------- 闭环爬梯（训练内容：改写自己的 kit 走设计好的技术树） ----------

    # 闭环节点里的输出牌（按 engine/daowen.py 公式归读；爬梯不许把最后一张
    # 输出牌改没——先有锤子，才谈得上 透支喂锤/封印清场）。
    DAMAGE_NODES = {"杀伐", "血债", "波及", "贯穿"}
    GOALS = ("透支", "封印")   # 透支=衰老换4X法力（破法力预算）；封印=异变换移怪（无视血墙）

    @staticmethod
    def _loop_graph() -> dict:
        """杀伐闭环邻接表：{源: [(残韵类型, 目标)]}。事实源=引擎 CLOSED_LOOPS。"""
        from engine.daowen import ResonanceEngine
        nxt: dict = {}
        for s, rtype, d in ResonanceEngine.CLOSED_LOOPS.get("杀伐闭环", []):
            nxt.setdefault(s, []).append((rtype, d))
        return nxt

    def _loop_distances(self) -> dict:
        """各节点到目标集 {透支, 封印} 的**有向**最短步数（BFS 反向边）。"""
        from collections import deque
        graph = self._loop_graph()
        rev: dict = {}
        for s, outs in graph.items():
            for _rtype, d in outs:
                rev.setdefault(d, []).append(s)
        dist = {g: 0 for g in self.GOALS}
        q = deque(self.GOALS)
        while q:
            n = q.popleft()
            for p in rev.get(n, ()):
                if p not in dist:
                    dist[p] = dist[n] + 1
                    q.append(p)
        return dist

    def _path_types_to_goal(self, start: str) -> list[str] | None:
        """从 start 沿有向闭环走到最近目标的**完整路径**（残韵类型序列）。
        返回 None = 无路可达。由 _loop_distances 的 BFS 性质：沿 dist 递减走。"""
        graph = self._loop_graph()
        dist = self._loop_distances()
        if start not in dist:
            return None
        cur, types = start, []
        seen = set()
        while dist.get(cur, 99) > 0:
            if cur in seen:
                return None
            seen.add(cur)
            step = next(((rt, d) for rt, d in graph.get(cur, ())
                         if dist.get(d, 99) == dist[cur] - 1), None)
            if step is None:
                return None
            rtype, cur = step
            types.append(rtype)
        return types

    def _ladder_candidates(self) -> list[dict]:
        """爬梯候选：对自己发动残韵，沿闭环把 kit 向 透支/封印 推进。

        训练点（2026-09-10 六审，用户「训练 AI」）：旧提案器只对敌方道纹用残韵
        （_resonance_candidates 只枚举敌方持有），从不改写自己的 kit——设计好的
        「用其他道纹解决」路径无人走过。这里补上自改写提案：
          · 只走**贴向目标**的边（distance 严格递减，多回合逐跳爬）；
          · 不把最后一张输出牌改没（DAMAGE_NODES 守卫）；
          · 已持有目标牌则跳过（同名只留一份，白烧残韵）。
        单跳在推演里通常看不出价值（0 分）——胜负同分时按「距目标更近」裁决，
        即把多跳计划编码进平局裁决（跨回合逐跳执行）。
        """
        from engine.ai_tactics import daowen_text_kind
        if self.player is self.engine.state.player:
            src_stock = self.engine.state.resonance
        else:
            src_stock = getattr(self.player, "resonance", None) or {}
        stock = {k: v for k, v in (src_stock or {}).items() if v > 0}
        if not stock:
            return []
        dist = self._loop_distances()
        graph = self._loop_graph()
        held = set(self.player.dao_wen.keys())
        dmg_held = [n for n, inst in self.player.dao_wen.items()
                    if daowen_text_kind(inst) == "damage"]
        out = []
        for s in list(self.player.dao_wen.keys()):
            for rtype, d in graph.get(s, ()):
                if stock.get(rtype, 0) <= 0 or d in held:
                    continue
                sd, dd = dist.get(s, 99), dist.get(d, 99)
                if dd >= sd:
                    continue
                if s in dmg_held and len(dmg_held) == 1 and d not in self.DAMAGE_NODES:
                    continue   # 不把最后一张输出牌改没
                # 可行性门（2026-09-10 六审迭代）：只有当**剩余全程**的残韵类型
                # 库存都付得起时才爬——买不起全程的计划是纯亏节奏（实测爬梯
                # 无门时 cleared 合计 38→32：浅局爬两步就死）。计划要承诺到底。
                full_path = self._path_types_to_goal(d)
                if full_path is None:
                    continue
                need: dict = {}
                for rt in [rtype] + full_path:
                    need[rt] = need.get(rt, 0) + 1
                if any(stock.get(rt, 0) < n for rt, n in need.items()):
                    continue
                params = {"source_daowen": s, "resonance_type": rtype,
                          "target": self.player.name,
                          "target_ref": self._target_ref_for(self.player)}
                if self._actor_ref:
                    params["actor_ref"] = self._actor_ref
                out.append((dd, {
                    "action": "use_resonance", "kind": "tactician",
                    "label": f"爬梯·{s}--{rtype}→{d}(距{self.GOALS[0]}/{self.GOALS[1]}{dd}步)",
                    "params": params, "_ladder_dist": dd}))
        out.sort(key=lambda t: t[0])
        return [c for _, c in out[:2]]

    # ---------- 推演计分 ----------

    def _my_sign(self, winner: str) -> int:
        """按我坐哪席把死斗结局换算成 ±1。actor=None=挑战席（驱动 state.player）。"""
        i_am_challenger = self._actor is None
        if winner == "challenger":
            return 1 if i_am_challenger else -1
        if winner == "defender":
            return -1 if i_am_challenger else 1
        return 0

    def _playout_score(self, cand: dict) -> int:
        """在世界深拷贝上真实打出本手，再推演到底，返回 ±1/0（异常=0 未决）。

        副作用：self._last_survival = 推演内自己存活的回合数（平局裁决用——
        同样要输，选择死得晚的走法；自爆型候选（如异变线上封印）因此落选）。"""
        self._last_survival = 0
        try:
            with self._swap_world():
                if cand.get("steps"):
                    # steps=(action, params|callable(prev))；_run_steps 走
                    # self.engine.execute_action——此刻世界已是副本，整串（含闪避
                    # 中继的 callable 串联）都打在推演世界里。
                    r = self._run_steps(cand["steps"])
                else:
                    r = self.engine.execute_action(cand["action"], cand["params"])
                if not r.get("success"):
                    return -1   # 非法提案=白给一手（提案器已预演过滤，理论少见）
                if getattr(self.engine.state, "in_final_duel", False):
                    return self._playout_duel()
                return self._playout_pve()
        except Exception:
            self._last_survival = 0
            return 0            # 推演世界出意外=未决；不得波及真实世界

    def _playout_duel(self) -> int:
        from sim.duel_pvp import run_duel_pvp
        from engine.ai_tactics import TacticalAI as _Base
        verdict = run_duel_pvp(self.engine, None,
                               max_rounds=self.PLAYOUT_MAX_ROUNDS,
                               max_steps=400, log=[], use_tactical=True,
                               ai_cls=_Base,      # 推演内环=基类：防递归推演
                               resume=True)
        return self._my_sign(verdict.get("winner") or "")

    def _playout_pve(self) -> int:
        """PvE：把本场从当前时点推演到终局。清场 +1 / 阵亡 −1 / 上限 0。

        驱动=build_learner 同款（基类出手 → 朋友员工 → 怪物阶段 → 回终/回始），
        例外/门禁按「未决」处理——推演是裁决器，不是被测对象。
        """
        from engine.ai_tactics import TacticalAI as _Base
        from sim.build_learner import (_resolve_monster_turn,
                                       _resolve_pending_choices,
                                       _drive_plight_monsters)
        from sim.build_learner import start_round_with_artifacts
        e = self.engine
        base = _Base(e)
        rounds = self.PLAYOUT_MAX_ROUNDS_PVE
        survived = 0
        for _ in range(rounds):
            survived += 1
            self._last_survival = survived
            # 本回合剩余玩家出手
            for _a in range(self.PLAYOUT_ACTION_CAP):
                if not [x for x in e.state.enemies if x.is_alive] and not getattr(e.state, "monster_reinforcements", None):  # 波次：增援未到不算清场
                    return 1
                if not e.state.player or not e.state.player.is_alive:
                    return -1
                if not base.take_action():
                    break
            if not [x for x in e.state.enemies if x.is_alive] and not getattr(e.state, "monster_reinforcements", None):  # 波次：增援未到不算清场
                return 1
            if not e.state.player or not e.state.player.is_alive:
                return -1
            e.execute_action("resolve_ally_phases", {})
            if not [x for x in e.state.enemies if x.is_alive] and not getattr(e.state, "monster_reinforcements", None):  # 波次：增援未到不算清场
                return 1
            _drive_plight_monsters(e)
            if not [x for x in e.state.enemies if x.is_alive] and not getattr(e.state, "monster_reinforcements", None):  # 波次：增援未到不算清场
                return 1
            mp = _resolve_monster_turn(e)
            if not mp.get("success") or (mp.get("result") or {}).get("player_dead"):
                return -1
            _resolve_pending_choices(e)
            re_ = e.execute_action("round_end", {})
            if not re_.get("success"):
                _resolve_pending_choices(e)
                re_ = e.execute_action("round_end", {})
                if not re_.get("success"):
                    return 0
            if not e.state.player or not e.state.player.is_alive:
                return -1     # 回终结算（癌变/凡庸）命零
            rs, _logs = start_round_with_artifacts(e)
            if not rs.get("success"):
                return 0
            base.new_round()
        return 0               # 上限内推不出胜负（磨血局常态）

    # ---------- 选择：提案裁剪 + ±1 裁决 ----------

    def _dynamic_action(self):
        candidates = []
        candidates.extend(self._daowen_candidates())
        basic_candidates = self._basic_attack_candidates()
        candidates.extend(basic_candidates)
        # 普攻必须保留在提案裁剪中；否则大量道纹候选会把它截掉，导致
        # WinOnlyAI连“先打一下再控场”的基本方案都看不到。
        if basic_candidates:
            reserve = min(len(basic_candidates), MAX_CANDIDATE_PREVIEWS)
            non_basic = [cand for cand in candidates if cand not in basic_candidates]
            candidates = (non_basic[:MAX_CANDIDATE_PREVIEWS - reserve]
                          + basic_candidates[:reserve])
        else:
            candidates = candidates[:MAX_CANDIDATE_PREVIEWS]
        immediate_damage_available = False
        immediate_damage_candidates: set[int] = set()
        # 残韵候选已按威胁预筛，并附带一个基础分：既含「我要压制什么」（威胁），
        # 也含「我能拿到什么」（转化后收益，2026-09-13 补上的那一半）。
        # 这个分只用于**提案裁剪的排序**，不进入最终裁决——最终裁决仍然只认
        # 整局推演的 ±1 结局（「胜负唯一计分」裁定不变）。此前它被整个丢弃，
        # 结果是残韵候选常常挤不进 PLAYOUT_TOP_N，推演席上根本看不到它们。
        resonance_bonus: dict = {}
        for bonus, cand in self._resonance_candidates():
            candidates.append(cand)
            resonance_bonus[id(cand)] = bonus
        # 提案阶段：预演只用来过滤非法与必死候选；启发式分只用于裁剪排序，
        # 不做最终裁决（最终裁决=整局结局 ±1）。
        scored = []
        for cand in candidates[:MAX_CANDIDATE_PREVIEWS]:
            pv = (self.previewer.preview_sequence(cand["steps"]) if cand.get("steps")
                  else self.previewer.preview(cand["action"], cand["params"]))
            res = pv.get("result") or {}
            if not res.get("success"):
                continue
            diff = pv.get("diff", {})
            s = self._score_candidate(diff, cand["label"],
                                      cand.get("kind"), cand.get("target"))
            if cand.get("kind") in ("attack", "damage"):
                # 不能把“敌人暂离/封印”产生的 hp_after=0 当成伤害。
                # 只有真实 damage_applied 且 actual_damage>0 才算完成首次接触。
                actual_damage = any(
                    ev.get("type") == "damage_applied"
                    and (ev.get("data") or {}).get("actual_damage", 0) > 0
                    for ev in ((diff or {}).get("events", []) or []))
                if actual_damage:
                    immediate_damage_available = True
                    immediate_damage_candidates.add(id(cand))
            if s is None:
                continue
            # 崩解线否决（训练 2026-09-10 六审）：异变 ≥ 50 即血 0 自爆
            # （seed5 实锤：异变 48 放封印 X=1 → 56 → 血 0）。推演视角看不到
            # 跨回合崩解结算，故在提案层显式否决「 projected ≥ 50 且非终结本
            # 场」的出手——纯 AI 知识，不碰引擎规则面。终结（all_gone）豁免：
            # 封印清场结束战斗不再进入崩解回合。
            p_diff = pv.get("diff", {}).get("player", {}) or {}
            projected = (getattr(self.player, "mutation_count", 0)
                         + (p_diff.get("mutation_delta") or 0))
            if projected >= 50 and not pv.get("all_gone"):
                continue
            scored.append((s + resonance_bonus.get(id(cand), 0.0), cand))
        # 第一次接触先造成实际伤害，再允许下一回合使用【封印】等控制。
        # 这是对“连续封印、五回合零伤害、被【凡庸】处决”的通用修复，
        # 不按道纹名称特判，只看真实预演是否存在安全的伤害候选。
        force_progress = (not self._first_contact()
                          and getattr(self, "_last_action_caused_damage", True) is False)
        if immediate_damage_available and (self._first_contact() or force_progress):
            progress = [row for row in scored
                        if id(row[1]) in immediate_damage_candidates]
            if progress:
                scored = progress
        scored.sort(key=lambda t: (-t[0], t[1].get("label", "")))
        proposals = [c for _, c in scored[:self.PLAYOUT_TOP_N]]
        # 爬梯候选直通推演席（不受启发式排序门槛限制）：改写自己的 kit 沿闭环
        # 向 透支/封印 推进——「用其他道纹解决」的设计路径（用户裁定 2026-09-10）。
        ladder = self._ladder_candidates()
        have = {(c.get("action"), tuple(sorted((c.get("params") or {}).items())))
                for c in proposals}
        for c in ladder:
            key = (c.get("action"), tuple(sorted((c.get("params") or {}).items())))
            if key not in have:
                proposals.append(c)
        if not proposals:
            return None
        best, best_s, best_ladder, best_surv = None, -2, 99, -1
        for cand in proposals:
            s = self._playout_score(cand)
            ld = cand.get("_ladder_dist", 99)
            surv = getattr(self, "_last_survival", 0)
            # 裁决序：±1 → 死得晚（同败局多活=规则钟变数+不自爆）→ 爬梯进度。
            if (s > best_s
                    or (s == best_s and surv > best_surv)
                    or (s == best_s and surv == best_surv and ld < best_ladder)):
                best_s, best, best_ladder, best_surv = s, cand, ld, surv
        # 全部同分（含全 −1）：结果对胜负无差别——若爬梯有进展就爬一格
        # （多跳计划跨回合逐跳执行），否则照提案器首选。
        self._last_action_caused_damage = id(best) in immediate_damage_candidates
        r = (self._run_steps(best["steps"]) if best.get("steps")
             else self.engine.execute_action(best["action"], best["params"]))
        if r.get("success"):
            base = best["label"].split("X=")[0].split("→")[0]
            self.used[base] = self.used.get(base, 0) + 1
            return r
        return None


def win_only_cls(proposer: type = TacticalAI) -> type:
    """给任意提案器配上 ±1 裁决：候选/裁剪口径用 proposer，胜负裁决用推演。

    例：`win_only_cls(LegacyAwareAI)`=遗言桥仍影响「试什么」，但「选什么」只认结局。
    """
    return type("WinOnly" + proposer.__name__, (WinOnlyAI, proposer), {})
