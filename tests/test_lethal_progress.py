"""致死进度（【迷失】/【癌变】/【凡庸】）——用户令 2026-09-15 / 2026-09-28：

「给致死的特殊事件标明进度」。2026-10-13：怪物不触发迷失，不显示迷失致死进度。

覆盖：
- 正常路径：进度串（迷失（10/50））与结构化计数同时可见；
- 边界条件：凡庸两条线取更接近触发的一条、血限为 0 时不产生癌变线、阈值恰好触发迷失；
  * 怪物达阈值：照常累计异变，collapsed=False / lost=False，不命零；
  * 非怪物达阈值：lost=True（由战斗层 _resolve_mutation_lost 决定变身/命零）。
- 错误输入：阈值唯一事实源——CombatEngine 的同名量必须引用 Entity，不得各写一份。
"""
import math
import os
import sys

from tests.setup_support import finish_initial_daowen, hand_priority_to_enemy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine import battle_report as BR
from engine.combat import CombatEngine, MEDIOCRITY_ROUNDS
from engine.models import Entity


def _player(**kw):
    data = dict(name="测试者", entity_type="轮回者", blood_limit=42, current_hp=42)
    data.update(kw)
    return Entity(**data)


def _monster(**kw):
    data = dict(name="测试怪", entity_type="怪物", blood_limit=30, current_hp=30)
    data.update(kw)
    return Entity(**data)


def test_progress_rendering_matches_rule_numbers():
    e = _player()
    e.mutation_count = 10
    e.total_healed = 30
    counters = e.lethal_counters()
    assert counters["迷失"] == (10, Entity.MUTATION_COLLAPSE_THRESHOLD) == (10, 50)
    assert counters["癌变"] == (30, math.ceil(42 * 2))
    assert e.lethal_progress() == ["迷失（10/50）", "癌变（30/84）"]


def test_mediocrity_shows_the_closer_line():
    e = _player()
    e.no_action_rounds, e.no_damage_rounds = 1, 4
    assert e.lethal_counters()["凡庸·未致敌掉血"] == (4, Entity.MEDIOCRITY_ROUNDS)
    assert "凡庸·未致敌掉血（4/5）" in e.lethal_progress()
    other = _player()
    other.no_action_rounds = 3
    assert "凡庸·未出手（3/5）" in other.lethal_progress()


def test_zero_blood_limit_has_no_cancer_line():
    e = _player(blood_limit=0, current_hp=0)
    assert "癌变" not in e.lethal_counters()


def test_to_dict_and_resource_line_carry_progress():
    e = _player()
    e.mutation_count = 45
    data = e.to_dict()
    assert data["lethal_progress"] == ["迷失（45/50）", "癌变（0/84）"]
    assert data["lethal_counters"]["迷失"] == [45, 50]
    assert data["mutation_count"] == 45
    assert "致死进度[迷失（45/50）" in BR.resource_line(e)


def test_thresholds_have_a_single_source():
    assert CombatEngine.PROLIFERATION_THRESHOLD == Entity.CANCER_HEAL_MULTIPLIER
    assert CombatEngine.CANCER_THRESHOLD == Entity.CANCER_HEAL_MULTIPLIER
    assert MEDIOCRITY_ROUNDS == Entity.MEDIOCRITY_ROUNDS == 5
    assert Entity.MUTATION_COLLAPSE_THRESHOLD == 50


def test_monster_mutation_threshold_does_not_kill():
    """怪物达到或超过旧迷失阈值，只累计异变，生命不变。"""
    m = _monster()
    m.add_mutation(49)
    assert m.is_alive is True
    for layers in (1, 100):
        r = m.add_mutation(layers)
        assert r["collapsed"] is False and r["lost"] is False
        assert m.is_alive is True and m.current_hp == 30
    assert m.mutation_count == 150


def test_lost_non_monster_flags_lost_without_dying():
    """非怪物（轮回者/朋友/员工/…）达阈值：lost=True，但模型层不直接命零——
    交给战斗层 _resolve_mutation_lost 根据是否有队友决定变身/命零。"""
    p = _player()
    p.add_mutation(49)
    assert p.is_alive is True
    r = p.add_mutation(1)
    assert r["collapsed"] is False
    assert r["lost"] is True
    # 模型层不直接改 is_alive：变身/命零属于战斗层逻辑
    assert p.is_alive is True


def _new_engine(tmp_path, region="龙心谷"):
    e = GameEngine(db_path=str(tmp_path / "lethal.db"), rng_seed=1)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": region})
    optional = set()   # 2026-10-13：【三相残韵盘】删除，无需规避
    choice = next((name for name in setup["result"]["relic_choices"] if name not in optional),
                  setup["result"]["relic_choices"][0])
    e.execute_action("choose_discovered_relic", {"relic_name": choice})
    e.state.energy = 0
    return e


def test_monster_phase_payload_carries_monster_progress(tmp_path):
    """怪物只显示实际适用的致死进度，不能显示虚假的迷失倒计时。"""
    e = _new_engine(tmp_path)
    e.execute_action("battle_start")
    e.execute_action("round_start", {})
    hand_priority_to_enemy(e)  # 交替出手：声明轮回者本回合已交出优先权
    prepared = e.execute_action("prepare_monster_phase", {})
    assert prepared["success"], prepared
    actors = prepared["result"]["actors"]
    assert actors, "本回合应有可行动的怪物 actor"
    for actor in actors:
        assert "迷失" not in actor["lethal_counters"]
        assert "癌变" in actor["lethal_counters"]
        assert all("迷失" not in item for item in actor["lethal_progress"])
