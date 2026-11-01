"""真实引擎七场清场与封存候选人死斗运行器。

用途：先用闻照野建立一阶封存候选，再用另一名轮回者跑七场并进入真实死斗。
所有战斗动作通过 GameEngine.execute_action；脚本只负责提交选择和串联阶段。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from engine.api import GameEngine
from engine.ai_tactics import TacticalAI
from sim.build_learner import _resolve_monster_turn
from sim.duel_pvp import run_duel_pvp
from sim.optional_actions import battle_start_relic_choices, round_start_relic_choices


def snapshot(e):
    p = e.state.player
    return {
        "round": e.state.current_round,
        "battle": e.state.current_battle,
        "phase": e.state.phase,
        "subphase": e.state.combat_subphase,
        "player": ({
            "name": p.name, "hp": p.current_hp, "blood_limit": p.blood_limit,
            "mana": p.current_mana, "mana_limit": p.mana_limit,
            "speed": p.current_speed, "speed_limit": p.speed_limit,
            "alive": p.is_alive,
            "actions_used": p.actions_used_this_round, "action_count": p.action_count,
        } if p else None),
        "friends": [{"name": f.name, "hp": f.current_hp, "alive": f.is_alive}
                    for f in e.state.friends],
        "enemies": [{"name": x.name, "hp": x.current_hp, "alive": x.is_alive,
                     "entity_type": x.entity_type}
                    for x in e.state.enemies],
        "delayed_monster_reentries": [
            {"name": entry["monster"].name, "return_round": entry["return_round"]}
            for entry in getattr(e.state, "delayed_monster_reentries", [])
        ],
        "monster_reinforcements": [m.get("name") for m in getattr(e.state, "monster_reinforcements", [])],
        "shards": e.state.shards,
        "energy": e.state.energy,
    }


def run(seed: int, name: str, slots: str, out_path: str):
    e = GameEngine(
        db_path=f"/tmp/wen_duel_{os.getpid()}_{seed}.db",
        rng_seed=seed,
        sealed_candidate_path=slots,
    )
    log = []

    def act(action, params=None, note="", thought=""):
        params = params or {}
        before = snapshot(e)
        result = e.execute_action(action, params)
        if not result.get("success"):
            raise RuntimeError(f"{action} failed: {result}")
        log.append({
            "kind": "action", "action": action, "params": params,
            "note": note, "thought": thought,
            "before": before, "after": snapshot(e),
        })
        return result

    setup = act("setup_attributes", {
        "name": name, "blood_points": 5, "speed_points": 8, "mana_points": 12,
    }, "分配开局属性", "先让自己有足够的法力和速度，才有机会把代价留到最后。")
    relics = setup["result"]["relic_choices"]
    relic = relics[0]
    act("choose_discovered_relic", {"relic_name": relic},
        f"选择遗物{relic}", "把残韵留给真正需要的回合。")

    initial = list(e.state.pending_initial_daowen_choices)
    pick = "封印" if "封印" in initial else initial[0]
    act("setup_choose_initial_daowen", {"daowen_name": pick},
        f"选择初始道纹{pick}", "先让危险离场，再决定什么时候正面换血。")
    act("setup_choose_resonance", {"resonance_type": "转换"},
        "选择转换残韵", "把遇到的力量转成能继续走下去的东西。")
    act("setup_choose_region", {"region": "龙心谷"},
        "进入龙心谷", "这一条路必须走到底。")

    # 三次炼心各接一场真实封印代价，形成后续封印可用的异变龙心。
    for battle_no in range(1, 8):
        if battle_no in (1, 2, 3):
            act("pre_battle_action", {"sub_action": "炼心"},
                f"第{battle_no}场前炼心",
                "先把下一次异变代价炼成可以留到终波使用的龙心。")

        while e.state.energy > 0:
            p = e.state.player
            missing = p.blood_limit - p.current_hp
            rest_amount = (p.blood_limit + 4) // 5
            if battle_no == 7 and missing >= rest_amount:
                act("pre_battle_action", {
                    "sub_action": "休整", "tier": 1,
                    "heal_allocations": [{"target_ref": "player:0", "amount": rest_amount}],
                }, f"第{battle_no}场前休整", "先把血线补回战斗安全线。")
            else:
                act("pre_battle_action", {"sub_action": "修行", "tier": 1},
                    f"第{battle_no}场前修行", "继续提升下一场真正需要的法限或速限。")
                if e.state.attribute_points >= 2:
                    key = "speed_points" if e.state.player.speed_limit < 6 else "mana_points"
                    allocations = {"blood_points": 0, "speed_points": 0, "mana_points": 0}
                    allocations[key] = 2
                    act("redeem_attribute_points", {"allocations": allocations},
                        f"兑换2点{'速限' if key == 'speed_points' else '法限'}",
                        "把已经积累的属性点兑换成当前面板最需要的上限。")

        bs = act("battle_start", {"relic_choices": battle_start_relic_choices(e)},
                 f"第{battle_no}场战始")
        ai = TacticalAI(e, verbose=False)
        # 新版【封印】是延迟回场，不是永久清场；本复盘保留初始道纹选择。
        # 前六场允许AI至多实际封印一次（然后由本轮的其它行动处理回场怪），
        # 第七场的两次封印仍由下方显式龙心行动提交；全部结算都走 execute_action。
        seal_used_this_battle = False
        ai.blocked_daowen_names.clear()
        for _ in range(120):
            ai.blocked_daowen_names = {"封印"} if battle_no == 7 or seal_used_this_battle else set()
            if not e.state.player or not e.state.player.is_alive:
                break
            alive = [x for x in e.state.enemies if x.is_alive]
            if (not alive
                    and not getattr(e.state, "monster_reinforcements", [])
                    and not getattr(e.state, "delayed_monster_reentries", [])):
                break

            act("round_start", {"relic_choices": round_start_relic_choices(e)},
                f"第{battle_no}场回始")
            ai.new_round()

            # 终场在断翼巨像、墓门卫出现后逐个使用异变龙心封印；
            # 目标引用与龙心代价都显式提交给真实接口。
            if battle_no == 7 and e.state.current_round >= 8 and "封印" in e.state.player.dao_wen:
                first = next((i for i, x in enumerate(e.state.enemies) if x.is_alive), None)
                heart = next((c for c in e.state.consumables
                              if c.kind == "dragon_heart"
                              and c.dragon_heart_type == "异变"
                              and c.current_uses >= 1), None)
                if first is not None and heart is not None:
                    act("use_daowen", {
                        "daowen_name": "封印", "x": 1,
                        "target_ref": f"enemy:{first}", "dragon_heart_use": 1,
                        "dodge": False, "blood_shadow": False,
                        "trigger_spell_choices": {},
                    }, "终波龙心封印", "使用前几场炼心形成的真实龙心抵消异变代价。")

            guard = 0
            while (e.state.combat_subphase == "player_actions"
                   and e.state.player.actions_used_this_round < e.state.player.action_count):
                result = ai.take_action()
                guard += 1
                if result is None:
                    break
                log.append({
                    "kind": "ai_action", "battle": battle_no,
                    "round": e.state.current_round,
                    "action": ai.last_decision,
                    "result": result, "state": snapshot(e),
                })
                if (ai.last_decision or {}).get("action") == "use_daowen" \
                        and (ai.last_decision or {}).get("params", {}).get("daowen_name") == "封印":
                    seal_used_this_battle = True
                    ai.blocked_daowen_names.add("封印")
                if guard > 12:
                    raise RuntimeError("player action runaway")

            alive = [x for x in e.state.enemies if x.is_alive]
            if not alive:
                prep = e.execute_action("prepare_monster_phase", {})
                if not prep.get("success"):
                    raise RuntimeError(f"empty monster prepare failed: {prep}")
                resolved = e.execute_action("resolve_monster_phase", {
                    "token": prep["result"]["token"], "choices": [],
                })
                if not resolved.get("success"):
                    raise RuntimeError(f"empty monster resolve failed: {resolved}")
                log.append({"kind": "monster_phase", "battle": battle_no,
                            "round": e.state.current_round,
                            "result": resolved, "state": snapshot(e)})
                act("round_end", {}, f"第{battle_no}场第{e.state.current_round}回终（等待增援）")
                continue

            ally = e.execute_action("resolve_ally_phases", {})
            if not ally.get("success"):
                raise RuntimeError(f"ally phase failed: {ally}")
            if not [x for x in e.state.enemies if x.is_alive]:
                prep = e.execute_action("prepare_monster_phase", {})
                if not prep.get("success"):
                    raise RuntimeError(f"empty monster prepare failed: {prep}")
                resolved = e.execute_action("resolve_monster_phase", {
                    "token": prep["result"]["token"], "choices": [],
                })
                if not resolved.get("success"):
                    raise RuntimeError(f"empty monster resolve failed: {resolved}")
                log.append({"kind": "monster_phase", "battle": battle_no,
                            "round": e.state.current_round,
                            "result": resolved, "state": snapshot(e)})
                act("round_end", {}, f"第{battle_no}场第{e.state.current_round}回终（等待增援）")
                continue

            monster = _resolve_monster_turn(e)
            if not monster.get("success"):
                raise RuntimeError(f"monster phase failed: {monster}")
            log.append({"kind": "monster_phase", "battle": battle_no,
                        "round": e.state.current_round,
                        "result": monster, "state": snapshot(e)})
            if not e.state.player or not e.state.player.is_alive:
                break
            end = e.execute_action("round_end", {})
            if not end.get("success"):
                raise RuntimeError(f"round end failed: {end}")
            log.append({"kind": "action", "action": "round_end", "note": "回合结束",
                        "before": {}, "after": snapshot(e)})

        if not e.state.player or not e.state.player.is_alive:
            break
        if (any(x.is_alive for x in e.state.enemies)
                or getattr(e.state, "monster_reinforcements", [])
                or getattr(e.state, "delayed_monster_reentries", [])):
            break
        ended = act("battle_end", {}, f"第{battle_no}场战终")
        # 2026-10-31：战终不再自动开启死斗；本脚本显式走旧入口。
        if e.state.current_battle == 7 and "final_crown" not in (ended.get("result") or {}):
            ended.setdefault("result", {})["final_crown"] = e._trigger_final_crown() or {}
        crown = (ended.get("result") or {}).get("final_crown") or {}
        if crown.get("outcome") == "duel_start":
            duel_log = []
            def challenger_action():
                p = e.state.player
                if not p or not p.is_alive or p.actions_used_this_round >= p.action_count:
                    return False
                before_duel_action = snapshot(e)
                prep = e.execute_action("prepare_attack", {"actor_ref": "player:0"})
                if not prep.get("success"):
                    return False
                options = prep.get("result", {}).get("target_options") or []
                target = next((o for o in options if o.get("ref", "").startswith("enemy:")), None)
                if target is None:
                    return False
                from sim.build_learner import _decline_spells
                hits = [{
                    "target_ref": target["ref"], "dodge": False,
                    "blood_shadow": False, "spell_choices": _decline_spells(target),
                } for _ in range(prep["result"].get("hit_count", 1))]
                resolved = e.execute_action("resolve_attack", {
                    "token": prep["result"]["token"], "hits": hits,
                })
                if not resolved.get("success"):
                    return False
                duel_log.append({
                    "action": "challenger_prepare_resolve_attack",
                    "target": target.get("name"), "hit_count": len(hits),
                    "before": before_duel_action, "after": snapshot(e),
                })
                return True

            duel = run_duel_pvp(
                e, player_act=challenger_action, log=duel_log,
                max_rounds=60, max_steps=400, max_wall_seconds=30,
                use_tactical=False,
            )
            log.append({"kind": "duel", "result": duel, "log": duel_log,
                        "state": snapshot(e)})
            if duel.get("winner") == "challenger":
                resolved = e.execute_action("resolve_final_duel", {"outcome": "victory"})
                log.append({"kind": "action", "action": "resolve_final_duel",
                            "result": resolved, "state": snapshot(e)})
                if resolved.get("success") and resolved.get("result", {}).get("pending_terminal_choice"):
                    options = resolved["result"].get("options") or []
                    if options:
                        chosen = e.execute_action("choose_terminal_artifact", {"choice": 1})
                        log.append({"kind": "action", "action": "choose_terminal_artifact",
                                    "result": chosen, "state": snapshot(e)})
                result = {"seed": seed, "name": name, "pve_cleared": battle_no,
                          "duel": duel, "completed": bool(duel.get("winner") == "challenger"),
                          "opening": {"relic": relic, "initial": pick,
                                      "initial_candidates": initial, "region": "龙心谷"},
                          "actions": log}
                Path(out_path).write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
                print(json.dumps({k: result[k] for k in ("seed", "name", "pve_cleared", "duel", "completed")}, ensure_ascii=False))
                return result
            result = {"seed": seed, "name": name, "pve_cleared": battle_no,
                      "duel": duel, "completed": False,
                      "opening": {"relic": relic, "initial": pick,
                                  "initial_candidates": initial, "region": "龙心谷"},
                      "actions": log}
            Path(out_path).write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            print(json.dumps({k: result[k] for k in ("seed", "name", "pve_cleared", "duel", "completed")}, ensure_ascii=False))
            return result

    result = {"seed": seed, "name": name, "pve_cleared": sum(
        1 for x in log if x.get("action") == "battle_end"),
              "duel": None, "completed": False,
              "opening": {"relic": relic, "initial": pick,
                          "initial_candidates": initial, "region": "龙心谷"},
              "actions": log}
    Path(out_path).write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(json.dumps({k: result[k] for k in ("seed", "name", "pve_cleared", "duel", "completed")}, ensure_ascii=False))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--name", default="闻照野")
    parser.add_argument("--slots", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    run(args.seed, args.name, args.slots, args.out)
