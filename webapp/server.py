#!/usr/bin/env python3
"""可玩网页界面：把引擎的开放世界接口包成 HTTP + 一张地图页面。

设计口径：
* 界面只是引擎的显示器与输入器，所有判定仍走 ``GameEngine.execute_action``；
* 玩家能做的事：在地图上走/等、与相邻单位交涉、向朋友/员工**建议**行为
  （是否照做由对方自己判定）、进入遭遇并打完整场战斗；
* 迷雾：只把 ``world_status`` 已公开的已知地形/可见单位发给浏览器，
  不把全地图数据提前泄漏给前端。

启动：``python3 webapp/server.py``（默认 0.0.0.0:8000，可用 PORT 覆盖）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.api import GameEngine  # noqa: E402
from engine.models import Entity  # noqa: E402
from engine.world import WorldState  # noqa: E402
from sim.monster_phase_submit import monster_phase_choices  # noqa: E402

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
INDEX_PATH = Path(__file__).resolve().parent / "index.html"

SESSION: dict[str, Any] = {"engine": None, "log": [], "last_world": None}
LOCK = threading.Lock()

REGIONS = ("罪孽都市", "扭曲都市", "龙心谷", "乱葬岗")


# ---------------------------------------------------------------- 开局


def _new_engine(payload: dict[str, Any]) -> GameEngine:
    workdir = tempfile.mkdtemp(prefix="linji-play-")
    seed = payload.get("seed")
    engine = GameEngine(
        db_path=os.path.join(workdir, "rulings.db"),
        save_dir=os.path.join(workdir, "saves"),
        sealed_candidate_path=os.path.join(workdir, "sealed.json"),
        death_book_path=os.path.join(workdir, "death_book.md"),
        rng_seed=int(seed) if seed not in (None, "") else None,
        autosave=False,
    )
    name = str(payload.get("name") or "轮回者").strip()[:12] or "轮回者"
    blood = int(payload.get("blood_points", 11))
    speed = int(payload.get("speed_points", 8))
    mana = int(payload.get("mana_points", 6))
    region = payload.get("region") if payload.get("region") in REGIONS else "罪孽都市"

    def call(action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        result = engine.execute_action(action, params or {})
        if not result.get("success"):
            raise RuntimeError(f"{action}失败：{result.get('error')}")
        return result

    setup = call("setup_attributes", {"name": name, "blood_points": blood,
                                      "speed_points": speed, "mana_points": mana})
    relic_choices = setup["result"]["relic_choices"]
    call("choose_discovered_relic", {"relic_name": relic_choices[0]})
    if engine.state.pending_initial_daowen_choices:
        call("setup_choose_initial_daowen",
             {"daowen_name": engine.state.pending_initial_daowen_choices[0]})
    if not engine.state.resonance:
        call("setup_choose_resonance", {"resonance_type": payload.get("resonance") or "转换"})
    call("setup_choose_region", {"region": region})

    if payload.get("demo_friend", True):
        # 演示用同伴：正式的朋友/员工走事件与雇佣流程；这里只为了让新玩家
        # 立刻能看到「它可以自己走，而玩家只能建议」。
        friend = Entity(name="随行朋友", entity_type="朋友",
                        blood_limit=max(12, blood * 2), current_hp=max(12, blood * 2),
                        attack_count=max(1, speed // 3), attack_power=max(1, mana // 2))
        engine.state.friends.append(friend)

    width = int(payload.get("width", 21))
    height = int(payload.get("height", 21))
    call("world_initialize", {"width": width, "height": height})
    return engine


# ---------------------------------------------------------------- 快照


def _log(text: str) -> None:
    SESSION["log"].append(text)
    SESSION["log"] = SESSION["log"][-200:]


def _player_view(engine: GameEngine) -> dict[str, Any] | None:
    player = engine.state.player
    if player is None:
        return None
    return {
        "name": player.name,
        "alive": bool(player.is_alive),
        "hp": player.current_hp,
        "hp_limit": player.blood_limit,
        "mana": player.current_mana,
        "mana_limit": player.mana_limit,
        "speed": player.current_speed,
        "speed_limit": player.speed_limit,
        "attack_count": player.attack_count,
        "attack_power": player.attack_power,
        "shards": engine.state.shards,
        "fake_shards": engine.state.fake_shards,
        "energy": engine.state.energy,
        "region": engine.state.current_region,
        "relics": [relic.name for relic in engine.state.relics],
        "daowen": list(player.dao_wen),
        "consumables": [{"name": item.name, "uses": item.current_uses,
                         "max_uses": item.max_uses} for item in engine.state.consumables],
    }


def _battle_view(engine: GameEngine) -> dict[str, Any] | None:
    state = engine.state
    if state.player is None or state.phase not in {"battle_start", "in_combat", "battle_end"}:
        return None
    player_side = ([state.player] + list(state.friends) + list(state.employees)
                   + list(state.temp_friends))
    refs = engine.combat._combat_entity_refs()
    ref_by_id = {id(entity): ref for ref, entity in refs.items()}
    sides = []
    for entity in player_side + list(state.enemies):
        if entity is None:
            continue
        sides.append({
            "ref": ref_by_id.get(id(entity)),
            "name": entity.name,
            "side": "player" if entity in player_side else "enemy",
            "hp": entity.current_hp,
            "hp_limit": entity.blood_limit,
            "alive": bool(entity.is_alive and not entity.is_departed),
            "attack_count": entity.attack_count,
            "attack_power": entity.attack_power,
        })
    view: dict[str, Any] = {
        "round": state.current_round,
        "subphase": state.combat_subphase,
        "combatants": sides,
        "actions_used": state.player.actions_used_this_round,
        "action_budget": engine._action_budget_of(state.player),
        "actions_left": max(0, engine._action_budget_of(state.player)
                            - state.player.actions_used_this_round),
        "enemies_alive": sum(1 for entity in state.enemies
                             if entity.is_alive and not entity.is_departed),
        "peaceful": state.phase == "battle_end",
        "pending_attack": None,
    }
    if state.pending_attack:
        pending = state.pending_attack
        options = pending["options"]
        view["pending_attack"] = {
            "token": pending.get("token"),
            "hit_count": options["hit_count"],
            "damage_budget": options["damage_budget"],
            "target_options": options["target_options"],
        }
    if state.world_encounter_id:
        view["world_encounter_id"] = state.world_encounter_id
    return view


def _world_view(engine: GameEngine) -> dict[str, Any] | None:
    if not engine.state.world_state:
        return None
    status = engine.execute_action("world_status", {})
    if not status.get("success"):
        # 事件待结算时引擎会拒绝一切非 resolve_event 行动（含 world_status）。
        # 这时沿用上一帧地图：位置不会变，玩家只是在等事件选项。
        cached = SESSION.get("last_world")
        if cached:
            stale = dict(cached)
            stale["stale"] = True
            stale["blocked"] = status.get("error")
            return stale
        return None
    result = status["result"]
    if "self" not in result:
        # 最终生存阶段已结算：world_status 只返回胜者，不再有“自己”。
        # 用上一帧地图补齐画面，并明确标记这一局已经结束。
        merged = dict(SESSION.get("last_world") or {})
        merged.update(result)
        merged["ended"] = True
        SESSION["last_world"] = merged
        return merged
    vision = engine.execute_action("world_vision", {})
    if vision.get("success"):
        result["visible_cells"] = vision["result"].get("visible_cells", [])
        result["reported_sightings"] = vision["result"].get("reported_sightings", [])
    world = WorldState.from_dict(engine.state.world_state)
    result["facts"] = [
        {"world_turn": event.get("world_turn"), "type": event.get("type"),
         "details": event.get("details", {})}
        for event in world.event_log[-18:]
    ]
    SESSION["last_world"] = result
    result["encounter_for_player"] = next(
        ({"encounter_id": enc.encounter_id, "participants": list(enc.participants),
          "location": list(enc.location), "final_duel": enc.final_duel}
         for enc in world.encounters.values()
         if enc.active and result["self"]["actor_id"] in enc.participants),
        None)
    return result


def _event_view(engine: GameEngine, actions: dict[str, Any]) -> dict[str, Any] | None:
    if engine.event_pool.current is None:
        return None
    event = engine.event_pool.events[engine.event_pool.current]
    options = []
    for action in actions.get("actions", []):
        if action.get("action_type") != "resolve_event":
            continue
        schema = action.get("params_schema", {})
        fields = [{"key": key,
                   "hint": value if isinstance(value, (str, list, dict, int, float))
                   else str(value)}
                  for key, value in schema.items() if key not in {"event", "option_id"}]
        options.append({"option_id": schema.get("option_id"),
                        "text": action.get("option", ""),
                        "fields": fields})
    return {"name": engine.event_pool.current, "desc": event.get("desc", ""),
            "options": options,
            "queued": len(engine.state.pending_event_queue)}


def _snapshot(engine: GameEngine | None) -> dict[str, Any]:
    if engine is None:
        return {"started": False, "log": []}
    actions = engine.get_available_actions()
    return {
        "started": True,
        "phase": engine.state.phase,
        "player": _player_view(engine),
        "world": _world_view(engine),
        "battle": _battle_view(engine),
        "event": _event_view(engine, actions),
        "actions": actions,
        "log": SESSION["log"][-80:],
    }


# ---------------------------------------------------------------- 动作


def _describe(action: str, params: dict[str, Any], result: dict[str, Any]) -> str:
    if not result.get("success"):
        return f"✗ {action}：{result.get('error')}"
    payload = result.get("result") or {}
    if action == "world_move":
        return f"移动到 ({params.get('x')}, {params.get('y')})"
    if action == "world_wait":
        return f"等待 {params.get('turns', 1)} 回合"
    if action == "world_suggest_ally":
        verdict = "接受了" if payload.get("accepted") else "拒绝了"
        return (f"建议{payload.get('target')}「{payload.get('suggestion')}」→ "
                f"它{verdict}（采纳率 {payload.get('chance_percent')}%，"
                f"当前行为 {payload.get('behavior')}）")
    if action == "world_interact":
        return (f"与{payload.get('target')}交涉：{payload.get('choice')} → "
                f"{json.dumps(payload, ensure_ascii=False)[:200]}")
    if action == "world_initialize":
        return "开放世界已就绪"
    if action == "battle_start":
        return "战斗开始"
    if action == "resolve_attack":
        dealt = [hit.get("damage_dealt") for hit in payload.get("hits", [])]
        return f"攻击落地伤害 {dealt}"
    if action == "resolve_monster_phase":
        return f"敌方阶段结算完毕：玩家 HP {payload.get('player_hp')}"
    if action == "round_end":
        return "本回合结算完毕"
    if action == "resolve_event":
        return "事件已结算"
    if action == "battle_end":
        return "战斗结束，回到地图"
    return f"{action} 完成"


def _execute(action: str, params: dict[str, Any]) -> dict[str, Any]:
    engine: GameEngine | None = SESSION["engine"]
    if engine is None:
        return {"success": False, "error": "还没有开局"}
    result = engine.execute_action(action, params or {})
    _log(_describe(action, params or {}, result))
    return result


def _ui_attack(params: dict[str, Any]) -> dict[str, Any]:
    engine: GameEngine = SESSION["engine"]
    pending = engine.state.pending_attack
    if not pending:
        return {"success": False, "error": "请先点『准备攻击』"}
    options = pending["options"]
    legal = {entry["ref"]: entry for entry in options["target_options"]}
    target_ref = params.get("target_ref")
    if target_ref not in legal:
        target_ref = options["target_options"][0]["ref"]
    target = legal[target_ref]
    hit_count = int(options["hit_count"])
    raw = params.get("damages") or []
    damages = [max(0, int(raw[index])) if index < len(raw) else 0
               for index in range(hit_count)]
    if sum(damages) > int(options["damage_budget"]):
        return {"success": False,
                "error": f"总伤害 {sum(damages)} 超出本回合预算 {options['damage_budget']}"}
    spell_choices = {timing: {spell["spell_name"]: {"use": False} for spell in spells}
                     for timing, spells in (target.get("spell_options") or {}).items()}
    dodge_flags = list(params.get("dodge") or [])
    hits = []
    for index, damage in enumerate(damages):
        hits.append({
            "target_ref": target_ref, "damage": damage,
            "dodge": bool(dodge_flags[index]) if index < len(dodge_flags) else False,
            "parry": False, "blood_shadow": False,
            "spell_choices": json.loads(json.dumps(spell_choices)),
        })
    return _execute("resolve_attack", {"token": pending.get("token"), "hits": hits})


def _ui_monster_phase(params: dict[str, Any]) -> dict[str, Any]:
    engine: GameEngine = SESSION["engine"]
    prepared = engine.execute_action("prepare_monster_phase", {})
    if not prepared.get("success"):
        _log(f"✗ 敌方阶段：{prepared.get('error')}")
        return prepared
    actors = prepared["result"]["actors"]
    choices = monster_phase_choices(actors, engine=engine)
    dodge_refs = set(params.get("dodge_refs") or [])
    for choice in choices:
        if choice["actor_ref"] in dodge_refs:
            for attack in choice["attack_actions"]:
                for hit in attack["hits"]:
                    hit["dodge"] = True
    result = engine.execute_action("resolve_monster_phase",
                                   {"token": prepared["result"]["token"], "choices": choices})
    if not result.get("success"):
        result["error"] = (result.get("error") or "") + "（闪避可能需要速度预算；取消闪避后重试）"
    _log(_describe("resolve_monster_phase", params, result))
    return result


def _start(payload: dict[str, Any]) -> dict[str, Any]:
    engine = _new_engine(payload)
    SESSION["engine"] = engine
    SESSION["log"] = []
    SESSION["last_world"] = None
    _log(f"开局：{engine.state.player.name} · {engine.state.current_region}")
    return _snapshot(engine)


# ---------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = "LinjiPlay/1.0"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in {"/", "/index.html"}:
            self._send(200, INDEX_PATH.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/api/snapshot":
            with LOCK:
                self._json(_snapshot(SESSION["engine"]))
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"success": False, "error": "请求体不是合法JSON"}, 400)
            return
        try:
            with LOCK:
                if path == "/api/new":
                    self._json({"success": True, "snapshot": _start(payload)})
                elif path == "/api/action":
                    action = payload.get("action")
                    if not isinstance(action, str) or not action:
                        self._json({"success": False, "error": "缺少action"}, 400)
                        return
                    result = _execute(action, payload.get("params") or {})
                    self._json({"success": bool(result.get("success")),
                                "result": result,
                                "snapshot": _snapshot(SESSION["engine"])})
                elif path == "/api/ui_attack":
                    result = _ui_attack(payload)
                    self._json({"success": bool(result.get("success")), "result": result,
                                "snapshot": _snapshot(SESSION["engine"])})
                elif path == "/api/ui_monster_phase":
                    result = _ui_monster_phase(payload)
                    self._json({"success": bool(result.get("success")), "result": result,
                                "snapshot": _snapshot(SESSION["engine"])})
                else:
                    self._json({"success": False, "error": "未知接口"}, 404)
        except Exception as exc:  # 引擎异常也要回到前端，而不是留一个空响应
            traceback.print_exc()
            self._json({"success": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[play] " + fmt % args + "\n")


def main() -> None:
    if not INDEX_PATH.exists():
        raise SystemExit(f"找不到界面文件：{INDEX_PATH}")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"可玩界面已启动：http://{HOST}:{PORT}/  （Ctrl+C 停止）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
