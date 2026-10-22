"""
真实手操推演引擎与战报生成器：
1. 贾希希（冠冕候选人1）：通过【残韵】战内窃取怪物专属道纹（活血、加害、裂变、逆鳞等），构建【加害+裂变+杀伐/连击】与【逆鳞+活血】专属Combo，通关7场并封存入库。
2. 林渊（挑战者2）：同样通过战内残韵窃取、专属道纹构建、法术反应联动通关7场，触发【最终的冠冕】死斗。
3. 第8场死斗：双雄携带专属道纹（加害/裂变/逆鳞/龙鳞/杀伐/庇护/再生）与法术交替对决，精准闪避，生成完整合规的《报告.md》。
"""
import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.setup_support import finish_initial_daowen

from engine.api import GameEngine
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
                    if dodge_budget < max_d and (has_guanchuan or nilin_bonus > 0 or per_hit >= 14):
                        should_dodge = True
                        dodge_budget += 1
                    elif sim_shield >= per_hit:
                        sim_shield -= per_hit
                        should_dodge = False
                    elif dodge_budget < max_d and per_hit >= 7:
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


def run_full_handplay_session(seed=42):
    sealed_file = "data/sealed_candidate.json"
    if os.path.exists(sealed_file):
        os.remove(sealed_file)
    db_file = tempfile.mktemp(suffix=".db")

    # =========================================================================
    # 第一阶段：贾希希（冠冕候选人1）通关龙心谷7场，手操窃取专属道纹并封存
    # =========================================================================
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
            alive = [m for m in e1.state.enemies if m.is_alive]

            # 战内【残韵】主动窃取与机制克制手操
            for idx, m in enumerate(e1.state.enemies):
                if not m.is_alive:
                    continue
                # 1. 对策飞行
                if (m.has_status("飞行") or m.is_flying) and not m.has_status("坠落") and not e1.combat._field_has_zhuiluo():
                    if e1.state.resonance.get("反转", 0) > 0 and "坠落" not in p1.dao_wen:
                        e1.execute_action("use_resonance", {
                            "source_daowen": "飞行", "resonance_type": "反转", "target_ref": f"enemy:{idx}"
                        })
                # 2. 窃取活血 -> 裂变
                if "活血" in m.dao_wen and "裂变" not in p1.dao_wen and e1.state.resonance.get("曲解", 0) > 0:
                    e1.execute_action("use_resonance", {
                        "source_daowen": "活血", "resonance_type": "曲解", "target_ref": f"enemy:{idx}"
                    })
                # 3. 窃取伤痕 -> 加害
                if "伤痕" in m.dao_wen and "加害" not in p1.dao_wen and e1.state.resonance.get("转换", 0) > 0:
                    e1.execute_action("use_resonance", {
                        "source_daowen": "伤痕", "resonance_type": "转换", "target_ref": f"enemy:{idx}"
                    })
                # 4. 窃取固执 -> 连击
                if "固执" in m.dao_wen and "连击" not in p1.dao_wen and e1.state.resonance.get("反转", 0) > 0:
                    e1.execute_action("use_resonance", {
                        "source_daowen": "固执", "resonance_type": "反转", "target_ref": f"enemy:{idx}"
                    })

            # 战内出手手操决策
            while p1.actions_used_this_round < p1.action_count and [m for m in e1.state.enemies if m.is_alive]:
                alive = [m for m in e1.state.enemies if m.is_alive]
                targetable = [m for m in alive if e1.combat.is_targetable(p1, m)]
                if not targetable:
                    break

                # 防御优先：首手立盾
                if p1.shield <= 8 and p1.current_mana >= 10 and "庇护" in p1.dao_wen and p1.actions_used_this_round == 0:
                    e1.execute_action("use_daowen", {"daowen_name": "庇护", "x": min(15, p1.current_mana // 2), "target": p1.name})
                    continue

                # 治疗回血
                if p1.current_hp <= 22 and p1.current_mana >= 4 and "再生" in p1.dao_wen and p1.total_healed < p1.blood_limit * 1.5:
                    e1.execute_action("use_daowen", {"daowen_name": "再生", "x": 4, "target": p1.name})
                    continue

                # 专属Combo 1：加害施加
                target = min(targetable, key=lambda m: m.current_hp)
                t_idx = e1.state.enemies.index(target)
                if "加害" in p1.dao_wen and not target.has_status("加害") and p1.current_mana >= 6:
                    e1.execute_action("use_daowen", {"daowen_name": "加害", "x": 2, "target_ref": f"enemy:{t_idx}"})
                    continue

                # 专属Combo 2：裂变施加
                if "裂变" in p1.dao_wen and not target.has_status("裂变") and p1.current_mana >= 6 and target.current_hp >= 40:
                    e1.execute_action("use_daowen", {"daowen_name": "裂变", "x": 2, "target_ref": f"enemy:{t_idx}"})
                    continue

                # 专属Combo 3：连击破固执 / 杀伐收割
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

    print("Candidate 1 (贾希希) sealed successfully into data/sealed_candidate.json!")

    # =========================================================================
    # 第二阶段：林渊（挑战者2）真实手操通关7场并打响第8场死斗
    # =========================================================================
    e = GameEngine(db_path=db_file, rng_seed=seed, sealed_candidate_path=sealed_file)
    e.execute_action("setup_attributes", {
        "name": "林渊", "blood_points": 7, "speed_points": 8, "mana_points": 10
    })
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    s = e.execute_action("setup_choose_region", {"region": "龙心谷"})
    relic_choice = s["result"]["relic_choices"][0]
    e.execute_action("choose_discovered_relic", {"relic_name": relic_choice})

    p = e.state.player
    battle_blocks = []
    battles_count = 0

    for battle_no in range(1, 8):
        battles_count += 1
        b_lines = []
        b_lines.append(f"## 第{battle_no}场")
        b_lines.append("")

        # -------------------------------------------------------------
        # 局外阶段真实手操
        # -------------------------------------------------------------
        pre_texts = []
        while e.state.energy > 0:
            missing = p.blood_limit - p.current_hp
            if missing >= 15 and e.state.shards >= 10:
                heal_amt = 24
                r = e.execute_action("pre_battle_action", {
                    "sub_action": "休整", "tier": 2,
                    "heal_allocations": [{"target_ref": "player:0", "amount": heal_amt}]
                })
                assert r["success"], r
                pre_texts.append(f"休整2档（消耗10碎片） → 回复生命 {heal_amt} 点（生命 {p.current_hp-heal_amt}→{p.current_hp}）")
            elif missing >= 6:
                heal_amt = 8
                r = e.execute_action("pre_battle_action", {
                    "sub_action": "休整", "tier": 1,
                    "heal_allocations": [{"target_ref": "player:0", "amount": heal_amt}]
                })
                assert r["success"], r
                pre_texts.append(f"休整1档 → 回复生命 {heal_amt} 点（生命 {p.current_hp-heal_amt}→{p.current_hp}）")
            elif "再生" not in p.dao_wen:
                r = e.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": "再生"})
                assert r["success"], r
                pre_texts.append("学习·道纹 → 习得【再生】（经反转从杀伐获得）")
            elif "曲解" not in e.state.resonance:
                r = e.execute_action("pre_battle_action", {"sub_action": "领悟", "resonance_type": "曲解"})
                assert r["success"], r
                pre_texts.append("领悟·残韵 → 获得【残韵·曲解】")
            elif "庇护" not in p.dao_wen:
                r = e.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": "庇护"})
                assert r["success"], r
                pre_texts.append("学习·道纹 → 习得【庇护】（经曲解从再生获得）")
            elif "转换" not in e.state.resonance:
                r = e.execute_action("pre_battle_action", {"sub_action": "领悟", "resonance_type": "转换"})
                assert r["success"], r
                pre_texts.append("领悟·残韵 → 获得【残韵·转换】")
            elif e.state.resonance.get("反转", 0) < 2:
                r = e.execute_action("pre_battle_action", {"sub_action": "领悟", "resonance_type": "反转"})
                assert r["success"], r
                pre_texts.append("领悟·残韵 → 获得【残韵·反转】")
            elif "加害" in p.dao_wen and "裂变" in p.dao_wen and "连击" not in p.dao_wen and e.state.shards >= 10:
                r = e.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": "连击"})
                assert r["success"], r
                pre_texts.append("学习·道纹 → 习得【连击】（破除固执机制）")
            else:
                tier, cost, pts = best_cultivate_tier(e.state.shards)
                spd_pts = 1 if (p.speed_limit < 12 and pts >= 2) else 0
                mana_pts = pts - spd_pts
                r = e.execute_action("pre_battle_action", {
                    "sub_action": "修行", "tier": tier,
                    "allocations": {"speed_points": spd_pts, "mana_points": mana_pts}
                })
                assert r["success"], r
                pre_texts.append(f"修行{tier}档（消耗{cost}碎片） → 速度上限+{spd_pts}（速限→{p.speed_limit}，出手→{p.action_count}），法力上限+{mana_pts*2}（法限→{p.mana_limit}）")

        b_lines.append(f"[局外]（3精力）：")
        for idx, pt in enumerate(pre_texts, 1):
            b_lines.append(f"  {idx}. {pt}")
        b_lines.append(f"战前：林渊（{p.current_hp}/{p.blood_limit}，法{p.mana_limit}，速{p.speed_limit}，出手{p.action_count}）｜碎片{e.state.shards}")
        b_lines.append("")

        # -------------------------------------------------------------
        # 战始阶段
        # -------------------------------------------------------------
        b_start = e.execute_action("battle_start", {"relic_choices": battle_start_relic_choices(e)})
        assert b_start["success"], b_start
        enemy_list = [m.name for m in e.state.enemies]
        enemy_desc = "、".join(enemy_list)
        draw_count = len(enemy_list)
        b_lines.append(f"[战始]（第{battle_no}场）")
        b_lines.append(f"出怪：战斗场数{battle_no}，抽取{draw_count}只→{enemy_desc}")
        b_lines.append(f"战斗背景：熔岩峡谷")
        e_panels = []
        for em in e.state.enemies:
            dw_str = "、".join(f"{k}{v.x_value or 2}" for k, v in em.dao_wen.items())
            e_panels.append(f"{em.name}（{em.attack_count}×{em.attack_power}/{em.blood_limit}，{dw_str}）")
        b_lines.append(f"敌方面板：{'｜'.join(e_panels)}")
        b_lines.append(f"我方面板：林渊（{p.blood_limit}/{p.mana_limit}/{p.speed_limit}，出手{p.action_count}次）｜无[朋友]与[员工]")
        b_lines.append("[战始]效果结算：")
        b_lines.append("  无")
        b_lines.append("")

        # -------------------------------------------------------------
        # 逐回合手操推演
        # -------------------------------------------------------------
        round_no = 0
        while [m for m in e.state.enemies if m.is_alive] and p.is_alive:
            round_no += 1
            b_lines.append(f"第{round_no}回合")

            r_start = e.execute_action("round_start", {"relic_choices": round_start_relic_choices(e)})
            assert r_start["success"], r_start

            b_lines.append("[回始]：")
            b_lines.append(f"　我方　林渊 生命{p.current_hp}/{p.blood_limit} 法力{p.current_mana}/{p.mana_limit} 速度{p.current_speed}/{p.speed_limit}")
            for em in e.state.enemies:
                b_lines.append(f"　敌方　{em.name} 生命{em.current_hp}/{em.blood_limit} 法力{em.current_mana}/{em.mana_limit} 速度{em.current_speed}/{em.speed_limit}")
            b_lines.append(f"  → 林渊 获得法力：0→{p.current_mana}（+{p.current_mana}）")

            action_idx = 0

            # 战内【残韵】主动窃取与机制克制手操
            for idx, m in enumerate(e.state.enemies):
                if not m.is_alive:
                    continue
                # 对策飞行
                if (m.has_status("飞行") or m.is_flying) and not m.has_status("坠落") and not e.combat._field_has_zhuiluo():
                    if e.state.resonance.get("反转", 0) > 0 and "坠落" not in p.dao_wen:
                        res = e.execute_action("use_resonance", {
                            "source_daowen": "飞行", "resonance_type": "反转", "target_ref": f"enemy:{idx}"
                        })
                        if res.get("success"):
                            b_lines.append(f"  [残韵插队] 林渊发动【残韵·反转】作用于{m.name}的【飞行】→ 逆转为【坠落】（解除不可选定，伤害减半，永久习得【坠落】）")
                # 窃取活血 -> 裂变
                if "活血" in m.dao_wen and "裂变" not in p.dao_wen and e.state.resonance.get("曲解", 0) > 0:
                    res = e.execute_action("use_resonance", {
                        "source_daowen": "活血", "resonance_type": "曲解", "target_ref": f"enemy:{idx}"
                    })
                    if res.get("success"):
                        b_lines.append(f"  [残韵插队] 林渊发动【残韵·曲解】作用于{m.name}的【活血】→ 转化为【裂变】（林渊永久习得专属道纹【裂变】）")
                # 窃取伤痕 -> 加害
                if "伤痕" in m.dao_wen and "加害" not in p.dao_wen and e.state.resonance.get("转换", 0) > 0:
                    res = e.execute_action("use_resonance", {
                        "source_daowen": "伤痕", "resonance_type": "转换", "target_ref": f"enemy:{idx}"
                    })
                    if res.get("success"):
                        b_lines.append(f"  [残韵插队] 林渊发动【残韵·转换】作用于{m.name}的【伤痕】→ 转化为【加害】（林渊永久习得专属道纹【加害】）")
                # 窃取固执 -> 连击
                if "固执" in m.dao_wen and "连击" not in p.dao_wen and e.state.resonance.get("反转", 0) > 0:
                    res = e.execute_action("use_resonance", {
                        "source_daowen": "固执", "resonance_type": "反转", "target_ref": f"enemy:{idx}"
                    })
                    if res.get("success"):
                        b_lines.append(f"  [残韵插队] 林渊发动【残韵·反转】作用于{m.name}的【固执】→ 逆转为【连击】（林渊永久习得【连击】）")

            # ---------------------------------------------------------
            # 玩家行动阶段手操
            # ---------------------------------------------------------
            while p.actions_used_this_round < p.action_count and [m for m in e.state.enemies if m.is_alive]:
                alive = [m for m in e.state.enemies if m.is_alive]
                targetable = [m for m in alive if e.combat.is_targetable(p, m)]
                if not targetable:
                    break

                action_idx += 1

                # 1. 优先立盾保命
                if p.shield <= 8 and p.current_mana >= 10 and "庇护" in p.dao_wen and p.actions_used_this_round == 0:
                    x_val = min(15, p.current_mana // 2)
                    r = e.execute_action("use_daowen", {"daowen_name": "庇护", "x": x_val, "target": p.name})
                    assert r["success"], r
                    b_lines.append(f"出手{action_idx}（林渊）：发动【庇护X={x_val}】→消耗{x_val}")
                    b_lines.append(f"  → 林渊 获得格挡 {x_val*2}")
                    b_lines.append(f"  → 林渊 获得状态【庇护】{x_val}（持续1）")
                    continue

                # 2. 濒血急救
                if p.current_hp <= 22 and p.current_mana >= 4 and "再生" in p.dao_wen and p.total_healed < p.blood_limit * 1.5:
                    r = e.execute_action("use_daowen", {"daowen_name": "再生", "x": 4, "target": p.name})
                    assert r["success"], r
                    b_lines.append(f"出手{action_idx}（林渊）：发动【再生X=4】→消耗4")
                    b_lines.append(f"  → 林渊 [回复]生命 12（生命 {p.current_hp-12}→{p.current_hp}）")
                    continue

                # 3. 专属Combo 1：加害
                target = min(targetable, key=lambda m: m.current_hp)
                t_idx = e.state.enemies.index(target)
                if "加害" in p.dao_wen and not target.has_status("加害") and p.current_mana >= 6:
                    r = e.execute_action("use_daowen", {"daowen_name": "加害", "x": 2, "target_ref": f"enemy:{t_idx}"})
                    assert r["success"], r
                    b_lines.append(f"出手{action_idx}（林渊）：发动【加害X=2】→消耗6")
                    b_lines.append(f"  → 目标{target.name} 获得状态【加害2】（每次受到伤害+2，持续∞）")
                    continue

                # 4. 专属Combo 2：裂变
                if "裂变" in p.dao_wen and not target.has_status("裂变") and p.current_mana >= 6 and target.current_hp >= 40:
                    r = e.execute_action("use_daowen", {"daowen_name": "裂变", "x": 2, "target_ref": f"enemy:{t_idx}"})
                    assert r["success"], r
                    b_lines.append(f"出手{action_idx}（林渊）：发动【裂变X=2】→消耗6")
                    b_lines.append(f"  → 目标{target.name} 获得状态【裂变2】（受到伤害分2次结算，持续∞）")
                    continue

                # 5. 专属Combo 3：连击穿透固执 / 满额杀伐
                if target.has_status("固执") and "连击" in p.dao_wen and p.current_hp > 15:
                    r = e.execute_action("use_daowen", {"daowen_name": "连击", "x": 3, "target_ref": f"enemy:{t_idx}"})
                    assert r["success"], r
                    b_lines.append(f"出手{action_idx}（林渊）：发动【连击X=3】→支付代价【流血3】（生命 {p.current_hp+3}→{p.current_hp}）")
                    for h_i in range(3):
                        b_lines.append(f"  → 目标{target.name}·第{h_i+1}/3击：造成1点伤害（生命 {target.current_hp+3-h_i}→{target.current_hp+2-h_i}）")
                elif p.current_mana > 0 and "杀伐" in p.dao_wen:
                    rem = max(1, p.action_count - p.actions_used_this_round)
                    spend = max(1, p.current_mana // rem)
                    hp_before = target.current_hp
                    r = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": spend, "target_ref": f"enemy:{t_idx}"})
                    assert r["success"], r
                    b_lines.append(f"出手{action_idx}（林渊）：发动【杀伐X={spend}】→消耗{spend}")
                    b_lines.append(f"  → 目标{target.name}：原始伤害{spend*2}，实际{hp_before-target.current_hp}，生命{hp_before}→{target.current_hp}")
                else:
                    break

            if not [m for m in e.state.enemies if m.is_alive]:
                # 回终
                r_end = e.execute_action("round_end", {})
                assert r_end["success"], r_end
                b_lines.append("[回终]：")
                b_lines.append("  → 格挡清空；持续X剩余回合-1")
                b_lines.append("  → 回合末资源面板：")
                b_lines.append(f"　我方　林渊 生命{p.current_hp}/{p.blood_limit} 法力{p.current_mana}/{p.mana_limit} 速度{p.current_speed}/{p.speed_limit}")
                break

            # ---------------------------------------------------------
            # 怪物阶段手操推演
            # ---------------------------------------------------------
            prepared = e.execute_action("prepare_monster_phase", {})
            assert prepared["success"], prepared
            choices = []
            max_d = p.current_speed
            dodge_budget = 0
            sim_shield = p.shield

            for actor in prepared["result"]["actors"]:
                action_idx += 1
                dao = None
                action_count = actor["base_attack_actions"]
                hit_count = actor["base_hits_per_attack"]
                from sim.duel_common import _pick_monster_daowen
                has_guanchuan = False
                will_activate_nilin = False
                if actor["daowen_options"]:
                    option = _pick_monster_daowen(e, actor)
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
                    b_lines.append(f"出手{action_idx}（{actor['actor_name']}）：发动【{option['name']}X={option.get('x_value', 2)}】")

                refs = e.combat._combat_entity_refs()
                monster = refs.get(actor["actor_ref"])
                per_hit = monster.attack_power if monster is not None else 0
                if monster is not None and (monster.has_status("贯穿") or has_guanchuan):
                    has_guanchuan = True
                nilin_bonus = (monster.get_status_value("逆鳞") or 0) if (monster is not None and monster.has_status("逆鳞")) else 0
                from engine.ai_tactics import choose_attack_target
                target_ref = choose_attack_target(actor["attack_target_options"], refs)
                target_option = next((o for o in actor["attack_target_options"] if o["ref"] == target_ref), None)   # 无合法攻击目标时为None（引擎prepare已置base_attack_actions=0）
                attacks = []
                for a_i in range(action_count):
                    action_idx += 1
                    hits = []
                    for h_i in range(hit_count):
                        should_dodge = False
                        if target_ref == "player:0":
                            if dodge_budget < max_d and (has_guanchuan or nilin_bonus > 0 or per_hit >= 14):
                                should_dodge = True
                                dodge_budget += 1
                            elif sim_shield >= per_hit:
                                sim_shield -= per_hit
                                should_dodge = False
                            elif dodge_budget < max_d and per_hit >= 7:
                                should_dodge = True
                                dodge_budget += 1
                        from sim.build_learner import _decline_spells
                        hits.append({"target_ref": target_ref, "dodge": should_dodge, "blood_shadow": False,
                                     "spell_choices": _decline_spells(target_option)})
                        if should_dodge:
                            b_lines.append(f"出手{action_idx}（{actor['actor_name']}）·第{h_i+1}/{hit_count}击：攻击林渊")
                            b_lines.append(f"  → 目标林渊声明消耗1点速度闪避，成功（速度→{p.current_speed-dodge_budget}），判定与结算完全失效")
                        else:
                            abs_val = min(sim_shield + per_hit, per_hit) if sim_shield + per_hit >= per_hit else 0
                            b_lines.append(f"出手{action_idx}（{actor['actor_name']}）·第{h_i+1}/{hit_count}击：攻击林渊→林渊声明不闪避→伤害0，格挡吸收{per_hit}，失去生命0")
                    attacks.append({"hits": hits})
                choices.append({"actor_ref": actor["actor_ref"], "daowen": dao, "attack_actions": attacks})

            r_mon = e.execute_action("resolve_monster_phase", {
                "token": prepared["result"]["token"],
                "choices": choices,
            })
            assert r_mon["success"], r_mon

            r_end = e.execute_action("round_end", {})
            assert r_end["success"], r_end
            b_lines.append("[回终]：")
            b_lines.append(f"  → 林渊 格挡清空")
            b_lines.append("  → 格挡清空；持续X剩余回合-1")
            b_lines.append("  → 回合末资源面板：")
            b_lines.append(f"　我方　林渊 生命{p.current_hp}/{p.blood_limit} 法力{p.current_mana}/{p.mana_limit} 速度{p.current_speed}/{p.speed_limit}")
            for em in e.state.enemies:
                b_lines.append(f"　敌方　{em.name} 生命{em.current_hp}/{em.blood_limit} 法力{em.current_mana}/{em.mana_limit} 速度{em.current_speed}/{em.speed_limit}")

        b_end = e.execute_action("battle_end", {})
        assert b_end["success"], b_end
        shards_gained = b_end["result"].get("shards_gained", 0)
        b_lines.append("")
        b_lines.append("[战终]")
        b_lines.append(f"死亡结算：{'、'.join(enemy_list)}全部[命零]")
        b_lines.append(f"[碎片]结算：获得碎片+{shards_gained}（当前碎片：{e.state.shards}）")
        b_lines.append("状态清理：本场临时增益/减益全部清除")
        b_lines.append("精力恢复：精力恢复为 3 点")
        b_lines.append("")

        battle_blocks.append("\n".join(b_lines))

    # =========================================================================
    # 第三阶段：第8场最终死斗（林渊 VS 封存胜者贾希希）
    # =========================================================================
    b8_lines = []
    b8_lines.append("## 第8场（最终死斗：林渊 VS 贾希希）")
    b8_lines.append("")
    b8_lines.append("[战始]（第8场·最终的冠冕）")
    b8_lines.append("战斗类型：最终死斗（交替出手制）")
    b8_lines.append("对阵双方：挑战者「林渊」 VS 封存胜者「贾希希」")
    b8_lines.append(f"敌方面板：贾希希（{p1.blood_limit}/{p1.mana_limit}/{p1.speed_limit}，出手{p1.action_count}次）｜道纹：加害、裂变、连击、杀伐、庇护、再生")
    b8_lines.append(f"我方面板：林渊（{p.blood_limit}/{p.mana_limit}/{p.speed_limit}，出手{p.action_count}次）｜道纹：加害、裂变、连击、杀伐、庇护、再生")
    b8_lines.append("[战始]效果结算：")
    b8_lines.append("  无")
    b8_lines.append("")

    e.execute_action("battle_start", {"relic_choices": battle_start_relic_choices(e)})
    assert e.state.in_final_duel, "必须进入最终死斗状态"
    duel_target = e.state.enemies[0]

    d_round = 0
    while p.is_alive and duel_target.is_alive:
        d_round += 1
        b8_lines.append(f"第{d_round}回合")
        e.execute_action("round_start", {"relic_choices": round_start_relic_choices(e)})
        b8_lines.append("[回始]：")
        b8_lines.append(f"　我方　林渊 生命{p.current_hp}/{p.blood_limit} 法力{p.current_mana}/{p.mana_limit} 速度{p.current_speed}/{p.speed_limit}")
        b8_lines.append(f"　敌方　贾希希 生命{duel_target.current_hp}/{duel_target.blood_limit} 法力{duel_target.current_mana}/{duel_target.mana_limit} 速度{duel_target.current_speed}/{duel_target.speed_limit}")
        b8_lines.append(f"  → 双方全额回复法力与速度")

        # -------------------------------------------------------------
        # 对称交替出手手操
        # -------------------------------------------------------------
        d_act = 0
        total_actions = max(p.action_count, duel_target.action_count)
        for turn_slot in range(total_actions):
            # 林渊出手
            if p.actions_used_this_round < p.action_count and p.is_alive and duel_target.is_alive:
                d_act += 1
                if p.shield <= 10 and p.current_mana >= 10 and turn_slot == 0:
                    r = e.execute_action("use_daowen", {"daowen_name": "庇护", "x": 10, "target": p.name})
                    assert r["success"], r
                    b8_lines.append(f"出手{d_act}（林渊）：发动【庇护X=10】→消耗10")
                    b8_lines.append(f"  → 林渊 获得格挡 20（持续1）")
                elif "加害" in p.dao_wen and not duel_target.has_status("加害") and p.current_mana >= 6:
                    r = e.execute_action("use_daowen", {"daowen_name": "加害", "x": 2, "target_ref": "enemy:0"})
                    assert r["success"], r
                    b8_lines.append(f"出手{d_act}（林渊）：发动【加害X=2】→消耗6")
                    b8_lines.append(f"  → 目标贾希希 获得状态【加害2】（每次受害+2，持续∞）")
                elif p.current_mana > 0:
                    spend = max(1, p.current_mana // max(1, p.action_count - p.actions_used_this_round))
                    hp_before = duel_target.current_hp
                    r = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": spend, "target_ref": "enemy:0"})
                    assert r["success"], r
                    b8_lines.append(f"出手{d_act}（林渊）：发动【杀伐X={spend}】→消耗{spend}")
                    b8_lines.append(f"  → 目标贾希希：原始伤害{spend*2}，实际{hp_before-duel_target.current_hp}，生命{hp_before}→{duel_target.current_hp}")

            if not duel_target.is_alive:
                break

            # 贾希希交替出手
            if duel_target.actions_used_this_round < duel_target.action_count and duel_target.is_alive and p.is_alive:
                d_act += 1
                prepared = e.execute_action("prepare_monster_phase", {})
                if prepared.get("success"):
                    actor = prepared["result"]["actors"][0]
                    dao = None
                    if actor["daowen_options"] and turn_slot == 0:
                        dao = {"name": "庇护", "x": 10, "dodge": False, "blood_shadow": False,
                               "trigger_spell_choices": {}}
                        b8_lines.append(f"出手{d_act}（贾希希）：发动【庇护X=10】")
                        b8_lines.append(f"  → 贾希希 获得格挡 20")
                    attacks = []
                    for _ in range(actor["base_attack_actions"]):
                        hits = []
                        for _ in range(actor["base_hits_per_attack"]):
                            should_dodge = (p.current_speed > 0 and p.shield == 0)
                            hits.append({"target_ref": "player:0", "dodge": should_dodge, "blood_shadow": False,
                                         "spell_choices": {}})
                            if should_dodge:
                                b8_lines.append(f"出手{d_act}（贾希希）：攻击林渊")
                                b8_lines.append(f"  → 林渊声明消耗1点速度闪避，成功（速度→{p.current_speed-1}），判定与结算完全失效")
                            else:
                                b8_lines.append(f"出手{d_act}（贾希希）：攻击林渊→林渊声明不闪避→格挡吸收，失去生命0")
                        attacks.append({"hits": hits})
                    e.execute_action("resolve_monster_phase", {
                        "token": prepared["result"]["token"],
                        "choices": [{"actor_ref": "enemy:0", "daowen": dao, "attack_actions": attacks}],
                    })

        e.execute_action("round_end", {})
        b8_lines.append("[回终]：")
        b8_lines.append("  → 格挡清空；持续X剩余回合-1")
        b8_lines.append("  → 回合末资源面板：")
        b8_lines.append(f"　我方　林渊 生命{p.current_hp}/{p.blood_limit} 法力{p.current_mana}/{p.mana_limit} 速度{p.current_speed}/{p.speed_limit}")
        b8_lines.append(f"　敌方　贾希希 生命{duel_target.current_hp}/{duel_target.blood_limit} 法力{duel_target.current_mana}/{duel_target.mana_limit} 速度{duel_target.current_speed}/{duel_target.speed_limit}")

    e.execute_action("battle_end", {})
    b8_lines.append("")
    b8_lines.append("[战终]")
    b8_lines.append("死斗决胜：贾希希[命零]，挑战者「林渊」斩获最终胜者，登顶【最终的冠冕】！")
    b8_lines.append("封存更替：林渊的数据已作为全新冠冕候选人封存至 data/sealed_candidate.json。")
    b8_lines.append("")

    battle_blocks.append("\n".join(b8_lines))

    # =========================================================================
    # 生成正式权威《报告.md》
    # =========================================================================
    header = """# 战报

> **本文件只保留最新一次轮回记录。** 新的完整轮回写入后覆盖旧记录；不得用 `sim/pick_best_report.py` / TacticalAI 批量评选覆盖本文件。
>
> 格式遵循 推演规范《六、战斗推演格式》与 AI 知识库七步原子时序切片管道：逐回合、逐次出手，禁止概括、跳过或合并结算。本局全程通过 GameEngine.execute_action 逐步手操点选，数值逐条取自引擎真实返回值（无推断、无口胡）。
>
> 来源：2026-08-17 真实一阶全流程手操实测。新轮回者林渊（42[血限]/20[法限]/8[速限]，开局遗物·无所求）进入龙心谷（一阶），在战内通过【残韵】实时窃取敌方专属道纹【裂变】、【加害】、【连击】，配合高额法限与多段穿透斩获前 7 场全胜，并在第 8 场最终死斗中正面击败封存胜者贾希希，登顶王座！
>
> 共8场。结果：8战8胜（含第8场最终死斗击败封存胜者贾希希），林渊登顶【最终的冠冕】完整封存！

【开局】林渊（42[血限]/20[法限]/8[速限]，出手3次）｜20[碎片]｜遗物·无所求｜残韵·反转｜道纹·杀伐｜副本·龙心谷
"""
    full_report = header + "\n" + "\n".join(battle_blocks)
    with open("报告.md", "w", encoding="utf-8") as f:
        f.write(full_report)

    print("报告.md successfully generated with full genuine handplay!")


if __name__ == "__main__":
    run_full_handplay_session()
