"""数据驱动的开放世界任务闭环。

任务进度只能由引擎已经记录的事实推进；AI可以选择任务，但不能直接提交“目标已完成”。
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tomllib
from typing import Any


QUESTS_FILE = Path(__file__).resolve().parents[1] / "data" / "quests" / "quests.toml"
FINAL_STATUSES = {"completed", "claimed", "failed"}


class QuestError(ValueError):
    """任务状态或定义不合法。"""


def load_quest_definitions(path: str | Path = QUESTS_FILE) -> list[dict[str, Any]]:
    """读取任务事实源；无定义时返回空列表，不自动编造剧情任务。"""
    file_path = Path(path)
    if not file_path.exists():
        return []
    with file_path.open("rb") as stream:
        data = tomllib.load(stream)
    definitions = data.get("quests", [])
    if not isinstance(definitions, list):
        raise QuestError("quests.toml 的 quests 必须是数组")
    return validate_definitions(definitions)


def validate_definitions(definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """校验任务ID、目标、前置、失败条件和奖励，拒绝含糊/不可执行定义。"""
    copied = deepcopy(definitions)
    by_id: dict[str, dict[str, Any]] = {}
    for quest in copied:
        quest_id = quest.get("id")
        if not isinstance(quest_id, str) or not quest_id.strip():
            raise QuestError("每个任务必须有非空id")
        if quest_id in by_id:
            raise QuestError(f"重复任务id：{quest_id}")
        if not isinstance(quest.get("title"), str) or not quest["title"].strip():
            raise QuestError(f"任务{quest_id}缺少title")
        objectives = quest.get("objectives", [])
        if not isinstance(objectives, list) or not objectives:
            raise QuestError(f"任务{quest_id}至少需要一个objective")
        seen_objectives: set[str] = set()
        for objective in objectives:
            oid = objective.get("id")
            if not isinstance(oid, str) or not oid.strip() or oid in seen_objectives:
                raise QuestError(f"任务{quest_id}的目标id必须非空且唯一")
            seen_objectives.add(oid)
            if not isinstance(objective.get("event"), str) or not objective["event"]:
                raise QuestError(f"任务{quest_id}目标{oid}缺少event")
            required = objective.get("required", 1)
            if not isinstance(required, int) or isinstance(required, bool) or required < 1:
                raise QuestError(f"任务{quest_id}目标{oid}的required必须是正整数")
            if not isinstance(objective.get("match", {}), dict):
                raise QuestError(f"任务{quest_id}目标{oid}的match必须是对象")
        rewards = quest.get("rewards", {})
        if not isinstance(rewards, dict):
            raise QuestError(f"任务{quest_id}的rewards必须是对象")
        for key, amount in rewards.items():
            if key not in {"shards", "attribute_points"}:
                raise QuestError(f"任务{quest_id}使用了未支持的奖励类型：{key}")
            if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
                raise QuestError(f"任务{quest_id}奖励{key}必须是非负整数")
        prerequisites = quest.get("prerequisites", [])
        fail_on = quest.get("fail_on", [])
        next_quests = quest.get("next_quests", [])
        if not all(isinstance(v, str) for v in prerequisites + next_quests):
            raise QuestError(f"任务{quest_id}的prerequisites/next_quests必须是字符串数组")
        if not all(isinstance(v, (str, dict)) for v in fail_on):
            raise QuestError(f"任务{quest_id}的fail_on必须是事件名或事件匹配对象")
        deadline = quest.get("deadline_turns")
        if deadline is not None and (not isinstance(deadline, int) or isinstance(deadline, bool) or deadline < 1):
            raise QuestError(f"任务{quest_id}的deadline_turns必须是正整数")
        by_id[quest_id] = quest
    for quest_id, quest in by_id.items():
        for dependency in quest.get("prerequisites", []) + quest.get("next_quests", []):
            if dependency not in by_id:
                raise QuestError(f"任务{quest_id}引用了不存在的任务：{dependency}")
        if quest_id in quest.get("prerequisites", []):
            raise QuestError(f"任务{quest_id}不能以前置条件依赖自身")
    return copied


class QuestEngine:
    """纯规则任务引擎。所有可变进度均放在GameState.quest_state中。"""

    def __init__(self, definitions: list[dict[str, Any]] | None = None):
        self.definitions = {
            quest["id"]: quest
            for quest in validate_definitions(definitions if definitions is not None else load_quest_definitions())
        }

    @staticmethod
    def initial_state() -> dict[str, Any]:
        return {
            "instances": {},
            "event_log": [],
            "story_flags": [],
            "next_event_id": 1,
        }

    def _ensure_state(self, state: dict[str, Any]) -> dict[str, Any]:
        state.setdefault("instances", {})
        state.setdefault("event_log", [])
        state.setdefault("story_flags", [])
        state.setdefault("next_event_id", 1)
        return state

    @staticmethod
    def _record(state: dict[str, Any], world_turn: int, event_type: str,
                quest_id: str, **details: Any) -> dict[str, Any]:
        event = {
            "event_id": state["next_event_id"],
            "world_turn": int(world_turn),
            "type": event_type,
            "quest_id": quest_id,
            "details": details,
        }
        state["next_event_id"] += 1
        state["event_log"].append(event)
        return event

    def _is_unlocked(self, state: dict[str, Any], quest: dict[str, Any]) -> bool:
        for prerequisite in quest.get("prerequisites", []):
            instance = state["instances"].get(prerequisite, {})
            if instance.get("status") not in {"completed", "claimed"}:
                return False
        return True

    def list_tasks(self, state: dict[str, Any], world_turn: int = 0) -> dict[str, Any]:
        state = self._ensure_state(state)
        available, active, history = [], [], []
        for quest_id, definition in self.definitions.items():
            instance = state["instances"].get(quest_id)
            item = {
                "id": quest_id,
                "title": definition["title"],
                "description": definition.get("description", ""),
                "giver_actor_id": definition.get("giver_actor_id"),
                "objectives": deepcopy(definition["objectives"]),
                "rewards": deepcopy(definition.get("rewards", {})),
                "status": instance.get("status") if instance else "locked" if not self._is_unlocked(state, definition) else "available",
                "progress": deepcopy(instance.get("progress", {})) if instance else {},
            }
            if instance and instance.get("status") == "active":
                active.append(item)
            elif instance and instance.get("status") in FINAL_STATUSES:
                history.append(item)
            elif not instance and self._is_unlocked(state, definition):
                available.append(item)
        return {
            "world_turn": int(world_turn),
            "available": available,
            "active": active,
            "history": history,
            "story_flags": list(state["story_flags"]),
        }

    def accept(self, state: dict[str, Any], quest_id: str, world_turn: int = 0,
               source_actor_id: str | None = None) -> dict[str, Any]:
        state = self._ensure_state(state)
        definition = self.definitions.get(quest_id)
        if definition is None:
            raise QuestError(f"任务不存在：{quest_id}")
        if not self._is_unlocked(state, definition):
            raise QuestError(f"任务{quest_id}的前置条件尚未满足")
        if quest_id in state["instances"]:
            raise QuestError(f"任务{quest_id}已经接取或结算，不能重复接取")
        giver = definition.get("giver_actor_id")
        if giver and source_actor_id != giver:
            raise QuestError(f"必须与任务委托者{giver}实际交互后才能接取")
        if definition.get("giver_name") and not source_actor_id:
            raise QuestError(f"必须与任务委托者{definition['giver_name']}实际交互后才能接取")
        progress = {objective["id"]: 0 for objective in definition["objectives"]}
        state["instances"][quest_id] = {
            "status": "active",
            "progress": progress,
            "accepted_turn": int(world_turn),
            "completed_turn": None,
            "failed_turn": None,
            "failure_reason": "",
        }
        self._record(state, world_turn, "quest_accepted", quest_id,
                     source_actor_id=source_actor_id)
        return deepcopy(state["instances"][quest_id])

    @staticmethod
    def _read_path(value: dict[str, Any], path: str) -> Any:
        current: Any = value
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return current

    def _matches(self, fact: dict[str, Any], event_name: str,
                 match: dict[str, Any], player_actor_id: str | None) -> bool:
        if fact.get("type") != event_name:
            return False
        for path, expected in match.items():
            actual = self._read_path(fact, path)
            if expected == "$player":
                expected = player_actor_id
            if actual != expected:
                return False
        return True

    def process_fact(self, state: dict[str, Any], fact: dict[str, Any],
                     world_turn: int = 0, player_actor_id: str | None = None) -> list[dict[str, Any]]:
        """仅处理引擎已记录的事实；返回本次产生的任务事实，供世界日志追加。"""
        state = self._ensure_state(state)
        generated: list[dict[str, Any]] = []
        for quest_id, instance in list(state["instances"].items()):
            if instance.get("status") != "active":
                continue
            definition = self.definitions[quest_id]
            fail_match = next((item for item in definition.get("fail_on", [])
                               if isinstance(item, str) and fact.get("type") == item), None)
            # dict型失败条件可以匹配事件类型与字段。
            if fail_match is None:
                for condition in definition.get("fail_on", []):
                    if isinstance(condition, dict) and self._matches(
                            fact, condition.get("event", ""), condition.get("match", {}), player_actor_id):
                        fail_match = condition
                        break
            if fail_match is not None:
                instance["status"] = "failed"
                instance["failed_turn"] = int(world_turn)
                instance["failure_reason"] = fact.get("type", "failed")
                generated.append(self._record(state, world_turn, "quest_failed", quest_id,
                                              reason=instance["failure_reason"]))
                continue
            for objective in definition["objectives"]:
                oid = objective["id"]
                if instance["progress"].get(oid, 0) >= objective.get("required", 1):
                    continue
                if self._matches(fact, objective["event"], objective.get("match", {}), player_actor_id):
                    instance["progress"][oid] = min(
                        objective.get("required", 1), instance["progress"].get(oid, 0) + 1)
                    generated.append(self._record(state, world_turn, "quest_progressed", quest_id,
                                                  objective_id=oid,
                                                  progress=instance["progress"][oid],
                                                  required=objective.get("required", 1)))
            if all(instance["progress"].get(obj["id"], 0) >= obj.get("required", 1)
                   for obj in definition["objectives"]):
                instance["status"] = "completed"
                instance["completed_turn"] = int(world_turn)
                flag = f"quest_completed:{quest_id}"
                if flag not in state["story_flags"]:
                    state["story_flags"].append(flag)
                generated.append(self._record(state, world_turn, "quest_completed", quest_id,
                                              next_quests=list(definition.get("next_quests", [])))
                                 )
                for next_id in definition.get("next_quests", []):
                    unlock_flag = f"quest_unlocked:{next_id}"
                    if unlock_flag not in state["story_flags"]:
                        state["story_flags"].append(unlock_flag)
        return generated

    def advance_time(self, state: dict[str, Any], world_turn: int) -> list[dict[str, Any]]:
        state = self._ensure_state(state)
        generated = []
        for quest_id, instance in list(state["instances"].items()):
            if instance.get("status") != "active":
                continue
            deadline = self.definitions[quest_id].get("deadline_turns")
            if deadline is not None and int(world_turn) > instance["accepted_turn"] + deadline:
                instance["status"] = "failed"
                instance["failed_turn"] = int(world_turn)
                instance["failure_reason"] = "deadline_expired"
                generated.append(self._record(state, world_turn, "quest_failed", quest_id,
                                              reason="deadline_expired"))
        return generated

    def claim(self, state: dict[str, Any], quest_id: str, world_turn: int = 0) -> dict[str, Any]:
        state = self._ensure_state(state)
        instance = state["instances"].get(quest_id)
        if instance is None:
            raise QuestError(f"任务{quest_id}尚未接取")
        if instance.get("status") != "completed":
            raise QuestError(f"任务{quest_id}当前状态为{instance.get('status')}，不能领取奖励")
        instance["status"] = "claimed"
        self._record(state, world_turn, "quest_reward_claimed", quest_id,
                     rewards=deepcopy(self.definitions[quest_id].get("rewards", {})))
        return deepcopy(self.definitions[quest_id].get("rewards", {}))
