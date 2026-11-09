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

    # 余下全部硬扛，一次结算
    done = server._ui_attack_auto({})
    assert done["success"], done
    hits = done["result"]["hits"]
    assert len(hits) == hit_count
    assert hits[0]["damage_dealt"] == 0          # 闪避掉了
    assert hits[1]["damage_dealt"] > 0           # 硬扛吃满
    assert not engine.state.pending_attack
    assert server.SESSION["attack"] is None


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
