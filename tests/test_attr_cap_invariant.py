"""重构2（2026-11-01）：`Entity.__setattr__` 焊死「当前值 ≤ 上限」。

不再依赖调用方自觉调用 `clamp_immortal_body`：
- current_hp ≤ 血限（豁免：遗物【猩红果实】、【增殖】的无限血限）
- current_mana ≤ 法限（豁免：遗物【苍白之花】）
- current_speed ≤ 速限（无豁免）

封顶在实体绑定战斗引擎后无条件生效；模型层/局外构造期不施加，
避免与「先写当前值、后写上限」的初始化/反序列化顺序打架。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import Entity, Relic, StatusEffect


def _engine_with_player(name="贾凡"):
    e = GameEngine(autosave=False, rng_seed=3)
    player = Entity(name, "轮回者", blood_limit=60, current_hp=60,
                    mana_limit=30, current_mana=30, speed_limit=9, current_speed=9)
    e.state.player = player
    e.state.phase = "in_combat"
    e.combat._bind_hp_hook(player)          # 绑定战斗引擎＝进入上限生效上下文
    return e, player


def test_current_hp_never_exceeds_blood_limit():
    e, p = _engine_with_player()
    p.current_hp = 999
    assert p.current_hp == 60


def test_current_mana_never_exceeds_mana_limit():
    e, p = _engine_with_player()
    p.current_mana = 999
    assert p.current_mana == 30


def test_current_speed_never_exceeds_speed_limit():
    e, p = _engine_with_player()
    p.current_speed = 999
    assert p.current_speed == 9


def test_clamping_is_not_bypassable_by_augmented_assignment():
    e, p = _engine_with_player()
    p.current_hp += 500
    p.current_mana += 500
    p.current_speed += 500
    assert (p.current_hp, p.current_mana, p.current_speed) == (60, 30, 9)


def test_scarlet_fruit_exempts_hp_only():
    e, p = _engine_with_player()
    e.state.relics = [Relic("猩红果实", "")]
    p.current_hp = 100
    assert p.current_hp == 100, "猩红果实：当前生命可超[血限]"
    p.current_mana = 100
    assert p.current_mana == 30, "豁免只作用于当前生命"


def test_pale_flower_exempts_mana_only():
    e, p = _engine_with_player()
    e.state.relics = [Relic("苍白之花", "")]
    p.current_mana = 100
    assert p.current_mana == 100, "苍白之花：当前法力可超[法限]"
    p.current_hp = 100
    assert p.current_hp == 60


def test_proliferation_exempts_hp():
    e, p = _engine_with_player()
    p.add_status(StatusEffect("无限血限", -1, 1, "增殖"))
    p.current_hp = 200
    assert p.current_hp == 200


def test_heal_cannot_overheal_without_exemption():
    e, p = _engine_with_player()
    p.current_hp = 50
    detail = p.heal(50)
    assert p.current_hp == 60
    assert detail["actual_heal"] == 10 and detail["overheal"] == 40


def test_heal_no_longer_takes_allow_overflow():
    import inspect
    from engine.models import Entity as E
    params = inspect.signature(E.heal).parameters
    assert "allow_overflow" not in params, "死参数已删除"


def test_apply_heal_respects_scarlet_exemption_out_of_combat():
    """局外（未绑定战斗引擎）也必须认同一豁免口径，否则会造出局外过疗。"""
    e = GameEngine(autosave=False, rng_seed=3)
    p = Entity("贾凡", "轮回者", blood_limit=40, current_hp=40,
               mana_limit=10, current_mana=10, speed_limit=5, current_speed=5)
    e.state.player = p
    e.state.relics = [Relic("猩红果实", "")]
    e.state.apply_heal(p, 40)
    assert p.current_hp == 80


def test_apply_heal_out_of_combat_without_relic_is_capped():
    e = GameEngine(autosave=False, rng_seed=3)
    p = Entity("贾凡", "轮回者", blood_limit=40, current_hp=40,
               mana_limit=10, current_mana=10, speed_limit=5, current_speed=5)
    e.state.player = p
    e.state.relics = []
    e.state.apply_heal(p, 40)
    assert p.current_hp == 40
