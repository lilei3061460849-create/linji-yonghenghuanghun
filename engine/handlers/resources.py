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
                self._grant_custom_resource(resource)
            created.append(f"{resource.category}【{resource.name}】")
        return {"success": True, "action": "声明自定义资源",
                "result": {"created": created,
                           "resources": [r.name for r in custom_resources.all_resources()]}}

    def _grant_custom_resource(self, resource) -> dict:
        """把自定义资源装进当前局：道纹进道纹栏、遗物进遗物栏、消耗品进背包。"""
        spec, category, name = resource.spec, resource.category, resource.name
        player = self.state.player
        if player is None:
            return {"granted": False, "reason": "没有玩家"}
        if category == "道纹":
            if name in player.dao_wen:
                return {"granted": False, "reason": "已持有"}
            player.dao_wen[name] = DaoWenInstance(DaoWen(
                name=name, formula=f"{name}X",
                cost_type=spec["cost"]["type"],
                cost_formula=str(spec["cost"].get("amount", "X")),
                effect_formula=spec.get("summary", "")))
            return {"granted": True, "slot": "dao_wen"}
        if category == "遗物":
            self.state.grant_relic(name, spec.get("summary", ""))
            return {"granted": True, "slot": "relics"}
        if category == "消耗品":
            uses = int(spec.get("uses", 1))
            self.state.consumables.append(Consumable(
                name=name, effect=spec.get("summary", ""),
                current_uses=uses, max_uses=uses, kind="custom"))
            return {"granted": True, "slot": "consumables"}
        self.state.artifacts_owned.append(name)
        return {"granted": True, "slot": "artifacts"}

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
        player = self.state.player
        if spec.get("trigger") is not None:
            return {"success": False, "error": f"【{name}】是触发式资源，不能主动激活"}
        if name not in self.state.artifacts_owned:
            return {"success": False, "error": f"未持有法器【{name}】"}
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

