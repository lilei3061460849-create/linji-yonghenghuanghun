"""开放世界基础状态与网格规则。

该模块只负责可验证的世界事实：时间、地图、单位位置、视野、寻路、遭遇和事件账本。
AI 只能提出意图；移动等操作必须通过本模块校验后才会改变状态。
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
import random
from typing import Any


Coord = tuple[int, int]
DIRECTIONS: tuple[Coord, ...] = ((0, -1), (0, 1), (-1, 0), (1, 0))

# 地图上会自主行动的友方单位类别（2026-10-24）。玩家对它们只能提"建议"。
ALLY_KINDS: frozenset[str] = frozenset({"friend", "employee", "temp_friend"})
# 建议必须说得出口：超过这个曼哈顿距离就传不过去。
ALLY_SUGGESTION_RANGE = 5

# 建议类型 → 该单位被采纳后的 behavior。
ALLY_SUGGESTIONS: dict[str, str] = {
    "follow": "follow",      # 跟着我
    "guard": "guard",        # 守在这里
    "explore": "explore",    # 自己去看看
    "hold": "guard",         # 原地待命（= 守卫当前格）
}

# 关系基础采纳率：临时朋友是被胁迫来的，听话程度最低。
ALLY_BASE_ACCEPTANCE: dict[str, int] = {
    "friend": 65,
    "employee": 55,
    "temp_friend": 40,
}


def suggestion_acceptance_percent(
    kind: str,
    suggestion: str,
    *,
    current_behavior: str = "explore",
    hp_ratio: float = 1.0,
    player_hp_ratio: float = 1.0,
    distance: int = 1,
) -> int:
    """友方单位对一条"建议"的自主采纳概率（5%~95%，整数百分比）。

    这是**建议**不是命令：玩家提交的只是意向，是否照做由本单位按自身处境判定。
    影响因素都是单位自己能感知的事实：
      * 关系基础值（朋友 > 员工 > 临时朋友）；
      * 自身伤势：健康照做，重伤先自保；
      * 建议与其当前行为一致 → 顺水推舟；
      * 玩家的处境：队长快死了，跟着他没好处；
      * 守卫点/集合点的距离：太远的要求更容易被敷衍。
    """
    if suggestion not in ALLY_SUGGESTIONS:
        raise ValueError(f"未知建议类型：{suggestion!r}")
    chance = ALLY_BASE_ACCEPTANCE.get(kind, 50)
    if hp_ratio >= 0.6:
        chance += 10
    elif hp_ratio < 0.3:
        chance -= 20
    target_behavior = ALLY_SUGGESTIONS[suggestion]
    if current_behavior == target_behavior:
        chance += 25
    if player_hp_ratio < 0.3:
        chance -= 10
    if distance > 8:
        chance -= 15
    elif distance <= 2:
        chance += 5
    return max(5, min(95, int(chance)))


@dataclass
class WorldClock:
    turn: int = 0

    def advance(self, amount: int = 1) -> int:
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
            raise ValueError("世界时间推进量必须是非负整数")
        self.turn += amount
        return self.turn


@dataclass
class WorldMap:
    width: int
    height: int
    terrain: dict[str, str] = field(default_factory=dict)
    opaque_terrain: set[str] = field(default_factory=lambda: {"wall", "building"})
    locations: dict[str, dict[str, Any]] = field(default_factory=dict)
    version: int = 1

    @staticmethod
    def key(pos: Coord) -> str:
        return f"{int(pos[0])},{int(pos[1])}"

    @staticmethod
    def parse(key: str) -> Coord:
        x, y = key.split(",", 1)
        return int(x), int(y)

    @classmethod
    def generate(cls, width: int = 21, height: int = 21, *, seed: int = 0,
                 obstacle_rate: float = 0.12) -> "WorldMap":
        if width < 3 or height < 3:
            raise ValueError("地图宽高至少为3")
        if not 0 <= obstacle_rate < 0.5:
            raise ValueError("障碍比例必须在[0, 0.5)范围内")
        rng = random.Random(seed)
        terrain: dict[str, str] = {}
        for y in range(height):
            for x in range(width):
                pos = (x, y)
                # 保证边界与起点区域可通行；其余地形按种子随机生成。
                if x == 0 or y == 0 or x == width - 1 or y == height - 1:
                    kind = "floor"
                elif x < 3 and y < 3:
                    kind = "floor"
                else:
                    roll = rng.random()
                    kind = "wall" if roll < obstacle_rate else ("ruin" if roll < obstacle_rate + 0.18 else "floor")
                terrain[cls.key(pos)] = kind

        # 保留随机墙体与废墟，但修补被墙完全隔断的可通行区域。
        # 否则随机地图会出现玩家永远走不到的事件点或出生在孤岛的NPC。
        def reachable_from_start() -> set[Coord]:
            start = (1, 1)
            seen = {start}
            queue = deque([start])
            while queue:
                x, y = queue.popleft()
                for nxt in ((x, y - 1), (x, y + 1), (x - 1, y), (x + 1, y)):
                    if (0 <= nxt[0] < width and 0 <= nxt[1] < height
                            and terrain.get(cls.key(nxt)) not in {"wall", "building", "void"}
                            and nxt not in seen):
                        seen.add(nxt)
                        queue.append(nxt)
            return seen

        while True:
            reachable = reachable_from_start()
            disconnected = [
                (x, y) for y in range(height) for x in range(width)
                if terrain.get(cls.key((x, y))) not in {"wall", "building", "void"}
                and (x, y) not in reachable
            ]
            if not disconnected:
                break
            source = min(disconnected, key=lambda pos: (
                min(abs(pos[0] - other[0]) + abs(pos[1] - other[1]) for other in reachable),
                pos[1], pos[0]))
            goal = min(reachable, key=lambda pos: (
                abs(source[0] - pos[0]) + abs(source[1] - pos[1]), pos[1], pos[0]))
            cursor = source
            while cursor != goal:
                if cursor[0] != goal[0]:
                    cursor = (cursor[0] + (1 if goal[0] > cursor[0] else -1), cursor[1])
                else:
                    cursor = (cursor[0], cursor[1] + (1 if goal[1] > cursor[1] else -1))
                key = cls.key(cursor)
                if terrain.get(key) == "wall":
                    terrain[key] = "floor"
        return cls(width=width, height=height, terrain=terrain)

    def in_bounds(self, pos: Coord) -> bool:
        return 0 <= pos[0] < self.width and 0 <= pos[1] < self.height

    def terrain_at(self, pos: Coord) -> str:
        if not self.in_bounds(pos):
            return "void"
        return self.terrain.get(self.key(pos), "void")

    def is_passable(self, pos: Coord) -> bool:
        return self.in_bounds(pos) and self.terrain_at(pos) not in {"wall", "building", "void"}

    def neighbors(self, pos: Coord) -> list[Coord]:
        return [q for dx, dy in DIRECTIONS if self.is_passable(q := (pos[0] + dx, pos[1] + dy))]

    def find_path(self, start: Coord, goal: Coord) -> list[Coord] | None:
        """返回包含起点、不包含起点外绕行信息的最短路径；不可达时返回None。"""
        if not self.is_passable(start) or not self.is_passable(goal):
            return None
        if start == goal:
            return [start]
        queue = deque([start])
        parent: dict[Coord, Coord | None] = {start: None}
        while queue:
            cur = queue.popleft()
            for nxt in self.neighbors(cur):
                if nxt in parent:
                    continue
                parent[nxt] = cur
                if nxt == goal:
                    path = [goal]
                    while path[-1] != start:
                        path.append(parent[path[-1]])  # type: ignore[arg-type]
                    return list(reversed(path))
                queue.append(nxt)
        return None

    def add_location(self, location_id: str, location: dict[str, Any]) -> None:
        if not location_id or location_id in self.locations:
            raise ValueError("地点ID必须非空且唯一")
        pos = (int(location.get("x", -1)), int(location.get("y", -1)))
        if not self.is_passable(pos):
            raise ValueError("地点必须位于可通行格")
        if any((int(existing.get("x", -1)), int(existing.get("y", -1))) == pos
               for existing in self.locations.values()):
            raise ValueError("同一格不能放置多个特殊地点")
        value = dict(location)
        value.update({"x": pos[0], "y": pos[1]})
        value.setdefault("discovered", False)
        value.setdefault("active", True)
        self.locations[location_id] = value

    def location_at(self, pos: Coord) -> tuple[str, dict[str, Any]] | None:
        for location_id, location in self.locations.items():
            if (int(location.get("x", -1)), int(location.get("y", -1))) == pos:
                return location_id, location
        return None

    def adjacent_vision(self, pos: Coord, radius: int = 1) -> list[Coord]:
        """按上下左右的曼哈顿距离计算视野；障碍本身可见，但会阻断其后的视线。"""
        radius = max(1, int(radius))
        visible: set[Coord] = set()
        queue = deque([(pos, 0)])
        visited = {pos}
        while queue:
            current, distance = queue.popleft()
            if distance >= radius:
                continue
            # 墙体/建筑本身可以被看见，但不会继续向其背后扩散视野。
            if distance > 0 and self.terrain_at(current) in self.opaque_terrain:
                continue
            for dx, dy in DIRECTIONS:
                nxt = (current[0] + dx, current[1] + dy)
                if not self.in_bounds(nxt) or nxt in visited:
                    continue
                visited.add(nxt)
                visible.add(nxt)
                queue.append((nxt, distance + 1))
        return sorted(visible, key=lambda cell: (cell[1], cell[0]))

    def to_dict(self) -> dict[str, Any]:
        return {
            "width": self.width, "height": self.height,
            "terrain": dict(self.terrain),
            "opaque_terrain": sorted(self.opaque_terrain),
            "locations": {key: dict(value) for key, value in self.locations.items()},
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorldMap":
        return cls(
            width=int(data["width"]), height=int(data["height"]),
            terrain={str(k): str(v) for k, v in data.get("terrain", {}).items()},
            opaque_terrain=set(data.get("opaque_terrain", ["wall", "building"])),
            locations={str(k): dict(v) for k, v in data.get("locations", {}).items()},
            version=int(data.get("version", 1)),
        )


@dataclass
class WorldActorState:
    actor_id: str
    name: str
    kind: str
    x: int
    y: int
    alive: bool = True
    faction: str = "neutral"
    mode: str = "autonomous"
    # 造物行为模式：explore=自主探索，follow=跟随主人，guard=守卫指定格。
    behavior: str = "explore"
    owner_actor_id: str = ""
    # 视野半径按上下左右的曼哈顿距离计；造物每次培养提高1。
    vision_range: int = 1
    guard_position: Coord | None = None
    known_cells: set[str] = field(default_factory=set)
    visited_cells: set[str] = field(default_factory=set)
    memory: list[dict[str, Any]] = field(default_factory=list)

    @property
    def position(self) -> Coord:
        return self.x, self.y

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["known_cells"] = sorted(self.known_cells)
        data["visited_cells"] = sorted(self.visited_cells)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorldActorState":
        values = dict(data)
        values["known_cells"] = set(values.get("known_cells", []))
        values["visited_cells"] = set(values.get("visited_cells", []))
        values["memory"] = list(values.get("memory", []))
        guard_position = values.get("guard_position")
        values["guard_position"] = tuple(guard_position) if guard_position is not None else None
        values.setdefault("behavior", "explore")
        values.setdefault("owner_actor_id", "")
        values.setdefault("vision_range", 1)
        return cls(**values)


@dataclass
class EncounterState:
    encounter_id: str
    location: Coord
    participants: list[str]
    started_turn: int
    active: bool = True
    outcome: str = ""
    final_duel: bool = False


@dataclass
class WorldState:
    clock: WorldClock
    map: WorldMap
    actors: dict[str, WorldActorState] = field(default_factory=dict)
    encounters: dict[str, EncounterState] = field(default_factory=dict)
    event_log: list[dict[str, Any]] = field(default_factory=list)
    next_event_id: int = 1
    next_encounter_id: int = 1
    # 每10个世界回合移除方形地图最外一圈；计数随存档保存。
    shrink_interval: int = 10
    shrink_count: int = 0
    final_duel_search: bool = False
    final_duel_finalists: list[str] = field(default_factory=list)
    final_duel_started: bool = False
    final_duel_complete: bool = False
    final_duel_winner: str = ""
    # 雇佣信号跨回合保存；轮回者可以离开信号发出地点继续行动。
    hiring_signals: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def create(cls, *, width: int = 21, height: int = 21, seed: int = 0) -> "WorldState":
        return cls(clock=WorldClock(), map=WorldMap.generate(width, height, seed=seed))

    def record(self, event_type: str, actor_id: str | None, **details: Any) -> dict[str, Any]:
        event = {
            "event_id": self.next_event_id,
            "world_turn": self.clock.turn,
            "type": event_type,
            "actor_id": actor_id,
            "details": details,
        }
        self.next_event_id += 1
        self.event_log.append(event)
        return event

    def add_actor(self, actor: WorldActorState) -> None:
        if not actor.actor_id or actor.actor_id in self.actors:
            raise ValueError("单位ID必须非空且唯一")
        if not self.map.is_passable(actor.position):
            raise ValueError("单位出生位置必须在可通行格")
        # 同格占位合法；敌对单位是否开战由遭遇检测统一处理。
        actor.visited_cells.add(self.map.key(actor.position))
        self.actors[actor.actor_id] = actor
        self.record("actor_spawned", actor.actor_id, name=actor.name, kind=actor.kind,
                    position=list(actor.position))

    def visible_cells(self, actor_id: str) -> list[Coord]:
        actor = self._living_actor(actor_id)
        cells = [actor.position] + self.map.adjacent_vision(actor.position, actor.vision_range)
        visible = set(cells)
        # 角色只能更新自己的情报；不共享其他角色的视野。
        actor.known_cells.update(self.map.key(p) for p in cells)
        for other in self.actors.values():
            if not other.alive or other.actor_id == actor_id or other.position not in visible:
                continue
            observation = {
                "turn": self.clock.turn,
                "actor_id": other.actor_id,
                "name": other.name,
                "kind": other.kind,
                "faction": other.faction,
                "position": list(other.position),
            }
            # 同一世界回合可能因多段结算多次刷新视野；地点记忆夹在中间时，
            # 仍按“目标+回合”去重，避免把同一目击重复塞满有限记忆。
            same_turn = next((entry for entry in reversed(actor.memory)
                              if entry.get("actor_id") == other.actor_id
                              and entry.get("turn") == self.clock.turn
                              and entry.get("kind") != "location_sighting"), None)
            if same_turn is None:
                actor.memory.append(observation)
                actor.memory = actor.memory[-50:]
                self.record("actor_seen", actor_id, target_actor_id=other.actor_id,
                            name=other.name, kind=other.kind, position=list(other.position))
            elif same_turn != observation:
                same_turn.update(observation)
        return cells

    def deactivate_location_sightings(self, location_id: str, *, reason: str = "inactive") -> None:
        """地点失效时同步所有单位的旧目击记录，避免NPC追逐已消失的目标。"""
        for actor in self.actors.values():
            for memory in actor.memory:
                if (memory.get("kind") == "location_sighting"
                        and memory.get("location_id") == location_id):
                    memory["active"] = False
                    memory["inactive_reason"] = reason
                    if reason == "removed_by_shrink":
                        memory["removed_by_shrink"] = True

    def _shrink_boundary_if_due(self) -> dict[str, Any] | None:
        """世界回合到达固定边界时，移除当前方形地图最外一圈。"""
        if self.shrink_interval < 1 or self.clock.turn <= 0:
            return None
        if self.clock.turn % self.shrink_interval != 0:
            return None
        ring = self.shrink_count
        if ring * 2 >= min(self.map.width, self.map.height) - 1:
            return None
        removed: list[Coord] = []
        for y in range(self.map.height):
            for x in range(self.map.width):
                if x == ring or y == ring or x == self.map.width - 1 - ring or y == self.map.height - 1 - ring:
                    pos = (x, y)
                    key = self.map.key(pos)
                    if self.map.terrain.get(key) != "void":
                        self.map.terrain[key] = "void"
                        removed.append(pos)
        removed_location_ids: set[str] = set()
        for location_id, location in self.map.locations.items():
            pos = (int(location.get("x", -1)), int(location.get("y", -1)))
            if pos in removed and location.get("active", True):
                location["active"] = False
                location["removed_by_shrink"] = True
                removed_location_ids.add(location_id)
                self.record("location_removed_by_shrink", None,
                            location_id=location_id, position=list(pos))
        # NPC只根据自己的记忆决策。若地点被边界吞没，必须同步其旧目击记录，
        # 否则它会把已失效的资源/遗物当作目标，持续向地图外缘移动。
        for location_id in removed_location_ids:
            self.deactivate_location_sightings(location_id, reason="removed_by_shrink")
        caught = [actor for actor in self.actors.values()
                  if actor.alive and actor.position in set(removed)]
        self.shrink_count += 1
        self.map.version += 1
        self.record("world_ring_shrunk", None, ring=ring,
                    removed_cells=len(removed), caught_actor_ids=[a.actor_id for a in caught])
        # 单位不会凭空瞬移或直接死亡；被圈层追上时记录危险状态，仍可向内撤离。
        for actor in caught:
            self.record("actor_caught_by_shrinking_boundary", actor.actor_id,
                        ring=ring, position=list(actor.position))
        next_ring = self.shrink_count
        next_turn = (next_ring * self.shrink_interval
                     if next_ring * 2 < min(self.map.width, self.map.height) - 1
                     else None)
        return {"ring": ring, "removed_cells": len(removed),
                "caught_actor_ids": [actor.actor_id for actor in caught],
                "next_shrink_turn": next_turn}

    def move_actor(self, actor_id: str, destination: Coord) -> dict[str, Any]:
        actor = self._living_actor(actor_id)
        if any(encounter.active and actor_id in encounter.participants
               for encounter in self.encounters.values()):
            raise ValueError("当前处于遭遇中，不能直接移动；请先处理遭遇")
        dx = abs(destination[0] - actor.x)
        dy = abs(destination[1] - actor.y)
        if dx + dy != 1:
            raise ValueError("每次移动只能到上下左右相邻一格")
        if not self.map.is_passable(destination):
            raise ValueError("目标格不可通行")
        # 世界规则允许多个单位占据同一格；进入同格后立即检查敌对遭遇。
        origin = actor.position
        actor.x, actor.y = destination
        actor.visited_cells.add(self.map.key(destination))
        self.clock.advance(1)
        shrink = self._shrink_boundary_if_due()
        self.record("actor_moved", actor_id, origin=list(origin), destination=list(destination))
        self.visible_cells(actor_id)
        encounters = self.detect_adjacent_encounters(actor_id)
        interactions = self.detect_adjacent_interactions(actor_id, origin)
        self._run_autonomous_units()
        # 返回值只报告涉及行动者的遭遇，不泄露地图另一端的隐藏战斗。
        encounters = sorted(set(encounters + [
            encounter.encounter_id for encounter in self.encounters.values()
            if encounter.active and actor_id in encounter.participants
        ]))
        result = {"actor_id": actor_id, "from": list(origin), "to": list(destination),
                  "world_turn": self.clock.turn, "new_encounters": encounters,
                  "new_interactions": interactions}
        if shrink is not None:
            result["boundary_shrunk"] = shrink
        return result

    def _find_known_path(self, actor_id: str, start: Coord, goal: Coord) -> list[Coord] | None:
        """只在该单位已知的可通行格中寻路，不读取迷雾里的地形。"""
        actor = self.actors.get(actor_id)
        if actor is None:
            return None
        known = {self.map.parse(key) for key in actor.known_cells}
        known.add(start)
        if goal not in known or not self.map.is_passable(goal):
            return None
        queue = deque([start])
        previous: dict[Coord, Coord | None] = {start: None}
        while queue:
            current = queue.popleft()
            if current == goal:
                break
            x, y = current
            for nxt in ((x, y - 1), (x, y + 1), (x - 1, y), (x + 1, y)):
                if (nxt in known and nxt not in previous
                        and self.map.is_passable(nxt)):
                    previous[nxt] = current
                    queue.append(nxt)
        if goal not in previous:
            return None
        path = []
        current: Coord | None = goal
        while current is not None:
            path.append(current)
            current = previous[current]
        path.reverse()
        return path

    def _run_autonomous_units(self) -> list[dict[str, Any]]:
        """推进当前世界回合的NPC行动；每个单位只使用自己的可见信息。"""
        generated: list[dict[str, Any]] = []
        # 临时同盟按世界回合到期；即使一次等待跨过多个回合，也不会额外多持续。
        for actor in self.actors.values():
            alliance = next((item for item in actor.memory
                             if item.get("kind") == "temporary_alliance"
                             and not item.get("expired")), None)
            if alliance is not None and self.clock.turn >= int(alliance.get("expires_turn", 0)):
                actor.kind = str(alliance.get("original_kind", "enemy"))
                actor.faction = str(alliance.get("original_faction", "enemy"))
                actor.owner_actor_id = ""
                actor.behavior = "explore"
                actor.mode = "autonomous"
                alliance["expired"] = True
                self.record("temporary_alliance_expired", actor.actor_id,
                            name=actor.name, expired_turn=self.clock.turn)
        # 一旦玩家进入遭遇，世界行动立即中断，不能让同一回合继续偷跑其他NPC。
        player_ids = {actor.actor_id for actor in self.actors.values()
                      if actor.kind == "player" and actor.alive}
        if any(encounter.active and player_ids.intersection(encounter.participants)
               for encounter in self.encounters.values()):
            return generated
        # 先登记已经相邻的敌对关系，遭遇中的单位不会再被NPC调度移动。
        self.detect_adjacent_encounters()
        if any(encounter.active and player_ids.intersection(encounter.participants)
               for encounter in self.encounters.values()):
            return generated
        # 不按随机UUID调度NPC；固定按地图位置和显示名排序，避免同一随机种子下行动顺序漂移。
        for actor_id in sorted(
                self.actors,
                key=lambda aid: (self.actors[aid].y, self.actors[aid].x,
                                self.actors[aid].name, aid)):
            actor = self.actors[actor_id]
            if not actor.alive or actor.kind == "player" or actor.mode != "autonomous":
                continue
            if any(e.active and actor_id in e.participants for e in self.encounters.values()):
                continue
            visible = set(self.visible_cells(actor_id))
            # 地点情报是单位自己的记忆，不把全地图位置直接塞进NPC决策。
            for location_id, location in self.map.locations.items():
                location_pos = (int(location.get("x", -1)), int(location.get("y", -1)))
                if location_pos not in visible:
                    continue
                sighting = next((entry for entry in reversed(actor.memory)
                                 if entry.get("kind") == "location_sighting"
                                 and entry.get("location_id") == location_id), None)
                observation = {
                    "kind": "location_sighting",
                    "location_id": location_id,
                    "position": list(location_pos),
                    "location_kind": location.get("kind", "special"),
                    "resource_type": location.get("resource_type", ""),
                    "active": bool(location.get("active", True)),
                    "turn": self.clock.turn,
                }
                if sighting is None:
                    actor.memory.append(observation)
                else:
                    sighting.update(observation)
            if len(actor.memory) > 50:
                actor.memory = actor.memory[-50:]
            targets = [
                other for other in self.actors.values()
                if other.alive and other.actor_id != actor_id
                and other.position in visible and self._hostile_pair(actor, other)
            ]
            if targets:
                created = self.detect_adjacent_encounters(actor_id)
                if created:
                    generated.extend({"type": "encounter_started", "encounter_id": eid}
                                     for eid in created)
                if any(e.active and actor_id in e.participants for e in self.encounters.values()):
                    continue
            occupied = {other.position for other in self.actors.values()
                        if other.alive and other.actor_id != actor_id}
            candidates = [p for p in self.map.neighbors(actor.position)
                          if p not in occupied]
            if not candidates:
                continue
            # 造物可自主探索、跟随主人或守卫指定地点。行为只使用已知主人坐标与本地寻路。
            destination = None
            # 追踪最近亲眼目击的敌对单位，但只使用记忆中的位置；情报超过3回合便失效，
            # 避免NPC凭空读取迷雾里的实时坐标，也避免无限追逐过期目标。
            latest_sightings: dict[str, dict[str, Any]] = {}
            for entry in actor.memory:
                target_id = str(entry.get("actor_id", ""))
                if (not target_id or target_id == actor_id
                        or not isinstance(entry.get("position"), (list, tuple))
                        or len(entry["position"]) != 2):
                    continue
                seen_turn = int(entry.get("turn", -1))
                if seen_turn < 0 or self.clock.turn - seen_turn > 3 or seen_turn > self.clock.turn:
                    continue
                previous = latest_sightings.get(target_id)
                if previous is None or int(previous.get("turn", -1)) < seen_turn:
                    latest_sightings[target_id] = entry
            hostile_goals = []
            for target_id, entry in latest_sightings.items():
                goal_pos = (int(entry["position"][0]), int(entry["position"][1]))
                if (goal_pos == actor.position or not self.map.in_bounds(goal_pos)
                        or not self.map.is_passable(goal_pos)):
                    continue
                remembered_target = WorldActorState(
                    actor_id=target_id, name=str(entry.get("name", "")),
                    kind=str(entry.get("kind", "enemy")),
                    x=goal_pos[0], y=goal_pos[1], alive=True,
                    faction=str(entry.get("faction", "neutral")))
                if self._hostile_pair(actor, remembered_target):
                    distance = abs(goal_pos[0] - actor.x) + abs(goal_pos[1] - actor.y)
                    hostile_goals.append((distance, -int(entry.get("turn", 0)),
                                          goal_pos[1], goal_pos[0], target_id, goal_pos))
            if hostile_goals:
                _, _, _, _, _, goal_pos = min(hostile_goals)
                path = self._find_known_path(actor_id, actor.position, goal_pos)
                if path is not None and len(path) > 1 and path[1] in candidates:
                    destination = path[1]
                if destination is None:
                    current_distance = abs(goal_pos[0] - actor.x) + abs(goal_pos[1] - actor.y)
                    closer = [candidate for candidate in candidates
                              if abs(goal_pos[0] - candidate[0])
                              + abs(goal_pos[1] - candidate[1]) < current_distance]
                    if closer:
                        destination = min(closer, key=lambda pos: (
                            abs(goal_pos[0] - pos[0]) + abs(goal_pos[1] - pos[1]),
                            self.map.key(pos) in actor.visited_cells, pos[1], pos[0]))
            # 轮回者阵营与可拾取资源的友方单位，会优先追踪自己亲眼发现的资源/遗物。
            # 目标来源只取本单位记忆；未知地点不会因为地图数据存在而被NPC直接利用。
            if (destination is None
                    and actor.faction in {"enemy", "friend", "employee", "temp_friend"}
                    and not (self.final_duel_search and actor_id in self.final_duel_finalists)
                    # 明确指挥造物跟随/守卫时，服从命令优先于自主搜刮资源。
                    and not (actor.kind in ALLY_KINDS and actor.owner_actor_id
                             and actor.behavior in {"follow", "guard"})):
                remembered_goals = [
                    entry for entry in actor.memory
                    if entry.get("kind") == "location_sighting"
                    and entry.get("active", False)
                    and (entry.get("location_kind") == "relic"
                         or (entry.get("location_kind") == "resource"
                             and entry.get("resource_type") == "shards"))
                ]
                remembered_goals.sort(key=lambda entry: (
                    0 if entry.get("location_kind") == "relic" else 1,
                    abs(int(entry["position"][0]) - actor.x)
                    + abs(int(entry["position"][1]) - actor.y),
                    -int(entry.get("turn", 0)),
                    str(entry.get("location_id", "")),
                ))
                for goal in remembered_goals:
                    goal_pos = tuple(goal.get("position", []))
                    if len(goal_pos) != 2 or goal_pos == actor.position:
                        continue
                    path = self._find_known_path(actor_id, actor.position, goal_pos)
                    if path and len(path) > 1:
                        preferred = path[1]
                        if preferred in candidates:
                            destination = preferred
                            break
                    # 没有已知路线时只按相邻格和已知目标坐标做局部推断；
                    # 不使用完整地图的寻路结果猜测迷雾中的墙体分布。
                    current_distance = abs(goal_pos[0] - actor.x) + abs(goal_pos[1] - actor.y)
                    closer = [candidate for candidate in candidates
                              if abs(goal_pos[0] - candidate[0])
                              + abs(goal_pos[1] - candidate[1]) < current_distance]
                    if closer:
                        destination = min(
                            closer,
                            key=lambda pos: (
                                abs(goal_pos[0] - pos[0]) + abs(goal_pos[1] - pos[1]),
                                self.map.key(pos) in actor.visited_cells,
                                pos[1], pos[0]))
                        break
            # 2026-10-24：朋友/员工/临时朋友也上地图自主行动（此前 mode=manual 原地不动）。
            # 玩家只能"建议"，是否照做由单位自己按 `suggestion_acceptance_percent` 判定。
            if (actor.kind in ALLY_KINDS and actor.owner_actor_id):
                owner = self.actors.get(actor.owner_actor_id)
                target_position = None
                if actor.behavior == "follow" and owner is not None and owner.alive:
                    target_position = owner.position
                    # 跟到身边就停，不踩到主人那一格（否则两人叠在一格，地图上看不见玩家）。
                    if (abs(actor.x - owner.x) + abs(actor.y - owner.y)) <= 1:
                        continue
                elif actor.behavior == "guard":
                    target_position = actor.guard_position or actor.position
                if target_position is not None and target_position != actor.position:
                    path = self._find_known_path(actor_id, actor.position, target_position)
                    if path and len(path) > 1:
                        preferred = path[1]
                        if preferred in candidates or preferred == target_position:
                            destination = preferred
                    if destination is None:
                        # 主人位置/守卫点是已知目标，但未知地形不能直接参与寻路。
                        # 仅根据当前可见的相邻格尝试接近，遇墙后再探索新的可见区域。
                        current_distance = (abs(target_position[0] - actor.x)
                                            + abs(target_position[1] - actor.y))
                        closer = [candidate for candidate in candidates
                                  if abs(target_position[0] - candidate[0])
                                  + abs(target_position[1] - candidate[1]) < current_distance]
                        if closer:
                            destination = min(
                                closer,
                                key=lambda pos: (
                                    abs(target_position[0] - pos[0])
                                    + abs(target_position[1] - pos[1]),
                                    self.map.key(pos) in actor.visited_cells,
                                    pos[1], pos[0]))
                if (actor.behavior in {"follow", "guard"}
                        and target_position == actor.position):
                    continue
            # 最终会合者先向地图中心收敛；到达中心附近后，继续按自己的迷雾记忆
            # 搜索已知的未探索格，避免因中心格被占或中心已到达而在周围反复打转。
            # 若曾目击另一名决斗者，优先沿已知地形前往其最后目击位置，不读取其当前隐藏坐标。
            if destination is None and self.final_duel_search and actor_id in self.final_duel_finalists:
                rival_ids = [item for item in self.final_duel_finalists if item != actor_id]
                rival_id = rival_ids[0] if rival_ids else None
                sightings = [entry for entry in actor.memory
                             if entry.get("actor_id") == rival_id
                             and isinstance(entry.get("position"), (list, tuple))
                             and len(entry["position"]) == 2]
                if sightings:
                    last_seen = max(sightings, key=lambda entry: int(entry.get("turn", 0)))
                    goal = (int(last_seen["position"][0]), int(last_seen["position"][1]))
                    path = self._find_known_path(actor_id, actor.position, goal)
                    if path is not None and len(path) > 1:
                        destination = path[1]

                center = (self.map.width // 2, self.map.height // 2)
                center_distance = abs(actor.x - center[0]) + abs(actor.y - center[1])
                if destination is None and center_distance > 1:
                    closer = [pos for pos in candidates
                              if abs(pos[0] - center[0]) + abs(pos[1] - center[1]) < center_distance]
                    if closer:
                        destination = min(
                            closer,
                            key=lambda pos: (
                                abs(pos[0] - center[0]) + abs(pos[1] - center[1]),
                                self.map.key(pos) in actor.visited_cells,
                                pos[1], pos[0]))
                if destination is None:
                    known_unvisited = sorted(
                        (self.map.parse(key) for key in actor.known_cells
                         if key not in actor.visited_cells and self.map.is_passable(self.map.parse(key))),
                        key=lambda pos: (abs(pos[0] - actor.x) + abs(pos[1] - actor.y), pos[1], pos[0]))
                    for goal in known_unvisited:
                        path = self._find_known_path(actor_id, actor.position, goal)
                        if path is not None and len(path) > 1:
                            destination = path[1]
                            break
                    if destination is None:
                        destination = min(candidates, key=lambda pos: (
                            self.map.key(pos) in actor.visited_cells, pos[1], pos[0]))
            elif destination is None:
                unvisited = [pos for pos in candidates
                             if self.map.key(pos) not in actor.visited_cells]
                choices = unvisited or candidates
                stable_identity = sum(ord(char) for char in actor.name) + actor.x * 31 + actor.y * 17
                offset = (self.clock.turn + stable_identity) % len(choices)
                destination = choices[offset]
            origin = actor.position
            actor.x, actor.y = destination
            actor.visited_cells.add(self.map.key(destination))
            self.record("actor_moved", actor_id, origin=list(origin),
                        destination=list(destination), autonomous=True)
            self.visible_cells(actor_id)
            generated.append({"type": "actor_moved", "actor_id": actor_id,
                              "from": list(origin), "to": list(destination)})
            created = self.detect_adjacent_encounters(actor_id)
            generated.extend({"type": "encounter_started", "encounter_id": eid}
                             for eid in created)
            if any(encounter.active and player_ids.intersection(encounter.participants)
                   for encounter in self.encounters.values()):
                break
        return generated

    @staticmethod
    def _hostile_pair(a: WorldActorState, b: WorldActorState) -> bool:
        if not a.alive or not b.alive or a.faction == "neutral" or b.faction == "neutral":
            return False
        if a.faction == "monster" or b.faction == "monster":
            other = b if a.faction == "monster" else a
            # 怪物威胁所有轮回者阵营；不能只追杀玩家而放过地图上的其他轮回者。
            return other.faction in {"player", "enemy", "friend", "employee", "temp_friend"}
        friendly = {"player", "friend", "employee", "temp_friend"}
        return ((a.faction in friendly and b.faction == "enemy")
                or (b.faction in friendly and a.faction == "enemy"))

    def detect_adjacent_encounters(self, actor_id: str | None = None) -> list[str]:
        """登记敌对遭遇；每个单位同一时刻只进入一个活跃遭遇。

        同格敌对单位先合并为一个遭遇，避免多个敌人占据同一格时为同一玩家
        创建数个互相锁死的战斗。相邻的其他敌人会在当前遭遇结束后再被登记。
        """
        created: list[str] = []
        living = [a for a in self.actors.values() if a.alive]
        engaged_ids = {
            participant_id
            for encounter in self.encounters.values() if encounter.active
            for participant_id in encounter.participants
        }

        # 先处理同格单位。一个同格簇中，只把通过敌对关系相连的单位放进同一场遭遇。
        by_position: dict[Coord, list[WorldActorState]] = {}
        for actor in living:
            by_position.setdefault(actor.position, []).append(actor)
        for position, occupants in by_position.items():
            if actor_id is not None and all(actor.actor_id != actor_id for actor in occupants):
                continue
            remaining = {actor.actor_id: actor for actor in occupants
                         if actor.actor_id not in engaged_ids}
            while remaining:
                seed_id = next(iter(remaining))
                component_ids = {seed_id}
                frontier = [seed_id]
                while frontier:
                    current_id = frontier.pop()
                    current = remaining.get(current_id)
                    if current is None:
                        continue
                    for other_id, other in list(remaining.items()):
                        if other_id in component_ids:
                            continue
                        if self._hostile_pair(current, other):
                            component_ids.add(other_id)
                            frontier.append(other_id)
                component = [actor for item, actor in remaining.items()
                             if item in component_ids]
                if len(component) >= 2:
                    encounter = self.start_encounter(
                        [actor.actor_id for actor in component], position)
                    created.append(encounter.encounter_id)
                    engaged_ids.update(component_ids)
                for item in component_ids:
                    remaining.pop(item, None)
                # Isolated occupants are not hostile to anyone at this position.
                if len(component) == 1:
                    continue

        # 仍未参战的单位按正交相邻关系触发遭遇；同一单位不会被重复锁入另一场。
        for index, a in enumerate(living):
            if a.actor_id in engaged_ids:
                continue
            for b in living[index + 1:]:
                if b.actor_id in engaged_ids:
                    continue
                if actor_id is not None and actor_id not in {a.actor_id, b.actor_id}:
                    continue
                if abs(a.x - b.x) + abs(a.y - b.y) != 1:
                    continue
                if not self._hostile_pair(a, b):
                    continue
                encounter = self.start_encounter([a.actor_id, b.actor_id], b.position)
                created.append(encounter.encounter_id)
                engaged_ids.update((a.actor_id, b.actor_id))
                break
        return created

    def detect_adjacent_interactions(self, actor_id: str, origin: Coord) -> list[dict[str, Any]]:
        """进入新相邻格时记录一次非敌对交互机会；不会自动替角色选择交易/合作等结果。"""
        actor = self._living_actor(actor_id)
        interactions: list[dict[str, Any]] = []
        for other in self.actors.values():
            if not other.alive or other.actor_id == actor_id:
                continue
            was_adjacent = abs(other.x - origin[0]) + abs(other.y - origin[1]) == 1
            is_adjacent = abs(other.x - actor.x) + abs(other.y - actor.y) == 1
            if not is_adjacent or was_adjacent or self._hostile_pair(actor, other):
                continue
            event = self.record("interaction_triggered", actor_id,
                                target_actor_id=other.actor_id, target_name=other.name,
                                target_kind=other.kind, position=list(other.position))
            outcomes = ["交谈", "交易", "合作", "攻击"]
            if actor.faction == "monster" and other.kind == "merchant":
                outcomes = ["交谈", "交易", "合作"]
            interactions.append({
                "event_id": event["event_id"], "actor_id": actor_id,
                "target_actor_id": other.actor_id, "target_name": other.name,
                "target_kind": other.kind, "position": list(other.position),
                "available_outcomes": outcomes,
            })
        return interactions

    def wait(self, actor_id: str, turns: int = 1) -> int:
        self._living_actor(actor_id)
        if not isinstance(turns, int) or isinstance(turns, bool) or turns < 1:
            raise ValueError("等待回合数必须是正整数")
        if any(encounter.active and actor_id in encounter.participants
               for encounter in self.encounters.values()):
            raise ValueError("当前处于遭遇中，不能直接等待世界时间")
        for _ in range(turns):
            self.clock.advance(1)
            shrink = self._shrink_boundary_if_due()
            if shrink is not None:
                self.record("boundary_shrink_during_wait", actor_id, **shrink)
            self.record("world_waited", actor_id, turns=1)
            self._run_autonomous_units()
            if any(encounter.active and actor_id in encounter.participants
                   for encounter in self.encounters.values()):
                break
        return self.clock.turn

    def start_encounter(self, participant_ids: list[str], location: Coord) -> EncounterState:
        unique = list(dict.fromkeys(participant_ids))
        if len(unique) < 2:
            raise ValueError("遭遇至少需要两个不同单位")
        for actor_id in unique:
            self._living_actor(actor_id)
        if not self.map.in_bounds(location):
            raise ValueError("遭遇地点超出地图")
        is_final_duel = (self.final_duel_search
                         and set(self.final_duel_finalists).issubset(set(unique)))
        encounter = EncounterState(
            encounter_id=f"encounter-{self.next_encounter_id}",
            location=location, participants=unique, started_turn=self.clock.turn,
            final_duel=is_final_duel,
        )
        if is_final_duel:
            self.final_duel_started = True
        self.next_encounter_id += 1
        self.encounters[encounter.encounter_id] = encounter
        self.record("encounter_started", unique[0], encounter_id=encounter.encounter_id,
                    participants=unique, location=list(location), final_duel=is_final_duel)
        return encounter

    def finish_encounter(self, encounter_id: str, outcome: str) -> None:
        encounter = self.encounters.get(encounter_id)
        if encounter is None or not encounter.active:
            raise ValueError("遭遇不存在或已经结束")
        encounter.active = False
        encounter.outcome = outcome
        self.record("encounter_finished", None, encounter_id=encounter_id, outcome=outcome)

    def _living_actor(self, actor_id: str) -> WorldActorState:
        actor = self.actors.get(actor_id)
        if actor is None or not actor.alive:
            raise ValueError("单位不存在或已死亡")
        return actor

    def to_dict(self) -> dict[str, Any]:
        return {
            "clock": {"turn": self.clock.turn},
            "map": self.map.to_dict(),
            "actors": {key: actor.to_dict() for key, actor in self.actors.items()},
            "encounters": {key: asdict(value) for key, value in self.encounters.items()},
            "event_log": list(self.event_log),
            "next_event_id": self.next_event_id,
            "next_encounter_id": self.next_encounter_id,
            "shrink_interval": self.shrink_interval,
            "shrink_count": self.shrink_count,
            "final_duel_search": self.final_duel_search,
            "final_duel_finalists": list(self.final_duel_finalists),
            "final_duel_started": self.final_duel_started,
            "final_duel_complete": self.final_duel_complete,
            "final_duel_winner": self.final_duel_winner,
            "hiring_signals": [dict(signal) for signal in self.hiring_signals],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorldState":
        return cls(
            clock=WorldClock(int(data.get("clock", {}).get("turn", 0))),
            map=WorldMap.from_dict(data["map"]),
            actors={k: WorldActorState.from_dict(v) for k, v in data.get("actors", {}).items()},
            encounters={k: EncounterState(
                encounter_id=v["encounter_id"],
                location=tuple(v["location"]),
                participants=list(v["participants"]),
                started_turn=int(v["started_turn"]),
                active=bool(v.get("active", True)),
                outcome=str(v.get("outcome", "")),
                final_duel=bool(v.get("final_duel", False)),
            ) for k, v in data.get("encounters", {}).items()},
            event_log=list(data.get("event_log", [])),
            next_event_id=int(data.get("next_event_id", 1)),
            next_encounter_id=int(data.get("next_encounter_id", 1)),
            shrink_interval=max(1, int(data.get("shrink_interval", 10))),
            shrink_count=max(0, int(data.get("shrink_count", 0))),
            final_duel_search=bool(data.get("final_duel_search", False)),
            final_duel_finalists=list(data.get("final_duel_finalists") or []),
            final_duel_started=bool(data.get("final_duel_started", False)),
            final_duel_complete=bool(data.get("final_duel_complete", False)),
            final_duel_winner=str(data.get("final_duel_winner", "") or ""),
            hiring_signals=[dict(signal) for signal in data.get("hiring_signals", [])],
        )
