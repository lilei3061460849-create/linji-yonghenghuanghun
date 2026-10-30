"""快照拷贝工具：给「预演沙盒」和「事务回滚」用的状态/随机源复制。

为什么要单独一层：``copy.deepcopy(GameState)`` 的成本几乎全部来自**只追加的
事实记录**——战斗事件流 ``combat_events`` 在长局里可以到几千条，实测占整份
状态深拷贝的 95% 以上（4200 条事件：33.7ms，其中 32.7ms 是事件本身）。而这类
记录按 ``engine/combat_events.py`` 的既有契约是**不可变事实源**：

    \"单一事实源不可变事件流\" —— 事件登记后只读，只追加、不原地修改。

所以快照只复制**容器**（列表本身照常新建，长度与顺序与真实状态一致），
事件对象共享引用：预演沙盒用后即弃，事务快照失败时按长度/内容还原，两者都
不需要事件对象的私有副本。若将来有代码要**原地修改**已登记事件，必须同时
改这里——否则预演会污染真实事件流。

随机源两种口径（见 ``DiceEngine``）：

* ``copy_dice_for_snapshot(keep_history=False)``：**预演沙盒专用**。预演结束时
  整份沙盒随机源被丢弃，只需要「同样的随机状态 + 同样的池」；把整份 roll
  历史再复制一遍纯属重复劳动（实测与整个 GameState 同价，且随局数增长）。
* ``keep_history=True``：**事务回滚专用**。回滚要还原的是可被外部读取的
  真实随机源，roll 历史是它的一部分（``dice.get_history()`` 对外可见），
  必须原样复制。
"""
from __future__ import annotations

import copy
import random
from typing import Any


# 快照共享的「只追加事实记录」字段：容器照常复制，元素共享引用。
_SHARED_RECORD_FIELDS = ("combat_events",)


def copy_state_for_snapshot(state: Any) -> Any:
    """复制一份状态快照：只追加的事实记录共享元素，其余照常深拷贝。"""
    memo: dict[int, Any] = {}
    for field in _SHARED_RECORD_FIELDS:
        for record in getattr(state, field, None) or []:
            memo[id(record)] = record
    return copy.deepcopy(state, memo)


def copy_dice_for_snapshot(dice: Any, *, keep_history: bool = True) -> Any:
    """复制随机源。

    keep_history=True：完整复制（事务回滚口径，roll 历史对外可见）。
    keep_history=False：只复制随机状态与命名池，历史留空（沙盒口径，
    沙盒里的 roll 结果对象本身仍带 record，调用方读 record 不受影响）。
    """
    if keep_history:
        return copy.deepcopy(dice)
    clone = copy.copy(dice)
    clone._pools = {name: list(options) for name, options in dict(dice._pools).items()}
    clone._history = []
    rng = random.Random()
    rng.setstate(dice._rng.getstate())
    clone._rng = rng
    return clone


# ===========================================================================
# 沙盒作用域：一次「不会碰到真实世界」的执行所需要的全部隔离项
# ===========================================================================
# 为什么要有这张表（而不是在预演里手写保存/恢复）：
#
# 隔离项是**引擎状态的清单**，不是某段代码的细节。历史上两次真实的污染
# （2026-09-19 combat 运行态、2026-09-20 event_pool）都是「有人加了一处
# 引擎侧可变状态，而沙盒没跟着加」。把清单集中在这里以后：
#   1. 新增引擎侧可变状态时，tests/test_sandbox_pollution.py 的
#      「未分类可变状态」用例会直接失败——由测试提醒，不靠记忆；
#   2. 预演/事务两条路径共用同一份读写实现，不会各写一份而漂移；
#   3. 每一项都写明「为什么必须隔离 / 为什么可以共享」，便于复核。
#
# 三类隔离方式（成本从低到高，按需选最低的那一档）：
#   swap    —— 整体换成副本对象，退出时换回原对象（大对象用，如 state/dice）
#   runtime —— 原地保存/恢复内容（小对象、且身份被别处引用，如 id 键字典）
#   restore —— 保存副本、退出时整体写回（长度/标量型，如行动历史）
#   context —— 对象自带 snapshot/restore 协议（如 ResolutionContext）
#
# 明确**不需要**隔离的（写清楚以免下次又被"顺手加上"）：
#   monster_pool / event_pool.events / relics_pool —— 规则数据，只读；
#   death_book / rulings_db                         —— 外部 IO，不属于战斗结算；
#   hook_manager / mechanism_bus                    —— 机制**定义**表（全局注册），
#                                                      每个引擎一份壳但内容只读。

#: 需要原地保存/恢复的战斗运行态（键多为 id(entity)，身份必须稳定）。
COMBAT_RUNTIME_ATTRS = (
    "_monster_activated",          # 本场已激活道纹（持续激活口径）
    "_monster_daowen_round_used",  # 本回合已发动道纹（每回合每道纹至多一次）
    "_resonance_rewrites",         # 残韵改写映射（按实体）
    # 2026-10-13 用户令删【三相残韵盘】：_sanxiang_consumed 运行态一并移除。
    "_split_clones_spawned",       # 【分裂】本场已创生数量
    "_monster_evolved",            # 本场已进化怪物（每场一次）
    "_effect_chain_depth",         # 效果链深度保险丝计数器
    "_resolving_life_lost_reactions",  # 失去生命反应的再入保护计数
    "_hp_loss_recording",          # 失血事件记账计数（抑制兜底钩子）
    # 以下四项由 Phase 8 的随机压力测试**实测**发现会从预演泄漏出来
    # （预演里结算伤害/攻击/闪避会在真实引擎对象上留下沙盒实体的痕迹）：
    "_hp_loss_ctx",                # 当前一次降血的来源上下文（持实体引用）
    "_attack_after_window_target", # 当前攻击中走反应窗口的目标（持实体引用）
    "_dodge_counts",               # 本回合各自动反应路径已用闪避次数
    "_dodge_round",                # 上面那份计数属于哪一回合
    "_branch_owner_token_value",   # 反应法术分支归属 token（惰性创建，只写一次）
    "_spell_stack",                # 可暂停施法帧；进入沙盒时重建到副本实体
    "_spell_window_serial",        # 窗口token序号
    "_priority_passes",            # 普通优先权连续跳过计数
    "_ended_turn_sides",           # 【跳过】结束当前方回合的阵营标记
    "_resuming_spell_stack",       # 原法术续跑标记（影响必中闸门）
)

#: 需要整体换对象再换回的状态根（大对象，深拷贝成本已由 sandbox 口径压低）。
SWAPPED_ROOTS = ("state", "dice")

#: 自带 snapshot/restore 协议的引擎侧对象（不是容器，按对象协议隔离）。
CONTEXT_OBJECTS = ("resolution",)


def copy_runtime_value(value: Any) -> Any:
    """运行态专用浅拷贝：结构都是「小容器套不可变值」，不值得走 deepcopy。

    dict 重建一层（值可能是 set/dict/list），set 重建，标量原样。
    """
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(v, set):
                out[k] = set(v)
            elif isinstance(v, dict):
                out[k] = dict(v)
            elif isinstance(v, (tuple, list)):
                out[k] = type(v)(set(x) if isinstance(x, set) else x for x in v)
            else:
                out[k] = v
        return out
    if isinstance(value, set):
        return set(value)
    return value


def snapshot_engine_side(engine: Any) -> dict:
    """保存一次沙盒作用域需要隔离的**引擎侧**（非 state）可变状态。

    返回的 token 交给 ``restore_engine_side`` 恢复；两者必须成对使用。
    """
    combat = engine.combat
    pool = getattr(engine, "event_pool", None)
    return {
        "runtime": {k: copy_runtime_value(getattr(combat, k, None))
                    for k in COMBAT_RUNTIME_ATTRS},
        # 有些运行态是惰性创建的（如 _split_clones_spawned 首次分裂才出现）。
        # 保存时必须记下「本来没有」，否则恢复会凭空创建出这个属性——那也是污染。
        "runtime_absent": tuple(k for k in COMBAT_RUNTIME_ATTRS
                                if not hasattr(combat, k)),
        "pending_interrupts": copy.deepcopy(engine._pending_interrupts),
        # 【重来本场】的战始快照。沙盒目前不会执行 battle_start（预览只用
        # consume_item / use_daowen / use_resonance / resolve_monster_phase），
        # 但"现在不会"不等于"永远不会"——一旦有人在沙盒里预览战始，这个字段
        # 就会被整体替换，真实引擎的重来目标随之被污染。存引用即可，因为
        # _action_battle_start 是整体重新赋值、从不在原对象上就地改写；
        # 若在此 deepcopy，AI 预览循环每步都要多拷一份 state，明显拖慢。
        "battle_restart_snapshot": getattr(engine, "_battle_restart_snapshot", None),
        "action_history_len": len(engine._action_history),
        "last_result": engine._last_result,
        # 事件池的「已触发集合 + 当前待结算事件」：结算事件的选项会写这两项，
        # 而沙盒不换 event_pool 对象本身（规则数据 events 是只读的大表）。
        "event_triggered": set(pool.triggered) if pool is not None else None,
        "event_current": getattr(pool, "current", None) if pool is not None else None,
        # 结算生命周期（深度/预算/链）：预演里也会进入结算，必须与 state/dice
        # 一起换回，否则预演会把真实行动的深度与预算吃掉（见 engine/resolution.py）。
        "resolution": combat.resolution.snapshot(),
    }


def restore_engine_side(engine: Any, token: dict) -> None:
    """按 token 恢复引擎侧可变状态（全部原地恢复，保持对象身份稳定）。"""
    combat = engine.combat
    absent = set(token.get("runtime_absent", ()))
    for key, value in token["runtime"].items():
        if key in absent:
            # 快照时本就不存在：沙盒里被惰性创建出来的，退出时删掉。
            if hasattr(combat, key):
                delattr(combat, key)
            continue
        current = getattr(combat, key, None)
        if isinstance(current, dict) and isinstance(value, dict):
            current.clear()
            for k, v in value.items():
                current[k] = set(v) if isinstance(v, set) else v
        elif isinstance(current, set) and isinstance(value, set):
            current.clear()
            current.update(value)
        else:
            setattr(combat, key, value)
    engine._pending_interrupts = token["pending_interrupts"]
    engine._battle_restart_snapshot = token.get("battle_restart_snapshot")
    del engine._action_history[token["action_history_len"]:]
    engine._last_result = token["last_result"]
    combat.resolution.restore(token["resolution"])
    pool = getattr(engine, "event_pool", None)
    if pool is not None and token["event_triggered"] is not None:
        # 原地恢复：event_pool 身份被 api.py 的存档/读档路径引用。
        pool.triggered.clear()
        pool.triggered.update(token["event_triggered"])
        pool.current = token["event_current"]
