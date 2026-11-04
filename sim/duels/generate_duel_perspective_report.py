#!/usr/bin/env python3
"""
真实死斗冒烟：生成一份同时含【当事人视角】与【后台审计数据】的完整死斗战报。

驱动方式：直接用 execute_action 手操推进一场 in_final_duel 状态下的死斗
（构造方式与 tests/test_duel_pvp_guards.py / tests/test_duel_perspective.py
一致的最小夹具，跳过 7 场通关的耗时铺垫，专注验证"前台信息隔离是否真实生效"）。

用法：
    python3 sim/duels/generate_duel_perspective_report.py
产物：
    data/duel_perspective_smoke_report.md
"""

# --- 2026-11-04：脚本放进子目录后，不再靠"往上数几层"猜仓库根 ---
# 向上找到同时含 engine/ 与 sim/ 的目录就是仓库根；再附带 sim/ 让老式裸模块名
# import（如 `import balance_sim`）继续可用。搬动本文件不需要再改这几行。
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.abspath(__file__))
while _ROOT != _os.path.dirname(_ROOT) and not _os.path.isdir(_os.path.join(_ROOT, "engine")):
    _ROOT = _os.path.dirname(_ROOT)
for _p in (_ROOT, _os.path.join(_ROOT, "sim"), _os.path.join(_ROOT, "tests")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

import os
import sys


from tests.setup_support import finish_initial_daowen
from tests.attack_support import resolve_attack as do_attack

from engine.api import GameEngine
from engine.enums import GamePhase
from engine.models import DaoWen, DaoWenInstance, Entity, Relic, StatusEffect
from engine import battle_report as BR
from engine import duel_perspective as DP


def build_duel_engine(tmp_dir=None):
    import tempfile
    tmp_dir = tmp_dir or tempfile.mkdtemp(prefix="duel_perspective_smoke_")
    os.makedirs(tmp_dir, exist_ok=True)
    db_path = os.path.join(tmp_dir, "smoke_duel.db")
    sealed_path = os.path.join(tmp_dir, "smoke_duel_seal.json")
    book_path = os.path.join(tmp_dir, "smoke_duel_book.md")
    for p in (db_path, sealed_path, book_path):
        if os.path.exists(p):
            os.remove(p)

    e = GameEngine(db_path=db_path, rng_seed=7,
                   sealed_candidate_path=sealed_path, death_book_path=book_path)
    e.execute_action("setup_attributes", {
        "name": "林渊", "blood_points": 7, "speed_points": 8, "mana_points": 10})
    finish_initial_daowen(e)
    p = e.state.player
    p.current_hp = 42
    p.blood_limit = 42
    p.current_speed = 12
    p.speed_limit = 12
    p.current_mana = 30
    p.mana_limit = 30
    p.dao_wen["杀伐"] = DaoWenInstance(DaoWen(
        name="杀伐", formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    p.dao_wen["庇护"] = DaoWenInstance(DaoWen(
        name="庇护", formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    p.dao_wen["再生"] = DaoWenInstance(DaoWen(
        name="再生", formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    e.state.relics = [Relic(name="无所求", effect="", tags=[])]

    lord = Entity("贾希希", "轮回者", blood_limit=42, current_hp=42,
                  mana_limit=28, current_mana=28, speed_limit=11, current_speed=11)
    for n in ("杀伐", "庇护"):
        lord.dao_wen[n] = DaoWenInstance(DaoWen(
            name=n, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
    e.state.enemies.clear()
    e.state.enemies.append(lord)
    e.state.opponent_relics = [Relic(name="守夜灯", effect="", tags=[])]

    e.state.in_final_duel = True
    e.state.duel_turn = "player_side"
    e.state.phase = GamePhase.IN_COMBAT.value
    e.state.combat_subphase = "await_round_start"
    return e


def run_smoke():
    e = build_duel_engine()
    lord = e.state.enemies[0]
    p = e.state.player

    perspective_entries: list = []
    audit_lines: list[str] = ["【死斗完整流水（引擎真实返回值，逐条不概括）】", ""]

    def audit(label: str, lines: list[str]):
        audit_lines.append(f"# {label}")
        audit_lines.extend(lines)
        audit_lines.append("")

    # ---------------- 回合1 ----------------
    rs = e.execute_action("round_start", {"relic_choices": {}})
    assert rs["success"], rs
    DP.record_environment(perspective_entries, e.state.current_round,
                          "王座死斗之渊：龙心火山断罪深渊，胜者登王座，败者入传承。")
    audit("回始", [f"round_start → {rs['success']}",
                  f"林渊：生命{p.current_hp}/{p.blood_limit} 法力{p.current_mana}/{p.mana_limit} "
                  f"速度{p.current_speed}/{p.speed_limit}",
                  f"贾希希：生命{lord.current_hp}/{lord.blood_limit} 法力{lord.current_mana}/{lord.mana_limit} "
                  f"速度{lord.current_speed}/{lord.speed_limit}"])

    # 出手1：林渊嘴上说要治疗，实际打出杀伐（对应任务书示例的欺骗场景）
    DP.record_speech(perspective_entries, e.state.current_round, "林渊", "我先给自己治疗一下。")
    r1 = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 12, "target_ref": "enemy:0"})
    assert r1["success"], r1
    DP.record_daowen(perspective_entries, e.state.current_round,
                     actor_side="player_side", actor_name="林渊", result=r1,
                     target_side="opponent_side", target_name="贾希希")
    audit("出手1（林渊）", BR.format_player_action(1, "林渊", r1))

    # 出手2：贾希希嘴硬「我毫发无伤」，实际上刚才已经掉血——这是他自己说的话，
    # 前台原样记录，不由旁白澄清真假。
    DP.record_speech(perspective_entries, e.state.current_round, "贾希希", "这点伤害对我毫发无损！")
    r2 = e.execute_action("use_daowen", {
        "actor_ref": "enemy:0", "daowen_name": "庇护", "x": 10, "target_ref": "enemy:0"})
    assert r2["success"], r2
    DP.record_daowen(perspective_entries, e.state.current_round,
                     actor_side="opponent_side", actor_name="贾希希", result=r2,
                     target_side="opponent_side", target_name="贾希希")
    audit("出手2（贾希希）", BR.format_player_action(2, "贾希希", r2))

    e.state.combat_subphase = "await_round_end"  # 死斗每回合出手预算未耗尽也可手动收束（与
    # tests/test_final_duel.py::finish_duel_round 同一手法），本冒烟只演示前后台分隔，不追求
    # 打满每一次出手预算。
    re1 = e.execute_action("round_end", {})
    assert re1["success"], re1
    audit("回终", [f"round_end → {re1['success']}"])

    # ---------------- 回合2 ----------------
    rs2 = e.execute_action("round_start", {"relic_choices": {}})
    assert rs2["success"], rs2
    audit("回始", [f"round_start → {rs2['success']}",
                  f"林渊：生命{p.current_hp}/{p.blood_limit} 法力{p.current_mana}/{p.mana_limit}",
                  f"贾希希：生命{lord.current_hp}/{lord.blood_limit} 法力{lord.current_mana}/{lord.mana_limit}"])

    # 死斗严格交替出手：谁先手取决于上一手结算后的 state.duel_turn，不手写死。
    if e.state.duel_turn == "opponent_side":
        DP.record_speech(perspective_entries, e.state.current_round, "贾希希", "看好了，我要反击了！")
        r3 = e.execute_action("use_daowen", {
            "actor_ref": "enemy:0", "daowen_name": "杀伐", "x": 8, "target_ref": "player:0",
            "dodge": False, "blood_shadow": False})
        assert r3["success"], r3
        DP.record_daowen(perspective_entries, e.state.current_round,
                         actor_side="opponent_side", actor_name="贾希希", result=r3,
                         target_side="player_side", target_name="林渊")
        audit("出手3（贾希希）", BR.format_player_action(3, "贾希希", r3))
    else:
        DP.record_speech(perspective_entries, e.state.current_round, "林渊", "还没完。")
        r3 = e.execute_action("use_daowen", {"daowen_name": "庇护", "x": 5, "target_ref": "player:0"})
        assert r3["success"], r3
        DP.record_daowen(perspective_entries, e.state.current_round,
                         actor_side="player_side", actor_name="林渊", result=r3,
                         target_side="player_side", target_name="林渊")
        audit("出手3（林渊）", BR.format_player_action(3, "林渊", r3))

    lord.current_hp = 3  # 手操推进到濒死，便于展示"命零"这一公开可观察结果
    if e.state.duel_turn != "player_side":
        e.state.duel_turn = "player_side"
    DP.record_speech(perspective_entries, e.state.current_round, "林渊", "结束了。")
    r4 = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 5, "target_ref": "enemy:0"})
    assert r4["success"], r4
    DP.record_daowen(perspective_entries, e.state.current_round,
                     actor_side="player_side", actor_name="林渊", result=r4,
                     target_side="opponent_side", target_name="贾希希")
    audit("出手4（林渊·终结）", BR.format_player_action(4, "林渊", r4))

    if not lord.is_alive:
        DP.record_note(perspective_entries, e.state.current_round, "贾希希[命零]，死斗胜负已分。")
        final = e.execute_action("resolve_final_duel", {"outcome": "victory"})
        audit("死斗结算", [f"resolve_final_duel → {final}"])

    perspective_lines = DP.render_report(perspective_entries)
    full_report = DP.assemble_duel_report(perspective_lines, audit_lines)
    # 仓库文档校验（tests/test_document_structure.py）要求每份 .md 都有一级标题；
    # 本文件是一次性冒烟产物，补一行 H1 即可满足，不影响【当事人视角】/【后台
    # 审计数据】两段本身的内容与分隔。
    full_report = ["# 死斗当事人视角·冒烟产物（sim/duels/generate_duel_perspective_report.py 生成）",
                   ""] + full_report

    out_path = os.path.join("data", "duel_perspective_smoke_report.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(full_report))
    return out_path, full_report, e, lord


if __name__ == "__main__":
    out_path, lines, e, lord = run_smoke()
    print(f"已生成：{out_path}\n")
    print("\n".join(lines))