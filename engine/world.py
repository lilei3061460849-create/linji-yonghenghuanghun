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

    def adjacent_vision(self, pos: Coord) -> list[Coord]:
        """只看上下左右一格；斜角不算。相邻障碍本身可见，但不会透视其后方。"""
        visible = []
        for dx, dy in DIRECTIONS:
            nxt = (pos[0] + dx, pos[1] + dy)
            if self.in_bounds(nxt):
                visible.append(nxt)
        return visible

    def to_dict(self) -> dict[str, Any]:
        return {
            "width": self.width, "height": self.height,
            "terrain": dict(self.terrain),
            "opaque_terrain": sorted(self.opaque_terrain),
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorldMap":
        return cls(
            width=int(data["width"]), height=int(data["height"]),
            terrain={str(k): str(v) for k, v in data.get("terrain", {}).items()},
            opaque_terrain=set(data.get("opaque_terrain", ["wall", "building"])),
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
    known_cells: set[str] = field(default_factory=set)
    memory: list[dict[str, Any]] = field(default_factory=list)

    @property
    def position(self) -> Coord:
        return self.x, self.y

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["known_cells"] = sorted(self.known_cells)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorldActorState":
        values = dict(data)
        values["known_cells"] = set(values.get("known_cells", []))
        values["memory"] = list(values.get("memory", []))
        return cls(**values)


@dataclass
class EncounterState:
    encounter_id: str
    location: Coord
    participants: list[str]
    started_turn: int
    active: bool = True
    outcome: str = ""


@dataclass
class WorldState:
    clock: WorldClock
    map: WorldMap
    actors: dict[str, WorldActorState] = field(default_factory=dict)
    encounters: dict[str, EncounterState] = field(default_factory=dict)
    event_log: list[dict[str, Any]] = field(default_factory=list)
    next_event_id: int = 1
    next_encounter_id: int = 1

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
        if any(a.alive and a.position == actor.position for a in self.actors.values()):
            raise ValueError("同一格不能同时容纳多个单位")
        self.actors[actor.actor_id] = actor
        self.record("actor_spawned", actor.actor_id, name=actor.name, kind=actor.kind,
                    position=list(actor.position))

    def visible_cells(self, actor_id: str) -> list[Coord]:
        actor = self._living_actor(actor_id)
        cells = [actor.position] + self.map.adjacent_vision(actor.position)
        # 角色只能更新自己的情报；不共享其他角色的视野。
        actor.known_cells.update(self.map.key(p) for p in cells)
        return cells

    def move_actor(self, actor_id: str, destination: Coord) -> dict[str, Any]:
        actor = self._living_actor(actor_id)
        dx = abs(destination[0] - actor.x)
        dy = abs(destination[1] - actor.y)
        if dx + dy != 1:
            raise ValueError("每次移动只能到上下左右相邻一格")
        if not self.map.is_passable(destination):
            raise ValueError("目标格不可通行")
        blocker = next((a for a in self.actors.values()
                        if a.alive and a.actor_id != actor_id and a.position == destination), None)
        if blocker:
            raise ValueError(f"目标格被单位占据：{blocker.name}")
        origin = actor.position
        actor.x, actor.y = destination
        self.clock.advance(1)
        self.record("actor_moved", actor_id, origin=list(origin), destination=list(destination))
        self.visible_cells(actor_id)
        return {"actor_id": actor_id, "from": list(origin), "to": list(destination),
                "world_turn": self.clock.turn}

    def wait(self, actor_id: str, turns: int = 1) -> int:
        self._living_actor(actor_id)
        self.clock.advance(turns)
        self.record("world_waited", actor_id, turns=turns)
        return self.clock.turn

    def start_encounter(self, participant_ids: list[str], location: Coord) -> EncounterState:
        unique = list(dict.fromkeys(participant_ids))
        if len(unique) < 2:
            raise ValueError("遭遇至少需要两个不同单位")
        for actor_id in unique:
            self._living_actor(actor_id)
        if not self.map.in_bounds(location):
            raise ValueError("遭遇地点超出地图")
        encounter = EncounterState(
            encounter_id=f"encounter-{self.next_encounter_id}",
            location=location, participants=unique, started_turn=self.clock.turn,
        )
        self.next_encounter_id += 1
        self.encounters[encounter.encounter_id] = encounter
        self.record("encounter_started", unique[0], encounter_id=encounter.encounter_id,
                    participants=unique, location=list(location))
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
            ) for k, v in data.get("encounters", {}).items()},
            event_log=list(data.get("event_log", [])),
            next_event_id=int(data.get("next_event_id", 1)),
            next_encounter_id=int(data.get("next_encounter_id", 1)),
        )
