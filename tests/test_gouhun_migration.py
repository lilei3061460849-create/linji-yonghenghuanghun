"""【勾魂】改版验证（2026-08-30，DM 裁定见 报告.md 硬伤2-C）。

语义沿革：
1. 初版（已废止）：消耗X，[回始]使[目标]失去 2X 点当前法力，持续∞。
   曾迁移为 ROUND_START 相位 Mechanism（priority 40，经 mana 动词）。
2. 2026-08-30 版：消耗X，使[目标]**无法获得[法力]**，持续X——只压制[回始]
   的法力回填（条目 mana_refill_blocked），不扣已有法力，到期自然恢复。
3. 2026-09-09 DM裁定：法力改一池制（[战始]给满、[回始]不回填、[战终]复原）
   后「无法获得法力」一度失去作用对象，改为**消耗法力翻倍**。
4. **现行（2026-10-08 用户令）：恢复「无法获得[法力]」原意，翻倍撤销。**
   此时战斗内已有多个法力来源——聚能／储能电池／守夜灯／承露盏／共罪／
   透支／法术——效果重新有对象。判定位唯一：
   `Entity.can_gain_mana()` / `Entity.gain_mana()`，任何来源都不得绕过。

另外本文件钉住硬伤2-D 的裁定：**转化（残韵）不清除已生效的 debuff**——
把怪物的【勾魂】转化成别的道纹，玩家身上已挂的勾魂状态按自然规则继续。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine  # noqa: E402
from engine.daowen import DaoWenEngine  # noqa: E402
from engine.dice import DiceEngine  # noqa: E402
from engine.enums import CombatSubphase  # noqa: E402
from engine.mechanisms import MECHANISMS, Phase  # noqa: E402
from engine.models import DaoWen, DaoWenInstance, Entity, GameState, StatusEffect  # noqa: E402
from engine.validator import check_migrated_mechanism_guards  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
COMBAT_SOURCE = (ROOT / "engine" / "combat.py").read_text(encoding="utf-8")


def _arena(mana=20, gouhun_rounds=None, entity_type="轮回者", alive=True):
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    # DM裁定 2026-09-09：轮回者普攻面板初始 1×1，雕塑不再排除轮回者。
    # 只对轮回者补面板——怪物档仍按各自用例设定，避免顺手改掉雕塑相关断言。
    atk = {"attack_count": 1, "attack_power": 1} if entity_type == "轮回者" else {}
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=50, current_mana=0, speed_limit=10, current_speed=5, **atk)
    ent = Entity("E", entity_type, blood_limit=50, current_hp=50,
                 mana_limit=30, current_mana=mana, **atk)
    if gouhun_rounds is not None:
        ent.add_status(StatusEffect(name="勾魂", remaining_rounds=gouhun_rounds,
                                    value=1, source="x"))
    ent.is_alive = alive
    state.player = player
    state.enemies = [ent]
    return state, CombatEngine(state, DiceEngine()), player, ent


# ==================== 1. 旧机制已拆除 ====================

def test_old_gouhun_mechanism_removed():
    assert MECHANISMS.get("勾魂") is None, "旧【勾魂】回始扣法力机制必须已删除"
    assert "勾魂" not in [m.name for m in MECHANISMS.all()]
    assert check_migrated_mechanism_guards() == []


def test_no_round_start_mana_drain_anywhere():
    assert "round_start_mana_drain" not in COMBAT_SOURCE
    assert "gouhun_mana" not in COMBAT_SOURCE, "不得残留回始扣法力的效果条目"
    calc = DaoWenEngine.resolve("勾魂", 3)
    assert "round_start_mana_drain" not in calc
    assert calc.get("no_mana_gain") is True, "2026-10-08：勾魂=无法获得[法力]"
    assert "mana_cost_multiplier" not in calc, "消耗翻倍已撤销"
    assert calc.get("duration") == 3, "持续 = X"
    assert "无法获得法力" in calc["summary"], calc["summary"]


# ====== 2. 现行语义（2026-10-08 用户令）：无法获得[法力] ======
# 判定位唯一：Entity.can_gain_mana() / Entity.gain_mana()。所有法力增益来源
# （聚能/储能电池/守夜灯/承露盏/共罪/透支/法术）都必须过这一处。
# 消耗**不**翻倍——【勾魂】只压制增益，不改消耗倍率。

def test_gouhun_blocks_mana_gain_not_spending():
    """正常路径：勾魂压制**增益**，但不改消耗倍率——花法力仍按原值。"""
    state, combat, _player, ent = _arena(mana=20, gouhun_rounds=2)
    assert ent.has_status("勾魂")
    # 消耗按原值，不翻倍
    assert ent.spend_mana(5) is True
    assert ent.current_mana == 15, f"勾魂不改消耗倍率，实剩 {ent.current_mana}"
    # 增益被压死
    assert ent.can_gain_mana() is False
    assert ent.gain_mana(7) == 0
    assert ent.current_mana == 15, f"勾魂期间不得获得法力，实{ent.current_mana}"


def test_gouhun_expires_after_x_rounds():
    """边界：持续 X 回合，[回终]递减，走完后恢复回填。"""
    state, combat, _player, ent = _arena(mana=5, gouhun_rounds=2)
    combat.round_start({"relic_choices": {}})
    assert ent.has_status("勾魂")
    state.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value
    combat.round_end()
    assert ent.has_status("勾魂"), "第1回合末仍在持续期内"
    state.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value
    combat.round_end()
    assert not ent.has_status("勾魂"), "持续X=2 走完应自然到期"
    ent.current_mana = 20
    assert ent.spend_mana(5) is True
    assert ent.current_mana == 15, "到期后恢复正常消耗（5 点就是 5 点）"


def test_gouhun_applies_to_monsters_too():
    """边界：新语义下怪物同样适用——怪物身上的法力增益也被压死。"""
    state, combat, _player, ent = _arena(mana=20, gouhun_rounds=2, entity_type="怪物")
    assert ent.has_status("勾魂")
    assert ent.can_gain_mana() is False, "怪物同样无法获得法力"
    assert ent.gain_mana(4) == 0
    assert ent.current_mana == 20, f"怪物法力不得增加，实{ent.current_mana}"
    assert ent.spend_mana(4) is True
    assert ent.current_mana == 16, "消耗不翻倍"


def test_gouhun_on_dead_entity_costs_nothing():
    """边界：已命零的实体不再行动，勾魂对其无任何消耗后果。"""
    state, combat, _player, ent = _arena(mana=10, gouhun_rounds=2, alive=False)
    assert not ent.is_alive
    assert ent.current_mana == 10, "命零实体的法力不因挂状态而变动"


def test_gouhun_cast_sets_duration_equal_x():
    """正常：施放勾魂X 挂在目标身上的状态持续 = X（不再是 ∞）。"""
    state, combat, player, ent = _arena(mana=10)
    calc = DaoWenEngine.resolve("勾魂", 4, target=ent, caster=player)
    res = combat.apply_daowen_effect("勾魂", calc, player, ent)
    assert ent.has_status("勾魂")
    dur = next(s.remaining_rounds for s in ent.status_effects if s.name == "勾魂")
    assert dur == 4, f"持续应为 X=4，实{dur}"
    gouhun_effect = next((e for e in res["effects"] if e.get("type") == "gouhun"), None)
    assert gouhun_effect and gouhun_effect.get("no_mana_gain") is True, gouhun_effect
    assert gouhun_effect.get("duration") == 4, gouhun_effect


def test_no_gouhun_pays_normal_mana_cost():
    """对照：没有勾魂时按原值扣费，且能正常获得法力。"""
    state, combat, _player, ent = _arena(mana=10)
    assert not ent.has_status("勾魂")
    assert ent.spend_mana(4) is True
    assert ent.current_mana == 6, ent.current_mana
    assert ent.can_gain_mana() is True
    assert ent.gain_mana(3) == 3
    assert ent.current_mana == 9, ent.current_mana


# ==================== 3. 硬伤2-D：转化不清除已生效 debuff ====================

def test_resonance_conversion_does_not_clear_active_gouhun():
    """DM 裁定：残韵转化只改怪物牌面，不解除玩家身上已挂的勾魂。

    转化后玩家仍按剩余持续回合继续被压制；怪物下回合改用新道纹（真实生效）。
    """
    from engine.api import GameEngine
    from tests.setup_support import finish_initial_daowen

    db = f"/tmp/linji_tests/test_gouhun_res_{os.getpid()}.db"
    e = GameEngine(db_path=db, rng_seed=7,
                   sealed_candidate_path=f"/tmp/linji_tests/test_gouhun_res_s_{os.getpid()}.json")
    e.execute_action("setup_attributes", {"name": "白某", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    e.execute_action("setup_choose_region", {"region": "乱葬岗"})
    e.state.phase = "in_combat"
    e.state.current_round = 2
    p = e.state.player
    # 【冥气】已遗物化删除；勾魂的反转分支只通向不可获得的闭环占位符。
    m = Entity(name="寄骨蝇", entity_type="怪物", blood_limit=200, current_hp=200,
               attack_count=1, attack_power=12)
    m.dao_wen["勾魂"] = DaoWenInstance(
        DaoWen(name="勾魂", formula="", cost_type="消耗", cost_formula="X",
               effect_formula=""), x_value=4)
    e.state.enemies.append(m)
    # 先让怪物的勾魂落到玩家身上（持续 X=4）
    from engine.daowen import DaoWenEngine as DWE
    calc = DWE.resolve("勾魂", 4, target=p, caster=m)
    e.combat.apply_daowen_effect("勾魂", calc, m, p)
    assert p.has_status("勾魂")

    # 玩家尝试沿已删除的【冥气】分支转化，必须被拒绝
    e.state.resonance = {"反转": 1}
    r = e.execute_action("use_resonance", {"source_daowen": "勾魂",
                                           "resonance_type": "反转",
                                           "target_ref": "enemy:0"})
    assert r.get("success") is False, r
    assert "勾魂" in m.dao_wen and not any(name.startswith("？？") for name in m.dao_wen), "删除的遗物化道纹不可授予"

    # 裁定要点：玩家身上已生效的勾魂**不**被清除，继续按剩余持续压制
    assert p.has_status("勾魂"), "转化不得清除已生效的 debuff"
    p.current_mana = 20
    assert p.can_gain_mana() is False, "转化后勾魂仍压制法力增益"
    assert p.gain_mana(5) == 0
    assert p.current_mana == 20, "转化后勾魂仍应让玩家无法获得法力"

    assert e.state.resonance["反转"] == 1, "走到已遗物化占位符不消耗残韵"




def test_resonance_into_reserved_slot_is_refused():
    """边界（2026-10-08 删【镇尸】；2026-10-29 勾魂—凋零补格）：残韵通向闭环空占位符，不可获得。

    旧行为：勾魂 —(曲解)→ 镇尸，玩家可把怪物的勾魂转成镇尸。
    现行：勾魂—曲解—凋零已是真边，空位改由【必中】的【转换】承载——
    走到占位符＝无事发生：失败、不消耗残韵、怪物牌面不变、施法者也不获得任何道纹。
    """
    import os
    from engine.api import GameEngine
    from engine.daowen import RESERVED_SLOT
    from engine.models import Entity, DaoWen, DaoWenInstance
    from tests.setup_support import finish_initial_daowen

    db = f"/tmp/linji_tests/test_gouhun_slot_{os.getpid()}.db"
    e = GameEngine(db_path=db, rng_seed=7,
                   sealed_candidate_path=f"/tmp/linji_tests/test_gouhun_slot_s_{os.getpid()}.json")
    e.execute_action("setup_attributes", {"name": "白某", "blood_points": 11,
                                          "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    e.execute_action("setup_choose_region", {"region": "乱葬岗"})
    e.state.phase = "in_combat"
    e.state.current_round = 2
    p = e.state.player
    m = Entity(name="寄骨蝇", entity_type="怪物", blood_limit=200, current_hp=200,
               attack_count=1, attack_power=12)
    m.dao_wen["必中"] = DaoWenInstance(
        DaoWen(name="必中", formula="", cost_type="消耗", cost_formula="X",
               effect_formula=""), x_value=1)
    e.state.enemies.append(m)

    e.state.resonance = {"转换": 1}
    r = e.execute_action("use_resonance", {"source_daowen": "必中",
                                           "resonance_type": "转换",
                                           "target_ref": "enemy:0"})
    assert r.get("success") is False, f"通向空占位符的残韵应失败，实：{r}"
    assert e.state.resonance.get("转换") == 1, "失败不应消耗残韵"
    assert "必中" in m.dao_wen, "怪物牌面不应被改写"
    assert RESERVED_SLOT not in m.dao_wen, "空占位符不得进入任何人的道纹列表"
    assert RESERVED_SLOT not in p.dao_wen, "施法者也不得获得空占位符"
