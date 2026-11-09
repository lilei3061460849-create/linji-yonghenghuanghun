"""AI 可见行动清单（`get_available_actions` 及其分组构建器）。

2026-11-03 从 `engine/api.py` **原样搬出**（只搬位置，不改逻辑），作为
`GameEngine` 的 mixin 挂回。这一组方法只读状态、拼行动清单，不写任何状态，
放在 api.py 里占了近千行，把主入口淹了。

与 `engine/handlers/*.py` 的区别：这里不注册 action（清单里的 action 由
各自的处理器模块注册），只负责"现在有哪些行动可选、参数范围是什么"。
"""
from __future__ import annotations

from typing import Any, Optional

from .. import custom_resources
from ..action_registry import ACTION_REGISTRY
from ..consumables import WING_MODES
from ..daowen import DaoWenEngine, PURIFIABLE_COST_KINDS, purifiable_cost_layers
from ..enums import CombatSubphase, GamePhase, RESOURCE_TYPES, TriggerTiming
from ..models import Consumable, Entity
from ..world import ALLY_SUGGESTIONS, WorldState
from ..yaoguai import has_banner as has_yaoguai_banner, owned_yaoguai


class AvailableActionsMixin:
    """行动清单构建器。宿主类：`GameEngine`。"""

    def get_available_actions(self) -> dict:
        """
        获取当前可用行动
        根据游戏阶段返回不同的行动列表
        """
        if self.state.pending_initial_daowen_choices:
            return {
                "phase": "初始道纹发现待选",
                "actions": [{"action_type": "setup_choose_initial_daowen",
                             "params_schema": {
                                 "daowen_name": list(self.state.pending_initial_daowen_choices)}}],
                "source": self.state.pending_initial_daowen_source,
            }
        if self.state.pending_redemption:
            return {
                "phase": "救赎待选",
                "actions": [{"action_type": "resolve_redemption",
                             "params_schema": {
                                 "option": [1, 2, "接纳", "终结"],
                                 "name": "接纳时必填且不得与场上重名"}}],
                "pending": dict(self.state.pending_redemption),
            }
        if self.state.pending_relic_choices:
            return {
                "phase": "遗物发现待选",
                "actions": [{"type": "choose_discovered_relic",
                             "relic_name": name} for name in self.state.pending_relic_choices],
                "source": self.state.pending_relic_source,
            }
        if getattr(self.state, "pending_derivative_choices", []):
            return {
                "phase": "衍生发现待选",
                "actions": [
                    {"action_type": "choose_derivative_daowen",
                     "params_schema": {"daowen_name": list(self.state.pending_derivative_choices),
                                       "abandon": "True＝放弃本次结果（已付代价不退）"}},
                    {"action_type": "refresh_derivative_choices",
                     "params_schema": {"cost": "血限2（重新随机列出未持有选项）"}},
                ],
                "parent": getattr(self.state, "pending_derivative_parent", ""),
                "choices": list(self.state.pending_derivative_choices),
            }
        if self.state.pending_item_choices:
            return {
                "phase": "消耗品发现待选",
                "actions": [{"type": "choose_discovered_item",
                             "item_name": name} for name in self.state.pending_item_choices],
                "source": self.state.pending_item_source,
            }
        if self.state.pending_daowen_choices:
            return {
                "phase": "员工转化道纹待选",
                "actions": [{"action_type": "choose_hired_daowen",
                             "params_schema": {"name": employee_name, "daowen": choices}}
                            for employee_name, choices in self.state.pending_daowen_choices.items()],
            }
        if self.event_pool.current is not None:
            event = self.event_pool.events[self.event_pool.current]
            has_wusuoqiu = any(r.name == "无所求" and getattr(r, "enabled", True) for r in self.state.relics)
            actions = []
            for option in event["options"]:
                schema: dict = {"event": self.event_pool.current, "option_id": option["id"]}
                if has_wusuoqiu and self._is_reject_option_text(option["text"]):
                    schema["note"] = "持【无所求】：本选项另获1点属性点（入属性点池，按正文2点一档兑速限/法限、1点一档兑血限）"
                if self.event_pool.current == "祭坛" and option["text"].startswith("投资"):
                    schema["x"] = "投入的正整数本金，不超过当前真实碎片余额"
                    schema["multiplier"] = "自选正整数倍率，范围1至(10+本金//100)；每100本金提高1点上限"
                    schema["note"] = "随机整数直接决定盈亏倍数，范围为-倍率至+倍率；本金100、倍率3、结果-2即净亏200，结果0不赚不亏。亏损可形成负碎片余额，并沿用动态债务线"
                if self.event_pool.current == "过路商人" and option["text"].startswith("遗物交易"):
                    self._init_relic_pool()
                    merchant = getattr(self.state, "merchant", None)
                    merchant_relics = [r.name for r in merchant.relics] if merchant else []
                    schema.update({"trade_direction": ["buy", "sell"],
                                   "relic_name": f"字符串；buy可选：{merchant_relics}；sell可选：{[r.name for r in self.state.relics]}",
                                   "price": "双方协商后的非负整数碎片价格",
                                   "confirmed": True})
                    schema["note"] = "同次遭遇可连续买卖多件遗物；成交前确认价格。选择其他选项结束交易会话。买卖立即转移遗物与碎片，不得复制遗物或通过交易透支"
                actions.append({"action_type": "resolve_event",
                                "params_schema": schema,
                                "option": option["text"],
                                "parameter_note": "按选项正文中的X/目标/分配要求补充真实参数"})
            return {
                "phase": "事件待结算",
                "event": self.event_pool.current,
                "desc": event["desc"],
                "actions": actions,
                "queued_events_remaining": len(self.state.pending_event_queue),
            }
        if self.state.pending_attack:
            pending = self.state.pending_attack
            return {
                "phase": "攻击待提交",
                "action_type": "resolve_attack",
                "params_schema": {"token": pending["token"], "hits": pending["options"]["hits_schema"]},
                **pending["options"],
            }
        if self.state.pending_monster_phase:
            pending = self.state.pending_monster_phase
            return {
                "phase": "怪物阶段待提交",
                "action_type": "resolve_monster_phase",
                "params_schema": {"token": pending["token"], "choices": "为全部actors各提交一次"},
                **pending["options"],
            }

        phase = self.state.phase

        if phase == "setup":
            return self._get_setup_actions()
        elif phase == "pre_battle":
            return self._get_pre_battle_actions()
        elif phase == "battle_start":
            return self._get_battle_start_actions()
        elif phase == "in_combat":
            available = self._get_combat_actions()
            player = self.state.player
            if player and player.has_status("束缚"):
                subphase = self.state.combat_subphase
                allowed_by_subphase = {
                    CombatSubphase.PLAYER_ACTIONS.value: {
                        "cast", "define_spell", "use_daowen",
                        "prepare_monster_phase"},
                    CombatSubphase.AWAIT_ROUND_START.value: {"round_start"},
                }
                allowed = allowed_by_subphase.get(subphase)
                if allowed is not None:
                    available["actions"] = [
                        action for action in available.get("actions", [])
                        if action.get("action_type") in allowed
                        and (action.get("action_type") not in {"cast", "define_spell"}
                             or action.get("available", True))
                    ]
                    available["binding_restriction"] = (
                        "【束缚】持续期间仅能施法：施放法术cast/define_spell，"
                        "或直接发动道纹use_daowen（法术由道纹构成，发动单个道纹同属施法）；"
                        "prepare_monster_phase/round_start只用于推进战斗流程。"
                    )
                    existing_note = available.get("note", "")
                    available["note"] = "；".join(
                        part for part in (existing_note, available["binding_restriction"]) if part)
            if player and self.combat.chenni_round_blocked(player):
                allowed_by_subphase = {
                    CombatSubphase.PLAYER_ACTIONS.value: {
                        "resolve_ally_phases", "prepare_monster_phase"},
                    CombatSubphase.AWAIT_ROUND_START.value: {"round_start"},
                    CombatSubphase.AWAIT_ROUND_END.value: {"round_end"},
                }
                allowed = allowed_by_subphase.get(self.state.combat_subphase, set())
                available["actions"] = [
                    action for action in available.get("actions", [])
                    if action.get("action_type") in allowed
                ]
                available["chenni_restriction"] = (
                    "【沉溺】已中断你的本次回合；只能推进其他角色的自主阶段与战斗流程。")
                available["note"] = "；".join(part for part in (
                    available.get("note", ""), available["chenni_restriction"]) if part)
            return available
        elif phase == "battle_end":
            return self._get_battle_end_actions()
        elif phase == "dead_duel":
            return self._get_dead_duel_actions()
        else:
            return {"actions": [], "note": "游戏已结束"}

    def _get_setup_actions(self) -> dict:
        actions = []
        if self.state.player is None:
            actions.append({"action_type": "setup_attributes", "params_schema": {
                "name": "string", "blood_points": "integer", "speed_points": "integer",
                "mana_points": "integer", "constraint": "sum=25"}})
        else:
            if self.state.pending_initial_daowen_choices or not self.state.player.dao_wen:
                actions.append({"action_type": "setup_choose_initial_daowen",
                                "params_schema": {
                                    "daowen_name": list(self.state.pending_initial_daowen_choices)}})
                return {"phase": GamePhase.SETUP.value, "actions": actions}
            if not self.state.resonance:
                actions.append({"action_type": "setup_choose_resonance",
                                "params_schema": {"resonance_type": ["转换", "反转", "曲解"]}})
            if not self.state.current_region:
                # 可选副本=已实现且阶级已解锁；无尽模式不设限制
                actions.append({"action_type": "setup_choose_region",
                                "params_schema": {"region": self.unlocked_regions()}})
        return {"phase": GamePhase.SETUP.value, "actions": actions}

    def _get_pre_battle_actions(self) -> dict:
        heal_targets = ([{"ref": "player:0", "name": self.state.player.name}]
                        if self.state.player and self.state.player.is_alive else [])
        heal_targets += [{"ref": f"friend:{index}", "name": entity.name}
                         for index, entity in enumerate(self.state.friends) if entity.is_alive]
        heal_targets += [{"ref": f"employee:{index}", "name": entity.name}
                         for index, entity in enumerate(self.state.employees) if entity.is_alive]
        actions = [
            {"action_type": "pre_battle_action", "params_schema": {
                "sub_action": "休整", "tier": [1, 2, 3],
                "heal_allocations": {"target_options": heal_targets,
                                     "constraint": "amount总和=档位基础恢复量"}}},
            {"action_type": "pre_battle_action", "params_schema": {
                "sub_action": "修行", "tier": [1, 2, 3, 4, 5, 6],
                "allocations": {"blood_points": "nonnegative integer",
                                "speed_points": "nonnegative integer",
                                "mana_points": "nonnegative integer",
                                "constraint": "三者之和≤档位属性点+池内余点；1点=6血限，2点=1速限=1法限"},
                "target": "player（默认）或已存在的朋友/员工/妖怪名称"}},
            {"action_type": "pre_battle_action", "params_schema": {
                "sub_action": "学习", "sub": ["daowen"],
                "tier": {"daowen": [1, 2]},
                "names": "daowen必填，数量必须等于tier",
                "recipient": "player（默认）、现存朋友/员工/妖怪名称，或pending暂存给未来朋友",
                "note": "学习所得可分配给其他角色或暂存；法术已于2026-09-16改为无需学习：持所需道纹即可在战斗中用 "
                        "统一自定义施法（define_spell，消耗1次主动出手）"}},
            {"action_type": "derive_daowen", "params_schema": {
                "source_daowen": "自己持有的母本道纹（杀伐／透支／再生）",
                "recipient": "player（默认）或现存朋友/员工/妖怪名称",
                "cost": "【转换】×1 + 血限2 + 灵魂碎片3",
                "note": "从该母本的衍生道纹池发现至多3个未持有选项；每个衍生道纹独立占1张死者之页"}},
            {"action_type": "pre_battle_action", "params_schema": {
                "sub_action": "共鸣",
                "tier": {1: "随机列3件候选并显式选择1件", 2: "额外1精力+15碎片自选"},
                "name": "2档必填，当前遗物池名称"}},
            {"action_type": "pre_battle_action", "params_schema": {
                "sub_action": "探索", "tier": {1: "发现1个未遇事件", 2: "30碎片发现2个未遇事件"}}},
            {"action_type": "set_active_relics", "params_schema": {
                "relic_names": "生效遗物名列表（自定义启用）：持有占死者之页，生效数不另限；"
                               "未列名遗物仍持有、只是不生效；也可改在 battle_start 时用 active_relics 提交"},
             "note": (f"当前生效：{sorted(self.state.enabled_relic_names())}；"
                      f"死者之页：{self.state.player.pages_used}/{self.state.player.load_capacity}张"
                      if self.state.player else "当前无玩家")},
            {"action_type": "toggle_relic", "params_schema": {
                "relic_name": "要切换启用/停用的单件遗物名；停用不释放死者之页，启用不另限数量"},
             "note": "与 set_active_relics 互补：本行动只翻一件的启用状态"},
        ]
        for region_action in self._get_region_actions():
            actions.append({
                "action_type": "pre_battle_action",
                "params_schema": {"sub_action": region_action["id"],
                                  **region_action["params_schema"]},
            })
        pending_learned = list(getattr(self.state, "pending_learned_daowen", []))
        if pending_learned:
            actions.append({"action_type": "assign_learned_daowen",
                            "params_schema": {"name": pending_learned,
                                              "recipient": "现存轮回者/朋友/员工/妖怪名称或runtime_id"},
                            "note": "学习成果可以暂存，待未来结识朋友后再分配"})
        if (has_yaoguai_banner(self.state)
                and not any(action.get("params_schema", {}).get("sub_action") == "造生"
                            for action in actions)):
            actions.append({"action_type": "pre_battle_action",
                            "params_schema": {
                                "sub_action": "造生",
                                "name": "可选：新妖怪名称；省略时自动生成唯一名称"},
                            "note": "持有煌妖幡可在乱葬岗外创造妖怪；幡内最多6只。"
                                    "造生不提供培养，成长收益通过修行、学习等通用行动分配"})
        dormant_targets = [{"ref": entity.runtime_id, "name": entity.name,
                            "current_hp": entity.current_hp, "blood_limit": entity.blood_limit}
                           for entity in owned_yaoguai(self.state)
                           if getattr(entity, "is_dormant", False)]
        if dormant_targets:
            actions.append({"action_type": "pre_battle_action",
                            "params_schema": {"sub_action": "唤醒",
                                              "target": dormant_targets},
                            "note": "主动唤醒一只当前生命大于0的休眠妖怪；恢复本身不会自动唤醒"})
        if any(entity.name == "医生" and entity.is_alive for entity in self.state.employees):
            actions.append({"action_type": "upgrade_doctor",
                            "params_schema": {"mode": ["attack_count", "attack_power"]},
                            "cost": "5碎片，不消耗精力"})
        # 2026-09-28 用户令：删除局外【忘忧】行动；忘忧香效果为溢出回复转格挡。
        player = self.state.player
        blood_pact_cost_targets = ([
            {"ref": ref, "name": entity.name}
            for ref, entity in self.combat.blood_pact_targets().items()
        ] if player and self.combat._has_active_blood_pact(player) else [])
        if self.state.has_sacrifice_action:
            actions.append({"action_type": "pre_battle_action",
                            "params_schema": {"sub_action": "献祭", "tier": [1, 2, 3],
                                              "cost_share_target_ref": blood_pact_cost_targets}})
        if self.state.pending_diandang_discoveries:
            actions.append({"action_type": "choose_diandang_daowen",
                            "params_schema": {"daowen_name": list(self.state.pending_diandang_discoveries[0])},
                            "remaining_groups": len(self.state.pending_diandang_discoveries),
                            "note": "领取【典当】发现：从当前组3选1里选1种获得，不消耗出手/精力"})
        if "真龙之心" in self.state.artifacts_owned:
            # 2026-10-23 用户令：龙性货币废除。数值型代价自动入账，
            # 满 12 点可直接换 1 件龙族遗物；不再有「换取龙性」这一步。
            actions.append(
                {"action_type": "unlock_dragon_trait",
                 "params_schema": {"trait": [name for name in
                                                self.DRAGON_TRAITS + list(self.DRAGON_CONSUMABLE_TRAITS)
                                                if name not in self.state.dragon_traits
                                                and not any(c.name == name for c in self.state.consumables)]},
                 "dragon_cost_points": self.state.dragon_cost_points,
                 "note": "每累计支付12点数值型代价可换1件；不足12点时会被拒绝"})
        for employee in self.state.employees:
            if employee.is_debt_bound and employee.shards < 0:
                actions.append({"action_type": "repay_debt_employee",
                                "params_schema": {"name": employee.name, "amount": -employee.shards}})
        for employee_name, choices in self.state.pending_daowen_choices.items():
            actions.append({"action_type": "choose_hired_daowen",
                            "params_schema": {"name": employee_name, "daowen": choices}})
        if self.state.pending_terminal_region:
            actions.append({"action_type": "choose_terminal_artifact",
                            "params_schema": {"choice": "listed terminal artifact index"}})
        if self.state.pending_first_embrace:
            actions.append({"action_type": "choose_first_embrace",
                            "params_schema": {"choice": "1..9"}})
        if self.state.energy <= 0:
            battle_start_schema: dict[str, Any] = {
                "relic_choices": "逐件显式提交可选战始遗物",
                "active_relics": "可选：本场生效的遗物名列表（自定义启用，不另限生效数量）；不提交则沿用当前启用集合"}
            global_spells_start = self.combat.prepare_global_trigger_spells(TriggerTiming.BATTLE_START.value)
            if global_spells_start:
                battle_start_schema["spell_choices"] = {
                    "_instruction": "存在【战始】全局法术候选，须逐一显式提交use/steps（循环法术可另附 max_iterations）"
                                    "（实际结算发生在战始遗物与法力重置之后，届时可能因法力变化而改变可承担的X）",
                    "candidates": global_spells_start,
                }
            actions.append({"action_type": "battle_start", "params_schema": battle_start_schema,
                            "note": "旧线性副本入口；默认应先执行列表首项world_initialize，"
                                    "仅在用户明确要求线性流程时使用"})
        if self.state.endless_mode:
            # 无尽模式「无法探索」：不出现在可用行动里，
            # 保证 get_available_actions 的输出仍可原样执行（开发规则 13）。
            actions = [a for a in actions
                       if a.get("params_schema", {}).get("sub_action") != "探索"]
        if getattr(self.state, "world_state", {}):
            # 开放世界中不再提供独立「探索」菜单；事件由地图地点触发，旧附煞已由造生取代。
            actions = [action for action in actions
                       if action.get("params_schema", {}).get("sub_action") not in {"探索", "附煞"}]
        if not getattr(self.state, "world_state", {}):
            actions.insert(0, {"action_type": "world_initialize",
                               "params_schema": {"width": "integer (default 21)", "height": "integer (default 21)"},
                               "note": "默认副本入口：初始化开放世界地图（21×21）；地图种子由引擎骰子产生并记录。"
                                       "只有用户明确要求旧线性流程时才跳过此项，继续旧线性局外行动并在精力耗尽后使用battle_start。"})
        else:
            if player is not None and "唤潮" in player.dao_wen:
                actions.append({"action_type": "use_daowen", "available": True,
                                "params_schema": {"daowen_name": "唤潮", "x": "1..8",
                                                  "terrain_cells": "恰好X个周围坐标，每项{x,y}或[x,y]"},
                                "note": "仅在开放地图发动；支付X法力，将自身周围X个可用格改为水流地形，只有鲛族可通行"})
            if player is not None and "蜃景" in player.dao_wen:
                actions.append({"action_type": "use_daowen", "available": True,
                                "params_schema": {"daowen_name": "蜃景",
                                                  "x": "1..8；幻象自动在施术者当前格生成"},
                                "note": "仅在开放地图发动；支付10X法力，在自身所在格创造X个外观相同的地图幻象；幻象自主分流，被其他角色接触或施术者死亡时消散"})
            actions.extend([
                {"action_type": "world_status", "params_schema": {}},
                {"action_type": "world_vision", "params_schema": {"actor_id": "optional; defaults to player"}},
                {"action_type": "world_move", "params_schema": {"x": "integer", "y": "integer",
                                                                  "actor_id": "optional; defaults to player"}},
                {"action_type": "world_wait", "params_schema": {"turns": "positive integer (default 1)",
                                                                  "actor_id": "optional; defaults to player"}},
                {"action_type": "world_command_created_being", "params_schema": {
                    "behavior": ["explore", "follow", "guard"],
                    "target": "可选：妖怪名称或runtime_id；省略时保持原默认第一只存活妖怪",
                    "x": "guard模式可选整数坐标", "y": "guard模式可选整数坐标"}},
                {"action_type": "world_suggest_ally", "params_schema": {
                    "target_actor_id": "相邻/近处友方单位的 actor_id（来自 world_status.allies）",
                    "suggestion": sorted(ALLY_SUGGESTIONS),
                    "x": "suggestion=guard 时可选：守卫坐标x",
                    "y": "suggestion=guard 时可选：守卫坐标y"},
                 "note": "只能建议，不能命令：朋友/员工/造物会按自己的处境决定是否照做"},
                {"action_type": "world_interact", "params_schema": {
                    "target_actor_id": "actor_id from visible_actors",
                    "choice": ["talk", "trade", "cooperate", "intimidate", "attack"],
                    "offer_shards": "trade时可选：你支付的碎片",
                    "request_shards": "trade时可选：你索取的碎片",
                    "give_relic": "trade时可选：你交出的遗物名称",
                    "request_relic": "trade时可选：你索取的遗物名称",
                    "trade_rule": "NPC按每件遗物10碎片估值；只接受对其价值不亏的报价"}},
            ])
        if getattr(self.state, "world_state", {}):
            world = WorldState.from_dict(self.state.world_state)
            player_id = next((key for key, actor in world.actors.items()
                              if actor.kind == "player" and actor.alive), None)
            encounter = next((item for item in world.encounters.values()
                              if item.active and player_id in item.participants), None)
            if encounter is not None:
                schema: dict[str, Any] = {
                    "world_encounter_id": encounter.encounter_id,
                    "relic_choices": "逐件显式提交可选战始遗物",
                    "summon_created_being": "可选布尔值；true时召唤五格可通行路径内的妖怪助战，默认false",
                    "summon_created_being_target": "可选：要召唤的妖怪名称或runtime_id；省略保持原默认",
                }
                global_spells = self.combat.prepare_global_trigger_spells(TriggerTiming.BATTLE_START.value)
                if global_spells:
                    schema["spell_choices"] = {
                        "_instruction": "存在战始全局法术候选，按现有战斗协议提交",
                        "candidates": global_spells,
                    }
                actions = [{"action_type": "battle_start", "params_schema": schema,
                            "note": "地图遭遇已发生；使用遭遇参与者进入现有战斗引擎"}]
            else:
                # 开放世界不允许脱离地图遭遇随机开启旧式战斗。
                actions = [action for action in actions
                           if action.get("action_type") != "battle_start"]
        return {"phase": GamePhase.PRE_BATTLE.value, "energy": self.state.energy, "actions": actions}

    def _get_region_actions(self) -> list[dict]:
        if self.state.current_region == "扭曲都市":
            mergeable = []
            for index, item in enumerate(self.state.consumables):
                mergeable.append({
                    "item_ref": f"consumable:{index}", "name": item.name,
                    "effect": item.effect, "current_uses": item.current_uses,
                    "max_uses": item.max_uses,
                    "passive_sources": [source["name"] for source in item.effect_sources()
                                        if Consumable.source_is_passive(source)],
                    "sources": [source["name"] for source in item.effect_sources()]})
            return [{"id": "改造", "params_schema": {
                "tier": {2: "合并2件不同名消耗品（0碎片）",
                         3: "合并3件不同名消耗品（5碎片）",
                         4: "合并4件不同名消耗品（12碎片）"},
                "names": mergeable,
                "target": "接收全部效果的消耗品名，必须是names之一",
                "constraint": ("names数量必须等于tier且互不同名；"
                               "被并入的条目从持有中移除，其当前耐久与耐久上限一并叠加到目标条目；"
                               "效果叠加为目标条目的效果文本；使用目标条目时逐条结算全部来源效果。"
                               "被动型效果（龙心、万钧印）随条目携带、不参与手动结算；"
                               "翼类／雕塑等需要选择的效果用source_params按来源名分别提交"),
            }}]
        if self.state.current_region == "罪孽都市":
            return [{"id": "雇佣", "params_schema": {"name": "string", "blood_alloc": "integer",
                                                       "atk_bundles": "integer"}}]
        if self.state.current_region == "龙心谷":
            return [{"id": "炼心", "params_schema": {}}]
        if self.state.current_region == "乱葬岗":
            return [{"id": "造生", "params_schema": {
                "name": "可选：新妖怪名称；须与其他角色不同，省略时自动生成唯一名称"},
                "note": "仅创造：消耗10碎片并获得固定专属道纹、初始偏爱；不提供培养，"
                        "成长收益通过修行、学习等通用行动分配。未入幡妖怪每只占5张死者之页"}]
        return []

    def _get_battle_start_actions(self) -> dict:
        relic_choices_schema: dict[str, Any] = {"_instruction": "按当前回始遗物逐件显式提交"}
        player = self.state.player
        round_start_schema: dict[str, Any] = {"relic_choices": relic_choices_schema}
        global_spells = self.combat.prepare_global_trigger_spells(TriggerTiming.ROUND_START.value)
        if global_spells:
            round_start_schema["spell_choices"] = {
                "_instruction": "存在【回始】全局法术候选，须逐一显式提交use/steps（循环法术可另附 max_iterations）",
                "candidates": global_spells,
            }
        actions = [{"action_type": "round_start", "params_schema": round_start_schema}]
        if "黑金名片" in self.state.artifacts_owned:
            actions.insert(0, {"action_type": "use_black_card", "params_schema": {}})
        if "共心环" in self.state.artifacts_owned:
            actions.insert(0, {"action_type": "select_shared_dragon_heart",
                               "params_schema": {"dragon_heart_type": "owned cost type"}})
        if "罪业金库" in self.state.artifacts_owned:
            actions.insert(0, {"action_type": "use_crime_vault", "params_schema": {"x": "integer"}})
        if self.state.in_final_duel:
            actions.insert(0, {"action_type": "activate_duel_relic",
                               "params_schema": {"side": ["player_side", "opponent_side"],
                                                 "relic": "持有者的可选战始遗物参数"}})
        return {"phase": CombatSubphase.AWAIT_ROUND_START.value,
                "current_battle": self.state.current_battle, "actions": actions}

    def _get_combat_actions(self) -> dict:
        """返回可直接交给 execute_action 的战斗 action schema。"""
        subphase = self.state.combat_subphase
        if subphase == CombatSubphase.AWAIT_ROUND_START.value:
            return self._get_battle_start_actions()
        if subphase == CombatSubphase.MONSTER_ACTIONS.value:
            pending = self.state.pending_monster_phase or {}
            return {"phase": subphase, "actions": [
                {"action_type": "resolve_monster_phase", "params_schema": {
                    "token": pending.get("token", "string"),
                    "choices": "恰好一个actor的一个行动：daowen或attack_actions，不得打包",
                }}
            ], "note": "怪物决策已准备；提交一个行动后统一恢复优先权与施法栈",
                    "spell_window": self._spell_window_payload()}
        if subphase == CombatSubphase.AWAIT_ROUND_END.value:
            return self._get_battle_end_actions()

        player = self.state.player
        if self.state.duel_turn == "opponent_side":
            refs = self.combat._combat_entity_refs()
            enemy_actors = [{"ref": ref, "name": entity.name}
                            for ref, entity in refs.items()
                            if self.state.on_enemy_side(entity)]
            actions = [{"action_type": "restart_battle", "params_schema": {}}]
            if enemy_actors and "opponent_side" not in self.combat._ended_turn_sides:
                actions.append({"action_type": "prepare_monster_phase", "params_schema": {},
                                "note": "准备一个敌方角色的一个真实行动"})
            if enemy_actors:
                actions.append({"action_type": "skip", "params_schema": {
                    "actor_ref": enemy_actors},
                    "note": "结束敌方回合，不消耗出手并保留当前出手数量"})
            return {"phase": subphase, "round": self.state.current_round,
                    "duel_turn": self.state.duel_turn, "actions": actions,
                    "ended_turn_sides": sorted(self.combat._ended_turn_sides),
                    "spell_window": self._spell_window_payload()}
        if not player or not player.is_alive:
            return {"phase": subphase, "actions": [], "note": "轮回者已死亡",
                    "spell_window": self._spell_window_payload()}
        refs = self.combat._combat_entity_refs()
        target_options = [{"ref": ref, "name": entity.name} for ref, entity in refs.items()]
        actions: list[dict] = [{
            "action_type": "restart_battle",
            "params_schema": {},
            "note": "结束本场战斗，恢复到战始前状态，并按当前规则重新开始本场战斗；不保留本场战斗结果",
        }]
        for name, instance in player.dao_wen.items():
            # 【唤潮】【蜃景】是开放地图道纹，不在战斗行动列表里显示。
            if name in {"唤潮", "蜃景"}:
                continue
            # 已定义【镇魔印】后，【封印】由法术流程自动发动，
            # 不再作为主动道纹候选；仅持有【封印】时仍照常显示。
            if (name == "封印" and player.entity_type == "轮回者"
                    and any(sp.name == "镇魔印" for sp in (getattr(player, "spells", None) or []))):
                continue
            if not player.can_use_daowen(name):
                actions.append({"action_type": "use_daowen", "available": False,
                                "params_schema": {"daowen_name": name},
                                "reason": "冷却中/被封印/【唯一】锁死"})
                continue
            max_x = self._max_legal_daowen_x(player, name)
            # 【真理】X=陈述句数（2026-10-23 用户裁定）；只需目标与待裁定陈述，
            # 发动仍沿用初拥旧口径，不消耗主动出手，也不经过普通数值道纹的闪避流程。
            if name == "真理":
                params_schema = {
                    "daowen_name": name,
                    "x": "int≥1（陈述句数）",
                    "target_ref": target_options,
                    "statement": "string（须正好X句）",
                }
            else:
                # 2026-10-09 用户令：波及 X≥2 下限 repealed，X 下限恒为 1（X_MIN 已拔除）。
                min_x = 1
                params_schema = {
                    "daowen_name": name,
                    "x": {"type": "integer", "minimum": min_x, "maximum": max_x},
                    "target_ref": target_options,
                    "dodge": "敌对单目标时必填布尔值",
                    "blood_shadow": "目标持有血影时必填布尔值",
                    "spell_choices": "若触发法术，按法术选项完整提交",
                    **({"lianmin_cost": list(DaoWenEngine.PAYABLE_TYPES)}
                       if name == "怜悯" else {}),
                    **({"relic_name": "目标可夺取的遗物或消耗品名；目标有多件时必须显式指定，同名跨种类时另加item_kind=relic/consumable，无可夺对象则本次发动被拒绝且不支付代价"}
                       if name == "豪夺" else {}),
                    **({"item_name": "目标要重铸/销毁的那件消耗品名；目标有多件时必须显式指定，无消耗品则本次发动被拒绝且不支付代价"}
                       if name in ("重铸", "回炉") else {}),
                    **({"corpse_name": "场上尸体名；多具时可显式指定，缺省取最近一具；无尸体则本次发动被拒绝且不支付代价"}
                       if name == "借尸" else {}),
                    **({"resource_type": sorted(RESOURCE_TYPES),
                        "note_suoqu": "索取：目标被迫支付X点该资源，实际支付量由你获得"}
                       if name == "索取" else {}),
                    **({"daowen_names": "要变为感染的道纹名列表（恰好X种）；目标道纹多于X种时必须显式指定"}
                       if name == "感染" else {}),
                    **({"daowen_names": "要转移的道纹名列表（恰好X种）；目标道纹多于X种时必须显式指定",
                        "creation_ref": "接收道纹的那个[造物]的引用；缺省取己方第一个造物；场上无造物则本次发动被拒绝且不支付代价"}
                       if name == "勾魂" else {}),
                    **({"replay_daowen": "目标本场已发动过的道纹名；有多种时必须显式指定，重演以本次X发动且不再另付其代价"}
                       if name == "重演" else {}),
                    **({"from_kind": ["血限", "枯竭", "法限", "速度", "异变"],
                        "to_kind": ["血限", "枯竭", "法限", "速度", "异变"],
                        "note_bingbian": "病变：将目标至多X层from_kind数值类代价转化为to_kind"}
                       if name == "病变" else {}),
                    **({"cost_choices": "净化：支付法力20X；显式选择目标拥有的数值类代价及层数，格式为[{type: 血限/枯竭/法限/速度/异变, layers: 正整数}]；可混选多种，所有layers之和不得超过X；单选项省略layers时默认吃满X；不能选择失忆或冷却"} if name == "净化" else {}),
                    "cost_share_target_ref": (
                        [{"ref": ref, "name": entity.name}
                         for ref, entity in self.combat.blood_pact_targets().items()]
                        if (self.combat._has_active_blood_pact(player)
                            and not self.combat._blood_pact_group(player)) else []
                    ),
                }
                if name in ("波及", "共罪"):
                    selectable = [{"ref": ref, "name": entity.name}
                                  for ref, entity in refs.items()
                                  if entity.is_alive and entity is not player]
                    params_schema["dodge_targets"] = {
                        # 2026-10-31：共罪目标数随 X（X 个非自身目标），与波及同口径。
                        "exact_count": "X", "targets": selectable,
                        "entry": {"target_ref": "存活非自身目标引用",
                                  "dodge": "敌对目标可选布尔值",
                                  "blood_shadow": "可选布尔值"},
                    }
            if name in ("点金", "典当"):
                # 失忆代价必须显式选纹（2026-11-01 用户令）：把候选直接公布，
                # 缺失/数量不符会被 use_daowen 在扣费前拒绝。
                params_schema["forget_daowen"] = {
                    "count": f"{'X' if name == '点金' else '2X'}（随发动X）",
                    "per_x": 1 if name == "点金" else 2,
                    "choices": custom_resources.amnesia_candidates(player),
                    "note": "道纹名数组；未提交或数量不符将被拒绝且不扣任何代价",
                }
            _custom = custom_resources.get(name)
            if (_custom is not None and _custom.category == "道纹"
                    and _custom.spec.get("cost", {}).get("type") == "失忆"):
                # 失忆代价必须显式选择要失去的道纹（缺失即拒绝，不替玩家选）。
                params_schema["forget_daowen"] = custom_resources.amnesia_candidates(player)
            params_schema.update(self.combat.daowen_extra_choice_schema(player,name,refs))
            actions.append({
                "action_type": "use_daowen", "available": max_x >= 1,
                "params_schema": params_schema,
            })
        for actor_ref, actor in refs.items():
            if actor is player or not self.state.on_player_side(actor):
                continue
            for name, instance in actor.dao_wen.items():
                if not actor.can_use_daowen(name):
                    continue
                fixed_x = instance.x_value if instance.x_value > 0 else 1
                # 波及X（固定X）：合法目标不足X时该指令永远无法发动——
                # 与玩家侧同一规则。
                if name in ("波及", "共罪"):
                    wave_candidates = [e for e in refs.values()
                                       if e.is_alive and e is not actor]
                    required_targets = fixed_x
                    if len(wave_candidates) < required_targets:
                        actions.append({"action_type": "use_daowen", "available": False,
                                        "params_schema": {"actor_ref": actor_ref,
                                                          "daowen_name": name, "x": fixed_x},
                                        "reason": f"{name}{fixed_x}需要{required_targets}个目标，"
                                                  f"当前仅{len(wave_candidates)}个存活非自身角色"})
                        continue
                actions.append({"action_type": "use_daowen", "available": True,
                                "params_schema": {"actor_ref": actor_ref, "daowen_name": name,
                                                  "x": fixed_x, "target_ref": target_options,
                                                  "dodge": "boolean", "blood_shadow": "boolean",
                                                  "trigger_spell_choices": "complete object",
                                                  **self.combat.daowen_extra_choice_schema(actor,name,refs)}})
        # 法术大全中的预定义法术也通过统一的自定义施法入口定义；没有额外状态。
        known = {sp.name: sp for sp in player.spells}
        available_builtin = set(self.combat.buildable_spells(player))
        for spell_name in sorted(available_builtin | set(known)):
            spell = known.get(spell_name) or self.combat.spell_definition(player, spell_name)
            if spell is None:
                continue
            usable = all(player.can_use_daowen(n) for n in spell.required_daowen)
            actions.append({"action_type": "define_spell",
                            "params_schema": {"spell_name": spell_name},
                            "available": usable,
                            "note": "通过自定义施法定义法术；定义后立即生效，消耗1次主动出手"})
        # 显式移除当前法术定义（不花出手）。
        removable = [b["name"] for b in self.combat.spell_bindings(player)]
        if removable:
            actions.append({
                "action_type": "undefine_spell",
                "params_schema": {"spell_name": removable},
                "available": True,
                "note": "移除一个当前法术定义，不花出手；"
                        "battle 作用域的自创法术本来会在战终自动清除"})
        # 战斗中自定义法术：持有所需道纹即可，消耗 1 次主动出手，立即生效。
        if self.state.phase == GamePhase.IN_COMBAT.value:
            actions.append({
                "action_type": "define_spell",
                "params_schema": {"spell": {"name": "str", "required_daowen": ["自身已持有道纹"],
                                            "trigger_condition": "str", "effect_flow": "str",
                                            "lifecycle": "可选 battle/permanent，默认battle"}},
                "available": bool(player.dao_wen) and player.is_alive,
                "note": "持有所需道纹即可自定义法术并立即生效，消耗1次主动出手；"
                        "默认 lifecycle=battle（本场战斗有效，战终自动清除），"
                        "显式传 permanent 才跨战斗保留；局外自定义入口已取消。"
                        "三大法则由句式解析器硬性校验"})
            # 瞬发法术：每个效果步骤支付1手，步骤间留敌方行动空档。
            actions.append({
                "action_type": "cast",
                "params_schema": {
                    "flow": "效果流程，如“发动杀伐X于目标→发动再生X于自身”；"
                            "含“循环”时由执行器逐轮结算（可选 max_iterations 限制轮数）",
                    "target_ref": target_options,
                    "steps": [{"x": "该步自己的正整数X", "target_ref": "仅“于任意目标”步骤填写",
                               "dodge": "该步目标为敌方时由其声明的布尔值",
                               "trigger_spell_choices": "该步的目标发动道纹前反应提交，结构同use_daowen"}],
                    "steps_note": "每条决策对应 flow 按深度优先顺序展开的一个决策槽位"
                                  "（if 的两个分支与循环体各计一条，即使本次不一定走到）；"
                                  "实际只消费走到的槽位，数量不符会被拒绝并报出应有步数",
                    "name": "可选，仅用于日志",
                    "max_iterations": "可选正整数：循环法术最多跑几轮（省略=按规则循环到不能继续）",
                },
                "available": bool(player.dao_wen) and player.is_alive,
                "note": "瞬发法术的每个实际效果步骤各扣1次出手，步骤间及最后一步后留敌方行动空档；"
                        "触发时机与最后空档不额外扣手，下一步付不起即中断，已结算步骤保留；"
                        "不写入角色法术列表。kind=daowen 为单道纹发动，kind=spell 为统一自定义施法"})
        if any(value > 0 for value in self.state.resonance.values()):
            actions.append({"action_type": "use_resonance", "params_schema": {
                "resonance_type": [n for n, v in self.state.resonance.items() if v > 0],
                "source_daowen": "道纹名", "target_ref": target_options,
            }})
        for item in self.state.consumables:
            if not item.is_depleted:
                schema: dict[str, Any] = {"name": item.name, "target_ref": target_options}
                entry: dict[str, Any] = {"action_type": "consume_item",
                                         "params_schema": schema,
                                         "uses_remaining": item.current_uses}
                if item.name == "破障晶":
                    schema["mode"] = {"发射": "15点伤害，无视格挡与闪避", "破障": "自身本回合伤害无视格挡"}
                if item.name in WING_MODES:
                    schema["mode"] = dict(WING_MODES[item.name])
                    entry["note"] = "翼类消耗品：每次使用选一个 mode，耗 1 点耐久"
                actions.append(entry)
        plight_options = self.combat.get_plight_evolution_options()
        actions.append({"action_type": "declare_evolution", "available": bool(plight_options),
                        "params_schema": {"monster": "string", "daowen": "string", "x": "integer"},
                        "plight_monsters": plight_options})
        actor_options = [{"ref": ref, "name": entity.name} for ref, entity in refs.items()
                         if self.state.on_player_side(entity) and entity.is_alive]
        for employee_index, employee in enumerate(self.state.employees):
            if employee.is_alive and not employee.is_deployed and not employee.is_debt_bound:
                actions.append({"action_type": "deploy_employee",
                                "params_schema": {"employee_ref": f"employee:{employee_index}",
                                                  "name": employee.name}})
        blood_pact_cost_targets = [
            {"ref": ref, "name": entity.name}
            for ref, entity in self.combat.blood_pact_targets().items()
        ] if self.combat._has_active_blood_pact(player) else []
        artifact_actions = {
            "教父左轮": ("fire_godfather_revolver", {"target_ref": target_options}),
            "负岳碑": ("declare_fuyuebei_toll", {
                "target_ref": target_options,
                "cost_share_target_ref": blood_pact_cost_targets,
            }),
        }
        for artifact in self.state.artifacts_owned:
            if artifact in artifact_actions:
                action_type, schema = artifact_actions[artifact]
                actions.append({"action_type": action_type, "params_schema": schema})
        trait_actions = {
            "吞骸龙胃": ("devour_monster", {"monster_ref": "enemy:index", "dragon_heart": "name"}),
            "断尾求生": ("declare_tail_sacrifice", {"trait": "owned dragon relic"}),
            "血族尖牙": ("enslave_as_chizu", {
                "target_ref": target_options,
                "cost_share_target_ref": blood_pact_cost_targets}),
            "血食": ("blood_feast", {"target_ref": target_options}),
        }
        for trait in self.state.dragon_traits + self.state.first_embrace_traits:
            if trait in trait_actions:
                action_type, schema = trait_actions[trait]
                actions.append({"action_type": action_type, "params_schema": schema})
        ally_options = [{"ref": ref, "name": entity.name}
                        for ref, entity in refs.items()
                        if self.state.on_player_side(entity) and entity.is_alive
                        and entity is not player and entity.entity_type in ("朋友", "员工")]
        idle_allies = [a for a in ally_options
                       if refs[a["ref"]].actions_used_this_round
                       < self.combat.single_round_action_count(refs[a["ref"]])]
        # 聚能消耗1出手；【跳过】不消耗出手，但会结束当前方回合。
        actions.extend([
            {"action_type": "focus",
             "params_schema": {"actor_ref": actor_options},
             "available": bool(player is not None and player.is_alive),
             "note": "聚能：消耗1次出手，恢复50%已损法力（向上取整）"},
            {"action_type": "skip",
             "params_schema": {"actor_ref": actor_options},
             "available": bool(player is not None and player.is_alive),
             "note": "跳过：结束你的回合，不消耗出手并保留当前出手数量"},
        ])
        actions.extend([
            {"action_type": "prepare_attack", "params_schema": {"actor_ref": actor_options}},
            {"action_type": "declare_wish", "params_schema": {"wish_text": "string", "target_ref": target_options}},
            {"action_type": "declare_escape", "params_schema": {},
             "note": "逃跑：一次付清5次出手，以当前状态强制脱离本场战斗（最终死斗/【不离】不可）"},
            {"action_type": "choose_diandang_daowen",
             "params_schema": {"daowen_name": list(self.state.pending_diandang_discoveries[0])
                               if self.state.pending_diandang_discoveries else "string"},
             "available": bool(self.state.pending_diandang_discoveries),
             "remaining_groups": len(self.state.pending_diandang_discoveries),
             "note": "领取【典当】发现：从当前组3选1里选1种获得，不消耗出手"},
            {"action_type": "command_ally", "params_schema": {
                "ally_ref": ally_options, "instruction": "语言命令，如：攻击 余烬侍者 / 发动 再生 / 护卫 3",
                "available": bool(ally_options)}},
            {"action_type": "resolve_ally_phases", "params_schema": {},
             "available": bool(idle_allies),
             "note": "让一名未出手的[朋友]/[员工]自主完成一个行动（无命令时）"},
        ])
        return {"phase": subphase, "round": self.state.current_round,
                "duel_turn": self.state.duel_turn,
                "ended_turn_sides": sorted(self.combat._ended_turn_sides),
                "spell_window": self._spell_window_payload(), "actions": actions}

    def _max_legal_daowen_x(self, actor: Entity, name: str, y: int = 1) -> int:
        """按真实代价口径枚举当前可提交的最大X，不把当前法力直接冒充上限。

        y 为预留双参数第二参数；现行【分裂】只有X（造物数量），不读取Y。
        【真理】的X=陈述句数（2026-10-23 用户裁定）：只受实际写出的句子上限约束，
        这里给出一个宽松上限，最终以提交的陈述句数为准。
        """
        if name == "真理":
            return 10
        upper = max(1, actor.current_mana, actor.current_hp, actor.blood_limit,
                    actor.current_speed, self.state.shards, self.state.fake_shards, 50)
        probe_kwargs={"target":actor}
        if name=="净化":
            # 可混选：以「所有可净化对象中可净化的总层数最多者」为准给出合法上限，
            # 真正的目标与层数分配仍由提交时的 cost_choices 逐项校验。
            best = None
            for target in self.combat._combat_entity_refs().values():
                if not target.is_alive or not self.combat.is_targetable(actor, target):
                    continue
                chosen = [{"type": kind, "layers": 1}
                          for kind in PURIFIABLE_COST_KINDS
                          if purifiable_cost_layers(target, kind) > 0]
                if chosen and (best is None or len(chosen) > len(best[1])):
                    best = (target, chosen)
            if best is None:
                return 0
            probe_kwargs={"target":best[0],"cost_choices":best[1]}
        legal = 0
        for x in range(1, upper + 1):
            try:
                calc = DaoWenEngine.resolve(name, x, y=y, caster=actor,**probe_kwargs)
            except Exception:
                continue
            ctype = calc.get("cost_type")
            # 2026-11-09：多途径收成单途径后，【再生】只看法力，【舍力】只看攻击力。
            if ctype == "攻击力" and calc.get("cost_attack", 0) > actor.effective_attack_power():
                continue
            elif ctype == "消耗" and calc.get("cost", 0) > actor.current_mana:
                continue
            # 2026-10-13：失忆类代价按持有道纹种数封顶（use_daowen 预检同口径）。
            # 此前 schema 对【点金】X 不设上限、会对【典当】给出永远付不起的X。
            if ctype == "失忆" and calc.get("cost", 0) > len(actor.dao_wen):
                continue
            if ctype in set(self.combat.SHAREABLE_RESOURCE_TYPES) | {"枯竭", "异变"}:
                amount = {
                    "生命": calc.get("cost_hp", calc.get("cost", 0)),
                    "血限": calc.get("cost_blood_limit", calc.get("cost", 0)),
                    "枯竭": calc.get("cost_mana_limit", calc.get("cost", 0)),
                    "法限": calc.get("cost_mana_cap", calc.get("cost", 0)),
                    "速度": calc.get("cost_speed", calc.get("cost", 0)),
                    "异变": calc.get("cost_mutation", calc.get("cost", 0)),
                }[ctype]
                candidate_refs = [""]
                if self.combat._has_active_blood_pact(actor):
                    candidate_refs.extend(self.combat.blood_pact_targets(actor))
                payable = False
                for share_ref in candidate_refs:
                    try:
                        self.combat.validate_numeric_cost(actor, ctype, amount, share_ref)
                    except ValueError:
                        continue
                    payable = True
                    break
                if not payable:
                    continue
            # 强化是法力＋生命的复合支付；只看法力会给出实际付不起的X。
            if calc.get("cost_hp", 0) and ctype != "生命":
                try:
                    self.combat.validate_numeric_cost(actor, "生命", int(calc["cost_hp"]))
                except ValueError:
                    continue
            legal = x
        # 波及X：X还不得超过合法目标数（全部存活非自身角色）——否则schema会给出
        # 永远无法发动的X。
        if name in ("波及", "共罪"):
            candidates = [e for e in self.combat._combat_entity_refs().values()
                          if e.is_alive and e is not actor]
            legal = min(legal, len(candidates)) if name == "波及" else (legal if len(candidates) >= 2 else 0)
        return legal

    def _get_battle_end_actions(self) -> dict:
        round_end_schema = {}
        player = self.state.player
        if (player and self.state._relic_active(player, "血族血脉")
                and self.combat._has_active_blood_pact(player)
                and not self.combat._blood_pact_group(player)
                and player.damage_dealt_this_round <= 0):
            round_end_schema["blood_lineage_cost_share_target_ref"] = [
                {"ref": ref, "name": entity.name}
                for ref, entity in self.combat.blood_pact_targets().items()
            ]
        global_spells_end = self.combat.prepare_global_trigger_spells(TriggerTiming.ROUND_END.value)
        if (global_spells_end
                and self.combat.has_manual_global_trigger_spells(TriggerTiming.ROUND_END.value)):
            round_end_schema["spell_choices"] = {
                "_instruction": "存在【回终】全局法术候选，须逐一显式提交use/steps（循环法术可另附 max_iterations）",
                "candidates": global_spells_end,
            }
        actions = [{"action_type": "round_end", "params_schema": round_end_schema}]
        if self.state.battle_won():
            battle_end_schema: dict[str, Any] = {}
            global_spells_battle_end = self.combat.prepare_global_trigger_spells(TriggerTiming.BATTLE_END.value)
            if global_spells_battle_end:
                battle_end_schema["spell_choices"] = {
                    "_instruction": "存在【战终】全局法术候选，须逐一显式提交use/steps（循环法术可另附 max_iterations）",
                    "candidates": global_spells_battle_end,
                }
            actions.append({"action_type": "battle_end", "params_schema": battle_end_schema})
        return {"phase": CombatSubphase.AWAIT_ROUND_END.value, "actions": actions}

    def _get_dead_duel_actions(self) -> dict:
        return {"phase": "最终死斗", "actions": self._get_combat_actions().get("actions", []),
                "note": "无法逃跑；必须遵守duel_turn和战斗子阶段"}

    # ==================== 核心行动接口 ====================

    _COMBAT_ONLY_ACTIONS = {
        "prepare_attack", "resolve_attack", "attack", "declare_wish", "declare_escape",
        "declare_parry", "cast", "focus", "rest", "skip",
        "deploy_employee", "lianxin_in_battle", "declare_evolution",
        "prepare_monster_phase", "resolve_monster_phase", "monster_phase",
        "round_start", "round_end",
        # resolve_final_duel不在此列：死斗败者先过死者之页（reset进setup相）
        # 再结算defeat，战斗限定会把它挡在setup外导致擂主永不回槽（r16实战复现）。
        "activate_duel_relic", "use_black_card", "use_crime_vault",
        "fire_godfather_revolver", "select_shared_dragon_heart", "declare_fuyuebei_toll",
        "devour_monster", "declare_tail_sacrifice",
        "enslave_as_chizu", "blood_feast",
        "command_ally", "resolve_ally_phases",
    }

    _PRIORITY_GATED_ACTIONS = {
        "use_daowen", "cast", "focus", "rest", "skip",
        "prepare_attack", "resolve_attack", "declare_wish", "declare_escape",
        "deploy_employee", "define_spell", "command_ally", "resolve_ally_phases",
    }

    # 需要执行【束缚】行动分类的主动入口。回合推进、怪物阶段提交、强制选择和战终结算
    # 不在此列；这些是流程/反应，不是目标主动使用的行动。
    _BOUND_RESTRICTED_ACTIONS = {
        "declare_parry", "use_daowen", "use_resonance", "consume_item",
        "cast", "focus", "prepare_attack", "resolve_attack",
        "declare_wish", "declare_escape", "deploy_employee",
        "define_spell", "undefine_spell", "lianxin_in_battle",
        "declare_evolution", "activate_duel_relic", "use_black_card",
        "use_crime_vault", "fire_godfather_revolver", "select_shared_dragon_heart",
        "declare_fuyuebei_toll", "devour_monster",
        "declare_tail_sacrifice",
        "enslave_as_chizu", "blood_feast", "use_truth_eye",
        "command_ally", "resolve_ally_phases",
        "unlock_dragon_trait",
    }

    def _bound_action_actor(self, action_type: str, params: dict) -> Optional[Entity]:
        """定位一次主动行动的执行者；两阶段攻击从pending快照读取攻击者。"""
        refs = self.combat._combat_entity_refs()
        actor_ref = params.get("actor_ref")
        if action_type == "resolve_attack":
            pending = self.state.pending_attack or {}
            actor_ref = (pending.get("options") or {}).get("actor_ref", actor_ref)
        if actor_ref:
            return refs.get(actor_ref)
        if action_type == "use_daowen" and params.get("actor"):
            matches = [entity for entity in refs.values()
                       if entity.name == params.get("actor")]
            if len(matches) == 1:
                return matches[0]
        return self.state.player

    @staticmethod
    def _is_allowed_bound_spell_action(action_type: str, params: dict) -> bool:
        """束缚允许施放瞬发法术，或定义/施展自定义法术；不允许直接发动道纹。"""
        if action_type == "define_spell":
            return True
        if action_type != "cast":
            return False
        if "flow" in params or "huangfu_spell" in params:
            return params.get("kind") in (None, "flow", "instant")
        return params.get("kind") == "spell"

    def _is_bound_ally_spell_command(self, ally: Entity, params: dict) -> bool:
        """束缚的友方仅可被命令施放已由【黄符】授予的瞬发法术。"""
        instruction = params.get("instruction", "")
        if not isinstance(instruction, str):
            return False
        for verb in self._ALLY_DAOWEN_VERBS:
            if instruction.startswith(verb):
                rest = instruction[len(verb):].strip()
                spell_name = rest.split()[0] if rest else ""
                return (bool(spell_name)
                        and any(spell.name == spell_name for spell in ally.spells)
                        and (ally.runtime_id, spell_name) in self.state.huangfu_grants())
        return False
