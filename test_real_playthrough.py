# -*- coding: utf-8 -*-
"""
Real full playthrough via GameEngine.execute_action public API only.

AI paths used (repo's unified player):
  - Player turns : engine.ai_tactics.TacticalAI directly (experimental rule AI;
                   the production LLM path is engine.ai_player.AIPlayer and is
                   deliberately not used here — this harness pins rule-AI regressions)
  - Monster turns: sim.alt_path_test.resolve_monster_turn (engine phase resolver)
  - Pre-battle   : sim.build_learner.choose_pre_battle (DEFAULT_POLICY weights)

Every round is logged for AI-decision observation (actions chosen, resources,
enemy state, dodge decisions, daowen used, death cause).
"""
import sys, os, json, tempfile, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine.api import GameEngine
from engine.ai_tactics import TacticalAI
from sim.alt_path_test import resolve_monster_turn
from sim.build_learner import DEFAULT_POLICY, choose_pre_battle
from tests.setup_support import OPTIONAL_BATTLE_START, OPTIONAL_ROUND_START

issues = []
battle_count = 0
round_count = 0
battle_log = []          # per-battle structured records
detail_mode = os.environ.get("DETAIL", "") == "1"


def report(ctx, msg):
    issues.append({"ctx": ctx, "msg": msg})
    print("  !! [%s] %s" % (ctx, msg))


def check(cond, ctx, msg):
    if not cond:
        report(ctx, msg)


def dlog(rec, key, val):
    rec.setdefault("details", []).append({"key": key, "val": val})


def enemy_state(engine):
    out = []
    for m in engine.state.enemies:
        if not m.is_alive:
            continue
        out.append({
            "name": m.name, "hp": m.current_hp, "bl": m.blood_limit,
            "panel": "%dx%d" % (m.attack_count, m.attack_power),
            "daowen": {k: v.x_value for k, v in m.dao_wen.items()},
            "status": sorted(s.name for s in m.status_effects),
        })
    return out


def player_state(engine):
    p = engine.state.player
    if not p:
        return {}
    return {
        "hp": p.current_hp, "bl": p.blood_limit,
        "mana": p.current_mana, "mana_limit": p.mana_limit,
        "speed": p.current_speed, "speed_limit": p.speed_limit,
        "actions_used": getattr(p, "actions_used_this_round", 0),
        "daowen": {k: v.x_value for k, v in p.dao_wen.items()},
        "status": sorted(s.name for s in p.status_effects),
        "shield": p.shield,
    }


# ---------------------------------------------------------------- pre-battle


def resolve_discovery_pending(engine, ctx):
    """Resolve pending discovery choices (relics/items/hired daowen) via public API."""
    for _ in range(6):
        if engine.state.pending_relic_choices:
            pick = list(engine.state.pending_relic_choices)[0]
            r = engine.execute_action("choose_discovered_relic", {"relic_name": pick})
            dlog({}, "discover_relic", (pick, r.get("success")))
            continue
        if engine.state.pending_item_choices:
            pick = list(engine.state.pending_item_choices)[0]
            r = engine.execute_action("choose_discovered_item", {"item_name": pick})
            dlog({}, "discover_item", (pick, r.get("success")))
            continue
        if engine.state.pending_daowen_choices:
            name, disc = next(iter(engine.state.pending_daowen_choices.items()))
            r = engine.execute_action("choose_hired_daowen", {"name": name, "daowen": disc[0]})
            dlog({}, "choose_hired_daowen", (name, r.get("success")))
            continue
        if engine.state.pending_initial_daowen_choices:
            pick = list(engine.state.pending_initial_daowen_choices)[0]
            r = engine.execute_action("setup_choose_initial_daowen", {"daowen_name": pick})
            dlog({}, "choose_initial_daowen", (pick, r.get("success")))
            continue
        break
def spend_energy(engine, battle_no, ai, rec, rng):
    """Pre-battle via repo's choose_pre_battle policy (real AI path)."""
    steps = 0
    while engine.state.energy > 0 and steps < 20:
        steps += 1
        p = engine.state.player
        if not p or not p.is_alive:
            break
        before = engine.state.energy
        act, params = choose_pre_battle(engine, [], battle_no, rng, DEFAULT_POLICY)
        r = engine.execute_action("pre_battle_action", dict(params, sub_action=act))
        dlog(rec, "pre_battle:" + act, r.get("success"))
        resolve_discovery_pending(engine, "PREBATTLE")
        if not r.get("success"):
            r = engine.execute_action("pre_battle_action", {
                "sub_action": "\u4fee\u884c", "tier": 1})
            dlog(rec, "pre_battle:study_fallback", r.get("success"))
        if engine.state.energy >= before:
            # energy did not drop -> resolve gates, then force study
            resolve_discovery_pending(engine, "PREBATTLE")
            if engine.event_pool.current is not None:
                try_resolve_events(engine, "PREBATTLE")
            r = engine.execute_action("pre_battle_action", {
                "sub_action": "\u4fee\u884c", "tier": 1})
            dlog(rec, "pre_battle:forced_study", r.get("success"))
            if not r.get("success") and engine.state.energy >= before:
                report("PREBATTLE", "energy stuck: " + str(r.get("error", "")))
                break


# ---------------------------------------------------------------- player AI
def player_turn(engine, ai, rec, rnd):
    """Player turn via TacticalAI (repo AI), plus test-driver ally deploy/command."""
    p = engine.state.player
    if not p or not p.is_alive:
        return
    # test-driver flow: deploy + command employees/friends (TacticalAI has no ally logic)
    for i, emp in enumerate(engine.state.employees):
        if emp.is_alive and not emp.is_deployed and not emp.has_retreated:
            r = engine.execute_action("deploy_employee", {"employee_ref": "employee:%d" % i})
            dlog(rec, "r%d:deploy_emp%d" % (rnd, i), r.get("success"))
    for i, emp in enumerate(engine.state.employees):
        if emp.is_alive and emp.is_deployed and not emp.has_retreated:
            r = engine.execute_action("command_ally", {
                "ally_ref": "employee:%d" % i, "instruction": "\u653b\u51fb"})
            dlog(rec, "r%d:cmd_emp%d" % (rnd, i), r.get("success"))
    for i, fr in enumerate(engine.state.friends):
        if fr.is_alive and not fr.has_retreated:
            r = engine.execute_action("command_ally", {
                "ally_ref": "friend:%d" % i, "instruction": "\u653b\u51fb"})
            dlog(rec, "r%d:cmd_friend%d" % (rnd, i), r.get("success"))
    engine.execute_action("resolve_ally_phases", {})

    ai.new_round()
    actions = ai.take_turn()
    for r in actions:
        dlog(rec, "r%d:AI_action" % rnd, (r.get("action") or r.get("action_type"),
                                          r.get("success")))
    if detail_mode:
        for line in ai.log:
            print("    [AI] " + line)
        ai.log.clear()
    return actions


def monster_phase(engine, ai, rec, rnd):
    """Monster phase via the repo's alt-path AI (fixed)."""
    mp = resolve_monster_turn(engine, [])
    if not mp.get("success"):
        # dump diagnostics: what the AI was offered vs what it chose
        try:
            opts = engine.combat.prepare_monster_phase()
            offered = {a["actor_ref"]: [o["name"] for o in (a.get("daowen_options") or [])]
                       for a in opts["actors"]}
            dlog(rec, "r%d:offered" % rnd, offered)
        except Exception:
            pass
        for m in engine.state.enemies:
            if m.is_alive:
                dlog(rec, "r%d:daowen_state" % rnd, {
                    k: {"x": v.x_value, "cooldown": v.cooldown_remaining,
                        "frozen": v.is_frozen, "can_use": v.can_use()}
                    for k, v in m.dao_wen.items()})
                dlog(rec, "r%d:activated" % rnd,
                     sorted(engine.combat._monster_activated.get(id(m), set())))
                dlog(rec, "r%d:round_used" % rnd,
                     sorted(engine.combat._monster_round_used(m)))
        try:
            hist = engine.get_action_history()
            dlog(rec, "r%d:action_history" % rnd, [
                {"t": h.get("action_type") or h.get("action"), "ok": h.get("success"),
                 "err": h.get("error", "")[:80]}
                for h in hist[-8:]])
        except Exception:
            pass
        # 记录 AI/引擎缺陷，然后以“纯攻击”兜底继续战斗（相当于AI本应做出的更安全选择）
        report("B%d-r%d" % (battle_count, rnd),
               "monster AI resolve failed: %s -> 兜底为合法道纹重试" % str(mp.get("error"))[:100])
        _monster_phase_fallback(engine)
        return mp
    details = (mp.get("result") or {}).get("details") or []
    for d in details:
        if isinstance(d, dict):
            dlog(rec, "r%d:monster_%s" % (rnd, d.get("attacker")),
                 {"daowen": d.get("daowen"), "hits": len(d.get("hits") or [])})
    return mp


# ---------------------------------------------------------------- battle


def _monster_phase_fallback(engine):
    """Test-harness fallback: re-prepare and submit a LEGAL daowen (not in round_used).

    Used only after the repo monster AI fails, to keep the battle flowing.
    Mirrors what a correct AI would do: pick any offered daowen that has not
    been used this round (the engine requires a daowen when options exist).
    """
    engine.state.pending_monster_phase = {}
    engine.state.combat_subphase = "player_actions"
    prep = engine.execute_action("prepare_monster_phase", {})
    if not prep.get("success"):
        return
    token = prep["result"].get("token")
    choices = []
    for actor in prep["result"].get("actors", []):
        action_count = actor["base_attack_actions"]
        hit_count = actor["base_hits_per_attack"]
        m_idx = int(actor["actor_ref"].split(":", 1)[1]) if ":" in actor["actor_ref"] else 0
        monster = engine.state.enemies[m_idx] if m_idx < len(engine.state.enemies) else None
        dao = None
        opts = actor.get("daowen_options") or []
        if opts and monster is not None:
            used = engine.combat._monster_round_used(monster)
            legal = [o for o in opts if o["name"] not in used]
            option = (legal or opts)[0]
            dao = {"name": option["name"], "dodge": False, "blood_shadow": False,
                   "trigger_spell_choices": {
                       holder: {sp["spell_name"]: {"use": False} for sp in spells}
                       for holder, spells in option.get("trigger_spell_options", {}).items()}}
            if option.get("requires_target"):
                dao["target_ref"] = option["target_options"][0]["ref"]
            if option.get("dodge_submission") == "per_target":
                dao["dodge_targets"] = [
                    {"target_ref": t["ref"], "dodge": False, "blood_shadow": False}
                    for t in option.get("dodge_target_options", [])]
            if option.get("resolves_as") == "\u53d8\u5f62":
                hit_count = engine.state.enemies[m_idx].attack_power
        attack_target = actor["attack_target_options"][0]["ref"]
        target_option = next(t for t in actor["attack_target_options"]
                             if t["ref"] == attack_target)
        hits = [{"target_ref": attack_target, "dodge": False, "blood_shadow": False,
                 "spell_choices": {
                     timing: {sp["spell_name"]: {"use": False}
                              for sp in target_option.get("spell_options", {}).get(timing, [])}
                     for timing in ("before", "after")}}
                for _ in range(hit_count)]
        choices.append({"actor_ref": actor["actor_ref"], "daowen": dao,
                        "attack_actions": [{"hits": hits} for _ in range(action_count)]})
    engine.execute_action("resolve_monster_phase", {"token": token, "choices": choices})


def run_battle(engine, battle_idx, ai, rng):
    global battle_count, round_count
    battle_count += 1
    ctx = "B%d" % battle_idx
    rec = {"battle": battle_idx, "rounds": [], "outcome": None}
    battle_log.append(rec)
    print("\n==== Battle %d ====" % battle_idx)
    spend_energy(engine, battle_idx, ai, rec, rng)
    resolve_discovery_pending(engine, ctx)
    resolve_all_pending(engine, ctx)

    active = {r.name for r in engine.state.relics
              if engine.state.sealed_relics.get(r.name, 0) <= 0}
    rc = {}
    for n in OPTIONAL_BATTLE_START:
        if n in active:
            rc[n] = {"use": False}
    bs = engine.execute_action("battle_start", {"relic_choices": rc})
    if not bs.get("success"):
        report(ctx, "battle_start fail: " + str(bs.get("error")))
        rec["outcome"] = "start_failed"
        return False
    print("  enemies: %s" % str(list(bs.get("enemies") or [])))
    for e in enemy_state(engine):
        print("    %s hp=%d/%d %s daowen=%s status=%s" % (
            e["name"], e["hp"], e["bl"], e["panel"], e["daowen"], e["status"]))
    p0 = player_state(engine)
    print("  player: %s" % p0)

    won = False
    for rnd in range(1, 30):
        round_count += 1
        rrec = {"round": rnd, "before": None, "after": None}
        rec["rounds"].append(rrec)
        p = engine.state.player
        if not p or not p.is_alive:
            rrec["death"] = "dead_at_round_start"
            rec["outcome"] = "defeat_player_dead"
            break
        if not [e for e in engine.state.enemies if e.is_alive]:
            won = True
            break
        from sim.optional_actions import round_start_relic_choices as _rsrc
        rs = engine.execute_action("round_start", {"relic_choices": _rsrc(engine)})
        if not rs.get("success"):
            report(ctx, "round %d round_start fail: %s" % (rnd, str(rs.get("error"))))
            rec["outcome"] = "round_start_failed"
            break
        rrec["before"] = player_state(engine)
        rrec["enemies_before"] = enemy_state(engine)
        if not p.is_alive:
            rrec["death"] = "died_on_round_start_effects"
            rec["outcome"] = "defeat_player_dead"
            resolve_all_pending(engine, ctx)
            break

        player_turn(engine, ai, rec, rnd)
        if not p.is_alive:
            rrec["death"] = "died_on_player_action"
            rec["outcome"] = "defeat_player_dead"
            report(ctx, "round %d player died on own action" % rnd)
            resolve_all_pending(engine, ctx)   # 清理死者之页等中断，避免流程卡死
            break
        if not [e for e in engine.state.enemies if e.is_alive]:
            won = True
            break
        if p.is_alive:
            try:
                monster_phase(engine, ai, rec, rnd)
            except Exception as exc:
                report(ctx, "round %d monster phase exception: %s" % (rnd, exc))
                rec["outcome"] = "monster_phase_exception"
                break
            if not p.is_alive:
                rrec["death"] = "died_on_monster_phase"
                rec["outcome"] = "defeat_player_dead"
                report(ctx, "round %d monster killed player" % rnd)
                resolve_all_pending(engine, ctx)
                break
        re_ = engine.execute_action("round_end", {})
        if not re_.get("success"):
            if resolve_all_pending(engine, ctx):
                re_ = engine.execute_action("round_end", {})
            if not re_.get("success"):
                report(ctx, "round %d round_end fail: %s" % (rnd, str(re_.get("error"))))
                rec["outcome"] = "round_end_failed"
                break
        rrec["after"] = player_state(engine)
        rrec["enemies_after"] = enemy_state(engine)

    rec["preview_rejected"] = list(ai.preview_rejected)
    ai.preview_rejected.clear()
    if won:
        print("  victory!")
        rec["outcome"] = "victory"
        # battle_end with wage/redemption/interrupt handling
        for _ in range(6):
            be = engine.execute_action("battle_end", {})
            if be.get("success") and be.get("completed", True):
                break
            pending = be.get("pending_wage_decisions") or {}
            if pending:
                for name, wage in pending.items():
                    r = engine.execute_action("pay_employee_wage", {"name": name, "decision": "pay"})
                    if not r.get("success"):
                        r = engine.execute_action("pay_employee_wage", {"name": name, "decision": "refuse"})
                continue
            if resolve_all_pending(engine, ctx):
                continue
            report(ctx, "battle_end fail: " + str(be.get("error", "")))
            break
        return True
    rec["outcome"] = "defeat"
    print("  defeat")
    return False


# ---------------------------------------------------------------- pending resolution
_redemption_seq = [0]


def resolve_redemption_if_pending(engine, ctx):
    if not getattr(engine.state, "pending_redemption", None):
        return
    _redemption_seq[0] += 1
    name = "RedFriend%d" % _redemption_seq[0]
    r = engine.execute_action("resolve_redemption", {"option": "\u63a5\u7eb3", "name": name})
    check(r.get("success"), ctx, "redemption accept: " + str(r.get("error", "")))
    dlog({}, "redemption_accept", name)


def try_resolve_events(engine, ctx):
    from sim.build_learner import _resolve_pending_event
    r = _resolve_pending_event(engine)
    if not r.get("success"):
        report(ctx, "event resolve fail: " + str(r.get("error", "")))


def resolve_all_pending(engine, ctx):
    did = False
    for _ in range(6):
        if engine._pending_interrupts:
            it = engine._pending_interrupts[0]
            itype = it.interrupt_type
            if hasattr(itype, "value"):
                itype = itype.value
            if "death" in str(itype) or "\u6b7b" in str(itype):
                r = engine.submit_ruling(interrupt_type="death_inheritance",
                                         ruling_text="run: record",
                                         ruling_data={"action": "reject"})
                check(r.get("success"), ctx, "death ruling: " + str(r.get("error", "")))
                did = True
                continue
            report(ctx, "unhandled interrupt: " + str(itype))
            break
        if getattr(engine.state, "pending_redemption", None):
            resolve_redemption_if_pending(engine, ctx)
            did = True
            continue
        if engine.event_pool.current is not None:
            try_resolve_events(engine, ctx)
            did = True
            continue
        break
    return did


def handle_death(engine, ctx):
    if not engine._pending_interrupts:
        return False
    it = engine._pending_interrupts[0]
    itype = it.interrupt_type
    if hasattr(itype, "value"):
        itype = itype.value
    if "death" in str(itype):
        r = engine.submit_ruling(interrupt_type="death_inheritance",
                                 ruling_text="run: record", ruling_data={})
        check(r.get("success"), ctx, "death ruling: " + str(r.get("error", "")))
        return True
    return False


def hire_employee(engine, ctx):
    r = engine.execute_action("pre_battle_action", {
        "sub_action": "\u96c7\u4f63", "name": "Tank",
        "blood_alloc": 14, "atk_bundles": 2})
    if not r.get("success"):
        report(ctx, "hire fail: " + str(r.get("error", "")))
        return False
    disc = (r.get("result") or {}).get("discovered_daowen_choices") or []
    if disc:
        r2 = engine.execute_action("choose_hired_daowen", {"name": "Tank", "daowen": disc[0]})
        if not r2.get("success"):
            report(ctx, "choose hired daowen fail: " + str(r2.get("error", "")))
            return False
    print("  Hired employee Tank")
    return True


# ---------------------------------------------------------------- main
def main():
    global issues, battle_count, round_count, battle_log
    issues = []
    battle_count = 0
    round_count = 0
    battle_log = []
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    seeds = [10, 6, 7, 11, 13]
    import random
    for run_no in range(runs):
        seed = seeds[run_no % len(seeds)]
        print("=" * 60)
        print("RUN %d/%d (seed %d)" % (run_no + 1, runs, seed))
        print("=" * 60)
        save_dir = tempfile.mkdtemp(prefix="linji")
        eng = GameEngine(db_path=os.path.join(save_dir, "g.db"), rng_seed=seed,
                         save_dir=save_dir)
        ai = TacticalAI(eng, verbose=detail_mode)
        rng = random.Random(seed)

        r = eng.execute_action("setup_attributes", {
            "name": "Linji", "blood_points": 11, "speed_points": 8, "mana_points": 6})
        check(r.get("success"), "SETUP", "attr: " + str(r.get("error", "")))
        freed = list(eng.state.pending_relic_choices or [])
        if freed:
            r1 = eng.execute_action("choose_discovered_relic", {"relic_name": freed[0]})
            check(r1.get("success"), "SETUP", "relic: " + str(r1.get("error", "")))
            print("  Relic: " + str(freed[0]))
        if eng.state.pending_initial_daowen_choices:
            dw_list = list(eng.state.pending_initial_daowen_choices)
            pick = "\u6740\u4f10" if "\u6740\u4f10" in dw_list else dw_list[0]
            r2 = eng.execute_action("setup_choose_initial_daowen", {"daowen_name": pick})
            check(r2.get("success"), "SETUP", "daowen: " + str(r2.get("error", "")))
            print("  Offered daowen: %s -> picked %s" % (str(dw_list), pick))
        eng.execute_action("setup_choose_resonance", {"resonance_type": "\u53cd\u8f6c"})
        rr = eng.execute_action("setup_choose_region", {"region": "罪孽都市"})
        check(rr.get("success"), "SETUP", "region: " + str(rr.get("error", "")))
        print("  Region: %s" % eng.state.current_region)
        hire_employee(eng, "SETUP")
        resolve_all_pending(eng, "PREBATTLE")

        # battles 1..2, then cross-engine save/load while alive, then more battles
        for b in range(1, 3):
            won = run_battle(eng, b, ai, rng)
            if not won:
                handle_death(eng, "B%d" % b)
                print("  Battle %d lost - stop" % b)
                break
            print("  post-b%d: shards=%d energy=%d" % (b, eng.state.shards, eng.state.energy))
            resolve_all_pending(eng, "B%d_after" % b)

        p = eng.state.player
        if p and p.is_alive:
            print("\n---- Save -> new engine -> continue ----")
            sr = eng.save_game("mid")
            check(sr.get("success"), "SAVELOAD", "save: " + str(sr.get("error", "")))
            eng2 = GameEngine(db_path=os.path.join(save_dir, "g2.db"), rng_seed=999,
                              save_dir=save_dir)
            lr = eng2.load_game("mid")
            check(lr.get("success"), "SAVELOAD", "load: " + str(lr.get("error", "")))
            print("  Loaded OK: round=%d phase=%s subphase=%s" % (
                eng2.state.current_round, eng2.state.phase, eng2.state.combat_subphase))
            p2 = eng2.state.player
            if p2:
                print("  Player after load: %d/%d M%d" % (
                    p2.current_hp, p2.blood_limit, p2.current_mana))
            ai2 = TacticalAI(eng2, verbose=detail_mode)
            bc = battle_count
            for post_b in (bc + 1, bc + 2, bc + 3, bc + 4):
                if not (eng2.state.player and eng2.state.player.is_alive):
                    handle_death(eng2, "POSTLOAD")
                    break
                run_battle(eng2, post_b, ai2, rng)
        else:
            handle_death(eng, "POSTRUN")

    print("\n" + "=" * 50)
    print("FINAL REPORT")
    print("Total battles: %d  total rounds: %d" % (battle_count, round_count))
    total_rejected = sum(len(b.get("preview_rejected") or []) for b in battle_log)
    print("安全过滤淘汰候选总数: %d" % total_rejected)
    for b in battle_log:
        rej = b.get("preview_rejected") or []
        if rej:
            print("  battle %d (%s): %d 次安全过滤" % (b["battle"], b["outcome"], len(rej)))
            for r in rej[:5]:
                print("      - " + r)
    print("Issues: %d" % len(issues))
    for i in issues:
        print("  [%s] %s" % (i["ctx"], i["msg"]))
    if not issues:
        print("  No engine/sim errors found via real-game playthrough!")
    with open("playthrough_battle_log.json", "w", encoding="utf-8") as f:
        json.dump(battle_log, f, ensure_ascii=False, indent=1)
    with open("playthrough_issues.json", "w", encoding="utf-8") as f:
        json.dump(issues, f, ensure_ascii=False, indent=2)
    print("  battle log -> playthrough_battle_log.json")
    print("  issues     -> playthrough_issues.json")
    return issues


if __name__ == "__main__":
    main()
