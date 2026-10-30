"""
死斗【当事人视角】前台战报生成器。

背景（先检查架构再修改的结论，写在这里备查）：
  * 现有战报生成是纯"排版层"：engine/battle_report.py 的 format_* 函数只把
    execute_action() 已经返回的结果字典转成文字，不产生任何新数值、不做
    任何规则判定。sim/*.py 里的多个死斗驱动脚本（generate_duel_report.py /
    duel_pvp.py / duel_common.py 等）负责手操调用引擎、拼装最终 报告.md。
  * 引擎（combat.py/api.py/models.py）里没有"信息隐藏"层：execute_action
    返回的是完整的上帝视角事实（双方真实HP/法力/道纹……），这是刻意的
    设计——后台审计需要这份完整事实源，AI 的真实决策也需要读取自己一方
    的真实状态。因此**不能**在引擎内部封堵字段，否则会连累后台审计与
    AI 决策；正确的扩展点是在结果产出**之后**、写入战报**之前**加一层
    只读的可见性过滤器——也就是本模块。
  * AI 获取"对手状态"的入口（engine/ai_tactics.py / ai_preview.py）已经
    只读取 self.engine 自身可访问的真实状态用于**引擎内部决策**，这是
    游戏规则允许的"上帝视角结算"，与"战报要不要把这份事实告诉玩家/读者"
    是两个独立问题——本任务只改后者，不改前者（不修改 ai_tactics.py /
    ai_preview.py / personality.py 的任何决策逻辑）。

设计结论：
  1. 不新增战斗系统、不改现有战报格式：engine/battle_report.py 与
     sim/*.py 现有的完整战报保留不动，作为战报末尾的【后台审计数据】。
  2. 新增本模块，只做"事后可见性过滤"：输入是 execute_action() 已经返回
     的结果字典（与后台审计同一份事实源），输出是面向死斗双方共同能够
     看到的【当事人视角】叙事行。
  3. 本模块是纯函数集合：不持有任何 GameState/Entity 引用、不发起任何
     随机数、不修改任何引擎状态、不参与结算判定——调用多少次、什么顺序
     调用都不会影响游戏结果，天然不触碰 save/load、事务回滚、死斗结算。

信息边界（对应任务书第 3/4/5/6/7/8/9 条）：
  * 双方隐藏资源——HP、血限、法力、法限、速度、道纹、残韵、遗物、消耗品、
    Buff/Debuff、尚未公开使用的能力、已使用但对方无法从公开结果确定的
    具体资源、AI 内部评分、候选行动、性格数据、内部推理、任何引擎内部
    状态——一律不进入本模块的任何输出。
  * 角色说的话（由调用方显式传入的 text，代表角色真正说出口的话）原样
    进入战报，不做真实性校验、不做心理解读、不由旁白拆穿。
  * 造成的伤害或直接失去生命数值属于"公开可观察结果"：一方能感知到自己挨了多少下、
    失去了多少生命，因此伤害/直接失血/闪避/死亡/撤退这类物理可见事件会展示；
    但引发该结果的具体道纹/法术/遗物名称，以及事后的资源总量（HP/血限/
    法力/法限/速度等），默认一律不可见——除非调用方显式传入 reveal_name
    （代表这次发动已经通过某种无可置疑的方式被公开识别）。默认永远选择
    "不泄露"，一切名称与资源总量在没有显式授权的情况下都不会出现。
"""
from __future__ import annotations

from typing import Any, Optional


# ---------------------------------------------------------------------------
# 事实提取：把引擎已经返回的结果字典，压缩成"只含公开可观察结果"的 fact 列表。
# 不读取任何 Entity/GameState 字段——只读传入的 dict，天然不可能读到隐藏状态。
# ---------------------------------------------------------------------------

def extract_daowen_facts(
    result: dict,
    *,
    actor_side: str,
    actor_name: str,
    target_side: Optional[str] = None,
    target_name: Optional[str] = None,
) -> list[dict]:
    """从 use_daowen 的 execute_action 返回值里提取公开事实。

    只识别：闪避（是否尝试/成功，物理可见动作）、跨阵营伤害（数值增量，
    不含事后HP/血限总量）、命零。己方内部效果（自我治疗/增益/状态）与
    法力/代价消耗一律不提取——这些是隐藏资源变化，不属于"公开可观察结果"。
    """
    if not isinstance(result, dict) or not result.get("success"):
        return [{"kind": "no_action"}]

    facts: list[dict] = []
    hostile = bool(target_name) and target_name != actor_name and actor_side != target_side

    dodge_log = result.get("dodge") or {}
    if dodge_log.get("fully_dodged"):
        for d in dodge_log.get("dodged_names", []) or []:
            if isinstance(d, dict):
                facts.append({"kind": "dodge", "who": d.get("name", target_name),
                              "attempted": True, "success": True})
        return facts  # 完全闪避：判定失效，没有后续可展示的公开结果

    if hostile and not dodge_log.get("must_hit"):
        # 必中(must_hit)由施法者自身资源决定，是否揭示"没有选择余地"会
        # 反过来暴露对方是否持有必中资源——一律不展示 must_hit 分支。
        facts.append({"kind": "dodge", "who": target_name, "attempted": False, "success": False})

    exec_block = result.get("execution") or {}
    for eff in exec_block.get("effects") or []:
        if not isinstance(eff, dict):
            continue
        if eff.get("type") in ("damage", "aoe_damage") and hostile:
            facts.append({
                "kind": "damage",
                "target": eff.get("target"),
                "amount": eff.get("actual_damage"),
                "absorbed": eff.get("shield_absorbed") or 0,
                "died": bool(eff.get("died")),
            })
        elif eff.get("type") == "target_life_loss" and hostile:
            # 杀伐等直接失去生命不是伤害，但数值与命零同样是公开可观察结果。
            facts.append({
                "kind": "life_loss", "target": eff.get("target"),
                "amount": eff.get("lost", 0), "died": bool(eff.get("died")),
            })
    return facts


def extract_hit_facts(
    detail: dict,
    *,
    actor_side: str,
    target_side: str,
) -> list[dict]:
    """从 attack 一击(resolve_attack 的 hit / resolve_monster_phase 的 detail)提取公开事实。

    兼容两种引擎结果形状：resolve_attack 返回的 hits[i]、resolve_monster_phase
    返回的 details[i]（字段基本一致，均为扁平 dict，不含execution嵌套）。
    """
    if not isinstance(detail, dict):
        return []
    if detail.get("skipped") or detail.get("collapsed"):
        return [{"kind": "no_action"}]
    if detail.get("cant_target"):
        return [{"kind": "cant_target"}]
    if detail.get("blood_shadow_success"):
        # 血影：目标支付生命取消判定——生命是隐藏资源变化，只展示"判定被取消"
        return [{"kind": "cancelled"}]

    hostile = actor_side != target_side
    facts: list[dict] = []
    target_name = detail.get("target")

    if hostile and detail.get("dodge_success"):
        facts.append({"kind": "dodge", "who": target_name, "attempted": True, "success": True})
        return facts  # 完全闪避：无后续伤害可展示

    if hostile and "dodge_attempted" in detail:
        facts.append({"kind": "dodge", "who": target_name,
                      "attempted": bool(detail.get("dodge_attempted")), "success": False})

    if hostile:
        dmg = detail.get("damage_dealt")
        if dmg is None:
            dmg = detail.get("hp_lost")
        if dmg is not None and (dmg > 0 or detail.get("target_died")):
            facts.append({
                "kind": "damage", "target": target_name,
                "amount": dmg, "absorbed": detail.get("shield_absorbed") or 0,
                "died": bool(detail.get("target_died")),
            })

    if detail.get("retreated"):
        facts.append({"kind": "retreat", "who": target_name})

    return facts


# ---------------------------------------------------------------------------
# 记录构造：驱动脚本调用这些便捷函数，把一场死斗按回合顺序压入 entries 列表。
# ---------------------------------------------------------------------------

def record_speech(entries: list, round_no: int, actor_name: str, text: str) -> None:
    """角色真正说出口的话——原样进入战报，不做真实性校验、不做心理解读。"""
    if not isinstance(text, str) or not text.strip():
        return
    entries.append({"round": round_no, "kind": "speech",
                     "actor_name": actor_name, "text": text.strip()})


def record_environment(entries: list, round_no: int, text: str) -> None:
    """必要的环境信息（背景描述等，不带任何数值），双方公开可见。"""
    if not text:
        return
    entries.append({"round": round_no, "kind": "environment", "text": text})


def record_note(entries: list, round_no: int, text: str) -> None:
    """公开、可观察的场面记述（如"死斗开始""胜负已分"），不含隐藏资源。"""
    if not text:
        return
    entries.append({"round": round_no, "kind": "note", "text": text})


def record_daowen(
    entries: list,
    round_no: int,
    *,
    actor_side: str,
    actor_name: str,
    result: dict,
    target_side: Optional[str] = None,
    target_name: Optional[str] = None,
    reveal_name: Optional[str] = None,
) -> None:
    """记录一次 use_daowen 行动。

    reveal_name：只有当调用方能够证明"这次发动已经通过某种无可置疑的方式
    被公开识别"时才传入（例如此前已经公开过的同名效果）。默认 None——
    道纹/法术具体名称一律不写入前台，无论后台是否已知。
    """
    facts = extract_daowen_facts(
        result, actor_side=actor_side, actor_name=actor_name,
        target_side=target_side, target_name=target_name,
    )
    label = reveal_name or "一种未公开的能力"
    entries.append({"round": round_no, "kind": "action", "actor_name": actor_name,
                     "target_name": target_name, "label": label, "facts": facts})


def record_attack(
    entries: list,
    round_no: int,
    *,
    actor_side: str,
    actor_name: str,
    target_side: str,
    hits: list,
) -> None:
    """记录一轮普通攻击（不涉及道纹，天然没有需要隐藏的"名称"）。"""
    facts: list[dict] = []
    for hit in hits or []:
        facts.extend(extract_hit_facts(hit, actor_side=actor_side, target_side=target_side))
    target_name = (hits[0].get("target") if hits else None)
    entries.append({"round": round_no, "kind": "action", "actor_name": actor_name,
                     "target_name": target_name, "label": "普通攻击", "facts": facts})


def record_monster_hits(
    entries: list,
    round_no: int,
    *,
    actor_side: str,
    actor_name: str,
    target_side: str,
    details: list,
) -> None:
    """记录 resolve_monster_phase 返回的一批 details（守擂方按怪物阶段结算时用）。"""
    facts: list[dict] = []
    for detail in details or []:
        facts.extend(extract_hit_facts(detail, actor_side=actor_side, target_side=target_side))
    entries.append({"round": round_no, "kind": "action", "actor_name": actor_name,
                     "target_name": None, "label": "普通攻击", "facts": facts})


# ---------------------------------------------------------------------------
# 渲染：把 entries 转成文字行。纯排版，不读取 GameState/Entity。
# ---------------------------------------------------------------------------

_FORBIDDEN_NARRATION_HINT = (
    # 仅用于开发期自查：这些词一旦出现在渲染结果里，说明有人往 label/text
    # 里塞了心理活动/性格解释，而不是"角色实际说的话"或"可观察结果"。
    "心想", "认为", "判断", "猜测", "故意", "看穿", "识破", "欺骗", "说谎",
)


def _render_fact(fact: dict) -> Optional[str]:
    kind = fact.get("kind")
    if kind == "damage":
        parts = [f"{fact.get('target')} 受到{fact.get('amount')}点伤害"]
        if fact.get("absorbed"):
            parts.append(f"（格挡吸收{fact['absorbed']}点）")
        if fact.get("died"):
            parts.append("，当场[命零]")
        return "".join(parts)
    if kind == "life_loss":
        parts = [f"{fact.get('target')} 失去{fact.get('amount')}点生命"]
        if fact.get("died"):
            parts.append("，当场[命零]")
        return "".join(parts)
    if kind == "dodge":
        who = fact.get("who")
        if fact.get("success"):
            return f"{who} 选择闪避，判定完全失效"
        if fact.get("attempted"):
            return f"{who} 试图闪避但未能成功"
        return f"{who} 选择不闪避"
    if kind == "retreat":
        return f"{fact.get('who')} 自动撤退，退出本场战斗"
    if kind == "no_action":
        return "未能完成本次行动"
    if kind == "cant_target":
        return "选定目标失败，未能行动"
    if kind == "cancelled":
        return "判定被取消"
    return None


def _act_header(actor_name: str, target_name: Optional[str], label: str) -> str:
    if not target_name or target_name == actor_name:
        return f"{actor_name} 发动了{label}"
    return f"{actor_name} 对 {target_name} 发动了{label}"


def render_report(entries: list[dict]) -> list[str]:
    """把 entries 渲染成【当事人视角】文字行（含标题行）。"""
    lines = ["【当事人视角】"]
    current_round: Any = object()  # 保证第一条一定换行
    for entry in entries:
        rnd = entry.get("round")
        if rnd != current_round:
            lines.append("")
            lines.append(f"第{rnd}回合")
            current_round = rnd
        kind = entry.get("kind")
        if kind == "speech":
            lines.append(f"{entry['actor_name']}：「{entry['text']}」")
        elif kind == "environment":
            lines.append(entry["text"])
        elif kind == "note":
            lines.append(f"　{entry['text']}")
        elif kind == "action":
            lines.append(f"　{_act_header(entry['actor_name'], entry.get('target_name'), entry['label'])}")
            for fact in entry.get("facts", []):
                text = _render_fact(fact)
                if text:
                    lines.append(f"　　→ {text}")
    return lines


def assemble_duel_report(perspective_lines: list[str], audit_lines: list[str]) -> list[str]:
    """把【当事人视角】与【后台审计数据】拼成一份完整死斗战报，明确分隔两段。

    audit_lines 就是现有 engine/battle_report.py 的完整输出，原样保留，
    不做任何裁剪——后台数据的准确性与完整性不受本模块影响。
    """
    lines = list(perspective_lines)
    lines.append("")
    lines.append("=" * 24)
    lines.append("")
    lines.append("【后台审计数据】")
    lines.append("")
    lines.extend(audit_lines)
    return lines
