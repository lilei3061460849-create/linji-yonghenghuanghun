"""遗物与消耗品行动：使用消耗品、启用/切换遗物、发现与放弃发现。

2026-11-03 从 `engine/api.py` **原样搬出**（只搬位置，不改逻辑），作为
`GameEngine` 的 mixin 挂回；`@ACTION_REGISTRY.action(...)` 声明归属，
`_dispatch_action` 查表即命中。
"""
from __future__ import annotations

from ..events import TWISTED_TOOL_LIBRARY
from ..models import Consumable, DaoWen, DaoWenInstance, Entity, Relic

from ..action_registry import ACTION_REGISTRY


class ItemHandlersMixin:
    """宿主类：`GameEngine`。"""

    @ACTION_REGISTRY.action("consume_item", group="items")
    def _action_consume_item(self, params: dict) -> dict:
        """使用消耗品（雕塑/普通/扭曲工具库8件，遵守现有消耗品规则，使用不消耗出手）"""
        item_name = params.get("name", "")
        item = None
        for c in self.state.consumables:
            if c.name == item_name and not c.is_depleted:
                item = c
                break
        if item is None:
            return {"success": False, "error": f"找不到可用消耗品: {item_name}"}

        # 【操纵】：使用者（玩家/朋友/员工）的这一次行动必须等于施法者指令。
        item_user = self.state.player
        caozuo_error = self.combat.caozuo_check(item_user, "使用消耗品", item_name)
        if caozuo_error:
            return caozuo_error

        if item.name == "万钧印":
            return {"success": False, "error": "万钧印在受到攻击时自动触发，不可主动使用"}

        # 【改造】合成的条目：逐条结算被并入的原始效果，最后只扣它自己的1点耐久。
        if item.merged_sources:
            return self._consume_merged_item(item, params)

        return self._consume_item_effect(item, params)

    @ACTION_REGISTRY.action("set_active_relics", group="items")
    def _action_set_active_relics(self, params: dict) -> dict:
        """自定义启用已持有的遗物：页数限制持有，不另限制生效数。

        局外整理用；战斗中不开放——生效集合在[战始]锁定，也可在
        battle_start 时以 active_relics 参数一次提交。
        """
        names = params.get("relic_names", params.get("active_relics"))
        ok, error = self.state.set_active_relics(names)
        if not ok:
            return {"success": False, "error": error}
        player = self.state.player
        return {"success": True, "action": "自定义启用遗物",
                "result": {"enabled": sorted(self.state.enabled_relic_names()),
                           "disabled": sorted({r.name for r in self.state.relics}
                                              - self.state.enabled_relic_names()),
                           "capacity": player.load_capacity if player else 0,
                           "death_pages": player.load_capacity if player else 0}}

    @ACTION_REGISTRY.action("toggle_relic", group="items")
    def _action_toggle_relic(self, params: dict) -> dict:
        """切换单件遗物启用/停用（2026-10-22 用户令）。

        局外行动；与 set_active_relics（整组改选）互补：本行动只翻一件。
        停用不释放页，启用不另限生效数量。
        """
        name = params.get("relic_name", params.get("name"))
        ok, result = self.state.toggle_relic(name)
        if not ok:
            return {"success": False, "error": result}
        player = self.state.player
        return {"success": True, "action": "切换遗物启用",
                "result": {"relic": name, "enabled": result,
                           "enabled_relics": sorted(self.state.enabled_relic_names()),
                           "capacity": player.load_capacity if player else 0}}

    @ACTION_REGISTRY.action("choose_discovered_relic", group="items")
    def _action_choose_discovered_relic(self, params: dict) -> dict:
        choice = params.get("relic_name", "")
        choices = self.state.pending_relic_choices
        if not choices:
            return {"success": False, "error": "当前没有待选择的遗物发现"}
        if choice not in choices:
            return {"success": False, "error": "只能选择本次发现列出的遗物", "choices": list(choices)}
        relic = next((r for r in self.state.relics_pool if r.name == choice), None)
        if relic is None:
            return {"success": False, "error": f"遗物池中不存在【{choice}】"}
        # 2026-10-27：四类资源共用死者之页；替换保留，获得由事务层统一校验。
        player = self.state.player
        replace_name = (params.get("replace") or "").strip()
        replaced = None
        if replace_name:
            replaced = next((r for r in self.state.relics if r.name == replace_name), None)
            if replaced is None:
                return {"success": False,
                        "error": f"未持有【{replace_name}】，无法用它替换",
                        "owned": sorted({r.name for r in self.state.relics})}
        if replaced is not None:
            self.state.relics.remove(replaced)
        self.state.relics_pool.remove(relic)
        self.state.relics.append(relic)
        self.state.note_relic_gained(relic)
        source = self.state.pending_relic_source
        self.state.pending_relic_choices = []
        self.state.pending_relic_source = ""
        next_round: dict = {}
        if source == "交易":
            # 【交易】发现的遗物只在本场战斗内有效：打标，战终统一移除。
            if "本场" not in relic.tags:
                relic.tags.append("本场")
            rounds_left = int(getattr(self.state, "pending_relic_discovery_rounds", 0) or 0)
            if rounds_left > 0:
                self.state.pending_relic_discovery_rounds = rounds_left - 1
                offer = self._offer_relic_discovery("交易")
                if offer.get("success"):
                    next_round = {"choices": offer["choices"],
                                  "remaining": self.state.pending_relic_discovery_rounds}
        result: dict = {
            "success": True,
            "action": "选择发现遗物",
            "result": {"relic": choice, "source": source, "enabled": relic.enabled,
                       **({"replaced": replaced.name} if replaced is not None else {})},
        }
        if next_round:
            result["result"].update(next_round)
            result["note"] = "交易仍在发现中：继续从当前候选里选择。"
        # 开局流程：先发现遗物，再发现初始道纹。
        if (source == "开局发现" and self.state.player is not None
                and not any(n != "透支" for n in self.state.player.dao_wen)
                and not self.state.pending_initial_daowen_choices):
            discovery = self._offer_initial_daowen_discovery("开局发现")
            if not discovery.get("success"):
                return discovery
            result["result"]["daowen_choices"] = discovery["choices"]
            result["next_actions"] = ["setup_choose_initial_daowen"]
            result["note"] = "开局遗物已选择；请从杀伐闭环的3个发现候选中显式选择1种作为初始道纹。"
        return result

    @ACTION_REGISTRY.action("discard_discovered_relic", group="items")
    def _action_discard_discovered_relic(self, params: dict) -> dict:
        """放弃本次遗物发现（2026-10-12 用户裁定③）。

        【承载上限】之后，「必须先选1件」的强制发现可能撞上额度已满：选择被拒、
        其他动作又被门禁拦住，形成无法推进的死局。本动作清空待选列表、不取任何遗物，
        是那条门禁下唯一的脱困出口（另一条是选择时带 replace 换掉一件）。
        """
        choices = list(self.state.pending_relic_choices)
        if not choices:
            return {"success": False, "error": "当前没有待选择的遗物发现"}
        source = self.state.pending_relic_source
        self.state.pending_relic_choices = []
        self.state.pending_relic_source = ""
        next_round: dict = {}
        # 旧档没有该字段：一律按 0 读。
        rounds_left = int(getattr(self.state, "pending_relic_discovery_rounds", 0) or 0)
        if rounds_left > 0:
            # 【交易】放弃本次候选也占一轮：剩余轮数照常推进。
            self.state.pending_relic_discovery_rounds = rounds_left - 1
            offer = self._offer_relic_discovery(source or "交易")
            if offer.get("success"):
                next_round = {"choices": offer["choices"],
                              "remaining": self.state.pending_relic_discovery_rounds}
        return {"success": True, "action": "放弃发现遗物",
                "result": {"discarded_choices": choices, "source": source,
                           "note": "本次发现不取任何遗物，待选列表已清空"}}

    @ACTION_REGISTRY.action("choose_discovered_item", group="items")
    def _action_choose_discovered_item(self, params: dict) -> dict:
        choice = params.get("item_name", "")
        if not self.state.pending_item_choices:
            return {"success": False, "error": "当前没有待选择的消耗品发现"}
        if choice not in self.state.pending_item_choices:
            return {"success": False, "error": "只能选择本次发现列出的消耗品",
                    "choices": list(self.state.pending_item_choices)}
        # 2026-11-02【锻造】实装：候选池扩到全部固定规格消耗品
        # （扭曲都市工具库 + 事件消耗品），两者规格都是既有登记，不接受自定义物品。
        from ..events import EVENT_CONSUMABLES
        spec_pool = {**TWISTED_TOOL_LIBRARY, **EVENT_CONSUMABLES}
        if choice not in spec_pool:
            return {"success": False, "error": f"未知工具: {choice}"}
        durability, effect = spec_pool[choice]
        # 【锻造】发现：耐久按本次 X 覆盖（上限取 max(基准上限, X)），其余发现走原规格。
        forge_uses = int(getattr(self.state, "pending_item_bonus_uses", 0) or 0)
        if forge_uses:
            current_uses = forge_uses
            max_uses = max(int(durability), forge_uses)
        else:
            current_uses = max_uses = int(durability)
        self.state.consumables.append(Consumable(
            name=choice, effect=effect, current_uses=current_uses, max_uses=max_uses,
        ))
        source = self.state.pending_item_source
        self.state.pending_item_choices = []
        self.state.pending_item_source = ""
        self.state.pending_item_bonus_uses = 0
        return {"success": True, "action": "选择发现消耗品",
                "result": {"item": choice, "durability": current_uses,
                           "max_durability": max_uses, "source": source}}

    @ACTION_REGISTRY.action("discard_discovered_item", group="items")
    def _action_discard_discovered_item(self, params: dict) -> dict:
        """放弃本次消耗品发现（与 discard_discovered_relic 对称的出口）。

        死者之页承载不足时「选择」会被事务拒掉，而「必须先选 1 件」的门禁又拦住
        其他一切动作——没有这个出口就是死局。发现不等于必须拿走。
        """
        choices = list(self.state.pending_item_choices)
        if not choices:
            return {"success": False, "error": "当前没有待选择的消耗品发现"}
        source = self.state.pending_item_source
        self.state.pending_item_choices = []
        self.state.pending_item_source = ""
        self.state.pending_item_bonus_uses = 0
        return {"success": True, "action": "放弃发现消耗品",
                "result": {"discarded_choices": choices, "source": source,
                           "note": "本次发现不取任何消耗品，待选列表已清空"}}
