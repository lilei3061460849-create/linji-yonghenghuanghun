"""【勾魂】改版验证。

语义沿革：
1. 初版（已废止）：消耗X，[回始]使[目标]失去 2X 点当前法力，持续∞。
2. 2026-08-30 版：使[目标]无法获得[法力]，持续X（只压制增益）。
3. 2026-09-09 DM裁定：法力改一池制后一度改为「消耗法力翻倍」。
4. 2026-10-08 用户令：恢复「无法获得[法力]」原意，翻倍撤销。
5. **现行（2026-11-08 用户令重做）：支付：法力5X。将[目标]的X种道纹
   转移至1个[造物]身上，持续∞。**「无法获得[法力]」整条撤销：
   `Entity.can_gain_mana()` 不再读【勾魂】，勾魂不挂持续状态。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine  # noqa: E402
from engine.daowen import DaoWenEngine  # noqa: E402
from engine.dice import DiceEngine  # noqa: E402
from engine.mechanisms import MECHANISMS  # noqa: E402
from engine.models import DaoWen, DaoWenInstance, Entity, GameState  # noqa: E402
from engine.validator import check_migrated_mechanism_guards  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
COMBAT_SOURCE = (ROOT / "engine" / "combat.py").read_text(encoding="utf-8")


def _give(ent, name):
    ent.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X",
               effect_formula=""))


def _arena(entity_type="怪物"):
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=50, current_mana=50, speed_limit=10, current_speed=5,
                    attack_count=1, attack_power=1)
    ent = Entity("E", entity_type, blood_limit=50, current_hp=50,
                 mana_limit=30, current_mana=20,
                 attack_count=1, attack_power=1)
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
    assert "no_mana_gain" not in calc, "2026-11-08：无法获得法力已随重做撤销"
    assert "mana_cost_multiplier" not in calc, "消耗翻倍已撤销"
    assert calc["cost_type"] == "消耗" and calc["cost"] == 15, "法力5X"
    assert calc["transfer_daowen_count"] == 3
    assert "转移" in calc["summary"], calc["summary"]


# ====== 2. 现行语义（2026-11-08 用户令）：转移道纹至造物 ======

def test_gouhun_transfers_daowen_not_mana_block():
    """正常路径：勾魂把目标道纹转移到造物身上，法力增益不受任何影响。"""
    state, combat, player, ent = _arena()
    _give(ent, "杀伐")
    _give(ent, "再生")
    creation = Entity("尸傀", "造物", blood_limit=30, current_hp=30)
    state.temp_friends.append(creation)
    calc = DaoWenEngine.resolve("勾魂", 1, target=ent, caster=player,
                                daowen_names=["杀伐"])
    res = combat.apply_daowen_effect("勾魂", calc, player, ent)
    assert any(e.get("type") == "daowen_transferred" for e in res["effects"])
    assert "杀伐" not in ent.dao_wen and "再生" in ent.dao_wen
    assert "杀伐" in creation.dao_wen
    assert not ent.has_status("勾魂"), "新勾魂不挂持续状态"
    # 法力：消耗按原值、增益照常
    assert ent.spend_mana(5) is True and ent.current_mana == 15
    assert ent.can_gain_mana() is True
    assert ent.gain_mana(7) == 7 and ent.current_mana == 22


def test_gouhun_without_creation_is_rejected():
    """边界：场上没有[造物]接收 → 发动不成立（支付前拒绝）。"""
    import pytest
    state, combat, player, ent = _arena()
    _give(ent, "杀伐")
    calc = DaoWenEngine.resolve("勾魂", 1, target=ent, caster=player,
                                daowen_names=["杀伐"])
    with pytest.raises(ValueError, match="没有可接收道纹的"):
        combat.apply_daowen_effect("勾魂", calc, player, ent)
    assert "杀伐" in ent.dao_wen, "拒绝后不得发生转移"


def test_gouhun_applies_to_monsters_too():
    """边界：怪物施法者同样走转移——玩家道纹被转到怪物方造物身上。"""
    state, combat, player, ent = _arena()
    _give(player, "杀伐")
    creation = Entity("尸傀", "造物", blood_limit=30, current_hp=30)
    state.enemies.append(creation)
    calc = DaoWenEngine.resolve("勾魂", 1, target=player, caster=ent,
                                daowen_names=["杀伐"])
    combat.apply_daowen_effect("勾魂", calc, ent, player)
    assert "杀伐" not in player.dao_wen and "杀伐" in creation.dao_wen


def test_no_gouhun_pays_normal_mana_cost():
    """对照：没有勾魂时按原值扣费，且能正常获得法力。"""
    state, combat, _player, ent = _arena()
    assert ent.spend_mana(4) is True
    assert ent.current_mana == 16, ent.current_mana
    assert ent.can_gain_mana() is True
    assert ent.gain_mana(3) == 3
    assert ent.current_mana == 19, ent.current_mana


# ==================== 3. 残韵：转化只改牌面 ====================

def test_resonance_conversion_uses_new_gouhun_edge():
    """2026-11-08 四面体一：勾魂的反转出口是【化尸】（反转 化尸⇄勾魂）。

    转化只改怪物牌面并授予施法者，不消耗玩家任何资源、不动已转移的道纹。
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
    m = Entity(name="寄骨蝇", entity_type="怪物", blood_limit=200, current_hp=200,
               attack_count=1, attack_power=12)
    _give(m, "勾魂")
    e.state.enemies.append(m)

    e.state.resonance = {"反转": 1}
    r = e.execute_action("use_resonance", {"source_daowen": "勾魂",
                                           "resonance_type": "反转",
                                           "target_ref": "enemy:0"})
    assert r.get("success") is True, r
    assert "化尸" in m.dao_wen and "勾魂" not in m.dao_wen
    assert "化尸" in e.state.player.dao_wen, "施法者同时永久获得"
    assert e.state.resonance["反转"] == 0, "真实转化照常消耗一次残韵"


def test_resonance_into_former_placeholder_now_grants_yapo():
    """边界（四面体）：原「必中—转换→？？」空位是真边，不是占位符。

    2026-11-02 该边由【篡改】补齐；2026-11-07 互换为【封印】；
    2026-11-08 再次调换后，必中属通用四面体四，转换→【压迫】是真边：
    转化成功、消耗 1 次残韵、双方都拿到真实道纹。
    """
    from engine.api import GameEngine
    from engine.daowen import RESERVED_SLOT
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
    _give(m, "必中")
    e.state.enemies.append(m)

    e.state.resonance = {"转换": 1}
    r = e.execute_action("use_resonance", {"source_daowen": "必中",
                                           "resonance_type": "转换",
                                           "target_ref": "enemy:0"})
    assert r.get("success") is True, r
    assert e.state.resonance.get("转换") == 0, "真实转化照常消耗残韵"
    assert "压迫" in m.dao_wen and "必中" not in m.dao_wen, "怪物牌面应被改写为压迫"
    assert "压迫" in p.dao_wen, "施法者同时永久获得"
    assert RESERVED_SLOT not in m.dao_wen and RESERVED_SLOT not in p.dao_wen
