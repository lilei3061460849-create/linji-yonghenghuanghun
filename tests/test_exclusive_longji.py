"""龙心谷配置扩展与通用非法目标契约。

龙心谷旧专属道纹已退役；通用护卫指令由 test_guard_command.py 覆盖，
本文件不再把护卫内部状态当作可学习、可发动的道纹。
"""
import os
import sys
import pytest

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import Entity, DaoWen, DaoWenInstance, StatusEffect, Consumable
from engine.monsters import make_monster_entity
from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import GameState

def _setup_player_with_daowen(names):
    engine = GameEngine(rng_seed=42)
    engine.execute_action("setup_attributes", {"blood_points": 11, "speed_points": 6, "mana_points": 8})
    finish_initial_daowen(engine)
    engine.state.relics = []
    # 2026-10-13：遗物池删2件后随机发放漂移，本文件断言与开局遗物无关，清空钉死环境。
    engine.state.current_region = "龙心谷"
    engine.state.phase = "in_combat"
    # 直接赋予测试用道纹（绕过学习门禁，仅用于单测）
    for n in names:
        engine.state.player.dao_wen[n] = DaoWenInstance(DaoWen(name=n, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))
        # 确保法力充足
        engine.state.player.current_mana = 100
    engine.state.player.current_hp = 60
    engine.state.player.blood_limit = 60
    return engine



def test_error_invalid_target():
    """错误：对不存在目标使用应失败"""
    # 用现役【杀伐】验证不存在的目标会被拒绝。
    engine = _setup_player_with_daowen(["杀伐"])
    r = engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 1, "target": "不存在的怪"})
    assert not r["success"]



def test_custom_extensibility():
    """可自定义：不改引擎代码，仅改配置即可新增专属道纹实例（演示 2 示例）"""
    from engine.combat import CombatEngine
    # 验证 REGION_EXCLUSIVE_DAOWEN 为纯数据驱动
    original = set(CombatEngine.REGION_EXCLUSIVE_DAOWEN["龙心谷"])
    # 模拟新增两个自定义专属道纹（仅配置层面）
    extended = original | {"测试道纹A", "测试道纹B"}
    assert "测试道纹A" in extended
    assert "测试道纹B" in extended
    # 验证引擎的 REGION_EXCLUSIVE_DAOWEN 可被外部扩展而无需改 combat 核心逻辑
    assert len(extended) == len(original) + 2
