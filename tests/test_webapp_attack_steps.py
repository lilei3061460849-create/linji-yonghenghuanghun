"""逐击普攻（A 分伤害 → B 选应对 → 回到 A）：服务端状态机的回归。

用户口径（2026-11-09）：普攻不是一次性把 N 刀全打出去。A 选中目标并决定这一击
的 X 值 → 交棒给 B，B 从可用被动行动里选（硬扛/招架/闪避/血影）→ A 还有余击就
回到起点，没有就结束这一次出手。

引擎仍然是「一次出手原子结算」，所以这里钉的是界面层状态机：每一步只暴露当前
这一击，最后把累积的逐击选择拼成一次 resolve_attack 提交。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from webapp import lab, server


@pytest.fixture
def duel(tmp_path, monkeypatch):
    monkeypatch.setenv("LINJI_CHARACTERS_FILE", str(tmp_path / "chars.json"))
    a = lab.save_sheet({"name": "甲", "mode": "free", "blood_limit": 90,
                        "speed_limit": 6, "mana_limit": 2,
                        "daowen": [], "relics": [], "consumables": []})
    b = lab.save_sheet({"name": "乙", "mode": "free", "blood_limit": 200,
                        "speed_limit": 2, "mana_limit": 2,
                        "daowen": [], "relics": [], "consumables": []})
    prepared = lab.duel({"a": {"id": a["id"]}, "b": {"id": b["id"]},
                         "manual_both": True, "seed": 4})
    engine = prepared["engine"]
    server.SESSION["engine"] = engine
    server.SESSION["log"] = []
    server.SESSION["last_world"] = None
    server.SESSION["attack"] = None
    assert server._execute("round_start", {"relic_choices": {}})["success"]
    return engine


def test_attack_is_declared_and_defended_hit_by_hit(duel):
    engine = duel
    prepared = server._ui_attack_prepare({})
    assert prepared["success"], prepared
    attack = prepared["attack"]
    assert attack["phase"] == "declare"
    assert attack["hit_index"] == 1
    hit_count = attack["hit_count"]
    assert hit_count > 1

    # 第 1 击：A 分 1 点 → 交棒 B
    step = server._ui_attack_declare({"target_ref": "enemy:0", "damage": 1})
    assert step["success"], step
    assert step["attack"]["phase"] == "respond"
    assert step["attack"]["current"]["target"] == "乙"
    # 还没结算：引擎的 pending_attack 仍在，敌人血量未变
    assert engine.state.pending_attack
    assert engine.state.enemies[0].current_hp == 200

    # B 选闪避 → 回到 A 的第 2 击
    step = server._ui_attack_respond({"choice": "dodge"})
    assert step["success"], step
    assert step["attack"]["phase"] == "declare"
    assert step["attack"]["hit_index"] == 2
    assert step["attack"]["hits"] == [{"index": 1, "target": "乙",
                                       "damage": 1, "choice": "dodge"}]

    # 余下全部硬扛：引擎逐击结算，最后一击收尾
    done = server._ui_attack_auto({})
    assert done["success"], done
    assert done["result"]["finished"] is True
    assert done["result"]["hit_index"] == hit_count
    assert done["result"]["hits"][-1]["damage_dealt"] > 0    # 硬扛吃满
    assert not engine.state.pending_attack
    # 第 1 击闪避掉了、其余落地：乙的血量必须真的掉了
    assert engine.state.enemies[0].current_hp < 200


def test_declare_cannot_exceed_the_damage_budget(duel):
    prepared = server._ui_attack_prepare({})
    budget = prepared["attack"]["damage_budget"]
    step = server._ui_attack_declare({"target_ref": "enemy:0", "damage": budget + 1})
    assert step["success"] is False
    assert "预算" in step["error"]
    # 被拒的一击不该吃掉预算，也不该推进到 B 方
    again = server._ui_attack_prepare({})
    assert again["attack"]["damage_left"] == budget


def test_defence_needs_speed_and_order_is_enforced(duel):
    engine = duel
    server._ui_attack_prepare({})
    # 顺序：还没分配伤害就先让 B 应对，必须报错
    early = server._ui_attack_respond({"choice": "dodge"})
    assert early["success"] is False
    assert "分配伤害" in early["error"]

    # 乙速限 2：第 3 次闪避必须被挡（闪避各占 1 点速度）
    for _ in range(2):
        assert server._ui_attack_declare({"target_ref": "enemy:0", "damage": 1})["success"]
        assert server._ui_attack_respond({"choice": "dodge"})["success"]
    assert server._ui_attack_declare({"target_ref": "enemy:0", "damage": 1})["success"]
    blocked = server._ui_attack_respond({"choice": "dodge"})
    assert blocked["success"] is False
    assert "速度" in blocked["error"]
    assert engine.state.pending_attack          # 没被这次拒绝弄丢


def test_target_dying_mid_attack_still_finishes_the_hand(duel):
    """逐击中途把人打死：剩下的击数照常走完（记空刀），不能把这一手卡住。"""
    engine = duel
    rival = engine.state.enemies[0]
    rival.current_hp = 1
    prepared = server._ui_attack_prepare({"actor_ref": "player:0"})
    budget = prepared["attack"]["damage_budget"]

    step = server._ui_attack_declare({"target_ref": "enemy:0", "damage": budget})
    assert step["success"], step
    step = server._ui_attack_respond({"choice": "tank"})
    assert step["success"], step
    assert rival.current_hp == 0 and not rival.is_alive

    done = server._ui_attack_auto({})
    assert done["success"], done
    assert done["result"]["finished"] is True
    assert not engine.state.pending_attack
    # 剩下的击全部以「目标已命零」跳过，且出手只算一次
    assert engine.state.player.actions_used_this_round == 1


def test_priority_hands_over_only_when_the_hand_is_finished(duel):
    """一次出手内优先权不换边：否则第 1 击结算完就换人，剩下的击永远打不出去。"""
    engine = duel
    prepared = server._ui_attack_prepare({"actor_ref": "player:0"})
    hit_count = prepared["attack"]["hit_count"]
    assert engine.state.duel_turn == "player_side"
    for index in range(hit_count):
        assert server._ui_attack_declare(
            {"target_ref": "enemy:0", "damage": 0})["success"]
        assert server._ui_attack_respond({"choice": "tank"})["success"]
        if index < hit_count - 1:
            assert engine.state.duel_turn == "player_side"
    assert engine.state.duel_turn == "opponent_side"     # 这一手打完了才交棒


# ==== 消耗品=主动且被动行动（2026-11-10 用户令）====
# 被普攻选中时也能先用一件消耗品再选应对：不耗出手、不耗应对，窗口保持开放；
# 【必中】次数覆盖本击时被动行动整体被锁（规则正文·被动行动定义已加入「使用消耗品」）。
@pytest.fixture
def duel_with_item(tmp_path, monkeypatch):
    """同 duel，但甲（玩家）带一件扭曲工具【备用血泵】（3 耐久，自身回复 20）。"""
    monkeypatch.setenv("LINJI_CHARACTERS_FILE", str(tmp_path / "chars.json"))
    a = lab.save_sheet({"name": "甲", "mode": "free", "blood_limit": 90,
                        "speed_limit": 6, "mana_limit": 2,
                        "daowen": [], "relics": [],
                        "consumables": [{"name": "备用血泵", "uses": 3}]})
    b = lab.save_sheet({"name": "乙", "mode": "free", "blood_limit": 200,
                        "speed_limit": 2, "mana_limit": 2,
                        "daowen": [], "relics": [], "consumables": []})
    prepared = lab.duel({"a": {"id": a["id"]}, "b": {"id": b["id"]},
                         "manual_both": True, "seed": 4})
    engine = prepared["engine"]
    server.SESSION["engine"] = engine
    server.SESSION["log"] = []
    server.SESSION["last_world"] = None
    server.SESSION["attack"] = None
    assert server._execute("round_start", {"relic_choices": {}})["success"]
    return engine


def _finish_this_hand():
    """余下所有击全部硬扛并结算。"""
    done = server._ui_attack_auto({})
    assert done["success"], done


def test_defender_consumable_options_surface_in_the_respond_window(duel_with_item):
    engine = duel_with_item
    # 甲打乙：乙（敌方）没有消耗品，选项为空且不可用
    assert server._ui_attack_prepare({})["success"]
    assert server._ui_attack_declare({"target_ref": "enemy:0", "damage": 1})["success"]
    cur = server._attack_view(engine)["current"]
    assert cur["item_options"] == [] and cur["can_use_item"] is False
    refused = server._ui_attack_respond({"choice": "use_item", "item_name": "备用血泵"})
    assert refused["success"] is False
    assert "没有可用消耗品" in refused["error"]
    assert engine.state.pending_attack          # 拒绝不弄丢窗口
    _finish_this_hand()

    # 乙打甲：甲的消耗品出现在应对选项里
    assert server._ui_attack_prepare({"actor_ref": "enemy:0"})["success"]
    assert server._ui_attack_declare({"target_ref": "player:0", "damage": 1})["success"]
    cur = server._attack_view(engine)["current"]
    assert [(i["name"], i["uses"]) for i in cur["item_options"]] == [("备用血泵", 3)]
    assert cur["can_use_item"] is True and cur["item_locked"] is False


def test_defender_can_use_a_consumable_before_choosing_the_response(duel_with_item):
    engine = duel_with_item
    # 甲先把这一手打完（伤害 0），轮到乙出手
    assert server._ui_attack_prepare({})["success"]
    for _ in range(server._attack_view(engine)["hit_count"]):
        assert server._ui_attack_declare({"target_ref": "enemy:0", "damage": 0})["success"]
        assert server._ui_attack_respond({"choice": "tank"})["success"]
    hands_after_a = engine.state.player.actions_used_this_round

    # 乙第 1 击打甲：甲先吃【备用血泵】再硬扛——不耗应对、不耗出手、窗口保持开放
    assert server._ui_attack_prepare({"actor_ref": "enemy:0"})["success"]
    assert server._ui_attack_declare({"target_ref": "player:0", "damage": 1})["success"]
    engine.state.player.current_hp = 50
    step = server._ui_attack_respond({"choice": "use_item", "item_name": "备用血泵"})
    assert step["success"], step
    assert engine.state.player.current_hp == 70          # 回复 20 真实落地
    assert engine.state.pending_attack                   # 窗口没关，本击还没结算
    cur = server._attack_view(engine)["current"]
    assert [(i["name"], i["uses"]) for i in cur["item_options"]] == [("备用血泵", 2)]
    step = server._ui_attack_respond({"choice": "tank"})
    assert step["success"], step
    assert engine.state.player.actions_used_this_round == hands_after_a   # 出手数没变


def test_bizhong_locks_the_respond_window_consumable(duel_with_item):
    """【必中】次数覆盖本击：被动行动整体被锁，消耗品也用不了。"""
    engine = duel_with_item
    assert server._ui_attack_prepare({})["success"]
    for _ in range(server._attack_view(engine)["hit_count"]):
        assert server._ui_attack_declare({"target_ref": "enemy:0", "damage": 0})["success"]
        assert server._ui_attack_respond({"choice": "tank"})["success"]

    # 给乙（攻击方）2 次必中，再让它打甲
    engine.combat.grant_bizhong(engine.state.enemies[0], 2)
    assert server._ui_attack_prepare({"actor_ref": "enemy:0"})["success"]
    assert server._ui_attack_declare({"target_ref": "player:0", "damage": 1})["success"]
    cur = server._attack_view(engine)["current"]
    assert cur["item_locked"] is True and cur["can_use_item"] is False
    assert cur["dodge_locked"] is True
    refused = server._ui_attack_respond({"choice": "use_item", "item_name": "备用血泵"})
    assert refused["success"] is False
    assert "必中" in refused["error"] and "被动行动" in refused["error"]
    assert engine.state.pending_attack
    _finish_this_hand()
