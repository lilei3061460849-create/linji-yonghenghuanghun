"""角色性格特征（Personality Traits）系统 —— 2026-08-26

核心原则："先射箭，后画靶"：
- 角色创建时不预设任何人格；
- 性格只能由角色实际发生的行为逐渐推断（每次行为 = 一条带方向与依据的证据）；
- 单次偶然行为不贴死标签（首条证据置信度封顶 0.30，强度减半）；
- 后续行为可持续修正（反向证据会削弱置信度并拉动倾向分）。

数据归属：绑定 Entity.runtime_id（实例级），同名角色不同实例互不共享；
不写入角色模板，不跨副本/轮回/实例自动继承。

生命周期：随 GameState 保存/回滚/存档（纯 dict，天然参与 deepcopy 与 pickle）；
命零时由统一死亡管线 CombatEngine._on_entity_death 调用 remove_personality 清除，
死后任何查询接口（get/format）都读不到。

边界：personality_traits 只是"行为倾向"信息，供 AI 结合局势/目标/情报/资源/情绪
综合参考，绝不是强制规则——"风险偏好：高"不等于每次都必须选危险行动。
"""
from __future__ import annotations

import copy
from typing import Any, Optional, Union

# ---------------------------------------------------------------------------
# 维度定义（只此九维，不扩展标签体系）
# key: {label: 维度名, negative: 负向倾向描述, positive: 正向倾向描述}
# ---------------------------------------------------------------------------
TRAIT_DIMENSIONS: dict[str, dict[str, str]] = {
    "risk_preference":        {"label": "风险偏好",   "negative": "求稳",   "positive": "冒险"},
    "interpersonal_tendency": {"label": "人际倾向",   "negative": "疏离",   "positive": "信任"},
    "moral_baseline":         {"label": "道德底线",   "negative": "利己",   "positive": "守义"},
    "resource_view":          {"label": "资源观",     "negative": "挥霍",   "positive": "节约"},
    "exploration_desire":     {"label": "探索欲",     "negative": "守成",   "positive": "探索"},
    "emotional_stability":    {"label": "情绪稳定性", "negative": "易波动", "positive": "沉稳"},
    "decision_habit":         {"label": "决策习惯",   "negative": "冲动",   "positive": "先观察后行动"},
    "expression_style":       {"label": "表达方式",   "negative": "内敛",   "positive": "直言"},
    "reaction_pattern":       {"label": "反应模式",   "negative": "慌乱",   "positive": "从容"},
}

# 推断参数（刻意简单，不做心理学模型）
FIRST_EVIDENCE_SCORE = 0.35  # 首条证据只落约1/3强度：单次行为给不了强标签
EMA_ALPHA = 0.35             # 后续证据按指数滑动平均向目标靠拢，可被反向行为修正
CONFIDENCE_START = 0.30      # 单条证据的置信度封顶
CONFIDENCE_STEP = 0.13       # 每条同向证据提升的置信度
CONFIDENCE_PENALTY = 0.10    # 反向证据削弱置信度
CONFIDENCE_MAX = 0.95
CONFIDENCE_MIN = 0.20
STRONG_THRESHOLD = 0.60      # |score| ≥ 0.60 才算"明显"，需要多次一致行为才能达到
WEAK_THRESHOLD = 0.20        # |score| ≥ 0.20 才形成方向性描述，否则"尚不定型"

CharacterRef = Union[Any, str]  # Entity 实例 / runtime_id / 角色 name


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _roster(state: Any) -> list:
    """当前仍被 GameState 追踪的全部实体（含已死者，供幂等清理）。"""
    entities = []
    if getattr(state, "player", None) is not None:
        entities.append(state.player)
    for attr in ("friends", "employees", "temp_friends", "enemies"):
        entities.extend(getattr(state, attr, []) or [])
    return entities


def resolve_entity(state: Any, character: CharacterRef):
    """把 Entity/runtime_id/name 解析为仍被追踪且存活的实体；找不到返回 None。

    死亡角色即使对象还在（如 state.player 命零后仍留在状态里）也不可解析——
    性格特征在命零时已被清除，接口层面同样拒绝读取。
    """
    if character is None:
        return None
    if not isinstance(character, str):
        return character if getattr(character, "is_alive", False) else None
    for entity in _roster(state):
        if entity.is_alive and (
            character == entity.runtime_id or character == entity.name
        ):
            return entity
    return None


def _prune_orphaned(state: Any) -> None:
    """清理已不被任何名单跟踪的 runtime_id（如战斗结束清场的造物）。

    只在写入时顺带执行，读接口保持纯函数。键按实例隔离，本就不存在跨实例污染，
    这里只是防止长局运行时字典缓慢增长。
    """
    traits_map = getattr(state, "personality_traits", None)
    if not traits_map:
        return
    live_ids = {entity.runtime_id for entity in _roster(state)}
    for key in [k for k in traits_map if k not in live_ids]:
        del traits_map[key]


def _describe(dimension: str, score: float) -> str:
    """把 [-1, 1] 倾向分映射为简短文字描述（不发明额外标签体系）。"""
    meta = TRAIT_DIMENSIONS[dimension]
    magnitude = abs(score)
    if magnitude < WEAK_THRESHOLD:
        return "尚不定型"
    pole = meta["positive"] if score > 0 else meta["negative"]
    return pole if magnitude >= STRONG_THRESHOLD else f"偏{pole}"


def _entry(dimension: str, score: float, confidence: float,
           evidence_count: int, evidence: str, battle: int) -> dict:
    return {
        "trait": dimension,
        "value": _describe(dimension, score),
        "score": round(score, 4),
        "strength": round(abs(score), 4),
        "confidence": round(confidence, 4),
        "evidence_count": evidence_count,
        "last_evidence": evidence,
        "last_updated_battle": battle,
    }


# ---------------------------------------------------------------------------
# 对外接口（核心层：直接操作 GameState，供 GameEngine 与死亡管线调用）
# ---------------------------------------------------------------------------
def record_behavior(state: Any, entity: Any, dimension: str, direction: float,
                    evidence: str, weight: float = 1.0, battle: Optional[int] = None
                    ) -> dict:
    """记录一条行为证据并更新该维度的性格推断。

    direction: +1 指向 positive 一极，-1 指向 negative 一极；
    weight: 该条证据的强度（0, 1]，默认 1.0；
    evidence: 形成该判断的行为依据（自然语言一句话，进 last_evidence）。
    返回更新后的维度条目；实体不存活/维度非法时抛 ValueError。
    """
    if dimension not in TRAIT_DIMENSIONS:
        raise ValueError(f"未知性格维度: {dimension}（可用: {sorted(TRAIT_DIMENSIONS)}）")
    if direction not in (1, -1, 1.0, -1.0):
        raise ValueError("direction 必须是 +1 或 -1")
    if not 0 < weight <= 1:
        raise ValueError("weight 必须在 (0, 1] 区间")
    if not evidence or not str(evidence).strip():
        raise ValueError("必须提供形成判断的行为依据 evidence")
    if not getattr(entity, "is_alive", False):
        raise ValueError("只有存活角色才能累积性格证据")

    _prune_orphaned(state)
    if battle is None:
        battle = int(getattr(state, "current_battle", 0) or 0)

    traits_map = getattr(state, "personality_traits", None)
    if traits_map is None:  # 兼容无该字段的旧状态对象
        traits_map = {}
        state.personality_traits = traits_map
    per_char = traits_map.setdefault(entity.runtime_id, {"name": entity.name, "traits": {}})
    per_char["name"] = entity.name  # 同名不同实例各占各的键，name 仅作展示

    current = per_char["traits"].get(dimension)
    if current is None:
        # 首条证据：强度减半、置信度封顶——单次行为绝不贴死标签
        score = direction * weight * FIRST_EVIDENCE_SCORE
        confidence = CONFIDENCE_START
        count = 1
    else:
        target = direction * weight
        score = current["score"] + EMA_ALPHA * (target - current["score"])
        consistent = (current["score"] == 0
                      or (target > 0) == (current["score"] > 0))
        if consistent:
            confidence = min(CONFIDENCE_MAX, current["confidence"] + CONFIDENCE_STEP)
        else:
            confidence = max(CONFIDENCE_MIN, current["confidence"] - CONFIDENCE_PENALTY)
        count = current["evidence_count"] + 1

    entry = _entry(dimension, score, confidence, count, str(evidence), battle)
    per_char["traits"][dimension] = entry
    return copy.deepcopy(entry)


def get_personality(state: Any, character: CharacterRef) -> Optional[dict]:
    """读取存活角色的性格条目 {runtime_id, name, traits}。

    存活但尚无任何记录 → traits 为空 dict（"未预设人格"是合法状态）；
    死亡/未知角色 → None（死亡角色的性格一律不可读）。
    """
    entity = resolve_entity(state, character)
    if entity is None:
        return None
    per_char = getattr(state, "personality_traits", {}).get(entity.runtime_id)
    if per_char is None:
        return {"runtime_id": entity.runtime_id, "name": entity.name, "traits": {}}
    return {"runtime_id": entity.runtime_id, "name": per_char.get("name", entity.name),
            "traits": copy.deepcopy(per_char.get("traits", {}))}


def remove_personality(state: Any, character: CharacterRef) -> bool:
    """清除角色性格数据（幂等）。死亡管线与手工移除共用此入口。返回是否实际删除。"""
    traits_map = getattr(state, "personality_traits", None)
    if not traits_map:
        return False
    key = None
    if not isinstance(character, str):
        key = getattr(character, "runtime_id", None)
    else:
        if character in traits_map:          # runtime_id 直捣
            key = character
        else:                                 # 按 name 兜底（同一时刻同名存活者应唯一）
            for rid, per_char in traits_map.items():
                if per_char.get("name") == character:
                    key = rid
                    break
    if key is not None and key in traits_map:
        del traits_map[key]
        return True
    return False


def format_personality_for_ai(state: Any, character: CharacterRef) -> Optional[str]:
    """渲染供 AI 使用的人格摘要（只列已形成的维度，含置信度与依据次数）。

    附带"倾向非强制"提示；其余维度明确标注证据不足，防止 AI 自行脑补。
    """
    personality = get_personality(state, character)
    if personality is None:
        return None
    lines = [f"角色：{personality['name']}", "性格特征（由实际行为推断，仅供参考，不是强制规则）："]
    formed = []
    for key, meta in TRAIT_DIMENSIONS.items():
        entry = personality["traits"].get(key)
        if entry is None:
            continue
        mark = "（初步，证据尚少）" if entry["evidence_count"] <= 1 else ""
        formed.append(key)
        lines.append(f"- {meta['label']}：{entry['value']}{mark}"
                     f"（置信度 {entry['confidence']:.2f}，依据 {entry['evidence_count']} 次；"
                     f"最近依据：{entry['last_evidence']}）")
    if not formed:
        lines.append("（尚无维度形成判断——未观察到足够行为）")
    else:
        rest = [meta["label"] for key, meta in TRAIT_DIMENSIONS.items() if key not in formed]
        if rest:
            lines.append(f"（其余维度证据不足，未形成判断：{'、'.join(rest)}）")
    return "\n".join(lines)


def export_for_ai(state: Any) -> dict:
    """GameState.to_dict() 用的结构化导出：{runtime_id: {name, entity_type, traits}}。

    天然只含仍被追踪的存活角色——死者条目已在命零时删除；
    万一残留孤儿键（清场离场的存活者）也因不在名单中而被跳过。
    """
    result: dict = {}
    traits_map = getattr(state, "personality_traits", {}) or {}
    for entity in _roster(state):
        per_char = traits_map.get(entity.runtime_id)
        if per_char is None or not entity.is_alive:
            continue
        result[entity.runtime_id] = {
            "name": per_char.get("name", entity.name),
            "entity_type": entity.entity_type,
            "traits": copy.deepcopy(per_char.get("traits", {})),
        }
    return result
