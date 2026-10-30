"""
最终死斗与冠冕处理器（Duel & Crown Handler）
负责死斗遗物激活、死斗胜负结算、终音法器选择与角色封存。
"""
from __future__ import annotations
import os
import json
from typing import Any, Dict, Optional
from ..death_book import validate_legacy


def handle_activate_duel_relic(engine: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """死斗开场可选遗物激活"""
    if not engine.state.in_final_duel:
        return {"success": False, "error": "当前没有进行中的最终死斗"}
    side = params.get("side", "")
    name = params.get("relic", "")
    use = params.get("use", True)
    if side not in ("player_side", "opponent_side"):
        return {"success": False, "error": "side必须是 player_side 或 opponent_side"}
    pool = engine.state.relics if side == "player_side" else engine.state.opponent_relics
    holder = engine.state.player if side == "player_side" else next(
        (e for e in engine.state.enemies if e.entity_type == "轮回者" and e.is_alive), None)
    if holder is None:
        return {"success": False, "error": "找不到该侧轮回者"}
    if not any(r.name == name for r in pool):
        return {"success": False, "error": f"{side}未持有遗物: {name}"}
    if not use:
        return {"success": True, "action": f"{holder.name}放弃发动【{name}】",
                "result": {"relic": name, "used": False}}
    return {"success": False, "error": f"【{name}】不是死斗开场可选遗物，或尚未接线"}


def handle_resolve_final_duel(engine: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """死斗胜负结算"""
    if not engine.state.in_final_duel:
        return {"success": False, "error": "当前没有进行中的最终死斗"}
    outcome = params.get("outcome", "")
    if outcome not in ("victory", "defeat"):
        return {"success": False, "error": "outcome必须是 victory 或 defeat"}
    legacy = params.get("death_book_entry")

    if outcome == "victory":
        # 挑战者胜利：擂主（死斗败方）失去轮回者身份，不再回到封存队列。
        engine.state.duel_defending_snapshot = {}
        engine.state.in_final_duel = False
        region = engine.state.current_region
        options = engine.TERMINAL_ARTIFACTS.get(region, [])
        next_tier = (engine.state.duel_tier or 0) + 1
        if not options:
            seal = engine._finalize_victory_seal(advance_tier=engine.state.duel_tier or None)
            return {"success": True, "action": "死斗结算",
                    "result": {"outcome": "victory", "seal": seal,
                               "instruction": f"{region}没有已定义的终音法器，已直接封存入{next_tier}阶进阶封存槽"}}
        engine.state.pending_terminal_region = region
        return {
            "success": True, "action": "死斗结算",
            "result": {
                "outcome": "victory", "pending_terminal_choice": region,
                "options": [{"id": i + 1, "name": n, "effect": e} for i, (n, e) in enumerate(options)],
                "instruction": "请调用 choose_terminal_artifact(choice=序号) 领取终音法器后才会完整封存"
                               f"（胜者进入{next_tier}阶进阶封存，不再与原阶级角色死斗）",
            }}
    else:
        # 擂主卫冕成功：挑战者落败不影响擂主封存。按 规则正文（封存"直到下一名
        # 同阶级轮回者完成第7场战斗"+队列先来后到语义）把原始快照放回队首——
        # 此前擂主在挑战者落败时被无声吞掉，封存队列越打越空、死斗不可持续。
        snap = getattr(engine.state, "duel_defending_snapshot", None) or {}
        if isinstance(snap, dict) and snap.get("player"):
            tier = engine.state.duel_tier or 1
            slots = engine._load_seal_slots()
            slots.setdefault(tier, [])
            slots[tier].insert(0, snap)
            engine._save_seal_slots(slots)
        engine.state.duel_defending_snapshot = {}
        engine.state.in_final_duel = False
        engine.state.duel_tier = 0
        player = engine.state.player
        if player is not None:
            # 死斗败者：这是**战斗已经结束后**的结算落幕，不是战斗内命零，
            # 因此刻意不接 CombatEngine._check_hp_zero_death()——否则会在战斗结束后
            # 再触发一轮[命零]效果（分裂造物/焦黑发丝等）。
            # 后续走 last_death_cause="duel" 与死者之书流程。
            player.current_hp = 0
            player.is_alive = False
        engine.state.last_death_cause = "duel"
        if isinstance(legacy, dict):
            try:
                engine.state.pending_death_draft = validate_legacy(
                    legacy, engine.state.death_book_capacity)
            except ValueError:
                engine.state.pending_death_draft = {}
        return {"success": True, "action": "死斗结算",
                "result": {"outcome": "defeat",
                           "instruction": "败者失去轮回者身份，已触发死者之页，等待审核后写入死者之书"}}
