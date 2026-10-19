"""回归测试：一阶真实胜者生成器的样本目录扫描。"""
import json
import sys


def test_real_winner_generator_scans_existing_samples_without_name_error(tmp_path, monkeypatch):
    from sim import produce_real_winners

    monkeypatch.setattr(produce_real_winners, "WINNER_DIR", str(tmp_path))
    (tmp_path / "winner_01.json").write_text(
        json.dumps({"candidates": []}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "winner_03.json").write_text(
        json.dumps({"origin": {"seed": 17}}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "produce_real_winners.py", "--legacy-seven-battle-mode", "--count", "0", "--max-games", "0"
    ])

    produce_real_winners.main()

    # 扫描旧格式文件时应安全跳过其来源信息，而不是覆盖或删除既有数据。
    assert (tmp_path / "winner_01.json").exists()
    assert (tmp_path / "winner_03.json").exists()



def test_real_winner_generator_fails_when_requested_samples_are_missing(tmp_path, monkeypatch):
    import pytest
    from sim import produce_real_winners

    monkeypatch.setattr(produce_real_winners, "WINNER_DIR", str(tmp_path))
    monkeypatch.setattr(produce_real_winners, "play_first_tier",
                        lambda *args, **kwargs: {"cleared": 0, "won": False, "invalid": False})
    monkeypatch.setattr(sys, "argv", [
        "produce_real_winners.py", "--legacy-seven-battle-mode", "--count", "1", "--max-games", "1"
    ])

    with pytest.raises(SystemExit) as exc:
        produce_real_winners.main()
    assert exc.value.code == 2



def test_city_growth_learns_before_spending_energy_on_hiring():
    from types import SimpleNamespace
    from sim.produce_real_winners import choose_pre_battle

    player = SimpleNamespace(dao_wen={"杀伐"}, current_hp=60, blood_limit=60)
    engine = SimpleNamespace(state=SimpleNamespace(
        current_region="罪孽都市", employees=[], player=player, friends=[]))

    action, params = choose_pre_battle(
        engine, 2, ["先发制人"], ["庇护"], rng=__import__("random").Random(1))

    assert action == "学习"
    assert params == {"sub": "daowen", "tier": 1, "names": ["庇护"]}


def test_dragon_heart_growth_learns_before_spending_energy_on_exploration():
    from types import SimpleNamespace
    from sim.produce_real_winners import choose_pre_battle

    player = SimpleNamespace(dao_wen={"再生"}, current_hp=60, blood_limit=60)
    engine = SimpleNamespace(state=SimpleNamespace(
        current_region="龙心谷", employees=[], player=player, friends=[]))

    action, params = choose_pre_battle(
        engine, 2, ["生生不息"], [], rng=__import__("random").Random(1))

    assert action == "学习"
    assert params == {"sub": "spell", "tier": 1, "names": ["生生不息"]}


def test_real_winner_snapshot_validator_rejects_legacy_candidate_wrapper():
    import pytest
    from sim.produce_real_winners import validate_winner_snapshot

    with pytest.raises(ValueError, match="legacy candidates wrapper"):
        validate_winner_snapshot({"candidates": []})


def test_real_winner_snapshot_validator_requires_seven_battles_and_seal_provenance():
    import pytest
    from sim.produce_real_winners import validate_winner_snapshot

    snapshot = {
        "player": {"entity_type": "轮回者", "is_alive": True},
        "origin": {
            "seed": 1, "region": "罪孽都市", "generator": "sim/produce_real_winners.py",
            "cleared": 4, "sealed": False,
        },
    }
    with pytest.raises(ValueError, match="all seven"):
        validate_winner_snapshot(snapshot)



def test_seal_queue_is_unwrapped_to_a_top_level_winner_snapshot():
    from sim.produce_real_winners import extract_sealed_snapshot, validate_winner_snapshot

    character = {
        "player": {"entity_type": "轮回者", "is_alive": True},
        "friends": [], "employees": [], "relics": [],
        "origin": {
            "seed": 4, "region": "罪孽都市", "generator": "sim/produce_real_winners.py",
            "cleared": 7, "sealed": True,
            "initial_attributes": {"blood_points": 11, "speed_points": 8, "mana_points": 6},
            "initial_attribute_points": 25,
        },
    }
    wrapped = {"candidates": {"2": [character]}, "progression": {"unlocked_tier": 1}}

    extracted = extract_sealed_snapshot(wrapped, 2)

    assert "candidates" not in extracted
    assert extracted["player"]["entity_type"] == "轮回者"
    validate_winner_snapshot(extracted)
