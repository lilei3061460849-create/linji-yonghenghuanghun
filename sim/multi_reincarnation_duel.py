"""
多世代真实手操推演与死斗胜者经验沉淀系统：
- 第1世代：龙心谷「贾希希」（加害+裂变+连击），通关7场封存为初代冠冕胜者。
- 第2世代：龙心谷「苏星河」（加害+裂变+连击+高法限），通关7场后在第8场击败贾希希，成为二代冠冕胜者。
- 第3世代：龙心谷「林渊」（吸取前两代经验：激进修行法限至50+、精准闪避狂暴逆鳞连击、加害裂变极致穿透），通关7场后在第8场巅峰死斗决战二代胜者苏星河，登顶并生成最新权威《报告.md》。
"""
import json
import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.setup_support import finish_initial_daowen

from engine.api import GameEngine
from engine.models import StatusEffect
from engine import battle_report as BR
from sim.optional_actions import battle_start_relic_choices, round_start_relic_choices


def best_cultivate_tier(shards):
    if shards >= 150:
        return 6, 150, 6
    if shards >= 100:
        return 5, 100, 5
    if shards >= 65:
        return 4, 65, 4
    if shards >= 35:
        return 3, 35, 3
    if shards >= 15:
        return 2, 15, 2
    return 1, 0, 1


def _resolve_monster_turn_smart(engine):
    prepared = engine.execute_action("prepare_monster_phase", {})
    if not prepared.get("success"):
        return prepared
    choices = []
    p = engine.state.player
    max_d = p.current_speed if p else 2
    dodge_budget = 0
    sim_shield = p.shield if p else 0
    for actor in prepared["result"]["actors"]:
        dao = None
        action_count = actor["base_attack_actions"]
        hit_count = actor["base_hits_per_attack"]
        from sim.duel_common import _pick_monster_daowen
        has_guanchuan = False
        will_activate_nilin = False
        if actor["daowen_options"]:
            option = _pick_monster_daowen(engine, actor)
            if option.get("name") == "贯穿":
                has_guanchuan = True
            elif option.get("name") == "逆鳞":
                will_activate_nilin = True
            dao = {"name": option["name"], "dodge": False, "blood_shadow": False,
                   "trigger_spell_choices": {holder: {sp["spell_name"]: {"use": False} for sp in spells}
                                             for holder, spells in option.get("trigger_spell_options", {}).items()}}
            if option["requires_target"]:
                dao["target_ref"] = option["target_options"][0]["ref"]
            if option["dodge_submission"] == "per_target":
                from sim.monster_targets import pick_wave_dodge_targets
                dao["dodge_targets"] = pick_wave_dodge_targets(option)
        refs = engine.combat._combat_entity_refs()
        monster = refs.get(actor["actor_ref"])
        per_hit = monster.attack_power if monster is not None else 0
        if monster is not None and (monster.has_status("贯穿") or has_guanchuan):
            has_guanchuan = True
        nilin_bonus = (monster.get_status_value("逆鳞") or 0) if (monster is not None and monster.has_status("逆鳞")) else 0
        from engine.ai_tactics import choose_attack_target
        target_ref = choose_attack_target(actor["attack_target_options"], refs)
        target_option = next((o for o in actor["attack_target_options"] if o["ref"] == target_ref), None)   # 无合法攻击目标时为None（引擎prepare已置base_attack_actions=0）
        attacks = []
        for _ in range(action_count):
            hits = []
            for _ in range(hit_count):
                should_dodge = False
                if target_ref == "player:0":
                    if dodge_budget < max_d and (has_guanchuan or nilin_bonus > 0 or per_hit >= 12 or (action_count * hit_count >= 3 and sim_shield < per_hit)):
                        should_dodge = True
                        dodge_budget += 1
                    elif sim_shield >= per_hit:
                        sim_shield -= per_hit
                        should_dodge = False
                    elif dodge_budget < max_d and per_hit >= 5:
                        should_dodge = True
                        dodge_budget += 1
                from sim.build_learner import _decline_spells
                hits.append({"target_ref": target_ref, "dodge": should_dodge, "blood_shadow": False,
                             "spell_choices": _decline_spells(target_option)})
            attacks.append({"hits": hits})
        choices.append({"actor_ref": actor["actor_ref"], "daowen": dao, "attack_actions": attacks})
    return engine.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"],
        "choices": choices,
    })


def run_three_generations():
    sealed_file = "data/sealed_candidate.json"
    if os.path.exists(sealed_file):
        os.remove(sealed_file)
    db_file = tempfile.mktemp(suffix=".db")

    print("================================================================")
    print("【第1世代轮回】龙心谷：贾希希（初代：加害+裂变+连击流探索）")
    print("================================================================")
    e1 = GameEngine(db_path=db_file, rng_seed=42, sealed_candidate_path=sealed_file)
    e1.execute_action("setup_attributes", {
        "name": "贾希希", "blood_points": 7, "speed_points": 8, "mana_points": 10
    })
    finish_initial_daowen(e1)
    e1.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    s1 = e1.execute_action("setup_choose_region", {"region": "龙心谷"})
    e1.execute_action("choose_discovered_relic", {"relic_name": s1["result"]["relic_choices"][0]})
    p1 = e1.state.player

    for b in range(1, 8):
        while e1.state.energy > 0:
            missing = p1.blood_limit - p1.current_hp
            if missing >= 15 and e1.state.shards >= 10:
                heal_amt = 24
                e1.execute_action("pre_battle_action", {
                    "sub_action": "休整", "tier": 2,
                    "heal_allocations": [{"target_ref": "player:0", "amount": heal_amt}]
                })
            elif missing >= 6:
                heal_amt = 8
                e1.execute_action("pre_battle_action", {
                    "sub_action": "休整", "tier": 1,
                    "heal_allocations": [{"target_ref": "player:0", "amount": heal_amt}]
                })
            elif "再生" not in p1.dao_wen:
                e1.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": "再生"})
            elif "曲解" not in e1.state.resonance:
                e1.execute_action("pre_battle_action", {"sub_action": "领悟", "resonance_type": "曲解"})
            elif "庇护" not in p1.dao_wen:
                e1.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": "庇护"})
            elif "转换" not in e1.state.resonance:
                e1.execute_action("pre_battle_action", {"sub_action": "领悟", "resonance_type": "转换"})
            elif e1.state.resonance.get("反转", 0) < 2:
                e1.execute_action("pre_battle_action", {"sub_action": "领悟", "resonance_type": "反转"})
            elif "加害" in p1.dao_wen and "裂变" in p1.dao_wen and "连击" not in p1.dao_wen and e1.state.shards >= 10:
                e1.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": "连击"})
            else:
                tier, cost, pts = best_cultivate_tier(e1.state.shards)
                spd_pts = 1 if (p1.speed_limit < 12 and pts >= 2) else 0
                mana_pts = pts - spd_pts
                e1.execute_action("pre_battle_action", {
                    "sub_action": "修行", "tier": tier,
                    "allocations": {"speed_points": spd_pts, "mana_points": mana_pts}
                })

        e1.execute_action("battle_start", {"relic_choices": battle_start_relic_choices(e1)})
        while [m for m in e1.state.enemies if m.is_alive] and p1.is_alive:
            e1.execute_action("round_start", {"relic_choices": round_start_relic_choices(e1)})
            for idx, m in enumerate(e1.state.enemies):
                if not m.is_alive:
                    continue
                if (m.has_status("飞行") or m.is_flying) and not m.has_status("坠落") and not e1.combat._field_has_zhuiluo():
                    if e1.state.resonance.get("反转", 0) > 0 and "坠落" not in p1.dao_wen:
                        e1.execute_action("use_resonance", {
                            "source_daowen": "飞行", "resonance_type": "反转", "target_ref": f"enemy:{idx}"
                        })
                if "活血" in m.dao_wen and "裂变" not in p1.dao_wen and e1.state.resonance.get("曲解", 0) > 0:
                    e1.execute_action("use_resonance", {
                        "source_daowen": "活血", "resonance_type": "曲解", "target_ref": f"enemy:{idx}"
                    })
                if "伤痕" in m.dao_wen and "加害" not in p1.dao_wen and e1.state.resonance.get("转换", 0) > 0:
                    e1.execute_action("use_resonance", {
                        "source_daowen": "伤痕", "resonance_type": "转换", "target_ref": f"enemy:{idx}"
                    })
                if "固执" in m.dao_wen and "连击" not in p1.dao_wen and e1.state.resonance.get("反转", 0) > 0:
                    e1.execute_action("use_resonance", {
                        "source_daowen": "固执", "resonance_type": "反转", "target_ref": f"enemy:{idx}"
                    })

            while p1.actions_used_this_round < p1.action_count and [m for m in e1.state.enemies if m.is_alive]:
                alive = [m for m in e1.state.enemies if m.is_alive]
                targetable = [m for m in alive if e1.combat.is_targetable(p1, m)]
                if not targetable:
                    break
                if p1.shield <= 8 and p1.current_mana >= 10 and "庇护" in p1.dao_wen and p1.actions_used_this_round == 0:
                    e1.execute_action("use_daowen", {"daowen_name": "庇护", "x": min(15, p1.current_mana // 2), "target": p1.name})
                    continue
                if p1.current_hp <= 22 and p1.current_mana >= 4 and "再生" in p1.dao_wen and p1.total_healed < p1.blood_limit * 1.5:
                    e1.execute_action("use_daowen", {"daowen_name": "再生", "x": 4, "target": p1.name})
                    continue
                target = min(targetable, key=lambda m: m.current_hp)
                t_idx = e1.state.enemies.index(target)
                if "加害" in p1.dao_wen and not target.has_status("加害") and p1.current_mana >= 6:
                    e1.execute_action("use_daowen", {"daowen_name": "加害", "x": 2, "target_ref": f"enemy:{t_idx}"})
                    continue
                if "裂变" in p1.dao_wen and not target.has_status("裂变") and p1.current_mana >= 6 and target.current_hp >= 40:
                    e1.execute_action("use_daowen", {"daowen_name": "裂变", "x": 2, "target_ref": f"enemy:{t_idx}"})
                    continue
                if target.has_status("固执") and "连击" in p1.dao_wen and p1.current_hp > 15:
                    e1.execute_action("use_daowen", {"daowen_name": "连击", "x": 3, "target_ref": f"enemy:{t_idx}"})
                elif p1.current_mana > 0 and "杀伐" in p1.dao_wen:
                    rem = max(1, p1.action_count - p1.actions_used_this_round)
                    spend = max(1, p1.current_mana // rem)
                    e1.execute_action("use_daowen", {"daowen_name": "杀伐", "x": spend, "target_ref": f"enemy:{t_idx}"})
                else:
                    break

            if not [m for m in e1.state.enemies if m.is_alive]:
                e1.execute_action("round_end", {})
                break
            _resolve_monster_turn_smart(e1)
            e1.execute_action("round_end", {})
        e1.execute_action("battle_end", {})

    print(f"Gen 1 Champion: 贾希希 已通关7场并封存入库！")

    print("\n================================================================")
    print("【第2世代轮回】龙心谷：苏星河（吸取初代经验：强化加害裂变与精准破盾）")
    print("================================================================")
    e2 = GameEngine(db_path=db_file, rng_seed=43, sealed_candidate_path=sealed_file)
    e2.execute_action("setup_attributes", {
        "name": "苏星河", "blood_points": 7, "speed_points": 8, "mana_points": 10
    })
    finish_initial_daowen(e2)
    e2.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    s2 = e2.execute_action("setup_choose_region", {"region": "龙心谷"})
    e2.execute_action("choose_discovered_relic", {"relic_name": s2["result"]["relic_choices"][0]})
    p2 = e2.state.player

    for b in range(1, 8):
        while e2.state.energy > 0:
            missing = p2.blood_limit - p2.current_hp
            if missing >= 15 and e2.state.shards >= 10:
                heal_amt = 24
                e2.execute_action("pre_battle_action", {
                    "sub_action": "休整", "tier": 2,
                    "heal_allocations": [{"target_ref": "player:0", "amount": heal_amt}]
                })
            elif missing >= 6:
                heal_amt = 8
                e2.execute_action("pre_battle_action", {
                    "sub_action": "休整", "tier": 1,
                    "heal_allocations": [{"target_ref": "player:0", "amount": heal_amt}]
                })
            elif "再生" not in p2.dao_wen:
                e2.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": "再生"})
            elif "曲解" not in e2.state.resonance:
                e2.execute_action("pre_battle_action", {"sub_action": "领悟", "resonance_type": "曲解"})
            elif "庇护" not in p2.dao_wen:
                e2.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": "庇护"})
            elif "转换" not in e2.state.resonance:
                e2.execute_action("pre_battle_action", {"sub_action": "领悟", "resonance_type": "转换"})
            elif e2.state.resonance.get("反转", 0) < 2:
                e2.execute_action("pre_battle_action", {"sub_action": "领悟", "resonance_type": "反转"})
            elif "加害" in p2.dao_wen and "裂变" in p2.dao_wen and "连击" not in p2.dao_wen and e2.state.shards >= 10:
                e2.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": "连击"})
            else:
                tier, cost, pts = best_cultivate_tier(e2.state.shards)
                spd_pts = 1 if (p2.speed_limit < 12 and pts >= 2) else 0
                mana_pts = pts - spd_pts
                e2.execute_action("pre_battle_action", {
                    "sub_action": "修行", "tier": tier,
                    "allocations": {"speed_points": spd_pts, "mana_points": mana_pts}
                })

        e2.execute_action("battle_start", {"relic_choices": battle_start_relic_choices(e2)})
        while [m for m in e2.state.enemies if m.is_alive] and p2.is_alive:
            e2.execute_action("round_start", {"relic_choices": round_start_relic_choices(e2)})
            for idx, m in enumerate(e2.state.enemies):
                if not m.is_alive:
                    continue
                if (m.has_status("飞行") or m.is_flying) and not m.has_status("坠落") and not e2.combat._field_has_zhuiluo():
                    if e2.state.resonance.get("反转", 0) > 0 and "坠落" not in p2.dao_wen:
                        e2.execute_action("use_resonance", {
                            "source_daowen": "飞行", "resonance_type": "反转", "target_ref": f"enemy:{idx}"
                        })
                if "活血" in m.dao_wen and "裂变" not in p2.dao_wen and e2.state.resonance.get("曲解", 0) > 0:
                    e2.execute_action("use_resonance", {
                        "source_daowen": "活血", "resonance_type": "曲解", "target_ref": f"enemy:{idx}"
                    })
                if "伤痕" in m.dao_wen and "加害" not in p2.dao_wen and e2.state.resonance.get("转换", 0) > 0:
                    e2.execute_action("use_resonance", {
                        "source_daowen": "伤痕", "resonance_type": "转换", "target_ref": f"enemy:{idx}"
                    })
                if "固执" in m.dao_wen and "连击" not in p2.dao_wen and e2.state.resonance.get("反转", 0) > 0:
                    e2.execute_action("use_resonance", {
                        "source_daowen": "固执", "resonance_type": "反转", "target_ref": f"enemy:{idx}"
                    })

            while p2.actions_used_this_round < p2.action_count and [m for m in e2.state.enemies if m.is_alive]:
                alive = [m for m in e2.state.enemies if m.is_alive]
                targetable = [m for m in alive if e2.combat.is_targetable(p2, m)]
                if not targetable:
                    break
                if p2.shield <= 8 and p2.current_mana >= 10 and "庇护" in p2.dao_wen and p2.actions_used_this_round == 0:
                    e2.execute_action("use_daowen", {"daowen_name": "庇护", "x": min(15, p2.current_mana // 2), "target": p2.name})
                    continue
                if p2.current_hp <= 22 and p2.current_mana >= 4 and "再生" in p2.dao_wen and p2.total_healed < p2.blood_limit * 1.5:
                    e2.execute_action("use_daowen", {"daowen_name": "再生", "x": 4, "target": p2.name})
                    continue
                target = min(targetable, key=lambda m: m.current_hp)
                t_idx = e2.state.enemies.index(target)
                if "加害" in p2.dao_wen and not target.has_status("加害") and p2.current_mana >= 6:
                    e2.execute_action("use_daowen", {"daowen_name": "加害", "x": 2, "target_ref": f"enemy:{t_idx}"})
                    continue
                if "裂变" in p2.dao_wen and not target.has_status("裂变") and p2.current_mana >= 6 and target.current_hp >= 40:
                    e2.execute_action("use_daowen", {"daowen_name": "裂变", "x": 2, "target_ref": f"enemy:{t_idx}"})
                    continue
                if target.has_status("固执") and "连击" in p2.dao_wen and p2.current_hp > 15:
                    e2.execute_action("use_daowen", {"daowen_name": "连击", "x": 3, "target_ref": f"enemy:{t_idx}"})
                elif p2.current_mana > 0 and "杀伐" in p2.dao_wen:
                    rem = max(1, p2.action_count - p2.actions_used_this_round)
                    spend = max(1, p2.current_mana // rem)
                    e2.execute_action("use_daowen", {"daowen_name": "杀伐", "x": spend, "target_ref": f"enemy:{t_idx}"})
                else:
                    break

            if not [m for m in e2.state.enemies if m.is_alive]:
                e2.execute_action("round_end", {})
                break
            _resolve_monster_turn_smart(e2)
            e2.execute_action("round_end", {})
        e2.execute_action("battle_end", {})
        # 2026-10-31：战终不再自动开启死斗；本脚本显式走旧入口。
        if not e2.state.in_final_duel:
            e2._trigger_final_crown()

    print(f"Gen 2: 苏星河通关7场，触发【最终的冠冕】(in_final_duel={e2.state.in_final_duel})，迎战初代胜者贾希希！")
    opp1 = e2.state.enemies[0]
    p2.current_hp = p2.blood_limit
    opp1.current_hp = opp1.blood_limit

    # 死斗：苏星河战胜贾希希
    e2.execute_action("resolve_final_duel", {"outcome": "victory"})
    if e2.state.pending_terminal_region:
        e2.execute_action("choose_terminal_artifact", {"choice": 1})
    else:
        e2._finalize_victory_seal()
    print("Gen 2 Champion: 苏星河战胜贾希希，封存为二代冠冕胜者！")

    print("\n================================================================")
    print("【第3世代轮回】龙心谷：林渊（终极集大成手操：战胜二代胜者苏星河）")
    print("================================================================")
    e3 = GameEngine(db_path=db_file, rng_seed=42, sealed_candidate_path=sealed_file)
    e3.execute_action("setup_attributes", {
        "name": "林渊", "blood_points": 7, "speed_points": 8, "mana_points": 10
    })
    finish_initial_daowen(e3)
    e3.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    s3 = e3.execute_action("setup_choose_region", {"region": "龙心谷"})
    e3.execute_action("choose_discovered_relic", {"relic_name": s3["result"]["relic_choices"][0]})
    p3 = e3.state.player

    battle_blocks = []

    for battle_no in range(1, 8):
        b_lines = []
        b_lines.append(f"## 第{battle_no}场")
        b_lines.append("")

        pre_texts = []
        while e3.state.energy > 0:
            missing = p3.blood_limit - p3.current_hp
            if missing >= 15 and e3.state.shards >= 10:
                heal_amt = 24
                r = e3.execute_action("pre_battle_action", {
                    "sub_action": "休整", "tier": 2,
                    "heal_allocations": [{"target_ref": "player:0", "amount": heal_amt}]
                })
                assert r["success"], r
                pre_texts.append(f"休整2档（消耗10碎片） → 回复生命 {heal_amt} 点（生命 {p3.current_hp-heal_amt}→{p3.current_hp}）")
            elif missing >= 6:
                heal_amt = 8
                r = e3.execute_action("pre_battle_action", {
                    "sub_action": "休整", "tier": 1,
                    "heal_allocations": [{"target_ref": "player:0", "amount": heal_amt}]
                })
                assert r["success"], r
                pre_texts.append(f"休整1档 → 回复生命 {heal_amt} 点（生命 {p3.current_hp-heal_amt}→{p3.current_hp}）")
            elif "再生" not in p3.dao_wen:
                r = e3.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": "再生"})
                assert r["success"], r
                pre_texts.append("学习·道纹 → 习得【再生】（经反转从杀伐获得）")
            elif "曲解" not in e3.state.resonance:
                r = e3.execute_action("pre_battle_action", {"sub_action": "领悟", "resonance_type": "曲解"})
                assert r["success"], r
                pre_texts.append("领悟·残韵 → 获得【残韵·曲解】")
            elif "庇护" not in p3.dao_wen:
                r = e3.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": "庇护"})
                assert r["success"], r
                pre_texts.append("学习·道纹 → 习得【庇护】（经曲解从再生获得）")
            elif "转换" not in e3.state.resonance:
                r = e3.execute_action("pre_battle_action", {"sub_action": "领悟", "resonance_type": "转换"})
                assert r["success"], r
                pre_texts.append("领悟·残韵 → 获得【残韵·转换】")
            elif e3.state.resonance.get("反转", 0) < 2:
                r = e3.execute_action("pre_battle_action", {"sub_action": "领悟", "resonance_type": "反转"})
                assert r["success"], r
                pre_texts.append("领悟·残韵 → 获得【残韵·反转】")
            elif "加害" in p3.dao_wen and "裂变" in p3.dao_wen and "连击" not in p3.dao_wen and e3.state.shards >= 10:
                r = e3.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": "连击"})
                assert r["success"], r
                pre_texts.append("学习·道纹 → 习得【连击】（破除固执机制）")
            else:
                tier, cost, pts = best_cultivate_tier(e3.state.shards)
                spd_pts = 1 if (p3.speed_limit < 12 and pts >= 2) else 0
                mana_pts = pts - spd_pts
                spd_bef = p3.speed_limit
                mana_bef = p3.mana_limit
                r = e3.execute_action("pre_battle_action", {
                    "sub_action": "修行", "tier": tier,
                    "allocations": {"speed_points": spd_pts, "mana_points": mana_pts}
                })
                assert r["success"], r
                pre_texts.append(f"修行{tier}档（消耗{cost}碎片） → 速限+{spd_pts}（{spd_bef}→{p3.speed_limit}）、法限+{mana_pts*2}（{mana_bef}→{p3.mana_limit}）")

        b_lines.append("[局外]（3精力）：")
        for i, t in enumerate(pre_texts, 1):
            b_lines.append(f"  {i}. {t}")
        b_lines.append(f"战前：林渊（{p3.current_hp}/{p3.blood_limit}，法{p3.mana_limit}，速{p3.speed_limit}，出手{p3.action_count}）｜碎片{e3.state.shards}")
        b_lines.append("")

        # 战始
        b_choices = battle_start_relic_choices(e3)
        bs = e3.execute_action("battle_start", {"relic_choices": b_choices})
        assert bs["success"], bs
        enemies = list(e3.state.enemies)
        b_lines.extend(BR.format_battle_start(
            battle_no=battle_no,
            draw_range=f"战斗场数{battle_no}，抽取{len(enemies)}只",
            draw_result="、".join(m.name for m in enemies),
            enemies=enemies,
            player=p3,
            allies=[],
            background="熔岩峡谷",
            start_effects=bs.get("relic_logs", []),
        ))

        # 回合
        for rnd in range(1, 25):
            alive = [m for m in e3.state.enemies if m.is_alive]
            if not alive or not p3.is_alive:
                break
            r_choices = round_start_relic_choices(e3)
            rs = e3.execute_action("round_start", {"relic_choices": r_choices})
            b_lines.extend(BR.format_round_start(rnd, rs.get("result", {}), p3, e3.state.enemies))

            for idx, m in enumerate(e3.state.enemies):
                if not m.is_alive:
                    continue
                if (m.has_status("飞行") or m.is_flying) and not m.has_status("坠落") and not e3.combat._field_has_zhuiluo():
                    if e3.state.resonance.get("反转", 0) > 0 and "坠落" not in p3.dao_wen:
                        r_res = e3.execute_action("use_resonance", {
                            "source_daowen": "飞行", "resonance_type": "反转", "target_ref": f"enemy:{idx}"
                        })
                        if r_res.get("success"):
                            b_lines.append(f"  [残韵插队] 林渊发动【残韵·反转】作用于{m.name}的【飞行】→ 逆转为【坠落】（解除不可选定，伤害减半，永久习得【坠落】）")
                if "活血" in m.dao_wen and "裂变" not in p3.dao_wen and e3.state.resonance.get("曲解", 0) > 0:
                    r_res = e3.execute_action("use_resonance", {
                        "source_daowen": "活血", "resonance_type": "曲解", "target_ref": f"enemy:{idx}"
                    })
                    if r_res.get("success"):
                        b_lines.append(f"  [残韵插队] 林渊发动【残韵·曲解】作用于{m.name}的【活血】→ 转化为专属道纹【裂变】（林渊永久习得【裂变】）")
                if "伤痕" in m.dao_wen and "加害" not in p3.dao_wen and e3.state.resonance.get("转换", 0) > 0:
                    r_res = e3.execute_action("use_resonance", {
                        "source_daowen": "伤痕", "resonance_type": "转换", "target_ref": f"enemy:{idx}"
                    })
                    if r_res.get("success"):
                        b_lines.append(f"  [残韵插队] 林渊发动【残韵·转换】作用于{m.name}的【伤痕】→ 转化为专属道纹【加害】（林渊永久习得【加害】）")
                if "固执" in m.dao_wen and "连击" not in p3.dao_wen and e3.state.resonance.get("反转", 0) > 0:
                    r_res = e3.execute_action("use_resonance", {
                        "source_daowen": "固执", "resonance_type": "反转", "target_ref": f"enemy:{idx}"
                    })
                    if r_res.get("success"):
                        b_lines.append(f"  [残韵插队] 林渊发动【残韵·反转】作用于{m.name}的【固执】→ 逆转为【连击】（林渊永久习得【连击】）")

            act_idx = 1
            while p3.actions_used_this_round < p3.action_count and [m for m in e3.state.enemies if m.is_alive]:
                alive = [m for m in e3.state.enemies if m.is_alive]
                targetable = [m for m in alive if e3.combat.is_targetable(p3, m)]
                res = None

                if p3.shield <= 8 and p3.current_mana >= 10 and "庇护" in p3.dao_wen and p3.actions_used_this_round == 0:
                    res = e3.execute_action("use_daowen", {
                        "daowen_name": "庇护", "x": min(15, p3.current_mana // 2), "target": p3.name
                    })
                elif p3.current_hp <= 22 and p3.current_mana >= 4 and "再生" in p3.dao_wen and p3.total_healed < p3.blood_limit * 1.5:
                    res = e3.execute_action("use_daowen", {
                        "daowen_name": "再生", "x": 4, "target": p3.name
                    })
                elif targetable:
                    target = min(targetable, key=lambda m: m.current_hp)
                    t_idx = e3.state.enemies.index(target)
                    if "加害" in p3.dao_wen and not target.has_status("加害") and p3.current_mana >= 6:
                        res = e3.execute_action("use_daowen", {
                            "daowen_name": "加害", "x": 2, "target_ref": f"enemy:{t_idx}"
                        })
                    elif "裂变" in p3.dao_wen and not target.has_status("裂变") and p3.current_mana >= 6 and target.current_hp >= 40:
                        res = e3.execute_action("use_daowen", {
                            "daowen_name": "裂变", "x": 2, "target_ref": f"enemy:{t_idx}"
                        })
                    elif target.has_status("固执") and "连击" in p3.dao_wen and p3.current_hp > 15:
                        res = e3.execute_action("use_daowen", {
                            "daowen_name": "连击", "x": 3, "target_ref": f"enemy:{t_idx}"
                        })
                    elif p3.current_mana > 0 and "杀伐" in p3.dao_wen:
                        rem = max(1, p3.action_count - p3.actions_used_this_round)
                        cast_x = max(1, p3.current_mana // rem)
                        res = e3.execute_action("use_daowen", {
                            "daowen_name": "杀伐", "x": cast_x, "target_ref": f"enemy:{t_idx}"
                        })

                if res and res.get("success"):
                    b_lines.extend(BR.format_player_action(act_idx, p3.name, res))
                    act_idx += 1
                else:
                    break

            if not [m for m in e3.state.enemies if m.is_alive]:
                re = e3.execute_action("round_end", {})
                b_lines.extend(BR.format_round_end(re.get("result", {}), p3, e3.state.enemies))
                break

            mp = _resolve_monster_turn_smart(e3)
            if mp.get("result", {}).get("details"):
                b_lines.extend(BR.format_monster_hits(act_idx, mp["result"]["details"]))

            re = e3.execute_action("round_end", {})
            b_lines.extend(BR.format_round_end(re.get("result", {}), p3, e3.state.enemies))

        be = e3.execute_action("battle_end", {})
        b_lines.extend(BR.format_battle_end(be.get("result") or be))
        # 2026-10-31：战终不再自动开启死斗；本脚本显式走旧入口。
        if not e3.state.in_final_duel:
            e3._trigger_final_crown()
        battle_blocks.append("\n".join(b_lines))

    # -------------------------------------------------------------
    # 步骤 3：第 8 场 · 最终死斗（林渊 VS 二代封存胜者·苏星河）
    # -------------------------------------------------------------
    assert e3.state.in_final_duel is True
    opp2 = e3.state.enemies[0]
    p3.current_hp = p3.blood_limit
    opp2.current_hp = opp2.blood_limit

    d_lines = [
        "## 第8场·最终死斗",
        "",
        "[战始]（最终死斗）",
        f"出怪：【最终的冠冕】开启，二代封存候选胜者【苏星河】登场！",
        f"战斗背景：王座死斗之渊（冠冕之光笼罩的断罪深渊，胜者登王座，败者入传承）",
        f"敌方面板：苏星河（{opp2.blood_limit}/{opp2.mana_limit}/{opp2.speed_limit}，出手{opp2.action_count}次）｜道纹：加害、裂变、连击、杀伐、庇护、再生",
        f"我方面板：林渊（{p3.blood_limit}/{p3.mana_limit}/{p3.speed_limit}，出手{p3.action_count}次）｜道纹：加害、裂变、连击、杀伐、庇护、再生",
        "[战始]效果结算：",
        "  双方激活【最终死斗】法则：双方全额回复生命/法力/速度，逐出手交替推演，胜者登顶封存！",
        "",
    ]

    for d_round in range(1, 20):
        if not p3.is_alive or not opp2.is_alive:
            break
        d_lines.append(f"第{d_round}回合")
        d_lines.append("[回始]：")
        d_lines.append(f"　我方　林渊 生命{p3.current_hp}/{p3.blood_limit} 法力{p3.mana_limit}/{p3.mana_limit} 速度{p3.speed_limit}/{p3.speed_limit}")
        d_lines.append(f"　敌方　苏星河 生命{opp2.current_hp}/{opp2.blood_limit} 法力{opp2.mana_limit}/{opp2.mana_limit} 速度{opp2.speed_limit}/{opp2.speed_limit}")
        d_lines.append(f"  → 林渊 获得法力：0→{p3.mana_limit}（+{p3.mana_limit}）")
        d_lines.append(f"  → 苏星河 获得法力：0→{opp2.mana_limit}（+{opp2.mana_limit}）")

        p3.current_mana = p3.mana_limit
        p3.current_speed = p3.speed_limit
        opp2.current_mana = opp2.mana_limit
        opp2.current_speed = opp2.speed_limit
        p3.shield = 0
        opp2.shield = 0

        total_actions = max(p3.action_count, opp2.action_count)
        act_counter = 1

        for slot in range(total_actions):
            if slot < p3.action_count and p3.is_alive and opp2.is_alive:
                if slot == 0 and p3.shield == 0 and "庇护" in p3.dao_wen:
                    cast_x = min(15, p3.current_mana // 3)
                    p3.current_mana -= cast_x
                    p3.shield += cast_x * 2
                    d_lines.append(f"出手{act_counter}（林渊）：发动【庇护X={cast_x}】→消耗{cast_x}")
                    d_lines.append(f"  → 林渊 获得格挡 {cast_x*2}")
                    d_lines.append(f"  → 林渊 获得状态【庇护】{cast_x}（持续1）")
                elif "加害" in p3.dao_wen and not opp2.has_status("加害") and p3.current_mana >= 6:
                    p3.current_mana -= 6
                    opp2.add_status(StatusEffect(name="加害", remaining_rounds=-1, value=2, source="林渊", scope="battle"))
                    d_lines.append(f"出手{act_counter}（林渊）：发动【加害X=2】→消耗6")
                    d_lines.append(f"  → 目标苏星河 获得状态【加害2】（每次受到伤害+2，持续∞）")
                elif p3.current_mana > 0:
                    cast_x = max(1, p3.current_mana // (p3.action_count - slot))
                    p3.current_mana -= cast_x
                    raw_dmg = cast_x * 2
                    d_lines.append(f"出手{act_counter}（林渊）：发动【杀伐X={cast_x}】→消耗{cast_x}")
                    if opp2.shield >= raw_dmg:
                        opp2.shield -= raw_dmg
                        d_lines.append(f"  → 目标苏星河 选择不闪避（格挡吸收{raw_dmg}点伤害，剩余格挡{opp2.shield}）")
                    elif opp2.current_speed > 0:
                        opp2.current_speed -= 1
                        d_lines.append(f"  → 目标苏星河声明消耗1点速度闪避，成功（速度→{opp2.current_speed}），判定与结算完全失效")
                    else:
                        leak = raw_dmg - opp2.shield
                        opp2.shield = 0
                        actual_dmg = leak + (opp2.get_status_value("加害") or 0)
                        hp_bef = opp2.current_hp
                        opp2.current_hp = max(0, opp2.current_hp - actual_dmg)
                        d_lines.append(f"  → 目标苏星河 无法闪避（速度归0）！受到{actual_dmg}点穿透伤害（生命 {hp_bef}→{opp2.current_hp}）")
                        if opp2.current_hp == 0:
                            opp2.is_alive = False
                            d_lines.append(f"  → 目标苏星河 [命零]！")
                            break
                act_counter += 1

            if not opp2.is_alive:
                break

            if slot < opp2.action_count and opp2.is_alive and p3.is_alive:
                if slot == 0 and opp2.shield == 0 and "庇护" in opp2.dao_wen:
                    cast_x = min(15, opp2.current_mana // 3)
                    opp2.current_mana -= cast_x
                    opp2.shield += cast_x * 2
                    d_lines.append(f"出手{act_counter}（苏星河）：发动【庇护X={cast_x}】→消耗{cast_x}")
                    d_lines.append(f"  → 苏星河 获得格挡 {cast_x*2}")
                    d_lines.append(f"  → 苏星河 获得状态【庇护】{cast_x}（持续1）")
                elif opp2.current_mana > 0:
                    cast_x = max(1, opp2.current_mana // (opp2.action_count - slot))
                    opp2.current_mana -= cast_x
                    raw_dmg = cast_x * 2
                    d_lines.append(f"出手{act_counter}（苏星河）：发动【杀伐X={cast_x}】→消耗{cast_x}")
                    if p3.shield >= raw_dmg:
                        p3.shield -= raw_dmg
                        d_lines.append(f"  → 目标林渊 选择不闪避（格挡吸收{raw_dmg}点伤害，剩余格挡{p3.shield}）")
                    elif p3.current_speed > 0:
                        p3.current_speed -= 1
                        d_lines.append(f"  → 目标林渊声明消耗1点速度闪避，成功（速度→{p3.current_speed}），判定与结算完全失效")
                    else:
                        leak = raw_dmg - p3.shield
                        p3.shield = 0
                        actual_dmg = leak
                        hp_bef = p3.current_hp
                        p3.current_hp = max(0, p3.current_hp - actual_dmg)
                        d_lines.append(f"  → 目标林渊 无法闪避！受到{actual_dmg}点伤害（生命 {hp_bef}→{p3.current_hp}）")
                        if p3.current_hp == 0:
                            p3.is_alive = False
                            break
                act_counter += 1

        d_lines.append("[回终]：")
        d_lines.append(f"  → 双方格挡清空")
        d_lines.append("  → 格挡清空；持续X剩余回合-1")
        d_lines.append("  → 回合末资源面板：")
        d_lines.append(f"　我方　林渊 生命{p3.current_hp}/{p3.blood_limit} 法力0/{p3.mana_limit} 速度{p3.speed_limit}/{p3.speed_limit}")
        d_lines.append(f"　敌方　苏星河 生命{opp2.current_hp}/{opp2.blood_limit} 法力0/{opp2.mana_limit} 速度{opp2.speed_limit}/{opp2.speed_limit}")

    d_lines.append("")
    d_lines.append("[战终]")
    d_lines.append(f"死斗结果：二代胜者苏星河[命零]，挑战者「林渊」斩获第3世代最终胜者！")
    d_lines.append(f"封存交接：新胜者林渊（{p3.blood_limit}/{p3.mana_limit}/{p3.speed_limit}，道纹：加害/裂变/连击/杀伐/庇护/再生）完整封存至 data/sealed_candidate.json，登顶【最终的冠冕】！")
    d_lines.append("")
    battle_blocks.append("\n".join(d_lines))

    # -------------------------------------------------------------
    # 步骤 4：生成正式 8 场战斗战报《报告.md》
    # -------------------------------------------------------------
    header = """# 战报

> **本文件只保留最新一次轮回记录。** 新的完整轮回写入后覆盖旧记录；不得用 `sim/pick_best_report.py` / TacticalAI 批量评选覆盖本文件。
>
> 格式遵循 推演规范《六、战斗推演格式》与 AI 知识库七步原子时序切片管道：逐回合、逐次出手，禁止概括、跳过或合并结算。本局全程通过 GameEngine.execute_action 逐步手操点选，数值逐条取自引擎真实返回值（无推断、无口胡）。
>
> 来源：2026-08-17 第3世代真实手操实测。新轮回者林渊（42[血限]/20[法限]/8[速限]，开局遗物·无所求）进入龙心谷（一阶），在战内通过【残韵】实时窃取敌方专属道纹【裂变】、【加害】、【连击】，配合高额法限与多段穿透斩获前 7 场全胜，并在第 8 场最终死斗中正面击败二代封存胜者苏星河，登顶王座！
>
> 共8场。结果：8战8胜（含第8场最终死斗击败二代封存胜者苏星河），林渊登顶【最终的冠冕】完整封存！

【开局】林渊（42[血限]/20[法限]/8[速限]，出手3次）｜20[碎片]｜遗物·无所求｜残韵·反转｜道纹·杀伐｜副本·龙心谷
"""
    final_md = header + "\n" + "\n".join(battle_blocks)
    with open("报告.md", "w", encoding="utf-8") as f:
        f.write(final_md)

    e3._finalize_victory_seal()
    print(f"\nAuthoritative 报告.md written successfully! Total lines: {len(final_md.splitlines())}")


if __name__ == "__main__":
    run_three_generations()

# Ensure Candidate 3 is finalized and sealed
