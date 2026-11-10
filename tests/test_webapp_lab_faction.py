"""角色卡自选阵营（2026-11-10 用户令）：轮回者 / 怪物，任意组合。

口径：**侧位就是 A/B 槽位**（A=你操作的轮回者侧，B=对面侧），阵营只是
**单位类型**，决定该单位按哪套规则逻辑运行——不按槽位硬编码，也不限制
组合：轮回者打轮回者、怪物打怪物、轮回者+怪物队友打轮回者，照摆。
引擎的两侧判定本来就是结构性的（on_player_side），与阵营无关。

每侧还能带怪物队友（state.friends / 并入 state.enemies），与现有
[朋友] 同机制：能被逐击选中、能挡刀，不占玩家自己的出手。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from webapp import lab, server


@pytest.fixture(autouse=True)
def clean_roster(tmp_path, monkeypatch):
    monkeypatch.setenv("LINJI_CHARACTERS_FILE", str(tmp_path / "chars.json"))


def _sheet(**overrides):
    sheet = {"mode": "free", "blood_limit": 90, "speed_limit": 6, "mana_limit": 2,
             "daowen": [], "relics": [], "consumables": []}
    sheet.update(overrides)
    return sheet


def _install(engine):
    server.SESSION["engine"] = engine
    server.SESSION["log"] = []
    server.SESSION["last_world"] = None
    server.SESSION["attack"] = None
    assert server._execute("round_start", {"relic_choices": {}})["success"]


def _play_hand(engine, actor_ref, target_ref, damage=0):
    """一个出手方的一整手（逐击全硬扛）。"""
    prepared = server._ui_attack_prepare({"actor_ref": actor_ref})
    assert prepared["success"], prepared
    for _ in range(prepared["attack"]["hit_count"]):
        assert server._ui_attack_declare(
            {"target_ref": target_ref, "damage": damage})["success"]
        assert server._ui_attack_respond({"choice": "tank"})["success"]


def _play_both_hands(engine):
    """按当前交替出手次序，双方各打一手。"""
    turn = engine.state.duel_turn
    first = "player:0" if turn == "player_side" else "enemy:0"
    second = "enemy:0" if first == "player:0" else "player:0"
    _play_hand(engine, first, "enemy:0" if first == "player:0" else "player:0")
    _play_hand(engine, second, "player:0" if second == "enemy:0" else "enemy:0")


def test_sheet_faction_defaults_and_validation():
    sheet = lab.normalize_sheet(_sheet(name="甲"))
    assert sheet["faction"] == "轮回者"          # 旧卡/缺省一律轮回者
    sheet = lab.normalize_sheet(_sheet(name="乙", faction="怪物"))
    assert sheet["faction"] == "怪物"
    with pytest.raises(ValueError, match="阵营"):
        lab.normalize_sheet(_sheet(name="丙", faction="神族"))


def test_monster_faction_card_on_slot_b_is_a_monster_rival():
    a = lab.save_sheet(_sheet(name="轮甲"))
    b = lab.save_sheet(_sheet(name="怪乙", faction="怪物", blood_limit=150,
                              speed_limit=4, mana_limit=3))
    prepared = lab.duel({"a": {"id": a["id"]}, "b": {"id": b["id"]},
                         "manual_both": True, "seed": 4})
    engine = prepared["engine"]
    assert engine.state.player.name == "轮甲"
    rival = engine.state.enemies[0]
    assert rival.name == "怪乙" and rival.entity_type == "怪物"
    assert rival.blood_limit == 150 and rival.speed_limit == 4 and rival.mana_limit == 3
    # 怪物阵营逻辑随实体类型自动生效：出厂自带【痛苦的偏爱】与【透支】
    assert any(relic.name == "痛苦的偏爱" for relic in rival.relics)
    assert "透支" in rival.dao_wen

    _install(engine)
    _play_both_hands(engine)        # 怪物侧照常可被逐击驱动、也照常出手


def test_monster_faction_card_on_slot_a_keeps_slot_a_on_player_side():
    """A 槽=你操作的一侧，不因阵营换位；A 卡是怪物时玩家实体就按怪物逻辑运行。"""
    a = lab.save_sheet(_sheet(name="怪甲", faction="怪物", blood_limit=150,
                              speed_limit=4, mana_limit=3))
    b = lab.save_sheet(_sheet(name="轮乙", blood_limit=80))
    prepared = lab.duel({"a": {"id": a["id"]}, "b": {"id": b["id"]},
                         "manual_both": True, "seed": 4})
    engine = prepared["engine"]
    player = engine.state.player
    assert player.name == "怪甲" and player.entity_type == "怪物"
    # 怪物出厂不变量补齐（与 Entity.__post_init__ 怪物分支同口径）
    assert any(relic.name == "痛苦的偏爱" for relic in player.relics)
    assert player.current_mana == 3 and player.current_speed == 4
    rival = engine.state.enemies[0]
    assert rival.name == "轮乙" and rival.entity_type == "轮回者"

    _install(engine)
    _play_both_hands(engine)


def test_monster_vs_monster_both_slots():
    a = lab.save_sheet(_sheet(name="怪甲", faction="怪物", blood_limit=150,
                              speed_limit=4, mana_limit=3))
    b = lab.save_sheet(_sheet(name="怪乙", faction="怪物", blood_limit=120,
                              speed_limit=3, mana_limit=2))
    prepared = lab.duel({"a": {"id": a["id"]}, "b": {"id": b["id"]},
                         "manual_both": True, "seed": 4})
    engine = prepared["engine"]
    assert engine.state.player.entity_type == "怪物"
    assert engine.state.enemies[0].entity_type == "怪物"

    _install(engine)
    _play_both_hands(engine)


def test_reincarnator_with_monster_teammates_vs_reincarnator():
    a = lab.save_sheet(_sheet(name="轮甲"))
    b = lab.save_sheet(_sheet(name="轮乙", speed_limit=3))
    prepared = lab.duel({"a": {"id": a["id"], "teammates": ["窃贼"]},
                         "b": {"id": b["id"]}, "manual_both": True, "seed": 4})
    engine = prepared["engine"]
    assert engine.state.player.entity_type == "轮回者"
    # A 方怪物队友进 state.friends（轮回者侧），B 卡是对面侧轮回者
    assert len(engine.state.friends) == 1
    friend = engine.state.friends[0]
    assert friend.entity_type == "怪物" and friend.name == "窃贼"
    assert engine.state.enemies[0].entity_type == "轮回者"

    _install(engine)
    _play_hand(engine, "player:0", "enemy:0")
    # B 手：怪物队友在玩家侧 → 是对面侧的合法逐击目标
    prepared = server._ui_attack_prepare({"actor_ref": "enemy:0"})
    assert prepared["success"], prepared
    assert server._ui_attack_declare({"target_ref": "friend:0", "damage": 0})["success"]
    assert server._ui_attack_respond({"choice": "tank"})["success"]


def test_reincarnator_vs_monster_card_with_teammates():
    a = lab.save_sheet(_sheet(name="轮甲"))
    b = lab.save_sheet(_sheet(name="怪乙", faction="怪物", blood_limit=130,
                              speed_limit=2, mana_limit=2))
    prepared = lab.duel({"a": {"id": a["id"]},
                         "b": {"id": b["id"], "teammates": ["窃贼"]},
                         "manual_both": True, "seed": 4})
    engine = prepared["engine"]
    # B 侧 = 怪物阵营卡 + 怪物队友，同在对面侧
    assert [e.entity_type for e in engine.state.enemies] == ["怪物", "怪物"]
    assert engine.state.enemies[0].name == "怪乙"
    assert engine.state.enemies[1].name == "窃贼"
    assert not engine.state.friends

    _install(engine)
    turn = engine.state.duel_turn
    actor = "player:0" if turn == "player_side" else "enemy:0"
    target = "enemy:1" if actor == "player:0" else "player:0"
    _play_hand(engine, actor, target)


def test_single_player_start_accepts_monster_faction_card():
    a = lab.save_sheet(_sheet(name="怪甲", faction="怪物", blood_limit=120,
                              speed_limit=3, mana_limit=2))
    prepared = lab.start({**a, "region": "罪孽都市"})
    engine = prepared["engine"]
    assert engine.state.player.entity_type == "怪物"
    assert any(relic.name == "痛苦的偏爱" for relic in engine.state.player.relics)


def test_teammate_cap_per_side():
    a = lab.save_sheet(_sheet(name="轮甲"))
    b = lab.save_sheet(_sheet(name="轮乙"))
    too_many = {"teammates": ["窃贼"] * (lab.MAX_ENEMIES + 1)}
    with pytest.raises(ValueError, match="A方怪物队友最多"):
        lab.duel({"a": {"id": a["id"], **too_many}, "b": {"id": b["id"]},
                  "manual_both": True})
    with pytest.raises(ValueError, match="B方怪物队友最多"):
        lab.duel({"a": {"id": a["id"]}, "b": {"id": b["id"], **too_many},
                  "manual_both": True})


def test_enemy_side_raizhe_rival_uses_own_consumable_in_respond_window():
    """敌方侧的轮回者（卡建对手）应对窗口用**自己携带的**消耗品。"""
    a = lab.save_sheet(_sheet(name="轮甲", blood_limit=80))
    b = lab.save_sheet(_sheet(name="轮乙", speed_limit=3,
                              consumables=[{"name": "备用血泵", "uses": 2}]))
    prepared = lab.duel({"a": {"id": a["id"]}, "b": {"id": b["id"]},
                         "manual_both": True, "seed": 4})
    engine = prepared["engine"]
    rival = engine.state.enemies[0]
    assert rival.entity_type == "轮回者" and rival.name == "轮乙"

    _install(engine)
    # 甲打乙：乙（敌方侧轮回者）被选中，先吃自己的备用血泵再硬扛
    assert server._ui_attack_prepare({})["success"]
    assert server._ui_attack_declare({"target_ref": "enemy:0", "damage": 1})["success"]
    rival.current_hp = 30
    cur = server._attack_view(engine)["current"]
    assert [(i["name"], i["uses"]) for i in cur["item_options"]] == [("备用血泵", 2)]
    step = server._ui_attack_respond({"choice": "use_item", "item_name": "备用血泵"})
    assert step["success"], step
    assert rival.current_hp == 50                       # 回复 20 落在乙身上
    assert engine.state.pending_attack                  # 窗口保持开放
    assert server._ui_attack_respond({"choice": "tank"})["success"]


def test_monster_defender_consumes_from_its_own_pool():
    """消耗品逻辑按阵营中立：怪物单位带消耗品也能被动使用（用自己那份）。"""
    a = lab.save_sheet(_sheet(name="轮甲", blood_limit=80))
    b = lab.save_sheet(_sheet(name="怪乙", faction="怪物", speed_limit=2,
                              consumables=[{"name": "备用血泵", "uses": 1}]))
    prepared = lab.duel({"a": {"id": a["id"]}, "b": {"id": b["id"]},
                         "manual_both": True, "seed": 4})
    engine = prepared["engine"]
    rival = engine.state.enemies[0]
    assert rival.entity_type == "怪物"

    _install(engine)
    prepared = server._ui_attack_prepare({})
    for _ in range(prepared["attack"]["hit_count"] - 1):
        assert server._ui_attack_declare({"target_ref": "enemy:0", "damage": 0})["success"]
        assert server._ui_attack_respond({"choice": "tank"})["success"]
    assert server._ui_attack_declare({"target_ref": "enemy:0", "damage": 1})["success"]
    rival.current_hp = 40
    step = server._ui_attack_respond({"choice": "use_item", "item_name": "备用血泵"})
    assert step["success"], step
    assert rival.current_hp == 60
    assert server._ui_attack_respond({"choice": "tank"})["success"]
