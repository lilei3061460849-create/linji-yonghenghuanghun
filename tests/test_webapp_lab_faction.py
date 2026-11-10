"""角色卡自选阵营（2026-11-10 用户令）：轮回者 / 怪物。

选了什么阵营，开战就按那个阵营的逻辑运行——引擎的战斗结构是
「轮回者侧 vs 怪物侧」，谁当轮回者由卡的阵营说了算，不按 A/B 槽位硬编码：
A 卡是怪物、B 卡是轮回者时，B 卡驱动轮回者侧、A 卡造成怪物对手。

消耗品的使用者也随阵营摆位走（actor_ref），任何参战单位都用自己携带的
消耗品——不再硬编码 state.player。
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


def _play_one_hand_each(engine):
    """双方各打一手（伤害 0，纯走流程）。"""
    prepared = server._ui_attack_prepare({})
    for _ in range(prepared["attack"]["hit_count"]):
        assert server._ui_attack_declare({"target_ref": "enemy:0", "damage": 0})["success"]
        assert server._ui_attack_respond({"choice": "tank"})["success"]
    prepared = server._ui_attack_prepare({"actor_ref": "enemy:0"})
    assert prepared["success"], prepared
    for _ in range(prepared["attack"]["hit_count"]):
        assert server._ui_attack_declare({"target_ref": "player:0", "damage": 0})["success"]
        assert server._ui_attack_respond({"choice": "tank"})["success"]


def test_sheet_faction_defaults_and_validation():
    sheet = lab.normalize_sheet(_sheet(name="甲"))
    assert sheet["faction"] == "轮回者"          # 旧卡/缺省一律轮回者
    sheet = lab.normalize_sheet(_sheet(name="乙", faction="怪物"))
    assert sheet["faction"] == "怪物"
    with pytest.raises(ValueError, match="阵营"):
        lab.normalize_sheet(_sheet(name="丙", faction="神族"))


def test_monster_faction_card_becomes_a_monster_opponent():
    a = lab.save_sheet(_sheet(name="轮甲"))
    b = lab.save_sheet(_sheet(name="怪乙", faction="怪物", blood_limit=150,
                              speed_limit=4, mana_limit=3))
    prepared = lab.duel({"a": {"id": a["id"]}, "b": {"id": b["id"]},
                         "manual_both": True, "seed": 4})
    engine = prepared["engine"]
    # A 卡是轮回者 → 照常驱动轮回者侧；B 卡是怪物 → 造成怪物对手
    assert engine.state.player.name == "轮甲"
    rival = engine.state.enemies[0]
    assert rival.name == "怪乙" and rival.entity_type == "怪物"
    assert rival.blood_limit == 150 and rival.speed_limit == 4 and rival.mana_limit == 3
    # 怪物阵营逻辑随实体类型自动生效：出厂自带【痛苦的偏爱】与【透支】
    assert any(relic.name == "痛苦的偏爱" for relic in rival.relics)
    assert "透支" in rival.dao_wen

    _install(engine)
    _play_one_hand_each(engine)     # 双方各打一手：怪物侧照常可被逐击驱动


def test_monster_card_on_slot_a_swaps_the_sides():
    a = lab.save_sheet(_sheet(name="怪甲", faction="怪物", blood_limit=150,
                              speed_limit=4, mana_limit=3))
    b = lab.save_sheet(_sheet(name="轮乙", blood_limit=80))
    prepared = lab.duel({"a": {"id": a["id"]}, "b": {"id": b["id"]},
                         "manual_both": True, "seed": 4})
    engine = prepared["engine"]
    # 阵营决定摆位：B 卡（轮回者）上轮回者侧，A 卡（怪物）上怪物侧
    assert engine.state.player.name == "轮乙"
    assert engine.state.player.entity_type == "轮回者"
    rival = engine.state.enemies[0]
    assert rival.name == "怪甲" and rival.entity_type == "怪物"

    _install(engine)
    _play_one_hand_each(engine)
    # 怪物实体的阵营规则照常跑：回始回满法力/速度（引擎怪物分支）
    assert rival.current_mana == 3 and rival.current_speed == 4


def test_two_monster_sides_are_rejected():
    a = lab.save_sheet(_sheet(name="怪甲", faction="怪物"))
    b = lab.save_sheet(_sheet(name="怪乙", faction="怪物"))
    with pytest.raises(ValueError, match="恰好一名轮回者"):
        lab.duel({"a": {"id": a["id"]}, "b": {"id": b["id"]}, "manual_both": True})
    with pytest.raises(ValueError, match="轮回者阵营的角色卡"):
        lab.duel({"a": {"id": a["id"]}, "b": {"kind": "monster", "monsters": ["鼠潮"]},
                  "manual_both": True})


def test_single_player_start_rejects_monster_faction_card():
    a = lab.save_sheet(_sheet(name="怪甲", faction="怪物"))
    with pytest.raises(ValueError, match="怪物阵营卡请放进 A/B 对决"):
        lab.start({**a, "region": "罪孽都市"})


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
    """消耗品逻辑按阵营中立：怪物单位带消耗品也能主动+被动使用（用自己那份）。"""
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
    # 耐久 1→0 耗尽：下一次应对窗口里不再出现
    assert server._ui_attack_respond({"choice": "tank"})["success"]
