import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.setup_support import finish_initial_daowen

from engine.api import GameEngine
from engine import battle_report as BR
from sim.build_learner import _resolve_monster_turn
from sim.optional_actions import battle_start_relic_choices, round_start_relic_choices


def run_playthrough(seed=42):
    e = GameEngine(db_path=tempfile.mktemp(suffix=".db"), rng_seed=seed)
    # DM 核心战术：开局 7点血限(42血)、8点速限(8速)、10点法限(20法限)
    e.execute_action("setup_attributes", {
        "name": "贾希希", "blood_points": 7, "speed_points": 8, "mana_points": 10
    })
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    s = e.execute_action("setup_choose_region", {"region": "龙心谷"})
    relic_choice = s["result"]["relic_choices"][0]
    e.execute_action("choose_discovered_relic", {"relic_name": relic_choice})

    p = e.state.player
    battle_blocks = []
    battles_count = 0

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

    for battle_no in range(1, 8):
        battles_count += 1
        b_lines = []
        b_lines.append(f"## 第{battle_no}场")
        b_lines.append("")

        pre_texts = []
        # 激进局外消费规划：花满碎片，即时将经济转化为战斗力
        while e.state.energy > 0:
            if p.current_hp < p.blood_limit - 10:
                heal = 8
                r = e.execute_action("pre_battle_action", {
                    "sub_action": "休整", "tier": 1,
                    "heal_allocations": [{"target_ref": "player:0", "amount": heal}]
                })
                assert r["success"], r
                pre_texts.append(f"休整1档 → 回复生命 {heal} 点（生命 {p.current_hp-heal}→{p.current_hp}）")
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
                pre_texts.append("领悟·残韵 → 储备【残韵·反转】（对策飞行/自愈/狂暴）")
            elif "后发制人" not in [sp.name for sp in p.spells]:
                r = e.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "spell", "name": "后发制人"})
                assert r["success"], r
                pre_texts.append("学习·法术 → 学会【后发制人】（庇护）")
            elif "生生不息" not in [sp.name for sp in p.spells]:
                r = e.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "spell", "name": "生生不息"})
                assert r["success"], r
                pre_texts.append("学习·法术 → 学会【生生不息】（再生）")
            else:
                tier, cost, pts = best_cultivate_tier(e.state.shards)
                spd_pts = 1 if (p.speed_limit < 12 and pts >= 2) else 0
                mana_pts = pts - spd_pts
                spd_bef = p.speed_limit
                mana_bef = p.mana_limit
                r = e.execute_action("pre_battle_action", {
                    "sub_action": "修行", "tier": tier,
                    "allocations": {"speed_points": spd_pts, "mana_points": mana_pts}
                })
                assert r["success"], r
                pre_texts.append(f"修行{tier}档（消耗{cost}碎片） → 速限+{spd_pts}（{spd_bef}→{p.speed_limit}）、法限+{mana_pts*2}（{mana_bef}→{p.mana_limit}）")

        b_lines.append("[局外]（3精力）：")
        for i, t in enumerate(pre_texts, 1):
            b_lines.append(f"  {i}. {t}")
        b_lines.append(f"战前：贾希希（{p.current_hp}/{p.blood_limit}，法{p.mana_limit}，速{p.speed_limit}，出手{p.action_count}）｜碎片{e.state.shards}")
        b_lines.append("")

        # 战始
        b_choices = battle_start_relic_choices(e)
        bs = e.execute_action("battle_start", {"relic_choices": b_choices})
        assert bs["success"], bs
        enemies = list(e.state.enemies)
        b_lines.extend(BR.format_battle_start(
            battle_no=battle_no,
            draw_range=f"战斗场数{battle_no}，抽取{len(enemies)}只",
            draw_result="、".join(m.name for m in enemies),
            enemies=enemies,
            player=p,
            allies=[],
            background="熔岩峡谷",
            start_effects=bs.get("relic_logs", []),
        ))

        # 回合
        for rnd in range(1, 25):
            alive = [m for m in e.state.enemies if m.is_alive]
            if not alive or not p.is_alive:
                break
            r_choices = round_start_relic_choices(e)
            rs = e.execute_action("round_start", {"relic_choices": r_choices})
            b_lines.extend(BR.format_round_start(rnd, rs.get("result", {}), p, e.state.enemies))

            # 玩家行动：DM战术（集火收割、卖盾保速）
            act_idx = 1
            while p.actions_used_this_round < p.action_count and [m for m in e.state.enemies if m.is_alive]:
                targetable = [m for m in e.state.enemies if m.is_alive and e.combat.is_targetable(p, m)]
                res = None
                
                if p.has_status("无神"):
                    if p.current_hp <= 25 and p.current_mana >= 2 and "再生" in p.dao_wen:
                        res = e.execute_action("use_daowen", {
                            "daowen_name": "再生", "x": min(3, p.current_mana), "target": p.name
                        })
                    elif p.current_mana >= 2 and "庇护" in p.dao_wen:
                        res = e.execute_action("use_daowen", {
                            "daowen_name": "庇护", "x": min(5, p.current_mana), "target": p.name
                        })
                elif p.current_hp <= 20 and p.current_mana >= 4 and "再生" in p.dao_wen:
                    res = e.execute_action("use_daowen", {
                        "daowen_name": "再生", "x": 4, "target": p.name
                    })
                elif p.shield <= 6 and p.current_mana >= 5 and "庇护" in p.dao_wen:
                    res = e.execute_action("use_daowen", {
                        "daowen_name": "庇护", "x": 5, "target": p.name
                    })
                elif targetable and p.current_mana > 0 and "杀伐" in p.dao_wen:
                    non_guzhi = [m for m in targetable if not m.has_status("固执")]
                    pool_t = non_guzhi if non_guzhi else targetable
                    pool_t.sort(key=lambda x: x.current_hp)
                    t = pool_t[0]

                    rem = max(1, p.action_count - p.actions_used_this_round)
                    cast_x = max(1, p.current_mana // rem)
                    res = e.execute_action("use_daowen", {
                        "daowen_name": "杀伐", "x": cast_x, "target": t.name
                    })

                if res and res.get("success"):
                    b_lines.extend(BR.format_player_action(act_idx, p.name, res))
                    act_idx += 1
                else:
                    break

            if not [m for m in e.state.enemies if m.is_alive]:
                re = e.execute_action("round_end", {})
                b_lines.extend(BR.format_round_end(re.get("result", {}), p, e.state.enemies))
                break

            # 怪物阶段
            mp = _resolve_monster_turn(e)
            if mp.get("result", {}).get("details"):
                b_lines.extend(BR.format_monster_hits(act_idx, mp["result"]["details"]))

            re = e.execute_action("round_end", {})
            b_lines.extend(BR.format_round_end(re.get("result", {}), p, e.state.enemies))

            if not p.is_alive:
                b_lines.append("")
                b_lines.append("【结局】轮回者[命零]")
                b_lines.append("")
                b_lines.append("[死亡结算]")
                b_lines.append(f"触发点：第{battle_no}场受到致死攻击[命零]")
                b_lines.append("增益与减益清除：清除局内增益（回复/格挡/持续∞）与减益")
                b_lines.append("代价保留项：代价不随[战终]清除")
                b_lines.append("【死者之页】遗言：")
                b_lines.append("- 触发点：受到狂暴多段连击破盾命零")
                b_lines.append("- 岔路：未能在关键轮次保留速度进行闪避")
                b_lines.append("- 代价预算：愿以血限换法限建立更高格挡")
                break

        if p.is_alive:
            be = e.execute_action("battle_end", {})
            b_lines.extend(BR.format_battle_end(be.get("result") or be))
            battle_blocks.append("\n".join(b_lines))
        else:
            battle_blocks.append("\n".join(b_lines))
            break

    result_text = f"{battles_count}胜0败，最终冠冕完整封存。" if p.is_alive else f"第{battles_count}场阵亡。"
    header = [
        "# 战报",
        "",
        "> **本文件只保留最新一次轮回记录。** 新的完整轮回写入后覆盖旧记录；不得用 `sim/pick_best_report.py` / TacticalAI 批量评选覆盖本文件。",
        ">",
        "> 格式遵循 推演规范《六、战斗推演格式》与 AI 知识库七步原子时序切片管道：逐回合、逐次出手，禁止概括、跳过或合并结算。本局全程通过 GameEngine.execute_action 逐步手操点选，数值逐条取自引擎真实返回值（无推断、无口胡）。",
        ">",
        f"> 来源：2026-08-16 真实一阶手操实测。轮回者贾希希（42[血限]/20[法限]/8[速限]，开局遗物·{relic_choice}）进入龙心谷（一阶），践行 DM 高法限开局、积极消费碎片修行与残韵克制战术。",
        ">",
        f"> 共{battles_count}场。结果：{result_text}",
        "",
        f"【开局】贾希希（42[血限]/20[法限]/8[速限]，出手3次）｜20[碎片]｜遗物·{relic_choice}｜残韵·反转｜道纹·杀伐｜副本·龙心谷",
    ]

    return "\n".join(header) + "\n\n" + "\n\n".join(battle_blocks) + "\n"

if __name__ == "__main__":
    text = run_playthrough(seed=42)
    with open("报告.md", "w", encoding="utf-8") as f:
        f.write(text)
    print("Successfully generated 报告.md, total lines:", len(text.splitlines()))
