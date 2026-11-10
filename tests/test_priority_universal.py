"""先手顺序是通用规则（2026-11-09 用户令）。

规则正文·战斗行动：[速限]→[法限]→[血限]→当前生命，逐项比较，数值大者先出手；
四项完全相同则随机首手、之后每回合交替。**没有**「仅死斗适用」的例外，也不区分
开放地图、副本遭遇、演武场或任何其它进入战斗的途径。

此前引擎在 `_initial_priority_side` 里写死了「轮回者侧优先」，只有演武场 A/B
对打时用一个 `duel_priority_by_stats` 开关切到属性口径——那等于给玩家一个结构性
先手红利，也把通用规则锁进了「仅某模式」的兼容路径。本文件锁住三件事：

1. 开放地图（非死斗、非演武场）遇到速限更高的怪物时，怪物先手；
2. 反过来玩家属性占优时，玩家先手；
3. 四项完全相同：随机首手，之后逐回合交替。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.api import GameEngine  # noqa: E402
from engine.models import Entity  # noqa: E402
from sim.build_learner import round_start_relic_choices  # noqa: E402
from tests.setup_support import finish_initial_daowen, opponent_passes  # noqa: E402


def _engine(tmp_path, name: str) -> GameEngine:
    e = GameEngine(db_path=str(tmp_path / f"{name}.db"),
                   save_dir=str(tmp_path / "saves"),
                   sealed_candidate_path=str(tmp_path / "sealed.json"),
                   death_book_path=str(tmp_path / "death.md"), rng_seed=11)
    assert e.execute_action("setup_attributes", {
        "name": "测试者", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })["success"]
    assert finish_initial_daowen(e)["success"]
    assert e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})["success"]
    setup = e.execute_action("setup_choose_region", {"region": "乱葬岗"})
    assert setup["success"], setup
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    # 遗物效果与本用例无关（先手只看四项属性），清空后钉死环境。
    e.state.relics = []
    return e


def _open_map_battle(tmp_path, name: str, start_round: bool = True,
                     **monster_stats) -> GameEngine:
    """开放地图遭遇：不是死斗、不是演武场，只按正常 battle_start 进场。"""
    e = _engine(tmp_path, name)
    e.state.energy = 0
    bs = e.execute_action("battle_start", {"relic_choices": {}})
    assert bs["success"], bs
    stats = {"blood_limit": 40, "current_hp": 40, "attack_count": 1, "attack_power": 3}
    stats.update(monster_stats)
    e.state.enemies.clear()
    e.state.enemies.append(Entity("靶怪", "怪物", **stats))
    e.state.monster_reinforcements = []
    assert e.state.in_final_duel is False, "本文件必须跑在开放地图路径上"
    if start_round:
        started = e.execute_action("round_start", {"relic_choices": round_start_relic_choices(e)})
        assert started["success"], started
    return e


def test_faster_monster_takes_the_first_hand_in_an_open_map_encounter(tmp_path):
    """速限更高的怪物在开放地图同样先手——先手不是死斗专属。"""
    e = _open_map_battle(tmp_path, "fast", speed_limit=9, current_speed=9,
                         mana_limit=2, current_mana=2)
    player = e.state.player
    assert e.combat._duel_priority_key_of(e.state.enemies[0]) > \
        e.combat._duel_priority_key_of(player), "夹具前提：怪物四项占优"
    assert e.state.duel_turn == "opponent_side", "怪物必须先手"

    # 而且这个先手是真的能办事的优先权：怪物【休整】交出后轮回者才接手。
    assert opponent_passes(e)["success"]
    assert e.state.duel_turn == "player_side"


def test_player_keeps_the_first_hand_when_its_stats_win(tmp_path):
    """玩家属性占优时仍然先手（规则对称：谁大谁先，不区分阵营）。"""
    e = _open_map_battle(tmp_path, "slow", speed_limit=0, current_speed=0,
                         mana_limit=0, current_mana=0)
    assert e.combat._duel_priority_key_of(e.state.player) > \
        e.combat._duel_priority_key_of(e.state.enemies[0]), "夹具前提：玩家四项占优"
    assert e.state.duel_turn == "player_side"


def test_tied_first_hand_is_random_then_alternates(tmp_path):
    """四项完全相同：随机首手，之后每回合交替（不固定成某一侧）。"""
    # 先起战斗、不急着回始：照轮回者回始前的四项面板造一只完全同档的怪。
    e = _open_map_battle(tmp_path, "tie", start_round=False, speed_limit=1, current_speed=1,
                         mana_limit=1, current_mana=1)
    key = e.combat._duel_priority_key_of(e.state.player)
    e.state.enemies[0] = Entity(
        "同档怪", "怪物",
        blood_limit=key[2], current_hp=key[3],
        speed_limit=key[0], current_speed=key[0],
        mana_limit=key[1], current_mana=key[1],
        attack_count=1, attack_power=1)
    assert e.combat._duel_priority_key_of(e.state.enemies[0]) == key, "夹具前提：四项全同"
    started = e.execute_action("round_start", {"relic_choices": round_start_relic_choices(e)})
    assert started["success"], started

    # 回始刚判过一次：第一次平局决定首手；再判一次必须是另一侧。
    first = e.state.event_modifiers["duel_priority_tie_first"]
    assert first in ("player_side", "opponent_side")
    second = e.combat._stats_priority_side()
    assert second != first, "平局必须逐回合交替，不能每次都给同一侧"


def test_priority_no_longer_depends_on_a_mode_flag(tmp_path):
    """通用规则不靠开关：开放地图战斗里没有任何 `duel_priority_by_stats` 标记。"""
    e = _open_map_battle(tmp_path, "noflag", speed_limit=9, current_speed=9)
    assert "duel_priority_by_stats" not in e.state.event_modifiers
    assert e.state.duel_turn == "opponent_side"
