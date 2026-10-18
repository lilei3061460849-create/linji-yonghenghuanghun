"""
战术AI：为轮回者在战斗中选择行动。

2026-08-26 防公式化改造（DM裁决）：固定战术表（TACTICAL_ROLES）、固定残韵
目标表（HIGH_VALUE_ENEMY_DAOWEN）与固定策略顺序（STRATEGIES 串）已删除——
所有角色按同一张表出牌必然公式化。

现行决策方式（实时、状态驱动）：
  1. 候选生成：只看玩家**实际持有**的道纹 × 若干X × 合法目标（含残韵候选）；
  2. 逐候选经 ActionPreview 在**真实引擎管线**副本上预演，得到完整后果 diff；
  3. 按「当前局势（威胁/血线/法力/凡庸压力）+ 角色性格（personality_traits，
     实例级、行为推断而来）+ 可见信息（diff 数值）」实时打分；
  4. 最高分执行；预演致轮回者命零（LETHAL）的候选一律拒绝。
AI 不复制任何引擎公式；新增/修改道纹零维护（预演即事实源）。

性格只调制**倾向**（风险代价容忍度/节俭/求新/先观察后行动……），
不是强制规则——同一性格在不同局面可以打出不同牌。

兼容层：旧 try_* 策略名保留为「类别策略」薄封装（类别由预演 diff 归纳，
非历史经验表），供 tests 与 archive 实验（sim/victory_path_tournament 等
子类化 TacticalAI 的脚本）继续运行；基类 STRATEGIES=None，主入口
take_action 走实时评估，不再有固定顺序。
"""
from __future__ import annotations

import math
import os
from typing import Any, Optional
from engine.ai_preview import ActionPreview
# 招架减免/可用次数的规则真源（AI 不得自己再写一遍公式，见 engine/combat.py）

# 单次出手最多预演的候选数（性能护栏；候选按新鲜度与威胁优先）
MAX_CANDIDATE_PREVIEWS = 26

# 自保时钟（**默认开启**，2026-09-10 用户裁定）：把引擎自己的三条「自爆时钟」接进
# 候选打分。DM 裁定 2026-08-31 曾要求 132 局前后对照再定常开；因擂主农场 0/24 空转
# 跑不出对照，改按 24 种子养蛊证据裁定（纯AI层 56 → 开 59，+3 零副作用；复验两次
# 持平）。132 局对照留待擂主农场恢复后补。置 LJ_SELF_PRESERVE=0 可显式关闭复现旧行为。
SELF_PRESERVE = os.environ.get("LJ_SELF_PRESERVE", "1").strip().lower() not in {"0", "false", "no", "off"}


# ---------------------------------------------------------------------------
# 道纹效果分类：不查表，从「可见文本」做弱分类（仅用于威胁评估与兼容壳；
# 真正的候选评估一律以预演 diff 数值为准）。
# ---------------------------------------------------------------------------
_KIND_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("remove",   ("移出战斗", "移出", "封印")),
    ("heal",     ("回复", "恢复", "治疗")),
    ("shield",   ("格挡",)),
    ("control",  ("无法行动", "束缚", "蒙蔽", "无法出手", "攻击次数固定",
                  "强制目标", "缄默", "沉默")),
    ("debuff",   ("削弱", "受到伤害+", "失去生命", "衰老", "弱化", "退化", "衰减")),
    ("ramp",     ("重置随机", "获得法力", "法力+")),
    ("buff",     ("获得", "提升", "免疫", "翻倍", "速度+", "血限+")),
    ("damage",   ("伤害",)),
)


def daowen_text_kind(inst) -> str:
    """按道纹可见文本给出弱分类（引擎事实，非经验）。无法判断返回 'other'。"""
    if inst is None:
        return "other"
    dw = getattr(inst, "dao_wen", inst)
    text = " ".join(filter(None, (
        getattr(dw, "formula", ""), getattr(dw, "effect_formula", ""),
        getattr(dw, "trigger_timing", ""), getattr(dw, "cost_type", ""),
    )))
    for kind, keys in _KIND_HINTS:
        if any(k in text for k in keys):
            return kind
    return "other"


# 兼容壳的旧角色名 → 弱分类集合（owned() 用）
_ROLE_KINDS = {
    "nuke": ("damage",), "aoe": ("damage",), "finisher": ("damage",),
    "mark": ("damage",), "shield": ("shield",), "heal": ("heal",),
    "control": ("control", "tactician"), "debuff": ("debuff", "tactician"),
    "buff": ("buff",), "remove": ("remove",), "ramp": ("ramp",),
    "reroll": ("ramp",),
}


class TacticalAI:
    """轮回者战斗AI。engine 为 GameEngine 实例。

    基类无固定策略顺序（STRAGIES=None）：take_action 实时评估候选。
    实验子类可自定义 STRATEGIES 串走旧级联（仅为兼容 archive 实验）。
    """

    # 基类不再有固定顺序；子类（archive 实验脚本）可覆写为方法名元组。
    STRATEGIES: Optional[tuple] = None

    def __init__(self, engine: Any, verbose: bool = False, actor: Any = None,
                 enemies: Optional[list] = None, actor_ref: Optional[str] = None):
        self.engine = engine
        self.verbose = verbose
        # 视角重定向（2026-08-30）：PvP 死斗里挑战者与守擂者都是「轮回者」，共用同一套
        # TacticalAI（残韵候选 + 性格调制 + 变数决策），双方本应走完全相同的决策逻辑。
        # 早期 TacticalAI 硬绑定 player=state.player、enemies=state.enemies，只适用于
        # 挑战者视角；守擂者必须能指定自己的 actor/enemies 才能共享这套机制。
        # actor/enemies 为空时保持原行为（普通战斗/挑战者视角）。
        self._actor = actor
        self._enemies = enemies
        self._actor_ref = actor_ref
        self.log: list[str] = []
        # 最近一次实时决策的引擎动作；统一 AI 外壳用它记录同一条决策链，
        # 旧的直接使用 TacticalAI 的实验脚本也可以忽略该字段。
        self.last_decision: Optional[dict] = None
        self.used: dict[str, int] = {}   # 统计各道纹发动次数，便于流派对比
        self._controlled_this_round: set = set()   # 本回合已被控制的目标
        self._previewer = None           # 行动后果预演器（惰性创建）
        self.preview_rejected: list[str] = []   # 被安全过滤淘汰的候选
        self._sacrifice_actions: set = set()    # 显式允许的主动牺牲策略（默认空）
        # 运行器/实验可显式屏蔽某张牌，但真正的合法性与结算仍由 GameEngine 校验。
        self.blocked_daowen_names: set[str] = set()
        self._last_risk: tuple = ("SAFE", [])   # 最近一次候选的风险等级
        # 第十九批实验钩子（默认 None = 现行≤40%血线门，行为不变）
        self.consumable_gate = None
        self._hp_trace: list = []
        # 实时决策记账
        self._probe_cache: dict = {}     # (name, target, battle) → 预演归纳的分类/单价
        self._ptraits: dict = {}         # 本回合的性格权重缓存（score×confidence）
        self._damage_done_battle = False # 本回合是否已使敌方掉血（凡庸压力）
        self._rounds_since_damage = 0    # 连续未使敌方掉血的回合数
        # 硬伤3（红线 E 已解除 2026-08-30）：对手台词在**我**心里的可信度。
        # 只由我自己的性格算出，不查对手真实状态；None=没听到有实质主张的话。
        self._opponent_read: Optional[dict] = None
        self._last_logged_read = None    # 已记录过的读数（防同一句刷屏）

    @property
    def previewer(self):
        """行动后果预演器（惰性创建）。"""
        if self._previewer is None:
            from engine.ai_preview import ActionPreview
            self._previewer = ActionPreview(self.engine)
        return self._previewer

    # ---------- 基础工具 ----------

    @property
    def player(self):
        if self._actor is not None:
            return self._actor
        return self.engine.state.player

    def alive_enemies(self) -> list:
        if self._enemies is not None:
            return [e for e in self._enemies if e.is_alive]
        return [e for e in self.engine.state.enemies if e.is_alive]

    def mana(self) -> int:
        return self.player.current_mana

    def owned(self, role: str) -> list[str]:
        """兼容壳：持有道纹中属于该旧角色者（文本弱分类，空文本回退预演归纳）。"""
        kinds = _ROLE_KINDS.get(role, ())
        out = []
        for n, inst in sorted(self.player.dao_wen.items()):
            if inst is None or not inst.can_use():
                continue
            kind = daowen_text_kind(inst)
            if kind == "other":
                kind = (self._probe(n) or {}).get("kind", "other")
            if kind in kinds:
                out.append(n)
        return out

    def incoming_damage(self) -> int:
        return sum(e.effective_attack_count() * e.effective_attack_power()
                   for e in self.alive_enemies())

    def remaining_actions(self) -> int:
        # 2026-09-10：出手次数不再由速度换算，改读 action_count（全体角色基础2次，
        # 疯狂/无力/蓄锐·增的修正已含在属性里；朋友/员工不再按攻次算）。
        # 沿用 ceil(速限/3) 会让 AI 按旧口径估预算，法力分配跟着算错。
        total = max(1, self.player.action_count)
        return max(0, total - getattr(self.player, "actions_used_this_round", 0))

    def mana_budget(self) -> int:
        """按剩余出手次数均分法力，避免一次梭哈导致后续出手闲置。"""
        left = self.remaining_actions()
        if left <= 1:
            return self.mana()
        return max(1, self.mana() // left)

    def _refresh_personality(self) -> None:
        """拉取当前性格权重（实例级；无性格=全 0，退化为纯局势效用）。"""
        from engine.personality import get_personality
        data = get_personality(self.engine.state, self.player) or {}
        self._ptraits = {
            dim: entry["score"] * entry.get("confidence", 0.0)
            for dim, entry in (data.get("traits") or {}).items()
        }

    def _w(self, dim: str) -> float:
        """性格维度权重 ∈ [-1,1]（score×confidence，证据不足自然影响小）。"""
        return self._ptraits.get(dim, 0.0) or 0.0

    # ---------- 硬伤3：读对手的台词（红线 E 已解除） ----------

    def _refresh_opponent_read(self) -> None:
        """读战场公开频道里对手最后一句有实质主张的话，按**我自己的性格**判可信度。

        说话没有数值作用，但**让敌人忌惮**就是最大的作用：A 说自己没法力了，
        我得自己判断那是真的弹尽粮绝，还是在骗我全力压上然后收割。
        信不信只取决于我的性格——引擎绝不代我去查 A 的法力。
        """
        try:
            from engine.dialogue import read_opponent
            self._opponent_read = read_opponent(self.engine.state, self.player,
                                                traits=self._ptraits)
        except Exception:
            self._opponent_read = None

    DIALOGUE_BIAS_CAP = 10.0

    def _dialogue_bias(self, *, enemy_hp_loss: float, shield_useful: float,
                       heal: float, mana_spent: float) -> float:
        """按"我有多信对手那句话"微调本候选的分数（**不产生任何数值效果**）。

        含义（belief>0 = 信以为真；belief<0 = 认为是反话）：
          · 对手喊虚(weak)：信 → 趁势压上收割（进攻加值、龟缩减值）；
                           不信 → 怀疑是引我全力出击再收割（进攻减值、防御加值）。
          · 对手威胁(strong)：信 → 忌惮，加防、少冒进（这就是"让敌人忌惮"）；
                            不信 → 当它是虚张，照打。
          · 对手试探(probe)：不下断言，只是不轻易把法力梭哈在一手上。

        这是倾向而非规则：上限 DIALOGUE_BIAS_CAP=10，压不过 CRITICAL(-30)/
        LETHAL 等安全护栏，只在"压上"与"收手"之间替我把天平拨一点。
        """
        read = self._opponent_read
        if not read:
            return 0.0
        claim = read.get("claim")
        belief = float(read.get("belief") or 0.0)
        if claim not in ("weak", "strong", "probe") or abs(belief) < 0.05:
            return 0.0
        guard = shield_useful + heal
        if claim == "weak":
            bias = 0.9 * belief * enemy_hp_loss - 0.9 * belief * guard
        elif claim == "strong":
            bias = -0.9 * belief * enemy_hp_loss + 1.5 * belief * guard
        else:                                   # probe：不梭哈
            bias = -0.25 * abs(belief) * mana_spent
        return max(-self.DIALOGUE_BIAS_CAP, min(self.DIALOGUE_BIAS_CAP, bias))

    # ---------- 预演与归纳 ----------

    def _probe(self, name: str) -> Optional[dict]:
        """对道纹做多目标 X=1 预演，按实际效果方向归纳其事实特征（缓存至本场）。

        引擎会"默许"部分指向错误的目标（如庇护打向敌人=空效果），因此必须
        对 self / 敌方 等变体各预演一次，取**效果方向最优**的成功变体：
        敌方掉血 → damage；自身获益 → shield/heal/buff；敌方被移除 → remove；
        无面板位移 → tactician（控场/削弱/标记类，目标取该变体目标）。
        返回 {kind, cost_per_x, dmg, shield, heal, target_name}；全拒绝返回 None。
        """
        battle = self.engine.state.current_battle
        cache_key = (name, "*", battle)
        cached = self._probe_cache.get(cache_key)
        if cached is not None:
            return cached if cached.get("target") == "ok" else None

        best: Optional[dict] = None
        best_score = -1.0
        seen_targets = set()
        for target in (self._top_enemy_name(), self.player.name, None,
                       self._ally_name()):
            if target in seen_targets or target is None and None in seen_targets:
                continue
            seen_targets.add(target)
            key = (name, target, battle)
            per_key = self._probe_cache.get(key)
            if per_key is not None and per_key.get("target") == "reject":
                continue
            # 与正式发动共用稳定引用解析，避免重名怪物让预演走入“目标不唯一”分支。
            params = self._params(name, 1, target)
            pv = self.previewer.preview("use_daowen", params)
            res = pv.get("result") or {}
            if not res.get("success"):
                self._probe_cache[key] = {"target": "reject"}
                continue
            info = self._digest_diff(pv.get("diff", {}))
            info["target"] = "ok"
            info["target_name"] = target
            self._probe_cache[key] = info
            # 效果方向分：敌方掉血 > 自身获益 > 空效果。
            # 敌方被上状态（控场/削弱）同样计正向价值：否则纯 debuff 牌的
            # 「打敌人」「打自己」两个变体并列 0 分，探测会把真脸误锁到自指。
            orientation = (2.0 * info["dmg"] + info["shield"] + info["heal"]
                           + 0.5 * info.get("mana_gain", 0)
                           + 0.5 * info.get("bl_gain", 0)
                           + (6.0 if info["kind"] == "remove" else 0.0)
                           + (3.0 if info.get("status_on_enemy") else 0.0))
            # 窄幅并列裁决：仅当现任真脸在「喂敌人」（敌方 HP 净增）而本变体
            # 不喂时才翻转。不采用宽幅「并列优先自指」——满血自疗等无害并列
            # 也被翻转会在地下城改变既有行为（骨天使回归 cleared 1→0 实测）。
            if (orientation > best_score
                    or (abs(orientation - best_score) <= 1e-9
                        and info.get("foe_gain", 0) <= 0
                        and best is not None and best.get("foe_gain", 0) > 0)):
                best_score = orientation
                best = info
        self._probe_cache[cache_key] = best if best is not None else {"target": "reject"}
        return best

    def _dmg_at(self, name: str, x: int, target: Optional[str]) -> int:
        """预演指定 X 的实际伤害（事件流 raw_damage 口径），失败返回 0。

        为什么不能用「X=1 单价 × X」外推：伤害公式未必线性。【杀伐】自
        2026-09-13 起为 X² 点伤害，按 X=1（1 点）线性外推会把斩杀所需 X
        算成实际值的平方倍 —— 收割档因此被「买不起」剪掉，AI 明明一发就能
        打死也不出手。这里对具体 X 直接预演，让事实说话。
        """
        if x < 1:
            return 0
        battle = self.engine.state.current_battle
        key = ("__dmg__", name, x, target, battle)
        cached = self._probe_cache.get(key)
        if cached is not None:
            return cached
        # 伤害档位探测也必须使用与正式发动相同的稳定目标引用。
        params = self._params(name, x, target)
        pv = self.previewer.preview("use_daowen", params)
        if not (pv.get("result") or {}).get("success"):
            self._probe_cache[key] = 0
            return 0
        dmg = self._digest_diff(pv.get("diff", {})).get("dmg", 0)
        self._probe_cache[key] = dmg
        return dmg

    def _x_to_kill(self, name: str, need: int, target: Optional[str],
                   cap: int = 9) -> int:
        """求打出 >= need 伤害所需的最小 X（上限 cap），无解返回 0。

        从小到大逐档真实预演，命中即返回 —— 不能用「单价 × X」外推：伤害
        公式未必线性，且预演在法力不足时会直接失败（dmg=0），线性初猜那一
        档常常根本不可行。上界同时受 cap 与「买得起」约束。
        """
        probe = self._probe(name)
        per = (probe or {}).get("dmg", 0)
        if per <= 0:
            return 0
        cost = probe.get("cost_per_x", 0) or 0
        hi = min(cap, max(1, self.mana() // cost)) if cost > 0 else cap
        for x in range(1, hi + 1):
            if self._dmg_at(name, x, target) >= need:
                return x
        return 0

    def _target_ref_for(self, entity) -> str:
        """按实体找其稳定战斗引用（player:0 / enemy:N）。用于 use_resonance 的 target_ref。"""
        refs = self.engine.combat._combat_entity_refs()
        for ref, ent in refs.items():
            if ent is entity:
                return ref
        return getattr(entity, "name", "")

    def _top_enemy_name(self) -> Optional[str]:
        enemies = self.alive_enemies()
        if not enemies:
            return None
        return max(enemies,
                   key=lambda e: e.effective_attack_count() * e.effective_attack_power()).name

    def _ally_name(self) -> Optional[str]:
        allies = self._allies()
        return allies[0].name if allies else None

    def _split_diff(self, diff: dict) -> tuple[dict, list]:
        """把引擎 diff 的键控翻到**行动者视角**，返回 (自己快照, 敌方快照列表)。

        引擎 diff 永远以 state.player（挑战者）为 player 键、守擂方为 enemies。
        挑战者侧 AI（actor=None）直接可用；守擂者视角若不翻转，会把挑战者的
        掉血算成自己的自伤、把挑战者的回血/护盾当成自己的受益、把自己打出的
        击杀读成 player_dead=LETHAL 而拒绝出手（换席实录 seed1 守擂自施坏死、
        挑战者治疗送礼皆与此同源的视角错位相关）。
        """
        me = self.player.name if self.player else ""
        p = diff.get("player", {}) or {}
        enemies = diff.get("enemies", []) or []
        own = next((x for x in enemies if x.get("name") == me), None)
        if own is not None:   # 守擂者视角：镜像翻转
            foes = ([p] if p else []) + [x for x in enemies if x.get("name") != me]
            return own, foes
        return p, enemies     # 挑战者视角：引擎键控即自身视角

    def _digest_diff(self, diff: dict) -> dict:
        """把 X=1 预演 diff 归纳为 {kind, cost_per_x, dmg, shield, heal, ...}。

        kind 与 dmg 一律以**事件流**为准（面板位移会被格挡吸收等遮蔽）：
        damage_applied 命中敌方 = 输出（即使被格挡挡光）；status_applied 落在
        敌方 = 战术牌（控场/削弱），落在自身 = 增益；敌方无伤消失 = 移除。
        dmg 取事件流的 raw_damage（未扣盾）：面板净位移在被格挡全吸收时归零，
        会让下游「收割档」条件 dmg>0 整行短路（只剩 X=1 一档 → AI 永不出手）。
        """
        p, enemies = self._split_diff(diff)
        enemy_names = {e.get("name") for e in enemies}
        player_name = self.player.name if self.player else ""
        enemy_hp_loss = sum(max(0, e.get("hp_before", 0) - e.get("hp_after", 0))
                            for e in enemies)
        # ③修复（2026-09-10 用户裁定）：只有「动作前还活着、动作后死亡/消失」
        # 的敌人才算 remove。旧口径 any(dead) 把场上的尸体也算进去——多怪局首杀
        # 之后，自指变体靠尸体白得 remove +6.0 向性分压过真输出（实测杀伐X=1
        # 打自己）。alive_before 缺省回退 hp_before>0（兼容手工构造的 diff）。
        enemy_gone = any(e.get("dead") for e in enemies
                         if e.get("alive_before", (e.get("hp_before") or 0) > 0))
        shield = max(0, p.get("shield_after", 0) - p.get("shield_before", 0))
        heal = max(0, p.get("hp_after", 0) - p.get("hp_before", 0))
        cost = max(0, p.get("mana_before", 0) - p.get("mana_after", 0))
        mana_gain = max(0, p.get("mana_after", 0) - p.get("mana_before", 0))
        bl_gain = max(0, p.get("bl_after", 0) - p.get("bl_before", 0))
        speed_gain = max(0, p.get("speed_after", 0) - p.get("speed_before", 0))

        hit_enemy = False        # 伤害类事件命中敌方（含被格挡吸收）
        enemy_raw_damage = 0     # 事件流原始伤害合计（未扣盾，见 dmg 口径注释）
        status_on_enemy = False  # 状态/控制落在敌方
        status_on_self = False   # 状态落在自身
        heal_intent = 0          # 治疗意图：heal_applied 事件声明的回复量（含过量/0 实效）
        for ev in diff.get("events", []):
            etype = ev.get("type", "")
            target = ev.get("target", "")
            if etype == "damage_applied" and target in enemy_names:
                hit_enemy = True
                enemy_raw_damage += max(0, (ev.get("data") or {}).get("raw_damage") or 0)
            elif etype == "heal_applied" and target == player_name:
                heal_intent += max(0, (ev.get("data") or {}).get("heal_amount") or 0)
            elif etype == "status_applied":
                if target in enemy_names:
                    status_on_enemy = True
                elif target == player_name:
                    status_on_self = True

        if enemy_gone and enemy_hp_loss <= 0 and not hit_enemy:
            kind = "remove"
        elif enemy_hp_loss > 0 or hit_enemy:
            kind = "damage"
        elif status_on_enemy and not status_on_self:
            kind = "tactician"
        elif shield > 0:
            kind = "shield"
        elif heal > 0:
            kind = "heal"
        elif mana_gain > 0:
            kind = "ramp"
        elif bl_gain > 0 or speed_gain > 0 or status_on_self:
            kind = "buff"
        elif max(0, p.get("hp_before", 0) - p.get("hp_after", 0)) > 0:
            kind = "harm"      # 自伤不是战术牌（③修复连带：无活敌时 杀伐 自指
            #                    变体曾被兜底成 tactician → try_buff 拿它打自己）
        else:
            kind = "tactician"   # 其余无面板位移的战术牌
        # dmg 口径：事件流 raw_damage（未扣盾），与上面的 kind 判定同源同口径。
        # 取 max 而非直接替换：撤退/断尾求生/爆裂压制等路径不发 damage_applied
        # 事件（面板位移亦为 0），保持这些情形 dmg=0 的旧行为不变。
        # heal 同理带意图口径：满血时 hp 位移为 0，但引擎仍发 heal_applied
        # 事件（含 actual_heal=0/overheal>0）——意图才是这张牌的「真脸」。
        heal_final = max(heal, heal_intent)
        if heal_final > 0 and kind == "tactician":
            kind = "heal"
        # 敌方获益（喂敌人）：探脸并列时用于窄幅裁决——两个变体同分时，
        # 不喂敌人的那个才配代表这张牌的真脸（如被封疗的再生，打自己=空，
        # 打敌人=白送 3 回复；前者才应锁定）。
        foe_gain = sum(max(0, e.get("hp_after", 0) - e.get("hp_before", 0))
                       for e in enemies)
        return {"kind": kind, "cost_per_x": cost,
                "dmg": max(enemy_hp_loss, enemy_raw_damage),
                "shield": shield, "heal": heal_final, "mana_gain": mana_gain,
                "bl_gain": bl_gain,
                "status_on_enemy": status_on_enemy,
                "status_on_self": status_on_self,
                "foe_gain": foe_gain}

    # ---------- 候选生成与实时评分 ----------

    def _parry_candidate(self) -> Optional[dict]:
        """招架已改为受击逐刀选择，不再生成主动阶段候选。"""
        return None

    def _score_parry_candidate(self, candidate: dict) -> float:
        """按当前威胁给招架评分；高压/濒死时显著优先，安全时让出给输出。（2026-09-28 重算口径）"""
        player = self.player
        threat = self.incoming_damage()
        reduction = float(candidate.get("expected_reduction", 0))
        score = 1.15 * reduction
        effective_hp = player.current_hp + player.shield
        if threat >= effective_hp:
            score += 60.0
        elif threat >= max(1, player.current_hp * 0.7):
            score += 24.0
        elif player.current_hp <= player.blood_limit * 0.35:
            score += 16.0
        else:
            score -= 6.0
        # 招架本身不花法力/出手，但后续受击掉血会削弱减免，机会成本极低。
        score -= 0.05 * player.effective_attack_count() * player.effective_attack_power()
        return score

    def _daowen_candidates(self) -> list[dict]:
        """玩家实持道纹 → 聚焦候选（X 按预算/需求取 2~3 档）。"""
        out = []
        budget = self.mana_budget()
        for name, inst in sorted(self.player.dao_wen.items()):
            if (name == "封印" and self.player.entity_type == "轮回者"
                    and any(spell.name == "镇魔印" for spell in (self.player.spells or []))):
                # 已定义【镇魔印】后，【封印】由法术的自身回合结束流程结算，
                # 避免 AI 将同一道纹重复作为主动出手。
                continue
            if name in self.blocked_daowen_names:
                continue
            if inst is None or not inst.can_use():
                continue
            probe = self._probe(name)
            if probe is None:
                continue
            cost = probe["cost_per_x"]
            if cost > 0:
                budget_x = max(1, budget // cost)
                # 最后一手允许用尽全部法力；否则按预算档，防一发梭哈
                cap = budget_x if self.remaining_actions() > 1 else max(1, self.mana() // cost)
            else:                        # 代价型：不受法力限制，X 保守取小
                cap = 3
                budget_x = 1
            xs = {1, budget_x, cap}
            # DM 2026-09-10 提问「AI 为什么不会在怪物达到斩杀线一次性打空法力」的答案：
            # 不是幻觉，是均分预算把斩杀档在打分前就剪掉了——mana_budget = 法力 // 剩余
            # 出手，法力8/剩2手 → cap=4，而斩杀常需要更多，于是这一档**从未进入候选集**，
            # AI 根本没看见「打空法力收掉它」这个选项。开启本开关后，斩杀档只受**买得起**
            # 约束（x*cost <= 当前法力），不再受均分预算约束。
            harvest_cap = cap
            if os.environ.get("LJ_AI_HARVEST_DUMP") == "1" and cost > 0:
                harvest_cap = max(cap, max(1, self.mana() // cost))
            target = probe.get("target_name")
            kind = probe["kind"]
            kill_x = 0
            if kind == "damage":         # 输出牌给血最少敌人（收割/推进）
                foes = sorted(self.alive_enemies(), key=lambda e: e.current_hp)
                targets = [foes[0].name] if foes else []
                if foes and probe["dmg"] > 0:   # 收割档：恰好打死血最少者（真实预演，非线性外推）
                    _kx = self._x_to_kill(name, foes[0].current_hp, foes[0].name)
                    if _kx >= 1:
                        kill_x = _kx
                        xs.add(_kx)
                        # 收割档只受「买得起」约束，不受均分预算的 cap 裁剪：
                        # 超线性伤害（杀伐 X²）下击杀所需 X 常高于 budget_x，
                        # 被 cap 剪掉就等于 AI 根本看不见「这一发能打死它」。
                        if cost <= 0 or _kx * cost <= self.mana():
                            harvest_cap = max(harvest_cap, _kx)
            elif kind in ("shield", "heal", "buff", "ramp"):
                targets = [self.player.name]
            else:
                targets = ([t for t in (self._top_enemy_name(),) if t]
                           if probe.get("target_name") in (None, self._top_enemy_name())
                           else [probe.get("target_name")])
            xs = sorted((x for x in xs if 1 <= x <= max(harvest_cap, 1)), reverse=True)[:4]
            for t in targets or [None]:
                for x in xs:
                    out.append({"action": "use_daowen",
                                "label": f"{name}X={x}",
                                "kind": kind, "target": t,
                                # 斩杀档优先；若当前血量超过单次可击杀范围，也保留最大伤害档，
                                # 避免候选截断让AI第二手仍重复低伤普攻。
                                "priority_damage": bool(
                                    kind == "damage" and
                                    (x == max(xs) or (kill_x and x == kill_x))),
                                "params": self._params(name, x, t)})
        return out

    def _params(self, name: str, x: int, target: Optional[str]) -> dict:
        p: dict = {"daowen_name": name, "x": x, "dodge": False, "blood_shadow": False}
        if self._actor_ref:
            p["actor_ref"] = self._actor_ref   # 守擂者视角：所有道纹都指定施法者
        if target and name != "波及":
            refs = self.engine.combat._combat_entity_refs()
            matches = [(ref, entity) for ref, entity in refs.items()
                       if entity.name == target and entity.is_alive]
            if len(matches) == 1:
                p["target_ref"] = matches[0][0]
            elif matches:
                # 重名怪物很常见；名称接口会拒绝歧义目标。按当前目标选择规则
                # 锁定同名单位中生命最低者，并把稳定引用传给引擎。
                enemy_ids = {id(entity) for entity in self.alive_enemies()}
                hostile_matches = [pair for pair in matches if id(pair[1]) in enemy_ids]
                pool = hostile_matches or matches
                p["target_ref"] = min(pool, key=lambda pair: (
                    pair[1].current_hp, pair[0]))[0]
            else:
                p["target"] = target
        if name == "波及":
            refs = self.engine.combat._combat_entity_refs()
            # 视角重定向：波及目标是「本 AI 视角的敌人」；普通视角取非玩家侧，守擂视角取挑战者侧。
            enemy_ids = {id(e) for e in self.alive_enemies()}
            candidates = [
                {"target_ref": ref, "dodge": False, "blood_shadow": False}
                for ref, entity in refs.items()
                if entity.is_alive and id(entity) in enemy_ids
            ]
            if len(candidates) >= x:
                p["dodge_targets"] = candidates[:x]
                p["target_ref"] = candidates[0]["target_ref"]
        return p

    def _resonance_candidates(self) -> list[tuple[float, dict]]:
        """残韵候选：对**存在变化路径**的敌方道纹，按威胁动态打分（无固定表）。"""
        from engine.daowen import ResonanceEngine
        # 施法者残韵库存：挑战者经 State.resonance，守擂者经实体级 resonance（共用机制）。
        # 只读施法者**真实准备**的库存，绝不借他人——没有就是没有（不给没准备的塞残韵）。
        if self.player is self.engine.state.player:
            src_stock = self.engine.state.resonance
        else:
            src_stock = getattr(self.player, "resonance", None) or {}
        stock = {k: v for k, v in (src_stock or {}).items() if v > 0}
        if not stock:
            return []
        out = []
        best: dict = {}
        enemies = sorted(self.alive_enemies(),
                         key=lambda e: -e.effective_attack_count() * e.effective_attack_power())
        for enemy in enemies:
            threat_share = (enemy.effective_attack_count() * enemy.effective_attack_power()
                            / max(1, self.incoming_damage()))
            for dw in enemy.dao_wen:
                # 威胁构成可见信息：输出类文本权重高，其余次之
                text_kind = daowen_text_kind(enemy.dao_wen[dw])
                weight = {"damage": 1.0, "control": 0.7, "debuff": 0.6}.get(text_kind, 0.4)
                for path in ResonanceEngine.get_available_resonance(dw):
                    rtype = path.get("resonance_type")
                    if not rtype or stock.get(rtype, 0) <= 0:
                        continue
                    # 闭环上的空占位符（道纹被删后留的空位）不可获得，
                    # 别把它当成一个可收割的目标去提交残韵（2026-10-08）。
                    if path.get("reserved"):
                        continue
                    # 残韵是双向的：既削敌（敌人失去 dw），也补己
                    # （api._grant_transformed_daowen 把转化结果白送给施法者）。
                    # 2026-09-13 修正：此前只算"敌人少了什么"，漏掉"我多了什么"
                    # ——等于半个算式。残韵极稀缺（开局仅1个、之后只靠事件补），
                    # 且是副本专属道纹的唯一入口，收益项缺失导致实测 46 局里
                    # 只拿到过 1 个专属道纹。
                    score = 4.0 * (0.5 + threat_share) * weight
                    score += self._resonance_gain_bonus(path.get("target_daowen"))
                    params = {"source_daowen": dw, "resonance_type": rtype,
                              # 双向后同一（源道纹, 残韵）可能通向两个相邻节点，
                              # 必须显式提交走向，不让引擎替我们挑。
                              "target_daowen": path.get("target_daowen", ""),
                              "target": enemy.name,             # 兼容测试/旧解析：按名字找目标
                              "target_ref": self._target_ref_for(enemy)}  # use_resonance 需要稳定引用
                    if self._actor_ref:
                        params["actor_ref"] = self._actor_ref
                    # 残韵路径双向后，同一道纹可通向两个相邻节点；候选若全量铺开会
                    # 让每个敌人的每条道纹都翻一倍（AI 每个决策点只提交一条，多出来的
                    # 分支纯属浪费）。同一（敌人, 道纹）只保留**评分最高**的那条路径：
                    # 正反两向都参与打分，只是不再各自占一个候选位。
                    key = (enemy.name, dw)
                    prior = best.get(key)
                    if prior is None or score > prior[0]:
                        best[key] = (score, {
                            "action": "use_resonance",
                            "label": f"残韵·{rtype}→{dw}@{enemy.name}",
                            "params": params})
        out.extend(best.values())
        out.sort(key=lambda t: -t[0])
        return out[:3]

    def _resonance_gain_bonus(self, dest: Optional[str]) -> float:
        """转化所得道纹对**自己**的价值（残韵算式里此前缺失的那一半）。

        通用规则，不写死任何副本/道纹名：
        - 已持有 → 0（重复获得无增量）
        - 本副本专属道纹 → 高权重：残韵是其唯一入口，错过就没有第二次
        - 能产出资源（法力/速度）或直接造成伤害 → 中权重
        - 其余可用道纹 → 低权重
        """
        if not dest or self.player is None:
            return 0.0
        if dest in getattr(self.player, "dao_wen", {}) or {}:
            return 0.0
        from engine.daowen import DaoWenEngine
        from engine.gamedata import REGION_EXCLUSIVE_DAOWEN
        bonus = 1.0
        region = getattr(self.engine.state, "current_region", "") or ""
        if dest in REGION_EXCLUSIVE_DAOWEN.get(region, set()):
            bonus += 4.0      # 专属道纹：残韵是唯一获取渠道
        try:
            calc = DaoWenEngine.resolve(dest, 2, target=None, caster=None)
        except Exception:
            return bonus
        if calc.get("mana_gain") or calc.get("speed_boost"):
            bonus += 2.0      # 产资源（如透支多代价换法力、超频买速度）
        if calc.get("damage") or calc.get("target_damage"):
            bonus += 1.5      # 直伤
        return bonus

    def _score_candidate(self, diff: dict, label: str,
                         kind: Optional[str] = None,
                         target: Optional[str] = None) -> Optional[float]:
        """实时评分：局势效用 + 性格调制。返回 None 表示必须拒绝（LETHAL）。"""
        # 先把 diff 翻到行动者视角：引擎 diff 永远以挑战者为 player 键。
        # 不翻转则守擂者会拿挑战者的法力/速度/异变算自己的风险，甚至把
        # 「自己打出的击杀」当 player_dead=LETHAL 而拒绝出手。
        own, foes = self._split_diff(diff)
        view = dict(diff)
        view["player"] = own
        view["enemies"] = foes
        if own and own is not diff.get("player"):
            view["player_dead"] = bool(own.get("dead"))   # 仅守擂视角改写；挑战者保留原口径
        if ActionPreview.would_kill_player(view):
            self.preview_rejected.append(f"{label}（预演致轮回者命零）")
            return None
        risk, reasons = ActionPreview.risk_classify(view, self.player)
        self._last_risk = (risk, reasons)

        p = own
        enemies = foes
        hp = p.get("hp_before", 0), p.get("hp_after", 0)
        own_hp_loss = max(0, hp[0] - hp[1])
        heal = max(0, hp[1] - hp[0])
        bl_loss = max(0, p.get("bl_before", 0) - p.get("bl_after", 0))
        speed_loss = max(0, p.get("speed_before", 0) - p.get("speed_after", 0))
        mana_spent = max(0, p.get("mana_before", 0) - p.get("mana_after", 0))
        shield_gain = max(0, p.get("shield_after", 0) - p.get("shield_before", 0))
        mutation = max(0, p.get("mutation_delta", 0))
        shards_spent = max(0, diff.get("shards_before", 0) - diff.get("shards_after", 0))

        enemy_hp_loss = sum(max(0, e.get("hp_before", 0) - e.get("hp_after", 0))
                            for e in enemies)
        enemy_heal = sum(max(0, e.get("hp_after", 0) - e.get("hp_before", 0))
                         for e in enemies)
        kills = sum(1 for e in enemies if e.get("dead"))
        all_gone = bool(enemies) and all(
            e.get("dead") or e.get("hp_after", 1) == 0 or e.get("departed", False)
            for e in enemies)

        # ---- 局势效用（状态驱动，无任何道纹名特判） ----
        score = 0.0
        if all_gone:
            score += 100.0                        # 直接终结战斗
        score += 1.4 * enemy_hp_loss              # 推进敌方血量（与防御对称,倾斜交给局势与性格）
        score += 8.0 * kills                      # 击杀/移除
        # 战术牌价值：对敌方施加压制（状态事件可见，或引擎已受理的敌方指向
        # 战术牌——部分潜在规则型道纹零事件足迹，但引擎 result.calculation 证实生效）。
        # 价值来自可见信息：目标威胁占比（压制谁）、目标血量比（肥怪先上战术
        # 更划算）、本场是否首次使用（每张至少探索一次，防只打伤害的单调收敛）。
        enemy_by_name = {en.name: en for en in self.alive_enemies()}
        pressed: set = set()
        for ev in diff.get("events", []):
            if ev.get("type") == "status_applied" and ev.get("target", "") in enemy_by_name:
                pressed.add(ev.get("target", ""))
        if kind == "tactician" and target in enemy_by_name:
            pressed.add(target)
        for tname in pressed:
            ent = enemy_by_name[tname]
            share = (ent.effective_attack_count() * ent.effective_attack_power()
                     / max(1, self.incoming_damage()))
            ratio = ent.current_hp / max(1, ent.blood_limit)
            score += 2.0 + 4.0 * min(1.0, ratio) + 2.0 * min(1.0, share)
            # DM裁定 2026-09-10：压制期权按 effective 攻次×攻力（面板对轮回者只是 0 占位）。
            # 期权价值按「本手后目标存活的血量比」折价（2026-09-10 用户质询实锤）：
            # 压制一个本手就会被杀掉的目标毫无价值——不折价时，0 伤害 debuff 会凭
            # +112~128 的虚额压过预演已验证的必杀（119.24 vs 107.6，seed=2 实测）。
            after = next((e.get("hp_after", ent.current_hp) for e in enemies
                          if e.get("name") == tname), ent.current_hp)
            survive = max(0.0, after) / max(1, ent.blood_limit)
            score += 0.8 * ent.effective_attack_count() * ent.effective_attack_power() * survive
            if label.split("X=")[0].split("→")[0] not in self.used:
                score += 4.0             # 首次探索加成（每张一次）
        self_status = any(
            ev.get("type") == "status_applied"
            and ev.get("target", "") == (self.player.name if self.player else "")
            for ev in diff.get("events", []))
        if self_status or kind == "buff":
            score += 1.5                 # 自身增益的期权价值（小额，防无脑挂buff）
        score -= 1.2 * enemy_heal                 # 喂养敌方（癌变风险由 CRITICAL 兜底）
        threat = self.incoming_damage()
        shield_useful = min(shield_gain, max(0, threat - self.player.shield))
        score += 1.1 * shield_useful + 0.15 * max(0, shield_gain - shield_useful)
        missing = max(0, self.player.blood_limit - self.player.current_hp)
        score += 1.4 * min(heal, missing)
        # 凡庸压力：连续未使敌方掉血越久，输出候选越紧迫
        if enemy_hp_loss > 0 and self._rounds_since_damage >= 2:
            score += 1.2 * self._rounds_since_damage
        # ③修复（2026-09-10 用户裁定批次）：法力=攻力（一池制换算）。每花 1 法力，
        # 本场剩余每次普攻都少 1 攻力——按「攻次×0.5」的等价火力折价。旧固定
        # -0.12/点下 盾+1.1/点 ≫ 法力-0.12/点，AI 愿意满法力一手全换盾 → 空手
        # → 回终化雕塑（换席 seed 9/11/12 实锤）。速度 12 时折价 -6.12/点，
        # 满池梭哈盾净分为负；正常输出牌（杀伐 5X 伤）仍远正向。
        ap = self.player.effective_attack_count()
        score -= mana_spent * (0.12 + 0.5 * ap)
        score -= 0.4 * shards_spent
        score -= 0.35 * mutation
        score -= 0.8 * speed_loss

        # ---- 性格调制（倾向而非规则；权重=score×confidence，无性格=0） ----
        w_risk = self._w("risk_preference")          # +冒险 / -求稳
        w_res = self._w("resource_view")             # +节约 / -挥霍
        w_explore = self._w("exploration_desire")    # +求新
        w_habit = self._w("decision_habit")          # +先观察后行动 / -冲动
        w_stable = self._w("emotional_stability")    # +沉稳 / -易波动
        w_trust = self._w("interpersonal_tendency")  # +信任（护友）
        w_moral = self._w("moral_baseline")          # +守义（不伤友）
        w_expr = self._w("expression_style")         # +直言（进攻） / -内敛（防守）
        w_react = self._w("reaction_pattern")        # +从容

        tol = 1.0 - 0.85 * w_risk                   # 风险容忍 → 自伤/血限惩罚系数
        tol = min(2.0, max(0.3, tol))
        score -= (2.2 * tol) * own_hp_loss
        score -= (3.0 * tol) * bl_loss
        if w_risk < 0:                              # 求稳：受威胁时防回手段显著加值
            score += 0.8 * abs(w_risk) * (shield_useful + min(heal, missing))
        score -= 0.12 * (1.0 + 1.5 * max(0.0, w_res)) * mana_spent   # 节约：更疼法力
        score -= 0.4 * (1.0 + 1.5 * max(0.0, w_res)) * shards_spent
        if w_explore > 0:                           # 求新：没打过的牌加分
            base = label.split("X=")[0]
            if base not in self.used:
                score += 0.5 * w_explore
        if w_habit > 0 and self._first_contact():   # 先观察后行动：开局忌梭哈
            if mana_spent > 3:
                score -= 2.0 * w_habit * mana_spent / 6.0
        if w_habit < 0 and enemy_hp_loss > 0:       # 冲动：立打立伤偏好
            score += 0.3 * abs(w_habit)
        hp_ratio = self.player.current_hp / max(1, self.player.blood_limit)
        if hp_ratio < 0.35:                         # 低血：易波动者更重自伤
            score -= 0.9 * max(0.0, -w_stable) * own_hp_loss
        if threat > 0 and w_react > 0 and enemy_hp_loss == 0 and own_hp_loss == 0:
            score += 0.1 * w_react                  # 从容：高压下偏好稳妥手段
        if w_expr > 0 and enemy_hp_loss > 0:
            score += 0.15 * w_expr                  # 直言：进攻倾向
        if w_expr < 0 and enemy_hp_loss == 0:
            score += 0.15 * abs(w_expr)             # 内敛：防守/布局倾向
        for ally in diff.get("allies", []):         # 友军后果
            a_loss = max(0, (ally.get("hp_before") or 0) - (ally.get("hp_after") or 0))
            a_heal = max(0, (ally.get("hp_after") or 0) - (ally.get("hp_before") or 0))
            score -= (0.8 + 0.8 * w_moral) * a_loss
            score += 0.5 * w_trust * a_heal

        # ---- 台词影响（硬伤3；红线 E 已于 2026-08-30 由 DM 解除）----
        # 语言没有实质数值作用，但"让敌人忌惮"就是最大的作用。信不信由我的性格定。
        if self._opponent_read:
            score += self._dialogue_bias(
                enemy_hp_loss=enemy_hp_loss, shield_useful=shield_useful,
                heal=min(heal, missing), mana_spent=mana_spent)

        # ---- 实验：自保时钟（默认关闭；只读引擎阈值，不改任何规则数值） ----
        if SELF_PRESERVE:
            bias, fatal = self._self_preserve_bias(diff, enemy_hp_loss=enemy_hp_loss)
            if fatal:
                self.preview_rejected.append(f"{label}（自保否决：{fatal}）")
                return None
            score += bias

        # ---- 风险等级惩罚（引擎口径分级） ----
        if risk == "LETHAL":
            return None
        score -= {"CRITICAL": 30.0, "HIGH": 6.0, "MEDIUM": 1.2}.get(risk, 0.0)
        return score

    # ---------- 实验：自保时钟（DM裁定 2026-08-31 前先做前后对照） ----------
    SELF_PRESERVE_MARGIN = 0.8   # 距线 20% 起线性扣分（越贴线越疼）

    def _self_preserve_bias(self, diff: dict, *, enemy_hp_loss: float):
        """把引擎自己的三条「自爆时钟」接进候选打分，返回 (加分调整, 致命原因)。

        只读引擎阈值与实体字段（`cancer_threshold_of` / `MUTATION_COLLAPSE_THRESHOLD` /
        连续未使敌掉血回合数），**不按道纹名特判、不改任何规则数值**：
          ① 癌变：本手后 total_healed ≥ combat.cancer_threshold_of(自己)；
          ② 迷失：本手后 mutation_count ≥ Entity.MUTATION_COLLAPSE_THRESHOLD；
          ③ 凡庸：连续五回合未能使敌对角色掉血 → 越接近线，「能推进伤害」越值钱。
        致命原因非空 = 这一手就是自己把自己送走，与 LETHAL 同档一票否决。
        """
        me = self.player
        p = diff.get("player", {})
        adj = 0.0
        if me is None:
            return adj, None

        # ① 癌变线：累计恢复量达血限×2 即命零（过量回复按原值计入）
        combat = getattr(self.engine, "combat", None)
        if combat is not None:
            line = combat.cancer_threshold_of(me)
            after = getattr(me, "total_healed", 0) + max(0.0, p.get("healed_delta", 0) or 0)
            if line > 0:
                if after >= line:
                    return adj, f"本手后累计回复{after:.0f}≥癌变线{line}"
                soft = line * self.SELF_PRESERVE_MARGIN
                if after >= soft:
                    adj -= 25.0 * (after - soft) / max(1.0, line - soft)

        # ② 迷失阈值：仅适用于非豁免角色；怪物无迷失时钟
        collapse = getattr(type(me), "MUTATION_COLLAPSE_THRESHOLD", 0) or 0
        mut_after = p.get("mutation_after")
        if mut_after is None:
            mut_after = getattr(me, "mutation_count", 0) + max(0.0, p.get("mutation_delta", 0) or 0)
        if collapse > 0 and getattr(me, "can_trigger_mutation_lost", lambda: True)():
            if mut_after >= collapse:
                return adj, f"本手后异变{mut_after:.0f}≥迷失阈值{collapse}"
            soft = collapse * self.SELF_PRESERVE_MARGIN
            if mut_after >= soft:
                adj -= 25.0 * (mut_after - soft) / max(1.0, collapse - soft)

        # ③ 凡庸线：连续五回合未使敌掉血就命零 → 空转越久，输出越紧迫
        idle = self._rounds_since_damage
        if idle >= 3:
            if enemy_hp_loss > 0:
                adj += 10.0 * idle           # 能让对手掉血的手段显著加值
            else:
                adj -= 10.0 * (idle - 2)     # 继续空转 = 自己走向凡庸
        return adj, None

    def _first_contact(self) -> bool:
        """开局首轮/刚遇新敌（先观察型性格用；可见信息：本场出手与回合数）。

        注：mock state 可能缺 current_round 字段（test_win_only_ai::_VetoProbe 等），
        用 getattr 兜底=0，保证早期 mock 测试不崩。
        """
        cr = getattr(self.engine.state, "current_round", 0) or 0
        return (not self.used and cr <= 1)

    def _dynamic_action(self) -> Optional[dict]:
        """实时决策主路径：生成候选 → 预演评分 → 执行最高分。"""
        self._refresh_personality()
        self._refresh_opponent_read()
        # 只在**读到新的一句**时记录：对手不说话时每手都重读同一句会把日志刷爆
        # （信念本身不变，重复记录没有信息量）。
        r = self._opponent_read
        stamp = (r["speaker"], r["posture"], round(r["belief"], 4),
                 r["repeats"]) if r else None
        if self.verbose and r and stamp != self._last_logged_read:
            self._last_logged_read = stamp
            verdict = "信" if r["belief"] >= 0 else "疑"
            effect = ("趁势压上" if r["claim"] == "weak" and r["belief"] > 0
                      else "防被收割" if r["claim"] == "weak"
                      else "忌惮加防" if r["belief"] > 0 else "当它虚张")
            self.log.append(f"[读到] {r['speaker']}「{r['posture']}」"
                            f"→ 我{verdict}{abs(r['belief']):.2f}（{effect}）")
        scored: list[tuple[float, str, dict]] = []
        resonance_rows: list[int] = []   # scored 中残韵候选的下标（含收益项加成）
        _resonance_bonus_of: dict = {}   # 下标 → 已计入的收益项加成
        ends_battle = False              # 是否存在「本手即终结战斗」的候选
        candidates = []
        parry = self._parry_candidate()
        if parry is not None:
            # 防御候选放在前面，避免大量道纹候选触发性能上限时把招架裁掉。
            candidates.append(parry)
        candidates.extend(self._daowen_candidates())
        basic_candidates = self._basic_attack_candidates()
        candidates.extend(basic_candidates)
        for bonus, cand in self._resonance_candidates():
            candidates.append(cand)   # 残韵候选已按威胁预筛，附带基础分
        # 普攻与真实斩杀档都必须穿过预演预算闸门：否则大量按名称排序的
        # 道纹候选会把后面的斩杀候选截掉，AI明明有一手可终结战斗却重复普攻。
        if len(candidates) > MAX_CANDIDATE_PREVIEWS:
            basic_ids = {id(cand) for cand in basic_candidates}
            priority_damage_candidates = [
                cand for cand in candidates
                if cand.get("priority_damage") and id(cand) not in basic_ids
            ]
            reserve_basic = min(len(basic_candidates), MAX_CANDIDATE_PREVIEWS)
            reserve_priority_damage = min(
                len(priority_damage_candidates), MAX_CANDIDATE_PREVIEWS - reserve_basic)
            reserved_ids = basic_ids | {id(cand) for cand in priority_damage_candidates}
            regular_candidates = [
                cand for cand in candidates if id(cand) not in reserved_ids
            ]
            regular_slots = MAX_CANDIDATE_PREVIEWS - reserve_basic - reserve_priority_damage
            candidates = (
                regular_candidates[:regular_slots]
                + priority_damage_candidates[:reserve_priority_damage]
                + basic_candidates[:reserve_basic]
            )
        immediate_damage_available = False
        immediate_damage_candidates: set[int] = set()
        for cand in candidates:
            # 普攻是两段动作，预演必须整串跑完才看得见伤害
            pv = (self.previewer.preview_sequence(cand["steps"]) if cand.get("steps")
                  else self.previewer.preview(cand["action"], cand["params"]))
            res = pv.get("result") or {}
            if not res.get("success"):
                continue               # 引擎拒绝=非法候选，跳过
            if cand["action"] == "declare_parry":
                # 招架的姿态变化不在通用数值 diff 中；其收益由每击减免
                # 与当前威胁现场计算，声明本身仍先经过 ActionPreview 校验。
                s = self._score_parry_candidate(cand)
            else:
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
            if cand["action"] == "use_resonance":
                # 残韵收益项（"我能拿到什么"）只是**期权**：若本手已有候选能直接
                # 终结战斗，期权不该把它挤下去。否则 2026-09-13 杀伐改 X² 后
                # 低 X 输出变弱、分差收窄，收益项足以让 AI 放着必杀不打去转化
                # 敌方道纹（实测 残韵 108.0+bonus 压过 普攻 110.4 的一击致命）。
                s += bonus
                resonance_rows.append(len(scored))
                _resonance_bonus_of[len(scored)] = bonus
            # 首试配额：安全窗口（当前威胁低于自身生命）下，每张非输出牌每场
            # 至少真实试打一次——预演看不到潜在规则型道纹的延迟价值，
            # 用一次真实发动校准后续估值（先射箭后画靶；也保证持有即会发动）。
            base = cand["label"].split("X=")[0].split("→")[0]
            if (cand.get("kind") in ("tactician", "buff", "ramp")
                    and base not in self.used
                    and self.incoming_damage() < self.player.current_hp):
                s += 40.0
            _enemies = (pv.get("diff", {}) or {}).get("enemies", []) or []
            if _enemies and all(en.get("dead") or en.get("hp_after", 1) == 0
                                or en.get("departed", False) for en in _enemies):
                ends_battle = True
            scored.append((s, cand["label"], cand))
        if ends_battle:
            # 撤回残韵收益项：有必杀在手时，"这一手能顺便学到什么"不算数。
            for i in resonance_rows:
                sc, lbl, cd = scored[i]
                scored[i] = (sc - _resonance_bonus_of[i], lbl, cd)
        # 首次接触时，只要安全的普攻/伤害动作存在，就先让敌方掉血。
        # 控制、延迟和增益不能替代第一次造成伤害，否则【封印】会被用成
        # “连续空转五回合后触发【凡庸】”。第一次伤害完成后，下一回合仍可
        # 根据局势选择【封印】跳过怪物阶段。
        force_progress = (not self._first_contact()
                          and getattr(self, "_last_action_caused_damage", True) is False)
        if immediate_damage_available and (self._first_contact() or force_progress):
            progress = [row for row in scored
                        if id(row[2]) in immediate_damage_candidates]
            if progress:
                scored = progress
        if not scored:
            return None
        scored.sort(key=lambda t: (-t[0], t[1]))
        best = scored[0][2]
        self.last_decision = {
            "action": best["action"],
            "params": dict(best.get("params", {})),
            "label": best["label"],
        }
        if best.get("steps"):
            self.last_decision["steps"] = best["steps"]
        hp_before = sum(e.current_hp for e in self.alive_enemies())
        self._last_action_caused_damage = id(best) in immediate_damage_candidates
        r = (self._run_steps(best["steps"]) if best.get("steps")
             else self.engine.execute_action(best["action"], best["params"]))
        if r.get("success"):
            label = best["label"]
            base = label.split("X=")[0].split("→")[0]
            self.used[base] = self.used.get(base, 0) + 1
            # 记账：本回合是否推进了敌方血量（凡庸压力用）
            hp_after = sum(e.current_hp for e in self.alive_enemies())
            if hp_after < hp_before:
                self._damage_done_battle = True
            if self.verbose:
                self.log.append(f"[实时决策] {label}（得分 {scored[0][0]:.2f}）")
            return r
        if self.verbose:
            self.log.append(f"[跳过] {best['label']}: {r.get('error')}")
        return None

    # ---------- 普攻（DM裁定 2026-09-09，实验旗标 LJ_AI_BASIC_ATTACK=1） ----------

    @staticmethod
    def _target_worth_dodging(target, per_hit: int, hp_left: int) -> bool:
        """目标侧的闪避判据——**轮回者与怪物共用同一口径**（2026-09-17 用户令）。

        闪避要花 1 点当前速度，而[攻击次数]=当前速度，等于用一次出手换一次免伤，
        所以只在两种情形值得闪：
          1. 致命击：这一击会把自己打死（per_hit >= 剩余生命）；
          2. 闪比反打划算：单次伤害高于自己的攻击力——站着挨这一下，
             比省下速度去反打更亏。

        判据放在这里而不是散在各个调用点，是为了让"给怪物闪避权"只改一处闸门，
        不必复制一份可能漂移的判据。
        """
        return per_hit >= hp_left or per_hit > target.effective_attack_power()

    @staticmethod
    def _decline_spell_choices(option: dict) -> dict:
        """逐击法术反应全部不使用——接口要求按目标 spell_options 完整提交。"""
        spell_options = option.get("spell_options", {}) or {}
        return {timing: {spell["spell_name"]: {"use": False}
                         for spell in spell_options.get(timing, [])}
                for timing in ("before", "after")}

    def _attack_steps(self, target_name: str) -> list:
        """普攻两段动作；resolve 的 token/hits 由 prepare 的返回串联。"""
        actor_ref = self._actor_ref or "player:0"

        def _resolve(prev: dict) -> dict:
            res = prev.get("result") or {}
            opts = res.get("target_options") or []
            option = next((o for o in opts if o.get("name") == target_name), None)
            if option is None:
                return {"token": res.get("token"), "hits": []}
            # 闪避中继（2026-09-10，用户指出"后手方站着让人打"）：2026-08-26 的 PvP
            # 闪避中继修在旧驱动 _resolve_opponent_one（use_tactical=False 分支）里，
            # 双方改由 TacticalAI 驱动后 _resolve 写死 dodge=False，中继成孤儿——
            # 死斗双方从未行使 规则正文《基础定义》的闪避权。这里按**目标侧**口径逐击补齐：
            #   · 只在最终死斗且目标是轮回者时启用（PvE 怪物闪避口径不变）；
            #   · 必中覆盖的击不提交闪避（提交会被引擎整包拒绝）；
            #   · 预算=目标当前速度（每闪1次-1，逐击递减；闪避=自己攻次-1，
            #     因此只有"致命击"或"伤害>自己攻力（闪比反打划算）"才值得闪）；
            #   · LJ_AI_DUEL_DODGE=0 关闸复现旧行为（全程 dodge=False）。
            dodge_on = (os.environ.get("LJ_AI_DUEL_DODGE", "1") != "0"
                        and self.engine.state.in_final_duel)
            # 2026-09-17 用户令：怪物同样享有闪避权（此前只有最终死斗里的轮回者会闪）。
            # 判据与轮回者完全同口径——见 _target_worth_dodging；怪物"不一定用"，
            # 只在致命击或"闪比反打划算"时才花那 1 点速度。
            # LJ_AI_MONSTER_DODGE=0 关闸复现旧行为（怪物全程不闪）。
            monster_dodge_on = os.environ.get("LJ_AI_MONSTER_DODGE", "1") != "0"
            attacker = self.engine.combat._combat_entity_refs().get(actor_ref)
            target = self.engine.combat._combat_entity_refs().get(option["ref"])
            dodgeable = bool(target is not None and attacker is not None
                             and option.get("can_dodge")
                             and ((dodge_on and target.entity_type == "轮回者")
                                  or (monster_dodge_on and target.entity_type == "怪物")))
            if dodgeable:
                from engine.combat import CombatEngine as _CE
                bizhong = _CE.bizhong_remaining(self.engine.combat, attacker)
                per_hit = attacker.effective_attack_power()
                # 攻击力与剩余生命由 _target_worth_dodging 逐个击实时读取
                hp_left, budget = target.current_hp, target.current_speed
            hits = []
            for i in range(res.get("hit_count", 0)):
                dodge = False
                if dodgeable and budget > 0 and bizhong <= i:
                    if self._target_worth_dodging(target, per_hit, hp_left):
                        dodge = True
                        budget -= 1
                if dodgeable and not dodge:
                    hp_left = max(0, hp_left - per_hit)
                hits.append({"target_ref": option["ref"], "dodge": dodge,
                             "blood_shadow": False,
                             "spell_choices": self._decline_spell_choices(option)})
            return {"token": res.get("token"), "hits": hits}

        return [("prepare_attack", {"actor_ref": actor_ref}), ("resolve_attack", _resolve)]

    def _basic_attack_candidates(self) -> list:
        """普攻候选。

        DM裁定 2026-09-09：法力改一池制（[战始]给满、[回始]不回填）后，只靠法力型
        道纹会在池子花干后无事可做——实测同批种子通关率 3%→0%、平均经历场数
        1.56→0.87。普攻因此必须是常驻候选，与道纹一起参与打分。
        DM裁定 2026-09-10：普攻候选**默认开启**；如需复现旧行为（无普攻候选），
        设 LJ_AI_BASIC_ATTACK=0 显式关闸。
        """
        if os.environ.get("LJ_AI_BASIC_ATTACK", "1") == "0":
            return []
        me = self.player
        if me is None or not me.is_alive or me.effective_attack_count() <= 0:
            return []
        return [{"action": "prepare_attack", "params": {},
                 "steps": self._attack_steps(foe.name),
                 "label": f"普攻→{foe.name}", "kind": "attack", "target": foe.name}
                for foe in self.alive_enemies()]

    def _run_steps(self, steps: list) -> dict:
        """按序执行一串动作（params 可为 callable，收前一步返回）；失败即停。"""
        prev: dict = {}
        for action_type, params in steps:
            p = params(prev) if callable(params) else params
            prev = self.engine.execute_action(action_type, p)
            if not prev.get("success"):
                return prev
        return prev

    # ---------- 统一执行入口（安全过滤保留） ----------

    def _cast(self, name: str, x: int, target: Optional[str] = None,
              *, allow_sacrifice: bool = False) -> Optional[dict]:
        """候选动作 → 预演 → LETHAL 降X搜索 → 正式执行（兼容壳与测试共用）。"""
        if x < 1:
            return None
        p = self._params(name, x, target)
        if not allow_sacrifice:
            probe_x = x
            reason = None
            self._last_risk = ("SAFE", [])
            while probe_x >= 1:
                pv = self.previewer.preview("use_daowen", dict(p, x=probe_x))
                diff = pv.get("diff", {})
                res = pv.get("result") or {}
                if not res.get("success"):
                    # 大X可能因法力/代价不足非法：降档重试，全部非法才放弃
                    probe_x -= 1
                    continue
                self._last_risk = ActionPreview.risk_classify(diff, self.player)
                if not ActionPreview.would_kill_player(diff):
                    break
                reason = "预演致轮回者命零"
                probe_x -= 1
            if probe_x < 1:
                self.preview_rejected.append(f"{name}X={x}->{target or 'all'}（{reason}）")
                if self.verbose:
                    self.log.append(f"[安全过滤] 拒绝 {name}X={x}: {reason}")
                return None
            if probe_x != x:
                self.preview_rejected.append(
                    f"{name}X={x}->{target or 'all'}（预演致轮回者命零，降档到X={probe_x}）")
                if self.verbose:
                    self.log.append(f"[安全过滤] {name}X={x}→{probe_x}（降低到安全档）")
            x = probe_x
            p["x"] = x
        r = self.engine.execute_action("use_daowen", p)
        if r.get("success"):
            self.used[name] = self.used.get(name, 0) + 1
            return r
        if self.verbose:
            self.log.append(f"[跳过] {name}X={x}: {r.get('error')}")
        return None

    # ---------- 兼容壳：类别策略（供 tests 与 archive 实验子类） ----------

    def _sized_x(self, name: str, need_units: int, unit: str = "dmg") -> int:
        """按需求量与法力推导 X（每X产量来自 X=1 预演，缓存本场）。"""
        probe = self._probe(name)
        if probe is None:
            return 0
        per = probe.get(unit) or 1
        cost = probe["cost_per_x"]
        if unit == "dmg" and per > 0:
            kx = self._x_to_kill(name, need_units, probe.get("target_name"))
            if kx >= 1:
                return min(kx, 9) if cost <= 0 else min(
                    kx, max(1, self.mana() // cost), 9)
        x = max(1, math.ceil(need_units / max(1, per)))
        if cost > 0:
            x = min(x, max(1, self.mana() // cost))
        return min(x, 9)

    def try_survive(self) -> Optional[dict]:
        """保命：致死威胁 → 格挡；血线过低 → 回复（类别由预演归纳）。"""
        p = self.player
        threat = self.incoming_damage()
        if threat > 0 and threat >= p.current_hp * 0.5:
            names = list(dict.fromkeys(
                self.owned("shield") + [n for n, inst in sorted(self.player.dao_wen.items())
                                        if inst is not None and inst.can_use()
                                        and (self._probe(n) or {}).get("kind") == "shield"]))
            for name in names:
                need = max(1, threat - p.shield)
                r = self._cast(name, self._sized_x(name, need, "shield"), p.name)
                if r:
                    return r
        if p.current_hp <= p.blood_limit * 0.35:
            names = list(dict.fromkeys(
                self.owned("heal") + [n for n, inst in sorted(self.player.dao_wen.items())
                                      if inst is not None and inst.can_use()
                                      and (self._probe(n) or {}).get("kind") == "heal"]))
            for name in names:
                need = p.blood_limit - p.current_hp
                r = self._cast(name, self._sized_x(name, need, "heal"), p.name)
                if r:
                    return r
        return None

    def try_buff(self) -> Optional[dict]:
        """增益：每张每场至多一次（引擎/自身状态自然拒绝重复）。

        无活敌不出手（tests/test_ai_buff_fixes 钉死的口径：不浪费出手）。
        ③修复连带说明：旧版该测试靠尸体误判（杀伐被归纳成 remove）与
        龙鳞被归纳成 shield 的双重巧合才通过——修复后按原设计意图显式加门。
        """
        battle = self.engine.state.current_battle
        if not self.alive_enemies():
            return None
        for name, inst in sorted(self.player.dao_wen.items()):
            if inst is None or not inst.can_use():
                continue
            if self.used.get(f"buff:{name}:{battle}"):
                continue
            probe = self._probe(name)
            if probe is None or probe["kind"] not in ("buff", "tactician"):
                continue
            target = probe.get("target_name")
            r = self._cast(name, 2 if probe["cost_per_x"] > 0 else 1, target)
            if r:
                self.used[f"buff:{name}:{battle}"] = 1
                return r
        return None

    def _allies(self) -> list:
        out = [e for e in self.engine.state.friends
               if e.is_alive and not e.has_retreated]
        out += [e for e in self.engine.state.employees
                if e.is_alive and not e.has_retreated and e.is_deployed]
        return out

    def _best_attack_ally(self):
        allies = self._allies()
        return max(allies, key=lambda e: e.effective_attack_power() or 0) if allies else None

    def _tankiest_ally(self):
        allies = self._allies()
        return max(allies, key=lambda e: e.current_hp) if allies else None

    def _weakest_ally(self):
        allies = self._allies()
        return min(allies, key=lambda e: e.current_hp) if allies else None

    def resolve_pending_redemption(self, option: str = "终结") -> Optional[dict]:
        """救赎是强制待选：未结算前任何其它行动都会被引擎拒绝。"""
        pending = self.engine.state.pending_redemption
        if not pending:
            return None
        params: dict = {"option": option}
        if option in (1, "1", "接纳"):
            base = f"微光·{pending.get('name', '友')}"
            existing = {self.player.name} if self.player else set()
            for group in (self.engine.state.friends, self.engine.state.employees,
                          self.engine.state.temp_friends):
                existing.update(entity.name for entity in group)
            existing.update(entity.name for entity in self.alive_enemies())
            name = base
            suffix = 2
            while name in existing:
                name = f"{base}{suffix}"
                suffix += 1
            params["name"] = name
        r = self.engine.execute_action("resolve_redemption", params)
        if r.get("success"):
            self.used["救赎"] = self.used.get("救赎", 0) + 1
            return r
        if self.verbose:
            self.log.append(f"[跳过] 救赎: {r.get('error')}")
        return None

    def _best_resonance(self, min_score: float) -> Optional[dict]:
        for bonus, cand in self._resonance_candidates():
            if bonus < min_score:
                break
            r = self.engine.execute_action(cand["action"], cand["params"])
            if r.get("success"):
                rtype = cand["params"]["resonance_type"]
                self.used[f"残韵·{rtype}"] = self.used.get(f"残韵·{rtype}", 0) + 1
                self.resolve_pending_redemption()
                return r
        return None

    def try_resonance(self) -> Optional[dict]:
        """残韵：对存在变化路径的敌方道纹按威胁动态发动（无固定目标表）。"""
        return self._best_resonance(min_score=2.0)

    def try_finish(self) -> Optional[dict]:
        """收割：能一击打死就打死（每点X伤害来自预演归纳，非查表）。"""
        for e in sorted(self.alive_enemies(), key=lambda x: x.current_hp):
            for name in sorted(self.player.dao_wen):
                inst = self.player.dao_wen[name]
                if inst is None or not inst.can_use():
                    continue
                probe = self._probe(name)
                if probe is None or probe["kind"] != "damage" or probe["dmg"] <= 0:
                    continue
                need_x = self._x_to_kill(name, e.current_hp, e.name)
                cost = probe["cost_per_x"]
                if need_x < 1:
                    continue
                if cost > 0 and need_x * cost > self.mana():
                    continue
                if need_x >= 1:
                    r = self._cast(name, need_x, e.name)
                    if r:
                        return r
        return None

    def try_remove(self) -> Optional[dict]:
        """移除：把怪物移出战斗（X=1；异变代价由安全过滤与风险分级把关）。"""
        enemies = [en for en in self.alive_enemies() if en.entity_type == "怪物"]
        if not enemies:
            return None
        threat = max(enemies, key=monster_threat)
        for name in sorted(self.player.dao_wen):
            inst = self.player.dao_wen[name]
            if inst is None or not inst.can_use():
                continue
            if self._probe(name) and self._probe(name)["kind"] == "remove":
                r = self._cast(name, 1, threat.name)
                if r:
                    return r
        return None

    def try_control(self) -> Optional[dict]:
        """控场：对威胁最大的敌人上控制（目标本回合已控则不重复）。"""
        enemies = self.alive_enemies()
        if not enemies:
            return None
        p = self.player
        top = max(enemies,
                  key=lambda e: e.effective_attack_count() * e.effective_attack_power())
        if top.name in self._controlled_this_round:
            return None
        for name in sorted(self.player.dao_wen):
            inst = self.player.dao_wen[name]
            if inst is None or not inst.can_use():
                continue
            probe = self._probe(name)
            if probe is None or probe["kind"] != "tactician":
                continue
            x = 2 if probe["cost_per_x"] > 0 else 1
            r = self._cast(name, min(x, 3), top.name)
            if r:
                self._controlled_this_round.add(top.name)
                return r
        return None

    def try_aoe(self) -> Optional[dict]:
        """群伤：敌数≥2 时输出牌默认已覆盖（预演 diff 天然含全体敌方掉血）。"""
        if len(self.alive_enemies()) < 2:
            return None
        return None   # 实时评分已按敌方总掉血比较单体/群体，无需单独策略

    def try_debuff(self) -> Optional[dict]:
        """削弱：对最肥目标上战术牌（每张每目标每场一次）。"""
        enemies = self.alive_enemies()
        if not enemies:
            return None
        tank = max(enemies, key=lambda e: e.current_hp)
        for name in sorted(self.player.dao_wen):
            inst = self.player.dao_wen[name]
            if inst is None or not inst.can_use():
                continue
            if self.used.get(f"debuff:{name}:{tank.name}"):
                continue
            probe = self._probe(name)
            if probe is None or probe["kind"] != "tactician":
                continue
            x = 2 if probe["cost_per_x"] > 0 else 1
            r = self._cast(name, min(x, 3), tank.name)
            if r:
                self.used[f"debuff:{name}:{tank.name}"] = 1
                return r
        return None

    def try_pressure(self) -> Optional[dict]:
        """输出：预算内性价比最高（单价来自预演），焦点血最少目标。"""
        enemies = self.alive_enemies()
        if not enemies:
            return None
        target = min(enemies, key=lambda e: e.current_hp)
        budget = self.mana_budget()
        ranked = []
        for name in sorted(self.player.dao_wen):
            inst = self.player.dao_wen[name]
            if inst is None or not inst.can_use():
                continue
            probe = self._probe(name)
            if probe is None or probe["kind"] != "damage" or probe["dmg"] <= 0:
                continue
            cost = probe["cost_per_x"]
            x = min(9, max(1, budget // cost) if cost > 0 else 2)
            ranked.append((-self._dmg_at(name, x, target.name), name, x))
        for _, name, x in sorted(ranked):
            r = self._cast(name, x, target.name)
            if r:
                return r
        return None

    def try_ramp(self) -> Optional[dict]:
        """资源：法力见底且有换取手段时补充。"""
        if self.mana() >= 3:
            return None
        for name in self.owned("ramp"):
            r = self._cast(name, 2)
            if r:
                return r
        return None

    def try_reroll(self) -> Optional[dict]:
        """消灾类：血线告急且碎片富余时买一次重掷（文本『重置随机』归纳）。"""
        p = self.player
        if p.current_hp > p.blood_limit * 0.5:
            return None
        state = self.engine.state
        if not (state.shards >= 5 or state.fake_shards >= 50):
            return None
        for name in sorted(p.dao_wen):
            if daowen_text_kind(p.dao_wen[name]) == "ramp" and "重置" in _full_text(p.dao_wen[name]):
                return self._cast(name, 1)
        return None

    def try_consumable(self) -> Optional[dict]:
        """消耗品：不消耗出手；完整后果预演 + 通用风险分级（不查物品名）。"""
        p = self.player
        if self.consumable_gate is not None:
            try:
                if not self.consumable_gate(self):
                    return None
            except Exception:
                return None
        elif p.current_hp > p.blood_limit * 0.4:
            return None
        for item in list(self.engine.state.consumables):
            if getattr(item, "current_uses", 0) <= 0:
                continue
            pv = self.previewer.preview("consume_item", {"name": item.name})
            if not pv.get("result") or not pv["result"].get("success"):
                continue
            risk_level, reasons = ActionPreview.risk_classify(pv.get("diff", {}), p)
            if risk_level in ("LETHAL", "CRITICAL"):
                self.preview_rejected.append(
                    f"消耗品{item.name}（{risk_level}：{'；'.join(reasons) or '通用风险'})")
                if self.verbose:
                    self.log.append(f"[安全过滤] 拒绝 消耗品{item.name}: {risk_level} {' '.join(reasons)}")
                continue
            r = self.engine.execute_action("consume_item", {"name": item.name})
            if r.get("success"):
                self.used[f"消耗品·{item.name}"] = self.used.get(f"消耗品·{item.name}", 0) + 1
                return r
        return None

    def try_artifact(self) -> Optional[dict]:
        """可选法器（不占出手）：由 engine/ai_rules.py 的策略驱动，引擎校验把关。"""
        from engine.ai_rules import (
            try_fire_godfather_revolver, try_use_blood_wings,
        )
        for fn in (try_fire_godfather_revolver, try_use_blood_wings):
            r = fn(self.engine)
            if r and r.get("success"):
                self.used[f"法器·{r.get('action', '')}"] = self.used.get(
                    f"法器·{r.get('action', '')}", 0) + 1
                return r
        return None

    # ---------- 主入口 ----------

    def take_action(self) -> Optional[dict]:
        """执行一次出手：实时评估候选；子类固定串（archive 实验）走旧级联。"""
        self.last_decision = None
        self.resolve_pending_redemption()
        if not self.alive_enemies() or not self.player.is_alive:
            return None
        if self.STRATEGIES:            # 仅实验子类定义了固定顺序
            for fn in self.STRATEGIES:
                r = getattr(self, fn)()
                if r:
                    self.resolve_pending_redemption()
                    return r
            return None
        r = self._dynamic_action()
        if r:
            self.resolve_pending_redemption()
        return r

    def new_round(self) -> None:
        """[回始]调用：清回合记账，更新凡庸压力。"""
        self._controlled_this_round.clear()
        p = self.player
        if p is not None:
            self._hp_trace.append(p.current_hp)
        if self._damage_done_battle:
            self._rounds_since_damage = 0
        else:
            self._rounds_since_damage += 1
        self._damage_done_battle = False

    def take_turn(self) -> list[dict]:
        """执行本回合全部主动出手；普攻不支付当前速度。"""
        results = []
        self._refresh_personality()
        c = self.try_consumable()
        if c:
            results.append(c)
        # 轮回者的主动出手预算由 action_count（基础2次 + 修正）给出，
        # 不能再用速限/3；速度只在闪避等正文明确的机制里发生变化。
        for _ in range(max(1, self.player.action_count)):
            if not self.alive_enemies() or not self.player.is_alive:
                break
            r = self.take_action()
            if not r:
                break
            results.append(r)
        return results


def _full_text(inst) -> str:
    dw = getattr(inst, "dao_wen", inst)
    return " ".join(filter(None, (
        getattr(dw, "formula", ""), getattr(dw, "effect_formula", ""),
        getattr(dw, "trigger_timing", ""), getattr(dw, "cost_type", ""),
    )))


# 闪避启发式已迁到正式运行时助手 engine/ai_rules.py（引擎自动反应法术路径与
# 本实验模块共用同一实现）；此处保留同名再导出，供既有 sim/tests 引用。
from engine.ai_rules import choose_dodge  # noqa: E402,F401


def monster_threat(entity) -> int:
    """怪物视角的目标威胁分：物理输出 + 道纹持有加成（引擎可见事实，不查表）。

    施法型道纹（cost_type=消耗，如杀伐/庇护/再生）是可反复发动的主动手段，
    威胁 +10；代价型（异变/冷却/流血等）+4；空 cost_type（纯被动/触发）+0。
    """
    if entity is None:
        return 0
    # DM裁定 2026-09-10：威胁分按 effective 攻次×攻力（怪物回落面板，行为不变）
    score = ((entity.effective_attack_power() or 0)
             * (entity.effective_attack_count() or 0))
    for inst in (entity.dao_wen or {}).values():
        dw = getattr(inst, "dao_wen", inst)
        cost_type = getattr(dw, "cost_type", "") or ""
        if cost_type == "消耗":
            score += 10
        elif cost_type:
            score += 4
    return score


def choose_attack_target(attack_target_options: list[dict], refs: dict) -> str:
    """怪物攻击目标：挑威胁最大者。威胁分相同时取当前生命最低（脆的先倒）。"""
    if not attack_target_options:
        return ""
    scored = []
    for option in attack_target_options:
        entity = refs.get(option["ref"])
        score = monster_threat(entity)
        hp = entity.current_hp if entity is not None else 10 ** 9
        scored.append((score, hp, option["ref"]))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return scored[0][2]
