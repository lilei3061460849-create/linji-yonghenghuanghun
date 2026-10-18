from engine.api import GameEngine
from engine.events import resolve_option_effect
from engine.models import Entity


def _engine(tmp_path, shards=500, peak=500):
    engine = GameEngine(db_path=str(tmp_path / "altar.db"),
                        save_dir=str(tmp_path), rng_seed=101)
    engine.state.player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    engine.state.shards = shards
    engine.state.shards_peak = peak
    return engine


def test_altar_random_integer_is_direct_profit_or_loss_multiplier(tmp_path, monkeypatch):
    engine = _engine(tmp_path, shards=500, peak=500)
    monkeypatch.setattr(
        engine.dice, "auto_roll",
        lambda _key, options, context="": {
            "selected": -3,
            "record": {"selected": -3, "context": context},
        },
    )

    result = resolve_option_effect(
        "投资碎片", engine, event_name="祭坛",
        params={"x": 100, "multiplier": 3},
    )

    assert result["outcome_multiplier"] == -3
    assert result["net_delta"] == -300
    assert engine.state.shards == 200
    assert result["outcome"] == "亏损"


def test_altar_zero_result_returns_principal_without_profit_or_loss(tmp_path, monkeypatch):
    engine = _engine(tmp_path, shards=500, peak=500)
    monkeypatch.setattr(
        engine.dice, "auto_roll",
        lambda _key, options, context="": {
            "selected": 0,
            "record": {"selected": 0, "context": context},
        },
    )

    result = resolve_option_effect(
        "投资碎片", engine, event_name="祭坛",
        params={"x": 100, "multiplier": 3},
    )

    assert result["net_delta"] == 0
    assert engine.state.shards == 500
    assert result["outcome"] == "持平"


def test_altar_multiplier_cap_increases_once_per_hundred_principal(tmp_path):
    engine = _engine(tmp_path, shards=1000, peak=1000)
    invalid = resolve_option_effect(
        "投资碎片", engine, event_name="祭坛",
        params={"x": 100, "multiplier": 12},
    )
    valid = resolve_option_effect(
        "投资碎片", engine, event_name="祭坛",
        params={"x": 200, "multiplier": 12},
    )

    assert invalid.get("error") or invalid.get("success") is False
    assert valid.get("outcome_multiplier") is not None
    assert -12 <= valid["outcome_multiplier"] <= 12
