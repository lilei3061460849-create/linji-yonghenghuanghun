"""探针/模拟脚本专用：把 prepare_monster_phase 的选项转成**合法提交对象**。

2026-10-02 起怪物阶段的静态契约（引擎侧，`_validate_monster_phase_static`）：

* 有合法道纹选项（``daowen_options`` 非空）时**必须**提交一个 daowen 对象，
  不能省略、不能写 ``null``；没有合法选项时才必须写 ``null``；
* ``requires_target`` 的道纹，``target_ref`` 必须落在该选项的 ``target_options`` 内；
* 波及（``dodge_submission == "per_target"``）走 ``dodge_targets``，恰好 X 个不重复目标；
* ``trigger_spell_choices`` 必须覆盖该选项列出的**每个持有者 × 每个法术**；
* 每次攻击命中的 ``spell_choices`` 必须覆盖 before/after/damage_after/life_before
  四个时机（``damage_after``/``life_before`` 在该方确实无候选时可省略，见
  `validate_spell_reaction_submission` 的兼容注记）。

**位置变更（2026-11-03）**：本模块原在 `sim/monster_phase_submit.py`。
`webapp/server.py`（生产入口）需要这两个纯函数，于是形成 `webapp → sim` 的
生产依赖——"跑网页"被迫拖着整个模拟工具链。现已搬进 `engine/`，`sim/` 下保留
同名薄壳做兼容导出。

边界不变：这是**客户端助手**（只做提交对象的拼装与校验，不参与任何结算），
引擎核心（combat/api/mechanisms）**不得** import 它——依赖方向仍是
调用方 → 本模块。生产侧的等价决策助手是 ``engine/ai_rules.py``。
"""
from __future__ import annotations

from typing import Any, Optional

REACTION_SLOTS = ("before", "after", "damage_after", "life_before")


def decline_reaction_spell_choices(target_option: dict) -> dict:
    """给一次攻击命中的名义目标：逐时机、逐法术显式 ``use: False``。"""
    return {
        key: {sp["spell_name"]: {"use": False}
              for sp in target_option.get("spell_options", {}).get(key, [])}
        for key in REACTION_SLOTS
    }


def _decline_trigger_choices(option: dict) -> dict:
    return {
        holder_ref: {sp["spell_name"]: {"use": False} for sp in spells}
        for holder_ref, spells in (option.get("trigger_spell_options") or {}).items()
    }


def daowen_submission(option: dict, *, target_ref: Optional[str] = None,
                      dodge: bool = False, blood_shadow: bool = False,
                      trigger_decisions: Optional[dict] = None) -> dict:
    """把 prepare 列出的一个 daowen_option 转成合法提交（不改变语义）。"""
    out: dict[str, Any] = {
        "name": option["name"],
        "dodge": bool(dodge),
        "blood_shadow": bool(blood_shadow),
        "trigger_spell_choices": _decline_trigger_choices(option),
    }
    for holder_ref, spells in (trigger_decisions or {}).items():
        if holder_ref not in out["trigger_spell_choices"]:
            continue
        for spell_name, decision in spells.items():
            if spell_name in out["trigger_spell_choices"][holder_ref]:
                out["trigger_spell_choices"][holder_ref][spell_name] = decision
    if option.get("requires_target"):
        if target_ref is None:
            target_ref = option["target_options"][0]["ref"]
        out["target_ref"] = target_ref
    if option.get("dodge_submission") == "per_target":
        # 波及：必须恰好提交 X 个目标（多提交/少提交都会被拒）。
        # 实现本来就在 engine/ai_rules.py，sim/monster_targets.py 只是再导出。
        # 搬进 engine/ 后必须直接指向生产侧实现，否则 engine → sim 反向依赖
        # 会被 tests/test_production_isolation.py 拦下（已实测）。
        from .ai_rules import pick_wave_dodge_targets
        out["dodge_targets"] = pick_wave_dodge_targets(option)
    return out


def daowen_choice_from(actor: dict, *, first: bool = True, **kwargs) -> Optional[dict]:
    """从 prepare 的 actor 条目取一个道纹并转成合法提交；无合法选项时返回 None。"""
    options = actor.get("daowen_options") or []
    if not options:
        return None
    option = options[0] if first else options[-1]
    return daowen_submission(option, **kwargs)


def attack_actions(actor: dict, *, engine=None, daowen_option: Optional[dict] = None,
                   dodge: bool = False, spell_uses: Optional[dict] = None) -> list[dict]:
    """按 prepare 快照构造 attack_actions（含 疯狂/狂暴/变形 的次数修正）。

    spell_uses: {timing: {spell_name: decision}}，用于把某一击的法术从"谢绝"改为
    真实提交；每次命中的对象各自独立（同一个 dict 不能复用给多击，否则引擎的
    一次性 token/结构校验会串味，故这里逐击深拷贝）。
    """
    import copy

    action_count = int(actor.get("base_attack_actions", 0) or 0)
    hit_count = int(actor.get("base_hits_per_attack", 0) or 0)
    if daowen_option:
        resolves_as = daowen_option.get("resolves_as")
        if resolves_as == "疯狂":
            action_count += int(daowen_option.get("x", 0) or 0)
        elif resolves_as == "狂暴":
            action_count += 1
        elif resolves_as == "变形" and engine is not None:
            idx = int(str(actor["actor_ref"]).split(":", 1)[1])
            hit_count = int(engine.state.enemies[idx].attack_power)
    targets = actor.get("attack_target_options") or []
    if not targets or action_count <= 0 or hit_count <= 0:
        return []
    target_option = targets[0]
    base_choices = decline_reaction_spell_choices(target_option)
    for timing, spells in (spell_uses or {}).items():
        for spell_name, decision in spells.items():
            base_choices.setdefault(timing, {})[spell_name] = decision
    return [
        {"hits": [
            {"target_ref": target_option["ref"], "dodge": bool(dodge),
             "blood_shadow": False, "spell_choices": copy.deepcopy(base_choices)}
            for _ in range(hit_count)
        ]}
        for _ in range(action_count)
    ]


def single_actor_choices(actor: dict, *, engine=None, spell_uses: Optional[dict] = None,
                         raw_spell_uses: Optional[dict] = None,
                         trigger_decisions: Optional[dict] = None,
                         dodge: bool = False) -> list[dict]:
    """单个 actor 的最小合法提交（探针最常用）。

    raw_spell_uses：**原样**提交 spell_choices（不补四时机默认值）——
    专供"旧调用点只提交 before/after"的兼容性探针使用。
    """
    import copy

    option = actor["daowen_options"][0] if actor.get("daowen_options") else None
    dao = (daowen_submission(option, trigger_decisions=trigger_decisions or {},
                            dodge=dodge) if option else None)
    if raw_spell_uses is not None:
        targets = actor.get("attack_target_options") or []
        count = int(actor.get("base_attack_actions", 0) or 0)
        hits = int(actor.get("base_hits_per_attack", 0) or 0)
        if not targets or count <= 0 or hits <= 0:
            acts: list[dict] = []
        else:
            acts = [{"hits": [{"target_ref": targets[0]["ref"], "dodge": bool(dodge),
                               "blood_shadow": False,
                               "spell_choices": copy.deepcopy(raw_spell_uses)}
                              for _ in range(hits)]}
                    for _ in range(count)]
    else:
        acts = attack_actions(actor, engine=engine, daowen_option=option,
                              dodge=dodge, spell_uses=spell_uses)
    return [{"actor_ref": actor["actor_ref"], "daowen": dao, "attack_actions": acts}]


def monster_phase_choices(actors: list[dict], *, engine=None,
                          spell_uses: Optional[dict] = None,
                          daowen_overrides: Optional[dict] = None) -> list[dict]:
    """全体 actor 的最小合法提交：有合法道纹就声明第一个，其余全部谢绝。

    spell_uses / daowen_overrides 以 actor_ref 为键，便于探针只改自己要测的那一项。
    """
    out = []
    for actor in actors:
        ref = actor["actor_ref"]
        override = (daowen_overrides or {}).get(ref)
        option = None
        if override is not None:
            option = override if isinstance(override, dict) else None
        elif actor.get("daowen_options"):
            option = actor["daowen_options"][0]
        dao = daowen_submission(option) if option else None
        out.append({
            "actor_ref": ref,
            "daowen": dao,
            "attack_actions": attack_actions(
                actor, engine=engine, daowen_option=option,
                spell_uses=(spell_uses or {}).get(ref)),
        })
    return out
