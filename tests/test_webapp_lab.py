"""自定义角色演武场（webapp/lab.py）的服务端回归。

演武场是「把用户选择翻译成一次合法开局」的薄层，这里钉住四件事：

1. 可选项只来自运行时（道纹必须已注册、消耗品必须有登记规格）；
2. 规则模式仍受 25 点预算约束（法限 2 点一档，速限已放开为 1 点一档）；
3. 自由模式直接写三个上限，但只影响本局；
4. 指定对手随 battle_start 注入（`inject_enemies`），不建地图，敌人就是用户选的那几只。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.daowen import DaoWenEngine
from webapp import lab


def test_derivative_daowen_can_go_straight_into_a_character_sheet():
    """2026-11-09 用户令：衍生道纹也能像普通道纹一样直接放进角色卡的道纹栏。

    角色卡是局外配装，不该被「局内 0 出手衍生」这一条获得途径锁死——衍生道纹
    既是母本的收益置换，也是怪物去同质化的素材，必须先能在卡里选到。
    """
    from webapp import lab

    groups = {g["label"]: g["names"] for g in lab.options(refresh=True)["daowen_groups"]}
    label = next(name for name in groups if name.startswith("衍生道纹"))
    assert set(groups[label]) >= {"连击", "虚耗", "断脉", "献锋", "搏命"}

    sheet = lab.normalize_sheet({
        "name": "衍生测试", "mode": "free", "blood_limit": 40,
        "speed_limit": 6, "mana_limit": 8, "resonance": "转换",
        "daowen": ["杀伐", "连击", "断脉"],
    })
    assert sheet["daowen"] == ["杀伐", "连击", "断脉"]


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
    assert (player.blood_limit, player.speed_limit, player.mana_limit) == (90, 6, 2)
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
    ({"region": "罪孽都市", "blood_points": 20, "speed_points": 4, "mana_points": 1}, "偶数"),
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
    assert (saved["blood_limit"], saved["speed_limit"], saved["mana_limit"]) == (90, 6, 2)
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


def test_duel_b_side_relics_use_the_engine_opponent_channel(roster_file):
    """B 方遗物必须进 opponent_relics，否则 side_has/_relic_active 读不到，只是摆设。"""
    a = lab.save_sheet(_sheet("甲"))
    b = lab.save_sheet(_sheet(
        "乙", mode="free", blood_limit=30, speed_limit=1, mana_limit=1,
        relics=["叠鳞甲", "千荆甲", "避风铃"]))
    prepared = lab.duel({"a": {"id": a["id"]}, "b": {"id": b["id"]},
                         "region": "罪孽都市", "width": 11, "seed": 5})
    engine = prepared["engine"]
    rival = engine.state.enemies[0]
    assert [relic.name for relic in engine.state.opponent_relics] == ["叠鳞甲", "千荆甲", "避风铃"]
    # 启用槽位与 A 方同口径：ceil(30/30)=1，后面两件照常持有但停用。
    assert [relic.enabled for relic in engine.state.opponent_relics] == [True, False, False]
    assert engine.state.side_has(rival, "叠鳞甲")
    assert engine.state._relic_active(rival, "叠鳞甲")
    assert not engine.state._relic_active(rival, "千荆甲")


def test_duel_b_side_relic_really_changes_combat_numbers(roster_file):
    """叠鳞甲（受到伤害-1）在 B 方身上必须真的减伤，而不是只做展示。"""
    a = lab.save_sheet(_sheet("甲"))
    with_relic = lab.save_sheet(_sheet("乙", relics=["叠鳞甲"]))
    without = lab.save_sheet(_sheet("丙"))

    def _hit(b_id):
        engine = lab.duel({"a": {"id": a["id"]}, "b": {"id": b_id},
                           "region": "罪孽都市", "width": 11, "seed": 5})["engine"]
        rival = engine.state.enemies[0]
        before = rival.current_hp
        engine.combat._apply_hostile_damage(rival, 10, "普通", engine.state.player)
        return before - rival.current_hp

    assert _hit(with_relic["id"]) == 9
    assert _hit(without["id"]) == 10


def test_daowen_effects_come_from_the_index_and_cover_runtime_daowen():
    """界面「点一下看条文」：76 条运行时道纹都要有索引条文，不许只剩名字。"""
    catalog = lab.options(refresh=True)
    effects = catalog["daowen_effects"]
    runtime = set(DaoWenEngine.list_all())
    assert runtime - set(effects) == set()
    assert "法力" in effects["杀伐"]
    # 每条都带支付方式或效果动词，不是空串/标题残片
    assert all(len(text) >= 6 for text in effects.values())


def test_character_sheet_no_longer_requires_energy(roster_file):
    """精力是局外资源，角色卡不再要求它；缺省按 0，引擎侧仍保留该字段。"""
    card = _sheet("无精力")
    card.pop("energy", None)          # 旧卡默认带 energy，这里模拟界面不再提交它
    saved = lab.save_sheet(card)
    assert saved.get("energy", 0) == 0
    engine = lab.start({"id": saved["id"], "region": "罪孽都市", "mode": "rule",
                        "blood_points": 15, "speed_points": 6, "mana_points": 4,
                        "daowen": ["杀伐"], "relics": [], "consumables": []})["engine"]
    assert engine.state.energy == 0


# ---------------------------------------------------------------- 随机三维


def test_roll_limits_stays_inside_the_requested_window():
    """NPC/造物懒得想数值：随机三维必须严格落在用户给的下限~上限之间。"""
    ranges = {"blood": [40, 60], "speed": [2, 4], "mana": [1, 3]}
    for seed in range(20):
        rolled = lab.roll_limits({"ranges": ranges, "seed": seed})
        assert 40 <= rolled["blood_limit"] <= 60
        assert 2 <= rolled["speed_limit"] <= 4
        assert 1 <= rolled["mana_limit"] <= 3
        assert rolled["roll"] == ranges


def test_roll_limits_is_reproducible_with_a_seed():
    a = lab.roll_limits({"ranges": {"blood": [30, 300], "speed": [1, 9], "mana": [1, 9]},
                         "seed": "abc"})
    b = lab.roll_limits({"ranges": {"blood": [30, 300], "speed": [1, 9], "mana": [1, 9]},
                         "seed": "abc"})
    assert (a["blood_limit"], a["speed_limit"], a["mana_limit"]) == \
           (b["blood_limit"], b["speed_limit"], b["mana_limit"])


def test_roll_limits_rejects_inverted_or_out_of_range_windows():
    with pytest.raises(ValueError):
        lab.roll_limits({"ranges": {"blood": [80, 20]}})
    with pytest.raises(ValueError):
        lab.roll_limits({"ranges": {"speed": [0, 120]}})   # 速限硬上限 99
    with pytest.raises(ValueError):
        lab.roll_limits({"ranges": {"blood": [0, 30]}})    # 血限至少 1


def test_random_card_keeps_the_window_and_saves_as_free_mode(roster_file):
    rolled = lab.roll_limits({"ranges": {"blood": [40, 50], "speed": [2, 3], "mana": [1, 2]},
                              "seed": 3})
    saved = lab.save_sheet(_sheet(
        "随机造物", mode="free",
        blood_limit=rolled["blood_limit"], speed_limit=rolled["speed_limit"],
        mana_limit=rolled["mana_limit"], roll=rolled["roll"]))
    assert saved["mode"] == "free"
    assert saved["roll"] == rolled["roll"]
    assert (saved["blood_limit"], saved["speed_limit"], saved["mana_limit"]) == \
           (rolled["blood_limit"], rolled["speed_limit"], rolled["mana_limit"])
    reloaded = next(item for item in lab.list_sheets() if item["id"] == saved["id"])
    assert reloaded["roll"] == rolled["roll"]


def test_duel_runs_directly_without_a_dungeon_map(roster_file):
    """用户令 2026-11-09：不需要副本当地图，对手摆好就直接开打。

    这条钉住「直接对战」的两半：引擎侧 `inject_enemies` 真的把对手摆上台
    （不是被随机出怪顶掉），界面侧 `direct` 标记让前端隐藏地图卡。
    """
    from webapp import server

    a = lab.save_sheet(_sheet("甲"))
    b = lab.save_sheet(_sheet("乙", mode="free", blood_limit=120, speed_limit=4,
                              mana_limit=6))
    # 连副本都不传：直接对战不该为了开一场测试架逼用户选副本。
    prepared = lab.duel({"a": {"id": a["id"]}, "b": {"id": b["id"]},
                         "manual_both": True, "seed": 5})
    engine = prepared["engine"]

    assert engine.state.phase == "in_combat"
    assert not engine.state.world_state          # 没有地图
    assert engine.state.world_encounter_id in (None, "")
    assert [enemy.name for enemy in engine.state.enemies] == ["乙"]
    assert [enemy.name for enemy in engine.state.enemies] == ["乙"]  # 没被随机出怪替换
    assert engine.state.enemies[0].entity_type == "轮回者"

    view = server._battle_view(engine)
    assert view["direct"] is True
    assert server._snapshot(engine)["direct"] is True


def test_inject_enemies_guards_are_explicit(roster_file):
    """`inject_enemies` 只给演武场用：死单位、和地图遭遇同时用都必须报错。"""
    from engine.monsters import make_monster_entity

    a = lab.save_sheet(_sheet("甲"))
    prepared = lab.start({"name": "甲", "region": "罪孽都市", "mode": "free",
                          "blood_limit": 90, "speed_limit": 3, "mana_limit": 2,
                          "seed": 9})
    engine = prepared["engine"]

    dead = make_monster_entity(engine.monster_pool["罪孽都市"][0])
    dead.is_alive = False
    failed = engine.execute_action("battle_start", {"inject_enemies": [dead]})
    assert failed["success"] is False
    assert "已死亡" in failed["error"]

    alive = make_monster_entity(engine.monster_pool["罪孽都市"][0])
    failed = engine.execute_action("battle_start", {
        "inject_enemies": [alive], "world_encounter_id": "fake-encounter"})
    assert failed["success"] is False
    assert "world_encounter_id" in failed["error"]


def test_duel_first_move_uses_the_engine_priority_rule(roster_file):
    """A/B 必须对称：先手按 速限→法限→血限→当前生命 判定，不能默认 A 先手。

    这条同时钉住方向性——把两张卡对调，先手方必须跟着对调。
    """
    weak = lab.save_sheet(_sheet("弱", mode="free", blood_limit=30,
                                 speed_limit=3, mana_limit=2))
    strong = lab.save_sheet(_sheet("强", mode="free", blood_limit=120,
                                   speed_limit=8, mana_limit=8))

    def first_mover(a_id, b_id):
        engine = lab.duel({"a": {"id": a_id}, "b": {"id": b_id},
                           "manual_both": True, "seed": 7})["engine"]
        engine.execute_action("round_start", {"relic_choices": {}})
        return engine.state.duel_turn

    assert first_mover(weak["id"], strong["id"]) == "opponent_side"
    assert first_mover(strong["id"], weak["id"]) == "player_side"


def test_duel_priority_tie_is_random_then_alternates(roster_file):
    """四项全同不能固定成 A 先：随机首手并记下，之后每次平局交替。

    直接比对 CombatEngine 的判定：随机开局可能给玩家侧发【灾厄轮盘】等遗物，
    回始会改当前生命，真跑一整回合反而造不出「四项全同」的稳定场景。
    """
    left = lab.save_sheet(_sheet("同甲", mode="free", blood_limit=60,
                                 speed_limit=5, mana_limit=5))
    right = lab.save_sheet(_sheet("同乙", mode="free", blood_limit=60,
                                  speed_limit=5, mana_limit=5))
    engine = lab.duel({"a": {"id": left["id"]}, "b": {"id": right["id"]},
                       "manual_both": True, "seed": 11})["engine"]
    player, rival = engine.state.player, engine.state.enemies[0]
    rival.current_hp = player.current_hp = 60
    player.blood_limit = rival.blood_limit = 60
    player.speed_limit = rival.speed_limit = 5
    player.mana_limit = rival.mana_limit = 5

    first = engine.combat._stats_priority_side()
    assert first == engine.state.event_modifiers["duel_priority_tie_first"]
    assert first in {"player_side", "opponent_side"}
    second = engine.combat._stats_priority_side()
    assert second != first                     # 下一次平局交给另一边
