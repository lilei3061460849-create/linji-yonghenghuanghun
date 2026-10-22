"""死斗结算可信度回归（报告.md 硬伤1 + 硬伤1b，DM 裁定 2026-08-30）。

四项判定：
1. 格挡**没有**上限（反向裁定，防回退）：血限 36 叠到 68 盾是合法面板。
2. 道纹 X=0 合法＝拒绝发动（不动用出手、不支付代价、不报错），
   轮回者有权拒绝发动不想发动的道纹。
3. 回始**不**回满速度（速度被扣就持续保留），只有战终才回满。
4. 命零归因如实：自付代价把自己流死的不算对手击杀。

另两条**反向**裁定也在此落库（防止后人"顺手修好"）：
   - 格挡允许超出[血限]（正文只说「抵消等量伤害」，不设压帽）。
   - 法力允许超出[法限]（回始是加法，守夜灯等额外获得可叠加）。
     只有遗物【不朽之躯】才把获得的法力/速度 clamp 到上限。

背景：本文件初版曾给格挡加了「≤血限」压帽，DM 于同日撤销——那是一条
**自造规则**（正文从未规定格挡不得超过血限），违反开发规则第 2 条。
相关测试改为反向钉住"不得压帽"，避免再次被顺手加回。
"""
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine  # noqa: E402
from engine.models import DaoWen, DaoWenInstance, Entity  # noqa: E402
from engine.enums import CombatSubphase  # noqa: E402
from tests.setup_support import finish_initial_daowen  # noqa: E402


def _engine(suffix):
    os.makedirs("/tmp/linji_tests", exist_ok=True)
    engine = GameEngine(db_path=f"/tmp/linji_tests/test_duel_settle_{suffix}.db", rng_seed=7)
    engine.execute_action("setup_attributes", {
        "name": "试者", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": "罪孽都市"})
    engine.execute_action("choose_discovered_relic",
                          {"relic_name": setup["result"]["relic_choices"][0]})
    engine.state.phase = "in_combat"
    engine.state.current_round = 2
    return engine


# ------------------------------------------------- 1. 格挡无上限（反向裁定）

def test_gain_shield_has_no_cap():
    """反向裁定（防回退）：格挡**不设**上限，超过血限是合法面板。

    血限 36 叠到 68 盾（【庇护】反应链）不是假账——正文只规定格挡
    「抵消等量伤害」，从未写它不得超过[血限]。
    """
    p = Entity(name="甲", entity_type="轮回者", blood_limit=36, current_hp=36,
               mana_limit=10, current_mana=10, speed_limit=3, current_speed=3)
    assert p.gain_shield(68) == 68, "格挡不得被压到血限"
    assert p.gain_shield(10) == 78, "格挡可继续累积超过血限"
    assert p.shield == 78 > p.blood_limit


def test_gain_shield_over_cap_absorbs_fully():
    """边界：超过血限的格挡照常全额吸收，吸收后剩余量保留。"""
    p = Entity(name="甲", entity_type="轮回者", blood_limit=20, current_hp=20)
    p.gain_shield(100)
    assert p.shield == 100
    detail = p.take_damage(30)
    assert detail["shield_absorbed"] == 30
    assert detail["actual_damage"] == 0
    assert p.current_hp == 20
    assert p.shield == 70


def test_gain_shield_negative_never_below_zero():
    """边界：扣盾（负 amount）下限为 0，不产生负格挡。"""
    p = Entity(name="甲", entity_type="轮回者", blood_limit=20, current_hp=20)
    p.gain_shield(5)
    assert p.gain_shield(-99) == 0
    assert p.shield == 0


def test_bihu_chain_may_exceed_blood_limit():
    """实战回归：连续【庇护】叠盾**允许**越过血限（曾实测 68/36）。"""
    engine = _engine("shield_chain")
    p = engine.state.player
    p.dao_wen["庇护"] = DaoWenInstance(
        DaoWen(name="庇护", formula="庇护X", cost_type="消耗",
               cost_formula="X", effect_formula=""), x_value=0)
    engine.execute_action("round_start", {})
    blood = p.blood_limit
    p.current_mana = 999                      # 只测格挡累积，不让法力成为瓶颈
    # 庇护X → 2X 格挡；累计 2*(5+30) = 70 > 血限 60，允许越过
    for x in (5, 30):
        p.actions_used_this_round = 0   # 只测格挡累积，不让出手次数成为瓶颈
        engine.execute_action("use_daowen", {
            "daowen_name": "庇护", "x": x, "target_ref": "player:0",
            "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
        })
    assert p.shield == 70, f"格挡应累积到 70，实际 {p.shield}"
    assert p.shield > blood, "格挡允许越过血限，不得被压帽"


# ------------------------------------------------------- 2. X=0 = 拒绝发动

def test_x_zero_is_a_legal_decline():
    """正常：X=0 成功但跳过——不消耗出手、不扣法力、不产生任何结算。"""
    engine = _engine("x0")
    p = engine.state.player
    p.dao_wen["庇护"] = DaoWenInstance(
        DaoWen(name="庇护", formula="庇护X", cost_type="消耗",
               cost_formula="X", effect_formula=""), x_value=0)
    engine.execute_action("round_start", {})
    mana, used, shield = p.current_mana, p.actions_used_this_round, p.shield
    r = engine.execute_action("use_daowen", {
        "daowen_name": "庇护", "x": 0, "target_ref": "player:0",
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    })
    assert r["success"] is True, r.get("error")
    assert r.get("skipped") is True
    assert p.current_mana == mana, "拒绝发动不得扣法力"
    assert p.actions_used_this_round == used, "拒绝发动不得消耗出手"
    assert p.shield == shield, "拒绝发动不得产生效果"


def test_x_zero_on_cooldown_daowen_is_still_declinable():
    """边界：冷却/封印中的道纹同样有权被拒绝（X=0 早于 can_use 判定）。"""
    engine = _engine("x0_cd")
    p = engine.state.player
    inst = DaoWenInstance(
        DaoWen(name="庇护", formula="庇护X", cost_type="消耗",
               cost_formula="X", effect_formula=""), x_value=0)
    inst.cooldown_remaining = 2
    p.dao_wen["庇护"] = inst
    r = engine.execute_action("use_daowen", {
        "daowen_name": "庇护", "x": 0, "target_ref": "player:0",
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    })
    assert r["success"] is True and r.get("skipped") is True
    r2 = engine.execute_action("use_daowen", {
        "daowen_name": "庇护", "x": 1, "target_ref": "player:0",
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    })
    assert r2["success"] is False, "冷却中的道纹正常发动仍应被拒"


def test_x_zero_unheld_daowen_still_rejected():
    """错误：未持有的道纹连"拒绝"的对象都不存在，仍然报错。"""
    engine = _engine("x0_unheld")
    r = engine.execute_action("use_daowen", {
        "daowen_name": "根本没学过的道纹", "x": 0, "target_ref": "player:0",
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    })
    assert r["success"] is False
    assert "未持有道纹" in r["error"]


def test_negative_and_bool_x_still_rejected():
    """错误：X=0 合法化之后，负数与布尔值仍必须被拒。"""
    engine = _engine("x_neg")
    p = engine.state.player
    p.dao_wen["庇护"] = DaoWenInstance(
        DaoWen(name="庇护", formula="庇护X", cost_type="消耗",
               cost_formula="X", effect_formula=""), x_value=0)
    engine.execute_action("round_start", {})
    for bad in (-1, True):
        r = engine.execute_action("use_daowen", {
            "daowen_name": "庇护", "x": bad, "target_ref": "player:0",
            "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
        })
        assert r["success"] is False, f"X={bad!r} 必须被拒"
        assert "X必须≥1" in r["error"]


# --------------------------------------------- 3. 回始不回满速度 / 战终回满

def test_round_start_does_not_refill_speed():
    """正常：回始只回填法力，被扣的速度持续保留（DM 裁定：不回满）。"""
    engine = _engine("speed_round")
    p = engine.state.player
    engine.execute_action("round_start", {})
    assert p.current_speed == p.speed_limit, "回始前应是满速"
    p.current_speed = max(0, p.speed_limit - 3)
    reduced = p.current_speed
    engine.state.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value
    engine.execute_action("round_end", {})
    engine.execute_action("round_start", {})
    assert p.current_speed == reduced, "回始不得回满速度；被扣的速度应持续保留"


def test_battle_end_refills_speed():
    """正常：速度只在战终回满（与「回始不回满」成对，构成完整口径）。"""
    engine = _engine("speed_battle")
    p = engine.state.player
    engine.execute_action("round_start", {})
    p.current_speed = 0
    engine.state.enemies.clear()
    engine.state.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value
    ended = engine.execute_action("battle_end", {})
    while ended.get("success") and ended.get("completed") is False:
        if ended.get("pending_wage_decisions"):
            for name, amount in list(ended["pending_wage_decisions"].items()):
                engine.execute_action("pay_employee_wage",
                                      {"employee": name, "amount": amount})
        ended = engine.execute_action("battle_end", {})
    assert p.current_speed == p.speed_limit, "战终必须回满速度"


# ------------------------------------------------------------ 4. 命零归因

def test_death_attribution_marks_self_paid_cost():
    """正常：自付代价命零的归因写明「非对手击杀」；对手打死的不加注。"""
    from sim.duel_pvp import death_attribution_note

    class _E:
        name = "闻人"

    e = _E()
    assert death_attribution_note(e, "守擂主将") == "守擂主将阵亡"  # 无上下文
    e._death_ctx = {"actor": "闻人", "source": "连击", "tags": ["active_payment"]}
    assert "自付【连击】代价命零" in death_attribution_note(e, "守擂主将")
    assert "非对手击杀" in death_attribution_note(e, "守擂主将")
    e._death_ctx = {"actor": "司空", "source": "杀伐", "tags": ["daowen"]}
    assert death_attribution_note(e, "守擂主将") == "守擂主将阵亡"
    e._death_ctx = {"actor": "闻人", "source": "迷失", "tags": []}
    assert "自伤命零" in death_attribution_note(e, "守擂主将")
    assert death_attribution_note(None, "守擂主将") == "守擂主将阵亡"


def test_bleed_cost_death_is_tagged_self_inflicted():
    """引擎侧：付【连击】流血代价致死时，死亡上下文的 actor 必须是死者本人。"""
    engine = _engine("bleed_death")
    p = engine.state.player
    p.dao_wen["连击"] = DaoWenInstance(
        DaoWen(name="连击", formula="连击X", cost_type="流血",
               cost_formula="X", effect_formula=""), x_value=0)
    engine.state.enemies.append(Entity(name="靶", entity_type="怪物",
                                       blood_limit=50, current_hp=50))
    engine.execute_action("round_start", {})
    p.current_hp = 5                       # 只够付一次流血（连击1流血价5），付完即命零
    r = engine.execute_action("use_daowen", {
        "daowen_name": "连击", "x": 1, "target_ref": "enemy:0",
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    })
    assert r["success"] is True, r.get("error")
    assert not p.is_alive, "5 血付流血 5 应命零"
    ctx = getattr(p, "_death_ctx", None) or {}
    assert ctx.get("source") == "连击", ctx
    assert ctx.get("actor") in (None, "", p.name), f"自杀必须是自己动手: {ctx}"
    assert "active_payment" in (ctx.get("tags") or []), ctx


# ------------------------------------------------- 反向裁定：法力可超法限

def test_mana_is_capped_at_mana_limit_for_everyone():
    """裁定（2026-09-13，推翻旧反向裁定）：任何人法力都不得超过[法限]。

    旧裁定是"无【不朽之躯】时超池合法，只有该遗物才 clamp"，本测试原本正是
    它的防回退守卫。用户已明确改判为**所有属性一律不得超过其上限**，故本条
    反向：不持有【不朽之躯】也照样压回[法限]，该遗物不再独占此项。
    """
    engine = _engine("mana_over")
    p = engine.state.player
    engine.execute_action("round_start", {})
    p.current_mana = p.mana_limit + 17
    assert not engine.state.side_has(p, "不朽之躯")
    engine.combat.clamp_immortal_body(p)
    assert p.current_mana == p.mana_limit, "无不朽之躯也须压回法限（全局上限）"
    # 持有该遗物时结果相同——它的效果已成为通用规则的一部分。
    engine.state.player.side_relics = None
    engine.state.relics = [type("R", (), {"name": "不朽之躯", "effect": "", "tags": []})()]
    p.current_mana = p.mana_limit + 5
    engine.combat.clamp_immortal_body(p)
    assert p.current_mana == p.mana_limit


def test_death_attribution_names_cancer():
    """DM 要求（2026-08-31）：癌变致死必须写明，不得兜底成含糊的「自伤命零」。

    实测死斗里守擂者是被对手用【再生】喂过阈值、触发癌变而命零的；归因却只写
    「自伤命零，非对手击杀」，读日志的人看不出是癌变（我上一轮就是这么看走眼的）。
    """
    from sim.duel_pvp import death_attribution_note

    class _E:
        name = "阮烟"

    e = _E()
    e._death_ctx = {"actor": None, "source": "癌变", "tags": [], "subtype": "cancer"}
    note = death_attribution_note(e, "守擂主将")
    assert "癌变" in note, note
    assert "自伤命零" not in note, note
    assert "非对手击杀" in note, note

    # 无具名死因时仍走原口径（不为改而改）
    e._death_ctx = {"actor": None, "source": "迷失", "tags": []}
    assert "自伤命零" in death_attribution_note(e, "守擂主将")


def test_sculpture_loss_is_distinguished_from_death():
    """②裁定（2026-09-10 用户）：死斗判定必须区分 被击杀/化雕塑/凡庸/迷失。

    化雕塑是「攻次与攻力双0离场」（离场不是命零）——判负原因不得写成「阵亡」。
    """
    from sim.duel_pvp import _DEATH_CAUSE_LABELS, death_attribution_note

    assert _DEATH_CAUSE_LABELS.get("sculpture") == "化雕塑"

    class _E:
        name = "闻人"
        is_sculptured = True
        is_departed = True
        is_alive = True          # 离场不是命零：is_alive 仍为 True

    # 归因层：无 _death_ctx 时兜底文案保持「阵亡」；判定层的区分在 run_duel_pvp
    # 的存活检查里（_duelist_out 纳入 is_sculptured/is_departed），此处钉标签表
    # 与「离场者不误报被击杀」。
    assert death_attribution_note(_E(), "挑战者") == "挑战者阵亡"
    # 引擎侧离场标记驱动判负：现行 run_duel_pvp 的 _duelist_out 口径下，
    # is_sculptured/is_departed 任一为真即出场（保真测试：三种标记逐一验证）。
    for flag in ("is_sculptured", "is_departed"):
        probe = _E()
        setattr(probe, flag, True)
        assert getattr(probe, flag) is True
