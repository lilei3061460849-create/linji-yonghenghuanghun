"""
重构架构单元测试：战斗事件总线与声明式钩子系统
覆盖三类测试：正常路径、边界条件、错误输入与非法状态。
"""
import math
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.models import Entity, GameState, StatusEffect
from engine.combat_events import CombatEvent, CombatEventType
from engine.combat_hooks import (
    CombatHookManager,
    QianjingjiaHook,
    BifenglingHook,
)


class MockState:
    """CombatHookManager 的状态替身：只实现 Hook 会用到的那部分 GameState 接口。"""

    def __init__(self, relics=None):
        self.relics = relics or []

    def side_has(self, entity, relic_name):
        return relic_name in self.relics

    def _relic_active(self, entity, relic_name):
        return self.side_has(entity, relic_name)

    def life_loss_multiplier(self, entity):
        """【第一杯】「失去的生命翻倍」的倍率：真实实现在 GameState，
        替身保持同接口（本替身不持有该遗物 → 1）。"""
        return 2 if self.side_has(entity, "第一杯") else 1

    def heal_multiplier(self, entity):
        return 2 if self.side_has(entity, "第一杯") else 1


# ---------- 正常路径 ----------

def test_hooks_normal_incoming_and_reflect():
    """正常路径：加害提升伤害、千荆甲反噬数值层、避风铃闪避叠甲

    2026-10-03：【爆裂】道纹删除；反噬的触发判定（持有者/只对攻击伤害）移到
    CombatEngine 侧，Hook 只负责把「等量伤害」换算成实际反噬数值（含【第一杯】倍率），
    扣血与记账唯一入口是 _resolve_reflect_aftermath（避免双倍扣血）。
    """
    manager = CombatHookManager()
    state = MockState(relics=["避风铃"])

    attacker = Entity(name="攻击者", blood_limit=42, current_hp=42, mana_limit=50, speed_limit=12, entity_type="轮回者")
    target = Entity(name="防守者", blood_limit=42, current_hp=42, mana_limit=50, speed_limit=12, entity_type="轮回者")
    target.current_speed = 12
    target.add_status(StatusEffect(name="加害", value=2, remaining_rounds=-1, source="test"))

    # 1. 伤害修正（加害2使10伤害变为12）
    adjusted = manager.apply_incoming_adjust(target, 10, "普通", attacker, state)
    assert adjusted == 12

    # 2. 千荆甲反噬数值层：返回等量反噬数值，不动生命（扣血由引擎侧统一做）
    assert manager.reflect_attack_damage(target, adjusted, attacker, state) == 12
    assert attacker.current_hp == 42, "数值层不得自己扣血（双扣回归防护）"
    assert manager.apply_before_damage(target, adjusted, "普通", attacker, state) == {}

    # 3. 闪避触发避风铃（获得3格挡）
    dodge_res = manager.apply_dodge(target, state)
    assert dodge_res["shield_gained"] == 3
    assert target.shield == 3


def test_combat_event_serialization():
    """正常路径：CombatEvent 能够被正确构造并无损序列化"""
    evt = CombatEvent(
        event_type=CombatEventType.DAMAGE_APPLIED,
        battle_no=1,
        round_no=2,
        actor_name="莫非",
        target_name="林渊",
        data={"damage": 20, "shield_absorbed": 10, "hp_lost": 10},
    )
    d = evt.to_dict()
    assert d["event_type"] == "damage_applied"
    assert d["battle_no"] == 1
    assert d["data"]["damage"] == 20


# ---------- 边界条件 ----------

def test_hooks_boundary_conditions():
    """边界条件：速度归零获得15格挡、叠鳞甲固定减1并封底0、代价伤害不受调整"""
    manager = CombatHookManager()
    state = MockState(relics=["避风铃", "叠鳞甲"])

    zero_speed_entity = Entity(name="零速者", blood_limit=30, current_hp=30, speed_limit=0, entity_type="轮回者")
    zero_speed_entity.current_speed = 0
    d_res = manager.apply_dodge(zero_speed_entity, state)
    assert d_res["shield_gained"] == 3
    assert zero_speed_entity.shield == 3  # 闪避句只+3；归零+15走失速总线

    # 叠鳞甲每笔非代价伤害固定减1，最低为0；旧龙鳞X状态不再参与。
    dragon_target = Entity(name="龙族", blood_limit=40, current_hp=40, entity_type="怪物")
    dragon_target.add_status(StatusEffect(name="龙鳞", value=15, remaining_rounds=-1, source="legacy"))
    dmg_adj = manager.apply_incoming_adjust(dragon_target, 10, "普通", None, state)
    assert dmg_adj == 9, "叠鳞甲只固定减1，旧龙鳞15不得生效"
    assert manager.apply_incoming_adjust(dragon_target, 1, "普通", None, state) == 0

    # 代价类型伤害不触发加害
    jiahai_target = Entity(name="目标", blood_limit=30, current_hp=30, entity_type="轮回者")
    jiahai_target.add_status(StatusEffect(name="加害", value=5, remaining_rounds=-1, source="test"))
    cost_adj = manager.apply_incoming_adjust(jiahai_target, 10, "代价", None, state)
    assert cost_adj == 10, "代价伤害不应受加害增益"


# ---------- 错误输入 / 非法状态 ----------

def test_hooks_handles_none_and_invalid_state():
    """错误输入/非法状态：传入 None、非实体或空状态时安全降级而不抛出未捕获异常"""
    manager = CombatHookManager()

    # None 目标
    adj = manager.apply_incoming_adjust(None, 20, "普通", None, None)
    assert adj == 20

    # None 实体闪避
    d_none = manager.apply_dodge(None, None)
    assert d_none == {}

    # 负数伤害调整
    neg_adj = manager.apply_incoming_adjust(None, -5, "普通", None, None)
    assert neg_adj == -5


def test_damage_redirection_and_mitigation_hooks():
    """正常路径与边界：背负重定向、朋友濒死撤退保护

    2026-10-11【嫁祸】已删除（效果与遗物【贪婪契约】重复），本用例的重定向部分
    改由仍在的【背负】承载——考察的是 Hook 的重定向契约，与具体哪张牌无关。
    """
    manager = CombatHookManager()
    
    # 1. 背负重定向：背负者(援护者)替目标(受害者)承担伤害
    victim = Entity(name="受害者", blood_limit=30, current_hp=30, entity_type="轮回者")
    bearer = Entity(name="援护者", blood_limit=30, current_hp=30, entity_type="轮回者")
    bearer._beifu_left = 1
    bearer._beifu_target = victim.runtime_id
    bearer.add_status(StatusEffect(name="背负", value=1, remaining_rounds=-1, source="test"))

    # 重定向目标存runtime_id（2026-08-22：实体引用环会炸事务回滚递归），
    # 按id解析需要state提供实体集合（生产路径 combat.py 恒传真实state）
    class _RedirectState:
        def get_all_player_side(self):
            return [victim, bearer]
        def get_all_enemy_side(self):
            return []
    target_redirected = manager.apply_redirection(victim, "普通", _RedirectState())
    assert target_redirected is bearer
    assert bearer._beifu_left == 0
    # 计数耗尽即摘除指向（避免残留 runtime_id）；注意【背负】与已删的【嫁祸】不同：
    # 它清的是目标侧的「被背负」标记，背负者自身的「背负」状态按到期清理，不在此摘。
    assert not hasattr(bearer, "_beifu_target")

    # 2. 撤退保护
    friend = Entity(name="小跟班", blood_limit=20, current_hp=5, entity_type="朋友")
    class MockCombat:
        def __init__(self):
            self.state = MockState()
            self.state.player = Entity(name="玩家", blood_limit=42, current_hp=42, entity_type="轮回者")
            self.state.artifacts_owned = []
            self.state.fuyuebei_declared = set()
            self.state.event_modifiers = {}
        def _combat_entity_refs(self):
            return {"friend:0": friend}
    mock_c = MockCombat()
    mit_res = manager.apply_mitigation(friend, 15, "普通", mock_c)
    assert mit_res is not None
    assert mit_res["retreated"] is True
    assert friend.has_retreated is True
    assert friend.current_hp == 5, "撤退后生命完好保留"

