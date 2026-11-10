"""开局与局外行动：初始属性/残韵/副本/初始道纹、赎罪、战前小动作、医生升级、属性点赎回。

2026-11-03 从 `engine/api.py` **原样搬出**（只搬位置，不改逻辑），作为
`GameEngine` 的 mixin 挂回；`@ACTION_REGISTRY.action(...)` 声明归属，
`_dispatch_action` 查表即命中。
"""
from __future__ import annotations

import math

from ..effect_context import make_context
from ..models import Entity
from .setup import (handle_setup_attributes, handle_setup_choose_initial_daowen,
                    handle_setup_choose_region, handle_setup_choose_resonance)

from ..action_registry import ACTION_REGISTRY


class OpeningHandlersMixin:
    """宿主类：`GameEngine`。"""

    @ACTION_REGISTRY.action("setup_attributes", group="opening")
    def _action_setup_attributes(self, params: dict) -> dict:
        return handle_setup_attributes(self, params)

    @ACTION_REGISTRY.action("setup_choose_region", group="opening")
    def _action_setup_choose_region(self, params: dict) -> dict:
        return handle_setup_choose_region(self, params)

    @ACTION_REGISTRY.action("setup_choose_resonance", group="opening")
    def _action_setup_choose_resonance(self, params: dict) -> dict:
        return handle_setup_choose_resonance(self, params)

    @ACTION_REGISTRY.action("setup_choose_initial_daowen", group="opening")
    def _action_setup_choose_initial_daowen(self, params: dict) -> dict:
        return handle_setup_choose_initial_daowen(self, params)

    @ACTION_REGISTRY.action("resolve_redemption", group="opening")
    def _action_resolve_redemption(self, params: dict) -> dict:
        """救赎：接纳昏迷微光者为员工（待命，需派遣+战终工资；夺书对他们同样开放），或当场【终结】。

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

    @ACTION_REGISTRY.action("pre_battle_action", group="opening")
    def _action_pre_battle(self, params: dict) -> dict:
        """局外行动入口。开放世界中，耗时行动先推进世界，再结算收益。"""
        if getattr(self.state, "world_state", {}):
            return self._action_timed_world_pre_battle(params)
        return self._execute_pre_battle_immediate(params, world_mode=False)

    @ACTION_REGISTRY.action("upgrade_doctor", group="opening")
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

    @ACTION_REGISTRY.action("redeem_attribute_points", group="opening")
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
                             "{'blood_points': 非负整数, 'speed_points': 整数, 'mana_points': 偶数}",
                    "attribute_points": self.state.attribute_points}
        return self._redeem_attribute_points(allocations, target=params.get("target", "player"))
