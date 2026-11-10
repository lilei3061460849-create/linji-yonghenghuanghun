"""推演模式（`manual_both`）：演武场双方都由用户操作，B 方回合不调用 AI。

用户口径（2026-11-09）：「演武场本身就是方便推演测试的模式，让 AI 自动打
影响我测试」——所以 A 方出手后引擎把优先权交给 B 方，B 方的这一次真实行动
由界面上点选（目标/道纹/闪避），结算后自动换回 A 方。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from webapp import lab, server


@pytest.fixture
def roster_file(tmp_path, monkeypatch):
    path = tmp_path / "player_characters.json"
    monkeypatch.setenv("LINJI_CHARACTERS_FILE", str(path))
    return path


def _sheet(name, **overrides):
    sheet = {
        "name": name, "mode": "rule", "blood_points": 15,
        "speed_points": 6, "mana_points": 4, "daowen": ["杀伐"],
        "relics": [], "consumables": [], "resonance": "转换",
        "shards": 20, "energy": 3,
    }
    sheet.update(overrides)
    return sheet


def _install_duel(roster_file, manual_both=True, **b_overrides):
    a = lab.save_sheet(_sheet("甲"))
    b = lab.save_sheet(_sheet("乙", **b_overrides))
    payload = {"a": {"id": a["id"]}, "b": {"id": b["id"]},
               "region": "罪孽都市", "width": 11, "height": 11, "seed": 9}
    if manual_both is not None:
        payload["manual_both"] = manual_both
    prepared = lab.duel(payload)
    server.SESSION["engine"] = prepared["engine"]
    server.SESSION["log"] = []
    server.SESSION["last_world"] = None
    return prepared["engine"], prepared


def _battle(engine):
    return server._battle_view(engine)


def _player_attacks(engine):
    assert server._execute("round_start", {"relic_choices": {}})["success"]
    prepared = server._execute("prepare_attack", {"actor_ref": "player:0"})
    assert prepared["success"], prepared
    options = engine.state.pending_attack["options"]
    result = server._ui_attack({
        "target_ref": "enemy:0",
        "damages": [1] * int(options["hit_count"]),
    })
    assert result["success"], result
    return result


def test_duel_manual_flag_is_exposed_to_the_battle_view(roster_file):
    engine, prepared = _install_duel(roster_file)
    assert engine.state.event_modifiers["lab_manual_both"] is True
    assert any("推演模式" in note for note in prepared["notes"])

    snapshot = server._snapshot(engine)
    assert snapshot["manual_both"] is True
    assert snapshot["battle"]["manual_both"] is True
    # 战斗员面板带上「还剩几次出手」，推演时一眼看清谁还能动。
    assert all("actions_left" in c and "action_budget" in c
               for c in snapshot["battle"]["combatants"])


def test_duel_without_the_flag_keeps_the_ai_flow(roster_file):
    engine, _ = _install_duel(roster_file, manual_both=None)
    assert engine.state.event_modifiers.get("lab_manual_both") is False
    assert server._snapshot(engine)["manual_both"] is False


def test_turn_flips_after_each_side_acts(roster_file):
    engine, _ = _install_duel(roster_file)
    assert _battle(engine)["duel_turn"] == "player_side"

    _player_attacks(engine)
    assert _battle(engine)["duel_turn"] == "opponent_side"

    prepared = server._ui_monster_prepare()
    assert prepared["success"], prepared
    actors = prepared["result"]["actors"]
    assert [a["actor_ref"] for a in actors] == ["enemy:0"]
    assert actors[0]["attack_target_options"]
    assert "base_hits_per_attack" in actors[0]

    player = engine.state.player
    hp_before = player.current_hp
    result = server._ui_monster_phase({
        "manual": True, "target_ref": "player:0", "dodge": False})
    assert result["success"], result
    assert player.current_hp < hp_before
    # 交替出手：B 方行动完自动回到 A 方。
    assert _battle(engine)["duel_turn"] == "player_side"


def test_manual_mode_never_calls_the_ai_choice_builder(roster_file, monkeypatch):
    engine, _ = _install_duel(roster_file)
    _player_attacks(engine)

    def boom(*args, **kwargs):
        raise AssertionError("推演模式不应调用 AI 的 _monster_choices")

    monkeypatch.setattr(server, "_monster_choices", boom)
    assert server._ui_monster_prepare()["success"]
    result = server._ui_monster_phase({"manual": True, "target_ref": "player:0"})
    assert result["success"], result


def test_invalid_daowen_is_a_user_error_and_side_can_be_ended(roster_file):
    engine, _ = _install_duel(roster_file)
    _player_attacks(engine)

    prepared = server._ui_monster_prepare()
    assert prepared["success"]
    # 不在 prepare 选项里的道纹：报错让用户重选，不替他挑。
    bad = server._ui_monster_phase({"manual": True, "daowen_name": "不存在的道纹"})
    assert bad["success"] is False
    assert "不存在的道纹" in bad["error"]

    # 「结束 B 方回合」走引擎的 skip（不消耗出手，优先权回到 A 方）；
    # 即使刚点过「准备 B 方行动」留下 pending，也要能结束，而不是卡死。
    skip = server._ui_end_side({"actor_ref": "enemy:0"})
    assert skip["success"], skip
    assert engine.state.pending_monster_phase == {}
    assert _battle(engine)["duel_turn"] == "player_side"
