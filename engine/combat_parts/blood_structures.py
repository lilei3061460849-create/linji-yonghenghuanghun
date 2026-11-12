"""法力结构（血液结构）：普通攻击构建、层数累积的持续效果（2026-11-10 一期）。

规则事实源：《规则正文·法力结构（血液结构）》。

- 构建入口：`handlers/combat.py` 的 prepare_attack 声明结构名（每击造成≥1点
  伤害时 +1 层，走 `apply_structure_layer`）；
- 【永焰】[回始]结算：`combat.py::process_round_start` 调 `tick_yongyan`；
- 【脆化】伤害加成：`damage_death.py::_apply_hostile_damage_inner` 调
  `consume_cuihua_on_damage`；
- 【震爆】招架引爆 / 【粘滞】闪避加价：`combat.py::resolve_attack` 的
  招架/闪避分支调 `consume_zhenbao_on_parry` / `structure_dodge_extra` +
  `consume_nianzhi_on_dodge`。

结构层是普通 StatusEffect（remaining_rounds=-1、scope=BATTLE）：
同名合并层数（models.add_status::merge_with），[战终]随 BATTLE 作用域清除，
按「持续Y」类参与【流动】/【遗忘】（候选判据只看 is_expired，天然包含）。
"""
from __future__ import annotations

from typing import Optional

from ..enums import EffectPolarity
from ..models import Entity, StatusEffect


# 一期实装的四张结构（名 → 承载点数/材料/效果摘要）。
# 承载＝构建时攻击者当前[血限]的**门槛校验**（2026-11-10 裁定：只校验、不扣减）。
BLOOD_STRUCTURES: dict[str, dict] = {
    "永焰": {"carry": 10, "material": "白磷四面体",
             "desc": "每回合（回始）目标生命-X"},
    "脆化": {"carry": 10, "material": "超低温液氮",
             "desc": "目标下一次受到的伤害+X，触发后清空"},
    "震爆": {"carry": 15, "material": "硝化甘油晶格",
             "desc": "目标下一次宣告招架时，清空层数并受X点伤害"},
    "粘滞": {"carry": 15, "material": "瞬干高分子链",
             "desc": "目标下一次宣告闪避时需额外支付X点速度，触发后清空"},
}
IMPLEMENTED_BLOOD_STRUCTURES = frozenset(BLOOD_STRUCTURES)

# 二期条文（正文已先行，引擎未实装）：致幻/致盲/剧痛/蚀灵/流血/麻痹。
UNIMPLEMENTED_BLOOD_STRUCTURES = ("致幻", "致盲", "剧痛", "蚀灵", "流血", "麻痹")


class BloodStructureMixin:
    """法力结构结算：宿主类 CombatEngine。"""

    # ---------------------------------------------------------------- 查询
    @staticmethod
    def structure_layers(entity: Optional[Entity], name: str) -> int:
        """目标身上某种结构的当前层数（X）。"""
        if entity is None:
            return 0
        return sum(int(s.value) for s in entity.status_effects
                   if s.name == name and not s.is_expired and s.value > 0)

    def structure_dodge_extra(self, target: Entity) -> int:
        """【粘滞】：下一次宣告闪避需额外支付的速度点数。"""
        return self.structure_layers(target, "粘滞")

    def blood_structure_options(self, attacker: Entity) -> list[dict]:
        """按攻击者当前血限给出的可声明结构清单（prepare_attack 用）。"""
        if getattr(attacker, "entity_type", "") != "轮回者":
            return []
        options = []
        for name, meta in BLOOD_STRUCTURES.items():
            options.append({
                "name": name, "carry": meta["carry"],
                "material": meta["material"], "desc": meta["desc"],
                "eligible": int(attacker.blood_limit) >= meta["carry"],
            })
        return options

    def validate_structure_declaration(self, attacker: Entity, name: str) -> Optional[str]:
        """构建声明的门槛校验；返回错误消息或 None。"""
        if not name:
            return None
        if name not in IMPLEMENTED_BLOOD_STRUCTURES:
            return f"未实装的法力结构「{name}」，可选：{sorted(IMPLEMENTED_BLOOD_STRUCTURES)}"
        if getattr(attacker, "entity_type", "") != "轮回者":
            return f"{attacker.name}不是轮回者，不能构建法力结构"
        carry = BLOOD_STRUCTURES[name]["carry"]
        if int(attacker.blood_limit) < carry:
            return f"构建【{name}】需要当前[血限]≥{carry}（{attacker.name}当前{attacker.blood_limit}）"
        return None

    # ---------------------------------------------------------------- 构建
    def apply_structure_layer(self, builder: Entity, target: Entity, name: str) -> dict:
        """命中结算：给目标 +1 层结构；返回层结果（供战报与测试断言）。"""
        if name not in IMPLEMENTED_BLOOD_STRUCTURES:
            raise ValueError(f"未实装的法力结构「{name}」")
        before = self.structure_layers(target, name)
        target.add_status(StatusEffect(
            name=name, remaining_rounds=-1, value=1, source=builder.name,
            polarity=EffectPolarity.DEBUFF.value,
            effect_data={"structure": name, "builder_ref": str(builder.runtime_id)}))
        layers = self.structure_layers(target, name)
        return {"type": "structure_layer", "structure": name,
                "target": target.name, "builder": builder.name,
                "layers": layers, "layers_before": before}

    @staticmethod
    def _structure_status(entity: Entity, name: str) -> Optional[StatusEffect]:
        return next((s for s in entity.status_effects
                     if s.name == name and not s.is_expired and s.value > 0), None)

    def _structure_builder(self, status: StatusEffect, fallback: Optional[Entity]) -> Optional[Entity]:
        ref = str((status.effect_data or {}).get("builder_ref") or "")
        builder = self._combat_entity_refs().get(ref) if ref else None
        if builder is not None and builder.is_alive:
            return builder
        return fallback if (fallback is not None and fallback.is_alive) else None

    # ---------------------------------------------------------------- 触发
    def consume_cuihua_on_damage(self, target: Entity, amount: int) -> int:
        """【脆化】：本次伤害>0 时加 X 点并清空全部层数；返回加成值。

        只加「受到的伤害」：直接失血与代价不走统一伤害入口，天然不触发；
        空刀（amount=0）不触发。调用点在格挡/护盾之前（加成属于伤害本身）。
        """
        if amount <= 0:
            return 0
        status = self._structure_status(target, "脆化")
        if status is None:
            return 0
        bonus = int(status.value)
        target.status_effects.remove(status)
        return bonus

    def consume_nianzhi_on_dodge(self, target: Entity) -> dict:
        """【粘滞】：闪避成功（已支付 1 点速度）后额外支付 X 点速度并清空。"""
        status = self._structure_status(target, "粘滞")
        if status is None:
            return {}
        layers = int(status.value)
        target.status_effects.remove(status)
        before = int(target.current_speed)
        self.pay_resource(
            target, "速度", layers,
            ctx={"timing": self._current_context_timing(), "source": "粘滞",
                 "source_type": "structure", "actor": target, "target": target,
                 "mechanic": "speed_change", "subtype": "current_speed",
                 "amount": -layers, "tags": {"structure", "active_payment"}})
        return {"structure": "粘滞", "extra_speed_paid": min(layers, before),
                "speed_after": int(target.current_speed)}

    def consume_zhenbao_on_parry(self, target: Entity,
                                 attacker: Optional[Entity]) -> dict:
        """【震爆】：目标宣告招架（速度已付、招架成立）时引爆——清空层数并受X点伤害。

        这是结构自身的伤害，不属于被招架的那次攻击（招架的免疫只针对该击）。
        来源记为构建者（状态被【流动】转移后随对象走）；构建者不在场时回退本次
        攻击者。空刀招架同样引爆（宣告即触发）。
        """
        status = self._structure_status(target, "震爆")
        if status is None:
            return {}
        layers = int(status.value)
        target.status_effects.remove(status)
        source = self._structure_builder(status, attacker)
        damage = self._apply_hostile_damage(
            target, layers, "普通", source,
            ctx={"timing": self._current_context_timing(), "source": "震爆",
                 "source_type": "structure", "actor": source, "target": target,
                 "mechanic": "damage", "subtype": "structure",
                 "amount": layers, "tags": {"structure"}})
        return {"structure": "震爆", "layers": layers,
                "damage": damage.get("actual_damage", 0),
                "shield_absorbed": damage.get("shield_absorbed", 0),
                "target_hp_after": damage.get("hp_after", target.current_hp),
                "target_died": damage.get("died", False)}

    def tick_yongyan(self, entity: Entity) -> Optional[dict]:
        """【永焰】：[回始]目标失去 X 点生命（X=层数；直接失血，走统一失血钩子）。"""
        status = self._structure_status(entity, "永焰")
        if status is None:
            return None
        layers = int(status.value)
        source = self._structure_builder(status, None)
        loss = self._raw_hp_loss(entity, layers, ctx={
            "timing": "round_start", "source": "永焰", "source_type": "structure",
            "actor": source, "target": entity, "owner": source,
            "mechanic": "life_loss", "subtype": "structure_tick",
            "amount": layers, "tags": {"structure"}})
        return {"structure": "永焰", "target": entity.name, "layers": layers,
                "lost": loss.get("lost", 0), "hp_after": entity.current_hp,
                "died": loss.get("died", False)}
