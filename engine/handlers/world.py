"""开放世界地图行动：移动、等待、视野、交涉、向同伴提建议、造物指令。

2026-11-03 从 `engine/api.py` **原样搬出**（只搬位置，不改逻辑），作为
`GameEngine` 的 mixin 挂回；同时用 `@ACTION_REGISTRY.action(...)` 声明归属，
`_dispatch_action` 查表即命中，不再走 api.py 的旧 if/elif。

搬过来的私有辅助方法（`_world_from_state` / `_resolve_world_*` / `_spawn_world_crown` …）
只服务这一组行动，因此一起搬；`_entity_is_merfolk` 也在本组
（只被地图行动与视野复用）。
"""
from __future__ import annotations

import math
from typing import Optional

from ..action_registry import ACTION_REGISTRY
from ..consumables import WING_MODES, make_wing_item
from ..events import TWISTED_TOOL_LIBRARY
from ..models import Consumable, DaoWenInstance, Entity, LongJiXin, Relic
from ..npc_consumables import auto_use_consumables, tick_world_item_statuses
from ..world import (ALLY_KINDS, ALLY_SUGGESTION_RANGE, ALLY_SUGGESTIONS,
                     WorldActorState, WorldState, suggestion_acceptance_percent)
from ..yaoguai import BANNER_CAPACITY, BANNER_NAME, find_owned_yaoguai, owned_yaoguai


class WorldHandlersMixin:
    """开放世界地图行动处理器。宿主类：`GameEngine`。"""


    def _entity_is_merfolk(self, entity: Entity | None) -> bool:
        if entity is None:
            return False
        if entity.entity_type == "鲛族" or entity.has_status("鲛族"):
            return True
        return bool(self.combat._relic_active(entity, "鲛族血脉"))

    def _world_from_state(self) -> WorldState:
        if not self.state.world_state:
            raise ValueError("开放世界尚未初始化，请先执行world_initialize")
        return WorldState.from_dict(self.state.world_state)

    def _record_world_entity_outcomes(
        self, world: WorldState, *, extra_entities: list[Entity] | None = None,
    ) -> None:
        """把死亡/永久离场写入世界事实账本；同一单位的结果事件只记录一次。"""
        entities = []
        if self.state.player is not None:
            entities.append(self.state.player)
        for group_name in ("friends", "employees", "temp_friends", "enemies"):
            entities.extend(getattr(self.state, group_name, []) or [])
        if getattr(self.state, "merchant", None) is not None:
            entities.append(self.state.merchant)
        entities.extend(extra_entities or [])
        by_id = {getattr(entity, "runtime_id", ""): entity
                 for entity in entities if getattr(entity, "runtime_id", "")}
        recorded = {
            (event.get("type"), event.get("actor_id"))
            for event in world.event_log
            if event.get("type") in {"actor_died", "actor_departed"}
        }
        for actor in world.actors.values():
            entity = by_id.get(actor.actor_id)
            if entity is None:
                continue
            if entity.is_departed:
                event_type = "actor_departed"
                if (event_type, actor.actor_id) in recorded:
                    continue
                world.record(event_type, actor.actor_id, name=entity.name,
                             position=list(actor.position),
                             reason=getattr(entity, "departure_reason", "") or "permanent_departure")
                recorded.add((event_type, actor.actor_id))
            elif not entity.is_alive:
                event_type = "actor_died"
                if (event_type, actor.actor_id) in recorded:
                    continue
                death_ctx = getattr(entity, "_death_ctx", None)
                reason = (death_ctx.get("source", "") if isinstance(death_ctx, dict) else "")
                world.record(event_type, actor.actor_id, name=entity.name,
                             position=list(actor.position), reason=reason or "health_depleted")
                recorded.add((event_type, actor.actor_id))

    def _sync_world_entity_positions(self, world: WorldState) -> None:
        """将地图中的权威位置同步回实体存档字段，避免移动只存在于地图副本。"""
        from ..world_consumables import discover_consumables
        discover_consumables(world)
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
            # 实体命零/永久离场必须同步到地图单位，否则死者会继续移动或触发遭遇。
            actor.alive = bool(entity.is_alive and not entity.is_departed)
        # 蜃景只在施术者死亡时整体清除；进入战斗不影响幻象存续。
        for actor in world.actors.values():
            if actor.kind == "player" and not actor.alive:
                world._clear_illusions_for_owner(actor.actor_id, reason="caster_died")
        self._record_world_entity_outcomes(world)

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

    # ==================== 最终的冠冕（世界事件） ====================

    def _spawn_world_crown(self, world: WorldState) -> dict:
        """把【最终的冠冕】放到地图上一个随机可通行格。

        刷新点由骰子引擎在候选格里随机决定（种子可复现、有滚动记录），
        不预设"固定房间"或"必定在玩家附近"这类特例。
        """
        from ..world import CROWN_NAME

        cells = [
            (x, y)
            for y in range(world.map.height)
            for x in range(world.map.width)
            if world.map.is_passable((x, y)) and world.map.terrain_at((x, y)) != "water_current"
        ]
        if not cells:
            raise ValueError("地图上没有可刷新的落脚点")
        roll = self.dice.world_roll("最终的冠冕刷新点", cells,
                                   context=f"世界回合{world.clock.turn}：最终的冠冕随机刷新")
        # 骰子结果以 record.selected_index（0 基）为唯一口径，与其它随机点一致。
        position = cells[int(roll["record"]["selected_index"])]
        location_id = f"crown-{world.clock.turn}"
        world.map.locations[location_id] = {
            "kind": "crown", "name": CROWN_NAME,
            "x": int(position[0]), "y": int(position[1]), "active": True,
        }
        world.map.version += 1
        world.crown_spawned = True
        world.crown_location_id = location_id
        world.record("crown_spawned", None, location_id=location_id,
                     position=[int(position[0]), int(position[1])])
        return {"location_id": location_id, "position": [int(position[0]), int(position[1])]}

    def _collect_world_crown(self, world: WorldState) -> str:
        """任何踩在冠冕格上的真实单位都会持有它（不区分玩家/NPC）。"""
        location = world.map.locations.get(world.crown_location_id)
        if not location or not location.get("active", True):
            return ""
        position = (int(location.get("x", -1)), int(location.get("y", -1)))
        holders = sorted((actor for actor in world.actors.values()
                          if actor.alive and actor.position == position),
                         key=lambda actor: actor.actor_id)
        if not holders:
            return ""
        holder = holders[0]
        world.crown_holder_id = holder.actor_id
        world.crown_taken_turn = world.clock.turn
        location["active"] = False
        location["collected"] = True
        location["collected_by"] = holder.actor_id
        location["collected_turn"] = world.clock.turn
        world.deactivate_location_sightings(world.crown_location_id, reason="collected")
        world.record("crown_taken", holder.actor_id, position=list(position),
                     name=holder.name)
        return holder.actor_id

    def _drop_world_crown(self, world: WorldState) -> None:
        """持有者死亡/离场：冠冕掉回地图（原格或持有者最后位置），计时清零。"""
        holder = world.actors.get(world.crown_holder_id)
        position = holder.position if holder is not None else None
        if position is None or not world.map.is_passable(position):
            position = next(((x, y) for y in range(world.map.height)
                             for x in range(world.map.width)
                             if world.map.is_passable((x, y))), None)
        previous = world.crown_holder_id
        world.crown_holder_id = ""
        world.crown_taken_turn = 0
        if position is None:
            return
        location_id = f"crown-{world.clock.turn}-drop"
        world.map.locations[location_id] = {
            "kind": "crown", "name": "最终的冠冕",
            "x": int(position[0]), "y": int(position[1]), "active": True,
        }
        world.map.version += 1
        world.crown_location_id = location_id
        world.crown_spawned = True
        world.record("crown_dropped", previous or None, position=[int(position[0]), int(position[1])])

    def _exit_dungeon_with_crown(self, world: WorldState, holder) -> dict:
        """持有【最终的冠冕】并活过计时 → 带冠冕脱离副本 = 完整通关（封存）。

        用户裁定（2026-10-31）：拿到冠冕离场就是通关，接上原来"死斗胜利"之后那条
        既有封存链路（领终音法器 → 分层封存槽 → 下一阶级），不再需要死斗。
        """
        from ..world import CROWN_HOLD_WORLD_TURNS, CROWN_NAME

        held_turns = max(0, world.clock.turn - world.crown_taken_turn)
        payload = {
            "reason": CROWN_NAME,
            "actor_id": holder.actor_id,
            "actor_name": holder.name,
            "world_turn": world.clock.turn,
            "held_world_turns": held_turns,
            "required_world_turns": CROWN_HOLD_WORLD_TURNS,
        }
        world.crown_exited = True
        holder.alive = False   # 已离开副本，不再是地图上的活动单位
        # 直接摘掉地图单位：_sync_world_* 会依 entity.is_alive 把单位复活，
        # 而这里离场的是"通关封存"的活人，不能靠 alive 标记表达。
        world.actors.pop(holder.actor_id, None)
        # 冠冕随人离开：地图上不再有可争夺的冠冕。
        world.crown_holder_id = ""
        world.crown_location_id = ""
        # 注意：这里**不能**走 Entity.depart_battle——离场者不是战死，而是通关封存；
        # 标成死亡会触发【死者之页】并把「通关」记成「阵亡」。只让地图单位下线。
        # payload 里已有 actor_id，record 的第二个位置参数也是 actor_id，重复会 TypeError。
        world.record("dungeon_exited", holder.actor_id,
                     **{k: v for k, v in payload.items() if k != "actor_id"})
        self.state.dungeon_exit = dict(payload)
        self.state.enemies.clear()
        self.state.delayed_monster_reentries = []
        self.state.monster_reinforcements.clear() if hasattr(self.state, "monster_reinforcements") else None
        self.state.pending_monster_phase = {}
        self.state.combat_subphase = ""
        self.state.current_battle = 0
        self.state.phase = "pre_battle"
        # 人已经带着冠冕离场：地图上未结算的遭遇/事件随之作废，不能拦住封存流程。
        self.event_pool.current = None
        self._seal_crown_carrying_runner(payload)
        return payload

    def _seal_crown_carrying_runner(self, payload: dict) -> None:
        """把带冠冕通关者按既有封存链路处置（终音法器优先，无则直接封存）。"""
        region = self.state.current_region
        if self.TERMINAL_ARTIFACTS.get(region):
            # 与死斗胜利一致：先领终音法器，领取动作里才真正落封存槽。
            self.state.pending_terminal_region = region
            payload["pending_terminal_choice"] = region
            payload["instruction"] = (f"{payload['actor_name']}带着【最终的冠冕】离开副本："
                                      "请调用 choose_terminal_artifact(choice=序号) 领取终音法器后封存")
            self.state.dungeon_exit = dict(payload)
            return
        seal = self._finalize_victory_seal()
        payload["seal"] = seal
        self.state.dungeon_exit = dict(payload)

    def _depart_crown_carrier(self, holder) -> None:
        """让携带冠冕的单位真正离场。

        离场不是击杀：走统一离场入口（depart_battle），不产生击杀/碎片奖励，
        地图单位也会随 entity.is_alive/is_departed 同步（见 _sync_world_*）。
        """
        from ..world import CROWN_NAME

        entities = [self.state.player] if self.state.player is not None else []
        for group_name in ("friends", "employees", "temp_friends", "enemies"):
            entities.extend(getattr(self.state, group_name, None) or [])
        entity = next((item for item in entities
                       if getattr(item, "runtime_id", "") == holder.actor_id), None)
        if entity is not None and not entity.is_departed:
            entity.depart_battle(CROWN_NAME)

    def _leave_world_with_crown(self, world: WorldState, holder) -> dict:
        """非玩家持有者带冠冕离场：冠冕被夺走，副本随之关闭。

        用户裁定（2026-10-31）："谁抢到谁带走，剩下人都死里边"——
        冠冕离场后不再重刷，留在副本里的轮回者出不来（记死因 `crown_lost`，
        不是被谁击杀，故不走击杀/碎片结算）。
        """
        from ..world import CROWN_NAME

        location_id = world.crown_location_id
        if location_id and location_id in world.map.locations:
            world.map.locations[location_id]["active"] = False
            world.map.locations[location_id]["removed_with_crown"] = True
        holder.alive = False
        self._depart_crown_carrier(holder)
        world.crown_exited = True
        world.crown_holder_id = ""
        world.crown_taken_turn = 0
        world.crown_location_id = ""
        world.crown_spawned = False
        world.crown_spawn_turn = 0
        world.record("crown_left_world", holder.actor_id, name=holder.name,
                     reason=CROWN_NAME)
        payload = {"holder_id": holder.actor_id, "holder_name": holder.name,
                   "left_world": True, "world_turn": world.clock.turn}
        player = self.state.player
        if player is not None and player.is_alive and not player.is_departed:
            player.current_hp = 0
            player.is_alive = False
            self.state.last_death_cause = "crown_lost"
            world.record("player_trapped_in_dungeon", player.runtime_id,
                         name=player.name, reason=CROWN_NAME)
            payload["player_trapped"] = True
            self.state.enemies.clear()
            self.state.delayed_monster_reentries = []
            self.state.pending_monster_phase = {}
            self.state.combat_subphase = ""
            self.state.current_battle = 0
            self.state.phase = "pre_battle"
        self.state.dungeon_exit = {**payload, "reason": CROWN_NAME,
                                   "actor_id": holder.actor_id,
                                   "actor_name": holder.name,
                                   "held_world_turns": 0,
                                   "required_world_turns": 0}
        return payload

    def _resolve_world_crown(self, world: WorldState) -> dict | None:
        """【最终的冠冕】世界事件主循环：排期 → 刷新 → 拾取 → 存活计时 → 脱离副本。

        用户裁定：不再有第8场强制死斗。冠冕改为地图上按时间随机刷新的争夺目标，
        持有者连续存活 CROWN_HOLD_WORLD_TURNS 个世界回合就自动离开副本。
        """
        from ..world import CROWN_HOLD_WORLD_TURNS, CROWN_SPAWN_WORLD_TURNS

        if world.crown_exited:
            return None
        if not world.crown_spawn_turn:
            world.crown_spawn_turn = world.clock.turn + CROWN_SPAWN_WORLD_TURNS
            world.record("crown_scheduled", None, spawn_turn=world.crown_spawn_turn)
        if not world.crown_spawned and world.clock.turn >= world.crown_spawn_turn:
            self._spawn_world_crown(world)
        if not world.crown_holder_id and world.crown_spawned:
            self._collect_world_crown(world)
        if not world.crown_holder_id:
            return None
        holder = world.actors.get(world.crown_holder_id)
        if holder is None or not holder.alive:
            self._drop_world_crown(world)
            return None
        # 「活过」的判定只在自由身时成立：正被卷在遭遇战里的持有者不当场消失，
        # 否则会留下"参与者已离场"的半截战斗。
        if any(encounter.active and holder.actor_id in encounter.participants
               for encounter in world.encounters.values()):
            return None
        if world.clock.turn - world.crown_taken_turn >= CROWN_HOLD_WORLD_TURNS:
            if holder.kind == "player":
                return self._exit_dungeon_with_crown(world, holder)
            self._leave_world_with_crown(world, holder)
            return None
        return None

    def _resolve_world_npc_encounters(self, world: WorldState) -> list[dict]:
        """NPC之间的遭遇由现有攻击结算器逐个世界回合自动推进；玩家遭遇仍交给玩家操作。"""
        player_id = next((actor_id for actor_id, actor in world.actors.items()
                          if actor.kind == "player"), None)
        entities = ([self.state.player] if self.state.player else [])
        for group_name in ("friends", "employees", "temp_friends", "enemies"):
            entities.extend(getattr(self.state, group_name, []) or [])
        entity_by_id = {entity.runtime_id: entity for entity in entities if entity is not None}
        tick_world_item_statuses(self.state, entity_by_id.values(), world.clock.turn)
        # 地图互战以世界回合为轮次；延后的部分在下一世界回合造成普通伤害，
        # 也可再被沙漏延后。先取出本回合的伤害，避免把下一回合提前到当场。
        deferred_due = []
        for entity in entity_by_id.values():
            if entity is self.state.player:
                continue
            owed = getattr(entity, "deferred_damage", [])
            due = [entry for entry in owed if entry.get("due_world_turn", float("inf")) <= world.clock.turn]
            if due:
                entity.deferred_damage = [entry for entry in owed if entry not in due]
                deferred_due.append((entity, due))
        for entity, entries in deferred_due:
            self.combat.settle_deferred_damage(entity, entries)
            actor = world.actors.get(entity.runtime_id)
            if actor is not None:
                actor.alive = bool(entity.is_alive and not entity.is_departed)
            world.record("npc_hourglass_damage", entity.runtime_id,
                         amount=sum(entry["amount"] for entry in entries), alive=entity.is_alive)
        resolved = []
        for encounter in list(world.encounters.values()):
            if not encounter.active or player_id in encounter.participants:
                continue
            participants = [world.actors.get(actor_id) for actor_id in encounter.participants]
            participants = [actor for actor in participants if actor is not None and actor.alive]
            if len(participants) < 2:
                world.finish_encounter(encounter.encounter_id, "npc_encounter_ended")
                resolved.append({"encounter_id": encounter.encounter_id, "status": "ended"})
                continue
            for attacker_actor in participants:
                if not encounter.active or not attacker_actor.alive:
                    continue
                attacker = entity_by_id.get(attacker_actor.actor_id)
                if attacker is None or not attacker.is_alive or attacker.is_departed:
                    attacker_actor.alive = False
                    continue
                targets = [actor for actor in participants
                           if actor.alive and actor.actor_id != attacker_actor.actor_id
                           and world._hostile_pair(attacker_actor, actor)]
                if not targets:
                    continue
                target_actor = min(
                    targets,
                    key=lambda actor: (
                        entity_by_id[actor.actor_id].current_hp
                        / max(1, entity_by_id[actor.actor_id].blood_limit)
                        if actor.actor_id in entity_by_id else 1.0,
                        actor.actor_id))
                target = entity_by_id.get(target_actor.actor_id)
                if target is None or not target.is_alive:
                    target_actor.alive = False
                    continue
                item_logs = auto_use_consumables(
                    self.combat, attacker,
                    [entity_by_id[a.actor_id] for a in targets if a.actor_id in entity_by_id])
                for item_log in item_logs:
                    world.record("npc_consumable_used", attacker_actor.actor_id,
                                 item=item_log["item"], uses_remaining=item_log["uses_remaining"],
                                 detail=item_log["detail"])
                for participant in participants:
                    entity = entity_by_id.get(participant.actor_id)
                    if entity is not None:
                        participant.alive = bool(entity.is_alive and not entity.is_departed)
                # 一名单位在一个世界回合内按其攻击次数行动；不额外推进世界时间。
                for hit_index in range(max(1, int(getattr(attacker, "attack_count", 0)))):
                    if not attacker.is_alive or not target.is_alive:
                        break
                    self.combat.resolve_attack(
                        attacker, target, hit_index=hit_index, is_must_hit=False,
                        dodge=False, parry=False,
                        spell_choices={"before": {}, "after": {}})
                target_actor.alive = bool(target.is_alive and not target.is_departed)
                attacker_actor.alive = bool(attacker.is_alive and not attacker.is_departed)
                world.record("npc_skirmish_attack", attacker_actor.actor_id,
                             target_actor_id=target_actor.actor_id,
                             target_hp=target.current_hp, target_alive=target_actor.alive)
            survivors = [world.actors[actor_id] for actor_id in encounter.participants
                         if actor_id in world.actors and world.actors[actor_id].alive
                         and actor_id in entity_by_id and entity_by_id[actor_id].is_alive]
            survivor_sides = {actor.faction for actor in survivors}
            if len(survivor_sides) <= 1:
                world.finish_encounter(encounter.encounter_id, "npc_skirmish_resolved")
                world.record("npc_skirmish_resolved", survivors[0].actor_id if survivors else None,
                             encounter_id=encounter.encounter_id,
                             survivors=[actor.actor_id for actor in survivors])
                resolved.append({"encounter_id": encounter.encounter_id,
                                 "status": "resolved",
                                 "survivors": [actor.actor_id for actor in survivors]})
        for entity in entity_by_id.values():
            if entity is self.state.player:
                continue
            for entry in getattr(entity, "deferred_damage", []):
                entry.setdefault("due_world_turn", world.clock.turn + 1)
        return resolved

    def _advance_world_turns(self, world: WorldState, player_id: str, turns: int) -> int:
        """逐回合推进世界并处理NPC交战，玩家遭遇会立刻打断后续回合。"""
        if not isinstance(turns, int) or isinstance(turns, bool) or turns < 1:
            raise ValueError("等待回合数必须是正整数")
        advanced = 0
        for _ in range(turns):
            world.wait(player_id, 1)
            advanced += 1
            if any(encounter.active and player_id in encounter.participants
                   for encounter in world.encounters.values()):
                break
            self._resolve_world_npc_encounters(world)
            self._resolve_world_shard_pickups(world)
            self._resolve_world_relic_pickups(world)
            self._resolve_world_consumable_pickups(world)
            self._resolve_world_npc_merchant_trades(world)
            self._resolve_world_hiring_signals(world)
            self._resolve_world_temporary_alliances(world)
            self._share_created_being_vision(world)
            self._resolve_world_crown(world)
            if world.crown_exited:
                break   # 持有者已带冠冕脱离副本，本次等待不再继续
        return advanced

    def _resolve_world_shard_pickups(self, world: WorldState) -> list[dict]:
        """所有真实角色进入碎片资源格时各自领取；怪物与商人不拾取玩家资源点。"""
        entities = ([self.state.player] if self.state.player else [])
        entities += self.state.friends + self.state.employees + self.state.temp_friends + self.state.enemies
        by_id = {entity.runtime_id: entity for entity in entities if entity is not None}
        pickups = []
        for actor in world.actors.values():
            if not actor.alive:
                continue
            entity = by_id.get(actor.actor_id)
            if entity is None or entity.entity_type not in {"轮回者", "朋友", "员工"}:
                continue
            found = world.map.location_at(actor.position)
            if found is None:
                continue
            location_id, location = found
            if (location.get("kind") != "resource" or not location.get("active", True)
                    or location.get("resource_type") != "shards"):
                continue
            amount = max(0, int(location.get("amount", 0)))
            if amount <= 0:
                location["active"] = False
                location["collected"] = True
                world.deactivate_location_sightings(location_id, reason="empty_resource")
                continue
            if entity is self.state.player:
                self.state.shards += amount
                self.state.shards_peak = max(self.state.shards_peak, self.state.shards)
                balance = self.state.shards
            else:
                entity.shards += amount
                entity.shards_peak = max(entity.shards_peak, entity.shards)
                balance = entity.shards
            location["active"] = False
            location["collected"] = True
            location["collected_by"] = actor.actor_id
            location["collected_turn"] = world.clock.turn
            world.deactivate_location_sightings(location_id, reason="collected")
            world.record("resource_collected", actor.actor_id, location_id=location_id,
                         resource_type="shards", amount=amount, balance=balance,
                         position=list(actor.position))
            pickups.append({"actor_id": actor.actor_id, "amount": amount,
                            "player_visible": entity is self.state.player,
                            "location_id": location_id})
        return pickups

    def _resolve_world_consumable_pickups(self, world: WorldState) -> list[dict]:
        from ..world_consumables import collect_consumables
        return collect_consumables(world, self.state)

    def _world_consumable_specs(self) -> dict[str, Consumable]:
        from ..events import EVENT_CONSUMABLES
        specs = {name: Consumable(name, effect, uses, uses)
                 for name, (uses, effect) in {**EVENT_CONSUMABLES, **TWISTED_TOOL_LIBRARY}.items()}
        for name in WING_MODES:
            specs[name] = make_wing_item(name)
        return specs

    def _resolve_world_relic_pickups(self, world: WorldState) -> list[dict]:
        """处理世界时间推进后所有单位踩中真实遗物地点的拾取，不泄露NPC的隐藏所得。"""
        entities = []
        if self.state.player is not None:
            entities.append(self.state.player)
        for group_name in ("friends", "employees", "temp_friends", "enemies"):
            entities.extend(getattr(self.state, group_name, []) or [])
        if getattr(self.state, "merchant", None) is not None:
            entities.append(self.state.merchant)
        entity_by_id = {getattr(entity, "runtime_id", ""): entity for entity in entities
                        if getattr(entity, "runtime_id", "")}
        pickups = []
        for location_id, location in world.map.locations.items():
            if location.get("kind") != "relic" or not location.get("active", True):
                continue
            pos = (int(location.get("x", -1)), int(location.get("y", -1)))
            for actor in world.actors.values():
                if not actor.alive or actor.position != pos:
                    continue
                entity = entity_by_id.get(actor.actor_id)
                if entity is None:
                    continue
                is_player = entity is self.state.player
                inventory = self.state.relics if is_player else entity.relics
                owned_names = {relic.name for relic in inventory}
                relic_name = location.get("relic_name", location.get("name", ""))
                # 2026-11-01 重构1：遗物已脱离死者之页，拾取不再受页数拦截。
                if relic_name not in owned_names:
                    relic = Relic(name=relic_name, effect=location.get("relic_effect", ""),
                                  tags=list(location.get("relic_tags") or []))
                    inventory.append(relic)
                    if is_player:
                        self.state.note_relic_gained(relic)
                    location["active"] = False
                    location["collected"] = True
                    location["collected_by"] = actor.actor_id
                    location["collected_turn"] = world.clock.turn
                    world.deactivate_location_sightings(location_id, reason="collected")
                    world.record("resonance_relic_collected", actor.actor_id,
                                 location_id=location_id, relic_name=relic.name,
                                 position=list(pos))
                    pickups.append({"actor_id": actor.actor_id, "relic_name": relic.name,
                                    "player_visible": is_player})
                else:
                    known_by = location.setdefault("discovered_by", [])
                    if actor.actor_id not in known_by:
                        known_by.append(actor.actor_id)
                        location["discovered"] = True
                        world.record("resonance_relic_discovered", actor.actor_id,
                                     location_id=location_id, relic_name=relic_name,
                                     position=list(pos))
                if not location.get("active", True):
                    break
        return pickups

    def _resolve_world_npc_merchant_trades(self, world: WorldState) -> list[dict]:
        """NPC轮回者偶遇商人时可用有限碎片买入一件现存遗物；每个世界回合最多成交一次。"""
        merchant = getattr(self.state, "merchant", None)
        if merchant is None:
            return []
        merchant_actor = world.actors.get(getattr(merchant, "runtime_id", ""))
        if merchant_actor is None or not merchant_actor.alive:
            return []
        player_id = getattr(self.state.player, "runtime_id", "") if self.state.player else ""
        if any(encounter.active and player_id in encounter.participants
               for encounter in world.encounters.values()):
            return []
        # 不依赖调用方恰好只调用一次；同一世界回合已有成交记录时禁止重复成交。
        if any(event.get("type") == "npc_merchant_trade"
               and int(event.get("world_turn", -1)) == world.clock.turn
               for event in world.event_log):
            return []
        entities = list(getattr(self.state, "enemies", []) or [])
        entities += list(getattr(self.state, "friends", []) or [])
        entities += list(getattr(self.state, "employees", []) or [])
        entities += list(getattr(self.state, "temp_friends", []) or [])
        entity_by_id = {entity.runtime_id: entity for entity in entities if entity is not None}
        results = []
        price = 10
        # 先按地图坐标排序，而非随机生成的UUID；同一世界种子下交易优先级稳定。
        for actor_id in sorted(
                world.actors,
                key=lambda aid: (world.actors[aid].y, world.actors[aid].x,
                                world.actors[aid].name, aid)):
            actor = world.actors[actor_id]
            entity = entity_by_id.get(actor_id)
            if (not actor.alive or actor.kind != "enemy" or actor.faction != "enemy"
                    or actor.mode != "autonomous" or entity is None
                    or entity.entity_type != "轮回者" or not entity.is_alive
                    or entity.is_departed
                    or abs(actor.x - merchant_actor.x) + abs(actor.y - merchant_actor.y) != 1):
                continue
            if any(encounter.active and actor_id in encounter.participants
                   for encounter in world.encounters.values()):
                continue
            owned = {relic.name for relic in entity.relics}
            stock = next((relic for relic in merchant.relics if relic.name not in owned), None)
            # 2026-11-01 重构1：遗物不再占死者之页，NPC 买入不再受"承载件数"拦截。
            can_buy = stock is not None
            if can_buy and entity.shards >= price:
                # 固定参考价避免NPC凭空议价；库存与货币由真实实体状态原子转移。
                merchant.relics.remove(stock)
                entity.relics.append(stock)
                entity.shards -= price
                merchant.shards += price
                merchant.shards_peak = max(int(getattr(merchant, "shards_peak", 0)),
                                            int(merchant.shards))
                direction, traded_relic = "buy", stock
            else:
                # NPC也可把普通遗物卖给商人；特殊怪物偏爱遗物不是可交易库存。
                merchant_names = {relic.name for relic in merchant.relics}
                sellable = next((relic for relic in entity.relics
                                 if relic.name not in merchant_names
                                 and not ({"怪物自带", "怪物偏爱"} & set(relic.tags or []))), None)
                if sellable is None or merchant.shards < price:
                    continue
                entity.relics.remove(sellable)
                merchant.relics.append(sellable)
                entity.shards += price
                entity.shards_peak = max(int(getattr(entity, "shards_peak", 0)),
                                         int(entity.shards))
                merchant.shards -= price
                direction, traded_relic = "sell", sellable
            world.record("npc_merchant_trade", actor_id,
                         merchant_actor_id=merchant_actor.actor_id,
                         direction=direction, relic_name=traded_relic.name, price=price,
                         npc_shards=entity.shards, merchant_shards=merchant.shards)
            results.append({"actor_id": actor_id, "direction": direction,
                            "relic_name": traded_relic.name, "price": price})
            # 商人每回合最多完成一笔NPC交易。
            break
        return results

    def _share_created_being_vision(self, world: WorldState) -> None:
        """把妖怪已探索的地形和最近目击记录报告给主人，不泄露实时全图。"""
        for scout in world.actors.values():
            if (not scout.alive or scout.kind != "friend" or not scout.owner_actor_id
                    or scout.behavior not in {"explore", "follow", "guard"}):
                continue
            owner = world.actors.get(scout.owner_actor_id)
            if owner is None or not owner.alive:
                continue
            world.visible_cells(scout.actor_id)
            new_cells = sorted(scout.known_cells - owner.known_cells)
            if new_cells:
                owner.known_cells.update(new_cells)
                world.record("created_being_map_report", owner.actor_id,
                             scout_actor_id=scout.actor_id, new_known_cells=new_cells)
            # 妖怪看见的地点标记会报告给主人；未被玩家/妖怪视野触及的地点仍保持隐藏。
            scout_known = set(scout.known_cells)
            for location_id, location in world.map.locations.items():
                location_key = world.map.key((int(location.get("x", -1)),
                                               int(location.get("y", -1))))
                if location_key not in scout_known:
                    continue
                known_by = location.setdefault("discovered_by", [])
                newly_reported = owner.actor_id not in known_by
                if scout.actor_id not in known_by:
                    known_by.append(scout.actor_id)
                if owner.actor_id not in known_by:
                    known_by.append(owner.actor_id)
                if not location.get("discovered", False) or newly_reported:
                    location["discovered"] = True
                    world.record("created_being_location_reported", owner.actor_id,
                                 scout_actor_id=scout.actor_id, location_id=location_id,
                                 name=location.get("name", location_id))
            known_reports = {
                (item.get("reported_by"), item.get("actor_id"), item.get("turn"))
                for item in owner.memory if item.get("reported_by")
            }
            for observation in scout.memory:
                report_key = (scout.actor_id, observation.get("actor_id"), observation.get("turn"))
                if report_key in known_reports:
                    continue
                report = dict(observation)
                report["reported_by"] = scout.actor_id
                owner.memory.append(report)
                known_reports.add(report_key)
            owner.memory = owner.memory[-50:]

    @ACTION_REGISTRY.action("world_command_created_being", group="world",
                          allowed_phases=("pre_battle", "in_world"))
    def _action_world_command_created_being(self, params: dict) -> dict:
        """切换妖怪自主探索、跟随主人或守卫指定格。"""
        try:
            world = self._world_from_state()
            player_id = next((key for key, actor in world.actors.items()
                              if actor.kind == "player" and actor.alive), None)
            if player_id is None:
                return {"success": False, "error": "地图上没有存活的轮回者"}
            if "target" in params:
                being = find_owned_yaoguai(self.state, params.get("target"))
                if being is not None and not being.is_alive:
                    return {"success": False, "error": "目标妖怪已离场或休眠，无法指挥"}
            else:
                being = next((entity for entity in owned_yaoguai(self.state, player_id)
                              if entity.is_alive), None)
            if being is None:
                return {"success": False, "error": "没有可指挥的存活妖怪，或target未唯一指定你的妖怪"}
            actor = world.actors.get(being.runtime_id)
            if actor is None or not actor.alive:
                return {"success": False, "error": "妖怪不在地图上"}
            behavior = params.get("behavior")
            if behavior not in {"explore", "follow", "guard"}:
                return {"success": False, "error": "behavior必须是explore、follow或guard"}
            if behavior == "guard":
                gx, gy = params.get("x", actor.x), params.get("y", actor.y)
                if (not isinstance(gx, int) or isinstance(gx, bool)
                        or not isinstance(gy, int) or isinstance(gy, bool)
                        or not world.map.is_passable((gx, gy))):
                    return {"success": False, "error": "守卫坐标必须是可通行格上的整数坐标"}
                guard_position = (gx, gy)
            else:
                guard_position = None
            actor.behavior = behavior
            actor.guard_position = guard_position
            actor.owner_actor_id = player_id
            actor.mode = "autonomous"
            world.record("created_being_behavior_changed", player_id,
                         created_being=being.name, behavior=behavior,
                         guard_position=list(guard_position) if guard_position else None)
            self._share_created_being_vision(world)
            self._sync_world_entity_positions(world)
            self.state.world_state = world.to_dict()
            return {"success": True, "action": "指挥妖怪",
                    "result": {"created_being": being.name, "behavior": behavior,
                               "guard_position": list(guard_position) if guard_position else None,
                               "known_cells_shared": len(actor.known_cells)},
                    "instruction": {"explore": "妖怪会自主探索未知区域，并把地形和目击记录报告给主人",
                                    "follow": "妖怪会沿可通行路径向主人靠近",
                                    "guard": "妖怪会前往并守卫指定格"}[behavior]}
        except (ValueError, KeyError, TypeError) as exc:
            return {"success": False, "error": str(exc)}

    def _resolve_world_temporary_alliances(self, world: WorldState) -> list[dict]:
        """胁迫形成的临时同盟在3个世界回合后结束，避免永久阵营污染。"""
        resolved = []
        for actor in world.actors.values():
            alliance = next((item for item in actor.memory
                             if item.get("kind") == "temporary_alliance"), None)
            if alliance is None or world.clock.turn < int(alliance.get("expires_turn", 0)):
                continue
            entity = next((candidate for candidate in self.state.temp_friends
                           if candidate.runtime_id == actor.actor_id), None)
            if entity is not None:
                self.state.temp_friends.remove(entity)
                if entity.is_alive and not entity.is_departed:
                    self.state.enemies.append(entity)
            already_expired = bool(alliance.get("expired"))
            actor.kind = str(alliance.get("original_kind", "enemy"))
            actor.faction = str(alliance.get("original_faction", "enemy"))
            actor.owner_actor_id = ""
            actor.behavior = "explore"
            actor.mode = "autonomous"
            actor.memory = [item for item in actor.memory if item is not alliance]
            if not already_expired:
                world.record("temporary_alliance_expired", actor.actor_id,
                             name=actor.name, expired_turn=world.clock.turn)
            resolved.append({"actor_id": actor.actor_id, "name": actor.name,
                             "status": "expired"})
        return resolved

    def _resolve_world_hiring_signals(self, world: WorldState) -> list[dict]:
        """结算到期招募信号；响应者抵达信号地点后才真正创建员工。"""
        resolved = []
        for signal in world.hiring_signals:
            if signal.get("status") != "pending" or world.clock.turn < int(signal.get("arrival_turn", 0)):
                continue
            params = dict(signal.get("params") or {})
            name = str(params.get("name", ""))
            if (self.state.is_blacklisted
                    or any(entity.name == name for entity in self.state.employees)
                    or not name):
                signal["status"] = "failed"
                signal["resolved_turn"] = world.clock.turn
                signal["failure"] = ("已列入失信黑名单" if self.state.is_blacklisted
                                     else "员工名称已被占用或无效")
                world.record("hiring_signal_failed", None, signal_id=signal.get("signal_id"),
                             name=name, reason=signal["failure"])
                resolved.append({"signal_id": signal.get("signal_id"), "status": "failed",
                                 "reason": signal["failure"]})
                continue
            # 边界可能已收缩到信号发出地点；响应者在最近的可通行格抵达。
            requested = tuple(signal.get("position", (1, 1)))
            if not world.map.is_passable(requested):
                candidates = [(x, y) for y in range(world.map.height)
                              for x in range(world.map.width)
                              if world.map.is_passable((x, y))]
                if not candidates:
                    signal["status"] = "failed"
                    signal["resolved_turn"] = world.clock.turn
                    signal["failure"] = "地图已无可通行地点，无法完成招募"
                    world.record("hiring_signal_failed", None, signal_id=signal.get("signal_id"),
                                 name=name, reason=signal["failure"])
                    resolved.append({"signal_id": signal.get("signal_id"), "status": "failed",
                                     "reason": signal["failure"]})
                    continue
                requested = min(candidates, key=lambda pos: (
                    abs(pos[0] - requested[0]) + abs(pos[1] - requested[1]), pos[1], pos[0]))
            old_energy = self.state.energy
            hire_result = self._pre_battle_guyong(params)
            # 雇佣已改为世界时间行动，不能恢复旧精力系统的“失败退款”副作用。
            self.state.energy = old_energy
            if not hire_result.get("success"):
                signal["status"] = "failed"
                signal["resolved_turn"] = world.clock.turn
                signal["failure"] = hire_result.get("error", "雇佣结算失败")
                world.record("hiring_signal_failed", None, signal_id=signal.get("signal_id"),
                             name=name, reason=signal["failure"])
                resolved.append({"signal_id": signal.get("signal_id"), "status": "failed",
                                 "reason": signal["failure"]})
                continue
            employee = next((entity for entity in self.state.employees
                             if entity.name == name), None)
            if employee is None:
                signal["status"] = "failed"
                signal["resolved_turn"] = world.clock.turn
                signal["failure"] = "雇佣结算未生成员工实体"
                world.record("hiring_signal_failed", None, signal_id=signal.get("signal_id"),
                             name=name, reason=signal["failure"])
                resolved.append({"signal_id": signal.get("signal_id"), "status": "failed",
                                 "reason": signal["failure"]})
                continue
            actor_id = f"hire:{signal.get('signal_id')}"
            employee.runtime_id = actor_id
            employee.world_region = self.state.current_region
            employee.world_x, employee.world_y = requested
            world.add_actor(WorldActorState(
                actor_id=actor_id, name=employee.name, kind="employee",
                x=requested[0], y=requested[1], alive=employee.is_alive,
                faction="employee", mode="manual"))
            signal["status"] = "completed"
            signal["resolved_turn"] = world.clock.turn
            signal["responder_actor_id"] = actor_id
            world.record("hiring_signal_answered", signal.get("owner_actor_id"),
                         signal_id=signal.get("signal_id"), name=name,
                         employee_actor_id=actor_id, position=list(requested))
            resolved.append({"signal_id": signal.get("signal_id"), "status": "completed",
                             "employee": name, "actor_id": actor_id})
        return resolved

    @ACTION_REGISTRY.action("world_initialize", group="world",
                          allowed_phases=("pre_battle", "in_world"))
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
            spawned_reincarnators = []
            had_existing_enemies = bool(self.state.enemies)
            if not had_existing_enemies:
                # 开放世界把当前副本怪物池中的实体生成到地图；不是每次遭遇才临时抽怪。
                from ..monsters import make_monster_entity, assign_monster_preferences
                for monster_def in self._monster_pool_for_battle():
                    monster = make_monster_entity(monster_def)
                    assign_monster_preferences(monster, self._current_tier(), self.dice)
                    self.combat.init_monster_shards(monster)
                    self.state.enemies.append(monster)
                    spawned_monsters.append(monster)
                # 正式地图（面积至少150格）还会生成其他真实轮回者。
                # 小地图主要用于测试/自定义场景，避免自动塞入额外单位破坏调用方布置。
                if width * height >= 150:
                    npc_count = min(4, max(2, self._current_tier() + 1))
                    used_names = {entity.name for entity in
                                  ([self.state.player] if self.state.player else [])
                                  + list(self.state.enemies)}
                    for npc_index in range(npc_count):
                        npc_name = f"流浪轮回者·{npc_index + 1}"
                        suffix = npc_index + 1
                        while npc_name in used_names:
                            suffix += 1
                            npc_name = f"流浪轮回者·{suffix}"
                        used_names.add(npc_name)
                        template = self.state.player
                        npc = Entity(
                            name=npc_name, entity_type="轮回者",
                            blood_limit=max(1, int(template.blood_limit)),
                            current_hp=max(1, int(template.blood_limit)),
                            mana_limit=max(0, int(template.mana_limit)),
                            current_mana=max(0, int(template.mana_limit)),
                            speed_limit=max(0, int(template.speed_limit)),
                            current_speed=max(0, int(template.speed_limit)),
                            # 2026-10-24：攻击次数＝当前速度、攻击力＝当前法力（见
                            # Entity.effective_attack_count/effective_attack_power）。
                            # 直接复制模板的 attack_count/attack_power 会拿到开局前的 0，
                            # 让流浪轮回者变成打不还手、怪物阶段也交不出合法提交的空壳。
                            attack_count=max(0, int(template.speed_limit)),
                            attack_power=max(0, int(template.mana_limit)),
                        )
                        self.state.enemies.append(npc)
                        spawned_reincarnators.append(npc)
            # 每局进入开放世界时商人必须真实入场，而不是只在测试/手动调用中存在。
            # 初始化库存会从遗物池转移恰好5件，随后作为中立自主单位放置到地图。
            if self.state.merchant is None:
                self._initialize_merchant_inventory()
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
            player_actor_id = (getattr(self.state.player, "runtime_id", "") or "player") if self.state.player else ""
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
                # 2026-10-24：只有轮回者本人是手动单位；朋友/员工/造物自主行动，
                # 玩家对它们只能提"建议"（world_suggest_ally），是否照做由它们自己判定。
                mode = "manual" if kind == "player" else "autonomous"
                owner = player_actor_id if kind in ALLY_KINDS else ""
                world.add_actor(WorldActorState(actor_id=actor_id, name=entity.name, kind=kind,
                                                x=pos[0], y=pos[1], alive=entity.is_alive,
                                                faction=faction, mode=mode,
                                                species=("鲛族" if self._entity_is_merfolk(entity) else ""),
                                                behavior=("follow" if kind in ALLY_KINDS else "explore"),
                                                owner_actor_id=owner))
                if kind == "player":
                    world.record("world_entered", actor_id, region=self.state.current_region)
                elif entity in spawned_monsters:
                    world.record("monster_spawned", actor_id, name=entity.name,
                                 position=list(pos), region=self.state.current_region)
                elif entity in spawned_reincarnators:
                    world.record("reincarnator_spawned", actor_id, name=entity.name,
                                 position=list(pos), region=self.state.current_region)
            # 将当前副本可用的地点型事件放入真实地图格；纯状态/战斗触发事件不伪装成地点。
            non_location_events = {"过路商人", "都市的终音", "初拥之夜"}
            location_names = [name for name in self.event_pool.build_pool(self.state.current_region)
                              if name not in non_location_events]
            available = [pos for pos in candidates if pos not in used]
            generic_event_labels = {
                "遗忘书屋": ("遗忘书屋", "书架上的书名都已褪色，翻开后只剩下与自身有关的残页。"),
                "祭坛": ("碎片祭坛", "祭坛中央的凹槽等待投入碎片，回报与代价都无法预知。"),
                "猩红暴雨": ("暴雨避难点", "血色暴雨正在侵蚀街道，附近的漏雨废墟提供了暂避之处。"),
                "无名碑林": ("无名碑林", "苔藓覆盖的石碑刻满死者执念，靠近时会产生共鸣。"),
                "回音长廊": ("回音长廊", "破碎镜面映出此前轮回的惨死片段，尽头传来八音盒声。"),
                "回忆当铺": ("回忆当铺", "没有门的当铺以记忆作价，柜台后有阴影静候来客。"),
                "无魂泥潭": ("无魂泥潭", "黑色沥青状淤泥隔绝灵魂波动，采集时会腐蚀肉体。"),
            }
            twisted_event_labels = {
                "医生": ("社区诊所", "街角诊所仍亮着应急灯，医生可能愿意接触来访者。"),
                "乞丐": ("地铁口的乞丐", "地铁入口附近的乞丐向路人乞讨，也可能掌握城市传闻。"),
                "血肉温室": ("血肉温室", "玻璃穹顶内堆满无法辨认的血肉植株。"),
                "绝望来电": ("公共电话亭", "电话亭的线路仍然接通，听筒里传来不属于此处的声音。"),
                "皮衣店": ("皮衣店", "卷帘门半开，橱窗里挂着被血迹浸透的皮衣。"),
                "生锈邮筒": ("生锈邮筒", "锈蚀的邮筒里塞满了没有寄件人信息的信件。"),
                "尖叫下水道": ("下水道入口", "井盖下不断传出断续尖叫，回声会掩盖附近的动静。"),
                "恶魔医务室": ("恶魔医务室", "手术灯仍在运转，器械台上摆着不该存在的工具。"),
            }
            event_labels = {**generic_event_labels, **twisted_event_labels}
            for event_name in location_names:
                if not available:
                    break
                roll = self.dice.auto_roll(
                    f"open_world_location_{event_name}", available,
                    context=f"开放世界地点放置：{event_name}")
                pos = tuple(roll["selected"])
                display_name, description = event_labels.get(
                    event_name, (event_name, "到达此处后可触发对应的地点事件。"))
                world.map.add_location(event_name, {
                    "kind": "event", "name": display_name, "event_name": event_name,
                    "description": description,
                    "x": pos[0], "y": pos[1], "discovered": False, "active": True})
                available.remove(pos)
            # 扭曲都市额外生成可辨识的现代城市地标；地标只提供地图语境，不凭空触发事件。
            if self.state.current_region == "扭曲都市":
                urban_landmarks = [
                    ("医院", "废弃综合医院，药械储备区已被翻找过。"),
                    ("学校", "封锁中的学校，教室、走廊和操场组成开阔区域。"),
                    ("停车场", "多层停车场，车辆残骸可以遮挡视线。"),
                    ("地铁站", "停运地铁入口，地下通道深入城市下方。"),
                    ("便利店", "被洗劫的便利店，货架和收银台散落在地。"),
                    ("警察局", "废弃警局，卷帘门和防爆玻璃仍保留着。"),
                    ("购物中心", "空置的购物中心，内部通道错综复杂。"),
                    ("城市广场", "露天广场视野开阔，却几乎没有掩体。"),
                ]
                for landmark_index, (landmark_name, description) in enumerate(urban_landmarks):
                    if not available:
                        break
                    roll = self.dice.auto_roll(
                        f"open_world_urban_landmark_{landmark_index}", available,
                        context=f"扭曲都市地标放置：{landmark_name}")
                    pos = tuple(roll["selected"])
                    world.map.add_location(f"urban_{landmark_index + 1}", {
                        "kind": "landmark", "name": landmark_name,
                        "description": description, "x": pos[0], "y": pos[1],
                        "discovered": False, "active": True})
                    available.remove(pos)
            # 世界地图同时生成真实资源点；进入地点后立即拾取，且该点只结算一次。
            resource_count = max(1, min(5, (width * height) // 150))
            for resource_index in range(resource_count):
                if not available:
                    break
                resource_roll = self.dice.auto_roll(
                    f"open_world_resource_location_{resource_index}", available,
                    context=f"开放世界碎片资源点位置#{resource_index + 1}")
                resource_pos = tuple(resource_roll["selected"])
                amount_roll = self.dice.auto_roll(
                    f"open_world_resource_amount_{resource_index}", list(range(5, 16)),
                    context=f"开放世界碎片资源点储量#{resource_index + 1}")
                world.map.add_location(f"resource_shards_{resource_index + 1}", {
                    "kind": "resource", "name": "碎片残留", "resource_type": "shards",
                    "amount": int(amount_roll["selected"]),
                    "x": resource_pos[0], "y": resource_pos[1],
                    "discovered": False, "active": True})
                available.remove(resource_pos)
            from ..world_consumables import scatter_consumables
            scattered_count = scatter_consumables(world, self.dice, self._world_consumable_specs(), available)
            self._sync_world_entity_positions(world)
            self.state.world_state = world.to_dict()
            return {"success": True, "action": "world_initialize",
                    "result": {"width": width, "height": height,
                               "world_turn": world.clock.turn,
                               "consumable_locations": scattered_count,
                               "spawned_monsters": len(spawned_monsters),
                               "spawned_reincarnators": len(spawned_reincarnators),
                               "event_locations": len(world.map.locations)}}
        except (ValueError, KeyError, TypeError) as exc:
            return {"success": False, "error": str(exc)}

    @ACTION_REGISTRY.action("world_move", group="world",
                          allowed_phases=("pre_battle", "in_world"))
    def _action_world_move(self, params: dict) -> dict:
        try:
            world = self._world_from_state()
            if world.crown_exited:
                return {"success": False, "error": "【最终的冠冕】已被带出副本，地图行动已结束"}
            actor_id = params.get("actor_id")
            player_id = next((key for key, actor in world.actors.items() if actor.kind == "player"), None)
            actor_id = params.get("actor_id") or player_id
            if actor_id is None or actor_id != player_id:
                return {"success": False, "error": "当前API只允许轮回者控制自己的地图单位"}
            x, y = params.get("x"), params.get("y")
            if not isinstance(x, int) or isinstance(x, bool) or not isinstance(y, int) or isinstance(y, bool):
                return {"success": False, "error": "目标坐标x/y必须是整数"}
            player_entity = self.state.player
            world_actor = world.actors.get(actor_id)
            if world_actor is not None:
                world_actor.species = "鲛族" if self._entity_is_merfolk(player_entity) else ""
            result = world.move_actor(actor_id, (x, y))
            if not any(encounter.active and actor_id in encounter.participants
                       for encounter in world.encounters.values()):
                self._resolve_world_npc_encounters(world)
            self._resolve_world_hiring_signals(world)
            self._resolve_world_temporary_alliances(world)
            self._share_created_being_vision(world)
            self._sync_world_entity_positions(world)
            location_entry = world.map.location_at((x, y))
            if location_entry is not None:
                location_id, location = location_entry
                if location.get("active", True):
                    location["discovered"] = True
                    if location.get("kind") == "resource" and location.get("resource_type") == "shards":
                        amount = max(0, int(location.get("amount", 0)))
                        self.state.shards += amount
                        self.state.shards_peak = max(
                            int(getattr(self.state, "shards_peak", 0)),
                            max(0, int(self.state.shards)))
                        location["active"] = False
                        location["collected"] = True
                        location["collected_by"] = actor_id
                        location["collected_turn"] = world.clock.turn
                        world.deactivate_location_sightings(location_id, reason="collected")
                        world.record("resource_collected", actor_id, location_id=location_id,
                                     resource_type="shards", amount=amount,
                                     position=[x, y])
                        result["resource_collected"] = {"name": location.get("name", "碎片残留"),
                                                        "shards": amount}
                    elif location.get("kind") == "consumable":
                        pickups = self._resolve_world_consumable_pickups(world)
                        result["consumables_collected"] = [p for p in pickups if p["actor_id"] == actor_id]
                    elif location.get("kind") == "relic":
                        actor_entity = None
                        entity_candidates = ([self.state.player] if self.state.player else [])
                        for group_name in ("friends", "employees", "temp_friends", "enemies"):
                            entity_candidates.extend(getattr(self.state, group_name, []) or [])
                        if getattr(self.state, "merchant", None) is not None:
                            entity_candidates.append(self.state.merchant)
                        actor_entity = next((entity for entity in entity_candidates
                                             if getattr(entity, "runtime_id", "") == actor_id), None)
                        if actor_entity is not None:
                            is_player = actor_entity is self.state.player
                            owned_relics = self.state.relics if is_player else actor_entity.relics
                            owned_names = {relic.name for relic in owned_relics}
                            # 2026-11-01 重构1：遗物不占页，拾取不再有页数门禁。
                            if location.get("relic_name") not in owned_names:
                                relic = Relic(name=location["relic_name"],
                                              effect=location.get("relic_effect", ""),
                                              tags=list(location.get("relic_tags") or []))
                                owned_relics.append(relic)
                                if is_player:
                                    self.state.note_relic_gained(relic)
                                location["active"] = False
                                location["collected"] = True
                                location["collected_by"] = actor_id
                                location["collected_turn"] = world.clock.turn
                                world.deactivate_location_sightings(location_id, reason="collected")
                                world.record("resonance_relic_collected", actor_id,
                                             location_id=location_id, relic_name=relic.name,
                                             position=[x, y])
                                result["relic_collected"] = {"name": relic.name, "effect": relic.effect}
                            else:
                                location["discovered"] = True
                                location.setdefault("discovered_by", [])
                                if actor_id not in location["discovered_by"]:
                                    location["discovered_by"].append(actor_id)
                                world.record("resonance_relic_discovered", actor_id,
                                             location_id=location_id,
                                             relic_name=location.get("relic_name"),
                                             position=[x, y])
                    else:
                        event_name = location.get("event_name", location.get("name", location_id))
                        if (self.event_pool.current is None and event_name in self.event_pool.events
                                and (event_name == "祭坛" or event_name not in self.event_pool.triggered)):
                            self.event_pool.current = event_name
                            world.record("location_discovered", actor_id, location_id=location_id,
                                         location_name=location.get("name", location_id),
                                         event_name=event_name, position=[x, y])
                            result["location_event"] = event_name
            # NPC在本回合移动后踩中资源/遗物，也会真正拾取；隐藏NPC所得不会回传给玩家。
            self._resolve_world_shard_pickups(world)
            self._resolve_world_relic_pickups(world)
            self._resolve_world_crown(world)
            self._resolve_world_consumable_pickups(world)
            npc_trades = self._resolve_world_npc_merchant_trades(world)
            if npc_trades:
                player_actor = world.actors.get(actor_id)
                visible_positions = ({player_actor.position}
                                     | set(world.map.adjacent_vision(player_actor.position))
                                     if player_actor is not None else set())
                # NPC在视野外完成的交易只写入世界事实账本，不直接泄露给玩家。
                merchant_actor_id = getattr(self.state.merchant, "runtime_id", "")
                visible_trades = [
                    trade for trade in npc_trades
                    if trade["actor_id"] in world.actors
                    and world.actors[trade["actor_id"]].position in visible_positions
                    and merchant_actor_id in world.actors
                    and world.actors[merchant_actor_id].position in visible_positions
                ]
                if visible_trades:
                    result["npc_merchant_trades"] = visible_trades
            # 商人自己走近玩家也会建立接触；只在实际相邻时打开交易会话。
            self._open_adjacent_merchant_session(world)
            self.state.world_state = world.to_dict()
            return {"success": True, "action": "world_move", "result": result}
        except (ValueError, KeyError, TypeError) as exc:
            return {"success": False, "error": str(exc)}

    @ACTION_REGISTRY.action("world_wait", group="world",
                          allowed_phases=("pre_battle", "in_world"))
    def _action_world_wait(self, params: dict) -> dict:
        try:
            world = self._world_from_state()
            if world.crown_exited:
                return {"success": False, "error": "【最终的冠冕】已被带出副本，不能继续等待世界回合"}
            actor_id = params.get("actor_id")
            player_id = next((key for key, actor in world.actors.items() if actor.kind == "player"), None)
            actor_id = params.get("actor_id") or player_id
            if actor_id is None or actor_id != player_id:
                return {"success": False, "error": "当前API只允许轮回者控制自己的地图单位"}
            turns = params.get("turns", 1)
            start_turn = world.clock.turn
            self._advance_world_turns(world, actor_id, turns)
            self._resolve_world_shard_pickups(world)
            self._resolve_world_relic_pickups(world)
            self._resolve_world_crown(world)
            self._resolve_world_consumable_pickups(world)
            self._resolve_world_hiring_signals(world)
            self._resolve_world_temporary_alliances(world)
            self._share_created_being_vision(world)
            elapsed = world.clock.turn - start_turn
            active_encounter = next((encounter for encounter in world.encounters.values()
                                     if encounter.active and actor_id in encounter.participants), None)
            self._sync_world_entity_positions(world)
            if active_encounter is None:
                self._open_adjacent_merchant_session(world)
            self.state.world_state = world.to_dict()
            crown_status = self._world_crown_status(world)
            payload = {"world_turn": world.clock.turn, "turns": elapsed,
                       "requested_turns": turns,
                       "interrupted_by_encounter": active_encounter is not None,
                       "encounter_id": active_encounter.encounter_id if active_encounter else None,
                       "final_crown": crown_status}
            if world.crown_exited and self.state.dungeon_exit:
                payload["dungeon_exit"] = dict(self.state.dungeon_exit)
            return {"success": True, "action": "world_wait", "result": payload}
        except (ValueError, KeyError, TypeError) as exc:
            return {"success": False, "error": str(exc)}

    def _world_crown_status(self, world: WorldState) -> dict:
        """【最终的冠冕】的公开状态：只给玩家可见的事实与倒计时。"""
        from ..world import CROWN_HOLD_WORLD_TURNS

        status = {
            "spawn_turn": world.crown_spawn_turn,
            "spawned": world.crown_spawned,
            "exited": world.crown_exited,
            "holder_id": world.crown_holder_id,
            "held_world_turns": (max(0, world.clock.turn - world.crown_taken_turn)
                                 if world.crown_holder_id else 0),
            "required_world_turns": CROWN_HOLD_WORLD_TURNS,
        }
        location = world.map.locations.get(world.crown_location_id) or {}
        if world.crown_spawned and not world.crown_holder_id and location.get("active", True):
            status["position"] = [int(location.get("x", -1)), int(location.get("y", -1))]
        return status

    @ACTION_REGISTRY.action("world_status", group="world",
                          allowed_phases=("pre_battle", "in_world"))
    def _action_world_status(self, params: dict) -> dict:
        """只返回玩家已知信息，不把全地图、隐藏单位或完整事实账本交给AI。"""
        try:
            world = self._world_from_state()
            if world.crown_exited:
                return {"success": True, "action": "world_status",
                        "result": {"world_turn": world.clock.turn,
                                   "final_crown": self._world_crown_status(world),
                                   "exit": dict(self.state.dungeon_exit or {}),
                                   "instruction": "【最终的冠冕】已被带出副本，地图阶段已结束"}}
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
                 "description": location.get("description", ""),
                 "position": [location.get("x"), location.get("y")],
                 "active": location.get("active", True)}
                for location_id, location in world.map.locations.items()
                if location.get("discovered", False)
                and (location.get("kind") not in {"relic", "consumable"}
                     or actor_id in (location.get("discovered_by") or []))
            ]
            next_shrink_turn = None
            if (world.shrink_interval > 0
                    and world.shrink_count * 2 < min(world.map.width, world.map.height) - 1):
                next_shrink_turn = (world.shrink_count + 1) * world.shrink_interval
            self.state.world_state = world.to_dict()
            return {"success": True, "action": "world_status", "result": {
                "world_turn": world.clock.turn,
                "map_size": {"width": world.map.width, "height": world.map.height},
                "boundary": {
                    "shape": "square",
                    "shrink_interval": world.shrink_interval,
                    "shrunk_rings": world.shrink_count,
                    "next_shrink_turn": next_shrink_turn,
                },
                "final_crown": self._world_crown_status(world),
                "self": {"actor_id": actor.actor_id, "name": actor.name,
                         "position": [actor.x, actor.y], "alive": actor.alive},
                "known_terrain": known_terrain,
                "visible_actors": visible_actors,
                "visible_encounters": visible_encounters,
                "known_locations": known_locations,
                "allies": [
                    {"actor_id": other.actor_id, "name": other.name, "kind": other.kind,
                     "alive": other.alive,
                     "position": [other.x, other.y], "behavior": other.behavior,
                     "guard_position": (list(other.guard_position)
                                        if other.guard_position else None),
                     "owner_actor_id": other.owner_actor_id}
                    for other in world.actors.values()
                    if other.kind in ALLY_KINDS and other.owner_actor_id == actor_id
                ],
                "suggestion_range": ALLY_SUGGESTION_RANGE,
                "hiring_signals": [
                    {"signal_id": signal.get("signal_id"),
                     "status": signal.get("status"),
                     "issued_turn": signal.get("issued_turn"),
                     "arrival_turn": signal.get("arrival_turn"),
                     "resolved_turn": signal.get("resolved_turn"),
                     "name": (signal.get("params") or {}).get("name", ""),
                     "failure": signal.get("failure", "")}
                    for signal in world.hiring_signals
                    if signal.get("owner_actor_id") == actor_id
                ],
            }}
        except (ValueError, KeyError) as exc:
            return {"success": False, "error": str(exc)}

    @ACTION_REGISTRY.action("world_interact", group="world",
                          allowed_phases=("pre_battle", "in_world"))
    def _action_world_interact(self, params: dict) -> dict:
        """与相邻单位交涉；资产只在目标明确接受公平报价后转移。"""
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
            entity_candidates = ([self.state.player] if self.state.player else [])
            for group_name in ("friends", "employees", "temp_friends", "enemies"):
                entity_candidates.extend(getattr(self.state, group_name, []) or [])
            target_entity = next((entity for entity in entity_candidates
                                  if entity is not None and entity.runtime_id == target_id), None)

            if choice == "trade":
                if target.kind == "merchant":
                    if self.event_pool.current is not None:
                        return {"success": False, "error": "当前已有待结算事件，不能开启商人交易"}
                    self.event_pool.current = "过路商人"
                    world.record("merchant_encountered", player_id, merchant_actor_id=target_id,
                                 position=list(target.position))
                    self.state.world_state = world.to_dict()
                    return {"success": True, "action": "world_interact",
                            "result": {"choice": choice, "target": target.name,
                                       "session": "过路商人"}}
                if target_entity is None or target_entity.entity_type != "轮回者":
                    return {"success": False, "error": "普通双边交易只支持与其他轮回者进行"}
                offer_shards = params.get("offer_shards", 0)
                request_shards = params.get("request_shards", 0)
                give_relic = params.get("give_relic")
                request_relic = params.get("request_relic")
                if any(not isinstance(value, int) or isinstance(value, bool) or value < 0
                       for value in (offer_shards, request_shards)):
                    return {"success": False, "error": "offer_shards/request_shards必须是非负整数"}
                if not any((offer_shards, request_shards, give_relic, request_relic)):
                    return {"success": False, "error": "报价必须至少包含碎片或遗物；提交offer_shards/request_shards/give_relic/request_relic"}
                if offer_shards > self.state.shards:
                    return {"success": False, "error": "你没有足够碎片支付报价"}
                if request_shards > target_entity.shards:
                    return {"success": False, "error": "对方没有足够碎片支付报价"}
                player_relic = next((r for r in self.state.relics if r.name == give_relic), None) if give_relic else None
                target_relic = next((r for r in target_entity.relics if r.name == request_relic), None) if request_relic else None
                if give_relic and player_relic is None:
                    return {"success": False, "error": f"你没有遗物【{give_relic}】"}
                if request_relic and target_relic is None:
                    return {"success": False, "error": f"对方没有遗物【{request_relic}】"}
                # 2026-11-01 重构1：遗物不占页、持有不限量，交易不再因"承载满"拒绝；
                # 对方的启用槽位若已满，新到手的遗物按停用入库即可。
                # 默认议价基准：一件遗物按10碎片估值。目标只接受自己收到的价值不低于付出的报价。
                value_received = offer_shards + (10 if give_relic else 0)
                value_given = request_shards + (10 if request_relic else 0)
                world.record("trade_offer_made", player_id, target_actor_id=target_id,
                             offer_shards=offer_shards, request_shards=request_shards,
                             give_relic=give_relic, request_relic=request_relic,
                             estimated_value_received=value_received,
                             estimated_value_given=value_given)
                if value_received < value_given:
                    world.record("trade_offer_rejected", target_id, proposer_actor_id=player_id,
                                 reason="报价价值低于对方付出")
                    self.state.world_state = world.to_dict()
                    return {"success": True, "action": "world_interact",
                            "result": {"choice": "trade", "target": target.name,
                                       "accepted": False, "completed": False,
                                       "reason": "对方认为报价不划算；没有资产转移"}}
                # 先计算交易后的净余额，再一次性提交。不能把交易过程中的临时中间值
                # 记为历史峰值，否则对方先收到、再付出的同一笔碎片会凭空抬高峰值。
                player_final_shards = self.state.shards - offer_shards + request_shards
                target_final_shards = target_entity.shards + offer_shards - request_shards
                self.state.shards = player_final_shards
                target_entity.shards = target_final_shards
                self.state.shards_peak = max(self.state.shards_peak, player_final_shards)
                target_entity.shards_peak = max(target_entity.shards_peak, target_final_shards)
                if player_relic is not None:
                    self.state.relics.remove(player_relic)
                    target_entity.relics.append(player_relic)
                if target_relic is not None:
                    target_entity.relics.remove(target_relic)
                    self.state.relics.append(target_relic)
                    self.state.note_relic_gained(target_relic)
                world.record("trade_offer_accepted", target_id, proposer_actor_id=player_id,
                             offer_shards=offer_shards, request_shards=request_shards,
                             give_relic=give_relic, request_relic=request_relic)
                self.state.world_state = world.to_dict()
                return {"success": True, "action": "world_interact",
                        "result": {"choice": "trade", "target": target.name,
                                   "accepted": True, "completed": True,
                                   "offer_shards": offer_shards, "request_shards": request_shards,
                                   "gave_relic": give_relic, "received_relic": request_relic,
                                   "player_shards": self.state.shards,
                                   "counterparty_shards": target_entity.shards}}

            if choice == "attack":
                if target.kind != "enemy":
                    return {"success": False, "error": "当前只有敌方单位可直接进入战斗"}
                target.faction = "monster" if target.faction == "monster" else "enemy"
                encounter = world.start_encounter([player_id, target_id], target.position)
                world.record("hostility_declared", player_id, target_actor_id=target_id,
                             target_name=target.name, encounter_id=encounter.encounter_id)
                self._sync_world_entity_positions(world)
                self.state.world_state = world.to_dict()
                return {"success": True, "action": "world_interact",
                        "result": {"choice": choice, "encounter_id": encounter.encounter_id,
                                   "world_encounter_id": encounter.encounter_id,
                                   "participants": list(encounter.participants),
                                   "battle_started": False},
                        "instruction": "遭遇已登记；现在调用battle_start，并传入result.world_encounter_id以接入现有战斗引擎"}

            if choice == "talk":
                rumors = [dict(item) for item in target.memory[-3:]
                          if item.get("actor_id") and item.get("turn") is not None]
                event = world.record("conversation_requested", player_id,
                                     target_actor_id=target_id, target_name=target.name,
                                     shared_rumor_count=len(rumors))
                self.state.world_state = world.to_dict()
                return {"success": True, "action": "world_interact",
                        "result": {"choice": choice, "event_id": event["event_id"],
                                   "target": target.name, "reported_sightings": rumors,
                                   "narrative_input": "对话只可使用目标实际目击并分享的记录，不得虚构交易或承诺"}}

            if choice == "cooperate":
                if target_entity is None or target_entity.entity_type != "轮回者" or target_entity not in self.state.enemies:
                    return {"success": False, "error": "只有尚未结盟的其他轮回者可以接受结盟提议"}
                hp_ratio = target_entity.current_hp / max(1, target_entity.blood_limit)
                accept_percent = max(15, min(85, 25 + int((1 - hp_ratio) * 60)))
                roll = self.dice.auto_roll(
                    f"world_alliance_consent_{target_id}_{world.clock.turn}",
                    ["accept"] * accept_percent + ["reject"] * (100 - accept_percent),
                    context=f"{target.name}评估结盟提议：根据当前伤势决定是否接受")
                accepted = roll["selected"] == "accept"
                world.record("alliance_decision", target_id, proposer_actor_id=player_id,
                             accepted=accepted, accept_chance=accept_percent)
                if accepted:
                    self.state.enemies.remove(target_entity)
                    self.state.friends.append(target_entity)
                    target.kind, target.faction = "friend", "friend"
                    target.behavior = "explore"
                    target.owner_actor_id = ""
                    target.mode = "autonomous"
                self.state.world_state = world.to_dict()
                return {"success": True, "action": "world_interact",
                        "result": {"choice": choice, "target": target.name,
                                   "accepted": accepted, "completed": accepted,
                                   "accept_chance_percent": accept_percent}}

            # 恐吓只对其他轮回者进行可结算的胁迫；怪物不接受人类谈判。
            if target_entity is None or target_entity.entity_type != "轮回者" or target_entity not in self.state.enemies:
                encounter = world.start_encounter([player_id, target_id], target.position)
                world.record("intimidation_ignored", target_id, proposer_actor_id=player_id,
                             encounter_id=encounter.encounter_id)
                self.state.world_state = world.to_dict()
                return {"success": True, "action": "world_interact",
                        "result": {"choice": choice, "accepted": False,
                                   "encounter_id": encounter.encounter_id,
                                   "reason": "目标不接受胁迫，已进入战斗"},
                        "instruction": "接着调用battle_start处理这次遭遇"}
            player_entity = self.state.player
            player_power = max(1, int(getattr(player_entity, "attack_power", 0))
                               + int(getattr(player_entity, "attack_count", 0)))
            target_power = max(1, int(target_entity.attack_power) + int(target_entity.attack_count))
            player_hp_ratio = player_entity.current_hp / max(1, player_entity.blood_limit)
            target_hp_ratio = target_entity.current_hp / max(1, target_entity.blood_limit)
            chance = max(10, min(90, 35 + 20 * (player_power > target_power)
                                 + 25 * (target_hp_ratio < 0.5)
                                 - 20 * (player_hp_ratio < 0.5)))
            roll = self.dice.auto_roll(
                f"world_intimidation_{target_id}_{world.clock.turn}",
                ["success"] * chance + ["fail"] * (100 - chance),
                context=f"轮回者胁迫{target.name}：对比战力和伤势")
            if roll["selected"] == "success":
                self.state.enemies.remove(target_entity)
                self.state.temp_friends.append(target_entity)
                target.kind, target.faction = "friend", "temp_friend"
                target.behavior = "follow"
                target.owner_actor_id = player_id
                target.memory.append({"kind": "temporary_alliance",
                                      "expires_turn": world.clock.turn + 3,
                                      "original_kind": "enemy",
                                      "original_faction": "enemy"})
                world.record("intimidation_succeeded", player_id, target_actor_id=target_id,
                             target_name=target.name, expires_turn=world.clock.turn + 3,
                             success_chance_percent=chance)
                self.state.world_state = world.to_dict()
                return {"success": True, "action": "world_interact",
                        "result": {"choice": choice, "accepted": True, "completed": True,
                                   "target": target.name, "expires_turn": world.clock.turn + 3,
                                   "success_chance_percent": chance}}
            encounter = world.start_encounter([player_id, target_id], target.position)
            world.record("intimidation_failed", target_id, proposer_actor_id=player_id,
                         encounter_id=encounter.encounter_id, success_chance_percent=chance)
            self.state.world_state = world.to_dict()
            return {"success": True, "action": "world_interact",
                    "result": {"choice": choice, "accepted": False, "completed": False,
                               "encounter_id": encounter.encounter_id,
                               "success_chance_percent": chance},
                    "instruction": "胁迫失败，接着调用battle_start处理这次遭遇"}
        except (ValueError, KeyError, TypeError) as exc:
            return {"success": False, "error": str(exc)}

    def _ally_suggestion_chance(self, kind: str, suggestion: str, **factors) -> int:
        """建议采纳率（独立方法，便于按单位类型/处境替换判定）。"""
        return suggestion_acceptance_percent(kind, suggestion, **factors)

    @ACTION_REGISTRY.action("world_suggest_ally", group="world",
                          allowed_phases=("pre_battle", "in_world"))
    def _action_world_suggest_ally(self, params: dict) -> dict:
        """对友方单位提一条建议。玩家只能建议：是否照做由该单位自己判定。

        与 world_move 的关键区别：这个接口不改变任何单位的位置，只登记一次
        "建议 + 该单位自己的决定"。被拒绝时它的行为与目标保持原样，玩家再从
        world_status.allies 里看到它自己选了什么。
        """
        try:
            world = self._world_from_state()
            if world.crown_exited:
                return {"success": False, "error": "【最终的冠冕】已被带出副本，地图行动已结束"}
            player_id = next((key for key, actor in world.actors.items()
                              if actor.kind == "player" and actor.alive), None)
            if player_id is None:
                return {"success": False, "error": "地图上没有存活的轮回者"}
            target_id = params.get("target_actor_id")
            if not isinstance(target_id, str) or target_id not in world.actors:
                return {"success": False, "error": "必须指定 world_status.allies 里的友方单位"}
            target = world.actors[target_id]
            if target.kind not in ALLY_KINDS:
                return {"success": False, "error": "只能向朋友/员工/造物提建议"}
            if not target.alive:
                return {"success": False, "error": "目标单位已离场"}
            if any(encounter.active and target_id in encounter.participants
                   for encounter in world.encounters.values()):
                return {"success": False, "error": "目标正处于遭遇中，无法听取建议"}
            suggestion = params.get("suggestion")
            if suggestion not in ALLY_SUGGESTIONS:
                return {"success": False,
                        "error": f"suggestion必须是{sorted(ALLY_SUGGESTIONS)}之一"}
            player_actor = world.actors[player_id]
            distance = abs(player_actor.x - target.x) + abs(player_actor.y - target.y)
            if distance > ALLY_SUGGESTION_RANGE:
                return {"success": False,
                        "error": f"目标距离{distance}格，超出建议范围{ALLY_SUGGESTION_RANGE}"}
            guard_position = None
            if ALLY_SUGGESTIONS[suggestion] == "guard":
                gx = params.get("x", target.x)
                gy = params.get("y", target.y)
                if (not isinstance(gx, int) or isinstance(gx, bool)
                        or not isinstance(gy, int) or isinstance(gy, bool)
                        or not world.map.is_passable((gx, gy))):
                    return {"success": False, "error": "守卫坐标必须是可通行格上的整数坐标"}
                guard_position = (gx, gy)

            entity = self._world_entity_by_actor_id(world, target_id)
            hp_ratio = 1.0
            if entity is not None and entity.blood_limit > 0:
                hp_ratio = max(0.0, min(1.0, entity.current_hp / entity.blood_limit))
            player_entity = self.state.player
            player_hp_ratio = 1.0
            if player_entity is not None and player_entity.blood_limit > 0:
                player_hp_ratio = max(0.0, min(1.0, player_entity.current_hp / player_entity.blood_limit))
            chance = self._ally_suggestion_chance(
                target.kind, suggestion,
                current_behavior=target.behavior, hp_ratio=hp_ratio,
                player_hp_ratio=player_hp_ratio, distance=distance)
            roll = self.dice.auto_roll(
                f"world_ally_suggestion_{target_id}_{world.clock.turn}_{suggestion}",
                ["accept"] * chance + ["reject"] * (100 - chance),
                context=f"{target.name}考虑轮回者的建议：{suggestion}")
            accepted = roll["selected"] == "accept"
            previous = {"behavior": target.behavior,
                        "guard_position": list(target.guard_position) if target.guard_position else None}
            world.record("ally_suggestion_made", player_id, target_actor_id=target_id,
                         target_name=target.name, suggestion=suggestion,
                         accepted=accepted, chance_percent=chance)
            if accepted:
                target.behavior = ALLY_SUGGESTIONS[suggestion]
                target.guard_position = guard_position
                target.owner_actor_id = player_id
                target.mode = "autonomous"
                world.record("ally_suggestion_accepted", target_id,
                             suggestion=suggestion, behavior=target.behavior,
                             guard_position=list(guard_position) if guard_position else None)
            else:
                # 拒绝：它自己做主，玩家只能看到它现在的选择。
                world.record("ally_suggestion_rejected", target_id,
                             suggestion=suggestion, kept_behavior=target.behavior)
            self._sync_world_entity_positions(world)
            self.state.world_state = world.to_dict()
            return {
                "success": True,
                "action": f"建议{target.name}·{suggestion}",
                "result": {
                    "target": target.name, "target_actor_id": target_id,
                    "suggestion": suggestion,
                    "accepted": accepted,
                    "chance_percent": chance,
                    "behavior": target.behavior,
                    "guard_position": list(target.guard_position) if target.guard_position else None,
                    "previous": previous,
                },
                "instruction": ("它接受了建议，接下来会按此行动" if accepted
                                else "它没有采纳，仍在按自己的判断行动；可以换个说法再试"),
            }
        except (ValueError, KeyError, TypeError) as exc:
            return {"success": False, "error": str(exc)}

    def _world_entity_by_actor_id(self, world: WorldState, actor_id: str):
        """actor_id → 真实实体（用于读取伤势等自身处境）。"""
        candidates = ([self.state.player] if self.state.player else [])
        for group_name in ("friends", "employees", "temp_friends", "enemies"):
            candidates.extend(getattr(self.state, group_name, []) or [])
        if getattr(self.state, "merchant", None) is not None:
            candidates.append(self.state.merchant)
        return next((entity for entity in candidates if entity is not None
                     and getattr(entity, "runtime_id", "") == actor_id), None)

    @ACTION_REGISTRY.action("world_vision", group="world",
                          allowed_phases=("pre_battle", "in_world"))
    def _action_world_vision(self, params: dict) -> dict:
        try:
            world = self._world_from_state()
            actor_id = params.get("actor_id")
            player_id = next((key for key, actor in world.actors.items() if actor.kind == "player"), None)
            actor_id = params.get("actor_id") or player_id
            if actor_id is None or actor_id != player_id:
                return {"success": False, "error": "只能查看轮回者自己的视野"}
            visible = world.visible_cells(actor_id)
            self._share_created_being_vision(world)
            self._sync_world_entity_positions(world)
            self.state.world_state = world.to_dict()
            return {"success": True, "action": "world_vision",
                    "result": {"actor_id": actor_id, "visible_cells": [list(p) for p in visible],
                               "known_cells": sorted(world.actors[actor_id].known_cells),
                               "reported_sightings": [
                                   dict(item) for item in world.actors[actor_id].memory
                                   if item.get("reported_by")]}}
        except (ValueError, KeyError, TypeError) as exc:
            return {"success": False, "error": str(exc)}

