"""阈值实验的覆盖必须是进程内临时值，不能污染正式规则默认值。"""
from engine.combat import CombatEngine
from engine.models import Entity
from sim.audits.lethal_threshold_lab import PROFILES, apply_profile
import engine.combat as combat_module
import sim.duel_pvp as duel_pvp


def test_threshold_profiles_match_current_rules_and_candidates():
    assert PROFILES["baseline"] == {"cancer": 2.0, "mediocrity": 5, "debt": 10}
    assert PROFILES["cancer_3x"]["cancer"] == 3.0
    assert PROFILES["cancer_4x"]["cancer"] == 4.0
    assert PROFILES["mediocrity_7"]["mediocrity"] == 7
    assert PROFILES["mediocrity_8"]["mediocrity"] == 8
    assert PROFILES["debt_20"]["debt"] == 20
    assert PROFILES["debt_30"]["debt"] == 30


def test_profile_overrides_are_temporary_and_deadlock_guard_tracks_mediocrity():
    original = (
        Entity.CANCER_HEAL_MULTIPLIER,
        Entity.MEDIOCRITY_ROUNDS,
        combat_module.MEDIOCRITY_ROUNDS,
        duel_pvp.DEADLOCK_MIN_ROUNDS,
        CombatEngine.PROLIFERATION_THRESHOLD,
        CombatEngine.CANCER_THRESHOLD,
        CombatEngine.DEBT_THRESHOLD,
    )
    with apply_profile("mediocrity_8"):
        assert Entity.MEDIOCRITY_ROUNDS == 8
        assert combat_module.MEDIOCRITY_ROUNDS == 8
        assert duel_pvp.DEADLOCK_MIN_ROUNDS == 9
    assert (
        Entity.CANCER_HEAL_MULTIPLIER,
        Entity.MEDIOCRITY_ROUNDS,
        combat_module.MEDIOCRITY_ROUNDS,
        duel_pvp.DEADLOCK_MIN_ROUNDS,
        CombatEngine.PROLIFERATION_THRESHOLD,
        CombatEngine.CANCER_THRESHOLD,
        CombatEngine.DEBT_THRESHOLD,
    ) == original
