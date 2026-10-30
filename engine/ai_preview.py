"""TacticalAI 行动后果预演层（2026-08-19）。

架构：
    候选动作生成 → CombatEngine 预演 → 完整效果链与资源变化
        → 安全性过滤（预演导致轮回者命零则不可选） → 收益评分 → 正式 execute_action

预演的数值计算**全部由 CombatEngine 既有管线完成**（execute_action → combat 真实结算，
含机制总线/事件流/千荆甲反噬/触发法术/癌变等一切监听链）。本模块只做
「快照 → 真实执行 → 记录差异 → 回滚」，不复制任何伤害/反伤/回复/格挡/死亡规则，
因此不会出现"预演规则与结算规则不一致"的问题。

回滚基于 CombatEngine 既有事务/快照体系（P0 同款：deepcopy state +
原地恢复 state 内容 + 恢复引擎侧怪物状态/dice/中断/历史），保证预演零副作用。
"""
from __future__ import annotations

import copy
from typing import Any, Optional

from .pollution_guard import (assert_runtime_unchanged, guard_enabled,
                              snapshot_runtime_state)
from .sandbox import (COMBAT_RUNTIME_ATTRS, copy_dice_for_snapshot,
                      copy_state_for_snapshot, restore_engine_side,
                      snapshot_engine_side)


#: 调试开关：LJ_POLLUTION_GUARD=1 时每次预演自动自检（见 pollution_guard）。
_GUARD_ON = guard_enabled()


class ActionPreview:
    """行动后果预演器。preview() 返回动作的完整后果，不改变真实战斗状态。"""
    # 隔离项清单的唯一事实源在 engine/sandbox.py：预演与事务共用同一份读写实现，
    # 新增引擎侧可变状态时 tests/test_sandbox_pollution.py 的「未分类可变状态」
    # 用例会先失败——由测试提醒，不靠记忆。
    _RUNTIME_ATTRS = COMBAT_RUNTIME_ATTRS

    def __init__(self, engine: Any):
        self.engine = engine

    # ---------------- 快照 / 回滚（复用 P0 事务体系） ----------------

    def snapshot(self) -> dict:
        eng = self.engine
        combat = eng.combat
        return {
            "state": copy.deepcopy(eng.state),
            "activated": copy.deepcopy(combat._monster_activated),
            "round_used": copy.deepcopy(combat._monster_daowen_round_used),
            "rewrites": copy.deepcopy(combat._resonance_rewrites),
            "split_spawned": getattr(combat, "_split_clones_spawned", 0),
            "evolved": copy.deepcopy(combat._monster_evolved),
            "effect_chain_depth": combat._effect_chain_depth,
            "dice": copy.deepcopy(eng.dice),
            "pending_interrupts": copy.deepcopy(eng._pending_interrupts),
            "action_history_len": len(eng._action_history),
            "last_result": eng._last_result,
        }

    @staticmethod
    def _restore_entity(orig, snap_entity) -> None:
        """原地恢复单个实体属性：保持对象 id 稳定。

        预演执行会修改实体属性（HP/法力/状态/动态标记）；恢复时必须把快照
        属性写回**原实体对象**，绝不能替换为新对象——combat 的
        `_monster_activated`/`_monster_daowen_round_used` 等按 id(entity) 建索引，
        实体被替换会导致 id 失配，怪物后续决策"忘记"已激活/已用状态。
        """
        if orig is None:
            return
        if snap_entity is None:
            for k in list(orig.__dict__):
                delattr(orig, k)
            return
        orig.__dict__.clear()
        orig.__dict__.update(snap_entity.__dict__)

    @classmethod
    def _restore_group(cls, state, restored, attr: str) -> None:
        """列表实体组原地恢复；仅当预演增删了实体（长度变化）才整体替换。"""
        orig_list = getattr(state, attr)
        snap_list = getattr(restored, attr)
        if len(orig_list) != len(snap_list):
            setattr(state, attr, snap_list)
            return
        for o, s in zip(orig_list, snap_list):
            cls._restore_entity(o, s)

    def restore(self, snap: dict) -> None:
        eng = self.engine
        combat = eng.combat
        state = eng.state
        restored = snap["state"]
        # 1) 实体原地恢复（保持 id 稳定，combat 的 id-key 字典不失配）
        self._restore_entity(state.player, restored.player)
        for attr in ("friends", "employees", "temp_friends", "enemies"):
            self._restore_group(state, restored, attr)
        # 2) state 其余字段从快照复制；预演新增的动态字段（如 dead_monsters/
        #    _pending_split_clones）必须删除，避免微小泄漏累积改变后续行为。
        _groups = ("player", "friends", "employees", "temp_friends", "enemies")
        for k in list(state.__dict__):
            if k not in restored.__dict__ and k not in _groups:
                delattr(state, k)
        for k, v in restored.__dict__.items():
            if k not in _groups:
                setattr(state, k, v)
        # 3) combat / engine 侧状态
        combat._monster_activated = snap["activated"]
        combat._monster_daowen_round_used = snap["round_used"]
        combat._resonance_rewrites = snap["rewrites"]
        combat._split_clones_spawned = snap["split_spawned"]
        combat._monster_evolved = snap["evolved"]
        combat._effect_chain_depth = snap["effect_chain_depth"]
        eng.dice = snap["dice"]
        # combat 与 engine 共享同一 dice 引用：预演执行可能消耗了旧 dice 对象
        # 的 RNG 状态，必须一并恢复，否则后续 combat 结算 RNG 序列偏移。
        eng.combat.dice = snap["dice"]
        eng._pending_interrupts = snap["pending_interrupts"]
        del eng._action_history[snap["action_history_len"]:]
        eng._last_result = snap["last_result"]

    # ---------------- 预演主入口 ----------------

    def preview(self, action_type: str, params: Optional[dict] = None) -> dict:
        """预演一次动作：返回 {result, diff}；任何情况都不改变真实状态。

        result：execute_action 的原始返回（成功/失败含 error）。
        diff：动作前后的完整后果（玩家/队友/敌方 HP、命零、资源、状态、
              触发效果、完整事件链）。

        实现采用**副本执行**（2026-08-19 修正）：deepcopy 整个 state 后在副本
        上真实执行动作，真实 state 完全不动。回滚式预演（执行→恢复）会破坏
        实体间的对象引用（如怪物 _beifu_target 指向快照副本实体），导致
        背负等重定向在后续真实战斗中漂移；副本内实体互引自洽，预演
        结果与真实执行逐位一致。
        """
        return self.preview_sequence([(action_type, params or {})])

    def preview_sequence(self, steps: list) -> dict:
        """预演一串动作（普攻 prepare_attack → resolve_attack 两段就是这种）。

        step = (action_type, params)，params 也可以是 callable——收前一步的返回，
        用来串 token 这类只有运行时才知道的值。AI 不复制任何引擎公式，只转发引擎
        自己给出的快照。任一步失败即停，result 为该失败步的返回。
        返回 {result, diff, results}：result=最后执行步的返回，results=逐步返回。

        预演内部走 `_execute_action_core`（不经 `execute_action` 的事务层）：
        sandbox 本身用后即弃，再存一份 transaction snapshot 属于纯重复劳动
        （2026-09-19 性能优化）。规则逻辑仍是同一份实现，不存在第二套结算。
        """
        eng = self.engine
        combat = eng.combat
        real_state = eng.state
        real_dice = eng.dice
        # 副本世界：state + dice（实体互引在副本内自洽）。
        # 拷贝口径见 engine/sandbox.py：只追加的战斗事件流共享元素引用
        # （不可变事实记录），随机源不复制 roll 历史——沙盒用完即弃。
        snap_state = copy_state_for_snapshot(real_state)
        snap_dice = copy_dice_for_snapshot(real_dice, keep_history=False)
        # 引擎侧可变状态（combat 运行态 / 中断 / 行动历史 / 事件池）统一走
        # sandbox 的集中清单保存，退出时原地恢复——见 engine/sandbox.py。
        saved = snapshot_engine_side(eng)
        spell_runtime = eng._snapshot_combat_runtime()
        # 调试开关（LJ_POLLUTION_GUARD=1）：正式运行是 False，只有一次布尔判断。
        # 长局（事件流很长）自动降级为「只追加事实记录不逐元素比对内容」，避免
        # 打开开关就把模拟变成慢动作；短局仍然全字段比对。
        if _GUARD_ON:
            guard_before = snapshot_runtime_state(
                eng, check_record_contents=len(real_state.combat_events) <= 200)
        else:
            guard_before = None
        eng.state = snap_state
        combat.state = snap_state
        eng.dice = snap_dice
        combat.dice = snap_dice
        eng._restore_combat_runtime(spell_runtime)
        results: list = []
        try:
            try:
                for action_type, params in steps:
                    p = params(results[-1]) if callable(params) else (params or {})
                    # 不建 transaction：sandbox 本身就是一次性副本，回滚无意义。
                    result = eng._execute_action_core(action_type, p)
                    results.append(result)
                    if not result.get("success"):
                        break
            except Exception as exc:
                return {"result": None, "error": str(exc), "diff": {}, "results": results}
            diff = self._diff(real_state, snap_state)
            return {"result": results[-1] if results else None, "diff": diff,
                    "results": results}
        finally:
            eng.state = real_state
            combat.state = real_state
            eng.dice = real_dice
            combat.dice = real_dice
            restore_engine_side(eng, saved)
            if guard_before is not None:
                assert_runtime_unchanged(
                    guard_before,
                    snapshot_runtime_state(
                        eng, check_record_contents=len(real_state.combat_events) <= 200),
                    context=f"预演 {[s[0] for s in steps]}")

    # ---------------- 后果提取 ----------------

    def _diff(self, before, after) -> dict:
        """对比执行前后两个 state，输出完整后果。"""
        diff: dict = {"player": {}, "allies": [], "enemies": [],
                      "player_dead": False, "events": [], "triggered": []}

        pb, pa = before.player, after.player
        if pb is not None and pa is not None:
            diff["player"] = {
                "hp_before": pb.current_hp, "hp_after": pa.current_hp,
                "bl_before": pb.blood_limit, "bl_after": pa.blood_limit,
                "mana_before": pb.current_mana, "mana_after": pa.current_mana,
                "speed_before": pb.current_speed, "speed_after": pa.current_speed,
                "shield_before": pb.shield, "shield_after": pa.shield,
                "mutation_delta": getattr(pa, "mutation_count", 0) - getattr(pb, "mutation_count", 0),
                "mutation_after": getattr(pa, "mutation_count", 0),
                # 本手新增累计恢复量：引擎按 total_healed 原值累计（过量回复不缩水，
                # 见 combat.py 癌变阈值口径）。净血量差看不出满血时的过量回复，
                # 故单独给一项——AI 才可能在撞上【癌变】线之前看见它。
                "healed_delta": getattr(pa, "total_healed", 0) - getattr(pb, "total_healed", 0),
                "status_before": sorted(s.name for s in pb.status_effects),
                "status_after": sorted(s.name for s in pa.status_effects),
                "dead": not pa.is_alive,
            }
            diff["player_dead"] = not pa.is_alive
        diff["shards_before"] = before.shards
        diff["shards_after"] = after.shards

        # 队友（朋友/员工/造物）按名字对齐对比
        def _ally_map(state):
            out = {}
            for group in (state.friends, state.employees, state.temp_friends):
                for e in group:
                    out.setdefault(e.name, []).append(e)
            return out

        before_map, after_map = _ally_map(before), _ally_map(after)
        for name in set(before_map) | set(after_map):
            b = before_map.get(name, [])[0] if before_map.get(name) else None
            a = after_map.get(name, [])[0] if after_map.get(name) else None
            diff["allies"].append({
                "name": name,
                "hp_before": b.current_hp if b else None,
                "hp_after": a.current_hp if a else None,
                "dead": bool(a and not a.is_alive),
                "departed": bool(b and not a) or (a is not None and getattr(a, "is_departed", False)),
            })

        # 敌方按索引对齐对比（敌人可能死亡/离场）
        for i, eb in enumerate(before.enemies):
            ea = after.enemies[i] if i < len(after.enemies) else None
            diff["enemies"].append({
                "name": eb.name,
                "hp_before": eb.current_hp,
                "hp_after": ea.current_hp if ea else 0,
                "dead": not (ea and ea.is_alive),
                # ③修复（2026-09-10 用户裁定）：动作前已死的敌人不是这一手打死/
                # 移除的。多怪局首杀后场上有尸体，不带 alive_before 时尸体在 diff
                # 里恒 dead=True → 下游 _digest_diff 把「打自己」误归纳成 remove。
                "alive_before": bool(eb.is_alive),
                # 2026-10-03：补法力/速度/异变的前后值（纯追加字段）。怪物互斗的
                # 训练评分要区分「这次发动花了多少法力/速度、堆了多少异变」，
                # 而原来只有生命前后值；既有消费者只读指定键，不受影响。
                "mana_before": eb.current_mana, "mana_after": (ea.current_mana if ea else 0),
                "speed_before": eb.current_speed, "speed_after": (ea.current_speed if ea else 0),
                "mut_before": getattr(eb, "mutation_count", 0),
                "mut_after": getattr(ea, "mutation_count", 0) if ea else 0,
            })

        # 事件流增量 = 完整效果链（含被触发的被动/监听/反噬）
        before_events = getattr(before, "combat_events", []) or []
        after_events = getattr(after, "combat_events", []) or []
        new_events = after_events[len(before_events):]
        diff["events"] = [
            {
                "type": ev.event_type.value if hasattr(ev.event_type, "value") else str(ev.event_type),
                "actor": ev.actor_name, "target": ev.target_name, "data": ev.data,
                "event_id": ev.event_id, "parent_event_id": ev.parent_event_id,
            }
            for ev in new_events
        ]
        # 触发的效果类型（供收益评分/安全过滤直接使用）
        diff["triggered"] = sorted({ev["type"] for ev in diff["events"]})
        return diff

    # ---------------- 安全过滤 ----------------

    @staticmethod
    def would_kill_player(diff: dict) -> bool:
        """预演后果是否导致轮回者本人命零（第一阶段安全性过滤核心）。"""
        if not diff:
            return False
        return bool(diff.get("player_dead"))
    @staticmethod
    def risk_classify(diff: dict, player) -> tuple:
        """通用风险分类（非特判，不依赖道纹/消耗品名）。

        基于预演 diff 和当前玩家状态，输出风险等级与具体原因。
        等级：LETHAL > CRITICAL > HIGH > MEDIUM > LOW > SAFE

        LETHAL  — 动作直接导致轮回者命零（最高优先级禁止，等同于 would_kill_player）
        CRITICAL — 动作将玩家推入明确的危险阈值（异变 45+、HP ≤ 10%、必死链条触发）
        HIGH    — 动作导致显著资源损失或状态恶化（法力/速度归零、异变 +20+、格挡清零且受致命伤）
        MEDIUM  — 可管理的风险（小资源损失、可控负面状态）
        LOW     — 轻微风险
        SAFE    — 无明显风险

        这些分类**不依赖任何道纹/消耗品具体名称**，全部基于 diff 中的数值变化
        和玩家当前状态的定量关系——因此不会出现"只有某件消耗品被标记但别的漏了"
        的问题。新增加的伤害/回复/异变/状态效果只要走引擎管线，预演 diff 就会包含，
        风险分类器自动覆盖。
        """
        if not diff:
            return "SAFE", []

        reasons = []
        p = player

        # LETHAL（最高优先级，与 would_kill_player 同含义）
        if diff.get("player_dead"):
            return "LETHAL", ["完整效果链导致轮回者命零"]

        # CRITICAL：异变达危险阈值（≥45 时动作可能触发迷失）
        mut = getattr(p, "mutation_count", 0)
        mut_delta = diff.get("player", {}).get("mutation_delta", 0)
        mutation_sensitive = getattr(p, "can_trigger_mutation_lost", lambda: True)()
        if mutation_sensitive and mut + mut_delta >= _MUT_THRESHOLD - 5:   # 离阈值5以内
            reasons.append("异变即将达到迷失阈值（迷失（%d/%d）→ 动作后（%d/%d））"
                           % (mut, _MUT_THRESHOLD, mut + mut_delta, _MUT_THRESHOLD))
            return "CRITICAL", reasons

        # CRITICAL：HP 已极低且动作会进一步降低（不含治疗方向）
        hp = p.current_hp
        hp_delta = diff.get("player", {}).get("hp_after", hp) - hp
        # CRITICAL：HP ≤ 血限10%（生产规则，DM 裁定 2026-08-24；文档=AI_EXPERIENCE.md 风险口径）且动作继续扣血（不含治疗方向）
        if hp <= p.blood_limit * 0.10 and hp_delta < 0:
            reasons.append("HP 极低（%d/%d，≤血限10%%）且动作继续扣血" % (hp, p.blood_limit))
            return "CRITICAL", reasons

        # HIGH：异变显著增加（+10 以上，无论当前等级）
        if mutation_sensitive and mut_delta >= 10:
            reasons.append("异变增加 %d（迷失（%d/%d）→ 动作后（%d/%d））"
                           % (mut_delta, mut, _MUT_THRESHOLD, mut + mut_delta, _MUT_THRESHOLD))

        # HIGH：法力/速度归零
        mana_after = diff.get("player", {}).get("mana_after", 0)
        speed_after = diff.get("player", {}).get("speed_after", -1)
        if mana_after == 0 and p.current_mana > 0:
            reasons.append("法力耗尽（%d → 0）" % p.current_mana)
        if speed_after == 0 and p.current_speed > 0:
            reasons.append("速度耗尽（%d → 0）" % p.current_speed)

        # HIGH：触发负面效果链（diff 事件中含死亡/血限/迷失类事件）
        events = diff.get("events") or []
        event_types = {e.get("type") for e in events}
        if "entity_died" in event_types and any(e.get("actor") == p.name or e.get("target") == p.name
                                                  for e in events if e.get("type") == "entity_died"):
            reasons.append("效果链含自身死亡事件")
            return "HIGH", reasons
        if "blood_limit_changed" in event_types:
            bl_delta = sum(e.get("data", {}).get("delta", 0) for e in events
                           if e.get("type") == "blood_limit_changed" and e.get("target") == p.name)
            if bl_delta < 0:
                reasons.append("血限下降 %d" % (-bl_delta))

        if reasons:
            return "HIGH", reasons

        # MEDIUM：小资源损失或可控状态
        if hp_delta < 0 and -hp_delta > p.blood_limit * 0.15:
            reasons.append("HP 损失 >%.0f%% 血限" % (100 * (-hp_delta) / max(1, p.blood_limit)))
            return "MEDIUM", reasons
        if mana_after < p.mana_limit * 0.3:
            reasons.append("法余量低（%d/%d）" % (mana_after, p.mana_limit))
            return "MEDIUM", reasons

        return "LOW", reasons

# 异变迷失阈值（与 Entity 一致，保持单一事实源）
try:
    from engine.models import Entity
    _MUT_THRESHOLD = Entity.MUTATION_COLLAPSE_THRESHOLD
except Exception:
    _MUT_THRESHOLD = 50

