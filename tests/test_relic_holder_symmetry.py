"""遗物按**持有者阵营**对称结算的守护测试（2026-11-09 用户令）。

用户口径：「为什么 B 方不能生效？两方本质上不都是一样的吗？仅仅只是阵容不同
而已」——A 方与 B 方在规则层完全对称，只是阵容/归属不同。遗物效果必须由
**实际持有者**结算，而不是写死玩家：

- 【减速】[战始]：持有者让**自己的敌对侧**当前速度减半；
- 【回锋刀】[回始]：持有者按 3×（速限−当前速度）造伤，敌方没有「显式提交
  目标」这一步，走确定性缺省目标；
- 【忘忧香】：溢出回复 1:1 转格挡，敌方轮回者从 `opponent_relics` 读取。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.enums import TriggerTiming
from engine.models import Entity, Relic
from tests.setup_support import finish_initial_daowen


def _engine():
    engine = GameEngine(rng_seed=7)
    engine.execute_action("setup_attributes", {"blood_points": 11, "speed_points": 6, "mana_points": 8})
    finish_initial_daowen(engine)
    engine.state.current_region = "罪孽都市"
    engine.state.phase = "in_combat"
    engine.state.relics = []
    player = engine.state.player
    player.speed_limit = 10
    player.current_speed = 10
    player.current_hp = player.blood_limit
    return engine, player


def _rival(engine, *, speed_limit=10, current_speed=None, hp=90):
    rival = Entity(
        name="乙", entity_type="轮回者", blood_limit=hp, current_hp=hp,
        speed_limit=speed_limit,
        current_speed=speed_limit if current_speed is None else current_speed,
        attack_count=1, attack_power=5,
    )
    engine.state.enemies.append(rival)
    return rival


def test_opponent_su_jian_slows_player_side():
    """B 方持【减速】：[战始]让 A 方当前速度减半（与 A 方持有时打 B 方同口径）。"""
    engine, player = _engine()
    _rival(engine)
    engine.state.opponent_relics = [Relic(name="减速", effect="", enabled=True)]

    engine.combat.process_relics(TriggerTiming.BATTLE_START, {"relic_choices": {}})

    assert player.current_speed == 5


def test_player_su_jian_still_slows_enemy_side():
    """A 方持【减速】的原有口径不变（回归保护）。"""
    engine, player = _engine()
    rival = _rival(engine)
    engine.state.relics = [Relic(name="减速", effect="", enabled=True)]

    engine.combat.process_relics(TriggerTiming.BATTLE_START, {"relic_choices": {}})

    assert player.current_speed == 10
    assert rival.current_speed == 5


def test_opponent_huifeng_round_start_damages_player():
    """B 方持【回锋刀】：[回始]按速度缺口造 3×伤，目标取玩家（唯一敌对存活单位）。"""
    engine, player = _engine()
    _rival(engine, speed_limit=6, current_speed=2)
    engine.state.opponent_relics = [Relic(name="回锋刀", effect="", enabled=True)]
    hp_before = player.current_hp

    logs = engine.combat.process_relics(TriggerTiming.ROUND_START, {"relic_choices": {}})

    assert player.current_hp == hp_before - 3 * (6 - 2)
    assert any("回锋刀" in log for log in logs)


def test_opponent_huifeng_does_nothing_without_speed_gap():
    engine, player = _engine()
    _rival(engine, speed_limit=6, current_speed=6)
    engine.state.opponent_relics = [Relic(name="回锋刀", effect="", enabled=True)]
    hp_before = player.current_hp

    engine.combat.process_relics(TriggerTiming.ROUND_START, {"relic_choices": {}})

    assert player.current_hp == hp_before


def test_opponent_wangyouxiang_turns_overheal_into_shield():
    """B 方持【忘忧香】：溢出回复 1:1 转格挡。"""
    engine, player = _engine()
    rival = _rival(engine, hp=30)
    engine.state.opponent_relics = [Relic(name="忘忧香", effect="", enabled=True)]

    detail = engine.state.apply_heal(rival, 10)

    assert detail["overheal"] == 10
    assert detail["wangyouxiang_block"] == 10
    assert rival.shield == 10


def test_opponent_wangyouxiang_respects_disabled_slot():
    """持有但 `enabled=False` 的遗物不生效（启用槽位口径双方一致）。"""
    engine, player = _engine()
    rival = _rival(engine, hp=30)
    engine.state.opponent_relics = [Relic(name="忘忧香", effect="", enabled=False)]

    detail = engine.state.apply_heal(rival, 10)

    assert detail["overheal"] == 10
    assert "wangyouxiang_block" not in detail
    assert rival.shield == 0


def test_player_side_wangyouxiang_still_covers_friends():
    """A 方持【忘忧香】时，朋友/员工的溢出仍转格挡（阵营遗物口径，回归保护）。"""
    engine, player = _engine()
    engine.state.relics = [Relic(name="忘忧香", effect="", enabled=True)]
    friend = Entity("友", "朋友", blood_limit=30, current_hp=30)
    engine.state.friends = [friend]

    detail = engine.state.apply_heal(friend, 7)

    assert detail["wangyouxiang_block"] == 7
    assert friend.shield == 7
