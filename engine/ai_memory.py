"""统一 AI 的长期记忆与行为观察层。

记忆属于当前轮回者实体，不属于 GameEngine，也不属于《死者之书》：
- 轮回开始时由 AI 自述一份虚构的身世、遗憾、价值和目标；
- 每次成功行动后记录一条有限长度的经历，并抽取可解释的性格证据；
- 性格证据通过 GameEngine.update_personality 写入现有性格系统；
- 轮回者命零时由 GameEngine 把记忆压缩为一条遗言，然后清空全部记忆；
- 只有经死者之页审核写入的遗言跨轮回保留。

记忆中的 origin/regret/belief 是角色的主观叙事，不是引擎事实，不能改变
数值、合法性或随机结果。模块故意使用确定种子生成初始自述，保证存档和测试可复现，
同时让不同角色拥有不同的起点。
"""
from __future__ import annotations

import copy
import random
from typing import Any


SCHEMA_VERSION = 1
MAX_EPISODES = 48
MAX_LESSONS = 12

# 这些是“角色自己相信的故事”，不是世界事实。组合而不是固定整句，避免所有角色
# 只在几个预制人格之间切换；seed 决定初始组合，后续行为再修正它。
_ORIGINS = (
    "曾在罪孽都市替人收债，最后连自己的名字也被抵押",
    "曾是龙心谷边缘村落的守夜人，亲眼看过同伴被龙火带走",
    "曾在扭曲都市的废墟里寻找失踪的家人，却只找到一枚旧徽章",
    "曾被乱葬岗的无名者收留，靠替死人记名字活到今天",
    "曾经相信轮回是一场恩赐，后来发现自己忘掉了最重要的人",
    "曾在一场死斗中为了活命独自逃走，从此不敢轻易相信承诺",
)
_REGRETS = (
    "没有在最后一刻回头救人",
    "把活下来的机会让给了一个不值得的人",
    "明明看见危险，却因为犹豫错过了出手时机",
    "为了赢得更快，亲手耗尽了本来可以保命的资源",
    "从未向真正重要的人说过一句告别",
    "把一次失败归咎于别人，直到失去再次道歉的机会",
)
_VALUES = (
    "活下来比证明自己勇敢更重要",
    "承诺一旦说出口就不能轻易收回",
    "未知本身值得付出代价去接近",
    "任何资源都应该留一部分给明天",
    "不能把队友当成可以随时替换的工具",
    "真正的自由是不再欠任何人债",
)
_FEARS = (
    "再次在关键时刻被留下",
    "在还没有理解真相前就死去",
    "亲手造成和旧日一样的悲剧",
    "法力和退路同时耗尽",
    "相信错误的人并重复过去的选择",
    "活着，却逐渐认不出自己",
)
_GOALS = (
    "找到自己进入轮回的真正原因",
    "把那枚旧徽章交还给它原来的主人",
    "在终音之前证明那次逃走不是唯一的结局",
    "让至少一个同行者安全离开这一轮",
    "亲自向造成旧日悲剧的人问清答案",
    "留下一个不会被下一次轮回抹掉的选择",
)


def create_memory(name: str, seed: int | str | None = None) -> dict:
    """创建一份当前轮回者的自述记忆。"""
    rng = random.Random(seed if seed is not None else name)
    choices = [
        rng.choice(_ORIGINS),
        rng.choice(_REGRETS),
        rng.choice(_VALUES),
        rng.choice(_FEARS),
        rng.choice(_GOALS),
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "origin_type": "self_authored_fiction",
        "identity": {
            "origin": choices[0],
            "regret": choices[1],
            "value": choices[2],
            "fear": choices[3],
            "goal": choices[4],
        },
        "episodes": [],
        "lessons": [],
        "observed_actions": {},
        "last_reflection_battle": 0,
    }


def ensure_memory(entity: Any, seed: int | str | None = None) -> dict:
    """为存活实体惰性创建记忆；兼容旧存档和非轮回者实体。"""
    memory = getattr(entity, "ai_memory", None)
    if not isinstance(memory, dict) or not memory.get("identity"):
        memory = create_memory(getattr(entity, "name", "无名轮回者"), seed)
        entity.ai_memory = memory
    return memory


def _snapshot_facts(snapshot: dict) -> dict:
    return {
        "hp": int(snapshot.get("hp", 0) or 0),
        "blood_limit": int(snapshot.get("blood_limit", 0) or 0),
        "mana": int(snapshot.get("mana", 0) or 0),
        "mana_limit": int(snapshot.get("mana_limit", 0) or 0),
        "speed": int(snapshot.get("speed", 0) or 0),
        "shield": int(snapshot.get("shield", 0) or 0),
        "enemy_hp": int(snapshot.get("enemy_hp", 0) or 0),
        "threat": int(snapshot.get("threat", 0) or 0),
    }


def action_facts(before: dict, after: dict, result: dict, action_info: dict | None = None) -> dict:
    """把一次真实引擎动作归纳成记忆/性格观察所需的事实。"""
    b = _snapshot_facts(before)
    a = _snapshot_facts(after)
    info = action_info or {}
    action = info.get("action") or result.get("action", "") or "未知行动"
    label = info.get("label") or action
    calc = result.get("calculation", {}) or {}
    name = calc.get("dao_wen", "") or calc.get("daowen", "")
    if not name and action == "use_daowen":
        name = info.get("params", {}).get("daowen_name", "")
    x = calc.get("x", 0) or info.get("params", {}).get("x", 0) or 0
    return {
        "action": action,
        "label": label,
        "daowen": name,
        "x": int(x or 0),
        "hp_loss": max(0, b["hp"] - a["hp"]),
        "blood_limit_loss": max(0, b["blood_limit"] - a["blood_limit"]),
        "mana_spent": max(0, b["mana"] - a["mana"]),
        "mana_before": b["mana"],
        "mana_limit": b["mana_limit"],
        "speed_spent": max(0, b["speed"] - a["speed"]),
        "shield_gain": max(0, a["shield"] - b["shield"]),
        "enemy_damage": max(0, b["enemy_hp"] - a["enemy_hp"]),
        "threat_before": b["threat"],
        "threat_after": a["threat"],
        "hp_before": b["hp"],
        "hp_after": a["hp"],
    }


def infer_evidence(facts: dict, memory: dict) -> list[tuple[str, int, str]]:
    """从一手真实结果提取有限、可解释的性格证据。

    这是“观察行为”，不是让模型自由给自己贴标签。每次只返回少量证据，
    最终仍由 engine.personality 的 EMA/置信度规则决定性格强度。
    """
    evidence: list[tuple[str, int, str]] = []
    action = str(facts.get("action", ""))
    label = facts.get("label") or action
    hp = facts.get("hp_after", 0)
    blood_limit = max(1, facts.get("blood_limit", 0) or 1)
    high_pressure = facts.get("threat_before", 0) >= max(1, hp * 0.5)
    is_defense = (facts.get("shield_gain", 0) > 0
                  or "招架" in action or "招架" in label or "庇护" in label)
    is_attack = facts.get("enemy_damage", 0) > 0 or "攻击" in action or "普攻" in label

    if facts.get("hp_loss", 0) >= 3 or facts.get("blood_limit_loss", 0) > 0:
        evidence.append(("risk_preference", +1, f"{label}造成自身损失，仍选择承担风险"))
    elif high_pressure and is_defense and facts.get("hp_loss", 0) == 0:
        evidence.append(("risk_preference", -1, f"高压下以{label}优先保全自身"))

    spent = facts.get("mana_spent", 0)
    mana_limit = max(1, facts.get("mana_before", 0) or 1)
    if spent >= 0.7 * mana_limit:
        evidence.append(("resource_view", -1, f"单次行动{label}消耗大量法力"))
    elif facts.get("mana_before", 0) < 0.3 * max(1, facts.get("mana_limit", 0) or 1):
        evidence.append(("resource_view", +1, f"法力见底时仍保留低费选择"))

    daowen = facts.get("daowen") or ""
    used = memory.setdefault("observed_actions", {})
    if daowen and not used.get(daowen):
        evidence.append(("exploration_desire", +1, f"首次尝试道纹【{daowen}】"))
    elif daowen and used.get(daowen, 0) >= 3:
        evidence.append(("exploration_desire", -1, f"重复依赖道纹【{daowen}】"))

    if high_pressure and is_attack and facts.get("x", 0) in (1, 2):
        evidence.append(("decision_habit", +1, f"高压下先以小档位试探：{label}"))
    elif high_pressure and is_attack and spent >= 0.8 * max(1, facts.get("mana_before", 0)):
        evidence.append(("decision_habit", -1, f"高压下满额推进：{label}"))

    if hp / blood_limit < 0.35:
        if facts.get("hp_loss", 0) >= 3:
            evidence.append(("emotional_stability", -1, f"低血时仍选择冒险行动：{label}"))
        elif is_defense or facts.get("hp_after", 0) > facts.get("hp_before", 0):
            evidence.append(("emotional_stability", +1, f"低血时保持克制并防御：{label}"))

    if facts.get("enemy_damage", 0) > 0:
        evidence.append(("expression_style", +1, f"直接对敌造成伤害：{label}"))
    elif is_defense and facts.get("hp_loss", 0) == 0:
        evidence.append(("expression_style", -1, f"以布局/防守代替直接输出：{label}"))

    previous_threat = memory.get("last_threat", 0)
    if previous_threat and facts.get("threat_before", 0) >= previous_threat * 1.5:
        if is_defense:
            evidence.append(("reaction_pattern", +1, f"威胁骤升后仍然从容防御：{label}"))
        elif facts.get("hp_loss", 0) >= 3:
            evidence.append(("reaction_pattern", -1, f"威胁骤升后应激硬拼：{label}"))
    return evidence


def _lesson_for(facts: dict) -> str | None:
    label = facts.get("label") or facts.get("action") or "这次行动"
    if facts.get("hp_loss", 0) >= 3:
        return f"{label}让我付出了生命代价，硬撑不是免费的。"
    if "招架" in str(facts.get("action", "")) and facts.get("hp_loss", 0) == 0:
        return "招架让我活过了本轮，保留退路也算一种胜利。"
    if facts.get("enemy_damage", 0) > 0 and facts.get("mana_spent", 0) > 0:
        return f"{label}证明主动出手可以改变局面，但法力不能白白浪费。"
    if facts.get("shield_gain", 0) > 0:
        return "我选择先把下一次伤害接住，再等待更好的时机。"
    return None


def remember_action(memory: dict, before: dict, after: dict, result: dict,
                    action_info: dict | None = None, battle: int = 0,
                    round_no: int = 0) -> tuple[dict, list[tuple[str, int, str]]]:
    """写入一手成功行动，返回事实和应交给性格系统的证据。"""
    facts = action_facts(before, after, result, action_info)
    evidence = infer_evidence(facts, memory)
    lesson = _lesson_for(facts)
    episode = {
        "battle": int(battle or 0),
        "round": int(round_no or 0),
        "action": facts["action"],
        "label": facts["label"],
        "outcome": {
            "hp_loss": facts["hp_loss"],
            "enemy_damage": facts["enemy_damage"],
            "mana_spent": facts["mana_spent"],
            "shield_gain": facts["shield_gain"],
        },
        "self_interpretation": lesson or "这次经历暂时没有改变我的判断。",
        "evidence": [{"trait": d, "direction": direction, "reason": reason}
                     for d, direction, reason in evidence],
    }
    memory.setdefault("episodes", []).append(episode)
    memory["episodes"] = memory["episodes"][-MAX_EPISODES:]
    if lesson:
        lessons = memory.setdefault("lessons", [])
        if lesson not in lessons:
            lessons.append(lesson)
        memory["lessons"] = lessons[-MAX_LESSONS:]
    if facts.get("daowen"):
        used = memory.setdefault("observed_actions", {})
        used[facts["daowen"]] = used.get(facts["daowen"], 0) + 1
    memory["last_threat"] = facts.get("threat_after", 0)
    memory["last_reflection_battle"] = int(battle or 0)
    return facts, evidence


def context_for_ai(memory: dict, limit: int = 4) -> dict:
    """只向高层 AI 提供相关摘要，避免把整本经历原样塞入每次提示。"""
    identity = copy.deepcopy(memory.get("identity", {}))
    lessons = list(memory.get("lessons", []))[-limit:]
    episodes = []
    for episode in memory.get("episodes", [])[-limit:]:
        episodes.append({
            "battle": episode.get("battle"),
            "round": episode.get("round"),
            "action": episode.get("label") or episode.get("action"),
            "outcome": episode.get("outcome", {}),
            "interpretation": episode.get("self_interpretation", ""),
        })
    return {
        "identity_is_subjective": True,
        "identity": identity,
        "lessons": lessons,
        "recent_episodes": episodes,
    }


def condense_to_legacy(memory: dict, cause: str = "", last_action: dict | None = None,
                       capacity: int = 20) -> str:
    """把一生压缩为一条不超过 capacity 字的遗言；不携带结构化记忆跨轮回。"""
    identity = memory.get("identity", {}) if isinstance(memory, dict) else {}
    lessons = memory.get("lessons", []) if isinstance(memory, dict) else []
    if lessons:
        text = str(lessons[-1])
    elif identity.get("regret"):
        text = f"别重蹈覆辙：{identity['regret']}"
    elif identity.get("value"):
        text = str(identity["value"])
    else:
        text = "活下去，别把今天的教训留到明天"
    text = text.replace("。", "")
    if len(text) <= capacity:
        return text
    return text[:max(1, capacity - 1)] + "。"


def clear_memory(entity: Any) -> None:
    """清除当前实体全部长期记忆；用于命零/离开轮回。"""
    if entity is not None:
        entity.ai_memory = {}
