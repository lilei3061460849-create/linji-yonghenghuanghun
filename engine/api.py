"""
游戏引擎 API - AI的唯一交互入口
核心原则：
1. AI通过此API获取状态、做出决策、触发行动
2. 所有数值计算由引擎完成，AI禁止自行计算
3. 程序无法判定时返回Interrupt，等待DM裁定
4. DM裁定存入数据库，下次类似情况自动匹配
"""
from __future__ import annotations
import base64
import copy
import dataclasses
import weakref
import json
import pickle
import os
import re
import time
import uuid
from typing import Optional, Any

from .models import (
    Entity, GameState, DaoWen, DaoWenInstance, Spell,
    Relic, Consumable, StatusEffect, LongJiXin
)
from .world import WorldState, WorldActorState
from .enums import (GamePhase, CombatSubphase, ActionPhase, TriggerTiming,
                    InterruptType, EntityType, EffectScope, EffectPolarity)
from .dice import DiceEngine
from .daowen import DaoWenEngine, ResonanceEngine
from .combat import CombatEngine
from .spell_dsl import parse_spell_definition, SpellDslError
from .combat_events import register_combat_event_observer
from .consumables import (
    ZIYU_TRIGGER_BATTLE_END, apply_zi_yu_trigger, note_action_targets)
from .events import EventPool
from .rule_repository import RuleRepository
from .dungeons import DEFAULT_INDEX
from .gamedata import (REGION_EXCLUSIVE_DAOWEN, ORIGINAL_MONSTER_DAOWEN,
                       MONSTER_TRANSFORM_DAOWEN, SHAFA_LOOP_DAOWEN,
                       UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN, learnability_error)
from .dm_rulings import DMRulingsDB, DMRuling, Interrupt
from .death_book import DeathBookStore, draft_legacy, validate_legacy
from .ai_memory import clear_memory, condense_to_legacy
from .effect_context import make_context, normalize_context
from .personality import (
    get_personality as personality_get,
    record_behavior as personality_record,
    remove_personality as personality_remove,
    format_personality_for_ai as personality_format,
    resolve_entity as personality_resolve,
)
from .handlers.setup import (
    handle_setup_attributes,
    handle_setup_choose_region,
    handle_setup_choose_resonance,
    handle_setup_choose_initial_daowen,
)
from .handlers.economy import (
    handle_deploy_employee,
    handle_dismiss_employee,
    handle_repay_debt_employee,
    handle_pay_employee_wage,
    handle_suppress_rebellion,
    handle_resolve_rebellion_battle,
    handle_appease_rebellion,
    handle_negotiate_rebellion,
)
from .handlers.duel import (
    handle_activate_duel_relic,
    handle_resolve_final_duel,
)


# 扭曲都市废墟设施工具库（9件：名→(耐久, 效果文本逐字)）
TWISTED_TOOL_LIBRARY = {
    "反怪物电击枪": (3, "对一个[目标]造成25点伤害；若[目标]处于【飞行】，额外造成15点伤害。"),
    "备用血泵": (3, "使自身获得20点［回复］；若自身当前生命≤30%，额外获得30点格挡。"),
    "强光探照灯": (2, "使一个[目标]陷入【蒙蔽2】"),
    "高压水枪": (2, "清除全场所有敌方[目标]身上的所有“持续X”效果"),
    "储能电池": (3, "立即获得12点法力。"),
    "急救箱": (2, "使自身获得[回复25]，并清除自身身上一种“持续X”的负面减益。"),
    "干扰仪": (2, "使全场所有敌方[目标]本回合无法发动自身道纹"),
    "高爆手雷": (2, "对一个[目标]造成15点伤害，并使其本回合攻击次数-1"),
    "万钧印": (3, "有[目标]发动攻击时，可将其改为该攻击者对自己发动攻击；每触发一次消耗1点耐久。"),
}

# 自由结晶随机代价池（2026-10-05 用户令）：代价一节中可数值化的八种代价，共计10。
# 【唯一】无数值、无法按 X=10 结算，不入池（见 物品索引.md#自由结晶）。
FREE_CRYSTAL_COST_POOL = ("流血", "衰老", "枯竭", "萎缩", "疲惫", "失忆", "异变", "冷却")
FREE_CRYSTAL_COST_AMOUNT = 10


class GameEngine:
    """
    游戏引擎主类
    AI通过此接口与游戏交互，所有数值计算必须经过本引擎
    """

    def __init__(self, db_path: str = "data/dm_rulings.db", save_dir: str = "data/saves", autosave: bool = True,
                 rng_seed: Optional[int] = None, sealed_candidate_path: str = "data/sealed_candidate.json",
                 death_book_path: str = "死者之书.md"):
        """
        rng_seed: 引擎自身随机源的种子。默认为None（真实随机，用于实际游戏）；
        测试/回归场景可传入固定整数，使全程随机结果可复现。
        sealed_candidate_path: "最终的冠冕"封存候选人的持久化文件路径。必须在不同GameEngine
        实例/不同轮回者playthrough之间共享同一路径，候选人数据才能被下一位到达者读取。
        同一个文件同时承载阶级进度（`progression` 段，见 _load_progression）：
        跨轮回、按引擎实例隔离，测试传 tmp 路径即可，不会污染生产文件。
        """
        self.state = GameState()
        self.dice = DiceEngine(seed=rng_seed)
        self.combat = CombatEngine(self.state, self.dice)
        self.rulings_db = DMRulingsDB(db_path)
        self.save_dir = save_dir
        self.autosave = bool(autosave)
        self.sealed_candidate_path = sealed_candidate_path
        self.death_book_path = death_book_path
        self.death_book = DeathBookStore(death_book_path)
        self.state.death_book_legacies = self.death_book.load()
        self._load_progression()  # 阶级推进/无尽模式：与封存槽同文件持久（见 _save_progression）
        # 封存槽是跨轮回持久文件；若进程重启，从中恢复《死者之书》的系统记录。
        # （2026-10-05 用户令：癌变休整加成 rest_heal_bonus 已废止，旧封存文件里
        # 的该键直接忽略。）
        if os.path.exists(sealed_candidate_path):
            try:
                with open(sealed_candidate_path, encoding="utf-8") as handle:
                    sealed = json.load(handle)
                self.state.death_book_wisdom = list(sealed.get("death_book_wisdom") or [])
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass  # 正式读取/报错仍由最终冠冕流程负责；初始化不得因坏候选阻塞。
        os.makedirs(save_dir, exist_ok=True)

        # 规则校验器（延迟导入避免循环）
        self._validator = None
        self._rule_sync = None

        # 中断队列
        self._pending_interrupts: list[Interrupt] = []
        # 体外心脏：记录[战始]翻倍前的血限基准，[战终]用于还原
        self._artifact_base_blood_limit = 0

        # 事件系统 / 怪物池：静态规则只解析一次，多个引擎共享同一份解析结果
        # （rule_repository 按输入正文摘要缓存，并给每个引擎一份结构副本）。
        if DEFAULT_INDEX.exists():
            self.event_pool = EventPool(RuleRepository.events(DEFAULT_INDEX))
            self.monster_pool = RuleRepository.monster_pool(DEFAULT_INDEX)
        else:
            self.event_pool = EventPool({})
            self.monster_pool = {}
        # 行动历史（可追溯）
        self._action_history: list[dict] = []

        # 上一次的行动结果
        self._last_result: Optional[dict] = None
        # 战斗重来快照：记录进入本场战斗前的完整状态，供 restart_battle 使用。
        self._battle_restart_snapshot: Optional[dict] = None

    @property
    def validator(self):
        if self._validator is None:
            from .validator import RuleValidator
            self._validator = RuleValidator()
        return self._validator

    @property
    def rule_sync(self):
        return self._rule_sync

    def enable_rule_sync(self, rule_files: list[str] | None = None, rules_dir: str = "."):
        """启用规则同步；默认跟踪全部正文事实源。"""
        from .rule_sync import RuleSync
        self._rule_sync = RuleSync(rule_files=rule_files, rules_dir=rules_dir)

    # ==================== 状态查询 ====================

    def _project_state_for_world_player(self, state_data: dict) -> dict:
        """局外阶段给AI返回迷雾投影；绝不泄露未见单位、地图地形或地点坐标。"""
        if not getattr(self.state, "world_state", {}) or self.state.phase != GamePhase.PRE_BATTLE.value:
            return state_data
        world = WorldState.from_dict(self.state.world_state)
        player = next((actor for actor in world.actors.values()
                       if actor.kind == "player" and actor.alive), None)
        if player is None:
            return state_data
        # 决策观察会刷新该轮可见单位与私有记忆，但不会推进世界时间。
        world.visible_cells(player.actor_id)
        self.state.world_state = world.to_dict()
        visible_positions = {player.position, *world.map.adjacent_vision(player.position)}
        visible_actor_ids = {
            actor.actor_id for actor in world.actors.values()
            if actor.alive and actor.position in visible_positions
        }
        visible_actor_ids.add(player.actor_id)
        known_keys = set(player.known_cells)
        known_keys.update(world.map.key(pos) for pos in visible_positions)

        # GameState.to_dict() 引用原 world_state，必须深拷贝后再投影，不能修改真实存档。
        world_payload = copy.deepcopy(world.to_dict())
        map_payload = world_payload.get("map", {})
        map_payload["terrain"] = {
            key: terrain for key, terrain in map_payload.get("terrain", {}).items()
            if key in known_keys
        }
        map_payload["locations"] = {
            key: location for key, location in map_payload.get("locations", {}).items()
            if location.get("discovered", False)
        }
        world_payload["actors"] = {
            key: actor for key, actor in world_payload.get("actors", {}).items()
            if key in visible_actor_ids
        }
        world_payload["encounters"] = {
            key: encounter for key, encounter in world_payload.get("encounters", {}).items()
            if player.actor_id in encounter.get("participants", [])
        }
        # 仅向AI展示由轮回者亲历的世界事实；隐藏NPC的移动和遭遇不进入观察流。
        world_payload["event_log"] = [
            event for event in world_payload.get("event_log", [])
            if event.get("actor_id") == player.actor_id
        ]
        state_data["world_state"] = world_payload
        state_data["enemies"] = [
            entity for entity in state_data.get("enemies", [])
            if entity.get("runtime_id") in visible_actor_ids
        ]
        for group_name in ("friends", "employees", "temp_friends"):
            for entity in state_data.get(group_name, []):
                if entity.get("runtime_id") not in visible_actor_ids:
                    entity["world_x"] = None
                    entity["world_y"] = None
        merchant = state_data.get("merchant")
        if merchant and merchant.get("runtime_id") not in visible_actor_ids:
            state_data["merchant"] = None
        for key in ("world_enemy_reserve", "world_encounter_entities"):
            state_data[key] = [
                entity for entity in state_data.get(key, [])
                if entity.get("runtime_id") in visible_actor_ids
            ]
        traits = state_data.get("personality_traits")
        if isinstance(traits, dict):
            state_data["personality_traits"] = {
                key: value for key, value in traits.items() if key in visible_actor_ids
            }
        return state_data

    def get_state(self) -> dict:
        """
        获取当前决策者可观察的游戏状态。
        开放世界局外阶段按当前视野投影；战斗中保留现有完整战斗状态。
        """
        state_data = self._project_state_for_world_player(self.state.to_dict())
        return {
            "state": state_data,
            "pending_interrupts": [i.to_dict() for i in self._pending_interrupts],
            "last_result": self._last_result,
            "available_actions": self.get_available_actions(),
        }

    # ==================== 角色性格特征 ====================
    # 原则"先射箭，后画靶"：创建时不预设人格，只由实际行为逐渐推断；命零即清除。
    # 详见 engine/personality.py。性格只是行为倾向参考，绝不构成对决策的强制规则。

    def get_personality(self, character) -> dict:
        """读取存活角色性格：{success, runtime_id, name, traits}。

        character 接受 Entity 实例 / runtime_id / 角色名；
        死亡或未知角色一律 success=False——死亡角色的性格不得再被读取。
        """
        personality = personality_get(self.state, character)
        if personality is None:
            return {"success": False, "error": "角色不存在或已命零，无性格数据"}
        return {"success": True, **personality}

    def update_personality(self, character, dimension: str, direction: float,
                           evidence: str, weight: float = 1.0) -> dict:
        """以一条行为证据更新性格推断（不入模板、不跨实例、单次不贴死标签）。

        dimension 见 engine.personality.TRAIT_DIMENSIONS；direction ±1；
        evidence 必填（形成该判断的行为依据）；weight (0,1]。
        """
        entity = personality_resolve(self.state, character)
        if entity is None:
            return {"success": False, "error": "角色不存在或已命零，无法累积性格证据"}
        try:
            entry = personality_record(self.state, entity, dimension, direction,
                                       evidence, weight=weight)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        return {"success": True, "runtime_id": entity.runtime_id,
                "name": entity.name, "updated": entry}

    def remove_personality(self, character) -> dict:
        """手工清除角色性格数据（与死亡管线共用入口，幂等）。"""
        removed = personality_remove(self.state, character)
        return {"success": True, "removed": removed}

    def format_personality_for_ai(self, character) -> dict:
        """渲染供 AI 使用的人格摘要（含置信度、依据次数与'倾向非强制'提示）。"""
        summary = personality_format(self.state, character)
        if summary is None:
            return {"success": False, "error": "角色不存在或已命零，无性格数据"}
        return {"success": True, "summary": summary}


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
            has_wusuoqiu = any(r.name == "无所求" for r in self.state.relics)
            actions = []
            for option in event["options"]:
                schema: dict = {"event": self.event_pool.current, "option_id": option["id"]}
                if has_wusuoqiu and self._is_reject_option_text(option["text"]):
                    schema["note"] = "持【无所求】：本选项另获1点属性点（入属性点池，按正文2点一档兑速限/法限、1点一档兑血限）"
                if self.event_pool.current == "祭坛" and option["text"].startswith("投资"):
                    schema["x"] = "正整数，且不超过当前真实碎片余额；每次抵达最多投资一次"
                    schema["note"] = "祭坛投资概率：15%净赚4X、25%净赚X、35%净亏X、25%净亏3X；黑天鹅额外2X直接计入真实负余额，可能触发动态还债"
                if self.event_pool.current == "过路商人" and option["text"].startswith("遗物交易"):
                    self._init_relic_pool()
                    schema.update({"trade_direction": ["buy", "sell"],
                                   "relic_name": f"字符串；buy可选：{[r.name for r in (getattr(self.state, "merchant", None).relics if getattr(self.state, "merchant", None) else [])]}；sell可选：{[r.name for r in self.state.relics]}",
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
            return self._get_combat_actions()
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
                                "constraint": "三者之和≤档位属性点+池内余点；1点=6血限，2点=1速限=1法限"}}},
            {"action_type": "pre_battle_action", "params_schema": {
                "sub_action": "学习", "sub": ["daowen"],
                "tier": {"daowen": [1, 2]},
                "names": "daowen必填，数量必须等于tier",
                "note": "法术已于2026-09-16改为无需学习：持所需道纹即可在战斗中用 "
                        "统一自定义施法（define_spell，消耗1次主动出手）"}},
            {"action_type": "pre_battle_action", "params_schema": {
                "sub_action": "共鸣",
                "tier": {1: "随机列3件候选并显式选择1件", 2: "额外1精力+15碎片自选"},
                "name": "2档必填，当前遗物池名称"}},
            {"action_type": "pre_battle_action", "params_schema": {
                "sub_action": "探索", "tier": {1: "发现1个未遇事件", 2: "30碎片发现2个未遇事件"}}},
        ]
        for region_action in self._get_region_actions():
            actions.append({
                "action_type": "pre_battle_action",
                "params_schema": {"sub_action": region_action["id"],
                                  **region_action["params_schema"]},
            })
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
            actions.extend([
                {"action_type": "pay_for_dragon_nature",
                 "params_schema": {"cost_type": ["衰老", "枯竭", "萎缩"], "x": "integer",
                                   "cost_share_target_ref": blood_pact_cost_targets}},
                {"action_type": "unlock_dragon_trait",
                 "params_schema": {"trait": [name for name in self.DRAGON_TRAITS
                                                if name not in self.state.dragon_traits]}},
            ])
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
            battle_start_schema: dict[str, Any] = {"relic_choices": "逐件显式提交可选战始遗物"}
            global_spells_start = self.combat.prepare_global_trigger_spells(TriggerTiming.BATTLE_START.value)
            if global_spells_start:
                battle_start_schema["spell_choices"] = {
                    "_instruction": "存在【战始】全局法术候选，须逐一显式提交use/steps（循环法术可另附 max_iterations）"
                                    "（实际结算发生在战始遗物与法力重置之后，届时可能因法力变化而改变可承担的X）",
                    "candidates": global_spells_start,
                }
            actions.append({"action_type": "battle_start", "params_schema": battle_start_schema})
        if self.state.endless_mode:
            # 无尽模式「无法探索」：不出现在可用行动里，
            # 保证 get_available_actions 的输出仍可原样执行（开发规则 13）。
            actions = [a for a in actions
                       if a.get("params_schema", {}).get("sub_action") != "探索"]
        if getattr(self.state, "world_state", {}):
            # 开放世界中不再提供独立「探索」菜单；事件由地图地点触发。
            actions = [action for action in actions
                       if action.get("params_schema", {}).get("sub_action") not in {"探索", "附煞"}]
        if not getattr(self.state, "world_state", {}):
            actions.append({"action_type": "world_initialize",
                            "params_schema": {"width": "integer (default 21)", "height": "integer (default 21)"},
                            "note": "初始化开放世界地图；地图种子由引擎骰子产生并记录"})
        else:
            actions.extend([
                {"action_type": "world_status", "params_schema": {}},
                {"action_type": "world_vision", "params_schema": {"actor_id": "optional; defaults to player"}},
                {"action_type": "world_move", "params_schema": {"x": "integer", "y": "integer",
                                                                  "actor_id": "optional; defaults to player"}},
                {"action_type": "world_wait", "params_schema": {"turns": "positive integer (default 1)",
                                                                  "actor_id": "optional; defaults to player"}},
                {"action_type": "world_interact", "params_schema": {
                    "target_actor_id": "actor_id from visible_actors",
                    "choice": ["talk", "trade", "cooperate", "intimidate", "attack"]}},
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
            repairable = [
                {"item_ref": f"consumable:{index}", "name": item.name,
                 "current_uses": item.current_uses, "max_uses": item.max_uses}
                for index, item in enumerate(self.state.consumables)
                if 0 < item.current_uses < item.max_uses
            ]
            return [{"id": "维修", "params_schema": {
                "tier": [1, 2, 3],
                "allocations": {"type": "list", "items": {
                    "item_ref": repairable, "amount": "positive integer"},
                    "constraint": "amount总和必须等于档位耐久点"},
            }}]
        if self.state.current_region == "罪孽都市":
            return [{"id": "雇佣", "params_schema": {"name": "string", "blood_alloc": "integer",
                                                       "atk_bundles": "integer"}}]
        if self.state.current_region == "龙心谷":
            return [{"id": "炼心", "params_schema": {}}]
        if self.state.current_region == "乱葬岗":
            return [{"id": "附煞", "params_schema": {
                "mode": ["发现", "选择"], "sha_qi": "选择模式必填：法煞/魂煞/冥煞/血煞/锁煞/心煞",
                "daowen_name": "要附煞的道纹名"}}]
        return []

    def _get_battle_start_actions(self) -> dict:
        relic_choices_schema: dict[str, Any] = {"_instruction": "按当前回始遗物逐件显式提交"}
        player = self.state.player
        # 2026-10-12 用户令：【血契】只保留「数值型代价可与朋友/员工平分」，
        # 与【透支】重合的「[回始]流血4X换X法力」已删除，故它不再是回始主动遗物，
        # `血契`/`对手血契` 两个回始决策键取消。平分仍通过各代价提交处的
        # cost_share_target_ref 生效（见 _get_pre_battle_actions / use_daowen /
        # 负岳碑 / 血族血脉 四处，全部保留）。
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
        if "烬翼" in self.state.dragon_traits:
            actions.insert(0, {"action_type": "use_dragon_wings", "params_schema": {"x": "integer"}})
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
            return {"phase": subphase, "actions": [
                {"action_type": "prepare_monster_phase", "params_schema": {}}
            ], "note": "已进入怪物阶段，本步无需spell_choices（玩家行动阶段结束时刻的【自身回合结束】法术已在上一步prepare_monster_phase里结算过；【敌回始】当前无结算点，见 engine/combat_parts/spells.py 的 _WIRED_TRIGGERS 注释）"}
        if subphase == CombatSubphase.AWAIT_ROUND_END.value:
            return self._get_battle_end_actions()

        player = self.state.player
        if not player or not player.is_alive:
            return {"phase": subphase, "actions": [], "note": "轮回者已死亡"}
        refs = self.combat._combat_entity_refs()
        target_options = [{"ref": ref, "name": entity.name} for ref, entity in refs.items()]
        actions: list[dict] = [{
            "action_type": "restart_battle",
            "params_schema": {},
            "note": "结束本场战斗，恢复到战始前状态，并按当前规则重新开始本场战斗；不保留本场战斗结果",
        }]
        for name, instance in player.dao_wen.items():
            # 已定义【镇魔印】后，【封印】由法术流程自动发动，
            # 不再作为主动道纹候选；仅持有【封印】时仍照常显示。
            if (name == "封印" and player.entity_type == "轮回者"
                    and any(sp.name == "镇魔印" for sp in (getattr(player, "spells", None) or []))):
                continue
            if not instance.can_use():
                actions.append({"action_type": "use_daowen", "available": False,
                                "params_schema": {"daowen_name": name},
                                "reason": "冷却中或被封印"})
                continue
            max_x = self._max_legal_daowen_x(player, name)
            # 2026-10-09 用户令：波及 X≥2 下限 repealed，X 下限恒为 1（X_MIN 已拔除）。
            min_x = 1
            actions.append({
                "action_type": "use_daowen", "available": max_x >= min_x,
                "params_schema": {
                    "daowen_name": name,
                    "x": {"type": "integer", "minimum": min_x, "maximum": max_x},
                    "target_ref": target_options,
                    "dodge": "敌对单目标时必填布尔值",
                    "blood_shadow": "目标持有血影时必填布尔值",
                    "spell_choices": "若触发法术，按法术选项完整提交",
                    **({"zaisheng_cost": ["法力", "攻击力"]} if name == "再生" else {}),
                    "cost_share_target_ref": (
                        [{"ref": ref, "name": entity.name}
                         for ref, entity in self.combat.blood_pact_targets().items()]
                        if self.combat._has_active_blood_pact(player) else []
                    ),
                },
            })
        for actor_ref, actor in refs.items():
            if actor is player or not self.state.on_player_side(actor):
                continue
            for name, instance in actor.dao_wen.items():
                if not instance.can_use():
                    continue
                fixed_x = instance.x_value if instance.x_value > 0 else 1
                # 波及X（固定X）：合法目标不足X时该指令永远无法发动——
                # 与玩家侧同一规则。
                if name == "波及":
                    wave_candidates = [e for e in refs.values()
                                       if e.is_alive and e is not actor]
                    if len(wave_candidates) < fixed_x:
                        actions.append({"action_type": "use_daowen", "available": False,
                                        "params_schema": {"actor_ref": actor_ref,
                                                          "daowen_name": name, "x": fixed_x},
                                        "reason": f"波及{fixed_x}需要{fixed_x}个目标，"
                                                  f"当前仅{len(wave_candidates)}个存活非自身角色"})
                        continue
                actions.append({"action_type": "use_daowen", "available": True,
                                "params_schema": {"actor_ref": actor_ref, "daowen_name": name,
                                                  "x": fixed_x, "target_ref": target_options,
                                                  "dodge": "boolean", "blood_shadow": "boolean",
                                                  "trigger_spell_choices": "complete object"}})
        # 法术大全中的预定义法术也通过统一的自定义施法入口定义；没有额外状态。
        known = {sp.name: sp for sp in player.spells}
        available_builtin = set(self.combat.buildable_spells(player))
        for spell_name in sorted(available_builtin | set(known)):
            spell = known.get(spell_name) or self.combat.spell_definition(player, spell_name)
            if spell is None:
                continue
            usable = all(n in player.dao_wen and player.dao_wen[n].can_use()
                         for n in spell.required_daowen)
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
            # 瞬发法术：一次出手依次发动多种已持有道纹，执行完不留在角色身上。
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
                "note": "瞬发法术=一次出手依次发动多种道纹：只扣1次出手，每一步都算一次发动道纹"
                        "（照常触发敌方目标发动道纹前反应、受缄默面具影响、照付代价）；"
                        "按真实剩余法力逐步结算，某步付不起即中断，已结算的步骤保留、出手不退；"
                        "不写入角色法术列表。kind=daowen 为单道纹发动，kind=spell 为统一自定义施法"})
        if any(value > 0 for value in self.state.resonance.values()):
            actions.append({"action_type": "use_resonance", "params_schema": {
                "resonance_type": [n for n, v in self.state.resonance.items() if v > 0],
                "source_daowen": "道纹名", "target_ref": target_options,
            }})
        for item in self.state.consumables:
            if not item.is_depleted:
                actions.append({"action_type": "consume_item", "params_schema": {
                    "name": item.name, "target_ref": target_options,
                }, "uses_remaining": item.current_uses})
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
            "震岳龙躯": ("activate_dragon_body", {"x": "integer"}),
            "吞骸龙胃": ("devour_monster", {"monster_ref": "enemy:index", "dragon_heart": "name"}),
            "断尾求生": ("declare_tail_sacrifice", {"trait": "owned dragon relic"}),
            "鲜血之翼": ("use_blood_wings", {
                "x": "integer", "cost_share_target_ref": blood_pact_cost_targets}),
            "血族尖牙": ("enslave_as_chizu", {
                "target_ref": target_options,
                "cost_share_target_ref": blood_pact_cost_targets}),
            "真理眼": ("use_truth_eye", {"target_ref": target_options, "statement": "string"}),
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
        # 基础动作【聚能】/【蓄锐】：各消耗 1 次出手。
        # 它们一直存在于引擎执行表中，也必须出现在运行时可用行动里——否则
        # LLM 只能从 available_actions 里选，规则写了却永远选不到。
        actions.extend([
            {"action_type": "focus",
             "params_schema": {"actor_ref": actor_options},
             "available": bool(player is not None and player.is_alive),
             "note": "聚能：消耗1次出手，恢复50%已损法力（向上取整）"},
            {"action_type": "rest",
             "params_schema": {"actor_ref": actor_options},
             "available": bool(player is not None and player.is_alive),
             "note": "蓄锐：消耗1次出手，下[回始]获得【蓄锐·增】（出手+1，持续1回合）"},
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
                "ally_ref": ally_options, "instruction": "语言命令，如：攻击 余烬侍者 / 发动 背负",
                "available": bool(ally_options)}},
            {"action_type": "resolve_ally_phases", "params_schema": {},
             "available": bool(idle_allies),
             "note": "让所有未出手的[朋友]/[员工]自主行动一次（无命令时）"},
            {"action_type": "prepare_monster_phase", "params_schema": {},
             "note": "结束己方行动阶段并进入怪物行动"},
        ])
        return {"phase": subphase, "round": self.state.current_round, "actions": actions}

    def _max_legal_daowen_x(self, actor: Entity, name: str, y: int = 1) -> int:
        """按真实代价口径枚举当前可提交的最大X，不把当前法力直接冒充上限。

        y 为双参数道纹（如【分裂】X/Y）的第二参数，单参数道纹忽略。
        """
        upper = max(1, actor.current_mana, actor.current_hp, actor.blood_limit,
                    actor.current_speed, self.state.shards, self.state.fake_shards, 50)
        legal = 0
        for x in range(1, upper + 1):
            try:
                calc = DaoWenEngine.resolve(name, x, y=y, target=actor, caster=actor)
            except Exception:
                continue
            ctype = calc.get("cost_type")
            if name == "再生":
                mana_calc = DaoWenEngine.resolve(name, x, target=actor, caster=actor, cost_channel="法力")
                attack_calc = DaoWenEngine.resolve(name, x, target=actor, caster=actor, cost_channel="攻击力")
                mana_payable = mana_calc.get("cost", 0) <= actor.current_mana
                attack_payable = attack_calc.get("cost_attack", 0) <= actor.effective_attack_power()
                if not (mana_payable or attack_payable):
                    continue
            elif ctype == "消耗" and calc.get("cost", 0) > actor.current_mana:
                continue
            # 2026-10-13：失忆类代价按持有道纹种数封顶（use_daowen 预检同口径）。
            # 此前 schema 对【点金】X 不设上限、会对【典当】给出永远付不起的X。
            if ctype == "失忆" and calc.get("cost", 0) > len(actor.dao_wen):
                continue
            if ctype in self.combat.SHAREABLE_NUMERIC_COSTS:
                amount = {
                    "流血": calc.get("cost_hp", calc.get("cost", 0)),
                    "衰老": calc.get("cost_blood_limit", calc.get("cost", 0)),
                    "枯竭": calc.get("cost_mana_limit", calc.get("cost", 0)),
                    "萎缩": calc.get("cost_speed_limit", calc.get("cost", 0)),
                    "疲惫": calc.get("cost_speed", calc.get("cost", 0)),
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
            legal = x
        # 波及X：X还不得超过合法目标数（全部存活非自身角色）——否则schema会给出
        # 永远无法发动的X。
        if name == "波及":
            wave_candidates = [e for e in self.combat._combat_entity_refs().values()
                               if e.is_alive and e is not actor]
            legal = min(legal, len(wave_candidates))
        return legal

    def _get_battle_end_actions(self) -> dict:
        round_end_schema = {}
        player = self.state.player
        if (player and self.state.side_has(player, "血族血脉")
                and self.combat._has_active_blood_pact(player)
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
        "declare_parry", "cast", "focus", "rest",
        "deploy_employee", "lianxin_in_battle", "declare_evolution",
        "prepare_monster_phase", "resolve_monster_phase", "monster_phase",
        "round_start", "round_end", "resolve_rebellion_battle",
        # resolve_final_duel不在此列：死斗败者先过死之传承（reset进setup相）
        # 再结算defeat，战斗限定会把它挡在setup外导致擂主永不回槽（r16实战复现）。
        "activate_duel_relic", "use_black_card", "use_crime_vault",
        "fire_godfather_revolver", "select_shared_dragon_heart", "declare_fuyuebei_toll",
        "activate_dragon_body", "devour_monster", "declare_tail_sacrifice",
        "use_dragon_wings", "use_blood_wings", "enslave_as_chizu", "blood_feast",
        "command_ally", "resolve_ally_phases",
    }

    def _restore_state_in_place(self, snapshot: GameState) -> None:
        """事务失败时原位恢复，既回滚数值，也保持调用方已持有的实体引用有效。

        **不可序列化的运行时引用就地保留**：`_hp_engine_ref` 是指向战斗引擎的
        弱引用（Entity.__getstate__ 会剔除它，深拷贝后为 None）。若照搬快照值，
        回滚会把活实体上的绑定覆盖成 None——之后这个实体的降血兜底钩子就哑了。
        Phase 8 随机压力测试实测发现此缺陷；这里按「弱引用不覆盖」处理，
        既不丢绑定，也不给本来没绑定的实体凭空补绑定（那会改变既有行为）。
        """
        def restore_object(current, saved):
            current_keys = set(current.__dict__)
            saved_keys = set(saved.__dict__)
            for key in current_keys - saved_keys:
                delattr(current, key)
            for key in saved_keys:
                if isinstance(current.__dict__.get(key), weakref.ReferenceType):
                    continue     # 活对象上的弱引用绑定不是快照数据，原样保留
                saved_value = getattr(saved, key)
                if not hasattr(current, key):
                    setattr(current, key, copy.deepcopy(saved_value))
                    continue
                current_value = getattr(current, key)
                if (dataclasses.is_dataclass(current_value)
                        and dataclasses.is_dataclass(saved_value)
                        and type(current_value) is type(saved_value)):
                    restore_object(current_value, saved_value)
                elif isinstance(current_value, list) and isinstance(saved_value, list):
                    if (len(current_value) == len(saved_value)
                            and all(dataclasses.is_dataclass(a) and dataclasses.is_dataclass(b)
                                    and type(a) is type(b) for a, b in zip(current_value, saved_value))):
                        for a, b in zip(current_value, saved_value):
                            restore_object(a, b)
                    elif (len(current_value) == len(saved_value)
                            and all(isinstance(a, dict) and isinstance(b, dict)
                                    and set(a) == set(b) for a, b in zip(current_value, saved_value))):
                        # 【封印】暂离队列这类"list[dict]，dict 里装着实体"的结构：
                        # 必须逐键就地还原、实体按同一下标还原回原对象，不能整列表
                        # deepcopy 换掉。否则队列里的实体会和 state.player / enemies
                        # 里的是两个不同对象，回场时把副本塞回去、原对象丢失
                        # （实测：封印自己之后再执行任意一个失败行动就会触发——
                        # 失败回滚把队列换成副本，回场后 state.player 仍是原对象，
                        # 于是玩家永远解不开暂离）。
                        for a, b in zip(current_value, saved_value):
                            for key in list(a):
                                va, vb = a[key], b[key]
                                if (dataclasses.is_dataclass(va) and dataclasses.is_dataclass(vb)
                                        and type(va) is type(vb)):
                                    restore_object(va, vb)
                                else:
                                    a[key] = copy.deepcopy(vb)
                    else:
                        current_value[:] = copy.deepcopy(saved_value)
                elif isinstance(current_value, dict) and isinstance(saved_value, dict):
                    if (set(current_value) == set(saved_value)
                            and all(dataclasses.is_dataclass(current_value[k])
                                    and dataclasses.is_dataclass(saved_value[k])
                                    and type(current_value[k]) is type(saved_value[k])
                                    for k in current_value)):
                        for k in current_value:
                            restore_object(current_value[k], saved_value[k])
                    else:
                        current_value.clear()
                        current_value.update(copy.deepcopy(saved_value))
                else:
                    setattr(current, key, copy.deepcopy(saved_value))

        restore_object(self.state, snapshot)

    def _snapshot_combat_runtime(self) -> dict:
        """保存不在GameState内、但会被行动修改的战斗运行态，供失败事务回滚。"""
        groups = {
            "player": [self.state.player] if self.state.player else [],
            "friend": self.state.friends,
            "employee": self.state.employees,
            "temp_friend": self.state.temp_friends,
            "enemy": self.state.enemies,
        }
        refs = {(kind, i): entity for kind, entities in groups.items() for i, entity in enumerate(entities)}
        by_id = {id(entity): ref for ref, entity in refs.items()}
        return {
            "activated": {by_id[key]: set(value) for key, value in self.combat._monster_activated.items()
                          if key in by_id},
            "rewrites": {by_id[key]: dict(value) for key, value in self.combat._resonance_rewrites.items()
                         if key in by_id},
        }

    def _restore_combat_runtime(self, snapshot: dict) -> None:
        groups = {
            "player": [self.state.player] if self.state.player else [],
            "friend": self.state.friends,
            "employee": self.state.employees,
            "temp_friend": self.state.temp_friends,
            "enemy": self.state.enemies,
        }
        refs = {(kind, i): entity for kind, entities in groups.items() for i, entity in enumerate(entities)}
        self.combat._monster_activated = {
            id(refs[ref]): set(value) for ref, value in snapshot["activated"].items() if ref in refs
        }
        self.combat._resonance_rewrites = {
            id(refs[ref]): dict(value) for ref, value in snapshot["rewrites"].items() if ref in refs
        }

    def _roll_back_monster_phase_pending(self) -> None:
        """回滚怪物阶段pending token。

        普通提交失败（非法选择等）保持pending有效——战斗状态已整体回滚，
        prepare快照仍然成立，AI用同一token修正后重交即可（既有契约，见
        test_r11_r17_aoe_and_control_dodge_are_explicit 的同token重提）。
        只有快照本身失效（回合变化等）时才清除token并还原子阶段，
        使错误提示里的"重新prepare_monster_phase"真正可执行。
        """
        self.state.pending_monster_phase = {}
        if self.state.phase == "in_combat":
            self.state.combat_subphase = CombatSubphase.PLAYER_ACTIONS.value

    def _phase_error(self, action_type: str, params: dict) -> Optional[dict]:
        """执行入口的阶段门禁；【消灾】是唯一允许在局外发动的道纹。"""
        phase = self.state.phase
        if self.state.pending_daowen_choices and action_type != "choose_hired_daowen":
            return {"success": False,
                    "error": "雇佣后的转化道纹尚未选择，不能执行其它行动"}
        if (self.event_pool.current is not None
                and action_type not in {"resolve_event", "choose_discovered_relic", "choose_discovered_item"}):
            return {"success": False,
                    "error": f"事件【{self.event_pool.current}】尚未结算，不能执行其它行动"}
        if action_type.startswith("setup_") and phase != "setup":
            return {"success": False, "error": f"【{action_type}】只能在开局阶段执行"}
        if action_type.startswith("world_") and phase != "pre_battle":
            return {"success": False,
                    "error": "当前开放世界行动尚处于第一阶段，只能在局外阶段调用；战斗/世界统一主循环尚未接通"}
        if action_type in ("pre_battle_action", "resolve_event", "upgrade_doctor") and phase != "pre_battle":
            return {"success": False, "error": "局外行动/事件只能在局外阶段执行"}
        if action_type == "battle_start" and phase != "pre_battle":
            return {"success": False, "error": "只有局外阶段可以进入战始"}
        if action_type == "battle_start" and self.state.energy > 0:
            return {"success": False, "error": f"尚有{self.state.energy}点精力，耗尽后才能进入战斗"}
        if action_type == "use_daowen":
            name = params.get("daowen_name", "")
            if phase == "pre_battle" and name == "消灾":
                return None
            if phase != "in_combat":
                return {"success": False, "error": "道纹只能在战斗中发动；局外唯一例外是【消灾】"}
        if action_type == "restart_battle" and phase != "in_combat":
            return {"success": False, "error": "只有进行中的战斗可以重来"}
        if action_type in self._COMBAT_ONLY_ACTIONS and phase != "in_combat":
            return {"success": False, "error": f"【{action_type}】只能在战斗中执行"}
        if action_type == "battle_end" and phase != "in_combat":
            return {"success": False, "error": "只有进行中的战斗可以结算战终"}
        if phase == "in_combat":
            subphase = self.state.combat_subphase
            required = {
                "round_start": CombatSubphase.AWAIT_ROUND_START.value,
                "prepare_monster_phase": CombatSubphase.PLAYER_ACTIONS.value,
                "resolve_monster_phase": CombatSubphase.MONSTER_ACTIONS.value,
                "round_end": CombatSubphase.AWAIT_ROUND_END.value,
                "prepare_attack": CombatSubphase.PLAYER_ACTIONS.value,
                "resolve_attack": CombatSubphase.PLAYER_ACTIONS.value,
                "cast": CombatSubphase.PLAYER_ACTIONS.value,
                "focus": CombatSubphase.PLAYER_ACTIONS.value,
                "rest": CombatSubphase.PLAYER_ACTIONS.value,
            }.get(action_type)
            player_actions = {
                "declare_parry",
                "use_daowen", "use_resonance", "consume_item",
                "cast", "focus", "rest",
                "declare_wish", "declare_escape", "deploy_employee",
                "define_spell", "undefine_spell",
                "lianxin_in_battle", "declare_evolution", "activate_duel_relic",
                "use_black_card", "use_crime_vault", "fire_godfather_revolver",
                "select_shared_dragon_heart", "declare_fuyuebei_toll", "activate_dragon_body",
                "devour_monster", "declare_tail_sacrifice", "use_dragon_wings",
                "use_blood_wings", "enslave_as_chizu", "blood_feast",
                # 轮回者指挥与友方自主行动都属于己方行动阶段，不能在怪物阶段插队。
                "command_ally", "resolve_ally_phases",
            }
            if action_type in player_actions:
                required = CombatSubphase.PLAYER_ACTIONS.value
            if action_type == "consume_item" and params.get("name") == "活性土壤":
                required = CombatSubphase.AWAIT_ROUND_START.value
            if action_type in ("use_black_card", "select_shared_dragon_heart",
                               "use_crime_vault", "use_dragon_wings"):
                required = CombatSubphase.AWAIT_ROUND_START.value
            if action_type == "activate_duel_relic" and self.state.in_final_duel:
                required = CombatSubphase.AWAIT_ROUND_START.value
            if action_type == "declare_escape" and self.state.in_final_duel:
                required = None
            if required and subphase != required:
                return {"success": False,
                        "error": f"【{action_type}】要求战斗子阶段{required}，当前为{subphase}"}
        return None

    def execute_action(self, action_type: str, params: dict = None) -> dict:
        """正式执行行动的统一入口（事务层）。

        AI通过此接口执行所有行动。返回格式：
        {
            "success": bool,
            "action": str,
            "result": dict,      # 行动结果
            "state_update": dict, # 状态变化
            "interrupt": dict,   # 如果触发中断，此处不为空
            "next_actions": list  # 接下来可用的行动
        }

        正式执行与 ActionPreview 预演共用同一个实现 `_execute_action_core`
        。
        本入口是两者**唯一**的区别所在：改状态前保存快照，失败/异常时整体回滚。
        """
        return self._execute_action_core(action_type, params, transaction=True)

    def _execute_action_core(self, action_type: str, params: dict = None,
                             *, transaction: bool = False) -> dict:
        """动作执行的唯一实现：门禁 → dispatch → 记账/中断。

        transaction=True（正式执行）在 dispatch 前保存快照，失败/异常时原地回滚；
        transaction=False（ActionPreview 的 sandbox）跳过快照与回滚——sandbox 用后
        即弃，规则逻辑与正式执行逐行相同。

        Preview 的语义差别仅有这一处：失败回滚不会发生在 sandbox 内。引擎所有
        内部调用方（实验/模拟工具，如 sim 与 TacticalAI 对照脚本）只读取 `success=True` 的预演 diff，
        失败候选一律跳过，故对外可观测行为不变（见 tests/test_ai_safety_preview.py）。
        """
        if params is None:
            params = {}

        if (self.state.pending_initial_daowen_choices
                and action_type != "setup_choose_initial_daowen"):
            return {
                "success": False,
                "error": "必须先从杀伐闭环发现候选中选择1种初始道纹",
                "choices": list(self.state.pending_initial_daowen_choices),
            }
        if self.state.pending_redemption and action_type != "resolve_redemption":
            return {
                "success": False,
                "error": "必须先结算【救赎】：接纳或终结",
                "pending": dict(self.state.pending_redemption),
            }
        # 2026-10-12 用户裁定③：强制遗物发现在额度满时也要能脱困，故「放弃本次发现」
        # 与「选择」一样属于该门禁下的合法动作。少了它，额度满 + 有强制发现 = 死局
        # （两边都不许动，且没有任何动作能清空 pending_relic_choices）。
        if (self.state.pending_relic_choices
                and action_type not in ("choose_discovered_relic",
                                        "discard_discovered_relic")):
            return {
                "success": False,
                "error": "必须先从发现候选中选择1件遗物（额度已满时可改用"
                         " discard_discovered_relic 放弃，或在选择时带 replace 参数换掉一件）",
                "choices": list(self.state.pending_relic_choices),
            }
        if self.state.pending_item_choices and action_type != "choose_discovered_item":
            return {
                "success": False,
                "error": "必须先从发现候选中选择1件消耗品",
                "choices": list(self.state.pending_item_choices),
            }
        if self.state.pending_attack and action_type != "resolve_attack":
            return {
                "success": False,
                "error": "已有待提交的攻击决策，请先调用resolve_attack",
                "token": self.state.pending_attack.get("token"),
                "recoverable": True,
                "instruction": "攻击pending仍有效（上次提交失败已被整体回滚）；"
                               "用上方token重新调用resolve_attack提交完整选择",
            }
        if self.state.pending_monster_phase and action_type not in {"resolve_monster_phase", "restart_battle"}:
            return {
                "success": False,
                "error": "已有待提交的怪物阶段决策，请先调用resolve_monster_phase",
                "token": self.state.pending_monster_phase.get("token"),
                "recoverable": True,
                "instruction": "怪物阶段pending仍有效（上次提交失败已被整体回滚，"
                               "prepare快照未变）；用上方token重新调用"
                               "resolve_monster_phase提交完整选择",
            }
        # 检查是否有待处理的中断
        # 豁免：自创法术 dm_approved 重提是"结算中断"的动作，不该被自己挡住
        # 注：旧 is_custom_approve 旁路口（局外自创法术的 dm_approved 重提）
        # 已于 2026-09-16 随局外自创入口一并删除，不再需要豁免待裁定门禁。
        if self._pending_interrupts and action_type != "restart_battle":
            player_dead = (self.state.player is None) or (not self.state.player.is_alive)
            return {
                "success": False,
                "error": "有待处理的中断等待DM裁定",
                "pending_interrupts": [i.to_dict() for i in self._pending_interrupts],
                "result": {"player_dead": player_dead},
                "instruction": "请先通过 submit_ruling() 提交DM裁定"
            }

        queued = self._queue_death_inheritance_if_needed(action_type)
        if queued:
            return {
                "success": False,
                "error": "轮回者已命零，死之传承等待审核",
                "interrupt": queued.to_dict(),
                "pending_interrupts": [i.to_dict() for i in self._pending_interrupts],
                "result": {"player_dead": True},
                "instruction": "请先通过 submit_ruling() 审核遗言（approve/edit/reject）",
            }

        phase_error = self._phase_error(action_type, params)
        if phase_error:
            return phase_error

        state_before = None
        if transaction:
            # 所有行动按“校验失败不改变游戏状态”的原子契约执行。
            # 随机数请求必须在各处理器完成静态校验后才发起，避免失败消耗随机源。
            state_before = copy.deepcopy(self.state)
            combat_runtime_before = self._snapshot_combat_runtime()
            event_pool_before = (set(self.event_pool.triggered), self.event_pool.current)
            dice_before = copy.deepcopy(self.dice)
            interrupts_before = copy.deepcopy(self._pending_interrupts)
        # 结算生命周期：一次顶层行动 = 一次 resolution。
        # 只做计数清零与（可选的）链记录，不参与任何规则判定——见 engine/resolution.py。
        # 预演内部也走本函数，但它是沙盒执行：context 状态由 engine/sandbox.py
        # 在进出沙盒时保存/恢复，所以预演不会污染真实行动的预算/深度。
        # 预算随在场实体数放大：结算量天然与战场规模成正比（见 ResolutionContext）。
        try:
            _scale = len(self.combat._hp_record_entities())
        except Exception:          # 兜底：拿不到就按最小规模算
            _scale = 1
        self.combat.resolution.begin_action(action_type, params, scale=_scale)
        try:
            result = self._dispatch_action(action_type, params)

            if not result.get("success", False) and transaction:
                self._restore_state_in_place(state_before)
                self.combat.state = self.state
                self._restore_combat_runtime(combat_runtime_before)
                self.event_pool.triggered, self.event_pool.current = event_pool_before
                self.dice = dice_before
                self.combat.dice = self.dice
                self._pending_interrupts = interrupts_before
                # 两阶段提交失败：状态已整体回滚，pending保持有效——
                # 失败提交不得把战斗锁死。
                if action_type == "resolve_monster_phase" and self.state.pending_monster_phase:
                    result.setdefault("recoverable", True)
                    result.setdefault("token", self.state.pending_monster_phase.get("token"))
                    result.setdefault("instruction",
                                      "提交被拒绝且已整体回滚（零副作用），token仍有效；"
                                      "请修正选择后用同一token重新调用resolve_monster_phase")
                elif action_type == "resolve_attack" and self.state.pending_attack:
                    result.setdefault("recoverable", True)
                    result.setdefault("token", self.state.pending_attack.get("token"))
                    result.setdefault("instruction",
                                      "提交被拒绝且已整体回滚（零副作用），token仍有效；"
                                      "请修正选择后用同一token重新调用resolve_attack")

            # 记录行动历史
            self._action_history.append({
                "action": action_type,
                "params": params,
                "result": result,
                "timestamp": time.time(),
                "game_id": self.state.game_id,
                "round": self.state.current_round
            })

            # 自动校验（如果启用）
            validation_result = None
            if self._validator and result.get("success"):
                validation_result = self._validator.validate(
                    self.state,
                    {"action": action_type, "params": params},
                    result
                )
                if validation_result.get("violations"):
                    result["validation_violations"] = validation_result["violations"]
                if validation_result.get("warnings"):
                    result["validation_warnings"] = validation_result["warnings"]
                result["validation_passed"] = validation_result.get("valid", True)

            queued = self._queue_death_inheritance_if_needed(action_type)
            if queued:
                result["interrupt"] = queued.to_dict()
                result["instruction"] = (
                    (result.get("instruction") or "")
                    + " 轮回者命零，死之传承等待审核；请调用 submit_ruling(approve/edit/reject)。"
                ).strip()

            self._last_result = result
            self.combat.resolution.end_action()
            if transaction and self.autosave and result.get("success"):
                try:
                    self.save_game("current")
                    result["autosaved"] = True
                except Exception as exc:
                    # 存档失败不得回滚已经成功的游戏行动；把失败明确暴露给调用方。
                    result["autosaved"] = False
                    result["autosave_error"] = str(exc)
            return result

        except Exception as e:
            self.combat.resolution.end_action()
            if transaction:
                self._restore_state_in_place(state_before)
                self.combat.state = self.state
                self._restore_combat_runtime(combat_runtime_before)
                self.event_pool.triggered, self.event_pool.current = event_pool_before
                self.dice = dice_before
                self.combat.dice = self.dice
                self._pending_interrupts = interrupts_before
            # 引擎异常：状态已整体回滚，pending保持有效（与success=False路径一致），
            # 失败提交不得把战斗锁死——保留token供修正后重交。
            error_result = {
                "success": False,
                "error": str(e),
                "action": action_type,
                "instruction": "引擎计算出错，请检查参数；"
                               "若发生在两阶段提交中，token仍有效，"
                               "修正后用同一token重新提交"
            }
            if action_type == "resolve_monster_phase" and self.state.pending_monster_phase:
                error_result["recoverable"] = True
                error_result["token"] = self.state.pending_monster_phase.get("token")
            elif action_type == "resolve_attack" and self.state.pending_attack:
                error_result["recoverable"] = True
                error_result["token"] = self.state.pending_attack.get("token")
            self._last_result = error_result
            return error_result

    def _dispatch_action(self, action_type: str, params: dict) -> dict:
        """action 名 → 处理器：唯一的 action 路由表（执行实现见各 _action_*）。"""
        if action_type == "world_initialize":
            return self._action_world_initialize(params)
        elif action_type == "world_move":
            return self._action_world_move(params)
        elif action_type == "world_wait":
            return self._action_world_wait(params)
        elif action_type == "world_status":
            return self._action_world_status(params)
        elif action_type == "world_vision":
            return self._action_world_vision(params)
        elif action_type == "world_interact":
            return self._action_world_interact(params)
        elif action_type == "setup_attributes":
            return self._action_setup_attributes(params)
        elif action_type == "setup_choose_region":
            return self._action_setup_choose_region(params)
        elif action_type == "setup_choose_resonance":
            return self._action_setup_choose_resonance(params)
        elif action_type == "setup_choose_initial_daowen":
            return self._action_setup_choose_initial_daowen(params)
        elif action_type == "resolve_redemption":
            return self._action_resolve_redemption(params)
        elif action_type == "pre_battle_action":
            return self._action_pre_battle(params)
        elif action_type == "upgrade_doctor":
            return self._action_upgrade_doctor(params)
        elif action_type == "use_daowen":
            return self._action_use_daowen(params)
        # 2026-09-28 用户令：回合动作重构
        #   attack  → 普攻（等价 prepare_attack+resolve_attack 单步封装；或走prepare/resolve两步）
        #   cast    → 施法（=发动道纹或瞬发自定义施法）
        #   focus   → 聚能：消耗1出手，恢复50%已损法力（向上取整）
        #   rest    → 蓄锐：消耗1出手，下回合出手+1
        elif action_type == "cast":
            return self._action_cast(params)
        elif action_type == "focus":
            return self._action_focus(params)
        elif action_type == "rest":
            return self._action_rest(params)
        elif action_type == "define_spell":
            return self._action_define_spell(params)
        elif action_type == "undefine_spell":
            return self._action_undefine_spell(params)
        elif action_type == "use_resonance":
            return self._action_use_resonance(params)
        elif action_type == "redeem_attribute_points":
            return self._action_redeem_attribute_points(params)
        elif action_type == "declare_parry":
            return self._action_declare_parry(params)
        elif action_type == "prepare_attack":
            return self._action_prepare_attack(params)
        elif action_type == "resolve_attack":
            return self._action_resolve_attack(params)
        elif action_type == "attack":
            return {"success": False, "error": "旧attack已移除；请使用prepare_attack/resolve_attack"}
        elif action_type == "consume_item":
            return self._action_consume_item(params)
        elif action_type == "declare_wish":
            return self._action_declare_wish(params)
        elif action_type == "declare_escape":
            return self._action_declare_escape(params)
        elif action_type == "choose_diandang_daowen":
            return self._action_choose_diandang_daowen(params)
        elif action_type == "lianxin_in_battle":
            return self._action_lianxin_in_battle(params)
        elif action_type == "declare_evolution":
            return self._action_declare_evolution(params)
        elif action_type == "deploy_employee":
            return self._action_deploy_employee(params)
        elif action_type == "dismiss_employee":
            return self._action_dismiss_employee(params)
        elif action_type == "pay_employee_wage":
            return self._action_pay_employee_wage(params)
        elif action_type == "choose_hired_daowen":
            return self._action_choose_hired_daowen(params)
        elif action_type == "choose_sha_qi":
            return self._action_choose_sha_qi(params)
        elif action_type == "suppress_rebellion":
            return self._action_suppress_rebellion(params)
        elif action_type == "resolve_rebellion_battle":
            return self._action_resolve_rebellion_battle(params)
        elif action_type == "appease_rebellion":
            return self._action_appease_rebellion(params)
        elif action_type == "negotiate_rebellion":
            return self._action_negotiate_rebellion(params)
        elif action_type == "activate_duel_relic":
            return self._action_activate_duel_relic(params)
        elif action_type == "resolve_final_duel":
            return self._action_resolve_final_duel(params)
        elif action_type == "choose_terminal_artifact":
            return self._action_choose_terminal_artifact(params)
        elif action_type == "choose_first_embrace":
            return self._action_choose_first_embrace(params)
        elif action_type == "use_black_card":
            return self._action_use_black_card(params)
        elif action_type == "use_crime_vault":
            return self._action_use_crime_vault(params)
        elif action_type == "fire_godfather_revolver":
            return self._action_fire_godfather_revolver(params)
        elif action_type == "select_shared_dragon_heart":
            return self._action_select_shared_dragon_heart(params)
        elif action_type == "declare_fuyuebei_toll":
            return self._action_declare_fuyuebei_toll(params)
        elif action_type == "pay_for_dragon_nature":
            return self._action_pay_for_dragon_nature(params)
        elif action_type == "unlock_dragon_trait":
            return self._action_unlock_dragon_trait(params)
        elif action_type == "activate_dragon_body":
            return self._action_activate_dragon_body(params)
        elif action_type == "devour_monster":
            return self._action_devour_monster(params)
        elif action_type == "declare_tail_sacrifice":
            return self._action_declare_tail_sacrifice(params)
        elif action_type == "use_dragon_wings":
            return self._action_use_dragon_wings(params)
        elif action_type == "use_blood_wings":
            return self._action_use_blood_wings(params)
        elif action_type == "enslave_as_chizu":
            return self._action_enslave_as_chizu(params)
        elif action_type == "use_truth_eye":
            return self._action_use_truth_eye(params)
        elif action_type == "blood_feast":
            return self._action_blood_feast(params)
        elif action_type == "command_ally":
            return self._action_command_ally(params)
        elif action_type == "resolve_ally_phases":
            return self._action_resolve_ally_phases(params)
        elif action_type == "prepare_monster_phase":
            return self._action_prepare_monster_phase(params)
        elif action_type == "resolve_monster_phase":
            return self._action_resolve_monster_phase(params)
        elif action_type == "monster_phase":
            return self._action_monster_phase(params)
        elif action_type == "choose_discovered_relic":
            return self._action_choose_discovered_relic(params)
        elif action_type == "discard_discovered_relic":
            return self._action_discard_discovered_relic(params)
        elif action_type == "choose_discovered_item":
            return self._action_choose_discovered_item(params)
        elif action_type == "repay_debt_employee":
            return self._action_repay_debt_employee(params)
        elif action_type == "round_start":
            return self._action_round_start(params)
        elif action_type == "round_end":
            return self._action_round_end(params)
        elif action_type == "restart_battle":
            return self._action_restart_battle(params)
        elif action_type == "battle_start":
            return self._action_battle_start(params)
        elif action_type == "battle_end":
            return self._action_battle_end(params)
        elif action_type == "resolve_event":
            return self._action_resolve_event(params)
        elif action_type == "read_death_book":
            return self._action_read_death_book(params)
        else:
            return {"success": False, "error": f"未知行动类型: {action_type}"}

    # ==================== 开放世界基础行动 ====================

    def _world_from_state(self) -> WorldState:
        if not self.state.world_state:
            raise ValueError("开放世界尚未初始化，请先执行world_initialize")
        return WorldState.from_dict(self.state.world_state)

    def _sync_world_entity_positions(self, world: WorldState) -> None:
        """将地图中的权威位置同步回实体存档字段，避免移动只存在于地图副本。"""
        entities = []
        if self.state.player is not None:
            entities.append(self.state.player)
        for group_name in ("friends", "employees", "temp_friends", "enemies"):
            entities.extend(getattr(self.state, group_name, []) or [])
        if getattr(self.state, "merchant", None) is not None:
            entities.append(self.state.merchant)
        by_id = {getattr(entity, "runtime_id", ""): entity
                 for entity in entities if getattr(entity, "runtime_id", "")}
        for actor in world.actors.values():
            entity = by_id.get(actor.actor_id)
            if entity is None:
                continue
            entity.world_region = self.state.current_region
            entity.world_x, entity.world_y = actor.x, actor.y
            entity.is_neutral = actor.faction == "neutral"

    def _open_adjacent_merchant_session(self, world: WorldState) -> bool:
        """玩家与过路商人正交相邻时开启现有交易会话；商人不进入随机事件池。"""
        if self.event_pool.current is not None or "过路商人" not in self.event_pool.events:
            return False
        player = next((actor for actor in world.actors.values()
                       if actor.kind == "player" and actor.alive), None)
        if player is None:
            return False
        if any(encounter.active and player.actor_id in encounter.participants
               for encounter in world.encounters.values()):
            return False
        merchant = next((actor for actor in world.actors.values()
                         if actor.kind == "merchant" and actor.alive
                         and abs(actor.x - player.x) + abs(actor.y - player.y) == 1), None)
        if merchant is None:
            return False
        self.event_pool.current = "过路商人"
        world.record("merchant_encountered", player.actor_id,
                     merchant_actor_id=merchant.actor_id, position=list(merchant.position))
        return True

    def _action_world_initialize(self, params: dict) -> dict:
        """创建可存档的网格世界，并将当前存活单位登记为独立地图单位。"""
        if self.state.player is None:
            return {"success": False, "error": "请先完成角色开局，再初始化开放世界"}
        if getattr(self.state, "world_state", {}):
            return {"success": True, "action": "world_initialize",
                    "result": {"already_initialized": True,
                               "world_turn": self.state.world_state.get("clock", {}).get("turn", 0)}}
        width = params.get("width", 21)
        height = params.get("height", 21)
        if not isinstance(width, int) or isinstance(width, bool) or not isinstance(height, int) or isinstance(height, bool):
            return {"success": False, "error": "地图宽高必须是整数"}
        # 地图种子通过引擎骰子产生并记录，不让AI直接伪造随机结果。
        seed_roll = self.dice.auto_roll("open_world_map_seed", list(range(10000)),
                                        context="开放世界地图生成种子")
        seed = seed_roll["record"]["selected_index"]
        try:
            world = WorldState.create(width=width, height=height, seed=seed)
            spawned_monsters = []
            if not self.state.enemies:
                # 开放世界把当前副本怪物池中的实体生成到地图；不是每次遭遇才临时抽怪。
                from .monsters import make_monster_entity
                for monster_def in self._monster_pool_for_battle():
                    monster = make_monster_entity(monster_def)
                    self.combat.init_monster_shards(monster)
                    self.state.enemies.append(monster)
                    spawned_monsters.append(monster)
            entities = []
            if self.state.player is not None:
                entities.append((self.state.player, "player", "player"))
            for kind, group in (("friend", self.state.friends), ("employee", self.state.employees),
                                ("temp_friend", self.state.temp_friends), ("enemy", self.state.enemies)):
                entities.extend((entity, kind, "monster" if entity.entity_type == "怪物" else kind)
                                for entity in group if entity.is_alive)
            if self.state.merchant is not None:
                entities.append((self.state.merchant, "merchant", "neutral"))
            candidates = [(x, y) for y in range(height) for x in range(width)
                          if world.map.is_passable((x, y))]
            # 轮回者固定从安全起点入场；友方在附近，敌对单位与商人通过引擎骰子随机分散到远处。
            used = set()
            player_start = (1, 1)
            for entity, kind, faction in entities:
                if kind == "player":
                    pos = player_start
                else:
                    available = [p for p in candidates if p not in used]
                    if kind in {"friend", "employee", "temp_friend"}:
                        nearby = [p for p in available
                                  if abs(p[0] - 1) + abs(p[1] - 1) <= 3]
                        choices = nearby or available
                    else:
                        min_distance = max(4, min(width, height) // 4)
                        distant = [p for p in available
                                   if abs(p[0] - 1) + abs(p[1] - 1) >= min_distance]
                        choices = distant or available
                    if not choices:
                        raise ValueError("地图可通行格不足，无法放置全部单位")
                    placement_roll = self.dice.auto_roll(
                        f"open_world_spawn_{kind}_{entity.name}_{len(used)}", choices,
                        context=f"开放世界单位随机出生：{entity.name}")
                    pos = tuple(placement_roll["selected"])
                if pos in used:
                    raise ValueError("随机出生位置发生重叠，请重新初始化地图")
                used.add(pos)
                actor_id = getattr(entity, "runtime_id", "") or f"{kind}:{entity.name}:{len(used)}"
                mode = "manual" if kind in {"player", "friend", "employee", "temp_friend"} else "autonomous"
                world.add_actor(WorldActorState(actor_id=actor_id, name=entity.name, kind=kind,
                                                x=pos[0], y=pos[1], alive=entity.is_alive,
                                                faction=faction, mode=mode))
                if kind == "player":
                    world.record("world_entered", actor_id, region=self.state.current_region)
                elif entity in spawned_monsters:
                    world.record("monster_spawned", actor_id, name=entity.name,
                                 position=list(pos), region=self.state.current_region)
            # 将当前副本可用的地点型事件放入真实地图格；纯状态/战斗触发事件不伪装成地点。
            non_location_events = {"过路商人", "都市的终音", "初拥之夜"}
            location_names = [name for name in self.event_pool.build_pool(self.state.current_region)
                              if name not in non_location_events]
            available = [pos for pos in candidates if pos not in used]
            for event_name in location_names:
                if not available:
                    break
                roll = self.dice.auto_roll(
                    f"open_world_location_{event_name}", available,
                    context=f"开放世界地点放置：{event_name}")
                pos = tuple(roll["selected"])
                world.map.add_location(event_name, {
                    "kind": "event", "name": event_name, "event_name": event_name,
                    "x": pos[0], "y": pos[1], "discovered": False, "active": True})
                available.remove(pos)
            self._sync_world_entity_positions(world)
            self.state.world_state = world.to_dict()
            return {"success": True, "action": "world_initialize",
                    "result": {"width": width, "height": height,
                               "world_turn": world.clock.turn,
                               "spawned_monsters": len(spawned_monsters),
                               "event_locations": len(world.map.locations)}}
        except (ValueError, KeyError, TypeError) as exc:
            return {"success": False, "error": str(exc)}

    def _action_world_move(self, params: dict) -> dict:
        try:
            world = self._world_from_state()
            actor_id = params.get("actor_id")
            player_id = next((key for key, actor in world.actors.items() if actor.kind == "player"), None)
            actor_id = params.get("actor_id") or player_id
            if actor_id is None or actor_id != player_id:
                return {"success": False, "error": "当前API只允许轮回者控制自己的地图单位"}
            x, y = params.get("x"), params.get("y")
            if not isinstance(x, int) or isinstance(x, bool) or not isinstance(y, int) or isinstance(y, bool):
                return {"success": False, "error": "目标坐标x/y必须是整数"}
            result = world.move_actor(actor_id, (x, y))
            self._sync_world_entity_positions(world)
            location_entry = world.map.location_at((x, y))
            if location_entry is not None:
                location_id, location = location_entry
                if location.get("active", True):
                    location["discovered"] = True
                    event_name = location.get("event_name", location.get("name", location_id))
                    if (self.event_pool.current is None and event_name in self.event_pool.events
                            and (event_name == "祭坛" or event_name not in self.event_pool.triggered)):
                        self.event_pool.current = event_name
                        world.record("location_discovered", actor_id, location_id=location_id,
                                     location_name=location.get("name", location_id),
                                     event_name=event_name, position=[x, y])
                        result["location_event"] = event_name
            # 商人自己走近玩家也会建立接触；只在实际相邻时打开交易会话。
            self._open_adjacent_merchant_session(world)
            self.state.world_state = world.to_dict()
            return {"success": True, "action": "world_move", "result": result}
        except (ValueError, KeyError, TypeError) as exc:
            return {"success": False, "error": str(exc)}

    def _action_world_wait(self, params: dict) -> dict:
        try:
            world = self._world_from_state()
            actor_id = params.get("actor_id")
            player_id = next((key for key, actor in world.actors.items() if actor.kind == "player"), None)
            actor_id = params.get("actor_id") or player_id
            if actor_id is None or actor_id != player_id:
                return {"success": False, "error": "当前API只允许轮回者控制自己的地图单位"}
            turns = params.get("turns", 1)
            start_turn = world.clock.turn
            world.wait(actor_id, turns)
            elapsed = world.clock.turn - start_turn
            active_encounter = next((encounter for encounter in world.encounters.values()
                                     if encounter.active and actor_id in encounter.participants), None)
            self._sync_world_entity_positions(world)
            if active_encounter is None:
                self._open_adjacent_merchant_session(world)
            self.state.world_state = world.to_dict()
            return {"success": True, "action": "world_wait",
                    "result": {"world_turn": world.clock.turn, "turns": elapsed,
                               "requested_turns": turns,
                               "interrupted_by_encounter": active_encounter is not None,
                               "encounter_id": active_encounter.encounter_id if active_encounter else None}}
        except (ValueError, KeyError, TypeError) as exc:
            return {"success": False, "error": str(exc)}

    def _action_world_status(self, params: dict) -> dict:
        """只返回玩家已知信息，不把全地图、隐藏单位或完整事实账本交给AI。"""
        try:
            world = self._world_from_state()
            actor_id = params.get("actor_id")
            player_id = next((key for key, actor in world.actors.items() if actor.kind == "player"), None)
            actor_id = params.get("actor_id") or player_id
            if actor_id is None or actor_id != player_id or actor_id not in world.actors:
                return {"success": False, "error": "只能查询轮回者自己的地图情报"}
            visible = set(world.visible_cells(actor_id))
            self._sync_world_entity_positions(world)
            actor = world.actors[actor_id]
            known_terrain = [
                {"x": x, "y": y, "terrain": world.map.terrain_at((x, y))}
                for key in sorted(actor.known_cells)
                for x, y in [world.map.parse(key)]
            ]
            visible_actors = [
                {"actor_id": other.actor_id, "name": other.name, "kind": other.kind,
                 "faction": other.faction, "position": [other.x, other.y]}
                for other in world.actors.values()
                if other.alive and other.actor_id != actor_id and other.position in visible
            ]
            visible_encounters = [
                {"encounter_id": e.encounter_id, "participants": list(e.participants),
                 "location": list(e.location), "started_turn": e.started_turn}
                for e in world.encounters.values()
                if e.active and actor_id in e.participants
            ]
            known_locations = [
                {"location_id": location_id, "name": location.get("name", location_id),
                 "kind": location.get("kind", "special"),
                 "position": [location.get("x"), location.get("y")],
                 "active": location.get("active", True)}
                for location_id, location in world.map.locations.items()
                if location.get("discovered", False)
            ]
            self.state.world_state = world.to_dict()
            return {"success": True, "action": "world_status", "result": {
                "world_turn": world.clock.turn,
                "map_size": {"width": world.map.width, "height": world.map.height},
                "self": {"actor_id": actor.actor_id, "name": actor.name,
                         "position": [actor.x, actor.y], "alive": actor.alive},
                "known_terrain": known_terrain,
                "visible_actors": visible_actors,
                "visible_encounters": visible_encounters,
                "known_locations": known_locations,
            }}
        except (ValueError, KeyError) as exc:
            return {"success": False, "error": str(exc)}

    def _action_world_interact(self, params: dict) -> dict:
        """与实际相邻的地图单位交互；只记录已发生的意图，不替对方伪造同意。"""
        try:
            world = self._world_from_state()
            player_id = next((key for key, actor in world.actors.items()
                              if actor.kind == "player" and actor.alive), None)
            target_id = params.get("target_actor_id")
            choice = params.get("choice")
            allowed = {"talk", "trade", "cooperate", "intimidate", "attack"}
            if choice not in allowed:
                return {"success": False, "error": f"choice必须是{sorted(allowed)}之一"}
            if not player_id or not isinstance(target_id, str) or target_id not in world.actors:
                return {"success": False, "error": "必须指定一个真实存在的目标单位"}
            if target_id == player_id:
                return {"success": False, "error": "不能与自己交互"}
            if any(encounter.active and player_id in encounter.participants
                   for encounter in world.encounters.values()):
                return {"success": False, "error": "轮回者正处于遭遇中，不能开启普通交互"}
            player, target = world.actors[player_id], world.actors[target_id]
            if not target.alive:
                return {"success": False, "error": "目标单位已离场"}
            if abs(player.x - target.x) + abs(player.y - target.y) != 1:
                return {"success": False, "error": "目标必须位于上下左右相邻格"}
            if any(encounter.active and target_id in encounter.participants
                   for encounter in world.encounters.values()):
                return {"success": False, "error": "目标正处于遭遇中，不能开启普通交互"}
            if choice == "trade":
                if target.kind != "merchant":
                    world.record("trade_requested", player_id, target_actor_id=target_id,
                                 target_name=target.name)
                    self.state.world_state = world.to_dict()
                    return {"success": True, "action": "world_interact",
                            "result": {"choice": choice, "target": target.name,
                                       "pending_counterparty_consent": True,
                                       "completed": False},
                            "instruction": "已记录交易请求；轮回者间双边报价与对方同意流程尚未实现，不能直接转移资产"}
                if self.event_pool.current is not None:
                    return {"success": False, "error": "当前已有待结算事件，不能开启商人交易"}
                self.event_pool.current = "过路商人"
                world.record("merchant_encountered", player_id, merchant_actor_id=target_id,
                             position=list(target.position))
                self.state.world_state = world.to_dict()
                return {"success": True, "action": "world_interact",
                        "result": {"choice": choice, "target": target.name,
                                   "session": "过路商人"}}
            if choice == "attack":
                if target.kind != "enemy":
                    return {"success": False,
                            "error": "当前只有敌方单位可直接进入战斗；攻击商人/友方的阵营转换规则尚未接入"}
                target.faction = "monster" if target.faction == "monster" else "enemy"
                encounter = world.start_encounter([player_id, target_id], target.position)
                world.record("hostility_declared", player_id, target_actor_id=target_id,
                             target_name=target.name, encounter_id=encounter.encounter_id)
                self._sync_world_entity_positions(world)
                self.state.world_state = world.to_dict()
                return {"success": True, "action": "world_interact",
                        "result": {"choice": choice, "encounter_id": encounter.encounter_id,
                                   "participants": list(encounter.participants),
                                   "battle_started": False},
                        "instruction": "遭遇已登记；战斗参与者接入现有战斗引擎尚未完成，暂不能直接结算伤害"}
            event_type = {"talk": "conversation_requested",
                          "cooperate": "alliance_proposed",
                          "intimidate": "intimidation_attempted"}[choice]
            event = world.record(event_type, player_id, target_actor_id=target_id,
                                 target_name=target.name, position=list(target.position))
            self.state.world_state = world.to_dict()
            if choice == "talk":
                return {"success": True, "action": "world_interact",
                        "result": {"choice": choice, "event_id": event["event_id"],
                                   "target": target.name,
                                   "narrative_input": "根据已记录事实生成对话，不得虚构移动、交易或承诺"}}
            return {"success": True, "action": "world_interact",
                    "result": {"choice": choice, "event_id": event["event_id"],
                               "target": target.name, "completed": False,
                               "pending_counterparty_decision": True},
                    "instruction": "只记录了交涉意图；目标是否同意、勒索是否成功尚无对方决策与结算机制"}
        except (ValueError, KeyError, TypeError) as exc:
            return {"success": False, "error": str(exc)}

    def _action_world_vision(self, params: dict) -> dict:
        try:
            world = self._world_from_state()
            actor_id = params.get("actor_id")
            player_id = next((key for key, actor in world.actors.items() if actor.kind == "player"), None)
            actor_id = params.get("actor_id") or player_id
            if actor_id is None or actor_id != player_id:
                return {"success": False, "error": "只能查看轮回者自己的视野"}
            visible = world.visible_cells(actor_id)
            self._sync_world_entity_positions(world)
            self.state.world_state = world.to_dict()
            return {"success": True, "action": "world_vision",
                    "result": {"actor_id": actor_id, "visible_cells": [list(p) for p in visible],
                               "known_cells": sorted(world.actors[actor_id].known_cells)}}
        except (ValueError, KeyError, TypeError) as exc:
            return {"success": False, "error": str(exc)}

    # ==================== 开局行动 ====================

    def _action_setup_attributes(self, params: dict) -> dict:
        return handle_setup_attributes(self, params)

    def _action_setup_choose_region(self, params: dict) -> dict:
        return handle_setup_choose_region(self, params)

    def _action_setup_choose_resonance(self, params: dict) -> dict:
        return handle_setup_choose_resonance(self, params)

    def _action_setup_choose_initial_daowen(self, params: dict) -> dict:
        return handle_setup_choose_initial_daowen(self, params)

    def _offer_initial_daowen_discovery(self, source: str) -> dict:
        """从杀伐闭环不重复随机列出至多3种；只列候选，不替AI作选择。"""
        if self.state.pending_initial_daowen_choices:
            return {"success": False, "error": "已有待选择的初始道纹发现"}
        owned = set(self.state.player.dao_wen) if self.state.player else set()
        remaining = [name for name in SHAFA_LOOP_DAOWEN if name not in owned]
        if not remaining:
            return {"success": False, "error": "杀伐闭环已无可发现的道纹"}
        choices: list[str] = []
        for slot in range(min(3, len(remaining))):
            roll = self.dice.auto_roll(
                f"initial_daowen_discovery_{source}_{slot + 1}",
                remaining,
                context=f"{source}：随机列出初始道纹候选{slot + 1}",
            )
            selected = remaining.pop(roll["record"]["selected_index"])
            choices.append(selected)
        self.state.pending_initial_daowen_choices = choices
        self.state.pending_initial_daowen_source = source
        return {"success": True, "choices": choices, "source": source}

    def _grant_named_daowen(self, entity: Entity, name: str) -> None:
        if entity is None or name in entity.dao_wen:
            return
        entity.dao_wen[name] = DaoWenInstance(DaoWen(
            name=name, formula=f"{name}X", cost_type="消耗",
            cost_formula="X", effect_formula=""))

    def _action_resolve_redemption(self, params: dict) -> dict:
        """救赎：接纳昏迷微光者为员工（待命，需派遣+战终工资，计入背叛），或当场【终结】。

        【终结】：救赎不是免费收获——把昏迷
        的微光者当场终结，效果等同击杀：它被还原为一次正常[命零]，[战终]按与普通击杀
        完全相同的公式（⌈战始血限×2%⌉ + 道纹数×5）产出[碎片]，不再落入
        「永久离场不产碎片」的分类。
        """
        pending = self.state.pending_redemption
        if not pending:
            return {"success": False, "error": "当前没有待结算的救赎"}
        option = params.get("option")
        if option in (1, "1", "接纳"):
            name = params.get("name", "")
            if not isinstance(name, str) or not name.strip():
                return {"success": False, "error": "接纳时必须自定义员工名字"}
            name = name.strip()
            existing = set()
            if self.state.player:
                existing.add(self.state.player.name)
            for group in (self.state.friends, self.state.employees, self.state.temp_friends):
                existing.update(entity.name for entity in group)
            existing.update(entity.name for entity in self.state.enemies if entity.is_alive)
            if name in existing:
                return {"success": False, "error": f"名字【{name}】已被占用，请换一个"}
            employee = Entity(
                name=name,
                entity_type="员工",
                blood_limit=math.ceil(pending["blood_limit"] / 2),
                current_hp=math.ceil(pending["blood_limit"] / 2),
                attack_count=math.ceil(pending["attack_count"] / 2),
                attack_power=math.ceil(pending["attack_power"] / 2),
                is_deployed=False,
            )
            self.state.employees.append(employee)
            self.state.pending_redemption = {}
            return {
                "success": True,
                "action": "救赎·接纳",
                "result": {"employee": employee.to_dict(), "from": pending["name"]},
            }
        if option in (2, "2", "终结"):
            self.state.pending_redemption = {}
            monster = next((m for m in self.state.enemies
                            if m.name == pending.get("name")), None)
            if monster is None:
                # 实体已不在敌方列表（异常/旧档）：保守起见仍按快照给出击杀口径奖励，
                # 保证「等同击杀」不被静默降级成无事发生。
                blood_limit = pending.get("blood_limit", 0)
                dao_wen = pending.get("dao_wen") or {}
                reward = math.ceil(blood_limit * 0.02) + len(dao_wen) * 5
                self.state.shards += reward
                return {"success": True, "action": "救赎·终结",
                        "result": {"monster": pending.get("name"), "shard_reward": reward,
                                   "total_shards": self.state.shards,
                                   "note": "等同击杀，碎片已结算"}}
            # 还原为一次正常命零：清掉【离场】标记，让[战终]走普通击杀的奖励管线
            # （奖励公式与结算时点都由 battle_end 统一负责，引擎内只有一处口径）。
            monster.is_alive = False
            monster.is_departed = False
            monster.departure_reason = ""
            monster.removed_without_kill = False
            monster._redeemed = False
            monster._leave_ctx = None
            monster._death_ctx = make_context(
                timing=self.state.combat_subphase or self.state.phase,
                source="终结", source_type="system", target=monster,
                mechanic="death", subtype="redemption_finish",
                tags={"death", "redemption"},
            ).to_dict()
            return {"success": True, "action": "救赎·终结",
                    "result": {"monster": monster.name,
                               "note": "昏迷的微光者被当场终结，等同击杀，碎片于[战终]按命零奖励结算",
                               "battle_start_blood_limit": monster.battle_start_blood_limit,
                               "dao_wen_count": len(monster.dao_wen)}}
        return {"success": False, "error": "option必须是1/接纳或2/终结"}

    # ==================== 局外行动 ====================

    def _action_upgrade_doctor(self, params: dict) -> dict:
        """医生事件的后续服务：每支付5碎片，显式选择+1攻击次数或+2攻击力。"""
        doctor = next((entity for entity in self.state.employees
                       if entity.name == "医生" and entity.is_alive), None)
        if doctor is None:
            return {"success": False, "error": "当前没有存活医生"}
        mode = params.get("mode")
        if mode not in ("attack_count", "attack_power"):
            return {"success": False, "error": "mode必须是attack_count或attack_power"}
        if self.state.shards < 5:
            return {"success": False, "error": "医生升级需要5碎片"}
        self.state.shards -= 5
        if mode == "attack_count":
            doctor.attack_count += 1
        else:
            doctor.attack_power += 2
        return {"success": True, "action": "医生升级",
                "result": {"mode": mode, "paid": 5,
                           "attack_count": doctor.attack_count,
                           "attack_power": doctor.attack_power}}

    def _action_pre_battle(self, params: dict) -> dict:
        """局外行动入口。开放世界中，耗时行动先推进世界，再结算收益。"""
        if getattr(self.state, "world_state", {}):
            return self._action_timed_world_pre_battle(params)
        return self._execute_pre_battle_immediate(params, world_mode=False)

    def _action_timed_world_pre_battle(self, params: dict) -> dict:
        action = params.get("sub_action", "")
        durations = {
            "休整": lambda p: p.get("tier", 1),
            "修行": lambda p: p.get("tier", 1),
            "学习": lambda p: 3,
            "共鸣": lambda p: 1,
            "维修": lambda p: 1,
            "雇佣": lambda p: 1,
            "炼心": lambda p: 1,
        }
        if action == "探索":
            return {"success": False, "error": "开放世界已删除独立探索行动；请移动到地图地点"}
        if action == "附煞":
            return {"success": False, "error": "附煞已删除，待造生/造物系统接替；当前不能使用旧行动"}
        if action not in durations:
            # 献祭等尚未裁定为耗时的条件性操作暂时保留即时结算。
            return self._execute_pre_battle_immediate(params, world_mode=True)

        try:
            world = self._world_from_state()
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        player_id = next((key for key, actor in world.actors.items() if actor.kind == "player"), None)
        if player_id is None:
            return {"success": False, "error": "地图上找不到轮回者单位"}
        if any(encounter.active and player_id in encounter.participants
               for encounter in world.encounters.values()):
            return {"success": False, "error": "正在遭遇中，不能开始耗时行动；应先处理遭遇"}

        # 在隔离快照里先执行一次旧结算器作参数/资源预检；回滚状态与随机源，不产生真实收益。
        state_before = copy.deepcopy(self.state)
        combat_runtime_before = self._snapshot_combat_runtime()
        dice_before = copy.deepcopy(self.dice)
        event_pool_before = (set(self.event_pool.triggered), self.event_pool.current)
        interrupts_before = copy.deepcopy(self._pending_interrupts)
        try:
            preview = self._execute_pre_battle_immediate(dict(params), world_mode=True)
        finally:
            self._restore_state_in_place(state_before)
            self.combat.state = self.state
            self._restore_combat_runtime(combat_runtime_before)
            self.dice = dice_before
            self.combat.dice = self.dice
            self.event_pool.triggered, self.event_pool.current = event_pool_before
            self._pending_interrupts = interrupts_before
        if not preview.get("success", False):
            return preview

        duration = durations[action](params)
        if not isinstance(duration, int) or isinstance(duration, bool) or duration < 1:
            return {"success": False, "error": f"【{action}】耗时档位必须是正整数"}
        start_turn = world.clock.turn
        world.record("world_action_started", player_id, action=action, duration=duration)
        world.wait(player_id, duration)
        elapsed = world.clock.turn - start_turn
        active_encounter = next((encounter for encounter in world.encounters.values()
                                 if encounter.active and player_id in encounter.participants), None)
        self._sync_world_entity_positions(world)
        if active_encounter is not None:
            world.record("world_action_interrupted", player_id, action=action,
                         elapsed_turns=elapsed, encounter_id=active_encounter.encounter_id)
            self.state.world_state = world.to_dict()
            return {"success": True, "action": "world_action_interrupted",
                    "result": {"sub_action": action, "elapsed_turns": elapsed,
                               "required_turns": duration,
                               "encounter_id": active_encounter.encounter_id,
                               "reward_applied": False},
                    "instruction": "行动被遭遇打断，未结算本次收益；请先处理地图遭遇"}

        result = self._execute_pre_battle_immediate(dict(params), world_mode=True)
        if not result.get("success", False):
            world.record("world_action_interrupted", player_id, action=action,
                         elapsed_turns=elapsed, reason=result.get("error", "completion_failed"))
            self.state.world_state = world.to_dict()
            return {"success": True, "action": "world_action_interrupted",
                    "result": {"sub_action": action, "elapsed_turns": elapsed,
                               "required_turns": duration, "reward_applied": False,
                               "completion_error": result.get("error", "")}}
        world.record("world_action_completed", player_id, action=action,
                     elapsed_turns=elapsed)
        self._open_adjacent_merchant_session(world)
        self._sync_world_entity_positions(world)
        self.state.world_state = world.to_dict()
        result["world_time"] = {"elapsed_turns": elapsed, "world_turn": world.clock.turn}
        return result

    def _execute_pre_battle_immediate(self, params: dict, *, world_mode: bool) -> dict:
        """旧局外行动结算器；world_mode=True时复用效果但不再消耗旧精力。"""
        action = params.get("sub_action", "")

        if not world_mode:
            if self.state.energy <= 0:
                return {
                    "success": False,
                    "error": "精力已耗尽",
                    "instruction": "精力耗尽，进入战斗阶段。请调用 battle_start。"
                }

            self.state.energy -= 1
            # 旧战斗循环中的炼心精力惩罚仅适用于旧模式。
            if self.state.pending_energy_penalty > 0:
                self.state.energy -= self.state.pending_energy_penalty
                self.state.pending_energy_penalty = 0

        result_map = {
            "休整": self._pre_battle_xiuzheng,
            "修行": self._pre_battle_xiuxing,
            "学习": self._pre_battle_xuexi,
            "共鸣": self._pre_battle_gongming,
            "探索": self._pre_battle_tansuo,
            "维修": self._pre_battle_weixiu,
            "雇佣": self._pre_battle_guyong,
            "炼心": self._pre_battle_lianxin,
            "附煞": self._pre_battle_fusha,
            "献祭": self._pre_battle_sacrifice,
        }
        # 2026-09-28：局外【忘忧】已删除，忘忧香效果为溢出回复转格挡。

        # 副本专属行动门禁（规则正文：维修=扭曲都市、雇佣=罪孽都市、炼心=龙心谷专属）。
        # 缺少该校验会让任意副本都能用他人专属行动，统计与平衡数据将失真。
        REGION_EXCLUSIVE = {"维修": "扭曲都市", "雇佣": "罪孽都市", "炼心": "龙心谷", "附煞": "乱葬岗"}
        need_region = REGION_EXCLUSIVE.get(action)
        if need_region and self.state.current_region != need_region:
            if not world_mode:
                self.state.energy += 1
            return {"success": False,
                    "error": f"【{action}】是{need_region}专属行动，当前副本为"
                             f"{self.state.current_region or '未选择'}"}

        if action in result_map:
            energy_before = self.state.energy
            result = result_map[action](params)
            if world_mode:
                # 旧处理器会在失败分支退款精力；开放世界不再使用旧精力经济。
                self.state.energy = energy_before
            return result
        else:
            if not world_mode:
                self.state.energy += 1
            return {"success": False, "error": f"未知局外行动: {action}"}

    def _pre_battle_xiuzheng(self, params: dict) -> dict:
        """休整：产生恢复量，并按稳定引用在自己/朋友/员工间自由完整分配。"""
        tier = params.get("tier", 1)
        # 休整改制：恢复额度按
        # **轮回者血限百分比**提供——20%/40%/60%，向上取整。血限40 时 tier1=8
        # 与旧固定值一致；高档位较旧 24/48 收敛（百分比随血限放大，固定值不随）。
        tier_pct = {1: 20, 2: 40, 3: 60}
        tier_cost = {1: 0, 2: 10, 3: 25}
        if not isinstance(tier, int) or isinstance(tier, bool) or tier not in tier_pct:
            self.state.energy += 1
            return {"success": False, "error": "休整档位必须是1/2/3"}
        player = self.state.player
        base_heal = (math.ceil(player.blood_limit * tier_pct[tier] / 100)
                     if player is not None else 0)
        cost = tier_cost[tier]
        # 2026-10-05 用户令：癌变不再为【休整】提供额外恢复量，恢复额度=档位基础值。
        heal = base_heal
        refs = ({"player:0": self.state.player}
                if self.state.player and self.state.player.is_alive else {})
        refs.update({f"friend:{index}": entity for index, entity in enumerate(self.state.friends)
                     if entity.is_alive})
        refs.update({f"employee:{index}": entity for index, entity in enumerate(self.state.employees)
                     if entity.is_alive})
        allocations = params.get("heal_allocations")
        if (not isinstance(allocations, list) or not allocations
                or any(not isinstance(entry, dict) or entry.get("target_ref") not in refs
                       or not isinstance(entry.get("amount"), int) or isinstance(entry.get("amount"), bool)
                       or entry["amount"] < 0 for entry in allocations)
                or sum(entry["amount"] for entry in allocations) != heal):
            self.state.energy += 1
            return {"success": False,
                    "error": f"休整必须用heal_allocations把{heal}点恢复量完整分配给合法目标"}
        # 负债口径：冻结的是碎片**支出**（cost>0）；
        # 0费行动不属于支出，不得因负债被误拒（否则负债局pre_battle无法消耗精力=死锁）。
        if cost > 0 and self.state.shards < cost:
            self.state.energy += 1
            return {"success": False, "error": f"碎片不足，需要{cost}，当前{self.state.shards}"}

        self.state.shards -= cost
        healed = []
        for entry in allocations:
            target = refs[entry["target_ref"]]
            total_healed_before = target.total_healed
            healed_this_battle_before = target.healed_this_battle
            detail = self.state.apply_heal(target, entry["amount"], ctx={
                "timing": "pre_battle_action", "source": "休整", "source_type": "action",
                "actor": self.state.player, "target": target, "mechanic": "heal", "subtype": "rest",
                "amount": entry["amount"], "tags": {"pre_battle"},
            })
            # 局外恢复不计入“本场战斗内”的癌变/战终回复追踪。
            target.total_healed = total_healed_before
            target.healed_this_battle = healed_this_battle_before
            healed.append({"target_ref": entry["target_ref"], "target": target.name,
                           "allocated": entry["amount"], **detail})
        payload = {"base_heal_amount": base_heal,
                   "heal_amount": heal, "shard_cost": cost, "heals": healed,
                   "shards_remaining": self.state.shards}
        return {"success": True, "action": "休整", "result": payload}

    def _pre_battle_xiuxing(self, params: dict) -> dict:
        """修行：获得属性点并立即分配。

        计价与开局初始分配同口径：1属性点=6[血限]，2属性点=1[速限]=1[法限]
        。
        不朽之躯不阻止修行：它只锁[血限]增长，不限制属性点本身的增长。"""
        tier = params.get("tier", 1)
        tier_map = {1: (1, 0), 2: (2, 15), 3: (3, 35), 4: (4, 65), 5: (5, 100), 6: (6, 150)}
        if not isinstance(tier, int) or isinstance(tier, bool) or tier not in tier_map:
            self.state.energy += 1
            return {"success": False, "error": "修行档位必须是1~6"}
        points, cost = tier_map[tier]
        if cost > 0 and self.state.shards < cost:  # 负债口径同休整：0费不属于支出
            self.state.energy += 1
            return {"success": False, "error": f"碎片不足，需要{cost}"}
        alloc_keys = ("blood_points", "speed_points", "mana_points")
        allocations = params.get("allocations")
        if allocations is None and params.get("to") in ("blood", "speed", "mana"):
            allocations = {f"{params['to']}_points": points}
        if not isinstance(allocations, dict):
            allocations = {}
        vals: dict = {}
        for key in alloc_keys:
            v = allocations.get(key, 0)
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                vals = None
                break
            vals[key] = v
        if vals is None or sum(vals.values()) > points + self.state.attribute_points:
            self.state.energy += 1
            return {"success": False,
                    "error": f"修行{tier}档给{points}属性点（池内现有{self.state.attribute_points}），"
                             f"allocations 分配总数不能超过 {points + self.state.attribute_points}；"
                             f"不传 allocations 则全部存进属性点池"}

        self.state.shards -= cost
        # DM裁定 2026-09-10：修行给的属性点**先入池**，不强制当场花掉——
        # 档位点数（1/3/5）与 2 点一档的比价不再互相卡死，余点攒着随时兑。
        self.state.attribute_points += points
        redeemed = self._redeem_attribute_points(vals) if allocations else None
        if redeemed is not None and not redeemed.get("success"):
            return redeemed
        gained = ((redeemed or {}).get("result") or {}).get(
            "gained", {"blood": 0, "speed": 0, "mana": 0})
        player = self.state.player
        return {"success": True, "action": "修行",
                "result": {"points_gained": points, "shard_cost": cost,
                           "allocations": {k: vals[k] for k in alloc_keys},
                           "gained": gained,
                           "attribute_points": self.state.attribute_points,
                           "blood_limit": player.blood_limit,
                           "speed_limit": player.speed_limit,
                           "mana_limit": player.mana_limit,
                           "attack_count": player.effective_attack_count(),
                           "attack_power": player.effective_attack_power(),
                           "action_count": player.action_count}}

    # 法术大全注册表（名 → 所需道纹）
    # 2026-08-21：删除「临界泄压」（所需道纹·切割已删除）。
    # 唯一事实源在 CombatEngine.BUILTIN_SPELL_DAOWEN（与 SPELL_FLOWS 同处定义，
    # 保证「有流程」与「有所需道纹」永不脱节）。此处只做别名，避免两处维护。
    # 法术不再需要【学习】：持全部所需道纹即可直接使用（见 spell_definition）。
    SPELL_REGISTRY = CombatEngine.BUILTIN_SPELL_DAOWEN
    # 三副本终音法器（死斗胜利后按current_region发放，见resolve_final_duel/choose_terminal_artifact）
    TERMINAL_ARTIFACTS = {
        "扭曲都市": [
            ("体外心脏", "[战始]，自身[血限]与当前生命临时翻倍"),
            ("羔羊之泪", "[战始]，场上所有角色与怪物立刻失去50%当前生命"),
            ("红头绳", "局外【行动】永久新增\"献祭\"（衰老3，换取精力+2）"),
            ("猩红尖牙", "获得该法器，并立刻强制触发特殊事件\"初拥之夜\""),
        ],
        "罪孽都市": [
            ("黑金名片", "[战始]，可使场上所有敌方[目标][血限]减半后，付出等量[碎片]（允许负债，负债≤50）"),
            ("罪业金库", "[回始]，可消耗X点[碎片]，获得2X点格挡（X≤2%当前碎片）"),
            ("教父左轮", "（耐久6/6，永不消耗，[战终]耐久回满）。对[目标]打出30%自身[血限]×使用次数的【必中】伤害"),
        ],
        "龙心谷": [
            ("共心环", "[战始]，选择自身拥有的一枚【××龙心】；本场战斗中，自身、[朋友]与[员工]均可消耗该龙心耐久，抵消同类型代价"),
            ("负岳碑", "当任意[朋友]或[员工]即将触发【撤退】时，可以流血20，抵消本次伤害并取消本次【撤退】"),
            ("真龙之心", "每消耗12X龙性，获得X种不同龙族遗物（6X衰老=2X枯竭=X萎缩=12X龙性）"),
        ],
    }

    # 初拥之夜9选1（1~8限选1次，9号可重复触发权）
    FIRST_EMBRACE_OPTIONS = {
        1: ("血族血脉", "[回终]若本回合造成伤害则获得等量[回复]，否则流血20"),
        2: ("不朽之躯", "当前[血限]减半；免疫衰老；[血限]无法增加"),
        3: ("鲜血之翼", "代价：流血5X，发动【飞行X】回合"),
        4: ("血族尖牙", "代价：衰老20，使生命低于自身的一个[目标]转化为听命于你的赤族（诅咒：[回终]赤族流血20）"),
        5: ("真理眼", "代价：冷却2，使一个[目标]必须言明真理，否则无法开口"),
        6: ("寒冰法力", "对[目标]每累计施加10法力，使其本回合出手次数-1"),
        7: ("血影", "当自身被选为非必中判定的[目标]时，可流血10，取消本次判定"),
        8: ("血食", "可使一名听命于你的赤族[命零]，自身获得等同于该赤族当前生命的[回复]"),
        9: ("封存血脉", "保留触发权，随时再次触发初拥之夜"),
    }

    # 遗物池定义（25件；【发现】只列候选，效果在对应触发时点应用）
    # 2026-10-13 用户令：【买路财】改制为轮回者【逃跑】行动，【三相残韵盘】删除
    # （发现侧由道纹【典当】承接），两件均移出遗物池。
    RELIC_DEFS = [
        ("血誓戒", "[回始]首次主动支付流血代价时，获得等同于本次流血的格挡；若支付后生命≤30%，改为获得等量生命"),
        ("同魂笔", "对[目标]发动残韵时，可另选一[目标]使其一种道纹受同种残韵影响"),
        ("回锋刀", "每失去1点速度后对[目标]造成3伤害；[回始]对[目标]造成3×([速限]-当前速度)伤害"),
        ("血契", "数值型【代价】可与一名存活的朋友或员工平分，余数按随机数分配（通用规则见规则正文《基础定义·平分规则》）"),
        ("避风铃", "每次闪避后获得3格挡；当前速度归零时获得15格挡"),
        ("守夜灯", "[回始]获得[法限]50%的法力"),
        ("无所求", "每当在事件中选拒绝类选项，永久获得1属性点"),
        ("忘忧香", "你受到溢出的回复时将其转化为等量格挡"),
        ("承露盏", "每累计失去10点生命，获得1点法力（本场累计，余数滚存，[战始]归零）"),
        # 2026-10-03 新增 5 件（承接被删道纹，全部加入通用遗物池）：
        ("千荆甲", "你受到攻击伤害前，攻击者受到等量伤害（承接原【爆裂】道纹；只对攻击行动生效）"),
        
        ("癫狂之脑", "你每回合的出手次数+1，受到的攻击伤害翻倍"),
        ("活血衣", "你受到攻击造成的伤害后，恢复等同其一半的生命"),
        ("增生药剂", "你每次对自己造成伤害后，恢复10生命"),
        ("灾厄轮盘", "[回始]从场上存活角色（包括你）中随机选一名，使其失去30%当前生命"),
        # 2026-10-14：已裁定遗物化的道纹，统一加入通用遗物池。
        ("逆血龙鳞", "本回合你每失去1点生命，造成的伤害+1"),
        ("蚀命刻尺", "任意角色每次失去生命时，使其衰老1"),
        ("噬伤血囊", "任意角色受到伤害时，你回复该次实际伤害的20%"),
        ("倍速齿轮", "你获得速度时，实际获得的速度翻倍"),
        ("沉重枷锁", "所有角色每回合出手次数-1"),
        ("失速冥灯", "任意角色每失去一次速度，其速限降低2"),
        ("寂灭面具", "阻止命零触发的效果"),
        ("衰变沙漏", "任意角色每次发动道纹时，该次道纹数值降低1，最低为0"),
        ("蚀界残钟", "每回终，所有角色各自失去等同于其攻击力×攻击次数的血限"),
        ("窥隙法匣", "每次成功闪避后，下回合获得10点法力"),
        ("逐影胫骨", "每累计成功闪避两次，速度+1"),
    ]

    def _init_relic_pool(self):
        """每局仅初始化一次，池耗尽后不得重新生成重复遗物。"""
        if self.state.relic_pool_initialized:
            return
        self.state.relics_pool = [Relic(name=n, effect=e) for n, e in self.RELIC_DEFS]
        self.state.relic_pool_initialized = True

    def _initialize_merchant_inventory(self):
        """过路商人首次进入本局副本时，从真实遗物池随机取得5件独立库存，不重复生成。"""
        if getattr(self.state, "merchant", None) is not None:
            return self.state.merchant
        self._init_relic_pool()
        if len(self.state.relics_pool) < 5:
            raise ValueError("遗物池不足5件，无法生成过路商人的初始库存")
        remaining = list(self.state.relics_pool)
        chosen = []
        for slot in range(5):
            roll = self.dice.auto_roll(
                f"merchant_initial_stock_{slot + 1}",
                [r.name for r in remaining],
                context=f"过路商人首次进入副本：随机取得初始遗物{slot + 1}/5",
            )
            index = int(roll["record"]["selected_index"])
            chosen.append(remaining.pop(index))
        for relic in chosen:
            self.state.relics_pool.remove(relic)
        self.state.merchant = Entity(
            name="过路商人", entity_type="特殊角色", relics=chosen,
            shards=20, shards_peak=20,
        )
        # 地图坐标由开放世界地图生成器写入；空值表示地图移动系统尚未分配坐标。
        self.state.merchant_position = {}
        return self.state.merchant

    def _offer_relic_discovery(self, source: str) -> dict:
        """从当前池不重复随机列出至多3件；只列候选，不替AI作选择。"""
        self._init_relic_pool()
        if self.state.pending_relic_choices:
            return {"success": False, "error": "已有待选择的遗物发现"}
        if not self.state.relics_pool:
            return {"success": False, "error": "遗物池已耗尽"}
        remaining = [r.name for r in self.state.relics_pool]
        choices: list[str] = []
        for slot in range(min(3, len(remaining))):
            roll = self.dice.auto_roll(
                f"relic_discovery_{source}_{slot + 1}",
                remaining,
                context=f"{source}：随机列出遗物候选{slot + 1}",
            )
            selected = remaining.pop(roll["record"]["selected_index"])
            choices.append(selected)
        self.state.pending_relic_choices = choices
        self.state.pending_relic_source = source
        return {"success": True, "choices": choices, "source": source}

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
        return {"success": True, "action": "放弃发现遗物",
                "result": {"discarded_choices": choices, "source": source,
                           "note": "本次发现不取任何遗物，待选列表已清空"}}

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
        # 2026-10-12 用户令：30X[血限] = 允许拥有 X 种道纹和 X 种遗物。
        # 裁定①：**开局阶段豁免**——开局流程强制「必须先从发现候选中选择1件遗物」，
        # 低血限（血限18→额度0）会被这条卡死、连 setup_choose_region 都走不到。
        # 裁定③（2026-10-12）：额度已满时仍可「替换」——带 replace 参数指定要换掉的
        # 一件已持有遗物，先移除再放入，总种数不变，因此不受额度限制。
        player = self.state.player
        replace_name = (params.get("replace") or "").strip()
        replaced = None
        if replace_name:
            replaced = next((r for r in self.state.relics if r.name == replace_name), None)
            if replaced is None:
                return {"success": False,
                        "error": f"未持有【{replace_name}】，无法用它替换",
                        "owned": sorted({r.name for r in self.state.relics})}
        elif player is not None and self.state.phase != "setup":
            cap = player.relic_capacity
            owned = len({r.name for r in self.state.relics})
            if owned >= cap:
                return {"success": False,
                        "error": (f"遗物种类超出承载：[血限]{player.blood_limit} 只允许{cap}种"
                                  f"（30[血限]=1种），已持有{owned}种。"
                                  f"可带 replace 参数换掉一件，或放弃本次发现"),
                        "owned": sorted({r.name for r in self.state.relics})}
        if replaced is not None:
            self.state.relics.remove(replaced)
        self.state.relics_pool.remove(relic)
        self.state.relics.append(relic)
        source = self.state.pending_relic_source
        self.state.pending_relic_choices = []
        self.state.pending_relic_source = ""
        result: dict = {
            "success": True,
            "action": "选择发现遗物",
            "result": {"relic": choice, "source": source,
                       **({"replaced": replaced.name} if replaced is not None else {})},
        }
        # 开局流程：先发现遗物，再发现初始道纹。
        if (source == "开局发现" and self.state.player is not None
                and not self.state.player.dao_wen
                and not self.state.pending_initial_daowen_choices):
            discovery = self._offer_initial_daowen_discovery("开局发现")
            if not discovery.get("success"):
                return discovery
            result["result"]["daowen_choices"] = discovery["choices"]
            result["next_actions"] = ["setup_choose_initial_daowen"]
            result["note"] = "开局遗物已选择；请从杀伐闭环的3个发现候选中显式选择1种作为初始道纹。"
        return result

    def _offer_item_discovery(self, pool: list[str], source: str) -> dict:
        if self.state.pending_item_choices:
            return {"success": False, "error": "已有待选择的消耗品发现"}
        remaining = list(dict.fromkeys(pool))
        if not remaining:
            return {"success": False, "error": "没有可发现的消耗品"}
        choices = []
        for slot in range(min(3, len(remaining))):
            roll = self.dice.auto_roll(
                f"item_discovery_{source}_{slot + 1}", remaining,
                context=f"{source}：随机列出消耗品候选{slot + 1}",
            )
            choices.append(remaining.pop(roll["record"]["selected_index"]))
        self.state.pending_item_choices = choices
        self.state.pending_item_source = source
        return {"success": True, "choices": choices, "source": source}

    def _action_choose_discovered_item(self, params: dict) -> dict:
        choice = params.get("item_name", "")
        if not self.state.pending_item_choices:
            return {"success": False, "error": "当前没有待选择的消耗品发现"}
        if choice not in self.state.pending_item_choices:
            return {"success": False, "error": "只能选择本次发现列出的消耗品",
                    "choices": list(self.state.pending_item_choices)}
        if choice not in TWISTED_TOOL_LIBRARY:
            return {"success": False, "error": f"未知工具: {choice}"}
        durability, effect = TWISTED_TOOL_LIBRARY[choice]
        self.state.consumables.append(Consumable(
            name=choice, effect=effect, current_uses=durability, max_uses=durability,
        ))
        source = self.state.pending_item_source
        self.state.pending_item_choices = []
        self.state.pending_item_source = ""
        return {"success": True, "action": "选择发现消耗品",
                "result": {"item": choice, "durability": durability, "source": source}}

    def _build_custom_spell(self, actor: Entity, definition: dict, *, allow_predefined: bool = False) -> dict:
        """自创法术的唯一构建入口（三大法则校验 + 组装 Spell）。

        战斗内【define_spell】使用本方法——
        规则只有一处，禁止两条路径各自校验。

        返回 {"spell": Spell, "parsed": parsed} 成功；失败返回 {"error": str}。
        不改动任何状态（不扣精力/出手/碎片），由调用方决定代价与写入时机。
        """
        name = definition.get("name")
        required = definition.get("required_daowen")
        trigger = definition.get("trigger_condition")
        flow = definition.get("effect_flow")
        # 逐条校验而不是合成一个大判断：合一之后只能给出一句笼统的
        # 「需唯一名称、至少一种自身已持有道纹、触发条件和效果流程」，玩家（和 AI）
        # 看不出到底缺哪条道纹——定义预置法术时这是最常见的一条失败路径。
        if not isinstance(name, str) or not name.strip():
            return {"error": "自创法术需唯一名称、至少一种自身已持有道纹、触发条件和效果流程"}
        if name in self.SPELL_REGISTRY and not allow_predefined:
            return {"error": f"名称【{name}】与法术大全已有法术重名，请换一个"}
        if any(spell.name == name for spell in actor.spells):
            return {"error": f"法术【{name}】已经定义"}
        if not isinstance(required, list) or not required:
            return {"error": "自创法术需至少一种自身已持有道纹"}
        if len(set(required)) != len(required):
            return {"error": "自创法术的所需道纹不可重复"}
        missing = [d for d in required if d not in actor.dao_wen]
        if missing:
            return {"error": f"自创法术缺少所需道纹：{'、'.join(missing)}（须自身已持有）"}
        if (not isinstance(trigger, str) or not trigger.strip()
                or not isinstance(flow, str) or not flow.strip()):
            return {"error": "自创法术需触发条件和效果流程"}
        # 句式校验：提交时就必须能被完整解析，解析失败直接拒绝并附带具体原因，
        # 禁止"定义成功但因文本对不上而永远不触发"的静默哑火。已持有道纹之外
        # 引用的道纹同样在此处一并拒绝。
        try:
            parsed = parse_spell_definition(trigger, flow, set(DaoWenEngine.list_all()))
        except SpellDslError as exc:
            return {"error": f"自创法术句式错误：{exc}"}
        from .spell_dsl import collect_step_daowen, TRIGGER_INSTANT
        from .spell_execution import Lifecycle, TriggerType
        # 生命周期校验（Part 6）：允许的取值只有 battle / permanent。
        # 战斗中自创的法术默认是 battle——本场战斗的即兴产物，[战终]即失效；
        # 要跨战斗保留必须显式声明 permanent。instant 不是可保存的生命周期。
        lifecycle_raw = definition.get("lifecycle", Lifecycle.BATTLE.value)
        if lifecycle_raw not in (Lifecycle.BATTLE.value, Lifecycle.PERMANENT.value):
            return {"error": f"法术lifecycle只能是battle或permanent，收到{lifecycle_raw!r}"}
        if parsed.trigger == TRIGGER_INSTANT:
            # 瞬发法术是一次性执行对象（lifecycle=instant），不能保存为角色法术；
            # 否则会变成"定义了一个永不触发的法术"。请改用 cast(flow=...)。
            return {"error": "瞬发法术不能用define_spell保存：请用cast(flow=...)直接施放"}
        referenced = collect_step_daowen(parsed.steps)
        missing = referenced - set(required)
        if missing:
            return {"error": ("自创法术句式错误：效果流程引用了未列入"
                              f"required_daowen的道纹{sorted(missing)}")}
        return {"spell": Spell(
            name=name.strip(), required_daowen=list(required),
            trigger_condition=trigger.strip(), effect_flow=flow.strip(),
            rank=len(required),
            custom_conditions=list(definition.get("custom_conditions") or []),
            automatic=parsed.trigger == TriggerTiming.SELF_TURN_END.value
                       or bool(definition.get("automatic", False)),
            lifecycle=lifecycle_raw,
            trigger=parsed.trigger,
        ), "parsed": parsed}

    def _action_undefine_spell(self, params: dict) -> dict:
        """移除一个法术定义（不花出手）。

        - 自创法术：从 entity.spells 移除（battle 作用域法术本来就会在战终被
          引擎自动清除；本入口用于战斗中主动放弃，或移除 permanent 定义）；
        - 自定义法术：移除当前定义；
        - 瞬发法术不产生绑定，因此不在此列。
        """
        player = self.state.player
        if player is None:
            return {"success": False, "error": "没有玩家"}
        spell_name = params.get("spell_name", "")
        if not isinstance(spell_name, str) or not spell_name.strip():
            return {"success": False, "error": "必须提交spell_name"}
        spell_name = spell_name.strip()
        known_custom = any(sp.name == spell_name for sp in player.spells)
        if not known_custom:
            return {"success": False, "error": f"玩家当前没有自定义法术【{spell_name}】的定义"}
        removed = self.combat.undefine_spell(player, spell_name)
        return {"success": True, "action": f"移除法术【{spell_name}】",
                "result": {"spell_name": spell_name, "removed": removed,
                           "cost": "无（移除不花出手）",
                           "remaining_bindings": self.combat.spell_bindings(player)}}

    def _action_define_spell(self, params: dict) -> dict:
        """战斗中自定义法术：持有所需道纹即可，消耗 1 次主动出手，立即生效。

        法术不属于学习行动，统一在战斗中自定义。三大法则由 DSL 解析器硬性把关
        （parse_spell_definition + 引用的道纹必须全部列入 required_daowen），
        因此不再额外要求 DM 审核中断，避免战斗中卡在待裁定队列里无法继续。
        """
        player = self.state.player
        if player is None:
            return {"success": False, "error": "没有玩家"}
        if self.state.phase != GamePhase.IN_COMBAT.value:
            return {"success": False,
                    "error": "自定义法术只能在战斗中进行（消耗1次主动出手）；局外入口已取消"}
        if not player.is_alive:
            return {"success": False, "error": "轮回者已死亡，无法自定义法术"}
        definition = params.get("spell")
        predefined_name = params.get("spell_name")
        if definition is None and isinstance(predefined_name, str):
            predefined = self.combat.spell_definition(player, predefined_name)
            if predefined is None:
                return {"success": False, "error": f"法术大全中不存在或当前不可用：{predefined_name}"}
            if any(sp.name == predefined_name for sp in player.spells):
                return {"success": False, "error": f"法术【{predefined_name}】已经定义"}
            definition = predefined.to_dict()
            definition["lifecycle"] = params.get("lifecycle", "battle")
        if not isinstance(definition, dict):
            return {"success": False, "error": "自定义施法必须提交spell对象或spell_name"}
        built = self._build_custom_spell(player, definition, allow_predefined=predefined_name is not None)
        if "error" in built:
            return {"success": False, "error": built["error"]}
        # 先扣出手再写入：出手预算不足时不产生任何效果，也不白造出法术。
        spell_tier = max(1, len(set(built["spell"].required_daowen)))
        budget_error = self._consume_actions_or_error(player, spell_tier)
        if budget_error:
            return budget_error
        player.spells.append(built["spell"])
        parsed = built["parsed"]
        wired = parsed.trigger in self.combat._WIRED_TRIGGERS
        result = {"spell": built["spell"].to_dict(), "wired": wired,
                  "tier": spell_tier, "cost": f"{spell_tier}次主动出手"}
        if not wired:
            result["warning"] = (f"触发时机【{parsed.trigger}】已通过句式校验，"
                                 "但该时机暂未接入战斗结算管线，本法术目前不会实际触发")
        return {"success": True, "action": "自定义法术", "result": result}

    def _pre_battle_xuexi(self, params: dict) -> dict:
        """学习：仅用于转化道纹1/2种对应0/10碎片。

        法术本身不再需要【学习】：持全部所需道纹即可
        直接使用，战斗中也可随时自创，法术统一从战斗中的 `define_spell` 入口定义。
        """
        player = self.state.player
        if not player:
            self.state.energy += 1
            return {"success": False, "error": "没有玩家"}
        sub = params.get("sub", "daowen")

        tier = params.get("tier", 1)
        if not isinstance(tier, int) or isinstance(tier, bool):
            self.state.energy += 1
            return {"success": False, "error": "学习tier必须是整数"}
        if sub in ("daowen", "转化道纹"):
            cost_map = {1: 0, 2: 10}
        else:
            self.state.energy += 1
            return {"success": False, "error": "学习sub必须是daowen"}
        if tier not in cost_map:
            self.state.energy += 1
            return {"success": False, "error": "学习道纹档位必须是1/2"}
        cost = cost_map[tier]
        names = params.get("names")
        if names is None and tier == 1 and isinstance(params.get("name"), str):
            names = [params["name"]]
        if (not isinstance(names, list) or len(names) != tier or len(set(names)) != tier
                or any(not isinstance(name, str) or not name for name in names)):
            self.state.energy += 1
            return {"success": False, "error": f"学习{tier}档必须用names提交{tier}个不同名称"}
        # 2026-10-12 用户令：30X[血限] = 允许拥有 X 种道纹和 X 种遗物。
        # 放在所有扣费与逐名校验之前：容量是结构性前提，不足时碎片一分不收、精力退回。
        new_names = [n for n in names if n not in player.dao_wen]
        owned = len(player.dao_wen)
        cap = player.daowen_capacity
        if owned + len(new_names) > cap:
            self.state.energy += 1
            return {"success": False,
                    "error": (f"道纹种类超出承载：[血限]{player.blood_limit} 只允许{cap}种"
                              f"（30[血限]=1种），已持有{owned}种，本次新增{len(new_names)}种")}
        if cost > 0 and self.state.shards < cost:  # 负债口径：0费学习不属于支出
            self.state.energy += 1
            return {"success": False, "error": f"碎片不足，需要{cost}"}

        def daowen_error(name: str) -> Optional[str]:
            # 2026-10-13：口径本体移入 gamedata.learnability_error（与【典当】发现池共用），
            # 此处只做转发，拒绝文案逐字不变。
            return learnability_error(name, owned=set(player.dao_wen),
                                      region=self.state.current_region,
                                      known=DaoWenEngine.list_all())

        errors = [error for name in names if (error := daowen_error(name))]
        if errors:
            self.state.energy += 1
            return {"success": False, "error": "；".join(errors)}
        for name in names:
            player.dao_wen[name] = DaoWenInstance(
                DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
        self.state.shards -= cost
        return {"success": True, "action": "学习·道纹",
                "result": {"learned": "daowen", "names": names, "shard_cost": cost,
                           "player_daowen": list(player.dao_wen)}}

    def _pre_battle_gongming(self, params: dict) -> dict:
        """共鸣：发现时随机列3件后显式选1；付费自选则直接加入。"""
        self._init_relic_pool()
        tier = params.get("tier", 1)
        if not isinstance(tier, int) or isinstance(tier, bool) or tier not in (1, 2):
            self.state.energy += 1
            return {"success": False, "error": "共鸣档位必须是1（发现）或2（自选）"}
        sub = params.get("sub", "choose" if tier == 2 else "discover")
        if sub not in ("discover", "choose") or (tier == 1 and sub != "discover") or (tier == 2 and sub != "choose"):
            self.state.energy += 1
            return {"success": False, "error": "共鸣1档=发现，2档=额外1精力并支付15碎片自选"}
        if not self.state.relics_pool:
            self.state.energy += 1
            return {"success": False, "error": "遗物池为空"}
        if sub == "choose":
            # 自选在通用局外行动1精力之外再次消耗1精力，并支付15碎片。
            if self.state.energy < 1:
                self.state.energy += 1
                return {"success": False, "error": "共鸣自选总共需要2点精力，当前不足"}
            name = params.get("name", "")
            idx = next((i for i,r in enumerate(self.state.relics_pool) if r.name == name), -1)
            if idx < 0:
                self.state.energy += 1
                return {"success": False, "error": f"遗物池无此遗物: {name}"}
            if self.state.shards < 15:
                self.state.energy += 1
                return {"success": False, "error": "碎片不足15"}
            # 2026-10-12 用户令：30X[血限] = 允许拥有 X 种道纹和 X 种遗物。
            player = self.state.player
            if player is not None:
                cap = player.relic_capacity
                owned = len({r.name for r in self.state.relics})
                if owned >= cap:
                    self.state.energy += 1
                    return {"success": False,
                            "error": (f"遗物种类超出承载：[血限]{player.blood_limit} 只允许{cap}种"
                                      f"（30[血限]=1种），已持有{owned}种")}
            self.state.shards -= 15
            self.state.energy -= 1
            relic = self.state.relics_pool.pop(idx)
            self.state.relics.append(relic)
            return {"success": True, "action": "共鸣(自选)", "result": {"gained_relic": relic.name, "effect": relic.effect}}
        discovery = self._offer_relic_discovery("共鸣发现")
        if not discovery.get("success"):
            return discovery
        return {
            "success": True,
            "action": "共鸣(发现)",
            "result": {"relic_choices": discovery["choices"]},
            "next_actions": ["choose_discovered_relic"],
        }


    def _pre_battle_tansuo(self, params: dict) -> dict:
        """探索：一档免费发现1个未遇事件；二档支付30碎片并依次发现2个不同未遇事件。"""
        if self.state.endless_mode:
            # 无尽模式「无法探索」：调用方已扣的 1 点精力按失败原子性退回。
            self.state.energy += 1
            return {"success": False, "error": "无尽模式下无法探索：进入无尽模式后不再开放【探索】"}
        if self.event_pool.current is not None or self.state.pending_event_queue:
            self.state.energy += 1
            pending = self.event_pool.current or self.state.pending_event_queue[0]
            return {"success": False, "error": f"事件【{pending}】尚未结算，不能再次探索"}
        tier = params.get("tier", 1)
        tier_map = {1: (1, 0), 2: (2, 30)}
        if not isinstance(tier, int) or isinstance(tier, bool) or tier not in tier_map:
            self.state.energy += 1
            return {"success": False, "error": "探索档位必须是1或2"}
        draw_count, shard_cost = tier_map[tier]
        region = self.state.current_region
        pool = self.event_pool.build_pool(region)
        if len(pool) < draw_count:
            self.state.energy += 1
            return {"success": False,
                    "error": f"当前事件池只剩{len(pool)}个未遇事件，无法执行发现{draw_count}次"}
        if shard_cost > 0 and self.state.shards < shard_cost:  # 负债口径：0费探索不属于支出
            self.state.energy += 1
            return {"success": False,
                    "error": f"探索{tier}档需要{shard_cost}碎片，当前{self.state.shards}"}

        self.state.shards -= shard_cost
        remaining = list(pool)
        discovered: list[str] = []
        random_records: list[dict] = []
        for index in range(draw_count):
            # 保留一档既有审计池名；二档用带序号的池名区分两次无放回抽取。
            pool_name = "event_pool" if tier == 1 else f"event_pool_{index + 1}"
            roll = self.dice.auto_roll(
                pool_name, remaining,
                context=f"探索{tier}档（{region}）第{index + 1}次",
            )
            name = roll["selected"]
            discovered.append(name)
            random_records.append(roll["record"])
            remaining.remove(name)
        self.event_pool.current = discovered[0]
        self.state.pending_event_queue = discovered[1:]
        event = self.event_pool.events[discovered[0]]
        return {
            "success": True,
            "action": f"探索{tier}档",
            "result": {
                "tier": tier,
                "shard_cost": shard_cost,
                "discovered_events": discovered,
                "event": discovered[0],
                "region": event["region"],
                "desc": event["desc"],
                "options": [{"id": option["id"], "text": option["text"]}
                            for option in event["options"]],
                "queued_events_remaining": len(self.state.pending_event_queue),
                "random": random_records,
            },
            "instruction": (f"遭遇【{discovered[0]}】，请先结算；"
                            f"其后还有{len(self.state.pending_event_queue)}个探索事件依次结算"),
        }

    def _pre_battle_weixiu(self, params: dict) -> dict:
        """维修：显式分配耐久，实际补入未耗尽消耗品且不得超过当前耐久上限。"""
        tier = params.get("tier", 1)
        tier_map = {1: (1, 0), 2: (2, 5), 3: (3, 12)}
        if not isinstance(tier, int) or isinstance(tier, bool) or tier not in tier_map:
            self.state.energy += 1
            return {"success": False, "error": "维修档位必须是1/2/3"}
        points, cost = tier_map[tier]
        allocations = params.get("allocations")
        if not isinstance(allocations, list) or not allocations:
            self.state.energy += 1
            return {"success": False, "error": "维修必须用allocations显式分配全部耐久点"}
        refs = {f"consumable:{index}": item for index, item in enumerate(self.state.consumables)}
        totals: dict[str, int] = {}
        for entry in allocations:
            if (not isinstance(entry, dict) or entry.get("item_ref") not in refs
                    or not isinstance(entry.get("amount"), int) or isinstance(entry.get("amount"), bool)
                    or entry["amount"] < 1):
                self.state.energy += 1
                return {"success": False, "error": "维修分配必须包含合法item_ref和正整数amount"}
            ref = entry["item_ref"]
            totals[ref] = totals.get(ref, 0) + entry["amount"]
        if sum(totals.values()) != points:
            self.state.energy += 1
            return {"success": False, "error": f"维修{tier}档必须恰好分配{points}点耐久"}
        for ref, amount in totals.items():
            item = refs[ref]
            if item.current_uses < 1:
                self.state.energy += 1
                return {"success": False, "error": f"【{item.name}】耐久已归零，不能维修"}
            if item.current_uses + amount > item.max_uses:
                self.state.energy += 1
                return {"success": False,
                        "error": f"【{item.name}】维修后将超过耐久上限{item.max_uses}"}
        if cost > 0 and self.state.shards < cost:  # 负债口径：0费维修不属于支出
            self.state.energy += 1
            return {"success": False, "error": f"碎片不足，需要{cost}"}

        self.state.shards -= cost
        repaired = []
        for ref, amount in totals.items():
            item = refs[ref]
            item.current_uses += amount
            repaired.append({"item_ref": ref, "item": item.name, "amount": amount,
                             "current_uses": item.current_uses, "max_uses": item.max_uses})
        return {"success": True, "action": "维修",
                "result": {"durability_points": points, "shard_cost": cost,
                           "allocations": repaired}}

    def _pre_battle_guyong(self, params: dict) -> dict:
        """
        雇佣（罪孽都市专属）：diy一位微光者员工。
        20点基础预算：1点分配值=12血限；3点分配值=1攻击次数+2攻击力（捆绑购买，不可拆分）。
        新员工默认 is_deployed=False（待命不占场），需在战斗中调用 deploy_employee 消耗1出手派遣。
        受出战支援/黑名单机制全局约束（is_blacklisted 时禁止本动作）。
        """
        if self.state.is_blacklisted:
            self.state.energy += 1
            return {"success": False, "error": "已被列入失信黑名单，本次轮回无法再执行雇佣"}
        name = params.get("name", "")
        if not name:
            self.state.energy += 1
            return {"success": False, "error": "必须指定员工名称(name)"}
        if any(e.name == name for e in self.state.employees):
            self.state.energy += 1
            return {"success": False, "error": f"已存在同名员工: {name}，请更换名称"}
        blood_alloc = params.get("blood_alloc", 0)
        atk_bundles = params.get("atk_bundles", 0)
        if (not isinstance(blood_alloc, int) or isinstance(blood_alloc, bool)
                or not isinstance(atk_bundles, int) or isinstance(atk_bundles, bool)
                or blood_alloc < 0 or atk_bundles < 0 or blood_alloc + 3 * atk_bundles != 20):
            return {"success": False,
                    "error": "预算分配非法：blood_alloc + 3×atk_bundles 必须恰好等于20，且均为非负整数"
                             "（允许攻击次数为0；1点分配值=12血限；每3点分配值捆绑购买1攻击次数+2攻击力）"}
        blood_limit = blood_alloc * 12
        attack_count = atk_bundles
        attack_power = atk_bundles * 2
        emp = Entity(name=name, entity_type="员工", blood_limit=blood_limit, current_hp=blood_limit,
                     attack_count=attack_count, attack_power=attack_power, is_deployed=False)
        self.state.employees.append(emp)

        # diy后"发现并选择一种转化道纹"：从19个转化道纹中随机抽取3个未持有的(全部19个都未持有)，
        # 引擎自动生成随机数抽取(DiceEngine.auto_roll)，玩家从3个候选里选1个，调用 choose_hired_daowen 完成
        remaining = list(DaoWenEngine.TRANSFORMED_DAOWEN)
        discovered = []
        for i in range(3):
            roll = self.dice.auto_roll(f"hire_daowen_discovery_{name}_{i}", remaining,
                                        context=f"雇佣{name}-发现转化道纹")
            discovered.append(roll["selected"])
            remaining.remove(roll["selected"])
        self.state.pending_daowen_choices[name] = discovered

        return {
            "success": True, "action": "雇佣",
            "result": {"name": name, "blood_limit": blood_limit, "attack_count": attack_count,
                       "attack_power": attack_power, "is_deployed": False,
                       "discovered_daowen_choices": discovered,
                       "note": "已加入员工名单，默认待命不占场；战斗中需调用 deploy_employee 消耗1出手派遣出战后才会参战与计入战终工资结算。"
                                "请从 discovered_daowen_choices 中调用 choose_hired_daowen(name, daowen) 选择1种转化道纹"},
        }

    def _action_choose_hired_daowen(self, params: dict) -> dict:
        """雇佣的diy后置步骤：从发现的3个转化道纹候选中选择1种，赋予该员工"""
        name = params.get("name", "")
        daowen_name = params.get("daowen", "")
        choices = self.state.pending_daowen_choices.get(name)
        if choices is None:
            return {"success": False, "error": f"{name}没有待选择的转化道纹（未雇佣该员工/已选择过）"}
        if daowen_name not in choices:
            return {"success": False, "error": f"{daowen_name}不在候选范围内: {choices}"}
        emp = next((e for e in self.state.employees if e.name == name), None)
        if emp is None:
            return {"success": False, "error": f"找不到员工: {name}"}
        emp.dao_wen[daowen_name] = DaoWenInstance(
            DaoWen(name=daowen_name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
        del self.state.pending_daowen_choices[name]
        return {"success": True, "action": "选择转化道纹",
                "result": {"employee": name, "chosen": daowen_name,
                           "employee_daowen": list(emp.dao_wen.keys())}}

    # ==================== 员工背叛（三选一处理分支） ====================

    def _pending_rebellion_error(self, force: bool) -> Optional[dict]:
        """未强制触发时，必须存在[战终]检查命中的待处理背叛才能选择处理分支"""
        if force:
            return None
        if not self.state.rebellion_active:
            return {"success": False, "error": "当前没有待处理的员工背叛"}
        return None

    def _action_suppress_rebellion(self, params: dict) -> dict:
        return handle_suppress_rebellion(self, params)

    def _action_resolve_rebellion_battle(self, params: dict) -> dict:
        return handle_resolve_rebellion_battle(self, params)

    def _action_appease_rebellion(self, params: dict) -> dict:
        return handle_appease_rebellion(self, params)

    def _action_negotiate_rebellion(self, params: dict) -> dict:
        return handle_negotiate_rebellion(self, params)

    def _pre_battle_lianxin(self, params: dict) -> dict:
        """炼心（龙心谷专属，局外版：已消耗1精力，见_action_pre_battle统一扣减）"""
        self.state.pending_lianxin = True
        return {
            "success": True,
            "action": "炼心",
            "result": {
                "pending_lianxin": True,
                "instruction": "直到你下一次实际支付数值为X的代价后，获得对应类型的【××龙心】(耐久X)"
            }
        }

    # 乱葬岗·附煞：煞气库（6种）
    SHA_QI_LIBRARY = {
        "法煞": "该道纹消耗-X（最低为X）",
        "魂煞": "该道纹的持续X+X（例如持续X变成2X）",
        "冥煞": "该道纹造成的伤害+100%",
        "血煞": "该道纹[回复]+100%",
        "锁煞": "该道纹造成伤害后使[目标]失去等量法力",
        "心煞": "该道纹[战终]冷却已完成战斗场数额外+1",
    }

    def _pre_battle_fusha(self, params: dict) -> dict:
        """附煞（乱葬岗专属）：发现（10碎片）/选择（25碎片）一种煞气，赋予指定道纹。

        每种道纹最多保留1种煞气，重复附煞可覆盖。煞气存于 DaoWenInstance.sha_qi。
        """
        player = self.state.player
        if not player:
            return {"success": False, "error": "没有玩家"}
        mode = params.get("mode", "")
        if mode not in ("发现", "选择"):
            return {"success": False, "error": "mode必须是发现或选择"}
        if mode == "选择":
            sha = params.get("sha_qi", "")
            if sha not in self.SHA_QI_LIBRARY:
                return {"success": False,
                        "error": f"未知煞气「{sha}」；可选：{list(self.SHA_QI_LIBRARY)}"}
            cost = 25
        else:
            # 发现：随机列3件候选并显式选1
            import random as _r
            cands = _r.sample(list(self.SHA_QI_LIBRARY), min(3, len(self.SHA_QI_LIBRARY)))
            self.state.pending_sha_qi_choices = cands
            self.state.pending_sha_qi_mode = mode
            return {"success": True, "action": "附煞·发现",
                    "result": {"sha_qi_candidates": cands, "cost": 10,
                               "instruction": "调用 choose_sha_qi(sha_qi) 选择一种煞气"}}
        daowen = params.get("daowen_name", "")
        if daowen not in player.dao_wen:
            return {"success": False, "error": f"未持有道纹「{daowen}」；持有：{list(player.dao_wen)}"}
        if self.state.shards < cost:
            return {"success": False, "error": f"碎片不足，需要{cost}"}
        self.state.shards -= cost
        inst = player.dao_wen[daowen]
        inst.sha_qi = sha
        return {"success": True, "action": "附煞",
                "result": {"sha_qi": sha, "daowen": daowen, "shard_cost": cost,
                           "effect": self.SHA_QI_LIBRARY[sha], "shards": self.state.shards}}

    def _action_choose_sha_qi(self, params: dict) -> dict:
        """发现模式：从候选煞气中显式选择1种并附给道纹。"""
        player = self.state.player
        cands = self.state.pending_sha_qi_choices or []
        if not cands:
            return {"success": False, "error": "当前没有待选择的煞气发现"}
        sha = params.get("sha_qi", "")
        if sha not in cands:
            return {"success": False, "error": f"只能选择本次发现列出的煞气：{cands}"}
        daowen = params.get("daowen_name", "")
        if daowen not in player.dao_wen:
            return {"success": False, "error": f"未持有道纹「{daowen}」"}
        # 门禁与收费统一为10（发现接口明示{"cost": 10}；旧门禁50是复制残留，
        # 持10~49碎片会被错误拒之门外）。
        if self.state.shards < 10:
            return {"success": False, "error": "碎片不足，需要10"}
        self.state.shards -= 10
        inst = player.dao_wen[daowen]
        inst.sha_qi = sha
        self.state.pending_sha_qi_choices = []
        self.state.pending_sha_qi_mode = ""
        return {"success": True, "action": "附煞·发现选择",
                "result": {"sha_qi": sha, "daowen": daowen, "effect": self.SHA_QI_LIBRARY[sha],
                           "shards": self.state.shards}}

    def _action_lianxin_in_battle(self, params: dict) -> dict:
        """炼心（战斗中版）：不消耗出手，改为下一次局外行动额外多消耗1点精力"""
        self.state.pending_lianxin = True
        self.state.pending_energy_penalty += 1
        return {
            "success": True,
            "action": "炼心(战斗中)",
            "result": {
                "pending_lianxin": True,
                "pending_energy_penalty": self.state.pending_energy_penalty,
                "instruction": "不消耗本回合出手；下一次局外行动将额外多消耗1点精力",
            }
        }

    # 2026-09-28：_pre_battle_wangyou 已删除（局外【忘忧】被删除，忘忧香效果为溢出回复转格挡）。

    def _pre_battle_sacrifice(self, params: dict) -> dict:
        """献祭（需持有终音法器"红头绳"）：衰老3，换取精力+2"""
        if "红头绳" not in self.state.artifacts_owned:
            self.state.energy += 1
            return {"success": False, "error": "没有红头绳，无法执行献祭"}
        player = self.state.player
        if not player:
            self.state.energy += 1
            return {"success": False, "error": "没有玩家"}
        payment = self.combat.pay_numeric_cost(
            player, "衰老", 3,
            cost_share_target_ref=params.get("cost_share_target_ref", ""),
            cost_context={"timing": "pre_battle_action", "source": "红头绳", "source_type": "artifact", "tags": {"active_payment"}})
        self.state.energy += 2
        return {"success": True, "action": "献祭",
                "result": {"cost": payment, "blood_limit": player.blood_limit,
                           "energy": self.state.energy}}

    # ==================== 员工经济系统（出战支援 / 工资 / 黑名单 / 解雇） ====================
    # 全局对所有[员工]生效；"还债"转化来的员工(is_debt_bound=True)独立走负债偿还轨道，不受此处约束。

    WAGE_CAP = 12  # 工资上限[碎片]

    def _blacklist_departure(self, reason: str):
        """记一次员工离队(拒付工资/解雇/死亡)，累计达3触发失信黑名单"""
        self.state.blacklist_level += 1
        if self.state.blacklist_level >= 3:
            self.state.is_blacklisted = True

    # ==================== 出手预算校验 ====================
    # 覆盖：attack/use_daowen/deploy_employee/declare_wish 消耗1出手；
    # declare_escape（逃跑）一次付清5出手（2026-10-13 改制）；
    # consume_item(明确不消耗出手)、use_resonance(可任意时刻插队)不受此约束。

    def _action_budget_of(self, entity: "Entity") -> int:
        """本回合出手预算的唯一口径。

        怪物读正文「每回合 1 次攻击 + 1 种道纹」（single_round_action_count）——
        Entity.action_count 对怪物按速限推导，而怪物面板不含[速限]，恒为 0。
        其余角色读 Entity.action_count（全体基础 2 次，再受【疯狂】/【无力】/【蓄锐·增】修正），
        并叠加遗物【癫狂之脑】的 +1（统一走 combat.single_round_action_count，避免口径分叉）。
        """
        if entity is None:
            return 0
        return self.combat.single_round_action_count(entity)

    def _consume_action_or_error(self, entity: "Entity") -> Optional[dict]:
        """校验entity本回合出手是否用尽；未用尽则消耗1次并返回None，用尽则返回错误dict。

        普通战斗里怪物走 prepare/resolve 两阶段，不经过本函数（直接放行）。
        死斗中守擂侧逐步结算，怪物必须**照常记账**——此前这里对怪物无条件
        return None，怪物的 actions_used_this_round 永不增长，配合
        _duel_side_can_act 的「存活即有余手」就成了事实上的无限出手。
        """
        if entity.entity_type == "怪物" and not self.state.in_final_duel:
            return None
        if not self.combat.can_act(entity):
            return {"success": False,
                    "error": f"{entity.name}无法出手（束缚）"}
        breath = self.combat.apply_opposing_longxi(entity)
        if breath and not entity.is_alive:
            return {"success": False, "error": f"{entity.name}被龙息命零",
                    "dragon_breath": breath}
        budget = self._action_budget_of(entity)
        if entity.actions_used_this_round >= budget:
            return {"success": False,
                    "error": f"{entity.name}本回合出手已用完"
                             f"({entity.actions_used_this_round}/{budget})"}
        entity.actions_used_this_round += 1
        return None

    def _consume_actions_or_error(self, entity: "Entity", count: int,
                                    action_label: str | None = None) -> Optional[dict]:
        """原子消耗多次出手；X阶法术/逃跑必须一次付清X次出手机会。"""
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            return {"success": False, "error": "出手消耗必须是正整数"}
        if entity.entity_type == "怪物" and not self.state.in_final_duel:
            return None
        if not self.combat.can_act(entity):
            return {"success": False, "error": f"{entity.name}无法出手（束缚）"}
        budget = self._action_budget_of(entity)
        remaining = budget - entity.actions_used_this_round
        if remaining < count:
            what = action_label if action_label else f"{count}阶法术"
            return {"success": False,
                    "error": f"{what}需一次付清{count}次出手，{entity.name}本回合仅剩{remaining}次"
                             f"({entity.actions_used_this_round}/{budget})"}
        # 龙息只在这次施法声明时结算一次，不按阶级重复触发。
        breath = self.combat.apply_opposing_longxi(entity)
        if breath and not entity.is_alive:
            return {"success": False, "error": f"{entity.name}被龙息命零", "dragon_breath": breath}
        entity.actions_used_this_round += count
        return None

    # ==================== 最终死斗·交替出手校验 ====================

    def _check_duel_turn_or_error(self, actor: "Entity") -> Optional[dict]:
        """死斗中(state.in_final_duel)，出手方必须与state.duel_turn一致，否则拒绝；非死斗时直接放行"""
        if not self.state.in_final_duel:
            return None
        actor_side = "player_side" if actor in self.state.get_all_player_side() else "opponent_side"
        if actor_side != self.state.duel_turn:
            return {"success": False,
                    "error": f"死斗须严格交替出手：当前轮到{self.state.duel_turn}，{actor.name}({actor_side})不能行动"}
        return None

    def _duel_side_can_act(self, side: str) -> bool:
        """该侧是否还有未用完的出手预算（存活且未撤退；get_all_* 已排除死者/撤退者）。

        对称性修复：守擂侧（怪物阶段逐 actor 结算）不能只看 Entity.action_count——
        怪物 action_count 按速限推导恒为0，会导致守擂永远"无余手"、挑战者连动。
        守擂侧"有余手"= 仍有存活敌人未结算（驱动维护已结算集合）。"""
        if side == "player_side":
            entities = self.state.get_all_player_side()
            return any(e.actions_used_this_round < self.combat.single_round_action_count(e)
                       and self.combat.can_act(e) for e in entities)
        # 守擂侧：怪物按正文口径「每回合 1 次攻击 + 1 种道纹」计出手预算
        # （single_round_action_count），与 _consume_action_or_error 同一口径——
        # 此前这里写成"存活未撤退即有余手"，与实际结算门禁不一致：怪物既被
        # 0 预算卡死无法出手，本函数又坚称它还有余手，回合因此永远结束不了。
        # 对手轮回者/盟友按各自出手预算判断（耗尽则连动）。
        return any(
            e.is_alive and not e.has_retreated and self.combat.can_act(e)
            and e.actions_used_this_round < self.combat.single_round_action_count(e)
            for e in self.state.get_all_enemy_side())

    def _advance_duel_turn(self):
        """死斗中一次出手成功结算后：对方还有余手则换边；否则本侧连动，余手不作废。

        对称性修复：原实现只对挑战者侧生效——挑战者每次出手后换边，
        守擂方（怪物阶段）不参与、每回合全量输出，导致守擂方机制性必胜
        （镜像12/12全胜暴露）。修复后守擂侧同样遵守本换边：守擂每步只结算
        1个actor（见 resolve_monster_phase 死斗部分提交），双方逐出手交替，
        任一侧余手耗尽则另一方连动。"""
        if not self.state.in_final_duel:
            return
        other = "opponent_side" if self.state.duel_turn == "player_side" else "player_side"
        if self._duel_side_can_act(other):
            self.state.duel_turn = other

    def _hostile_to(self, actor: Entity, target: Entity) -> bool:
        if target is None or target is actor:
            return False
        actor_on_player = actor in self.state.get_all_player_side()
        target_on_player = target in self.state.get_all_player_side()
        return actor_on_player != target_on_player

    def _resolve_daowen_dodge(self, name: str, actor: Entity, target: Entity,
                              dodge: bool, dodge_targets: list,
                              blood_shadow: bool = False,
                              blood_shadow_cost_share_target_ref: str = "",
                              dodge_relic_target_ref: str = "",
                              wave_count: int = 0, activation_x: int = 0) -> tuple[dict, Optional[list[Entity]]]:
        """结算显式闪避，并直接返回本次AOE目标，避免跨行动保存临时跳过状态。
        波及X：按显式提交逐目标建立/解除波及标记，日志返回 wave_marked/wave_unmarked。"""
        log = {"must_hit": False, "dodged_names": [], "fully_dodged": False}
        hostile_possible = (target is not None and self._hostile_to(actor, target))
        # 必中X self-buff（remaining_rounds>0）：必中但不消耗次数
        _bizhong_st = next((s for s in actor.status_effects if s.name == "必中"), None)
        _real_self_buff = bool(_bizhong_st and _bizhong_st.remaining_rounds > 0)
        if hostile_possible and _real_self_buff:
            log["must_hit"] = True
            return log, None
        if hostile_possible and self.combat.bizhong_remaining(actor) > 0:
            log["must_hit"] = self.combat.consume_bizhong(actor)
            if log["must_hit"]:
                return log, None
        if blood_shadow:
            if (target is None or not self._hostile_to(actor, target)
                    or not self.state.side_has(target, "血影") or target.current_hp <= 10):
                raise ValueError("当前判定不能使用血影")
            self.combat.pay_numeric_cost(
                target, "流血", 10,
                cost_share_target_ref=blood_shadow_cost_share_target_ref,
                cost_context={"timing": "reaction", "source": "血影", "source_type": "relic", "tags": {"active_payment"}})
            log["blood_shadow"] = True
            log["fully_dodged"] = True
            return log, None
        if name == "波及":
            refs = self.combat._combat_entity_refs()
            if not isinstance(dodge_targets, list) or len(dodge_targets) != wave_count:
                raise ValueError(f"波及必须为{wave_count}个目标显式提交dodge_targets")
            received = {}
            for entry in dodge_targets:
                if (not isinstance(entry, dict) or not isinstance(entry.get("target_ref"), str)
                        or not isinstance(entry.get("dodge"), bool)
                        or not isinstance(entry.get("blood_shadow"), bool)
                        or entry["dodge"] and entry["blood_shadow"]):
                    raise ValueError("波及每个目标必须提交合法target_ref/dodge/blood_shadow")
                if entry["target_ref"] in received:
                    raise ValueError("波及dodge_targets不能重复")
                received[entry["target_ref"]] = entry
            wave_marked: list[str] = []
            wave_unmarked: list[str] = []
            for entry in dodge_targets:
                ent = refs.get(entry["target_ref"])
                if ent is None or not ent.is_alive or ent is actor:
                    raise ValueError("波及目标必须为当前存活的非自身角色")
                if log["must_hit"]:
                    marked = self.combat._toggle_wave_mark(ent, actor, activation_x=activation_x)
                    (wave_marked if marked else wave_unmarked).append(ent.name)
                    continue
                if entry["blood_shadow"]:
                    if not self.state.side_has(ent, "血影") or ent.current_hp <= 10:
                        raise ValueError(f"{ent.name}不能使用血影")
                    self.combat.pay_numeric_cost(
                        ent, "流血", 10,
                        cost_share_target_ref=entry.get("cost_share_target_ref", ""),
                        cost_context={"timing": "reaction", "source": "血影", "source_type": "relic", "tags": {"active_payment"}})
                    log["dodged_names"].append({"name": ent.name, "blood_shadow": True})
                elif entry["dodge"]:
                    if ent.current_speed < 1:
                        raise ValueError(f"{ent.name}速度不足")
                    extra = self.combat._spend_dodge_speed(ent, entry.get("dodge_relic_target_ref"))
                    log["dodged_names"].append({"name": ent.name, "speed_after": ent.current_speed, **extra})
                else:
                    marked = self.combat._toggle_wave_mark(ent, actor, activation_x=activation_x)
                    (wave_marked if marked else wave_unmarked).append(ent.name)
            log["wave_marked"] = wave_marked
            log["wave_unmarked"] = wave_unmarked
            log["fully_dodged"] = False
            return log, None
        if not dodge or target is None or not self._hostile_to(actor, target):
            return log, None
        if target.current_speed < 1:
            log["dodge_fail_reason"] = "速度不足"
            return log, None
        extra = self.combat._spend_dodge_speed(target, dodge_relic_target_ref)
        entry = {"name": target.name, "speed_after": target.current_speed}
        if extra:
            entry.update(extra)
        log["dodged_names"].append(entry)
        log["fully_dodged"] = True
        return log, None

    def _action_deploy_employee(self, params: dict) -> dict:
        return handle_deploy_employee(self, params)

    def _action_dismiss_employee(self, params: dict) -> dict:
        return handle_dismiss_employee(self, params)

    def _compute_pending_wages(self):
        """战终首次结算：为每个"存活+已部署+非还债"的员工计算应付工资，写入 pending_wage_decisions。
        已经出现过的key(无论是否已决策)不会被重新计算，避免同一员工在同一场战斗内被反复计费。
        wage_bonus(员工背叛·让利)在封顶后叠加，不影响12碎片的封顶本身。"""
        for e in self.state.employees:
            if e.is_alive and e.is_deployed and not e.is_debt_bound and e.name not in self.state.pending_wage_decisions:
                # current_round 为1-indexed的"当前回合序号"，与 deployed_at_round 同口径，故+1为闭区间计数
                rounds_participated = max(0, self.state.current_round - e.deployed_at_round + 1)
                wage = min(self.WAGE_CAP, 2 * (1 + rounds_participated)) + self.state.wage_bonus
                self.state.pending_wage_decisions[e.name] = wage

    def _action_repay_debt_employee(self, params: dict) -> dict:
        return handle_repay_debt_employee(self, params)

    def _action_pay_employee_wage(self, params: dict) -> dict:
        return handle_pay_employee_wage(self, params)

    # ==================== 战斗行动 ====================

    # ---------- "发动道纹"的共用环节（use_daowen 与瞬发法术每一步共用） ----------

    def _silenced_by_mask(self, actor: Entity, calc: dict) -> bool:
        """缄默面具：持有方无法发动附带代价（非"消耗"法力）的道纹。

        规则实现已上移到 CombatEngine.silenced_by_mask（法术单步与 use_daowen
        共用同一口径），这里保留同名入口供既有调用点使用。
        """
        return self.combat.silenced_by_mask(actor, calc)

    def _pay_daowen_shard_cost(self, actor: Entity, name: str, calc: dict, x: int) -> Optional[str]:
        """消灾X 的碎片类代价。

        规则实现已上移到 CombatEngine.pay_daowen_shard_cost（法术单步与
        use_daowen 共用同一口径），这里保留同名入口供既有调用点使用。
        """
        return self.combat.pay_daowen_shard_cost(actor, name, calc, x)

    def _action_use_daowen(self, params: dict) -> dict:
        """
        发动道纹。
        params.actor 留空时=玩家自行发动道纹(法力制，行为与此前完全一致)。
        params.actor 指定为已部署[朋友]/[员工]时=听从轮回者指令代其发动：
        1.[朋友]/[员工]**照常支付法力**（2026-09-17 用户令裁定；旧版"不持有法力、
          不支付法力"是已废止条文的残留，见 AI_EXPERIENCE.md:278/:1254 微光者持有
          [法限]且为一池制），并额外消耗其出手；
          附带【代价】的道纹仍照常由该实体自身支付代价。
        2.必须指定一个不是其自身的目标(听从指令的道纹/攻击均需面向"其他非自身目标")。
        """
        actor_ref = params.get("actor_ref", "")
        actor_name = params.get("actor", "")  # 旧存档/外部调用仅在名称唯一时兼容
        is_command = False
        refs = self.combat._combat_entity_refs()
        if actor_ref:
            actor = refs.get(actor_ref)
        elif actor_name:
            matches = [entity for entity in refs.values() if entity.name == actor_name]
            actor = matches[0] if len(matches) == 1 else None
        else:
            actor = self.state.player
        # 2026-10-08：被【封印】暂离的实体不在场上，不能发动道纹（含封印自己后
        # 靠默认 actor=player 绕开 refs 的路径——上面 `actor = self.state.player`
        # 是直取单字段，不过 _combat_entity_refs，必须在这里单独挡一道）。
        if self.state.is_sealed_away(actor):
            return {"success": False, "error": f"{actor.name}正处于【封印】暂离，不能发动道纹"}
        if actor is None or not actor.is_alive or actor.has_retreated:
            return {"success": False, "error": "actor_ref不是当前存活行动者"}
        if actor is self.state.player:
            pass
        elif (self.state.in_final_duel and actor.entity_type == "轮回者"
              and actor in self.state.get_all_enemy_side()):
            pass
        elif actor in self.state.friends or actor in [e for e in self.state.employees if e.is_deployed]:
            is_command = True
        else:
            return {"success": False, "error": f"{actor.name}不能作为use_daowen的行动者"}

        name = params.get("daowen_name", "")
        x = params.get("x", 1)
        target_name = params.get("target", "")
        target_ref = params.get("target_ref", "")

        if name not in actor.dao_wen:
            return {"success": False, "error": f"{actor.name}未持有道纹: {name}"}

        # X=0 = 拒绝发动。
        # 轮到发动却不想发动的道纹，用 X=0 显式放弃：视为无效，不支付代价、
        # 不消耗出手、不进入任何结算，也不报错。与「轮空/让行」同一语义：
        # 是一次合法的"我放弃这次机会"，而不是一次失败的尝试。
        # 位置在 can_use() 之前：正在冷却/被封印的道纹同样有权被拒绝。
        if x == 0 and not isinstance(x, bool):
            return {
                "success": True, "action": "use_daowen", "skipped": True,
                "result": {"daowen": name, "x": 0, "skipped": True, "actor": actor.name,
                           "note": f"X=0：【{name}】视为无效，{actor.name}放弃本次发动"
                                   f"（不消耗出手、不支付代价）"},
            }

        dw_instance = actor.dao_wen[name]

        if not dw_instance.can_use():
            return {"success": False, "error": f"道纹{name}不可用（冷却/封印）"}

        if not isinstance(x, int) or isinstance(x, bool) or x < 1:
            return {"success": False, "error": "X必须≥1且为整数"}
        if is_command and dw_instance.x_value > 0 and x != dw_instance.x_value:
            return {"success": False, "error": f"{actor.name}的【{name}】固定为X={dw_instance.x_value}"}

        # 计算函数声明了target即正文含[目标]：缺少显式目标时判定失效，禁止静默改为自身。
        import inspect
        DaoWenEngine.register_all()
        requires_target = "target" in inspect.signature(DaoWenEngine._registry[name]).parameters
        if name == "波及" and not (target_ref or target_name):
            # 波及X：目标由dodge_targets显式提交X个；主引用取首个已提交目标。
            submitted = list(params.get("dodge_targets") or [])
            if submitted and isinstance(submitted[0], dict) and submitted[0].get("target_ref"):
                target_ref = submitted[0]["target_ref"]
        if requires_target and not (target_ref or target_name):
            return {"success": False, "error": f"【{name}】需要显式指定目标target_ref；缺少[目标]时失效"}

        if is_command and not (target_ref or target_name):
            return {"success": False, "error": "听从指令发动道纹必须指定一个非自身的目标"}

        # 使用稳定引用定位目标；旧名称仅在唯一匹配时兼容。
        refs = self.combat._combat_entity_refs()
        target = actor
        if target_ref:
            target = refs.get(target_ref)
            if target is None:
                return {"success": False, "error": "target_ref不是当前合法实体"}
        elif target_name:
            matches = [entity for entity in refs.values() if entity.name == target_name]
            if len(matches) != 1:
                return {"success": False, "error": f"目标名称{target_name}不唯一或不存在，请改用target_ref"}
            target = matches[0]
        if is_command and target is actor:
            return {"success": False, "error": "听从指令发动道纹必须指定一个非自身目标"}
        if target is not actor and not self.combat.is_targetable(actor, target):
            return {"success": False, "error": f"{target.name}处于飞行，无法被选中"}
        # 自愈X「目标死亡时」：显式目标（默认自身不算选中；波及X 记全部已提交目标）。
        _dw_selected = [target] if (target_ref or target_name) else []
        for _dt in params.get("dodge_targets") or []:
            if isinstance(_dt, dict) and _dt.get("target_ref"):
                _ent = refs.get(_dt["target_ref"])
                if _ent is not None:
                    _dw_selected.append(_ent)

        duel_error = self._check_duel_turn_or_error(actor)
        if duel_error:
            return duel_error
        if self.state.phase == "in_combat":
            if not self.combat.can_act(actor):
                return {"success": False, "error": f"{actor.name}当前无法出手"}
            budget = self._action_budget_of(actor)
            if actor.actions_used_this_round >= budget:
                return {"success": False,
                        "error": f"{actor.name}本回合出手已用完({actor.actions_used_this_round}/{budget})"}

        # 调用道纹引擎计算
        # 【分裂】是双参数道纹（X=数量、Y=规模档）；y 缺省 1，单参数道纹不受影响。
        daowen_y = params.get("y")
        if not isinstance(daowen_y, int) or isinstance(daowen_y, bool) or daowen_y < 1:
            daowen_y = 1
        try:
            resolve_kw = {"target": target, "caster": actor}
            resolve_kw["y"] = daowen_y
            # 2026-10-14：【透支】多代价通道（缺省流血；resolve 按签名分发，其余道纹无感）。
            if name == "透支":
                resolve_kw["cost_channel"] = params.get("touzhi_cost", "流血")
            if name == "再生":
                resolve_kw["cost_channel"] = params.get("zaisheng_cost", "法力")
            calc = DaoWenEngine.resolve(name, x, **resolve_kw)
        except Exception as e:
            return {"success": False, "error": f"道纹计算失败: {str(e)}"}
        if calc.get("cost_type") == "攻击力" and calc.get("cost_attack", 0) > actor.effective_attack_power():
            return {"success": False,
                    "error": f"再生攻击力代价需要{calc.get('cost_attack', 0)}点，{actor.name}当前有效攻击力为{actor.effective_attack_power()}"}
        if calc.get("cost_type") == "失忆" and calc.get("cost", 0) > len(actor.dao_wen):
            return {"success": False,
                    "error": f"失忆{calc.get('cost', 0)}无法支付：{actor.name}仅持有{len(actor.dao_wen)}种道纹"}

        # 「发动道纹」的共用前置环节（与法术每一步同一实现，见
        # CombatEngine._daowen_step_preflight）：缄默面具 → 目标发动道纹前反应 →
        # 施法者是否存活。顺序与 cast(flow=...) 的每一步完全相同——这就是
        # R-1 的裁定：法术的一步就是一次发动道纹，只有反应链层数不同。
        from .spell_execution import InterruptReason as _IR
        early, trigger_logs = self.combat._daowen_step_preflight(
            daowen=name, x=x, calc=calc, caster=actor, target=target, refs=refs,
            entry_extra={"trigger_spell_choices": params.get("trigger_spell_choices", {})},
            policy=self.combat.CAST_STEP_POLICY)
        if early is not None:
            if early.reason == _IR.CASTER_DEAD:
                note_action_targets(self.state, actor, _dw_selected)
                return {"success": True, "action": f"{actor.name}发动道纹前被反应法术命零",
                        "result": {"trigger_spell_logs": trigger_logs, "daowen_resolved": False}}
            return {"success": False, "error": early.detail}

        # 检查法力是否足够（代价道纹不消耗法力）
        # 2026-09-17 用户令裁定：微光者（[朋友]/[员工]）**同样支付法力**。
        # 旧注释称其"不持有法力、发动道纹不支付法力，只消耗出手"，自我论证是
        # 「与怪物规则一致」——而怪物那条已于 2026-09-16 用户令废止
        # （见 engine/combat.py 同位置）。正文 AI_EXPERIENCE.md:278 明载
        # 「怪物与轮回者、微光者共用同一套面板数据，同样持有[血限]/[法限]/[速限]」，
        # :1254 明载微光者「仍是一池制，[回始]不回填」——不支出就无所谓"一池制"。
        # 故三类角色同口径支付【消耗】类法力；微光者仍照常额外消耗其出手。
        #
        # 2026-10-08 结构审查：此处的法力支付原为一份内联副本，口径与法术单步
        # （CombatEngine.pay_daowen_mana）表面相同、实则多一条 `cost_mutation`
        # 兜底，属「同一条规则两处实现」。现统一调用共享实现。
        # 等价性已验证：遍历全部 63 个道纹 × X∈{1,3}，凡 cost_type=="消耗" 的
        # calc 都带 `cost` 键；缺 `cost` 只有 `cost_mutation` 的 6 个道纹
        # （全力/减速/封印/必中/疯狂/自愈）全部是「异变」类型，不进法力分支，
        # 故该兜底是死代码，删除后数值逐字节不变。
        _cost_paid, mana_error = self.combat.pay_daowen_mana(actor, calc, target)
        if mana_error:
            return {"success": False, "error": mana_error}

        # 碎片类主动道纹代价预检与支付（目前为消灾；遗物灾厄轮盘无发动/费用）
        shard_error = self._pay_daowen_shard_cost(actor, name, calc, x)
        if shard_error:
            return {"success": False, "error": shard_error}

        if self.state.phase == "in_combat":
            budget_error = self._consume_action_or_error(actor)
            if budget_error:
                return budget_error

        dodge_value = params.get("dodge", False)
        blood_shadow_value = params.get("blood_shadow", False)
        if not isinstance(dodge_value, bool) or not isinstance(blood_shadow_value, bool):
            return {"success": False, "error": "dodge与blood_shadow必须是布尔值"}
        if dodge_value and blood_shadow_value:
            return {"success": False, "error": "不能同时闪避并使用血影"}
        dodge = dodge_value
        dodge_targets = list(params.get("dodge_targets") or [])
        dodge_log, aoe_targets_override = self._resolve_daowen_dodge(
            name, actor, target, dodge, dodge_targets,
            blood_shadow=bool(params.get("blood_shadow", False)),
            blood_shadow_cost_share_target_ref=params.get(
                "blood_shadow_cost_share_target_ref", ""),
            dodge_relic_target_ref=params.get("dodge_relic_target_ref", ""),
            wave_count=int(calc.get("mark_targets", 0)),
            activation_x=x,
        )
        if dodge_log.get("fully_dodged"):
            self._advance_duel_turn()
            if self.state.in_final_duel and not self._duel_side_can_act("player_side") and not self._duel_side_can_act("opponent_side"):
                self.state.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value
            note_action_targets(self.state, actor, _dw_selected)
            return {
                "success": True,
                "action": f"发动道纹【{name}X={x}】" + (f"（{actor.name}听从指令发动）" if is_command else ""),
                "calculation": calc,
                "execution": {"daowen": name, "effects": [], "dodged": dodge_log},
                "dodge": dodge_log,
                "trigger_spell_logs": trigger_logs,
                "state": self.combat._get_combat_state(),
            }

        dragon_heart_use = params.get("dragon_heart_use", 0)
        execution = self._execute_daowen_effect(
            name, calc, actor, target, dragon_heart_use,
            cost_share_target_ref=params.get("cost_share_target_ref", ""),
            aoe_targets_override=aoe_targets_override,
        )
        if dodge_log.get("dodged_names"):
            execution = dict(execution)
            execution["dodged"] = dodge_log
        self._advance_duel_turn()
        if self.state.in_final_duel and not self._duel_side_can_act("player_side") and not self._duel_side_can_act("opponent_side"):
            self.state.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value
        note_action_targets(self.state, actor, _dw_selected)

        return {
            "success": True,
            "action": f"发动道纹【{name}X={x}】" + (f"（{actor.name}听从指令发动）" if is_command else ""),
            "calculation": calc,
            "execution": execution,
            "dodge": dodge_log or None,
            "trigger_spell_logs": trigger_logs,
            "state": self.combat._get_combat_state()
        }

    def _execute_daowen_effect(
        self, name: str, calc: dict, caster: Entity, target: Entity,
        dragon_heart_use: int = 0, *, cost_share_target_ref: str = "",
        aoe_targets_override: Optional[list[Entity]] = None,
    ) -> dict:
        """发动道纹：法力检查后委托combat.apply_daowen_effect。"""
        return self.combat.apply_daowen_effect(
            name, calc, caster, target, dragon_heart_use,
            cost_share_target_ref=cost_share_target_ref,
            aoe_targets_override=aoe_targets_override,
        )

    def _find_resonance_holder(self, source: str, target_ref: str):
        """按稳定引用定位残韵作用的道纹持有者；未指定时只接受唯一持有者。"""
        player = self.state.player
        refs = self.combat._combat_entity_refs()
        # 显式目标优先：不能因为玩家也持有同一道纹而忽略调用方指定的目标。
        if target_ref:
            target = refs.get(target_ref)
            if target is None:
                return None, f"找不到target_ref: {target_ref}"
            if source not in target.dao_wen:
                return None, f"{target.name}未持有道纹: {source}"
            return target, None
        # 未指定目标时，只有场上唯一持有者可被自动选中；玩家也参与唯一性判断。
        holders = [entity for entity in refs.values() if source in entity.dao_wen]
        if len(holders) == 1:
            return holders[0], None
        if not holders:
            return None, f"场上无人持有道纹: {source}"
        return None, f"多名角色持有{source}，请指定target_ref"

    def _permanently_convert_daowen(self, holder: Entity, source: str, dest: str) -> bool:
        """将持有者的源道纹永久变为变化后的道纹；同名只保留一份。"""
        if source not in holder.dao_wen:
            return False
        if holder.entity_type == "怪物" and source in ORIGINAL_MONSTER_DAOWEN:
            holder._had_monster_daowen = True
        old = holder.dao_wen[source]
        if dest not in holder.dao_wen:
            holder.dao_wen[dest] = DaoWenInstance(DaoWen(
                name=dest, formula=f"{dest}X",
                cost_type=old.dao_wen.cost_type,
                cost_formula=old.dao_wen.cost_formula,
                effect_formula=old.dao_wen.effect_formula,
            ), x_value=old.x_value)
        if dest != source:
            del holder.dao_wen[source]
        return True

    def _grant_transformed_daowen(self, player: Entity, dest: str) -> bool:
        """残韵获得变化后道纹。X不从原道纹拷贝；同名不重复。

        2026-10-07 用户令：残韵路径双向 + 完全放开——变化结果是原始怪物道纹时
        施法者同样永久获得（旧版在此处直接拒绝授予，与「双向 + 可永久持有」冲突）。
        """
        if dest in player.dao_wen:
            return False
        # 2026-10-12 用户裁定②：残韵转化补给施法者的这一份是真正的获得，
        # 同样受【承载上限】约束；超出额度就**不授予**（持有者的一换一照常发生）。
        if len(player.dao_wen) >= player.daowen_capacity:
            return False
        player.dao_wen[dest] = DaoWenInstance(DaoWen(
            name=dest, formula=f"{dest}X", cost_type="消耗",
            cost_formula="X", effect_formula=""))
        return True

    def _action_use_resonance(self, params: dict) -> dict:
        """使用残韵。actor_ref 指定施法者（默认 player:0），使守擂者等轮回者也能用自己
        的残韵转化敌人道纹并让自己获得转化后道纹——与挑战者共用同一套机制。"""
        source = params.get("source_daowen", "")
        rtype = params.get("resonance_type", "")
        actor_ref = params.get("actor_ref", "player:0")
        refs = self.combat._combat_entity_refs()
        actor = refs.get(actor_ref)
        if actor is None:
            return {"success": False, "error": f"找不到actor_ref: {actor_ref}"}
        # 施法者残韵库存：玩家侧兼容 State.resonance；守擂者等非玩家轮回者严格用
        # 自己实体级的 resonance（真实准备量，无则 0）。绝不回落借他人的库存——
        # 「有就有，没有就没有」，不给没准备的对手塞残韵。
        if actor is self.state.player:
            stock = self.state.resonance
        else:
            stock = getattr(actor, "resonance", None) or {}
        if not isinstance(stock, dict) or stock.get(rtype, 0) <= 0:
            return {"success": False, "error": f"没有可用的{rtype}残韵（当前：{stock}）"}

        player = self.state.player
        if not player:
            return {"success": False, "error": "没有玩家"}

        holder, holder_err = self._find_resonance_holder(source, params.get("target_ref", "") or "")
        if holder_err:
            return {"success": False, "error": holder_err}

        caster_has = holder is actor

        # 路径双向后，同一（源道纹, 残韵类型）可能通向两个相邻节点
        # （例：【杀伐】的【反转】通向【再生】与【封印】）。歧义时不替发动者挑，
        # 必须显式提交 target_daowen；未指定则原样拒绝并列出候选（残韵不消耗）。
        target_daowen = params.get("target_daowen", "") or ""

        result = ResonanceEngine.apply_resonance(
            source, rtype,
            caster_has_daowen=caster_has,
            target_has_daowen=True,
            resonance_stock=stock,  # 传入施法者残韵库存用于校验
            target_daowen=target_daowen,
        )

        if not result["success"]:
            return {"success": False, "error": result["error"]}

        # 路径与持有者均已确认，此时才消耗（规则1：未生效不消耗）
        stock[rtype] = stock[rtype] - 1

        dest = result["target"]
        holder_changed = self._permanently_convert_daowen(holder, source, dest)
        granted = self._grant_transformed_daowen(actor, dest)
        redemption = None
        if holder.entity_type == "怪物":
            redemption = self.combat.check_redemption(holder)

        second = params.get("second_target_ref", "")
        second_source_daowen = params.get("second_source_daowen", "")
        second_log = None
        if second and any(r.name == "同魂笔" for r in self.state.relics):
            if not second_source_daowen:
                second_log = "同魂笔：必须指定second_source_daowen(第二个目标身上要受影响的道纹)，未生效"
            else:
                second_entity = self.combat._combat_entity_refs().get(second)
                if second_entity is None:
                    second_log = f"同魂笔：找不到目标引用{second}，未生效"
                elif second_source_daowen not in second_entity.dao_wen:
                    second_log = f"同魂笔：{second}未持有{second_source_daowen}，未生效"
                else:
                    r2 = ResonanceEngine.apply_resonance(
                        second_source_daowen, rtype,
                        caster_has_daowen=(second_source_daowen in actor.dao_wen),
                        target_has_daowen=True,
                        target_daowen=params.get("second_target_daowen", "") or "")
                    if r2.get("success"):
                        new_name = r2["target"]
                        self._permanently_convert_daowen(second_entity, second_source_daowen, new_name)
                        if self._grant_transformed_daowen(actor, new_name):
                            second_log = f"同魂笔：{second_entity.name}的{second_source_daowen}永久变为{new_name}，施法者同时永久获得{new_name}"
                        else:
                            second_log = f"同魂笔：{second_entity.name}的{second_source_daowen}永久变为{new_name}；施法者已持有{new_name}"
                        if second_entity.entity_type == "怪物" and not redemption:
                            redemption = self.combat.check_redemption(second_entity)
                    else:
                        second_log = f"同魂笔：{r2.get('error', '未知原因')}，未生效"
        payload = {
            "success": True,
            "action": f"残韵【{rtype}】{source} → {result['target']}",
            "result": result,
            "holder": holder.name,
            "holder_is_player": caster_has,
            "holder_changed": holder_changed,
            "granted_daowen": dest if granted else None,
            "actor": actor.name,
            "second_target_log": second_log,
            "resonance_remaining": dict(stock)
        }
        if redemption:
            payload["redemption"] = redemption
        return payload

    def _redeem_attribute_points(self, allocations: dict) -> dict:
        """把属性点池里的点兑成[血限]/[速限]/[法限]。

        计价与开局初始分配同口径：1属性点=6[血限]，2属性点=1[速限]=1[法限]。
        用户裁定 2026-09-12：局外修行不再与初始分配区分，[血限]同样可以后天提升。
        """
        player = self.state.player
        if player is None:
            return {"success": False, "error": "尚未分配初始属性"}
        alloc_keys = ("blood_points", "speed_points", "mana_points")
        vals = {}
        for key in alloc_keys:
            v = allocations.get(key, 0)
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                return {"success": False, "error": f"{key} 必须是非负整数"}
            vals[key] = v
        need = sum(vals.values())
        if need == 0:
            return {"success": True, "action": "兑换属性点",
                    "result": {"gained": {"blood": 0, "speed": 0, "mana": 0},
                               "attribute_points": self.state.attribute_points}}
        if need > self.state.attribute_points:
            return {"success": False,
                    "error": f"属性点不足：需要{need}，池内{self.state.attribute_points}"}
        if vals["speed_points"] % 2 or vals["mana_points"] % 2:
            return {"success": False,
                    "error": "速限/法限按2属性点一档计价，兑换点数必须是偶数",
                    "instruction": "血限按1点一档不受此限；余点会留在池里，攒够2点再兑"}
        blood_gain = vals["blood_points"] * 6
        # 不朽之躯：[血限]无法增加，兑血限的点会白扔，直接拒绝而不是静默吞点。
        if blood_gain and self.state.side_has(player, "不朽之躯"):
            return {"success": False,
                    "error": "【不朽之躯】使[血限]无法增加，不能把属性点兑成血限",
                    "instruction": "改兑 speed_points / mana_points"}
        self.state.attribute_points -= need
        gained = {"blood": blood_gain,
                  "speed": vals["speed_points"] // 2,
                  "mana": vals["mana_points"] // 2}
        if blood_gain:
            player.blood_limit += blood_gain
            player.current_hp += blood_gain
        player.speed_limit += gained["speed"]
        player.mana_limit += gained["mana"]
        player.current_speed = player.speed_limit
        player.current_mana = player.mana_limit
        return {"success": True, "action": "兑换属性点",
                "result": {"gained": gained,
                           "attribute_points": self.state.attribute_points,
                           "blood_limit": player.blood_limit,
                           "speed_limit": player.speed_limit,
                           "mana_limit": player.mana_limit}}

    def _action_redeem_attribute_points(self, params: dict) -> dict:
        """自由动作：把存下的属性点兑成面板（不耗精力、不耗碎片）。

        限局外：兑换会把当前速度/当前法力一并补到新的上限，战斗内兑换等于免费重置
        法力一池，所以战斗中拒绝。
        """
        if self.state.phase != "pre_battle":
            return {"success": False,
                    "error": "属性点只能在局外兑换（战斗内兑换会连带补满当前速度/法力，等于重置一池）"}
        allocations = params.get("allocations")
        if not isinstance(allocations, dict):
            return {"success": False,
                    "error": "请用 allocations 指定兑换："
                             "{'blood_points': 非负整数, 'speed_points': 偶数, 'mana_points': 偶数}",
                    "attribute_points": self.state.attribute_points}
        return self._redeem_attribute_points(allocations)

    def _action_declare_parry(self, params: dict) -> dict:
        """兼容旧调用；招架现已改为每一击在 resolve_attack 中提交。"""
        return {"success": False,
                "error": "招架不再是回合姿态；请在resolve_attack的对应hit中提交parry=true"}

    # ---------- 2026-09-28 新回合动作：cast/focus/rest ----------

    def _action_cast(self, params: dict) -> dict:
        """施法：发动道纹或执行统一自定义施法。

        params:
            kind: "daowen" 或 "spell"（默认 "daowen"）
            kind=spell 时透传给统一的 define_spell 语义。
        """
        if "flow" in params or "huangfu_spell" in params:
            # Phase 2：cast(flow=...) = 瞬发法术（trigger=瞬发, lifecycle=instant）。
            # 独立入口，不转发给普通道纹发动或自定义施法定义。
            # 2026-10-14 审计F4：【黄符】施法（huangfu_spell）同走瞬发入口，
            # 流程文本取自受授法术本身。
            if params.get("kind") not in (None, "flow", "instant"):
                return {"success": False,
                        "error": "cast(flow=...)不能与kind=daowen/spell同时使用"}
            return self._action_cast_instant(params)
        kind = params.get("kind", "daowen")
        sub = {k: v for k, v in params.items() if k != "kind"}
        if kind == "daowen":
            return self._action_use_daowen(sub)
        if kind == "spell":
            return self._action_define_spell(sub)
        return {"success": False, "error": f"cast.kind 必须是 daowen 或 spell，收到 {kind!r}"}

    def _action_cast_instant(self, params: dict) -> dict:
        """cast(flow=...)：一次消耗1出手的瞬发 SpellExecution。

        params:
            actor_ref: 施法者（默认 player:0）
            flow: 效果流程文本，如 "发动杀伐X于目标→发动再生X于自身"
            target_ref: 本次施法的[目标]（流程含"于目标"时必填）
            steps: 每步决策列表 [{x, target_ref?, dodge?}, ...]，一次性完整提交
            name: 可选显示名（默认"瞬发法术"），仅用于日志

        语义：
        - 提交结构非法（句式/步数/X/目标）= 契约错误：返回 success=False，不扣出手；
        - 结构合法则扣 1 次出手（整次 cast 只扣一次，与步骤数无关），
          然后按步骤顺序逐步执行；每一步都以执行时真实法力/速度判定；
        - 资源不足等正常中断：已结算步骤保留，execution.status=interrupted，
          返回 success=True（施法动作本身发生了）；
        - lifecycle=instant：不写入 entity.spells，不留任何绑定。
        """
        actor_ref = params.get("actor_ref", "player:0")
        refs = self.combat._combat_entity_refs()
        actor = refs.get(actor_ref)
        if actor is None or not actor.is_alive or actor.has_retreated:
            return {"success": False, "error": "actor_ref不是当前存活行动者"}
        if self.state.phase != GamePhase.IN_COMBAT.value:
            return {"success": False, "error": "瞬发法术只能在战斗中施放"}
        if actor.entity_type == "怪物" and not self.state.in_final_duel:
            return {"success": False, "error": "普通怪物必须通过怪物阶段行动"}
        duel_err = self._check_duel_turn_or_error(actor)
        if duel_err:
            return duel_err
        if not self.combat.can_act(actor):
            return {"success": False, "error": f"{actor.name}当前无法行动"}
        target_ref = params.get("target_ref")
        cast_target = None
        if target_ref is not None:
            cast_target = refs.get(target_ref)
            if cast_target is None:
                return {"success": False, "error": "target_ref不是当前合法实体"}
        # 2026-10-14 审计F4：【黄符】施法——朋友/员工发动受授予的法术，以法力为能源。
        # 流程文本取自受授法术本身（忽略 params.flow）；所需道纹在施法前后临时授予/
        # 收回（黄符以法力替代道纹持有要求，不在施法核心开旁路）。
        huangfu_name = params.get("huangfu_spell", "")
        huangfu_spell = None
        huangfu_temp_granted: list[str] = []
        flow_text = params.get("flow")
        if huangfu_name:
            huangfu_spell = next((sp for sp in actor.spells if sp.name == huangfu_name), None)
            if huangfu_spell is None or (actor.runtime_id, huangfu_name) not in self.state.huangfu_grants():
                return {"success": False, "error": f"{actor.name}未经【黄符】获授法术「{huangfu_name}」"}
            flow_text = huangfu_spell.effect_flow
            for daowen in huangfu_spell.required_daowen:
                if daowen not in actor.dao_wen:
                    actor.dao_wen[daowen] = DaoWenInstance(DaoWen(
                        name=daowen, formula="", cost_type="", cost_formula="", effect_formula=""))
                    huangfu_temp_granted.append(daowen)
        spell_name = params.get("name") or huangfu_name or "瞬发法术"
        try:
            execution = self.combat.build_instant_execution(
                actor, flow_text, cast_target, params.get("steps"), refs,
                spell_name=str(spell_name), max_iterations=params.get("max_iterations"))
        except ValueError as exc:
            for daowen in huangfu_temp_granted:
                actor.dao_wen.pop(daowen, None)
            return {"success": False, "error": str(exc)}
        # 法术本质上就是依次发动多种道纹：每一步都必须提交
        # 敌方「目标发动道纹前」反应的决策，口径与 use_daowen 的 trigger_spell_choices 相同。
        # 在扣出手之前校验：提交不合法属于契约错误，不扣出手。
        for idx, entry in enumerate(execution.request.steps, 1):
            try:
                self.combat.validate_daowen_trigger_spells(
                    actor, entry.extra.get("trigger_spell_choices", {}), refs)
            except ValueError as exc:
                return {"success": False,
                        "error": f"瞬发法术第{idx}步的trigger_spell_choices非法：{exc}"}
        # 结构合法：按【法术阶级规则】整次 cast 一次付清 X 次出手
        # （X = 用到的不同道纹种数；见 法术索引.md「法术阶级规则」——
        #  施展或定义X阶法术必须在声明时一次性消耗X次出手机会，出手不足时不能开始施法）。
        # 出手不按步骤数扣；此后无论哪一步中断（法力不足等），出手都已用掉——施法失败不退出手。
        spell_tier = max(1, len(set(execution.definition.required_daowen)))
        huangfu_mana = 0
        if huangfu_spell is not None:
            huangfu_mana = max(1, huangfu_spell.rank or 1)
            if actor.current_mana < huangfu_mana:
                for daowen in huangfu_temp_granted:
                    actor.dao_wen.pop(daowen, None)
                return {"success": False,
                        "error": f"【黄符】施法需要{huangfu_mana}点法力，{actor.name}当前{actor.current_mana}"}
        budget_err = self._consume_actions_or_error(actor, spell_tier)
        if budget_err:
            for daowen in huangfu_temp_granted:
                actor.dao_wen.pop(daowen, None)
            return budget_err
        if huangfu_spell is not None:
            actor.current_mana -= huangfu_mana
        try:
            execution.run_all()
        finally:
            for daowen in huangfu_temp_granted:
                actor.dao_wen.pop(daowen, None)
        from .spell_execution import ExecutionStatus
        # 一次 cast = 一次施法声明：死斗轮次推进一次（与 use_daowen 同口径）
        self._advance_duel_turn()
        if (self.state.in_final_duel and not self._duel_side_can_act("player_side")
                and not self._duel_side_can_act("opponent_side")):
            self.state.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value
        status = execution.status.value
        reason = execution.interrupt_reason.value or None
        step_logs = [log for idx, step_log in enumerate(execution.logs())
                     for log in (execution.step_extras[idx].get("trigger_spell_logs", [])
                                 + [step_log])]
        if execution.status == ExecutionStatus.FAILED:
            # 契约错误（道纹计算异常等）：出手已扣，但必须如实报错而不是吞掉
            return {"success": False,
                    "error": f"瞬发法术结算失败({reason}): {execution.interrupt_detail}",
                    "result": {"execution_status": status,
                               "step_results": execution.step_summaries(),
                               "steps": step_logs}}
        cast_result = {
            "spell": spell_name,
            "trigger": execution.definition.trigger.value,
            "lifecycle": execution.definition.lifecycle.value,
            "execution_status": status,
            "interrupt_reason": reason,
            "interrupt_detail": execution.interrupt_detail or None,
            "step_results": execution.step_summaries(),
            "steps": step_logs,
            "tier": spell_tier,
            "cost": f"{spell_tier}次主动出手",
        }
        if huangfu_spell is not None:
            cast_result["huangfu_mana_spent"] = huangfu_mana
        # 自愈X「目标死亡时」：记持有者本次施法的显式目标。
        _cast_refs = [params.get("target_ref")] + [
            st.get("target_ref") for st in params.get("steps") or []
            if isinstance(st, dict)]
        note_action_targets(self.state, actor,
                            [refs.get(r) for r in _cast_refs if r])
        return {"success": True, "action": f"{actor.name}施放{spell_name}",
                "result": cast_result}

    def _action_focus(self, params: dict) -> dict:
        """聚能：消耗1次主动出手，恢复50%已损法力，向上取整。"""
        actor_ref = params.get("actor_ref", "player:0")
        refs = self.combat._combat_entity_refs()
        actor = refs.get(actor_ref)
        if actor is None or not actor.is_alive:
            return {"success": False, "error": "actor_ref不是当前存活行动者"}
        if actor.entity_type == "怪物" and not self.state.in_final_duel:
            return {"success": False, "error": "普通怪物必须通过怪物阶段行动"}
        duel_err = self._check_duel_turn_or_error(actor)
        if duel_err:
            return duel_err
        if not self.combat.can_act(actor):
            return {"success": False, "error": f"{actor.name}当前无法行动"}
        # 出手机会校验
        budget_err = self._consume_action_or_error(actor)
        if budget_err:
            return budget_err
        # 恢复已损法力的50%，向上取整；满法力时聚能不产生资源。
        import math
        missing_mana = max(0, actor.mana_limit - actor.current_mana)
        gain = math.ceil(missing_mana * 50 / 100)
        if gain <= 0:
            return {"success": True, "action": f"{actor.name}聚能",
                    "result": {"actor": actor.name, "mana_gained": 0,
                               "mana_before": actor.current_mana,
                               "mana_after": actor.current_mana,
                               "note": f"{actor.name}当前法力已满，聚能未获法力"}}
        mana_before = actor.current_mana
        # 2026-10-08：走统一入口，【勾魂】（无法获得[法力]）期间增益不生效
        gained = actor.gain_mana(gain)
        return {"success": True, "action": f"{actor.name}聚能",
                "result": {"actor": actor.name, "mana_gained": gained,
                           "mana_before": mana_before,
                           "mana_after": actor.current_mana}}

    def _action_rest(self, params: dict) -> dict:
        """蓄锐：消耗1次主动出手，下回合出手+1。"""
        actor_ref = params.get("actor_ref", "player:0")
        refs = self.combat._combat_entity_refs()
        actor = refs.get(actor_ref)
        if actor is None or not actor.is_alive:
            return {"success": False, "error": "actor_ref不是当前存活行动者"}
        if actor.entity_type == "怪物" and not self.state.in_final_duel:
            return {"success": False, "error": "普通怪物必须通过怪物阶段行动"}
        duel_err = self._check_duel_turn_or_error(actor)
        if duel_err:
            return duel_err
        if not self.combat.can_act(actor):
            return {"success": False, "error": f"{actor.name}当前无法行动"}
        budget_err = self._consume_action_or_error(actor)
        if budget_err:
            return budget_err
        actor._xurui_pending = int(getattr(actor, "_xurui_pending", 0) or 0) + 1
        return {"success": True, "action": f"{actor.name}蓄锐",
                "result": {"actor": actor.name,
                           "xurui_pending": actor._xurui_pending,
                           "note": "下[回始]获得【蓄锐·增】：出手+1持续1回合"}}

    def _action_prepare_attack(self, params: dict) -> dict:
        """第一阶段：绑定一次行动的逐击目标、闪避、血影和法术反应选项。"""
        if self.state.pending_attack:
            return {"success": False, "error": "已有待提交攻击"}
        refs = self.combat._combat_entity_refs()
        actor_ref = params.get("actor_ref") or "player:0"
        attacker = refs.get(actor_ref)
        if attacker is None or not attacker.is_alive:
            return {"success": False, "error": "actor_ref不是当前存活行动者"}
        if attacker.entity_type == "怪物" and not self.state.in_final_duel:
            return {"success": False, "error": "普通怪物必须通过怪物阶段行动"}
        duel_error = self._check_duel_turn_or_error(attacker)
        if duel_error:
            return duel_error
        if not self.combat.can_act(attacker):
            return {"success": False, "error": f"{attacker.name}当前无法行动"}
        if attacker.actions_used_this_round >= self._action_budget_of(attacker):
            return {"success": False, "error": f"{attacker.name}本回合出手已用完"}

        target_refs = [ref for ref, entity in refs.items()
                       if self.state.on_player_side(entity) != self.state.on_player_side(attacker)
                       and self.combat.is_targetable(attacker, entity)]
        if (self.state.on_enemy_side(attacker) and "龙威" in self.state.dragon_traits
                and "player:0" in refs):
            target_refs = ["player:0"]
        if (self.state.on_player_side(attacker) and "龙威" in self.state.opponent_dragon_traits):
            opponent_ref = next((ref for ref, entity in refs.items()
                                 if entity.entity_type == "轮回者" and self.state.on_enemy_side(entity)), None)
            if opponent_ref:
                target_refs = [opponent_ref]
        if not target_refs and attacker.effective_attack_count() > 0:
            return {"success": False, "error": "没有合法攻击目标"}

        target_options = []
        for ref in target_refs:
            target = refs[ref]
            blood_pact_options = self.combat.blood_shadow_cost_share_options(target)
            target_options.append({
                "ref": ref, "name": target.name,
                "can_dodge": target.current_speed > 0,
                "can_parry": target.current_speed > 0,
                "can_blood_shadow": (self.state.side_has(target, "血影")
                                     and (target.current_hp > 10 or bool(blood_pact_options))),
                "blood_shadow_cost_share_target_options": blood_pact_options,
                "spell_options": self.combat.prepare_spell_reactions(target, attacker),
                "dodge_relic_target_options": [
                    {"ref": r, "name": e.name} for r, e in refs.items()
                    if self.state.on_player_side(e) != self.state.on_player_side(target) and e.is_alive
                ] if self.state.side_has(target, "回锋刀") else [],
            })
        options = {
            "actor_ref": actor_ref,
            "actor": attacker.name,
            # DM裁定 2026-09-10：轮回者的击数=当前速度（换算仅限轮回者）
            "hit_count": attacker.effective_attack_count(),
            "damage_budget": attacker.effective_attack_count() * attacker.effective_attack_power(),
            "target_options": target_options,
            "hits_schema": [{
                "target_ref": [o["ref"] for o in target_options],
                "damage": "非负整数；所有击的总和不得超过damage_budget",
                "dodge": "boolean", "parry": "boolean", "blood_shadow": "boolean",
                "dodge_relic_target_ref": "持有回锋刀且闪避时必填",
                "spell_choices": "按目标spell_options完整提交",
            } for _ in range(attacker.effective_attack_count())],
        }
        token = uuid.uuid4().hex
        self.state.pending_attack = {"token": token, "round": self.state.current_round, "options": options}
        return {"success": True, "action": "准备攻击", "result": {"token": token, **options}}

    def _action_resolve_attack(self, params: dict) -> dict:
        """第二阶段：只接受prepare快照中的逐击完整选择并原子结算。"""
        pending = self.state.pending_attack
        if not pending:
            return {"success": False, "error": "请先调用prepare_attack"}
        if params.get("token") != pending.get("token") or pending.get("round") != self.state.current_round:
            return {"success": False, "error": "攻击token无效或已过期"}
        hits = params.get("hits")
        options = pending["options"]
        if not isinstance(hits, list) or len(hits) != options["hit_count"]:
            return {"success": False, "error": f"必须提交{options['hit_count']}个hits"}
        refs = self.combat._combat_entity_refs()
        attacker = refs.get(options["actor_ref"])
        if attacker is None or not attacker.is_alive:
            return {"success": False, "error": "攻击者已失效"}
        legal = {entry["ref"]: entry for entry in options["target_options"]}
        speed_spend: dict[str, int] = {}
        blood_spend: dict[str, int] = {}
        for index, hit in enumerate(hits):
            if not isinstance(hit, dict) or hit.get("target_ref") not in legal:
                return {"success": False, "error": f"第{index + 1}击目标不在prepare快照中"}
            # 旧客户端未提交新字段时保持旧等伤分刀；新客户端可显式空刀/集中伤害。
            hit.setdefault("damage", attacker.effective_attack_power())
            hit.setdefault("parry", False)
            if (not isinstance(hit.get("damage"), int) or isinstance(hit.get("damage"), bool)
                    or hit["damage"] < 0):
                return {"success": False, "error": "每击damage必须是非负整数"}
            if (not isinstance(hit.get("dodge"), bool) or not isinstance(hit.get("parry"), bool)
                    or not isinstance(hit.get("blood_shadow"), bool)):
                return {"success": False, "error": "每击必须显式提交布尔值dodge、parry与blood_shadow"}
            if sum((hit["dodge"], hit["parry"], hit["blood_shadow"])) > 1:
                return {"success": False, "error": "同一次判定只能选择闪避、招架、血影之一"}
            ref = hit["target_ref"]
            target = refs.get(ref)
            if target is None:
                return {"success": False, "error": "prepare攻击目标已失效"}
            if hit["dodge"] and self.combat.bizhong_remaining(attacker) <= index:
                speed_spend[ref] = speed_spend.get(ref, 0) + 1
                if speed_spend[ref] > target.current_speed:
                    return {"success": False, "error": f"{target.name}速度不足以完成全部闪避"}
                if self.state.side_has(target, "回锋刀"):
                    rr = hit.get("dodge_relic_target_ref")
                    allowed = {x["ref"] for x in legal[ref]["dodge_relic_target_options"]}
                    if rr not in allowed:
                        return {"success": False, "error": "回锋刀触发必须显式提交合法dodge_relic_target_ref"}
            if hit["blood_shadow"]:
                if not legal[ref]["can_blood_shadow"]:
                    return {"success": False, "error": f"{target.name}不能使用血影"}
                try:
                    _, owner_part, ally, ally_part = self.combat.validate_numeric_cost(
                        target, "流血", 10, hit.get("cost_share_target_ref", ""))
                except ValueError as exc:
                    return {"success": False, "error": str(exc)}
                for bearer, part in ((target, owner_part), (ally, ally_part)):
                    if bearer is None or part <= 0:
                        continue
                    key = id(bearer)
                    blood_spend[key] = blood_spend.get(key, 0) + part
                    if blood_spend[key] >= bearer.current_hp:
                        return {"success": False,
                                "error": f"{bearer.name}生命不足以支付全部血影分担"}
            self.combat.validate_spell_reaction_submission(
                target, attacker, hit.get("spell_choices"), refs,
            )
        allocated_damage = sum(hit["damage"] for hit in hits)
        if allocated_damage > options["damage_budget"]:
            return {"success": False,
                    "error": f"分配总伤害{allocated_damage}超过上限{options['damage_budget']}"}
        # 闪避和招架都预付1速度；招架真刀会在单击结算后返还。
        for ref in legal:
            defense_spend = sum(1 for hit in hits if hit["target_ref"] == ref
                                and (hit["dodge"] or hit["parry"]))
            target = refs.get(ref)
            if target is not None and defense_spend > target.current_speed:
                return {"success": False, "error": f"{target.name}速度不足以完成全部防御"}

        duel_error = self._check_duel_turn_or_error(attacker)
        if duel_error:
            return duel_error
        budget_error = self._consume_action_or_error(attacker)
        if budget_error:
            return budget_error
        results = []
        for index, hit in enumerate(hits):
            target = refs[hit["target_ref"]]
            if not target.is_alive:
                results.append({"hit_index": index + 1, "skipped": "目标已命零"})
                continue
            result = self.combat.resolve_attack(
                attacker, target, dodge=hit["dodge"], parry=hit["parry"],
                allocated_damage=hit["damage"], blood_shadow=hit["blood_shadow"],
                spell_choices=hit.get("spell_choices") or {}, entity_refs=refs,
                dodge_relic_target_ref=hit.get("dodge_relic_target_ref"),
                cost_share_target_ref=hit.get("cost_share_target_ref", ""),
            )
            result["hit_index"] = index + 1
            results.append(result)
        self.state.pending_attack = {}
        self._advance_duel_turn()
        if self.state.in_final_duel and not self._duel_side_can_act("player_side") and not self._duel_side_can_act("opponent_side"):
            self.state.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value
        # 自愈X「目标死亡时」（2026-10-14 用户裁定）：记持有者本轮选中的目标。
        note_action_targets(self.state, attacker,
                            [refs.get(hit["target_ref"]) for hit in hits])
        return {"success": True, "action": f"{attacker.name}结算一轮攻击",
                "result": {"attacker": attacker.name, "hits": results}}

    def _action_declare_wish(self, params: dict) -> dict:
        """声明许愿（消耗玩家1出手）：轮回者向"某人"祈求。

        愿望没有固定的可行范围，也不存在"无法实现"的愿望；"某人"会以能够
        实现愿望、但最符合其扭曲本质的方式实现；代价与扭曲方式由愿望本身
        决定，不预先公开。引擎只提交现场状态，实现方式与代价由 DM 裁定。
        """
        player = self.state.player
        if not player:
            return {"success": False, "error": "没有玩家"}
        wish_text = params.get("wish_text", "")
        if not isinstance(wish_text, str) or not wish_text.strip():
            return {"success": False, "error": "必须写明许下的愿望（wish_text）"}
        wish_text = wish_text.strip()
        target = self.combat._combat_entity_refs().get(params.get("target_ref", ""))
        if params.get("target_ref") and not target:
            return {"success": False, "error": "target_ref不是当前合法目标"}

        budget_error = self._consume_action_or_error(player)
        if budget_error:
            return budget_error

        interrupt = self.combat.initiate_wish(player, wish_text, target)
        self._pending_interrupts.append(interrupt)

        return {
            "success": True,
            "action": "声明许愿",
            "interrupt": interrupt.to_dict(),
            "instruction": "需要DM裁定「某人」以何种扭曲方式实现愿望及轮回者付出的代价"
        }

    ESCAPE_ACTION_COST = 5

    def _action_declare_escape(self, params: dict) -> dict:
        """逃跑（2026-10-13 改制）：轮回者行动，一次付清5次出手，
        以当前状态强制脱离本场战斗，不再走DM追击裁定。

        前置：非最终死斗；未持有生效中的【不离】（无法逃跑/撤退/被移出）；
        本轮剩余出手≥5（原子消费，不足则一次不扣）。
        脱战清理口径沿用原【买路财】撤退：敌方清空、临时朋友解散、员工撤部署、
        朋友/员工撤退标记重置、精力回满、阶段回 pre_battle；不产生击杀碎片奖励。
        """
        escaper = self.state.player
        if not escaper:
            return {"success": False, "error": "没有玩家"}
        if self.state.in_final_duel:
            return {"success": False, "error": "最终死斗无法逃跑"}
        if any(r.name == "不离" for r in self.state.relics
               if self.state.sealed_relics.get(r.name, 0) <= 0):
            return {"success": False, "error": "【不离】：无法逃跑、撤退，也无法被移出本场战斗"}

        budget_error = self._consume_actions_or_error(
            escaper, self.ESCAPE_ACTION_COST, action_label="逃跑")
        if budget_error:
            return budget_error

        # 强制脱战（清理口径 = 原买路财撤退，不改语义）
        self.state.enemies.clear()
        self.state.temp_friends.clear()
        # 2026-10-14：逃跑不走战终结算，【增殖】战终待决随脱战作废（不得带入下场）。
        self.state.event_modifiers.pop("zengzhi_pending", None)
        for emp in self.state.employees:
            if emp.is_alive and not emp.is_debt_bound:
                emp.is_deployed = False
                emp.deployed_at_round = 0
        for ally in self.state.friends + self.state.employees:
            ally.has_retreated = False
        self.state.energy = self.state.energy_budget  # 无尽模式逐轮递减，见 GameState.energy_budget
        self.state.phase = "pre_battle"

        return {
            "success": True, "action": "逃跑·强制脱战",
            "result": {"actions_paid": self.ESCAPE_ACTION_COST,
                       "actions_used": escaper.actions_used_this_round,
                       "note": "本场战斗结束，未击败任何怪物，不产生击杀碎片奖励"},
        }

    def _action_choose_diandang_daowen(self, params: dict) -> dict:
        """领取【典当】发现：从当前第一组3选1里选1种获得；不消耗出手。

        领取时重验学习口径（发现之后获得的道纹可能让候选过期）：
        本组全部过期→本组作废；所选不在本组→报错不消耗；承载已满→报错不消耗。
        战斗内/局外均可领取（无阶段门禁，避免待领卡死后续流程）。
        """
        player = self.state.player
        if not player:
            return {"success": False, "error": "没有玩家"}
        queue = self.state.pending_diandang_discoveries
        if not queue:
            return {"success": False, "error": "当前没有待领取的【典当】发现"}
        group = list(queue[0])
        valid = [n for n in group
                 if learnability_error(n, owned=set(player.dao_wen),
                                       region=self.state.current_region,
                                       known=DaoWenEngine.list_all()) is None]
        if not valid:
            queue.pop(0)
            return {"success": True, "action": "领取典当发现",
                    "result": {"discarded_group": group,
                               "remaining_groups": len(queue),
                               "note": "本组候选已全部过期（发现后已获得），本组作废"}}
        choice = params.get("daowen_name", "")
        if choice not in valid:
            return {"success": False, "error": "只能选择本组发现列出的道纹",
                    "choices": valid}
        cap = player.daowen_capacity
        if len(player.dao_wen) >= cap:
            return {"success": False,
                    "error": (f"道纹种类超出承载：[血限]{player.blood_limit} 只允许{cap}种"
                              f"（30[血限]=1种），已持有{len(player.dao_wen)}种；"
                              f"本组发现保留，腾出额度后可重试")}
        player.dao_wen[choice] = DaoWenInstance(DaoWen(
            name=choice, formula="", cost_type="消耗",
            cost_formula="X", effect_formula=""))
        queue.pop(0)
        return {"success": True, "action": "领取典当发现",
                "result": {"granted": choice, "from_group": group,
                           "remaining_groups": len(queue),
                           "player_daowen": list(player.dao_wen)}}

    def _action_declare_evolution(self, params: dict) -> dict:
        """怪物进化：发动【原初X】借用原始怪物道纹（引擎直接结算，无需DM中断）"""
        monster_name = params.get("monster", "")
        # 同名重复抽 + 封印尸体仍留在 state.enemies：必须跳过已命零/已移出的，
        # 否则会命中第一具尸体并报「已命零」，活着的同名困境怪永远进化不了。
        monster = next((e for e in self.state.enemies
                        if e.name == monster_name and self.state.enemy_combat_active(e)), None)

        if not monster:
            return {"success": False, "error": f"找不到存活的怪物: {monster_name}"}

        daowen_name = params.get("daowen", "")
        try:
            x = int(params.get("x", 1))
        except (TypeError, ValueError):
            return {"success": False, "error": "X必须为整数"}

        return self.combat.execute_evolution(monster, daowen_name, x)

    def _settle_free_crystal_cost(self, target: Entity, cost_type: str, amount: int) -> dict:
        """自由结晶：强制目标付出指定代价共计 amount（2026-10-05 用户令）。

        流血/衰老/枯竭/萎缩/疲惫/异变走统一代价结算入口 _apply_numeric_cost_part
        （含致死判定、不朽之躯免疫、异变迷失等既有口径）；失忆与冷却没有
        现成结算体，在这里按 物品索引.md#自由结晶 的定义实现：失忆＝随机失去至多
        X 种自身道纹，冷却＝其持有的全部道纹进入冷却 X 场。
        """
        ctx = make_context(
            timing=self.state.combat_subphase or self.state.phase,
            source="自由结晶", source_type="consumable",
            actor=self.state.player, target=target, owner=self.state.player,
            mechanic="cost", subtype=self.combat._cost_context_subtype(cost_type),
            amount=amount, tags={"consumable", "free_crystal", "cost"})
        if cost_type == "失忆":
            lost: list[str] = []
            pool = list(target.dao_wen.keys())
            for slot in range(min(amount, len(pool))):
                roll = self.dice.auto_roll(
                    f"free_crystal_amnesia_{target.name}_{slot + 1}", pool,
                    context=f"自由结晶·失忆：{target.name}失去道纹{slot + 1}")
                name = roll["record"]["selected_value"]
                pool.remove(name)
                target.dao_wen.pop(name, None)
                lost.append(name)
            return {"payer": target.name, "cost_type": cost_type,
                    "paid": len(lost), "lost_daowen": lost}
        if cost_type == "冷却":
            cooled = []
            for name, inst in target.dao_wen.items():
                inst.cooldown_remaining = max(inst.cooldown_remaining, amount)
                cooled.append(name)
            return {"payer": target.name, "cost_type": cost_type,
                    "paid": amount, "cooled_daowen": cooled}
        return self.combat._apply_numeric_cost_part(target, cost_type, amount, ctx)

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

        if item.name == "万钧印":
            return {"success": False, "error": "万钧印在受到攻击时自动触发，不可主动使用"}

        # 扭曲工具库消耗品：引擎侧真实结算（F3）
        if item.name in TWISTED_TOOL_LIBRARY:
            return self._consume_twisted_tool(item, params)

        # 雕塑：消耗1耐久造成15伤害或获得20格挡
        if item.kind == "sculpture":
            mode = params.get("mode", "damage")  # damage / shield
            target = None
            if mode == "damage":
                target = self.combat._combat_entity_refs().get(params.get("target_ref", ""))
                if target not in self.state.get_all_enemy_side():
                    target = None
            result = self.combat.use_sculpture(item, target=target, mode=mode)
            return {
                "success": result.get("success", True),
                "action": f"使用雕塑【{item_name}】({mode})",
                "result": result,
                "state": self.combat._get_combat_state(),
            }

        # 自由结晶（2026-10-05 用户令重做，旧口径「对同一怪物喂满3次触发救赎」废止）：
        # 对[目标]使用→其随机付出一种代价，共计10；若目标是生命≤20%血限的怪物，
        # 代价结算后仍存活的，强制触发救赎。消耗品不占主动出手。
        if item.name == "自由结晶":
            target_ref = params.get("target_ref", "")
            target = self.combat._combat_entity_refs().get(target_ref)
            if target is None or not target.is_alive:
                return {"success": False, "error": "自由结晶只能对当前参战的存活目标使用"}
            redemption_eligible = (
                target.entity_type == "怪物"
                and target.blood_limit > 0
                and target.current_hp <= math.ceil(target.blood_limit * 0.2))
            remaining = item.use()
            roll = self.dice.auto_roll(
                f"free_crystal_cost_{target.name}_{remaining}",
                list(FREE_CRYSTAL_COST_POOL),
                context=f"自由结晶：{target.name}随机付出一种代价，共计{FREE_CRYSTAL_COST_AMOUNT}")
            cost_type = roll["record"]["selected_value"]
            cost_detail = self._settle_free_crystal_cost(target, cost_type, FREE_CRYSTAL_COST_AMOUNT)
            redemption = None
            if redemption_eligible and target.is_alive:
                redemption = self.combat.check_redemption(target, force=True)
            return {
                "success": True,
                "action": f"对【{target.name}】使用自由结晶",
                "result": {
                    "target": target.name,
                    "cost_type": cost_type,
                    "cost_amount": FREE_CRYSTAL_COST_AMOUNT,
                    "cost_detail": cost_detail,
                    "uses_remaining": remaining,
                    "redemption": redemption,
                },
                "state": self.combat._get_combat_state(),
            }

        # 【无限肉块】：恢复10生命（2026-10-05 用户令：5→10），但本次恢复不计入癌变累计治疗量。
        if item.kind == "infinite_meat":
            before_total = self.state.player.total_healed
            before_battle_total = self.state.player.healed_this_battle
            remaining = item.use()
            heal = self.state.apply_heal(self.state.player, 10, ctx={
                "timing": self.state.phase, "source": item.name, "source_type": "consumable",
                "actor": self.state.player, "target": self.state.player, "mechanic": "heal",
                "subtype": "infinite_meat", "amount": 10,
                "tags": {"consumable", "exclude_cancer_accumulation"},
            })
            self.state.player.total_healed = before_total
            self.state.player.healed_this_battle = before_battle_total
            return {
                "success": True,
                "action": f"使用消耗品【{item.name}】",
                "result": {"uses_remaining": remaining, "heal": heal,
                           "excluded_from_cancer_accumulation": True},
                "state": self.combat._get_combat_state(),
            }

        # 正文具名消耗品：全部在扣耐久前完成参数校验；未实现项不得“成功但只扣耐久”。
        # 2026-10-14 审计F4：镇魂铁钉处理器补齐（此前发而无用）；替死鬼/赶尸铃/黄符见下。
        if item.name in {"绝息淤泥", "活性土壤", "假钞贴", "穿甲弹", "洗劫面具", "赤泉囊", "龙血瓶",
                          "镇魂铁钉", "赶尸铃", "黄符"}:
            return self._consume_named_event_item(item, params)

        # 普通消耗品：扣减耐久；异变类效果走统一入口 add_mutation
        # （裁定⑧= A4全量：任何角色的任何异变来源同一入口，达50层即【迷失】命零；
        #  其余效果仅限已经有机械解析器的回复/异变文本；未知效果在扣耐久前拒绝。）
        effect = item.effect or ""
        heal_match = (re.search(r"恢复(\d+)生命", effect)
                      or re.search(r"\[回复\]\s*(\d+)", effect)
                      or re.search(r"回复(\d+)", effect))
        mut_match = re.search(r"获得异变(\d+)", effect)
        if heal_match is None and mut_match is None:
            return {"success": False, "error": f"消耗品【{item.name}】没有已注册的效果处理器"}
        remaining = item.use()
        mutation_info = None
        heal_info = None
        cancer_info = None
        if self.state.player:
            if heal_match:
                amount = int(heal_match.group(1))
                heal_info = self.state.apply_heal(self.state.player, amount, ctx={
                    "timing": self.state.phase, "source": item.name, "source_type": "consumable",
                    "actor": self.state.player, "target": self.state.player, "mechanic": "heal",
                    "subtype": "consumable", "amount": amount, "tags": {"consumable"},
                })
                cancer = self.combat.check_cancer(self.state.player)
                if cancer:
                    cancer_info = cancer
                    if not self.state.player.is_alive and not self.state.last_death_cause:
                        self.state.last_death_cause = "cancer"
        mut_match = re.search(r"获得异变(\d+)", effect)
        if mut_match and self.state.player:
            layers = int(mut_match.group(1))
            mut = self.state.player.add_mutation(layers)
            mutation_info = {
                "mutation_added": mut["mutation_added"],
                "mutation_total": mut["mutation_total"],
                "collapsed": mut["collapsed"],
                "lost": mut.get("lost", False),
            }
            if mut["collapsed"]:
                # 怪物（理论上玩家不会走到这里）：异变爆体命零
                self.combat._on_entity_death(
                    self.state.player,
                    ctx=self.combat._lost_context(self.state.player, {
                        "timing": self.state.combat_subphase or self.state.phase,
                        "source": "消耗品", "source_type": "consumable",
                        "actor": self.state.player, "target": self.state.player,
                        "mechanic": "cost", "subtype": "mutation", "amount": layers,
                        "tags": {"consumable", "active_payment"}}, subtype="collapse"))
                self.state.last_death_cause = "collapse"
                mutation_info["note"] = (
                    f"异变达{mut['mutation_total']}层触发【迷失】，"
                    f"{self.state.player.name}异变爆体直接命零")
            elif mut.get("lost"):
                # 非怪物：触发【迷失】——战斗中则变身/命零；局外则直接命零。
                if self.state.phase == "in_combat":
                    res = self.combat._resolve_mutation_lost(self.state.player, {
                        "timing": self.state.combat_subphase or self.state.phase,
                        "source": "消耗品", "source_type": "consumable",
                        "actor": self.state.player, "target": self.state.player,
                        "mechanic": "cost", "subtype": "mutation", "amount": layers,
                        "tags": {"consumable", "active_payment"}})
                    # 变身不产生"死亡"，不触发死之传承；命零才走 collapse 死因
                    if res["lost"] == "death":
                        self.state.last_death_cause = "collapse"
                    mutation_info["note"] = res["note"]
                    mutation_info["lost_transform"] = (res["lost"] == "transform")
                    mutation_info["collapsed"] = (res["lost"] == "death")
                else:
                    # 局外触发【迷失】无队友可战：直接命零。_on_entity_death 本身不置 hp/is_alive，
                    # 需调用方先归零（与 add_mutation 怪物路径的口径一致）。
                    self.state.player.current_hp = 0
                    self.state.player.is_alive = False
                    self.combat._on_entity_death(
                        self.state.player,
                        ctx=self.combat._lost_context(self.state.player, {
                            "timing": self.state.combat_subphase or self.state.phase,
                            "source": "消耗品", "source_type": "consumable",
                            "actor": self.state.player, "target": self.state.player,
                            "mechanic": "cost", "subtype": "mutation", "amount": layers,
                            "tags": {"consumable", "active_payment"}}, subtype="collapse"))
                    self.state.last_death_cause = "collapse"
                    mutation_info["collapsed"] = True
                    mutation_info["note"] = (
                        f"异变达{mut['mutation_total']}层触发【迷失】，"
                        f"身边无可战之敌，{self.state.player.name}直接命零")
        return {
            "success": True,
            "action": f"使用消耗品【{item_name}】",
            "result": {
                "effect": item.effect,
                "uses_remaining": remaining,
                "is_depleted": item.is_depleted,
                "heal": heal_info,
                "cancer": cancer_info,
                "mutation": mutation_info,
                "note": "消耗品效果按其描述结算，使用不消耗出手",
            },
            "state": self.combat._get_combat_state(),
        }

    def _consume_named_event_item(self, item: Consumable, params: dict) -> dict:
        refs = self.combat._combat_entity_refs()
        player = self.state.player
        if player is None:
            return {"success": False, "error": "没有玩家"}
        name = item.name
        result: dict[str, Any] = {"item": name}
        if name == "绝息淤泥":
            if self.state.phase != GamePhase.IN_COMBAT.value:
                return {"success": False, "error": "绝息淤泥只能在战斗中使用"}
            item.use()
            self.state.event_modifiers["escape_at_battle_end"] = True
            result["battle_end_escape"] = True
        elif name == "活性土壤":
            if (self.state.phase != GamePhase.IN_COMBAT.value
                    or self.state.combat_subphase != CombatSubphase.AWAIT_ROUND_START.value
                    or self.state.current_round != 0):
                return {"success": False, "error": "活性土壤只能在本场[战始]窗口使用"}
            x = params.get("x")
            panel = params.get("friend")
            if not params.get("dm_approved"):
                interrupt = Interrupt(
                    interrupt_type=InterruptType.UNSEEN_SCENE,
                    description="玩家设计朋友面板，等待DM确认",
                    context={"x": x, "friend": panel}, options=[],
                    state_snapshot=self.state.to_dict(),
                )
                self._pending_interrupts.append(interrupt)
                return {"success": True, "action": "活性土壤等待DM确认",
                        "interrupt": interrupt.to_dict(), "uses_remaining": item.current_uses}
            if (not isinstance(x, int) or isinstance(x, bool) or x < 1 or player.current_mana < x
                    or not isinstance(panel, dict)):
                return {"success": False, "error": "活性土壤需要合法x、足够法力和DM确认的friend面板"}
            ac, ap, hp = panel.get("attack_count"), panel.get("attack_power"), panel.get("blood_limit")
            budget = (ac * ac + ap + math.ceil(hp / 6)) if all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (ac, ap, hp)) else -1
            if budget > x or budget < 0 or not isinstance(panel.get("name"), str) or not panel["name"]:
                return {"success": False, "error": f"朋友面板属性点成本必须≤X={x}"}
            player.current_mana -= x
            friend = Entity(panel["name"], "朋友", blood_limit=hp, current_hp=hp,
                            attack_count=ac, attack_power=ap)
            self.state.friends.append(friend)
            item.use()
            result.update({"friend": friend.name, "budget": budget, "mana_paid": x})
        elif name == "假钞贴":
            item.use(); self.state.fake_shards += 20
            result.update({"fake_shards_gained": 20, "fake_shards": self.state.fake_shards})
        elif name == "穿甲弹":
            target = refs.get(params.get("target_ref", ""))
            if target is None or self.state.on_player_side(target) == self.state.on_player_side(player):
                return {"success": False, "error": "穿甲弹必须显式指定合法敌方target_ref"}
            item.use()
            detail = self.combat._apply_hostile_damage(target, 15, "无视格挡", player, ctx={
                "timing": self.state.combat_subphase or self.state.phase, "source": "穿甲弹", "source_type": "consumable",
                "actor": player, "target": target, "mechanic": "damage", "subtype": "consumable",
                "amount": 15, "tags": {"consumable", "ignore_shield"},
            })
            result.update({"target": target.name, "damage": detail})
        elif name == "洗劫面具":
            item.use(); self.combat.grant_bizhong(player, 2)
            result.update({"guaranteed_hits": 2, "remaining": self.combat.bizhong_remaining(player)})
        elif name == "赤泉囊":
            if self.state.phase != GamePhase.PRE_BATTLE.value:
                return {"success": False, "error": "赤泉囊只能在局外使用"}
            allocations = params.get("heal_allocations")
            outside_refs = {"player:0": player}
            outside_refs.update({f"friend:{i}": e for i, e in enumerate(self.state.friends) if e.is_alive})
            outside_refs.update({f"employee:{i}": e for i, e in enumerate(self.state.employees) if e.is_alive})
            if (not isinstance(allocations, list) or sum(entry.get("amount", -1) for entry in allocations if isinstance(entry, dict)) != 8
                    or any(not isinstance(entry, dict) or entry.get("target_ref") not in outside_refs
                           or not isinstance(entry.get("amount"), int) or isinstance(entry.get("amount"), bool)
                           or entry["amount"] < 0 for entry in allocations)):
                return {"success": False, "error": "赤泉囊必须用heal_allocations把8点恢复量完整分配给合法目标"}
            item.use()
            heals = []
            for entry in allocations:
                heal_target = outside_refs[entry["target_ref"]]
                heals.append({"target": heal_target.name,
                              **self.state.apply_heal(heal_target, entry["amount"], ctx={
                                  "timing": "pre_battle", "source": "赤泉囊", "source_type": "consumable",
                                  "actor": player, "target": heal_target, "mechanic": "heal",
                                  "subtype": "consumable", "amount": entry["amount"], "tags": {"consumable", "pre_battle"},
                              })})
            self.state.event_modifiers["red_spring_battle_losses"] = 2
            result.update({"heals": heals, "future_battle_start_losses": 2})
        elif name == "龙血瓶":
            amount = params.get("amount")
            target = refs.get(params.get("target_ref", ""))
            if (not isinstance(amount, int) or isinstance(amount, bool) or amount < 1
                    or amount > item.current_uses or target is None or not self.state.on_player_side(target)):
                return {"success": False, "error": "龙血瓶需要合法amount和玩家侧target_ref"}
            item.current_uses -= amount
            result.update({"target": target.name, "extracted": amount,
                           "heal": self.state.apply_heal(target, amount, ctx={
                               "timing": self.state.combat_subphase or self.state.phase,
                               "source": "龙血瓶", "source_type": "consumable",
                               "actor": self.state.player, "target": target, "owner": self.state.player,
                               "mechanic": "heal", "subtype": "dragon_blood_bottle_extract", "amount": amount,
                               "tags": {"consumable", "stored_heal"},
                           })})
        elif name == "镇魂铁钉":
            # 2026-10-14 审计F4：对[目标]施加【束缚2】；目标是轮回者时可额外耐久-1使其速度归零。
            target = refs.get(params.get("target_ref", ""))
            if target is None or not target.is_alive:
                return {"success": False, "error": "镇魂铁钉必须显式指定合法存活target_ref"}
            zero_speed = params.get("zero_speed", False)
            if zero_speed and target.entity_type != "轮回者":
                return {"success": False, "error": "镇魂铁钉的速度归零只对[轮回者]目标可用"}
            if zero_speed and item.current_uses < 2:
                return {"success": False, "error": "镇魂铁钉耐久不足，无法额外-1使速度归零"}
            item.use()
            target.add_status(StatusEffect("束缚", 2, 1, "镇魂铁钉"))
            result.update({"target": target.name, "shackled_rounds": 2})
            if zero_speed:
                item.use()
                lost = self.combat._lose_current_speed(target, target.current_speed, ctx={
                    "timing": self.state.combat_subphase or self.state.phase, "source": "镇魂铁钉",
                    "source_type": "consumable", "actor": player, "target": target,
                    "mechanic": "speed_change", "subtype": "current_speed",
                    "amount": target.current_speed, "tags": {"consumable"},
                })
                result.update({"zero_speed": True, "speed_lost": lost})
        elif name == "赶尸铃":
            # 2026-10-14 审计F4：召唤2具【行尸】（1攻×4次/36血）为临时朋友；
            # 战终自动解体（temp_friends 战终清理）。行尸不可被命令/自主出手，
            # 与道纹【招魂】的临时朋友一致（纯肉身，可被集火、可吃回始/回终结算）。
            if self.state.phase != GamePhase.IN_COMBAT.value:
                return {"success": False, "error": "赶尸铃只能在战斗中使用"}
            item.use()
            summoned = []
            base = len(self.state.temp_friends)
            for i in range(2):
                corpse = Entity(name=f"行尸·{base + i + 1}", entity_type="临时朋友",
                                blood_limit=36, current_hp=36,
                                attack_count=4, attack_power=1)
                self.state.temp_friends.append(corpse)
                summoned.append(corpse.name)
            result.update({"summoned": summoned})
        elif name == "黄符":
            # 2026-10-14 审计F4：两步——刻印（imprint_spell，耗法力=阶级，不耗耐久，
            # 局外/战中均可）→ 交付（target_ref 朋友/员工，战中，耗尽耐久）。
            # 只支持瞬发法术（反应型进不了朋友的反应链，刻印时明确拒绝）；
            # 朋友发动走语言命令「发动 <法术名>」，以法力为能源（每阶1点）。
            imprint = params.get("imprint_spell", "")
            marked = re.search(r"【刻印：(.+?)】", item.effect or "")
            if imprint:
                if marked:
                    return {"success": False, "error": "黄符已刻印，不可重复刻印"}
                spell = next((sp for sp in player.spells if sp.name == imprint), None)
                if spell is None:
                    return {"success": False, "error": f"你尚未学会法术「{imprint}」"}
                trig = spell.trigger
                if trig is None:
                    parsed = self.combat._parse_custom_spell(spell)
                    trig = parsed["trigger"] if parsed else None
                trig_value = trig.value if hasattr(trig, "value") else trig
                if trig_value != "瞬发":
                    return {"success": False, "error": f"黄符只能刻印瞬发法术，「{imprint}」是{trig_value}法术"}
                rank = max(1, spell.rank or 1)
                if player.current_mana < rank:
                    return {"success": False,
                            "error": f"刻印「{imprint}」需要{rank}点法力，当前{player.current_mana}"}
                player.current_mana -= rank
                item.effect = (item.effect or "") + f"【刻印：{spell.name}】"
                result.update({"imprinted": spell.name, "rank": rank, "mana_spent": rank})
                result.update({"uses_remaining": item.current_uses, "is_depleted": item.is_depleted})
                return {"success": True, "action": f"使用消耗品【{name}】", "result": result}
            if not marked:
                return {"success": False, "error": "黄符尚未刻印，先用imprint_spell刻印已学瞬发法术"}
            if self.state.phase != GamePhase.IN_COMBAT.value:
                return {"success": False, "error": "黄符交付只能在战斗中进行"}
            target = refs.get(params.get("target_ref", ""))
            if target is None or not target.is_alive or target.entity_type not in ("朋友", "员工"):
                return {"success": False, "error": "黄符必须交付给一名存活[朋友]/[员工]"}
            spell_name = marked.group(1)
            origin = next((sp for sp in player.spells if sp.name == spell_name), None)
            if origin is None:
                return {"success": False, "error": f"刻印的法术「{spell_name}」已不在你的法术列表"}
            item.use()
            granted = Spell(name=origin.name, required_daowen=list(origin.required_daowen),
                            trigger_condition=origin.trigger_condition, effect_flow=origin.effect_flow,
                            rank=origin.rank, custom_conditions=list(origin.custom_conditions),
                            automatic=False, lifecycle="battle", trigger=origin.trigger)
            target.spells = [sp for sp in target.spells if sp.name != granted.name]
            target.spells.append(granted)
            self.state.huangfu_grants().add((target.runtime_id, granted.name))
            result.update({"delivered_to": target.name, "spell": granted.name, "rank": granted.rank})
        result.update({"uses_remaining": item.current_uses, "is_depleted": item.is_depleted})
        return {"success": True, "action": f"使用消耗品【{name}】", "result": result}

    def _consume_twisted_tool(self, item: Consumable, params: dict) -> dict:
        """扭曲工具库 8 件的引擎侧真实结算（F3）"""
        name = item.name
        player = self.state.player
        if not player:
            return {"success": False, "error": "没有玩家"}
        def selected_enemy():
            target = self.combat._combat_entity_refs().get(params.get("target_ref", ""))
            if target in self.state.get_all_enemy_side():
                return target
            legacy = params.get("target", "")
            matches = [entity for entity in self.state.get_all_enemy_side() if entity.name == legacy]
            return matches[0] if len(matches) == 1 else None
        # 急救箱的“清除一种”必须由AI显式选择，先校验后扣耐久。
        aid_status = None
        if name == "急救箱":
            negative = [s for s in player.status_effects
                        if s.remaining_rounds > 0 or s.name in ("坏死", "退化", "伤痕", "畸变", "蒙蔽")]
            selected = params.get("remove_status")
            if negative:
                if not isinstance(selected, str) or selected not in [s.name for s in negative]:
                    return {"success": False,
                            "error": f"急救箱必须显式提交remove_status，可选{[s.name for s in negative]}"}
                aid_status = next(s for s in negative if s.name == selected)
            elif selected not in (None, ""):
                return {"success": False, "error": "当前没有可由急救箱清除的负面状态"}

        # 全部参数合法后统一扣耐久。
        remaining = item.use()
        result: dict[str, Any] = {"tool": name, "uses_remaining": remaining, "is_depleted": item.is_depleted}
        # 1. 反怪物电击枪：对目标 25 伤害，飞行目标额外 +15
        #
        if name == "反怪物电击枪":
            target = selected_enemy()
            if target is None:
                item.current_uses += 1
                return {"success": False, "error": "找不到敌方target_ref"}
            # 只有当前飞行状态算飞行；仅“持有”飞行道纹不算已经飞行。
            flying = target.is_flying or target.has_status("飞行")
            dmg = 25 + (15 if flying else 0)
            detail = self.combat._apply_hostile_damage(target, dmg, source=player, ctx={
                "timing": self.state.combat_subphase or self.state.phase, "source": "反怪物电击枪", "source_type": "consumable",
                "actor": player, "target": target, "mechanic": "damage", "subtype": "consumable",
                "amount": dmg, "tags": {"consumable"},
            })
            result.update({"target": target.name, "damage": dmg, "flying_bonus": 15 if flying else 0, "hp_after": target.current_hp, "detail": detail})
            # 2026-10-03：原“击落”（施【坠落1】）随道纹删除移除。
        # 2. 备用血泵：回复20（走 heal，计入癌变/战终回吐），≤30% 额外30格挡
        elif name == "备用血泵":
            heal_detail = self.state.apply_heal(player, 20, ctx={
                "timing": self.state.combat_subphase or self.state.phase, "source": "备用血泵", "source_type": "consumable",
                "actor": player, "target": player, "mechanic": "heal", "subtype": "consumable",
                "amount": 20, "tags": {"consumable"},
            })
            healed = heal_detail["actual_heal"]
            shield_gained = 0
            if player.current_hp <= player.blood_limit * 0.3:
                player.shield += 30
                shield_gained = 30
            cancer = self.combat.check_cancer(player)
            result.update({"healed": healed, "hp_after": player.current_hp,
                           "shield_gained": shield_gained, "heal": heal_detail,
                           "cancer": cancer})
        # 3. 强光探照灯：目标蒙蔽2
        elif name == "强光探照灯":
            target = selected_enemy()
            if target is None:
                item.current_uses += 1
                return {"success": False, "error": "找不到敌方target_ref"}
            target.add_status(StatusEffect(name="蒙蔽", value=2, remaining_rounds=-1, source="强光探照灯"))
            result.update({"target": target.name, "蒙蔽": 2})
        # 4. 高压水枪：清除全场敌方所有持续X效果
        elif name == "高压水枪":
            cleared = []
            for m in self.state.get_all_enemy_side():
                before = len(m.status_effects)
                m.status_effects = [s for s in m.status_effects if s.remaining_rounds <= 0]
                if len(m.status_effects) != before:
                    cleared.append(m.name)
            result.update({"cleared_enemies": cleared})
        # 5. 储能电池：使用后立即获得12法力
        elif name == "储能电池":
            # 2026-10-08：走统一入口，【勾魂】期间不生效
            gained = player.gain_mana(12)
            self.combat.clamp_immortal_body(player)
            result.update({"mana_gained": gained, "mana_after": player.current_mana})
        # 6. 急救箱：回复25（走 heal）并清一种负面持续
        elif name == "急救箱":
            heal_detail = self.state.apply_heal(player, 25, ctx={
                "timing": self.state.combat_subphase or self.state.phase, "source": "急救箱", "source_type": "consumable",
                "actor": player, "target": player, "mechanic": "heal", "subtype": "consumable",
                "amount": 25, "tags": {"consumable"},
            })
            healed = heal_detail["actual_heal"]
            removed = None
            if aid_status is not None:
                player.status_effects.remove(aid_status)
                removed = aid_status.name
            cancer = self.combat.check_cancer(player)
            result.update({"healed": healed, "removed_status": removed,
                           "heal": heal_detail, "cancer": cancer})
        # 7. 干扰仪：全场敌方本回合无法发动道纹（加干扰1状态）
        elif name == "干扰仪":
            jammed = []
            for m in self.state.get_all_enemy_side():
                m.add_status(StatusEffect(name="干扰", value=1, remaining_rounds=1, source="干扰仪"))
                jammed.append(m.name)
            result.update({"jammed": jammed})
        # 8. 高爆手雷：目标15伤害 + 本回合攻击次数-1
        elif name == "高爆手雷":
            target = selected_enemy()
            if target is None:
                item.current_uses += 1
                return {"success": False, "error": "找不到敌方target_ref"}
            detail = self.combat._apply_hostile_damage(target, 15, source=player, ctx={
                "timing": self.state.combat_subphase or self.state.phase, "source": "高爆手雷", "source_type": "consumable",
                "actor": player, "target": target, "mechanic": "damage", "subtype": "consumable",
                "amount": 15, "tags": {"consumable"},
            })
            # 攻击次数-1：用状态标记，本回合内 _monster_attack_actions 会读取
            target.add_status(StatusEffect(name="手雷减攻", value=1, remaining_rounds=1, source="高爆手雷"))
            result.update({"target": target.name, "damage": 15, "detail": detail, "nade_minus": 1})
        else:
            result.update({"note": "未知工具"})
        return {"success": True, "action": f"使用工具【{name}】", "result": result, "state": self.combat._get_combat_state()}

    # ==================== 事件结算 ====================

    REJECT_OPTION_KEYWORDS = ("无事发生", "观棋", "无视", "离开", "目送", "绕桥", "让炉", "避开", "捂住", "转身")

    # 带实际代价或收益的「拒绝改造」不算真拒绝——出现这些字样即判定为
    # 「有实质的拒绝类选项」，不发放拒绝奖励（正文·事件拒绝奖励）。
    # 必须是"付出/得到"的实质词，不能只写一个「拒绝」：拒绝本身就是这类选项的字面。
    REJECT_BUT_PAID_KEYWORDS = (
        "失去", "流血", "衰老", "枯竭", "萎缩", "疲惫", "异变", "失忆", "代价",
        "支付", "消耗", "销毁", "献祭", "扣除", "获得", "得到", "回复", "碎片",
        "属性点", "遗物", "法术", "道纹", "残韵", "生命", "血限", "法力", "速限",
    )

    @staticmethod
    def _is_reject_option_text(text: str) -> bool:
        """真拒绝判定。

        真拒绝 ＝ 选项正文写「无事发生」，或以「拒绝：/拒绝:」起头，
        或以【无视】【离开】【目送】【绕桥】【避开】【捂住】【转身】等
        明确离开/拒绝语义**开头**。

        两条收紧：
        1. 关键词必须出现在**开头**（或紧跟在「N.」序号后），不再做全文子串匹配——
           旧实现只要句中含有「避开」等词就判真拒绝，误判风险高。
        2. 带实际代价或收益的「拒绝改造」显式排除：正文中出现
           「失去/获得/流血/属性点…」等实质得失字样即不算真拒绝。
        """
        body = text.strip()
        # 去掉「1.」「2.」这类选项序号前缀
        stripped = re.sub(r"^\d+\s*[.、)）]\s*", "", body)
        if "无事发生" in stripped:
            # 「无事发生」是完整语义，允许出现在句中（如「拒绝：无事发生」）
            pass
        elif not (stripped.startswith("拒绝")   # 拒绝：/拒绝:/拒绝下注 皆计
                  or any(stripped.startswith(k) for k in GameEngine.REJECT_OPTION_KEYWORDS)):
            return False
        # 显式排除带代价/收益的「拒绝改造」
        if any(k in stripped for k in GameEngine.REJECT_BUT_PAID_KEYWORDS):
            return False
        return True

    def _action_resolve_event(self, params: dict) -> dict:
        """结算事件选项：自动应用常见代价/收益，特殊效果交DM"""
        from .events import resolve_option_effect
        name = params.get("event", "")
        option_id = params.get("option_id")
        if self.event_pool.current is None:
            return {"success": False, "error": "当前没有待结算事件；请先通过探索触发事件"}
        if name != self.event_pool.current:
            return {"success": False,
                    "error": f"只能结算当前事件【{self.event_pool.current}】，不能结算【{name}】"}
        ev = self.event_pool.events.get(name)
        if not ev:
            return {"success": False, "error": f"未知事件: {name}"}
        opt = next((o for o in ev["options"] if o["id"] == option_id), None)
        if opt is None:
            return {"success": False, "error": f"事件{name}无选项{option_id}"}
        # 「拒绝改造」等带代价的选项也含「拒绝」，不能当拒绝类。
        # 真拒绝：正文写「无事发生」，或选项以「拒绝：/拒绝:」起头。
        # 真拒绝必须在结算选项效果之前判定（旧版曾据此索取分配去向）。
        text = opt["text"]
        is_reject = self._is_reject_option_text(text)
        has_wusuoqiu = any(r.name == "无所求" for r in self.state.relics)
        res = resolve_option_effect(opt["text"], self, event_name=name, params=params)
        if res.get("interrupt_required"):
            interrupt = Interrupt(
                interrupt_type=InterruptType.UNSEEN_SCENE,
                context=res["interrupt_required"],
                description=f"事件【{name}】包含创造性效果，等待DM裁定",
                options=[], state_snapshot=self.state.to_dict(),
            )
            self._pending_interrupts.append(interrupt)
            return {"success": True, "action": f"事件【{name}】等待裁定",
                    "completed": False, "interrupt": interrupt.to_dict()}
        if res.get("error"):
            return {"success": False, "error": res["error"],
                    "pages": res.get("pages"), "instruction": res.get("instruction", "")}
        # 任意事件造成的真实碎片余额变化都立即检查动态还债线；历史峰值只在余额刷新纪录时上升。
        player = self.state.player
        peak = max(0, int(getattr(self.state, "shards_peak", 0)))
        if player and player.is_alive and self.state.shards <= -(10 + peak):
            player.current_hp = 0
            player.depart_battle("还债")
            res.setdefault("applied", []).append(
                f"{player.name}被祭坛嚼碎：碎片余额{self.state.shards}触及动态还债线-{10 + peak}")
            res["debt_result"] = {"entity": player.name, "balance": self.state.shards,
                                  "threshold": 10 + peak, "triggered": True}
        if is_reject:
            # DM 裁定 2026-09-10（用户）：事件选拒绝选项后随机获得一种残韵——
            # 「用其他道纹解决」怪物墙：放弃事件收益换构筑深度，而非数值对撞。
            # 口径复用 _is_reject_option_text（带代价的「拒绝改造」类不算）；
            # 随机走引擎 dice（seed 可复现），入 State.resonance（局中插队可耗）。
            _rtype = ("转换", "反转", "曲解")[self.dice.randrange(3)]
            self.state.resonance[_rtype] = self.state.resonance.get(_rtype, 0) + 1
            res["applied"].append(f"拒绝奖励：随机获得{_rtype}残韵")
        if is_reject and has_wusuoqiu:
            # 2026-09-16 裁定（清单 A1）：【无所求】按《物品索引》原文"永久获得
            # 1点属性点"入属性点池，不再由引擎折算成面板——旧实现按
            # "1属性点=1速限=2法限"直接加速限/法限，与正文
            # 「2属性点=1[速限]=1[法限]=12[血限]」相悖，且把速限与法限做成
            # 不等价。入池后由玩家按正文自行兑换（属性点可存储、随时兑换）。
            self.state.attribute_points += 1
            res["applied"].append(
                f"无所求：+1属性点（属性点池 {self.state.attribute_points}；"
                "按正文 2点=1速限=1法限、1点=6血限，兑换仅限战前）")
        # 过路商人交易是一个持续会话：同次遭遇可连续买卖多件遗物，直到选择其他选项离开。
        keep_merchant_open = (name == "过路商人" and text.startswith("遗物交易")
                              and bool(res.get("trade")))
        if not keep_merchant_open:
            self.event_pool.resolve(name)
            # 一次性地点事件结算后关闭地点；祭坛与商人按既有规则可重复遭遇。
            if name not in {"祭坛", "过路商人"} and getattr(self.state, "world_state", {}):
                try:
                    world = self._world_from_state()
                    for location in world.map.locations.values():
                        if location.get("event_name") == name:
                            location["active"] = False
                    self.state.world_state = world.to_dict()
                except (ValueError, KeyError, TypeError):
                    pass
        # 扭曲都市完成事件后附赠【发现】：正式随机只走DiceEngine，并等待显式选1。
        # 连续交易期间尚未离开商人，不重复发放事件完成奖励。
        bonus = None
        if (not keep_merchant_open and self.state.current_region == "扭曲都市"
                and self.state.player and self.state.player.is_alive):
            owned = {c.name for c in self.state.consumables}
            pool = [n for n in TWISTED_TOOL_LIBRARY if n not in owned]
            if pool:
                discovery = self._offer_item_discovery(pool, f"事件【{name}】完成后附赠发现")
                if not discovery.get("success"):
                    return discovery
                bonus = {"候选": discovery["choices"], "等待选择": True,
                         "来源": discovery["source"]}
        result_payload = {"option": opt["text"], "applied": res["applied"], "instructions": res["instructions"],
                          "shards": self.state.shards,
                          "player_hp": self.state.player.current_hp if self.state.player else None}
        for key in ("random", "outcome", "net_delta", "debt_result", "trade"):
            if key in res:
                result_payload[key] = res[key]
        if name == "过路商人":
            result_payload["merchant_session_open"] = keep_merchant_open
        if bonus:
            result_payload["附赠发现"] = bonus
        next_event = None
        if self.state.pending_event_queue and not keep_merchant_open:
            next_event = self.state.pending_event_queue.pop(0)
            self.event_pool.current = next_event
            queued = self.event_pool.events[next_event]
            result_payload["next_event"] = {
                "event": next_event,
                "region": queued["region"],
                "desc": queued["desc"],
                "options": [{"id": option["id"], "text": option["text"]}
                            for option in queued["options"]],
                "queued_events_remaining": len(self.state.pending_event_queue),
            }
        return {
            "success": True, "action": f"事件【{name}】选项{option_id}",
            "result": result_payload,
            "completed_exploration": next_event is None,
            "instruction": (f"请继续结算探索队列中的事件【{next_event}】"
                            if next_event else "本次探索事件已全部结算"),
        }

    # ==================== 回合管理 ====================

    # 语言命令白名单：指令动作与目标/道纹提取。解析严格，解析不出即拒绝并给合法示例。
    _ALLY_ATTACK_VERBS = ("攻击", "打")
    _ALLY_DAOWEN_VERBS = ("发动", "用", "使用")
    _ALLY_GUARD_VERBS = ("护卫", "保护我", "挡伤", "替我挡")

    def _ally_daowen_x(self, ally, name: str, target) -> int:
        """微光者发动道纹时自选 X。

        面板仍写死 X 时（x_value>0）沿用固定值，保持迁移期双向兼容；
        x_free 时交给 engine/ai_rules.py 的**整场预算分配**决策器——
        微光者按 AI_EXPERIENCE.md:1254 是一池制、[回始]不回填，与怪物
        （遗物【某人的偏爱】每[回始]回满）不是同一个问题，不能套用怪物侧
        的每回合预演评分。返回 0 表示本回合不该发动，调用方应跳过而非报错。
        """
        inst = ally.dao_wen.get(name)
        if inst is not None and getattr(inst, "x_value", 0) > 0:
            return int(inst.x_value)
        from .ai_rules import pick_ally_daowen_x
        return pick_ally_daowen_x(ally, name, target)

    def _action_command_ally(self, params: dict) -> dict:
        """轮回者用语言命令[朋友]/[员工]行动（攻击/发动道纹/护卫/黄符施法）。

        指令格式（白名单，非命中即拒绝，不猜测）：
          「攻击 <目标名>」「打 <目标名>」 → 一轮攻击
          「发动 <道纹名>」「用 <道纹名> [打/对 <目标名>]」 → 发动道纹
          「发动 <法术名> [打 <目标名>]」+ spell_steps → 施放【黄符】授予的法术
            （2026-10-14 审计F4；法术须经黄符获授且在本场有效）
          「护卫 [X]」「保护我 [X]」「挡伤 [X]」「替我挡 [X]」 → 无消耗强制护卫：
            该[朋友]/[员工]替轮回者承担下X次受到的伤害（X默认1，1~9）。
            机制=对盟友强制施加背负标记（复用龙心谷背负的伤害重定向），
            不消耗盟友出手、不消耗法力，视为轮回者的指挥能力。
        目标名缺省时取当前存活敌人中当前生命最少者。
        """
        ally_ref = params.get("ally_ref", "")
        instruction = params.get("instruction", "")
        if not isinstance(instruction, str) or not instruction.strip():
            return {"success": False,
                    "error": "指令为空；合法格式：攻击 <目标名> / 发动 <道纹名> / 护卫 [X]"}
        instruction = instruction.strip()
        refs = self.combat._combat_entity_refs()
        ally = refs.get(ally_ref)
        if ally is None or not ally.is_alive:
            return {"success": False, "error": "ally_ref必须指向一名存活[朋友]/[员工]"}
        if ally.entity_type not in ("朋友", "员工"):
            return {"success": False, "error": "只有[朋友]/[员工]可被语言命令"}
        if ally.has_retreated:
            return {"success": False, "error": f"{ally.name}已撤退"}

        # ---- 护卫指令：无消耗强制挡伤（对盟友施加背负标记，复用重定向逻辑） ----
        for verb in self._ALLY_GUARD_VERBS:
            if instruction.startswith(verb):
                rest = instruction[len(verb):].strip()
                x = 1
                if rest:
                    try:
                        x = int(rest)
                    except (TypeError, ValueError):
                        return {"success": False,
                                "error": f"护卫次数必须是1~9的整数：{rest!r}"}
                if not (1 <= x <= 9):
                    return {"success": False, "error": "护卫次数必须是1~9的整数"}
                player = self.state.player
                if player is None or not player.is_alive:
                    return {"success": False, "error": "轮回者不在场，无法护卫"}
                # 强制施加背负：盟友下X次替轮回者承担伤害（无消耗，不占盟友出手）
                # 存 runtime_id 而非实体引用
                ally._beifu_left = max(getattr(ally, "_beifu_left", 0) or 0, x)
                ally._beifu_target = player.runtime_id
                ally.add_status(StatusEffect(name="背负", value=x, remaining_rounds=-1,
                                            source=player.name))
                player.add_status(StatusEffect(name="被背负", value=x, remaining_rounds=-1,
                                               source=ally.name))
                return {"success": True, "action": f"命令{ally.name}护卫轮回者",
                        "result": {"ally": ally.name, "guard_times": x,
                                   "note": f"{ally.name}将替轮回者承担下{x}次受到的伤害"
                                           f"（无消耗强制施加，剩余{ally._beifu_left}次）"}}

        enemies = [e for e in self.state.enemies if e.is_alive]
        if not enemies:
            return {"success": False, "error": "场上没有存活敌人"}

        def _resolve_target(name_hint: str = "", allow_allies: bool = False):
            """解析命令目标。allow_allies=False（攻击指令）只允许敌方；
            allow_allies=True（道纹指令）允许全场存活单位（轮回者/朋友/员工/怪物），
            使「发动背负 打 轮回者」这类保护/治疗命令可行。
            「轮回者/我/我自己」是玩家别名（道纹指令保护队友时用）。"""
            if allow_allies and name_hint in ("轮回者", "我", "我自己"):
                pl = self.state.player
                return pl if (pl and pl.is_alive) else None
            if name_hint:
                pool = list(refs.values()) if allow_allies else list(enemies)
                matches = [e for e in pool if e.name == name_hint and e.is_alive]
                return matches[0] if len(matches) == 1 else None
            return min(enemies, key=lambda e: e.current_hp)

        # ---- 攻击指令：必须指令开头就是攻击动词（"攻击 X"或"打 X"） ----
        for verb in self._ALLY_ATTACK_VERBS:
            if instruction.startswith(verb):
                rest = instruction.split(verb, 1)[1].strip()
                target = _resolve_target(rest)
                if target is None:
                    return {"success": False,
                            "error": f"找不到目标「{rest}」；合法格式：攻击 <目标名>"}
                prepared = self.execute_action("prepare_attack", {"actor_ref": ally_ref})
                if not prepared.get("success"):
                    return prepared
                option = next((o for o in prepared["result"]["target_options"]
                               if o["ref"] == f"enemy:{self.state.enemies.index(target)}"), None)
                if option is None:
                    option = prepared["result"]["target_options"][0]
                hits = [{"target_ref": option["ref"], "dodge": False, "blood_shadow": False,
                         "spell_choices": {timing: {sp["spell_name"]: {"use": False}
                                                    for sp in option.get("spell_options", {}).get(timing, [])}
                                           for timing in ("before", "after", "damage_after", "life_before")}}
                        for _ in range(prepared["result"]["hit_count"])]
                resolved = self.execute_action("resolve_attack", {
                    "token": prepared["result"]["token"], "hits": hits})
                return {"success": True, "action": f"命令{ally.name}攻击{target.name}",
                        "result": {"ally": ally.name, "instruction": instruction,
                                   "attacked": target.name, "detail": resolved.get("result")}}
        # ---- 道纹指令：必须指令开头就是道纹动词；中间的"打/对"是目标分隔符 ----
        for verb in self._ALLY_DAOWEN_VERBS:
            if instruction.startswith(verb):
                rest = instruction.split(verb, 1)[1].strip()
                daowen = rest.split()[0] if rest else ""
                target_name = ""
                for sep in ("打", "对"):
                    if sep in rest:
                        target_name = rest.split(sep, 1)[1].strip()
                        break
                if daowen not in ally.dao_wen:
                    # 2026-10-14 审计F4：【黄符】施法——「发动 <法术名>」。
                    # 须经黄符获授（授权在本场有效）；steps 由 spell_steps 结构化提交。
                    granted = next((sp for sp in ally.spells if sp.name == daowen), None)
                    if granted is not None and (ally.runtime_id, daowen) in self.state.huangfu_grants():
                        steps = params.get("spell_steps")
                        if not isinstance(steps, list) or not steps:
                            return {"success": False,
                                    "error": f"命令{ally.name}施放「{daowen}」须用spell_steps提交每步决策"}
                        target = _resolve_target(target_name, allow_allies=True)
                        if target is None:
                            return {"success": False,
                                    "error": f"找不到目标「{target_name}」；合法格式：发动 <法术名> 打 <目标名>"}
                        spell_target_ref = next((ref for ref, ent in refs.items() if ent is target), "")
                        cast = self.execute_action("cast", {
                            "actor_ref": ally_ref, "huangfu_spell": daowen,
                            "target_ref": spell_target_ref, "steps": steps,
                            "max_iterations": params.get("spell_max_iterations")})
                        if not cast.get("success"):
                            return cast
                        return {"success": True, "action": f"命令{ally.name}施放{daowen}（黄符）",
                                "result": {"ally": ally.name, "instruction": instruction,
                                           "spell": daowen, "target": target.name,
                                           "detail": cast.get("result")}}
                    return {"success": False,
                            "error": f"{ally.name}未持有道纹「{daowen}」；持有：{list(ally.dao_wen)}"}
                # 道纹指令允许指定我方目标（背负/再生/庇护保护队友），按全场解析
                target = _resolve_target(target_name, allow_allies=True)
                if target is None:
                    return {"success": False,
                            "error": f"找不到目标「{target_name}」；合法格式：发动 <道纹名> 打 <目标名>"
                                     "（保护类道纹可指向轮回者/队友）"}
                target_ref = next((ref for ref, ent in refs.items() if ent is target), "")
                daowen_x = self._ally_daowen_x(ally, daowen, target)
                if daowen_x < 1:
                    return {"success": False,
                            "error": f"{ally.name}此刻不宜发动「{daowen}」"
                                     f"（一池制余量不足或代价付不起）"}
                use = self.execute_action("use_daowen", {
                    "actor_ref": ally_ref, "daowen_name": daowen, "x": daowen_x,
                    "target_ref": target_ref,
                    "dodge": False, "blood_shadow": False, "trigger_spell_choices": {}})
                if not use.get("success"):
                    return use
                return {"success": True, "action": f"命令{ally.name}发动{daowen}",
                        "result": {"ally": ally.name, "instruction": instruction,
                                   "daowen": daowen, "target": target.name,
                                   "detail": use.get("result")}}
        return {"success": False,
                "error": "无法解析指令；合法格式：攻击 <目标名> 或 发动 <道纹名> [打 <目标名>]"}

    def _action_resolve_ally_phases(self, params: dict) -> dict:
        """无命令时[朋友]/[员工]自主出手一次（规则正文：微光者会根据情况对敌方出手）。

        每个未出手完毕的存活朋友/员工：优先发动自身道纹（若有可用），否则一轮攻击；
        只对存活敌人行动；用完各自 action_count 或行动一次即停。
        """
        refs = self.combat._combat_entity_refs()
        results = []
        acted = 0
        # 死斗交替（对称）：挑战者盟友也须在 player_side 回合行动，
        # 避免盟友绕过 duel_turn 在守擂回合出手。
        if self.state.in_final_duel and self.state.duel_turn != "player_side":
            return {"success": True, "action": "朋友/员工自主行动",
                    "result": {"acted_count": 0, "allies": [],
                               "note": f"死斗交替：当前轮到{self.state.duel_turn}，"
                                       f"挑战者盟友(player_side)本阶段不行动"}}
        for prefix, entities in (("friend", self.state.friends), ("employee", self.state.employees)):
            for index, ally in enumerate(entities):
                if not ally.is_alive or ally.has_retreated:
                    continue
                if prefix == "employee" and not ally.is_deployed:
                    continue
                if ally.actions_used_this_round >= ally.action_count:
                    continue
                enemies = [e for e in self.state.enemies if e.is_alive]
                if not enemies:
                    break
                ally_ref = f"{prefix}:{index}"
                entry = {"ally": ally.name, "actions": []}
                # 自主出手次数：用完 action_count（至少1次）；每次攻击或道纹
                for _ in range(max(1, ally.action_count)):
                    if not ally.is_alive or not enemies:
                        break
                    if ally.actions_used_this_round >= ally.action_count:
                        break
                    target = min(enemies, key=lambda e: e.current_hp)
                    # 优先道纹
                    cast = None
                    for name, inst in ally.dao_wen.items():
                        if not inst.can_use():
                            continue
                        # 背负类道纹对敌使用=让施法者替敌方承担伤害（帮敌人挡刀），自主不出
                        if name == "背负":
                            continue
                        daowen_x = self._ally_daowen_x(ally, name, target)
                        if daowen_x < 1:
                            continue      # 一池制余量不足/付不起 → 不发动，改用普攻
                        use = self.execute_action("use_daowen", {
                            "actor_ref": ally_ref, "daowen_name": name, "x": daowen_x,
                            "target_ref": f"enemy:{self.state.enemies.index(target)}",
                            "dodge": False, "blood_shadow": False, "trigger_spell_choices": {}})
                        if use.get("success"):
                            cast = {"kind": "daowen", "name": name, "target": target.name,
                                    "x": daowen_x, "detail": use.get("result")}
                            break
                    if cast is None:
                        prepared = self.execute_action("prepare_attack", {"actor_ref": ally_ref})
                        if not prepared.get("success"):
                            break
                        option = next((o for o in prepared["result"]["target_options"]
                                       if o["ref"] == f"enemy:{self.state.enemies.index(target)}"), None) \
                                 or prepared["result"]["target_options"][0]
                        hits = [{"target_ref": option["ref"], "dodge": False, "blood_shadow": False,
                                 "spell_choices": {timing: {sp["spell_name"]: {"use": False}
                                                            for sp in option.get("spell_options", {}).get(timing, [])}
                                                   for timing in ("before", "after", "damage_after", "life_before")}}
                                for _ in range(prepared["result"]["hit_count"])]
                        resolved = self.execute_action("resolve_attack", {
                            "token": prepared["result"]["token"], "hits": hits})
                        if not resolved.get("success"):
                            break
                        cast = {"kind": "attack", "target": target.name,
                                "detail": resolved.get("result")}
                    entry["actions"].append(cast)
                    acted += 1
                    enemies = [e for e in self.state.enemies if e.is_alive]
                results.append(entry)
        return {"success": True, "action": "朋友/员工自主行动",
                "result": {"acted_count": acted, "allies": results}}

    def _action_prepare_monster_phase(self, params: dict) -> dict:
        """第一阶段：只返回合法选项，绝不替AI选择道纹、目标或闪避。

        全局法术【自身回合结束】：普通战斗里轮回者的自身行动阶段
        结束、怪物阶段开始前，是自动封印类法术的真实触发点。它不能
        误挂在完整回合的 round_end（那已经晚于怪物行动）。死斗双方
        都有独立的玩家侧回合，暂不从这条普通怪物阶段路径触发。
        """
        if self.state.pending_monster_phase:
            return {"success": False, "error": "已有待提交的怪物阶段决策"}
        # 死斗交替（对称）：守擂方（opponent_side）仅在轮到己方时行动。
        # 此前守擂侧走怪物阶段无任何 duel_turn 校验，每回合全量输出，
        # 挑战者却被严格交替限制——守擂方机制性必胜（镜像12/12全胜暴露）。
        if self.state.in_final_duel and self.state.duel_turn != "opponent_side":
            return {"success": True, "action": "准备怪物阶段",
                    "result": {"round": self.state.current_round, "actors": [], "skipped": [],
                               "note": f"死斗交替：当前轮到{self.state.duel_turn}，"
                                       f"守擂方(opponent_side)本阶段不行动"},
                    "instruction": "请调用resolve_monster_phase(choices=[])结束本阶段"}
        if not self.state.in_final_duel:
            spell_logs = self._resolve_global_trigger_spells_for_action(
                TriggerTiming.SELF_TURN_END.value, params)
        else:
            spell_logs = []
        options = self.combat.prepare_monster_phase()
        token = uuid.uuid4().hex
        self.state.pending_monster_phase = {"token": token, "round": self.state.current_round,
                                            "options": options}
        self.state.combat_subphase = CombatSubphase.MONSTER_ACTIONS.value
        return {"success": True, "action": "准备怪物阶段",
                "result": {"token": token, "spell_logs": spell_logs, **options},
                "instruction": "请为每个actors条目提交完整选择后调用resolve_monster_phase"}

    def _action_resolve_monster_phase(self, params: dict) -> dict:
        """第二阶段：验证完整选择与一次性token，通过后统一结算。

        提交失败时战斗状态由事务整体回滚（零副作用），pending保持有效：
        AI用同一token修正后重交即可，失败提交不得把战斗锁死在怪物阶段
        。仅当prepare快照本身失效（回合变化）时
        回滚pending并还原子阶段，让"重新prepare"成为可执行的恢复路径。
        """
        pending = self.state.pending_monster_phase
        if not pending:
            return {"success": False, "error": "请先调用prepare_monster_phase"}
        if params.get("token") != pending.get("token"):
            return {"success": False, "error": "怪物阶段token无效或已过期",
                    "recoverable": True, "token": pending.get("token"),
                    "instruction": "请使用上方当前有效token重新调用resolve_monster_phase"}
        if pending.get("round") != self.state.current_round:
            # 快照已跨回合失效：普通重提无意义，回滚pending让重新prepare可执行
            self._roll_back_monster_phase_pending()
            return {"success": False, "error": "回合已变化，请重新prepare_monster_phase",
                    "recoverable": True,
                    "instruction": "回合已变化，怪物阶段pending已回滚；"
                                   "请重新调用prepare_monster_phase获取最新token与合法选项"}
        try:
            results = self.combat.resolve_monster_phase(
                params.get("choices"), prepared=pending["options"],
            )
        except Exception as exc:
            # 状态已回滚、pending保持有效：同token修正后重交即可（不要重新prepare）
            return {"success": False, "error": str(exc), "recoverable": True,
                    "token": pending.get("token"),
                    "instruction": "提交被拒绝且已整体回滚（零副作用），token仍有效；"
                                   "请修正选择后用同一token重新调用resolve_monster_phase"}
        self.state.pending_monster_phase = {}
        # 全局法术【敌回终】：怪物阶段真正结算完毕的这一刻，是普通战斗里
        # "敌方回合结束"最接近的真实执行点。必须在怪物阶段结果已经落地
        # （道纹/攻击/死亡判定均已完成）之后才检查，不能提前，否则法术
        # 依赖的"怪物是否存活/已造成多少伤害"等条件会读到旧状态。
        if not self.state.in_final_duel:
            enemy_round_end_spells = self._resolve_global_trigger_spells_for_action(
                TriggerTiming.ENEMY_ROUND_END.value, params)
        else:
            enemy_round_end_spells = []
        # 死斗交替（对称）：守擂方结算完后换回挑战者侧（若还有余手），
        # 与挑战者每次行动后 _advance_duel_turn 的行为一致。
        # 死斗中若任一侧仍有余手，子阶段回到 player_actions 允许继续交替；
        # 仅双方都无余手才进入 await_round_end。
        if self.state.in_final_duel:
            if self._duel_side_can_act("player_side") or self._duel_side_can_act("opponent_side"):
                self.state.combat_subphase = CombatSubphase.PLAYER_ACTIONS.value
            else:
                self.state.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value
            self._advance_duel_turn()
        elif getattr(self.state, "arena_ffa", False):
            # 养蛊场：允许逐 actor 提交，每步只结算 1 只怪物（严格交替）。
            # 只要还有「本回合尚未出手」的存活怪物，就把子阶段退回玩家行动阶段，
            # 让下一次 prepare_monster_phase 能列出剩余怪物的合法选项；全部出手完
            # 才进入 await_round_end。默认关闭时走下面的原分支，行为不变。
            if any(m.is_alive and m.actions_used_this_round < 1 for m in self.state.enemies):
                self.state.combat_subphase = CombatSubphase.PLAYER_ACTIONS.value
            else:
                self.state.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value
        else:
            self.state.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value
        player_dead = (self.state.player is None) or (not self.state.player.is_alive)
        return {
            "success": True,
            "action": "结算怪物阶段",
            "result": {"entries": len(results), "player_dead": player_dead,
                       "player_hp": self.state.player.current_hp if self.state.player else 0,
                       "details": results, "spell_logs": enemy_round_end_spells},
        }

    def _action_monster_phase(self, params: dict) -> dict:
        """拒绝旧版固定策略入口，防止绕过两阶段AI显式决策。"""
        return {"success": False,
                "error": "monster_phase已停用；请依次调用prepare_monster_phase与resolve_monster_phase"}

    def _resolve_global_trigger_spells_for_action(self, trigger: str, params: dict) -> list[dict]:
        """全局时点法术统一入口。

        普通全局法术仍要求调用方在 params.spell_choices 中完整提交；
        ``automatic=True`` 的法术由引擎在真实触发点自动生成同一份结构化
        提交，既保留 Spell/DSL/道纹结算链，也不会把自动法术算作主动出手。
        """
        refs = self.combat._combat_entity_refs()
        expected = self.combat.prepare_global_trigger_spells(trigger)
        if not expected:
            return []

        automatic = self.combat.automatic_global_trigger_choices(trigger)
        has_manual = self.combat.has_manual_global_trigger_spells(trigger)
        if has_manual:
            # 自动候选不可由外部 use=false 覆盖；普通法术仍必须由调用方
            # 显式补齐，不能因为自动构造器预填了use=false就绕过原有契约。
            submitted = automatic
            for holder_ref, choices in submitted.items():
                for spell_name in list(choices):
                    if not self.combat.is_automatic_spell(holder_ref, spell_name):
                        del choices[spell_name]
            supplied = params.get("spell_choices", {})
            if isinstance(supplied, dict):
                for holder_ref, choices in supplied.items():
                    if holder_ref not in submitted or not isinstance(choices, dict):
                        continue
                    for spell_name, decision in choices.items():
                        if not self.combat.is_automatic_spell(holder_ref, spell_name):
                            submitted[holder_ref][spell_name] = decision
        else:
            # 全部是自动法术时，回合切换不需要额外参数。
            submitted = automatic
        self.combat.validate_global_trigger_spells(trigger, submitted, refs)
        previous_timing = getattr(self.combat, "_forced_context_timing", "")
        self.combat._forced_context_timing = trigger
        try:
            return self.combat.resolve_global_trigger_spells(trigger, submitted, refs)
        finally:
            self.combat._forced_context_timing = previous_timing

    def _action_round_start(self, params: dict) -> dict:
        """回始；完全平局死斗在每个新回合交换该回合首手方。"""
        relic_choices = params.get("relic_choices", {})
        self.combat.validate_round_start_relic_choices(relic_choices)
        result = self.combat.round_start(relic_choices)
        # 全局法术【回始】：普通战斗与死斗双方共用同一个回始，因此持有者扫描
        # 覆盖玩家侧与死斗对手侧；死斗对手视角下这就是其"敌回始"，见下方
        # _action_prepare_monster_phase/_action_resolve_monster_phase 的普通
        # 战斗敌回始接线，以及 round_start 本身对死斗对手来说是"己方回始"。
        result["spell_logs"] = self._resolve_global_trigger_spells_for_action(
            TriggerTiming.ROUND_START.value, params)
        self.state.combat_subphase = CombatSubphase.PLAYER_ACTIONS.value
        if self.state.in_final_duel and self.state.duel_tie_alternating:
            if self.state.duel_rounds_started > 0:
                self.state.duel_round_first = (
                    "opponent_side" if self.state.duel_round_first == "player_side" else "player_side"
                )
            self.state.duel_rounds_started += 1
            self.state.duel_turn = self.state.duel_round_first
            result["duel_round_first"] = self.state.duel_round_first
            result["duel_tie_alternating"] = True
        return {"success": True, "action": "回始", "result": result}

    def _action_round_end(self, params: dict) -> dict:
        """回终"""
        # 全局法术【回终】必须在 combat.round_end() 真正清空法力/格挡之前结算——
        # "回终"是"回合即将结束"的那一刻，法术若挂在这个时点，理应还能花费
        # 本回合剩余的法力（否则「回终」这个时机会因为法力恒为0而变成实质上
        # 的伪接线，等同于没有真正实现）。这与"受到伤害前"必须在伤害落地前
        # 触发是同一个道理：时点名字里的"终/前"表示的是决策窗口的位置，
        # 不是"所有清算都做完之后才轮到法术"。
        spell_logs = self._resolve_global_trigger_spells_for_action(
            TriggerTiming.ROUND_END.value, params)
        result = self.combat.round_end(
            params.get("blood_lineage_cost_share_target_ref", ""))
        result["spell_logs"] = spell_logs
        self.state.combat_subphase = CombatSubphase.AWAIT_ROUND_START.value

        # 提取多路径胜利结果（已由 combat.round_end 结算）
        alt_paths = [e for e in result.get("effects", [])
                     if isinstance(e, dict) and e.get("type") in
                     ("sculpture", "proliferation", "cancer", "debt_bind", "redemption")]

        # 检查怪物困境
        difficulties = []
        for monster in self.state.enemies:
            if monster.is_alive:
                diff = self.combat.check_monster_difficulty(monster)
                if diff:
                    difficulties.append(diff)

        return {
            "success": True,
            "action": "回终",
            "result": result,
            "victory_paths": alt_paths,
            "monster_difficulties": difficulties,
            "note": "多路径胜利（雕塑/癌变/还债）已结算；消耗品可在后续回合使用"
        }

    def _apply_terminal_artifacts_on_battle_start(self) -> list[str]:
        """终音被动：两边各一份。体外心脏翻自己；羔羊之泪每边持有就打一轮全场50%。"""
        logs = []
        player = self.state.player
        opp = next((e for e in self.state.enemies
                    if e.entity_type == "轮回者" and e.is_alive), None)

        if "体外心脏" in self.state.artifacts_owned and player:
            self._artifact_base_blood_limit = player.blood_limit
            player.blood_limit *= 2
            player.current_hp *= 2
            logs.append(
                f"体外心脏：血限与当前生命临时翻倍"
                f"({self._artifact_base_blood_limit}→{player.blood_limit})")
        if "体外心脏" in self.state.opponent_artifacts_owned and opp:
            self._opponent_artifact_base_blood_limit = opp.blood_limit
            opp.blood_limit *= 2
            opp.current_hp *= 2
            logs.append(
                f"对手体外心脏：血限与当前生命临时翻倍"
                f"({self._opponent_artifact_base_blood_limit}→{opp.blood_limit})")

        def _lamb_tear():
            for e in self.state.get_all_player_side() + self.state.get_all_enemy_side():
                loss = math.ceil(e.current_hp * 0.5)
                self.combat._apply_hostile_damage(e, loss, ctx={
                    "timing": "battle_start", "source": "羔羊之泪", "source_type": "artifact",
                    "target": e, "mechanic": "damage", "subtype": "artifact",
                    "amount": loss, "tags": {"artifact", "battle_start"},
                })

        if "羔羊之泪" in self.state.artifacts_owned:
            _lamb_tear()
            logs.append("羔羊之泪：场上所有角色与怪物立刻失去50%当前生命")
        if "羔羊之泪" in self.state.opponent_artifacts_owned:
            _lamb_tear()
            logs.append("对手羔羊之泪：场上所有角色与怪物立刻失去50%当前生命")
        return logs

    def _action_restart_battle(self, params: dict) -> dict:
        """恢复到本场战始前并按当前规则重新开始；不产生战斗奖励或战终结算。"""
        snapshot = self._battle_restart_snapshot
        if not snapshot:
            return {"success": False, "error": "本场没有可用的战始快照，无法重来"}
        self.state = copy.deepcopy(snapshot["state"])
        self.dice = copy.deepcopy(snapshot["dice"])
        self.combat.state = self.state
        self.combat.dice = self.dice
        register_combat_event_observer(self.state, self.combat)
        self._restore_combat_runtime(copy.deepcopy(snapshot["combat_runtime"]))
        # 重来本场时丢弃旧战斗遗留的中断、pending 和上次结果。
        self._pending_interrupts = []
        self._last_result = None
        start_params = copy.deepcopy(snapshot.get("battle_start_params") or {})
        result = self._action_battle_start(start_params)
        if not result.get("success"):
            return result
        result["action"] = "重来本场战斗"
        result["restart"] = True
        return result

    def _action_battle_start(self, params: dict) -> dict:
        """战始：配方式出怪(总数N=随机(1,上界)、首发S=随机(1,N)、增援R_i/T_i 随机；
        上界：一阶=战斗场数-3最低1，二阶及以上=12；允许重复抽选同一怪物种族)→结算战始遗物。
        配方口径唯一事实源见 `engine/monsters.py::roll_spawn_plan`。
        战斗背景：文档"战斗背景：（名称与影响）"仅为战斗推演格式里的占位提示，
        正文未定义任何具体名称与机制效果，故本引擎不做机制化处理，留给叙事层自由发挥。"""
        from .monsters import make_monster_entity, roll_spawn_plan, scale_monster_def_for_endless
        relic_choices = params.get("relic_choices", {})
        # 先完成全部静态校验，再抽怪；非法遗物参数不得消耗正式随机源。
        self.combat.validate_battle_start_relic_choices(relic_choices)
        world_encounter_id = params.get("world_encounter_id")
        world_battle = bool(world_encounter_id)
        world_participant_enemies = []
        if world_battle:
            try:
                world = self._world_from_state()
            except ValueError as exc:
                return {"success": False, "error": str(exc)}
            encounter = world.encounters.get(world_encounter_id)
            player_id = getattr(self.state.player, "runtime_id", "") if self.state.player else ""
            if encounter is None or not encounter.active or player_id not in encounter.participants:
                return {"success": False, "error": "world_encounter_id必须指向包含当前轮回者的有效地图遭遇"}
            enemy_by_id = {entity.runtime_id: entity for entity in self.state.enemies}
            participant_ids = [actor_id for actor_id in encounter.participants if actor_id != player_id]
            if not participant_ids or any(actor_id not in enemy_by_id for actor_id in participant_ids):
                return {"success": False,
                        "error": "当前战斗接入仅支持已登记在敌方单位列表中的遭遇参与者；目标未映射到现有战斗实体"}
            world_participant_enemies = [enemy_by_id[actor_id] for actor_id in participant_ids]
            if any(not entity.is_alive for entity in world_participant_enemies):
                return {"success": False, "error": "地图遭遇包含已死亡的敌方单位"}
            self.state.world_enemy_reserve = [
                entity for entity in self.state.enemies
                if entity.runtime_id not in set(participant_ids)
            ]
            self.state.world_encounter_id = world_encounter_id
            self.state.world_encounter_entities = list(world_participant_enemies)
        # 在战始产生随机怪物前保存快照；重来会恢复随机源并再次按当前代码抽取。
        self._battle_restart_snapshot = {
            "state": copy.deepcopy(self.state),
            "dice": copy.deepcopy(self.dice),
            "combat_runtime": copy.deepcopy(self._snapshot_combat_runtime()),
            "battle_start_params": copy.deepcopy(params),
        }
        self.state.phase = GamePhase.IN_COMBAT.value
        self.state.combat_subphase = CombatSubphase.AWAIT_ROUND_START.value
        self.state.current_battle += 1
        # 记录[战始]生命，作为[战终]清除"回复"类增益后的生命下限
        for entity in (([self.state.player] if self.state.player else [])
                       + self.state.friends + self.state.employees + self.state.temp_friends):
            entity.battle_start_hp = entity.current_hp
            entity.healed_this_battle = 0
            entity.total_healed = 0
            entity.no_action_rounds = 0
            entity.no_damage_rounds = 0
        self.state.scoped_effect_ledger = []
        # 2026-10-14：【增殖】战终待决按场重置（逃跑/重开等未结算路径不得带入下场）。
        self.state.event_modifiers.pop("zengzhi_pending", None)
        # 事件流按场重置：只在本场战斗内可观测，避免长模拟无界增长。
        self.state.combat_events = []
        self.state.current_round = 0
        self.combat.reset_monster_activation()
        self.state.shared_dragon_heart_type = ""  # 共心环：每场需重新选定
        self.state.godfather_revolver_uses = 0
        gun = next((c for c in self.state.consumables if c.name == "教父左轮" and c.kind == "artifact_weapon"), None)
        if gun is not None:
            gun.current_uses = gun.max_uses  # 教父左轮：[战终]耐久回满(此处按下一场[战始]起效实现，效果等价)

        region = self.state.current_region
        pool = self._monster_pool_for_battle()
        endless_cycle = int(getattr(self.state, "endless_cycle", 0) or 0)
        self.state.enemies.clear()
        # 自愈X「目标死亡时」：新战斗选中集合清零（上场选过的死了不算）。
        self.state.player_selected_targets.clear()
        self.state.monster_reinforcements = []
        self.state.delayed_monster_reentries = []
        drawn_names = []
        queued_names = []
        draw_count = 0
        spawn_plan = None
        if world_battle:
            self.state.enemies.extend(world_participant_enemies)
            draw_count = len(world_participant_enemies)
            drawn_names = [entity.name for entity in world_participant_enemies]
            spawn_plan = {"total": draw_count, "first_count": draw_count,
                          "queue_rounds": [], "waves": [], "world_encounter_id": world_encounter_id}
        elif pool:
            spawn_plan = roll_spawn_plan(self.dice, self.state.current_battle, self._current_tier())
            draw_count = spawn_plan["total"]
            drawn_defs = []
            for i in range(draw_count):
                roll = self.dice.auto_roll(f"monster_draw_{self.state.current_battle}_{i}", pool,
                                            context=f"出怪(第{self.state.current_battle}场,第{i + 1}只)")
                drawn_defs.append(scale_monster_def_for_endless(roll["selected"], endless_cycle))
            first_count = min(int(spawn_plan["first_count"]), draw_count)
            queue_rounds = spawn_plan["queue_rounds"]
            for i, monster_def in enumerate(drawn_defs):
                if i < first_count:
                    m = make_monster_entity(monster_def)
                    m.spawned_round = 1
                    self.combat.init_monster_shards(m)
                    self.state.enemies.append(m)
                    drawn_names.append(monster_def["name"])
                else:
                    entry = dict(monster_def)
                    entry["arrive_round"] = int(queue_rounds[i - first_count])
                    self.state.monster_reinforcements.append(entry)
                    queued_names.append(entry["name"])

        # 事件登记的下一场修正全部在战始一次性消费。
        modifiers = self.state.event_modifiers
        reveal_full_information = bool(modifiers.pop("next_battle_full_information", False))
        if not world_battle and modifiers.pop("bounty_extra_monster", False) and pool:
            roll = self.dice.auto_roll(
                f"bounty_monster_{self.state.current_battle}", pool,
                context="通缉悬赏榜额外帮派怪物",
            )
            bonus = make_monster_entity(scale_monster_def_for_endless(roll["selected"], endless_cycle))
            self.combat.init_monster_shards(bonus)
            self.state.enemies.append(bonus)
            drawn_names.append(bonus.name + "(悬赏额外)")
        next_fake = modifiers.pop("next_battle_fake_shards", 0)
        if next_fake:
            self.state.fake_shards += next_fake
        if modifiers.get("loan_active"):
            self.state.shards -= 10
            if self.state.shards < 0 and self.state.player:
                interest = math.ceil(abs(self.state.shards) / 10) * 5
                self.state.player.blood_limit = max(0, self.state.player.blood_limit - interest)
                # 2026-10-14：【猩红果实】持有者豁免血限压顶（生命可超血限）。
                if not self.state.hp_overflow_exempt(self.state.player):
                    self.state.player.current_hp = min(self.state.player.current_hp, self.state.player.blood_limit)
        red_spring = modifiers.get("red_spring_battle_losses", 0)
        if red_spring > 0 and self.state.player:
            self.combat._raw_hp_loss(self.state.player, 4, ctx={
                "timing": "battle_start", "source": "赤泉囊", "source_type": "consumable",
                "actor": self.state.player, "target": self.state.player, "mechanic": "hp_loss",
                "subtype": "battle_start_loss", "amount": 4, "tags": {"consumable", "battle_start"},
            })
            modifiers["red_spring_battle_losses"] = red_spring - 1

        # 事件登记的"下一场额外出现的怪物"（如龙心谷"追求者·拿走口粮"）
        forced = [] if world_battle else list(self.state.forced_monsters_next_battle)
        if not world_battle:
            self.state.forced_monsters_next_battle = []
        for fm in forced:
            m = make_monster_entity(scale_monster_def_for_endless(fm, endless_cycle))
            self.combat.init_monster_shards(m)
            self.state.enemies.append(m)
            drawn_names.append(fm["name"] + "(额外出现)")
        arena_percent = modifiers.pop("arena_health_percent", 0)
        if arena_percent:
            for monster in self.state.enemies:
                gain = math.ceil(monster.blood_limit * arena_percent / 100)
                self.state.apply_scoped_delta(
                    monster, "blood_limit", gain,
                    scope=EffectScope.BATTLE.value, polarity=EffectPolarity.BUFF.value,
                    source="地下角斗场")
                monster.current_hp += gain

        # DM裁定 2026-09-09：法力不再每[回始]回填，改为**一池制**——[战始]给满
        # 等同[法限]的一池，整场只出不进，[战终]复原（与[速度]同口径）。
        # 先赋值再结算战始遗物，遗物额外法力因此叠在满池之上，不会被赋值冲掉。
        if self.state.player and self.state.player.is_alive:
            self.state.player.current_mana = self.state.player.mana_limit
        relic_logs = self.combat.process_relics(TriggerTiming.BATTLE_START, {"relic_choices": relic_choices})

        artifact_logs = self._apply_terminal_artifacts_on_battle_start()

        # 全局法术【战始】：必须在战始遗物结算之后才检查，
        # 否则法力已被本action开头清零、任何消耗法力的战始法术都会法力不足——
        # 与"战始遗物在0上叠加"的既有顺序一致，法术同样吃得到本场战始的
        # 法力加成。
        spell_logs = self._resolve_global_trigger_spells_for_action(
            TriggerTiming.BATTLE_START.value, params)

        waves = (spawn_plan["waves"] if spawn_plan else [])
        return {
            "success": True, "action": "战始",
            "battle_number": self.state.current_battle,
            "region": region,
            "draw_count": draw_count,                 # 本场怪物总数 N（配方口径）
            "first_wave_count": (spawn_plan["first_count"] if spawn_plan else 0),
            "spawn_plan": spawn_plan,                  # 配方明细：N/S/每波进场回合与数量
            "enemies": drawn_names,
            "queued_reinforcements": queued_names,
            "reinforcement_waves": waves,
            "full_information": ([enemy.to_dict() for enemy in self.state.enemies]
                                 if reveal_full_information else None),
            "relic_logs": relic_logs,
            "spell_logs": spell_logs,
            "artifact_logs": artifact_logs,
            "instruction": (f"首发{len(drawn_names)}只怪物已进场"
                            + (f"；另有{len(queued_names)}只增援待命（"
                               + "、".join(f"R{w['arrive_round']}进{w['count']}只" for w in waves)
                               + "）" if queued_names else "（无增援）")
                            + "；请补充选择本场战斗背景(纯叙事，不影响数值)并结算其余[战始]效果"),
        }

    # ==================== 最终的冠冕 / 第8场最终死斗 ====================
    # 完成第7场后自动触发：无封存候选→完整封存当前角色(含团队)，玩家以新轮回者重新开始；
    # 已有候选→双方(各自带队伍)进入死斗。整套流程无需玩家选择，[战终]内自动判定与转场。
    # 二阶及以上副本未实现：胜者同样被完整封存(而不是尝试接入不存在的下一阶内容)，
    # 成为下一位挑战者的候选人，效果上形成擂台循环。

    def _serialize_entity_full(self, e: Entity) -> dict:
        """完整序列化单个实体(用于封存候选人，需能无损还原道纹X值/法术/状态/面板等)"""
        return {
            "name": e.name, "entity_type": e.entity_type,
            "world_region": e.world_region, "world_x": e.world_x, "world_y": e.world_y,
            "is_neutral": e.is_neutral,
            "blood_limit": e.blood_limit, "current_hp": e.current_hp,
            "mana_limit": e.mana_limit, "current_mana": e.current_mana,
            "speed_limit": e.speed_limit, "current_speed": e.current_speed,
            "attack_count": e.attack_count, "attack_power": e.attack_power,
            "shield": e.shield, "is_flying": e.is_flying, "is_alive": e.is_alive,
            "shards": e.shards, "shards_peak": e.shards_peak,
            "is_debt_bound": e.is_debt_bound,
            # ④修复：异变是代价资源、封存前已实付——
            # 带伤续战必须带着异变续战，否则封存=免费洗白迷失进度。
            # （癌变 total_healed 不随封存走：DM 已裁定它是局内减益、每场归零。）
            "mutation_count": e.mutation_count,
            # 致死进度：封存快照同样保留可读进度
            "no_action_rounds": e.no_action_rounds, "no_damage_rounds": e.no_damage_rounds,
            "lethal_counters": {k: list(v) for k, v in e.lethal_counters().items()},
            "lethal_progress": e.lethal_progress(),
            "dao_wen": {k: v.x_value for k, v in e.dao_wen.items()},
            "spells": [s.to_dict() for s in e.spells],
            "relics": [r.to_dict() for r in e.relics],
            "status_effects": [{"name": s.name, "value": s.value,
                                 "remaining_rounds": s.remaining_rounds, "source": s.source,
                                 "scope": s.scope, "polarity": s.polarity,
                                 "daowen_order": s.daowen_order}
                                for s in e.status_effects],
        }

    def _deserialize_entity_full(self, d: dict) -> Entity:
        """按_serialize_entity_full的格式还原实体"""
        e = Entity(name=d["name"], entity_type=d["entity_type"],
                   world_region=d.get("world_region", ""), world_x=d.get("world_x"),
                   world_y=d.get("world_y"), is_neutral=d.get("is_neutral", False),
                   blood_limit=d["blood_limit"], current_hp=d["current_hp"],
                   mana_limit=d["mana_limit"], current_mana=d["current_mana"],
                   speed_limit=d["speed_limit"], current_speed=d["current_speed"],
                   attack_count=d["attack_count"], attack_power=d["attack_power"],
                   shield=d.get("shield", 0), is_flying=d.get("is_flying", False),
                   is_alive=d.get("is_alive", True), shards=d.get("shards", 0),
                   shards_peak=d.get("shards_peak", max(0, int(d.get("shards", 0)))),
                   is_debt_bound=d.get("is_debt_bound", False))
        e.mutation_count = d.get("mutation_count", 0)   # ④修复：旧档无此键回退0
        for name, x in d.get("dao_wen", {}).items():
            e.dao_wen[name] = DaoWenInstance(
                DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""), x_value=x)
        # 旧封存未记录道纹位置；对同名道纹状态从实体当前序列恢复位置。
        # 新格式优先使用显式 daowen_order，避免状态名与来源道纹名不同时猜测。
        daowen_order_by_name = {name: index for index, name in enumerate(e.dao_wen)}
        for sp in d.get("spells", []):
            e.spells.append(Spell(name=sp["name"], required_daowen=sp["required_daowen"],
                                   trigger_condition=sp["trigger_condition"], effect_flow=sp["effect_flow"],
                                   rank=sp.get("rank", 1),
                                   custom_conditions=sp.get("custom_conditions", []),
                                   automatic=sp.get("automatic", False),
                                   # 旧档无 lifecycle 键 → permanent（与旧行为一致）
                                   lifecycle=sp.get("lifecycle", "permanent"),
                                   trigger=sp.get("trigger")))
        # 旧存档的 armed_spells 迁移为正式法术定义；新存档不再写入该字段。
        for spell_name in d.get("armed_spells", []) or []:
            if any(sp.name == spell_name for sp in e.spells):
                continue
            migrated = self.combat.spell_definition(e, spell_name)
            if migrated is not None:
                e.spells.append(migrated)
        for relic in d.get("relics", []):
            e.relics.append(Relic(name=relic["name"], effect=relic.get("effect", ""),
                                  tags=list(relic.get("tags") or [])))
        for st in d.get("status_effects", []):
            e.status_effects.append(StatusEffect(name=st["name"], value=st.get("value", 0),
                                                  remaining_rounds=st.get("remaining_rounds", -1),
                                                  source=st.get("source", ""),
                                                  scope=st.get("scope", EffectScope.BATTLE.value),
                                                  polarity=st.get("polarity", EffectPolarity.NEUTRAL.value),
                                                  # 旧档 X/施加序号仅保留为兼容元数据，不参与结算顺序。
                                                  activation_x=st.get("activation_x"),
                                                  application_sequence=st.get("application_sequence", 0),
                                                  daowen_order=(st.get("daowen_order")
                                                                if st.get("daowen_order") is not None
                                                                else daowen_order_by_name.get(st["name"]))))
        return e

    def _serialize_full_character(self) -> dict:
        """完整封存：玩家+队友(朋友/员工)+遗物+残韵+碎片+死者之书等级+属性点+终音法器/初拥之夜/真龙之心记录。
        死斗载入时写入 opponent_relics / opponent_artifacts_owned，被动按持有者一侧各自结算。"""
        s = self.state
        return {
            "player": self._serialize_entity_full(s.player) if s.player else None,
            "friends": [self._serialize_entity_full(f) for f in s.friends],
            "employees": [self._serialize_entity_full(e) for e in s.employees],
            "relics": [r.to_dict() for r in s.relics],
            "resonance": dict(s.resonance),
            "shards": s.shards,
            "shards_peak": s.shards_peak,
            "death_book_wisdom": list(s.death_book_wisdom),
            "death_book_legacies": [dict(entry) for entry in s.death_book_legacies],
            "attribute_points": s.attribute_points,
            "current_region": s.current_region,
            "artifacts_owned": list(s.artifacts_owned),
            "first_embrace_traits": list(s.first_embrace_traits),
            "pending_first_embrace": s.pending_first_embrace,
            "chizu_names": list(s.chizu_names),
            "dragon_nature": s.dragon_nature,
            "dragon_traits": list(s.dragon_traits),
            "has_sacrifice_action": s.has_sacrifice_action,
        }

    def _restore_side_from_snapshot(self, snapshot: dict) -> list[Entity]:
        """把封存快照的player+friends+employees还原为一个整体阵营(供死斗对手使用)。
        与挑战者一侧(玩家/朋友/员工)存在同名实体时强制改名，避免按名字查找目标时
        (全引擎的目标解析都是按name匹配)误伤/误判到己方同名单位——这不是死斗专属规则，
        是让"两个都叫轮回者的角色对战"这一必然场景下引擎仍能正确工作的必要前提。"""
        existing_names = {e.name for e in self.state.get_all_player_side()}
        side = []

        def _uniquify(entity: Entity):
            if entity.name in existing_names:
                entity.name = f"{entity.name}（对手）"
            existing_names.add(entity.name)
            side.append(entity)

        if snapshot.get("player"):
            opponent_player = self._deserialize_entity_full(snapshot["player"])
            opponent_player.entity_type = "轮回者"
            # 死斗快照顶层的碎片余额属于轮回者本体；战斗时映射到对手实体账本。
            opponent_player.shards = int(snapshot.get("shards", opponent_player.shards))
            opponent_player.shards_peak = max(
                int(getattr(opponent_player, "shards_peak", 0)),
                int(snapshot.get("shards_peak", max(0, opponent_player.shards))))
            _uniquify(opponent_player)
        for f in snapshot.get("friends", []):
            _uniquify(self._deserialize_entity_full(f))
        for e in snapshot.get("employees", []):
            _uniquify(self._deserialize_entity_full(e))
        return side

    def _duel_priority_key(self, e: Entity) -> tuple:
        """先手顺序：速限→法限→血限→当前生命，数值越大越先手"""
        return (e.speed_limit, e.mana_limit, e.blood_limit, e.current_hp)

    def _read_seal_payload(self) -> dict:
        """封存槽文件的原始 payload（{"candidates": …, "progression": …}）；缺失/坏文件→空。"""
        path = self.sealed_candidate_path
        if not path or not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (ValueError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _progression_payload(self) -> dict:
        """跨轮回进度的落盘内容。

        返回非空（需要落盘）的条件：
        1) PROGRESSION_ENABLED=True —— 只要开关打开，就持久化 unlocked_tier/endless_mode/
           endless_cycle，哪怕 unlocked_tier=1、还在第一阶；
        2) 或者当前已处于无尽模式（endless_mode=True）—— 即使开关已关，也保留无尽进度
           避免回退。
        否则返回 {} —— 默认第一阶体验不写任何 progression 段，保持旧「无进度即无段」语义。
        """
        from .monsters import PROGRESSION_ENABLED
        if not PROGRESSION_ENABLED and not self.state.endless_mode:
            return {}
        return {"unlocked_tier": int(self.state.unlocked_tier),
                "endless_mode": bool(self.state.endless_mode),
                "endless_cycle": int(self.state.endless_cycle)}

    def _write_seal_payload(self, candidates: dict, progression: dict) -> None:
        """写回封存槽与进度；两者皆空时删除文件（旧「无候选即删文件」语义不变）。"""
        path = self.sealed_candidate_path
        payload: dict = {}
        non_empty_candidates = {str(t): q for t, q in sorted(candidates.items()) if q}
        if non_empty_candidates:
            payload["candidates"] = non_empty_candidates
        if progression:
            payload["progression"] = progression
        if not payload:
            if os.path.exists(path):
                os.remove(path)
            return
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def _load_seal_slots(self) -> dict:
        """读取阶级封存槽文件 {阶级: [候选快照, ...]}（先来后到队列）。

        兼容旧版单候选格式（直接是快照 dict）——视为一阶槽队首，
        保证升级前后已有的候选人文件不丢失。
        """
        slots: dict = {}
        data = self._read_seal_payload()
        if isinstance(data.get("candidates"), dict):
            for tier_str, queue in data["candidates"].items():
                try:
                    tier = int(tier_str)
                except (TypeError, ValueError):
                    continue
                slots[tier] = [q for q in (queue or []) if isinstance(q, dict)]
        elif isinstance(data.get("player"), dict):
            slots[1] = [data]
        return slots

    def _save_seal_slots(self, slots: dict) -> None:
        """写回阶级封存槽文件；空阶级槽不落盘；全部槽为空时删除文件（跨轮回进度随后落盘）。"""
        self._write_seal_payload(slots, self._progression_payload())

    # ==================== 阶级推进 / 无尽模式 ====================

    def _load_progression(self) -> None:
        """从封存槽文件的 `progression` 段装回「已解锁阶级 / 无尽模式」。

        与封存槽同文件：跨轮回有效、随 `sealed_candidate_path` 一起被测试隔离。
        文件缺失、缺段或数值非法时一律保持初始值（1 阶、非无尽、0 轮）。
        """
        data = self._read_seal_payload().get("progression")
        if not isinstance(data, dict):
            return
        try:
            self.state.unlocked_tier = max(1, int(data.get("unlocked_tier", 1)))
            self.state.endless_cycle = max(0, int(data.get("endless_cycle", 0)))
        except (TypeError, ValueError):
            return
        self.state.endless_mode = bool(data.get("endless_mode", False))

    def _save_progression(self, slots: Optional[dict] = None) -> None:
        """落盘「已解锁阶级 / 无尽模式」：跨轮回进度，不随新轮回者清零（候选队列原样保留）。
        若调用方已加载好 slots（例如已把本次胜者追加进去），直接使用以避免把胜者写丢。
        """
        base = slots if slots is not None else self._load_seal_slots()
        self._write_seal_payload(base, self._progression_payload())

    def _advance_region_unlock(self, won_duel_tier: int, slots: Optional[dict] = None) -> dict:
        """死斗胜利后推进阶级：解锁 `won_duel_tier + 1` 阶副本。

        「胜者进入下一阶级副本」在引擎里的落点是**轮回者血脉的进度**（持久化），
        因为胜者本人随后进进阶封存槽、不再被玩家操作。
        通过五阶死斗 ⇒ 进入无尽模式（`endless_cycle` 从 1 起算）。

        参数 `slots`：调用方已组装好的封存槽（含本次胜者）；当开关关闭时仍需落盘
        （否则下面 _replace_state_preserving_death_book_progress 会清空 state，
        本次胜者的快照丢失）。开关打开时则由 _save_progression 一并把 slots 重写。

        2026-09-28 用户令：阶级推进总开关 `PROGRESSION_ENABLED` 默认 False；
        关闭时不修改 unlocked_tier/endless_mode、不写 progression 段——
        留作未来五阶全部接入后启用。
        """
        from .monsters import PROGRESSION_ENABLED
        if not PROGRESSION_ENABLED:
            if slots is not None:
                self._save_seal_slots(slots)
            return {}
        won_duel_tier = int(won_duel_tier or 0)
        if won_duel_tier <= 0:
            return {}
        before = int(self.state.unlocked_tier)
        self.state.unlocked_tier = max(before, won_duel_tier + 1)
        entered_endless = False
        if won_duel_tier >= GameState.MAX_TIER and not self.state.endless_mode:
            self.state.endless_mode = True
            self.state.endless_cycle = 1
            entered_endless = True
        self._save_progression(slots=slots)
        note = ""
        if self.state.unlocked_tier > before:
            note = (f"阶级推进：已解锁至{self.state.unlocked_tier}阶副本"
                    + ("；无尽模式开启（怪物池=全部副本，精力递减，无法探索）" if entered_endless else ""))
        return {"unlocked_tier": self.state.unlocked_tier,
                "advanced": self.state.unlocked_tier > before,
                "endless_mode": self.state.endless_mode,
                "endless_cycle": self.state.endless_cycle,
                "note": note}

    def _advance_endless_cycle(self) -> dict:
        """无尽模式每完成一轮（第7场战终）强度递增一档。

        总开关 PROGRESSION_ENABLED=False 时不推进轮次（无尽模式本身只会由
        _advance_region_unlock 打开，关推进=永不进入无尽）。
        """
        from .monsters import PROGRESSION_ENABLED
        if not PROGRESSION_ENABLED or not self.state.endless_mode:
            return {}
        self.state.endless_cycle = int(self.state.endless_cycle) + 1
        self._save_progression()
        return {"endless_cycle": self.state.endless_cycle,
                "energy_budget": self.state.energy_budget}

    def unlocked_regions(self) -> list[str]:
        """当前可选副本：已实现（进入运行时）且阶级≤已解锁阶级；无尽模式不设限制。

        阶级推进总开关关闭（PROGRESSION_ENABLED=False）时，固定返回全部已实现副本，
        与 2026-09-28 之前的旧行为一致；开关打开后才按 unlocked_tier 门禁。
        顺序固定按 `副本索引.md` 的登记顺序（`monster_pool` 的键序）。
        """
        from .gamedata import REGION_TIERS
        from .monsters import PROGRESSION_ENABLED
        regions = list(self.monster_pool.keys())
        if not PROGRESSION_ENABLED or self.state.endless_mode:
            return regions
        return [r for r in regions if int(REGION_TIERS.get(r, 1)) <= int(self.state.unlocked_tier)]

    def _monster_pool_for_battle(self) -> list[dict]:
        """本场出怪池：无尽模式＝全部已实现副本合并。"""
        if self.state.endless_mode:
            from .monsters import merge_monster_pools
            return merge_monster_pools(self.monster_pool)
        return self.monster_pool.get(self.state.current_region, [])

    def _current_tier(self) -> int:
        """当前副本阶级（无尽模式下按当前副本记，用于出怪配方 N 的上界）。"""
        from .gamedata import REGION_TIERS
        return int(REGION_TIERS.get(self.state.current_region, 1))

    def _trigger_final_crown(self) -> dict:
        """完成第7场后自动触发【最终的冠冕】。

        死斗规则：封存按阶级分槽，每个阶级槽是一份候选队列。
        挑战者只能与自身副本阶级的队首候选死斗；通过死斗者进入下一阶级封存槽
        （进阶封存），不得再与原阶级角色死斗。
        """
        from .gamedata import REGION_TIERS
        tier = REGION_TIERS.get(self.state.current_region, 1)
        # 无尽模式：一轮=7场打完触发冠冕，此时强度递增一档。
        # 放在封存/转场之前，因为下面的流程可能整体替换 state。
        endless_update = self._advance_endless_cycle()
        sealed_name = self.state.player.name if self.state.player else "轮回者"
        slots = self._load_seal_slots()
        queue = [q for q in slots.get(tier, []) if q]
        if not queue:
            slots[tier] = [self._serialize_full_character()]
            self._save_seal_slots(slots)
            self._replace_state_preserving_death_book_progress()
            return {
                "outcome": "sealed",
                "sealed_name": sealed_name,
                "tier": tier,
                "endless": (endless_update or None),
                "instruction": f"无{tier}阶封存候选，{sealed_name}已连同队伍完整封存入{tier}阶封存槽；"
                               "请调用 setup_attributes 开始新的轮回者"
                               + (("；" + f"无尽第{endless_update['endless_cycle']}轮开始，"
                                           f"局外精力{endless_update['energy_budget']}点")
                                  if endless_update else ""),
            }
        candidate_snapshot = queue.pop(0)
        slots[tier] = queue
        self._save_seal_slots(slots)  # 队首候选已被取用；同槽剩余候选继续排队
        # 暂存擂主快照：挑战者落败时放回队首（规则正文「封存…直到下一名同阶级
        # 轮回者完成第7场战斗"——卫冕成功则封存依旧有效，此前擂主被无声吞掉）。
        self.state.duel_defending_snapshot = candidate_snapshot

        self.state.duel_tier = tier
        challenger_player = self.state.player
        opponent_side = self._restore_side_from_snapshot(candidate_snapshot)
        opponent_leader = next((e for e in opponent_side if e.entity_type == "轮回者"), None)
        # 残韵是轮回者**局外准备**的资源；封存快照顶层把主人的残韵完整记载。
        # 守擂者同为轮回者，应带上自己真实准备的残韵（哪怕为 0），而非清空或凭空充能——
        # 这样「有就有，没有就没有」，才不会给没准备的对手塞残韵。
        if opponent_leader is not None:
            opponent_leader.resonance = dict(candidate_snapshot.get("resonance") or {})

        self.state.enemies = opponent_side
        self.state.current_round = 0
        self.combat.reset_monster_activation()
        self.state.in_final_duel = True
        self.state.opponent_relics = [
            Relic(name=r.get("name", ""), effect=r.get("effect", ""), tags=r.get("tags") or [])
            for r in (candidate_snapshot.get("relics") or [])
            if r.get("name")
        ]
        self.state.opponent_artifacts_owned = list(candidate_snapshot.get("artifacts_owned") or [])
        # 旧快照可能只记下了 dragon_traits / first_embrace_traits 名字，遗物缺 tag
        dragon_names = list(candidate_snapshot.get("dragon_traits") or [])
        embrace_names = list(candidate_snapshot.get("first_embrace_traits") or [])
        existing = {r.name for r in self.state.opponent_relics}
        for r in self.state.opponent_relics:
            if r.name in dragon_names and "龙族" not in r.tags:
                r.tags.append("龙族")
            if r.name in embrace_names and "血族" not in r.tags:
                r.tags.append("血族")
        for name in dragon_names:
            if name not in existing:
                self.state.opponent_relics.append(Relic(name=name, effect="", tags=["龙族"]))
                existing.add(name)
        for name in embrace_names:
            if name not in existing:
                self.state.opponent_relics.append(Relic(name=name, effect="", tags=["血族"]))
                existing.add(name)
        # 与战始相同：死斗开场给双方轮回者各一满池法力，
        # 整场不再回填，[战终]复原。
        for entity in self.state.get_all_player_side() + self.state.get_all_enemy_side():
            if entity.entity_type == "轮回者" and entity.is_alive:
                entity.current_mana = entity.mana_limit
                entity.battle_start_hp = entity.current_hp
                entity.healed_this_battle = 0
        artifact_logs = self._apply_terminal_artifacts_on_battle_start()

        challenger_key = self._duel_priority_key(challenger_player)
        opponent_key = self._duel_priority_key(opponent_leader) if opponent_leader else (0, 0, 0, 0)
        complete_tie = challenger_key == opponent_key
        if complete_tie:
            roll = self.dice.auto_roll(
                "final_duel_complete_tie_first",
                ["player_side", "opponent_side"],
                context="最终死斗四项先手属性完全平局：随机首手",
            )
            first_mover = roll["selected"]
        else:
            first_mover = "player_side" if challenger_key > opponent_key else "opponent_side"
        self.state.duel_turn = first_mover
        self.state.duel_tie_alternating = complete_tie
        self.state.duel_round_first = first_mover
        self.state.duel_rounds_started = 0
        self.state.phase = GamePhase.IN_COMBAT.value
        self.state.combat_subphase = CombatSubphase.AWAIT_ROUND_START.value

        # 2026-10-13 用户令删【三相残韵盘】后，死斗开场已无可选遗物；
        # optional_relics 键保留（契约稳定），恒为空列表。
        optional = []

        return {
            "outcome": "duel_start",
            "tier": tier,
            "endless": (endless_update or None),
            "opponent_name": opponent_leader.name if opponent_leader else "未知对手",
            "opponent_side": [e.name for e in opponent_side],
            "first_mover": first_mover,
            "complete_tie_alternating": complete_tie,
            "optional_relics": optional,
            "artifact_logs": artifact_logs,
            "instruction": f"第8场最终死斗（{tier}阶）开始：双方交替出手，残韵可任意时刻插队，无法逃跑。"
                           "可选遗物由持有者自己决定是否发动（activate_duel_relic）；"
                           "请调用 resolve_final_duel(outcome=victory/defeat) 结算胜负",
        }

    def _action_activate_duel_relic(self, params: dict) -> dict:
        return handle_activate_duel_relic(self, params)

    def _action_resolve_final_duel(self, params: dict) -> dict:
        return handle_resolve_final_duel(self, params)

    def _finalize_victory_seal(self, advance_tier: Optional[int] = None) -> dict:
        """完整封存当前(胜利的)角色，写入阶级封存槽，重置引擎状态等待新轮回者。

        advance_tier：死斗胜者传入其死斗阶级——胜者进入下一阶级的进阶封存，
        不得再与原阶级角色死斗。未传入时按当前副本阶级封存。
        """
        from .gamedata import REGION_TIERS
        sealed_name = self.state.player.name if self.state.player else "轮回者"
        if advance_tier:
            tier = advance_tier + 1
        else:
            tier = REGION_TIERS.get(self.state.current_region, 1)
        slots = self._load_seal_slots()
        slots.setdefault(tier, []).append(self._serialize_full_character())
        # 阶级推进：
        # - 开关关闭：仅把含本次胜者的 slots 落盘，不推进阶级。
        # - 开关打开：同时推进 unlocked_tier/endless 并把 slots+progression 一起落盘。
        # 必须 **在** replace_state 之前落盘，否则新 state 的默认 unlocked_tier=1 会把进度写没。
        progression = self._advance_region_unlock(int(advance_tier or 0), slots=slots)
        self._replace_state_preserving_death_book_progress()
        note = ("；" + progression["note"]) if progression and progression.get("note") else ""
        return {"sealed_name": sealed_name, "tier": tier,
                "progression": (progression or None),
                "instruction": f"{sealed_name}获胜并连同队伍完整封存入{tier}阶封存槽"
                               f"（进阶封存：不再与原阶级角色死斗）；"
                               "请调用 setup_attributes 开始新的轮回者" + note}

    def _action_choose_terminal_artifact(self, params: dict) -> dict:
        """领取死斗胜利后的终音法器；若选择"猩红尖牙"则先触发初拥之夜，之后才真正完整封存"""
        region = self.state.pending_terminal_region
        if not region:
            return {"success": False, "error": "当前没有待领取的终音法器"}
        options = self.TERMINAL_ARTIFACTS.get(region, [])
        choice = params.get("choice", 0)
        if not isinstance(choice, int) or not (1 <= choice <= len(options)):
            return {"success": False, "error": f"choice必须是1~{len(options)}之间的整数"}
        name, effect = options[choice - 1]
        self.state.artifacts_owned.append(name)
        self.state.pending_terminal_region = ""

        if name == "红头绳":
            self.state.has_sacrifice_action = True
        if name == "教父左轮":
            self.state.consumables.append(Consumable(
                name="教父左轮", effect=effect, current_uses=6, max_uses=6, kind="artifact_weapon"))
        if name == "猩红尖牙":
            self.state.pending_first_embrace = True
            self.state.seal_pending_after_embrace = True
            return {"success": True, "action": "领取终音法器",
                    "result": {"artifact": name, "effect": effect, "first_embrace_pending": True,
                               "instruction": "已强制触发【初拥之夜】，请调用 choose_first_embrace(choice=1~9) 选择"}}

        seal = self._finalize_victory_seal(advance_tier=self.state.duel_tier or None)
        return {"success": True, "action": "领取终音法器",
                "result": {"artifact": name, "effect": effect, "seal": seal}}

    def _action_choose_first_embrace(self, params: dict) -> dict:
        """
        初拥之夜：9选1。1~8每项限选1次(除9外)，选择后[回复]30%[血限]。
        猩红尖牙触发的这一次选择完成后，若仍处于死斗胜利流程中，会紧接着完整封存角色。
        """
        if not self.state.pending_first_embrace:
            return {"success": False, "error": "当前没有待处理的初拥之夜"}
        choice = params.get("choice", 0)
        if choice not in self.FIRST_EMBRACE_OPTIONS:
            return {"success": False, "error": "choice必须是1~9之间的整数"}
        name, effect = self.FIRST_EMBRACE_OPTIONS[choice]
        if choice != 9 and name in self.state.first_embrace_traits:
            return {"success": False, "error": f"{name}已经选过，1~8每项限选1次"}

        player = self.state.player
        if not player:
            return {"success": False, "error": "没有玩家"}

        if choice != 9:
            # 以【遗物】形式授予：遗物是唯一事实源，自动继承遗物通用规则
            self.state.grant_relic(name, effect, tag="血族")
            self.state.pending_first_embrace = False
            if name == "不朽之躯":
                player.blood_limit = math.ceil(player.blood_limit / 2)
                # 2026-10-14：【猩红果实】持有者豁免血限压顶（生命可超血限）。
                if not self.state.hp_overflow_exempt(player):
                    player.current_hp = min(player.current_hp, player.blood_limit)
        else:
            pass  # 封存血脉：不清空pending_first_embrace，保留触发权

        heal_amount = math.ceil(player.blood_limit * 0.3)
        heal_detail = self.state.apply_heal(player, heal_amount, ctx={
            "timing": "pre_battle_event", "source": "初拥之夜", "source_type": "event",
            "actor": player, "target": player, "mechanic": "heal", "subtype": "first_embrace",
            "amount": heal_amount, "tags": {"event"},
        })

        result = {"success": True, "action": "初拥之夜",
                  "result": {"choice": choice, "trait": name, "effect": effect,
                             "healed": heal_detail["actual_heal"]}}

        if self.state.seal_pending_after_embrace:
            self.state.seal_pending_after_embrace = False
            result["result"]["seal"] = self._finalize_victory_seal(advance_tier=self.state.duel_tier or None)
        return result

    # ==================== 初拥之夜：可主动发动的具体能力 ====================

    def _action_use_blood_wings(self, params: dict) -> dict:
        """鲜血之翼：代价流血5X，发动【飞行X】回合"""
        if "鲜血之翼" not in self.state.first_embrace_traits:
            return {"success": False, "error": "没有鲜血之翼"}
        player = self.state.player
        x = params.get("x", 0)
        if not player or not isinstance(x, int) or x < 1:
            return {"success": False, "error": "X必须是正整数"}
        payment = self.combat.pay_numeric_cost(
            player, "流血", 5 * x,
            cost_share_target_ref=params.get("cost_share_target_ref", ""),
            cost_context={"timing": "player_action", "source": "鲜血之翼", "source_type": "relic", "tags": {"active_payment"}})
        player.add_status(StatusEffect(name="飞行", value=x, remaining_rounds=x, source="鲜血之翼"))
        return {"success": True, "action": "鲜血之翼",
                "result": {"cost": payment, "bled": 5 * x,
                           "flying_rounds": x, "hp": player.current_hp}}

    def _action_enslave_as_chizu(self, params: dict) -> dict:
        """血族尖牙：代价衰老20，使生命低于自身的一个[目标]转化为听命于你的赤族"""
        if "血族尖牙" not in self.state.first_embrace_traits:
            return {"success": False, "error": "没有血族尖牙"}
        player = self.state.player
        target = self.combat._combat_entity_refs().get(params.get("target_ref", ""))
        if not player or target is None or target not in self.state.enemies:
            return {"success": False, "error": "target_ref不是存活敌方目标"}
        if target.current_hp >= player.current_hp:
            return {"success": False, "error": "目标当前生命必须低于自身才能被转化"}
        payment = self.combat.pay_numeric_cost(
            player, "衰老", 20,
            cost_share_target_ref=params.get("cost_share_target_ref", ""),
            cost_context={"timing": "player_action", "source": "血族尖牙", "source_type": "relic", "tags": {"active_payment"}})
        target.entity_type = "赤族"
        target.is_chizu_of = player.name
        target.is_deployed = True
        self.state.enemies.remove(target)
        self.state.friends.append(target)
        self.state.chizu_names.append(target.name)
        return {"success": True, "action": "血族尖牙",
                "result": {"cost": payment, "enslaved": target.name,
                           "chizu_names": list(self.state.chizu_names)}}

    def _action_use_truth_eye(self, params: dict) -> dict:
        """真理眼：代价冷却2(按战斗场数计)，使一个[目标]必须言明真理，否则无法开口；真伪由DM裁定"""
        if "真理眼" not in self.state.first_embrace_traits:
            return {"success": False, "error": "没有真理眼"}
        if self.state.truth_eye_cooldown > 0:
            return {"success": False, "error": f"真理眼冷却中，还需{self.state.truth_eye_cooldown}场战斗"}
        target = self.combat._combat_entity_refs().get(params.get("target_ref", ""))
        statement = params.get("statement", "")
        if target is None or not statement:
            return {"success": False, "error": "必须指定合法target_ref与statement"}
        self.state.truth_eye_cooldown = 2
        interrupt = Interrupt(
            interrupt_type=InterruptType.CUSTOM,
            context={"ability": "真理眼", "target": target.name, "statement": statement},
            description=f"对{target.name}发动【真理眼】，要求其就以下内容言明真理，否则无法开口：\n{statement}\n"
                        f"请DM裁定对方的回应是否属实。",
            options=[{"id": "truth", "label": "属实", "description": "按DM裁定的真实内容生效"},
                     {"id": "silence", "label": "无法开口", "description": "对方拒绝或无法说出真话，只能沉默"}],
            state_snapshot=self.state.to_dict(),
        )
        self._pending_interrupts.append(interrupt)
        return {"success": True, "action": "真理眼",
                "result": {"interrupt": interrupt.to_dict(), "cooldown_battles": 2}}

    def _action_blood_feast(self, params: dict) -> dict:
        """血食：可使一名听命于你的赤族[命零]，自身获得等同于该赤族当前生命的[回复]"""
        if "血食" not in self.state.first_embrace_traits:
            return {"success": False, "error": "没有血食"}
        player = self.state.player
        chizu = self.combat._combat_entity_refs().get(params.get("target_ref", ""))
        if (not player or chizu is None or chizu not in self.state.friends
                or chizu.entity_type != "赤族" or not chizu.is_alive):
            return {"success": False, "error": "target_ref不是存活赤族"}
        amount = chizu.current_hp
        feast_ctx = {
            "timing": self.state.combat_subphase or self.state.phase, "source": "血食",
            "source_type": "relic", "actor": player, "target": chizu, "owner": player,
            "mechanic": "death", "subtype": "blood_feast", "amount": amount,
            "tags": {"relic", "sacrifice"},
        }
        chizu.current_hp = 0
        # 修复：此前直接写 is_alive=False，赤族之死不产生 _death_ctx、不触发任何[命零]效果。
        self.combat._check_hp_zero_death(chizu, ctx=feast_ctx)
        heal_detail = self.state.apply_heal(player, amount, ctx={
            "timing": self.state.combat_subphase or self.state.phase, "source": "血食", "source_type": "relic",
            "actor": player, "target": player, "mechanic": "heal", "subtype": "blood_feast",
            "amount": amount, "tags": {"relic"},
        })
        return {"success": True, "action": "血食",
                "result": {"sacrificed": chizu.name, "healed": heal_detail["actual_heal"], "player_hp": player.current_hp}}

    # ==================== 终音法器：可主动发动的具体效果 ====================

    def _action_use_black_card(self, params: dict) -> dict:
        """黑金名片(罪孽都市终音)：[战始]可使所有敌方[目标][血限]减半，付出等量[碎片](允许负债，负债≤50)"""
        if "黑金名片" not in self.state.artifacts_owned:
            return {"success": False, "error": "没有黑金名片"}
        if self.state.event_modifiers.get("black_card_used_battle") == self.state.current_battle:
            return {"success": False, "error": "黑金名片本场战始已经发动"}
        enemies = self.state.get_all_enemy_side()
        if not enemies:
            return {"success": False, "error": "没有可生效的敌方目标"}
        total_cost = sum(math.ceil(e.blood_limit / 2) for e in enemies)
        if self.state.shards - total_cost < -50:
            return {"success": False, "error": f"负债不能超过50(需要付出{total_cost}，当前{self.state.shards})"}
        halved = []
        for e in enemies:
            half = math.ceil(e.blood_limit / 2)
            self.state.apply_scoped_delta(
                e, "blood_limit", -half,
                scope=EffectScope.BATTLE.value, polarity=EffectPolarity.DEBUFF.value,
                source="黑金名片")
            e.current_hp = min(e.current_hp, e.blood_limit)
            halved.append({"name": e.name, "new_blood_limit": e.blood_limit})
        self.state.shards -= total_cost
        self.state.event_modifiers["black_card_used_battle"] = self.state.current_battle
        self.combat._on_cost_paid(self.state.player)
        return {"success": True, "action": "黑金名片",
                "result": {"cost": total_cost, "shards": self.state.shards, "halved": halved}}

    def _action_use_crime_vault(self, params: dict) -> dict:
        """罪业金库(罪孽都市终音)：[回始]可消耗X点[碎片](X≤2%当前碎片)，获得2X点格挡"""
        if "罪业金库" not in self.state.artifacts_owned:
            return {"success": False, "error": "没有罪业金库"}
        player = self.state.player
        if not player:
            return {"success": False, "error": "没有玩家"}
        target_round = self.state.current_round + 1
        if self.state.event_modifiers.get("crime_vault_used_round") == target_round:
            return {"success": False, "error": "罪业金库本回始已经发动"}
        x = params.get("x", 0)
        cap = math.floor(self.state.shards * 0.02)
        if not isinstance(x, int) or x < 1 or x > cap:
            return {"success": False, "error": f"X必须是1~{cap}(当前碎片{self.state.shards}的2%)之间的整数"}
        self.state.shards -= x
        player.gain_shield(2 * x)
        self.state.event_modifiers["crime_vault_used_round"] = target_round
        return {"success": True, "action": "罪业金库",
                "result": {"spent": x, "shield_gained": 2 * x, "shards": self.state.shards, "shield": player.shield}}

    def _action_fire_godfather_revolver(self, params: dict) -> dict:
        """教父左轮(罪孽都市终音)：耐久6/6永不消耗(仅按场次回满)，对[目标]打出30%自身[血限]×本场使用次数的必中伤害"""
        if "教父左轮" not in self.state.artifacts_owned:
            return {"success": False, "error": "没有教父左轮"}
        gun = next((c for c in self.state.consumables if c.name == "教父左轮" and c.kind == "artifact_weapon"), None)
        if gun is None or gun.current_uses <= 0:
            return {"success": False, "error": "教父左轮本场弹药已耗尽，需等待下一场[战终]回满"}
        player = self.state.player
        target = self.combat._combat_entity_refs().get(params.get("target_ref", ""))
        if not player or target is None:
            return {"success": False, "error": "target_ref不是当前合法目标"}
        self.state.godfather_revolver_uses += 1
        gun.current_uses -= 1
        damage = math.ceil(player.blood_limit * 0.3) * self.state.godfather_revolver_uses
        dmg = self.combat._apply_hostile_damage(target, damage, "必中", player, ctx={
            "timing": self.state.combat_subphase or self.state.phase, "source": "教父左轮", "source_type": "artifact",
            "actor": player, "target": target, "mechanic": "damage", "subtype": "artifact",
            "amount": damage, "tags": {"artifact", "must_hit"},
        })
        return {"success": True, "action": "教父左轮",
                "result": {"target": target.name, "damage": damage,
                           "uses_this_battle": self.state.godfather_revolver_uses,
                           "ammo_remaining": gun.current_uses, **dmg}}

    def _action_select_shared_dragon_heart(self, params: dict) -> dict:
        """共心环(龙心谷终音)：[战始]选定自身拥有的一枚【××龙心】类型，本场自身/朋友/员工均可用它抵消同类型代价"""
        if "共心环" not in self.state.artifacts_owned:
            return {"success": False, "error": "没有共心环"}
        if self.state.shared_dragon_heart_type:
            return {"success": False, "error": "共心环本场战始已经选择龙心"}
        heart_type = params.get("dragon_heart_type", "")
        heart = next((c for c in self.state.consumables
                      if c.kind == "dragon_heart" and c.dragon_heart_type == heart_type), None)
        if heart is None:
            return {"success": False, "error": f"自身没有持有{heart_type}龙心"}
        self.state.shared_dragon_heart_type = heart_type
        return {"success": True, "action": "共心环",
                "result": {"shared_dragon_heart_type": heart_type}}

    def _action_declare_fuyuebei_toll(self, params: dict) -> dict:
        """负岳碑(龙心谷终音)：预先声明"下次该[朋友]/[员工]即将撤退时，改为玩家流血20取消撤退与本次伤害" """
        if "负岳碑" not in self.state.artifacts_owned:
            return {"success": False, "error": "没有负岳碑"}
        ally = self.combat._combat_entity_refs().get(params.get("target_ref", ""))
        if ally is None or ally not in self.state.friends + self.state.employees:
            return {"success": False, "error": "target_ref不是朋友或员工"}
        target_ref = params["target_ref"]
        cost_share_ref = params.get("cost_share_target_ref", "")
        self.combat.validate_numeric_cost(
            self.state.player, "流血", 20, cost_share_ref)
        if target_ref not in self.state.fuyuebei_declared:
            self.state.fuyuebei_declared.append(target_ref)
        self.state.event_modifiers.setdefault("fuyuebei_cost_share_refs", {})[target_ref] = cost_share_ref
        return {"success": True, "action": "负岳碑·预声明保护",
                "result": {"protected": ally.name, "declared": list(self.state.fuyuebei_declared)}}

    # ==================== 真龙之心：龙性资源与8种龙族遗物 ====================

    DRAGON_NATURE_RATE = {"衰老": 2, "枯竭": 6, "萎缩": 12}  # 1点该类型代价 = N点龙性
    DRAGON_TRAITS = ["龙族血脉", "龙威", "龙族利爪", "龙息", "震岳龙躯", "吞骸龙胃", "断尾求生", "烬翼"]
    # 龙族项目以【遗物】形式授予，此表提供其效果文本（与《物品索引》一致）
    DRAGON_TRAIT_EFFECTS = {
        "龙族血脉": "对怪物造成伤害后，直接使其［命零］；对非怪物造成伤害翻倍",
        "龙威": "所有敌方必须优先选择自身为[目标]",
        "龙族利爪": "你的攻击力等同当前法力×2",
        "龙息": "所有敌方[目标]行动前，受到10×当前回合数的必中伤害",
        "震岳龙躯": "消耗6X点龙性，自身受到超出15点的所有伤害无效。持续X",
        "吞骸龙胃": "任意怪物［命零］后，可将其吞噬：自身获得[回复12]，并选择一枚【××龙心】，使其当前耐久+6",
        "断尾求生": "自身即将［命零］时，可销毁一件本场获得的其他龙族遗物，抵消本次使自身［命零］的伤害",
        "烬翼": "［回始］，可消耗3X点龙性，获得【飞行X】",
    }
    # 龙威通过prepare_monster_phase收窄合法目标列表实现；resolve只接受prepare列出的目标。

    def _action_pay_for_dragon_nature(self, params: dict) -> dict:
        """真龙之心：支付衰老/枯竭/萎缩代价换取龙性（6X衰老=2X枯竭=X萎缩=12X龙性）"""
        if "真龙之心" not in self.state.artifacts_owned:
            return {"success": False, "error": "没有真龙之心"}
        cost_type = params.get("cost_type", "")
        x = params.get("x", 0)
        if cost_type not in self.DRAGON_NATURE_RATE or not isinstance(x, int) or x < 1:
            return {"success": False, "error": "cost_type必须是衰老/枯竭/萎缩之一，x必须是正整数"}
        player = self.state.player
        if not player:
            return {"success": False, "error": "没有玩家"}
        payment = self.combat.pay_numeric_cost(
            player, cost_type, x,
            cost_share_target_ref=params.get("cost_share_target_ref", ""),
            cost_context={"timing": "pre_battle_action", "source": "真龙之心", "source_type": "artifact", "tags": {"active_payment"}})
        gained = x * self.DRAGON_NATURE_RATE[cost_type]
        self.state.dragon_nature += gained
        return {"success": True, "action": "真龙之心·换取龙性",
                "result": {"paid": f"{cost_type}{x}", "cost": payment,
                           "dragon_nature_gained": gained,
                           "dragon_nature": self.state.dragon_nature}}

    def _action_unlock_dragon_trait(self, params: dict) -> dict:
        """真龙之心：每消耗12龙性，获得1件未持有的龙族遗物"""
        if "真龙之心" not in self.state.artifacts_owned:
            return {"success": False, "error": "没有真龙之心"}
        trait = params.get("trait", "")
        if trait not in self.DRAGON_TRAITS:
            return {"success": False, "error": f"trait必须是{self.DRAGON_TRAITS}之一"}
        if trait in self.state.dragon_traits:
            return {"success": False, "error": f"已持有{trait}，不能重复获得"}
        if self.state.dragon_nature < 12:
            return {"success": False, "error": f"龙性不足，需要12，当前{self.state.dragon_nature}"}
        self.state.dragon_nature -= 12
        self.state.grant_relic(trait, self.DRAGON_TRAIT_EFFECTS.get(trait, ""), tag="龙族")
        # 【龙族利爪】2026-09-17 用户令改版：效果改为「攻击力 = 当前法力×2」，
        # 不再写死初始 3 攻次 / 1 攻力——那两行直接覆盖玩家遗留字段，
        # 而 models.py 的写穿对轮回者不生效（旧效果因此对玩家完全无效），
        # 一旦写穿还会把玩家真实的[速限]/[法限]直接冲掉（见 models.py 注释）。
        # 新效果走状态层，由 BATTLE_START 机制【龙族利爪】挂状态、
        # models.py::effective_attack_power 读取。
        return {"success": True, "action": "真龙之心·获得龙族遗物",
                "result": {"trait": trait, "dragon_nature_remaining": self.state.dragon_nature,
                           "dragon_traits": list(self.state.dragon_traits)}}

    def _action_activate_dragon_body(self, params: dict) -> dict:
        """震岳龙躯：消耗6X点龙性，自身受到超出15点的所有伤害无效，持续X回合"""
        if "震岳龙躯" not in self.state.dragon_traits:
            return {"success": False, "error": "没有震岳龙躯"}
        x = params.get("x", 0)
        if not isinstance(x, int) or x < 1:
            return {"success": False, "error": "x必须是正整数"}
        cost = 6 * x
        if self.state.dragon_nature < cost:
            return {"success": False, "error": f"龙性不足，需要{cost}，当前{self.state.dragon_nature}"}
        self.state.dragon_nature -= cost
        self.state.dragon_body_shield_rounds = x
        return {"success": True, "action": "震岳龙躯",
                "result": {"rounds": x, "dragon_nature": self.state.dragon_nature}}

    def _action_devour_monster(self, params: dict) -> dict:
        """吞骸龙胃：任意怪物[命零]后可将其吞噬，自身获得回复12，并选择一枚龙心使其耐久+6"""
        if "吞骸龙胃" not in self.state.dragon_traits:
            return {"success": False, "error": "没有吞骸龙胃"}
        monster_ref = params.get("monster_ref", "")
        monster = None
        if isinstance(monster_ref, str) and monster_ref.startswith("enemy:"):
            try:
                index = int(monster_ref.split(":", 1)[1])
                monster = self.state.enemies[index]
            except (ValueError, IndexError):
                monster = None
        if monster is None or monster.is_alive:
            return {"success": False, "error": "monster_ref不是已命零怪物"}
        player = self.state.player
        heal_detail = self.state.apply_heal(player, 12, ctx={
            "timing": self.state.combat_subphase or self.state.phase, "source": "吞骸龙胃", "source_type": "relic",
            "actor": player, "target": player, "mechanic": "heal", "subtype": "devour_monster",
            "amount": 12, "tags": {"relic"},
        })
        heart_name = params.get("dragon_heart", "")
        heart = next((c for c in self.state.consumables if c.name == heart_name and c.kind == "dragon_heart"), None)
        if heart is not None:
            heart.current_uses += 6
            heart.max_uses += 6
        return {"success": True, "action": "吞骸龙胃",
                "result": {"healed": heal_detail["actual_heal"],
                           "dragon_heart_boosted": heart_name if heart is not None else None}}

    def _action_declare_tail_sacrifice(self, params: dict) -> dict:
        """断尾求生：预先声明本次即将命零时，愿意移除哪一种本场获得的其他龙族遗物来抵消伤害"""
        if "断尾求生" not in self.state.dragon_traits:
            return {"success": False, "error": "没有断尾求生"}
        trait = params.get("trait", "")
        others = [t for t in self.state.dragon_traits if t != "断尾求生"]
        if trait not in others:
            return {"success": False, "error": f"trait必须是本场已获得的其他龙族遗物之一: {others}"}
        self.state.dragon_tail_sacrifice_declared = trait
        return {"success": True, "action": "断尾求生·预声明",
                "result": {"declared_sacrifice": trait}}

    def _action_use_dragon_wings(self, params: dict) -> dict:
        """烬翼：[回始]可消耗3X点龙性，获得飞行X"""
        if "烬翼" not in self.state.dragon_traits:
            return {"success": False, "error": "没有烬翼"}
        target_round = self.state.current_round + 1
        if self.state.event_modifiers.get("dragon_wings_used_round") == target_round:
            return {"success": False, "error": "烬翼本回始已经发动"}
        x = params.get("x", 0)
        if not isinstance(x, int) or x < 1:
            return {"success": False, "error": "x必须是正整数"}
        cost = 3 * x
        if self.state.dragon_nature < cost:
            return {"success": False, "error": f"龙性不足，需要{cost}，当前{self.state.dragon_nature}"}
        self.state.dragon_nature -= cost
        self.state.event_modifiers["dragon_wings_used_round"] = target_round
        self.state.player.add_status(StatusEffect(name="飞行", value=x, remaining_rounds=x, source="烬翼"))
        return {"success": True, "action": "烬翼",
                "result": {"flying_rounds": x, "dragon_nature": self.state.dragon_nature}}

    def _action_battle_end(self, params: dict) -> dict:
        """战终；仍有未移出的存活敌人时不得跳过战斗直接结算（统一判定见 GameState.battle_won）。"""
        world_battle = bool(getattr(self.state, "world_encounter_id", ""))
        living = [e.name for e in self.state.active_enemies()]
        escaping = self.state.event_modifiers.pop("escape_at_battle_end", False)
        if living and not escaping:
            return {"success": False, "error": f"仍有存活敌人，不能结算战终: {living}"}
        queued = list(getattr(self.state, "monster_reinforcements", []) or [])
        if queued and not escaping:
            return {"success": False, "error": f"仍有{len(queued)}只怪物增援未进场，不能结算战终: {[m.get('name', '?') for m in queued]}"}
        delayed = list(getattr(self.state, "delayed_monster_reentries", []) or [])
        if delayed and not escaping:
            return {"success": False, "error": f"仍有{len(delayed)}个单位处于【封印】暂离，不能结算战终: {[e['monster'].name for e in delayed]}"}
        if escaping:
            for enemy in self.state.enemies:
                if enemy.is_alive:
                    leave_ctx = make_context(
                        timing="battle_end", source="绝息淤泥", source_type="consumable",
                        target=enemy, mechanic="leave", subtype="escape",
                        amount=0, tags={"leave", "no_shards"},
                    )
                    enemy._leave_ctx = leave_ctx.to_dict()
                    enemy.depart_battle("逃跑")
        # 员工经济系统·工资结算门槛：先按"存活+已部署+非还债"员工计算工资写入待决列表；
        # 任何一名待决(值不为None，代表尚未pay/refuse)即阻塞后续战终结算。
        self._compute_pending_wages()
        still_pending = {k: v for k, v in self.state.pending_wage_decisions.items() if v is not None}
        if still_pending:
            return {
                "success": True,
                "action": "战终工资待决",
                "completed": False,
                "instruction": "请先为以下员工逐个调用 pay_employee_wage(name, decision=pay/refuse)，再重新调用battle_end",
                "pending_wage_decisions": still_pending,
            }
        self.state.pending_wage_decisions = {}
        # 死亡员工也必须先参与统一战终清理与作用域回滚，再从名单移除。
        departed_employees = [e for e in self.state.employees
                              if not e.is_alive and not e.is_debt_bound]

        relic_end = self.combat.process_relics(TriggerTiming.BATTLE_END)
        # 全局法术【战终】：必须在本函数下方"清除局内状态/法力相关计数"之前
        # 结算，否则法术依赖的道纹/法力/状态在校验时已经被清空，永远无法
        # 满足条件（与遗物战终结算 relic_end 同一时序原则：先结算再清理）。
        spell_logs = self._resolve_global_trigger_spells_for_action(
            TriggerTiming.BATTLE_END.value, params)
        # 碎片奖励计算（雕塑/癌变/还债/永久离场的怪物不视为击杀；【封印】暂离后回场，最终命零时正常产出碎片）
        # 奖励公式用的是[战始][血限]快照(battle_start_blood_limit)，不是当前血限(增殖等会改变当前血限)
        shard_reward = 0
        removed = []
        death_rewards = []
        for monster in self.state.enemies:
            if monster.is_departed or monster.is_sculptured or monster.removed_without_kill \
                    or monster.is_proliferated or monster.is_debt_bound:
                way = (monster.departure_reason or
                       ("雕塑" if monster.is_sculptured else
                        "救赎" if getattr(monster, "_redeemed", False) else
                        "癌变" if monster.is_proliferated else
                        "还债" if monster.is_debt_bound else "永久离场"))
                leave_parent = normalize_context(getattr(monster, "_leave_ctx", None))
                leave_ctx = make_context(
                    timing="battle_end", source=way, source_type="system",
                    target=monster, mechanic="combat_resolution", subtype="leave_no_shards",
                    amount=0, tags={"leave", "no_shards"},
                    parent_event_id=leave_parent.event_id if leave_parent else None,
                )
                removed.append({"name": monster.name, "way": way, "ctx": leave_ctx.to_dict()})
                continue
            if not monster.is_alive:
                reward = math.ceil(monster.battle_start_blood_limit * 0.02) + len(monster.dao_wen) * 5
                shard_reward += reward
                death_parent = normalize_context(getattr(monster, "_death_ctx", None))
                reward_ctx = make_context(
                    timing="battle_end", source="命零碎片奖励", source_type="system",
                    target=monster, mechanic="combat_resolution", subtype="death_shard_reward",
                    amount=reward, tags={"death", "shards"},
                    parent_event_id=death_parent.event_id if death_parent else None,
                )
                death_rewards.append({"name": monster.name, "reward": reward, "ctx": reward_ctx.to_dict()})

        # 2026-09-16 裁定（清单 B4）：【碎片】命零公式以正文为准，禁止改写。
        # shard_reward 恒等于 Σ(⌈战始血限×2%⌉ + 道纹数×5)，任何副本/事件增益
        # 都不得乘进或并入这一笔；事件收益改为下面单独列项的「战终奖金」，
        # 名目与来源可逐条核对。
        modifiers = self.state.event_modifiers
        event_bonuses: list[dict] = []
        if modifiers.pop("arena_double_loot", False):
            # 地下角斗场「赢了全拿」：不改命零公式，另发等额奖金。
            event_bonuses.append({"name": "角斗场奖金", "amount": shard_reward,
                                  "source": "地下角斗场"})
        bounty = modifiers.pop("bounty_reward", 0)
        if bounty:
            event_bonuses.append({"name": "悬赏金", "amount": bounty,
                                  "source": "通缉悬赏榜"})
        if modifiers.pop("arena_bet_three_rounds", False) and self.state.current_round <= 3:
            event_bonuses.append({"name": "三回合彩头", "amount": 45,
                                  "source": "地下角斗场"})
        bonus_total = sum(b["amount"] for b in event_bonuses)
        self.state.shards += shard_reward + bonus_total
        modifiers.pop("scarlet_fruit_active", False)  # 2026-10-14：旧战终血限+2 已随重做废弃（效果移交道纹【增殖】）
        # 2026-10-14：【增殖】战终待决统一结算（目标死亡/持【不朽之躯】落空）。
        zengzhi_settled = []
        for entry in modifiers.pop("zengzhi_pending", []) or []:
            tgt = self.combat._combat_entity_refs().get(entry.get("target_ref", "")) if isinstance(entry, dict) else None
            if tgt is None or not tgt.is_alive:
                continue
            if self.state.side_has(tgt, "不朽之躯"):
                zengzhi_settled.append({"target": tgt.name, "increase": 0, "blocked_by": "不朽之躯"})
                continue
            amt = entry.get("amount", 0)
            tgt.blood_limit += amt
            zengzhi_settled.append({"target": tgt.name, "increase": amt})
        modifiers.pop("pale_flower_active", False)   # 2026-10-11：旧战终精力+1 已随重做废弃
        modifiers.pop("brand_nail_target_ref", None)
        modifiers.pop("huifeng_target_ref", None)
        modifiers.pop("huifeng_target_ref_opponent", None)

        # 【自愈X】战终结算（2026-10-14 用户令通用化：无限肉块 80% 迁入时机表）。
        zi_yu_logs = apply_zi_yu_trigger(self.state.consumables, ZIYU_TRIGGER_BATTLE_END)

        # [战终]对所有角色统一清除局内回复、格挡与状态；不得只清轮回者。
        all_characters = (([self.state.player] if self.state.player else [])
                          + self.state.friends + self.state.employees
                          + self.state.temp_friends + self.state.enemies)
        persistent_scopes = {
            EffectScope.COST.value, EffectScope.RUN.value, EffectScope.PERMANENT.value,
        }
        runtime_counter_attrs = (
            "_jisu_dodges", "_dongcha_pending", "_bizhong_left", "_nilin",
            "_beifu_left", "_beifu_target",   # 2026-10-11：_jiahuo_* 随【嫁祸】删除
            "_death_triggers_emitted", "_shouyedeng_granted", "_huifeng_target_ref",
        )
        for entity in all_characters:
            entity.clear_shield()
            if entity.healed_this_battle > 0:
                entity.current_hp = (max(1, entity.current_hp - entity.healed_this_battle)
                                     if entity.is_alive else 0)
            entity.healed_this_battle = 0
            # 癌变累计回复量已裁定为局内减益追踪，每场必须归零。
            entity.total_healed = 0
            entity.status_effects = [s for s in entity.status_effects
                                     if s.scope in persistent_scopes]
            # 账本清空一律走唯一入口（引擎/机制两层的口径都从 ledger 模块取）
            from .mechanisms.ledger import clear_ledger
            clear_ledger(entity, "清算")   # 2026-10-13 删【逼债】，仅剩清算账本
            entity.current_speed = entity.speed_limit
            # DM裁定 2026-09-09：法力与速度同口径——只在[战终]复原（[回始]不再回填）。
            if entity.entity_type == "轮回者":
                entity.current_mana = entity.mana_limit
            entity.is_flying = False
            entity.hp_lost_this_round = 0
            entity.actions_used_this_round = 0
            entity.mana_inflicted_this_round = 0
            entity.damage_dealt_this_round = 0
            entity.no_action_rounds = 0
            entity.no_damage_rounds = 0
            for attr in runtime_counter_attrs:
                if hasattr(entity, attr):
                    delattr(entity, attr)
            if hasattr(entity, "_bianxing_original"):
                entity.attack_power, entity.attack_count = entity._bianxing_original
                delattr(entity, "_bianxing_original")
        scoped_rollbacks = self.state.rollback_scoped_effects(EffectScope.BATTLE.value)
        self.state.rollback_scoped_effects(EffectScope.ROUND.value)
        # 生命周期（Part 6）：battle 作用域的法术绑定随战终失效。
        # 必须在【战终】全局法术结算之后执行（上面 spell_logs），
        # 否则战终法术自己要用的 battle 法术会先被清掉。
        spell_bindings_removed = self.combat.clear_battle_scoped_spells()
        self.state.sealed_relics = {}
        self.state.fuyuebei_declared = []
        modifiers.pop("fuyuebei_cost_share_refs", None)

        # 体外心脏：临时翻倍的血限[战终]还原为基准值，当前生命同步封顶。
        if (self.state.player and "体外心脏" in self.state.artifacts_owned
                and self._artifact_base_blood_limit > 0):
            self.state.player.blood_limit = self._artifact_base_blood_limit
            # 2026-10-14：【猩红果实】持有者豁免血限压顶（生命可超血限）。
            if not self.state.hp_overflow_exempt(self.state.player):
                self.state.player.current_hp = min(self.state.player.current_hp, self.state.player.blood_limit)
            self._artifact_base_blood_limit = 0

        # 真理眼冷却：每场[战终]-1
        if self.state.truth_eye_cooldown > 0:
            self.state.truth_eye_cooldown -= 1

        # 道纹【冷却X】：规则正文「[战终]后已完成战斗场数+1，达到Y时才能再次使用」。
        # 对所有持有道纹的角色统一递减，归零即恢复可用。
        for _ent in ([self.state.player] if self.state.player else []) \
                + self.state.friends + self.state.employees:
            for _inst in _ent.dao_wen.values():
                if _inst.cooldown_remaining > 0:
                    _inst.cooldown_remaining -= 1

        # 员工经济系统·死亡离队：完成统一清理后计入黑名单并移除，避免遗漏回滚或重复计数。
        for emp in departed_employees:
            if emp in self.state.employees:
                self.state.employees.remove(emp)
                self._blacklist_departure("死亡离队")

        # 临时朋友消失
        self.state.temp_friends.clear()

        # 出战支援：每场战斗单独部署，战终后存活员工回到"待命"状态，下一场需重新派遣
        # （先记录本场参战者供成长判定；若重置后再判 is_deployed，则参战者全被误判为待命而永不成长）
        _battled_ids = {id(emp) for emp in self.state.employees
                        if emp.is_alive and not emp.is_debt_bound and emp.is_deployed}

        # 2026-09-17 用户令：[员工]**出场**并**存活**满 EMPLOYEE_PROMOTION_BATTLES
        # 场战斗后转为[朋友]。口径与 _battled_ids 一致——本场确实参战(is_deployed)
        # 且[战终]仍存活；待命未上场不计，阵亡者已被上面的 departed_employees 移除。
        # 还债员工(is_debt_bound)不晋升：其身份是债务约束而非雇佣关系。
        promoted = []
        for emp in list(self.state.employees):
            if id(emp) not in _battled_ids:
                continue
            emp.survived_battles_as_employee = getattr(
                emp, "survived_battles_as_employee", 0) + 1
            if emp.survived_battles_as_employee < Entity.EMPLOYEE_PROMOTION_BATTLES:
                continue
            self.state.employees.remove(emp)
            emp.entity_type = "朋友"
            emp.is_deployed = True          # [朋友]无待命概念，直接计入战场
            self.state.friends.append(emp)
            promoted.append({"name": emp.name,
                             "battles": emp.survived_battles_as_employee})

        for emp in self.state.employees:
            if emp.is_alive and not emp.is_debt_bound:
                emp.is_deployed = False
                emp.deployed_at_round = 0

        # 撤退：仅"无法再次加入本场战斗"，战终后重置，可正常参加下一场
        for ally in self.state.friends + self.state.employees + self.state.temp_friends:
            ally.has_retreated = False

        # 伙伴成长：每存活过一场战斗的[朋友]/[员工]，攻击次数+1（上限9）；达到9后改为攻击力+1。
        # 未部署的员工不参战，不成长；已命零/已离队的不成长。
        grown = []
        for ally in self.state.friends + self.state.employees:
            if not ally.is_alive or ally.is_debt_bound:
                continue
            if ally.entity_type == "员工" and id(ally) not in _battled_ids and ally not in self.state.friends:
                # 待命员工未上场，不累计战斗历练（以重置前记录的参战集合为准）
                continue
            if ally.attack_count < 9:
                ally.attack_count += 1
                grown.append(f"{ally.name}:攻击次数{ally.attack_count}")
            else:
                ally.attack_power += 1
                grown.append(f"{ally.name}:攻击力{ally.attack_power}")

        # 恢复精力（无尽模式下基础值逐轮递减）。
        # 2026-10-11：【苍白之花】重做为「当前法力可超出[法限]」的被动，战终精力+1 已废止。
        self.state.energy = self.state.energy_budget

        # 普通战斗清空敌人；开放世界遭遇则把存活参与者放回地图，并恢复未参战的敌方单位。
        if world_battle:
            world = self._world_from_state()
            encounter_id = self.state.world_encounter_id
            encounter = world.encounters.get(encounter_id)
            participants = list(getattr(self.state, "world_encounter_entities", []) or [])
            participant_by_id = {entity.runtime_id: entity for entity in participants}
            reserve = list(getattr(self.state, "world_enemy_reserve", []) or [])
            player_id = getattr(self.state.player, "runtime_id", "") if self.state.player else ""
            for actor_id in (encounter.participants if encounter else []):
                actor = world.actors.get(actor_id)
                if actor is None:
                    continue
                if actor_id == player_id and self.state.player is not None:
                    actor.alive = self.state.player.is_alive
                elif actor_id in participant_by_id:
                    entity = participant_by_id[actor_id]
                    actor.alive = bool(entity.is_alive and not entity.is_departed)
                    if entity.world_x is not None and entity.world_y is not None:
                        actor.x, actor.y = int(entity.world_x), int(entity.world_y)
                else:
                    actor.alive = False
            if encounter is not None and encounter.active:
                outcome = "player_dead" if not self.state.player or not self.state.player.is_alive else "resolved"
                world.finish_encounter(encounter_id, outcome)
            self.state.enemies = [entity for entity in reserve if entity.is_alive]
            self.state.enemies.extend(
                entity for entity in participants if entity.is_alive and not entity.is_departed)
            self.state.world_enemy_reserve = []
            self.state.world_encounter_entities = []
            self.state.world_encounter_id = ""
            self.state.world_state = world.to_dict()
        else:
            self.state.enemies.clear()
        self.state.delayed_monster_reentries = []

        self.state.phase = "pre_battle"

        rebellion_check = self.combat.check_employee_rebellion()
        if rebellion_check.get("rebellion"):
            self.state.rebellion_active = True

        # 捕获战终结果数值，因【最终的冠冕】可能在下面把self.state整个替换为新轮回者的空白状态
        battle_end_result = {
            "shard_reward": shard_reward,
            # 命零公式产出的碎片（正文口径，不含任何副本/事件增益）
            "event_bonuses": event_bonuses,       # 事件奖金，单独列项，不并入命零公式
            "event_bonus_shards": bonus_total,
            "total_shards": self.state.shards,
            "energy_restored": self.state.energy_budget,
            "cleared_temp_friends": True,
            "ally_growth": grown,
            "removed_via_alt_path": removed,
            "death_shard_rewards": death_rewards,
            "relic_end_logs": relic_end,
            "zengzhi_settled": zengzhi_settled,
            "zi_yu_logs": zi_yu_logs,
            "spell_logs": spell_logs,
            "scoped_effects_rolled_back": scoped_rollbacks,
            "spell_bindings_removed": spell_bindings_removed,
            "employee_rebellion": rebellion_check,
            "employee_promotions": promoted,   # [员工]出场存活满3场 → 转为[朋友]
            "player_dead": (not self.state.player.is_alive) if self.state.player else False,
        }

        # 最终的冠冕：完成第7场后自动触发，不消耗精力、不经玩家选择
        if not world_battle and self.state.current_battle == 7:
            battle_end_result["final_crown"] = self._trigger_final_crown()

        return {
            "success": True,
            "action": "战终",
            "result": battle_end_result,
        }

    # ==================== 死之传承 / 死者之书 ====================

    def _reload_death_book(self):
        self.state.death_book_legacies = self.death_book.load()

    def _replace_state_preserving_death_book_progress(self) -> None:
        """开始新轮回者时保留《死者之书》的系统记录。

        2026-10-05 用户令：癌变休整加成已废止，此处不再携带 rest_heal_bonus。"""
        wisdom = list(self.state.death_book_wisdom)
        # 死斗败者先过死之传承（reset）再走resolve_final_duel(defeat)；
        # reset若清空擂主快照，defeat就无声吞掉擂主（r14实战复现）。
        was_duel = self.state.in_final_duel
        duel_snap = dict(self.state.duel_defending_snapshot) if was_duel else {}
        duel_tier = self.state.duel_tier if was_duel else 0
        # 阶级推进与无尽模式属于跨轮回进度：换新轮回者时随文件装回，不清零。
        unlocked_tier = int(self.state.unlocked_tier)
        endless_mode = bool(self.state.endless_mode)
        endless_cycle = int(self.state.endless_cycle)
        self.state = GameState(death_book_wisdom=wisdom)
        self.state.unlocked_tier = unlocked_tier
        self.state.endless_mode = endless_mode
        self.state.endless_cycle = endless_cycle
        if endless_mode:
            # 新轮回者的局外精力同样吃无尽递减（字段默认值 3 不适用于无尽轮次）
            self.state.energy = self.state.energy_budget
        if was_duel:
            self.state.in_final_duel = True
            self.state.duel_tier = duel_tier
            self.state.duel_defending_snapshot = duel_snap
        self.combat.state = self.state
        # 事件遭遇记录属于单次轮回；新轮回不得继承已触发事件或未结算队列。
        self.event_pool.triggered.clear()
        self.event_pool.current = None
        self._reload_death_book()

    def _reset_after_death(self):
        """命零审核结束后结束本轮回，从文件装回已落盘遗言并保留永久强化。"""
        self._replace_state_preserving_death_book_progress()

    def _infer_death_cause(self, action_type: str = "") -> str:
        if self.state.last_death_cause:
            return self.state.last_death_cause
        player = self.state.player
        if player is not None and getattr(player, "is_proliferated", False):
            return "cancer"
        if (player is not None and player.can_trigger_mutation_lost()
                and player.mutation_count >= Entity.MUTATION_COLLAPSE_THRESHOLD):
            return "collapse"
        if self.state.in_final_duel or action_type == "resolve_final_duel":
            return "duel"
        if action_type == "round_end":
            return "mediocrity"
        if action_type == "consume_item":
            return "collapse"
        return "attack"

    def _queue_death_inheritance_if_needed(self, action_type: str = "") -> Optional[Interrupt]:
        """轮回者命零后只抛一次死之传承中断，草稿写入 context 待审核。"""
        if self.state.death_inheritance_queued:
            return None
        player = self.state.player
        if player is None or player.is_alive:
            return None
        last_action = self._action_history[-1] if self._action_history else None
        cause = self._infer_death_cause(action_type)
        if self.state.pending_death_draft:
            draft = self.state.pending_death_draft
        elif getattr(player, "ai_memory", None):
            # 记忆只活到当前轮回者命零这一刻。先压缩出遗言，再立即清空
            # 身世、经历、经验和人格缓存；是否真正写入仍交给死之传承审核。
            draft = validate_legacy({
                "text": condense_to_legacy(
                    player.ai_memory, cause, last_action,
                    self.state.death_book_capacity),
            }, self.state.death_book_capacity)
            clear_memory(player)
            personality_remove(self.state, player)
        else:
            draft = draft_legacy(
                self.state, cause, last_action,
                self.state.death_book_capacity)
        self.state.pending_death_draft = draft
        self.state.death_inheritance_queued = True
        interrupt = Interrupt(
            interrupt_type=InterruptType.DEATH_INHERITANCE,
            context={
                "draft": dict(draft),
                "cause": self._infer_death_cause(action_type),
                "player": player.name,
                "battle": self.state.current_battle,
                "region": self.state.current_region,
            },
            description=(
                f"{player.name}已[命零]，触发【死之传承】。\n"
                f"草稿：「{draft['text']}」（上限{self.state.death_book_capacity}字）\n"
                "请审核：通过、修改后写入、或驳回（驳回不写入死者之书）。"
            ),
            options=[
                {"id": "approve", "label": "通过", "description": "按草稿写入《死者之书》"},
                {"id": "edit", "label": "修改后写入", "description": "提交修改后的遗言再写入"},
                {"id": "reject", "label": "驳回", "description": "不写入《死者之书》，本轮回结束"},
            ],
            state_snapshot=self.state.to_dict(),
        )
        self._pending_interrupts.append(interrupt)
        return interrupt

    def _prepare_death_ruling(self, interrupt: Interrupt, ruling_data: dict) -> dict:
        action = (ruling_data or {}).get("action") or (ruling_data or {}).get("option") or ""
        action = str(action).strip().lower()
        aliases = {"通过": "approve", "修改后写入": "edit", "驳回": "reject",
                   "approve": "approve", "edit": "edit", "reject": "reject"}
        action = aliases.get(action, action)
        if action not in {"approve", "edit", "reject"}:
            raise ValueError("死之传承裁定必须是 approve / edit / reject")
        if action == "reject":
            return {"action": "reject"}
        if action == "approve":
            source = (ruling_data or {})
            if source.get("text"):
                legacy = validate_legacy(source, self.state.death_book_capacity)
            else:
                legacy = validate_legacy(
                    interrupt.context.get("draft") or self.state.pending_death_draft,
                    self.state.death_book_capacity)
            return {"action": "approve", "legacy": legacy}
        legacy = validate_legacy({
            "text": (ruling_data or {}).get("text"),
            **({"title": ruling_data["title"]} if (ruling_data or {}).get("title") else {}),
        }, self.state.death_book_capacity)
        return {"action": "edit", "legacy": legacy}

    def _commit_death_ruling(self, prepared: dict) -> dict:
        if prepared["action"] == "reject":
            self._reset_after_death()
            return {"written": False, "rejected": True,
                    "instruction": "遗言已驳回，未写入死者之书；请调用 setup_attributes 开始新的轮回者"}
        written = self.death_book.append(prepared["legacy"])
        self._reset_after_death()
        return {
            "written": True,
            "legacy": {"text": written["text"]},
            "total_legacies": len(self.state.death_book_legacies),
            "path": str(self.death_book.path),
            "instruction": "遗言已写入死者之书；请调用 setup_attributes 开始新的轮回者",
        }

    def _action_read_death_book(self, params: dict) -> dict:
        """翻阅《死者之书》：读回全部〖遗言〗与已积累的智慧条目。

        纯查询：不消耗精力、不掷骰、不改任何数值——《死者之书》与轮回者灵魂绑定
        （《故事文档》世界观·死者之书与微光者），本就随身携带，翻阅不花代价。
        在此之前遗言只有「写入」（死之传承 → `_commit_death_ruling`）没有「读取」：
        `state.death_book_legacies` 只被序列化与计数，轮回者/AI 一条也看不到。
        """
        legacies = [dict(entry) for entry in self.state.death_book_legacies]
        return {
            "success": True,
            "action": "翻阅死者之书",
            "legacies": legacies,
            "total_legacies": len(legacies),
            "wisdom": list(self.state.death_book_wisdom),
            "path": str(self.death_book.path),
            "instruction": "已读回《死者之书》遗言，可作为本轮决策的前车之鉴",
        }

    # ==================== DM裁定接口 ====================

    def submit_ruling(
        self,
        interrupt_type: str,
        ruling_text: str,
        ruling_data: dict = None,
        tags: list[str] = None
    ) -> dict:
        """
        DM提交裁定
        1. 移除待处理中断
        2. 保存到数据库
        3. 应用裁定效果
        """
        if not self._pending_interrupts:
            return {"success": False, "error": "没有待处理的中断"}

        interrupt = self._pending_interrupts[0]
        is_death = (
            interrupt.interrupt_type == InterruptType.DEATH_INHERITANCE
            or interrupt_type == InterruptType.DEATH_INHERITANCE.value
        )
        prepared = None
        if is_death:
            try:
                prepared = self._prepare_death_ruling(interrupt, ruling_data or {})
            except ValueError as exc:
                return {"success": False, "error": str(exc),
                        "instruction": "非法遗言未写入；中断仍在，请改提交合法遗言（一句话，≤20字）"}

        interrupt = self._pending_interrupts.pop(0)

        # 创建裁定记录
        ruling = DMRuling(
            interrupt_type=interrupt_type,
            context=interrupt.context,
            ruling_text=ruling_text,
            ruling_data=ruling_data or {},
            tags=tags or []
        )

        ruling_id = self.rulings_db.save_ruling(ruling)
        result = {
            "success": True,
            "action": "DM裁定",
            "ruling_id": ruling_id,
            "interrupt_type": interrupt_type,
            "ruling_text": ruling_text,
            "ruling_data": ruling_data,
            "note": "裁定已保存，下次类似场景将自动匹配"
        }
        if prepared is not None:
            result["death_book"] = self._commit_death_ruling(prepared)
        return result

    def check_precedent(self, interrupt_type: str, context: dict) -> dict:
        """
        查询是否有先例裁定
        AI在触发特殊事件前可先查询
        """
        similar = self.rulings_db.find_similar(interrupt_type, context)

        return {
            "found": len(similar) > 0,
            "count": len(similar),
            "rulings": [r.to_dict() for r in similar],
            "instruction": "如果有匹配的先例，可以直接应用；否则需要DM新裁定"
        }

    # ==================== 存档系统 ====================

    SAVE_FORMAT_VERSION = 5

    def _migrate_legacy_daowen_status_order(self) -> None:
        """迁移旧 pickle：补齐历史碎片峰值与道纹序列字段，优先从状态来源角色恢复道纹位置。"""
        state = self.state
        # 旧版 pickle 没有历史碎片峰值；只能以读档时真实余额作为可恢复的基线。
        if "shards_peak" not in getattr(state, "__dict__", {}):
            state.shards_peak = max(0, int(getattr(state, "shards", 0)))
        entities = [getattr(state, "player", None)]
        for field_name in ("friends", "employees", "temp_friends", "enemies", "dead_monsters"):
            entities.extend(getattr(state, field_name, []) or [])
        entities.extend(
            entry.get("monster") for entry in (getattr(state, "delayed_monster_reentries", []) or [])
            if isinstance(entry, dict)
        )
        unique_entities = []
        seen = set()
        for entity in entities:
            if isinstance(entity, Entity) and id(entity) not in seen:
                seen.add(id(entity))
                unique_entities.append(entity)
        # 状态可能挂在目标身上，但其排序锚点来自施加该状态的角色。
        # 例如【清算】的状态在目标身上，位置却属于施法者的道纹序列。
        entity_by_name = {entity.name: entity for entity in unique_entities}
        order_by_entity = {
            id(entity): {name: index for index, name in enumerate(entity.dao_wen)}
            for entity in unique_entities
        }
        for entity in unique_entities:
            if "shards_peak" not in getattr(entity, "__dict__", {}):
                entity.shards_peak = max(0, int(getattr(entity, "shards", 0)))
            owner_order = order_by_entity[id(entity)]
            for status in entity.status_effects:
                # 旧 pickle 反序列化不会自动填充新 dataclass 字段。
                if not hasattr(status, "daowen_order"):
                    status.daowen_order = None
                if status.daowen_order is None:
                    source_entity = entity_by_name.get(getattr(status, "source", ""))
                    source_order = order_by_entity.get(id(source_entity), {}) if source_entity else {}
                    # 能从来源角色的同名道纹确认位置时优先用来源；否则兼容退回状态持有者。
                    status.daowen_order = source_order.get(status.name, owner_order.get(status.name))

    def save_game(self, slot: str = "auto") -> dict:
        """保存可完整往返的版本化快照；只允许load_game读取本引擎生成的本地文件。"""
        snapshot = {
            "state": self.state,
            "dice": self.dice,
            "combat_runtime": self._snapshot_combat_runtime(),
            "event_triggered": set(self.event_pool.triggered),
            "event_current": self.event_pool.current,
            "pending_interrupts": self._pending_interrupts,
            "action_history": self._action_history,
            "last_result": self._last_result,
            "battle_restart_snapshot": self._battle_restart_snapshot,
        }
        encoded = base64.b64encode(pickle.dumps(snapshot, protocol=pickle.HIGHEST_PROTOCOL)).decode("ascii")
        save_data = {"format": "linji-save", "version": self.SAVE_FORMAT_VERSION,
                     "payload": encoded, "timestamp": time.time()}
        filepath = os.path.join(self.save_dir, f"save_{slot}.json")
        os.makedirs(self.save_dir, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as handle:
            json.dump(save_data, handle, ensure_ascii=False, indent=2)
        return {"success": True, "filepath": filepath, "version": self.SAVE_FORMAT_VERSION}

    def load_game(self, slot: str = "auto") -> dict:
        """原子恢复完整状态、随机源、事件池、待决项和战斗运行态。"""
        filepath = os.path.join(self.save_dir, f"save_{slot}.json")
        if not os.path.exists(filepath):
            return {"success": False, "error": f"存档不存在: {filepath}"}
        try:
            with open(filepath, "r", encoding="utf-8") as handle:
                save_data = json.load(handle)
            if save_data.get("format") != "linji-save" or save_data.get("version") != self.SAVE_FORMAT_VERSION:
                return {"success": False, "error": "不支持的存档格式或版本"}
            restored = pickle.loads(base64.b64decode(save_data["payload"].encode("ascii")))
            if not isinstance(restored.get("state"), GameState) or not isinstance(restored.get("dice"), DiceEngine):
                return {"success": False, "error": "存档内容类型无效"}
        except Exception as exc:
            return {"success": False, "error": f"存档损坏: {exc}"}

        self.state = restored["state"]
        self._migrate_legacy_daowen_status_order()
        self.dice = restored["dice"]
        self.combat.state = self.state
        self.combat.dice = self.dice
        # 机制系统：事件观察者必须重新绑定到当前引擎。
        # 跨进程读档时，pickle 恢复的观察者仍持旧引擎 id，会静默失效
        # （事件机制如焦黑发丝/洗劫·夺碎片不触发）。
        register_combat_event_observer(self.state, self.combat)
        self.event_pool.triggered = set(restored["event_triggered"])
        self.event_pool.current = restored["event_current"]
        self._pending_interrupts = restored["pending_interrupts"]
        self._action_history = restored["action_history"]
        self._last_result = restored["last_result"]
        self._battle_restart_snapshot = restored.get("battle_restart_snapshot")
        self._restore_combat_runtime(restored["combat_runtime"])
        return {"success": True, "filepath": filepath, "version": self.SAVE_FORMAT_VERSION,
                "state": self.state.to_dict()}

    def get_action_history(self) -> list[dict]:
        """获取行动历史"""
        return self._action_history

    def get_rulings_history(self) -> list[dict]:
        """获取所有DM裁定"""
        return [r.to_dict() for r in self.rulings_db.get_all_rulings()]


import math  # 修复 battle_end 中使用 math.ceil
