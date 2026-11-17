"""自定义资源行动：声明 / 激活 / 清单（`engine/custom_resources.py` 的 action 层）。

2026-11-03 从 `engine/api.py` 原样搬出（只搬位置，不改逻辑），作为
`GameEngine` 的 mixin 挂回，并用 `@ACTION_REGISTRY.action(...)` 声明归属。
"""
from __future__ import annotations

from ..action_registry import ACTION_REGISTRY
from .. import custom_resources
from ..events import TWISTED_TOOL_LIBRARY
from ..consumables import WING_BLOOD, WING_EMBER
from ..daowen import DaoWenEngine
from ..models import Consumable, DaoWen, DaoWenInstance, Entity, Relic


class ResourceHandlersMixin:
    """自定义资源行动处理器。宿主类：`GameEngine`。"""

    def _builtin_resource_names(self) -> set[str]:
        """内建资源名全集：自定义资源不得与其重名（命名唯一性）。"""
        DaoWenEngine.register_all()
        names = set(DaoWenEngine._registry) | {n for n, _ in self.RELIC_DEFS}
        names |= set(TWISTED_TOOL_LIBRARY)
        for items in self.TERMINAL_ARTIFACTS.values():
            names |= {n for n, _ in items}
        names |= set(self.DRAFT_ARTIFACTS)
        names |= set(self.DRAGON_TRAITS) | set(self.DRAGON_CONSUMABLE_TRAITS)
        names |= {WING_BLOOD, WING_EMBER}
        names |= {name for _, (name, _) in self.FIRST_EMBRACE_OPTIONS.items()}
        return names

    def _resolve_custom_resource_recipient(self, ref: str = ""):
        """解析自定义资源接收者；默认玩家，也允许显式指定当前场上的实体。"""
        player = self.state.player
        if not ref or (player is not None and ref in {"player", player.name, player.runtime_id}):
            return player
        entities = []
        for group in (self.state.get_all_player_side(), self.state.get_all_enemy_side()):
            entities.extend(group)
        unique = {entity.runtime_id: entity for entity in entities if entity is not None}
        if ref in unique:
            return unique[ref]
        matches = [entity for entity in unique.values() if entity.name == ref]
        return matches[0] if len(matches) == 1 else None

    @ACTION_REGISTRY.action("define_resource", group="resources",
                            allowed_phases=("pre_battle", "in_combat"))
    def _action_define_resource(self, params: dict) -> dict:
        """声明一条自定义资源（就地注册，可选就地授予）。

        这是「当场设计」的唯一入口：声明即用，不需要改代码。
        params.spec 或 params.specs 为声明对象；grant=False 时只注册不发放。
        """
        specs = params.get("specs", params.get("spec"))
        if specs is None:
            return {"success": False, "error": "缺少 spec 或 specs"}
        payload = specs if isinstance(specs, list) else [specs]
        recipient = self._resolve_custom_resource_recipient(
            str(params.get("recipient_ref") or "")) if params.get("grant", True) else None
        if params.get("grant", True) and recipient is None:
            return {"success": False,
                    "error": "自定义资源接收者不存在或名称不唯一；请提交 recipient_ref/runtime_id"}
        created: list[str] = []
        for spec in payload:
            try:
                builtin = self._builtin_resource_names()
                name = spec.get("name") if isinstance(spec, dict) else None
                if name in builtin:
                    return {"success": False,
                            "error": f"【{name}】与现有资源重名：命名在全宇宙内唯一"}
                resource = custom_resources.register(spec)
            except custom_resources.CustomResourceError as exc:
                return {"success": False, "error": f"声明被拒绝：{exc}"}
            if params.get("grant", True):
                self._grant_custom_resource(resource, recipient=recipient)
            created.append(f"{resource.category}【{resource.name}】")
        return {"success": True, "action": "声明自定义资源",
                "result": {"created": created,
                           "resources": [r.name for r in custom_resources.all_resources()]}}

    def _grant_custom_resource(self, resource, recipient=None) -> dict:
        """把自定义资源授予指定实体；默认玩家，怪物等实体使用自身持有栏。"""
        spec, category, name = resource.spec, resource.category, resource.name
        recipient = recipient or self.state.player
        if recipient is None:
            return {"granted": False, "reason": "没有合法接收者"}
        if category == "道纹":
            if name in recipient.dao_wen:
                return {"granted": False, "reason": "已持有"}
            recipient.dao_wen[name] = DaoWenInstance(DaoWen(
                name=name, formula=f"{name}X",
                cost_type=spec["cost"]["type"],
                cost_formula=str(spec["cost"].get("amount", "X")),
                effect_formula=spec.get("summary", "")))
            return {"granted": True, "slot": "dao_wen", "recipient": recipient.name}
        if category == "遗物":
            if recipient is self.state.player:
                self.state.grant_relic(name, spec.get("summary", ""), tag="自定义")
            else:
                if any(relic.name == name for relic in recipient.relics):
                    return {"granted": False, "reason": "已持有"}
                recipient.relics.append(Relic(name=name, effect=spec.get("summary", ""),
                                               tags=["自定义"]))
            return {"granted": True, "slot": "relics", "recipient": recipient.name}
        if category == "消耗品":
            uses = int(spec.get("uses", 1))
            item = Consumable(name=name, effect=spec.get("summary", ""),
                              current_uses=uses, max_uses=uses, kind="custom")
            if recipient is self.state.player:
                self.state.consumables.append(item)
            else:
                recipient.consumables.append(item)
            return {"granted": True, "slot": "consumables", "recipient": recipient.name}
        if name not in recipient.artifacts:
            recipient.artifacts.append(name)
        if recipient is self.state.player and name not in self.state.artifacts_owned:
            self.state.artifacts_owned.append(name)
        return {"granted": True, "slot": "artifacts", "recipient": recipient.name}

    @ACTION_REGISTRY.action("activate_resource", group="resources",
                          allowed_phases=("pre_battle", "in_combat"))
    def _action_activate_resource(self, params: dict) -> dict:
        """主动使用一件自定义资源（法器，或无触发时机的自定义消耗品以外的主动项）。

        法器默认不占出手（「不受限其他资源规则」）；声明 costs_action=True 时占 1 次。
        """
        name = params.get("name", "")
        resource = custom_resources.get(name)
        if resource is None:
            return {"success": False, "error": f"没有自定义资源【{name}】"}
        if resource.category == "消耗品":
            return {"success": False, "error": "消耗品请用 consume_item 使用"}
        if resource.category == "道纹":
            return {"success": False, "error": "道纹请用 use_daowen 发动"}
        if resource.category == "遗物":
            return {"success": False, "error": "遗物是被动触发，不能主动激活"}

        spec = resource.spec
        player = self._resolve_custom_resource_recipient(
            str(params.get("owner_ref") or params.get("recipient_ref") or ""))
        if player is None:
            return {"success": False, "error": "法器持有者不存在或名称不唯一"}
        if spec.get("trigger") is not None:
            return {"success": False, "error": f"【{name}】是触发式资源，不能主动激活"}
        if name not in getattr(player, "artifacts", ()) and not (
                player is self.state.player and name in self.state.artifacts_owned):
            return {"success": False, "error": f"【{player.name}】未持有法器【{name}】"}
        if spec.get("costs_action") and self.state.phase == "in_combat":
            budget_error = self._consume_action_or_error(player)
            if budget_error:
                return budget_error
        target = self.combat._combat_entity_refs().get(
            params.get("target_ref", "entity:0"), player)
        logs = custom_resources.apply_effects(
            self.combat, spec["effects"], actor=player, target=target or player,
            x=int(params.get("x", 1)), source=name)
        return {"success": True, "action": f"激活法器【{name}】",
                "result": {"effects": logs},
                "state": self.combat._get_combat_state()}

    @ACTION_REGISTRY.action("list_resources", group="resources",
                          allowed_phases=("pre_battle", "in_combat"))
    def _action_list_resources(self, params: dict) -> dict:
        """列出已注册的自定义资源及其声明摘要。"""
        return {"success": True, "action": "自定义资源清单",
                "result": {"resources": [
                    {"name": r.name, "category": r.category,
                     "trigger": r.spec.get("trigger"),
                     "effects": [e["verb"] for e in r.spec["effects"]]}
                    for r in custom_resources.all_resources()]}}

