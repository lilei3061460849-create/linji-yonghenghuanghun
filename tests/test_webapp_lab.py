"""自定义角色演武场（webapp/lab.py）的服务端回归。

演武场是「把用户选择翻译成一次合法开局」的薄层，这里钉住四件事：

1. 可选项只来自运行时（道纹必须已注册、消耗品必须有登记规格）；
2. 规则模式仍受 25 点预算与偶数档位约束；
3. 自由模式直接写三个上限，但只影响本局；
4. 指定对手走真实地图遭遇 → battle_start，敌人就是用户选的那几只。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.daowen import DaoWenEngine
from webapp import lab


def test_options_only_expose_runtime_entries():
    catalog = lab.options(refresh=True)
    assert [region["name"] for region in catalog["regions"]]
    assert all(region["monsters"] for region in catalog["regions"])

    runtime = set(DaoWenEngine.list_all())
    offered = [name for group in catalog["daowen_groups"] for name in group["names"]]
    assert offered and set(offered) <= runtime
    # 每条道纹只出现一次，界面勾选才不会被重复计数。
    assert len(offered) == len(set(offered))

    names = [item["name"] for item in catalog["consumables"]]
    assert len(names) == len(set(names))
    assert {"急救箱", "定命丸", "血翼"} <= set(names)


def test_rule_mode_custom_loadout_opens_a_real_battle():
    prepared = lab.start({
        "name": "试刀人", "region": "罪孽都市", "mode": "rule",
        "blood_points": 15, "speed_points": 6, "mana_points": 4,
        "relics": ["叠鳞甲"], "daowen": ["杀伐", "再生", "操纵"],
        "consumables": ["急救箱", {"name": "血翼", "uses": 5}],
        "shards": 300, "energy": 3, "enemies": ["毒枭", "打手"],
        "width": 11, "height": 11, "seed": 42, "start_battle": True,
    })
    engine = prepared["engine"]
    player = engine.state.player
    assert (player.blood_limit, player.speed_limit, player.mana_limit) == (90, 3, 2)
    assert {"杀伐", "再生", "操纵"} <= set(player.dao_wen)
    assert [relic.name for relic in engine.state.relics] == ["叠鳞甲"]
    assert {item.name: item.current_uses for item in engine.state.consumables} == {
        "急救箱": 2, "血翼": 5}
    assert engine.state.shards == 300
    assert engine.state.phase == "in_combat"
    assert engine.state.combat_subphase == "await_round_start"
    assert [enemy.name for enemy in engine.state.enemies] == ["毒枭", "打手"]


def test_free_mode_writes_limits_directly():
    prepared = lab.start({
        "name": "自由人", "region": "乱葬岗", "mode": "free",
        "blood_limit": 300, "speed_limit": 9, "mana_limit": 12,
        "daowen": ["杀伐"], "start_battle": False, "seed": 7,
    })
    player = prepared["engine"].state.player
    assert (player.blood_limit, player.current_hp) == (300, 300)
    assert (player.speed_limit, player.current_speed) == (9, 9)
    assert (player.mana_limit, player.current_mana) == (12, 12)
    assert prepared["engine"].state.phase == "pre_battle"


def test_random_battle_uses_engine_spawn_recipe():
    prepared = lab.start({
        "name": "随机客", "region": "龙心谷", "mode": "rule",
        "blood_points": 25, "speed_points": 0, "mana_points": 0,
        "start_battle": True, "seed": 3,
    })
    engine = prepared["engine"]
    assert engine.state.phase == "in_combat"
    assert engine.state.enemies, "随机开战必须真的抽出怪物"
    pool = {monster["name"] for monster in engine.monster_pool["龙心谷"]}
    assert {enemy.name for enemy in engine.state.enemies} <= pool


@pytest.mark.parametrize("payload, message", [
    ({"region": "罪孽都市", "blood_points": 20, "speed_points": 4, "mana_points": 2}, "25"),
    ({"region": "罪孽都市", "blood_points": 20, "speed_points": 3, "mana_points": 2}, "偶数"),
    ({"region": "罪孽都市", "blood_points": 20, "speed_points": 4, "mana_points": 0,
      "daowen": ["不存在的道纹"]}, "道纹"),
    ({"region": "罪孽都市", "blood_points": 20, "speed_points": 4, "mana_points": 0,
      "consumables": ["不存在的道具"]}, "消耗品"),
    ({"region": "罪孽都市", "blood_points": 20, "speed_points": 4, "mana_points": 0,
      "start_battle": True, "enemies": ["不存在的怪物"]}, "怪物"),
])
def test_bad_payloads_are_rejected_with_readable_errors(payload, message):
    with pytest.raises(ValueError) as caught:
        lab.start(payload)
    assert message in str(caught.value)


# ---------------------------------------------------------------- 角色卡 + A vs B


@pytest.fixture
def roster_file(tmp_path, monkeypatch):
    """角色卡写到临时文件，绝不碰仓库里的 data/player_characters.json。"""
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


def test_character_cards_can_be_saved_updated_and_deleted(roster_file):
    saved = lab.save_sheet(_sheet("甲", relics=["叠鳞甲"], consumables=["急救箱"]))
    assert roster_file.exists()
    assert saved["id"] and saved["name"] == "甲"
    assert (saved["blood_limit"], saved["speed_limit"], saved["mana_limit"]) == (90, 3, 2)
    assert saved["consumables"] == [{"name": "急救箱", "uses": 2}]

    updated = lab.save_sheet({**saved, "name": "甲改"})
    assert updated["id"] == saved["id"]
    assert [item["name"] for item in lab.list_sheets()] == ["甲改"]

    assert lab.delete_sheet(saved["id"]) is True
    assert lab.list_sheets() == []
    assert lab.delete_sheet(saved["id"]) is False


def test_duel_character_vs_character_builds_a_real_rival(roster_file):
    a = lab.save_sheet(_sheet("甲"))
    b = lab.save_sheet(_sheet(
        "乙", mode="free", blood_limit=120, speed_limit=4, mana_limit=6,
        daowen=["庇护", "操纵"], relics=["缓厄沙漏"],
        consumables=[{"name": "定命丸", "uses": 1}], resonance="反转"))
    prepared = lab.duel({"a": {"id": a["id"]}, "b": {"id": b["id"]},
                         "region": "罪孽都市", "width": 11, "height": 11, "seed": 5})
    engine = prepared["engine"]
    assert engine.state.phase == "in_combat"
    assert engine.state.combat_subphase == "await_round_start"
    rival = engine.state.enemies[0]
    assert rival.entity_type == "轮回者"
    assert (rival.blood_limit, rival.speed_limit, rival.mana_limit) == (120, 4, 6)
    assert (rival.current_hp, rival.current_speed, rival.current_mana) == (120, 4, 6)
    assert {"庇护", "操纵"} <= set(rival.dao_wen)
    assert rival.resonance == {"反转": 1}
    assert [relic.name for relic in rival.relics] == ["缓厄沙漏"]
    assert [item.name for item in rival.consumables] == ["定命丸"]


def test_duel_rejects_a_missing_character_card(roster_file):
    a = lab.save_sheet(_sheet("甲"))
    with pytest.raises(ValueError) as caught:
        lab.duel({"a": {"id": a["id"]}, "b": {"id": "does-not-exist"},
                  "region": "罪孽都市"})
    assert "B方" in str(caught.value)


def test_duel_monster_side_still_uses_engine_monster_panels(roster_file):
    a = lab.save_sheet(_sheet("甲"))
    prepared = lab.duel({"a": {"id": a["id"]},
                         "b": {"kind": "monster", "monsters": ["毒枭", "打手"]},
                         "region": "罪孽都市", "width": 11, "seed": 6})
    assert [enemy.name for enemy in prepared["engine"].state.enemies] == ["毒枭", "打手"]
