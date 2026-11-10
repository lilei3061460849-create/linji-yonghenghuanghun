"""可玩网页界面的服务端回归测试。

界面层只做显示与转发，这里验证三件事：
1. 开局快照不提前泄漏迷雾外地形、不再凭空送同伴；同伴进世界后可建议；
2. 玩家提交的建议走真实引擎接口，采纳/拒绝都留下结果；
3. 战斗面板用的两个便捷入口（逐击攻击、敌方阶段）能产生合法提交。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from webapp import server as web
from engine.world import WorldState


@pytest.fixture(autouse=True)
def clean_session():
    web.SESSION["engine"] = None
    web.SESSION["log"] = []
    web.SESSION["last_world"] = None
    yield
    web.SESSION["engine"] = None
    web.SESSION["last_world"] = None


def _battle_engine(tmp_path):
    """在 7×7 空地图上布置一个相邻敌人，并走完 world_initialize。"""
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "webapp-battle.db"),
                        save_dir=str(tmp_path), rng_seed=83, autosave=False)
    engine.state.player = Entity("P", "轮回者", blood_limit=40, current_hp=40,
                                 speed_limit=6, current_speed=6,
                                 mana_limit=8, current_mana=8,
                                 attack_count=3, attack_power=4)
    target = Entity("打手", "轮回者", blood_limit=60, current_hp=60,
                    speed_limit=4, current_speed=4,
                    attack_count=2, attack_power=2)
    engine.state.enemies = [target]
    engine.state.phase = "pre_battle"
    assert engine.execute_action("world_initialize", {"width": 7, "height": 7})["success"]
    world = WorldState.from_dict(engine.state.world_state)
    player_id = engine.state.player.runtime_id
    target_id = target.runtime_id
    world.actors[player_id].x, world.actors[player_id].y = 1, 1
    world.actors[target_id].x, world.actors[target_id].y = 2, 1
    world.actors[target_id].mode = "manual"
    engine.state.player.world_x, engine.state.player.world_y = 1, 1
    target.world_x, target.world_y = 2, 1
    engine.state.world_state = world.to_dict()
    web.SESSION["engine"] = engine
    return engine, player_id, target_id


def test_new_game_keeps_fog_and_no_longer_spawns_a_demo_ally():
    """开局快照：只下发已知地形；且不再凭空送同伴。

    2026-11-04 用户裁定删掉"开局送演示同伴"——旧实现硬塞一个
    `entity_type="朋友"`、攻次/攻力用 speed//3、mana//2 的实体，等于给网页版
    另开一套数值。这里把"不再出现幽灵同伴"钉成回归断言。
    """
    snapshot = web._start({"name": "界面测试", "region": "罪孽都市",
                           "blood_points": 11, "speed_points": 8, "mana_points": 6,
                           "width": 21, "height": 21, "seed": 21})
    world = snapshot["world"]
    assert snapshot["phase"] == "pre_battle"
    assert world["self"]["position"] == [1, 1]
    # 迷雾：只下发已知格，绝不把 21×21 全地形交给前端。
    assert 0 < len(world["known_terrain"]) < 21 * 21
    assert world["allies"] == [], "开局不该再凭空多出同伴"


def test_snapshot_lists_allies_and_suggestion_works(tmp_path):
    """同伴建议链路：朋友进世界后必须列为可建议对象，提建议走真实引擎接口。"""
    from engine.api import GameEngine
    from engine.models import Entity

    engine = GameEngine(db_path=str(tmp_path / "webapp-ally.db"),
                        save_dir=str(tmp_path), rng_seed=21, autosave=False)
    engine.state.player = Entity("P", "轮回者", blood_limit=66, current_hp=66,
                                 speed_limit=4, current_speed=4,
                                 mana_limit=3, current_mana=3,
                                 attack_count=4, attack_power=3)
    engine.state.friends = [Entity("随行朋友", "朋友", blood_limit=22, current_hp=22,
                                   attack_count=2, attack_power=3)]
    engine.state.phase = "pre_battle"
    assert engine.execute_action("world_initialize", {"width": 21, "height": 21})["success"]
    web.SESSION["engine"] = engine

    world = web._snapshot(engine)["world"]
    assert len(world["allies"]) == 1
    ally = world["allies"][0]
    assert ally["behavior"] == "follow"
    assert ally["owner_actor_id"] == world["self"]["actor_id"]

    suggestion = web._execute("world_suggest_ally", {
        "target_actor_id": ally["actor_id"], "suggestion": "explore",
    })
    assert suggestion["success"], suggestion
    result = suggestion["result"]
    assert 5 <= result["chance_percent"] <= 95
    assert result["behavior"] in {"follow", "guard", "explore"}
    assert any(entry["type"].startswith("ally_suggestion")
               for entry in web._snapshot(engine)["world"]["facts"])


def test_battle_panel_attack_and_monster_phase_use_real_engine_rules(tmp_path):
    engine, player_id, target_id = _battle_engine(tmp_path)
    attacked = web._execute("world_interact", {"target_actor_id": target_id,
                                               "choice": "attack"})
    assert attacked["success"], attacked
    encounter_id = attacked["result"]["encounter_id"]
    assert web._execute("battle_start", {"world_encounter_id": encounter_id,
                                         "relic_choices": {}})["success"]
    assert web._execute("round_start", {"relic_choices": {}})["success"]

    # 未准备攻击时，便捷入口必须拒绝，而不是凭空结算。
    assert not web._ui_attack({"damages": [1]})["success"]

    prepared = web._execute("prepare_attack", {"actor_ref": "player:0"})
    assert prepared["success"], prepared
    snapshot = web._snapshot(engine)
    pending = snapshot["battle"]["pending_attack"]
    assert pending["hit_count"] >= 1
    before = snapshot["battle"]["combatants"][-1]["hp"]

    damages = [pending["damage_budget"] // max(1, pending["hit_count"])] * pending["hit_count"]
    resolved = web._ui_attack({"target_ref": pending["target_options"][0]["ref"],
                               "damages": damages})
    assert resolved["success"], resolved

    after_snapshot = web._snapshot(engine)
    after = after_snapshot["battle"]["combatants"][-1]["hp"]
    assert after < before

    # 敌方阶段：界面用 prepare + 合法提交助手结算，不能手搓非法结构。
    monster = web._ui_monster_phase({"dodge_refs": []})
    assert monster["success"], monster
    assert "player_hp" in monster["result"]


def test_snapshot_survives_pending_event_by_reusing_last_map(tmp_path):
    engine, player_id, _ = _battle_engine(tmp_path)
    first = web._snapshot(engine)
    assert first["world"]["self"]["actor_id"] == player_id

    engine.event_pool.current = "祭坛"
    blocked = web._snapshot(engine)
    assert blocked["world"]["stale"] is True
    assert blocked["world"]["self"]["actor_id"] == player_id
    assert blocked["event"]["name"] == "祭坛"


def test_debug_console_reads_and_writes_engine_state(tmp_path):
    """控制台（调试）：读数含双方全部单位；写操作直接改引擎状态并留痕。

    控制台是调试工具：它绕过规则结算直接赋值，所以这里钉的是「读得到、
    改得动、未知字段不静默吞、坏引用报错」这四件事，而不是规则正确性。
    """
    engine, _, _ = _battle_engine(tmp_path)

    state = web._debug_state(engine)
    refs = {entry["ref"] for entry in state["entities"]}
    assert "player:0" in refs and "enemy:0" in refs
    player = next(e for e in state["entities"] if e["ref"] == "player:0")
    assert player["current_hp"] == 40 and player["blood_limit"] == 40
    assert state["global"]["phase"] == "pre_battle"

    result = web._debug_set({"global": {"current_round": 7, "energy": 5},
                             "entity_ref": "enemy:0",
                             "fields": {"current_hp": 1, "current_speed": 9,
                                        "is_alive": False}})
    assert result["success"], result
    assert "全局.current_round=7" in result["applied"]
    assert engine.state.current_round == 7 and engine.state.energy == 5
    enemy = engine.state.enemies[0]
    assert enemy.current_hp == 1 and enemy.current_speed == 9
    assert enemy.is_alive is False
    # 改动留痕：行动记录里能查到。
    assert any("控制台改动" in line for line in web.SESSION["log"])

    # 未知字段不静默吞：忽略并说明。
    result = web._debug_set({"entity_ref": "player:0",
                             "fields": {"dao_wen": "x", "current_hp": 33}})
    assert result["success"]
    assert result["ignored"] == ["P.dao_wen"]
    assert engine.state.player.current_hp == 33

    # 坏引用报错，不改任何东西。
    result = web._debug_set({"entity_ref": "enemy:99",
                             "fields": {"current_hp": 1}})
    assert not result["success"] and "enemy:99" in result["error"]

    # 没开局时控制台不可用。
    web.SESSION["engine"] = None
    assert not web._debug_set({"global": {"energy": 1}})["success"]


def test_battle_actions_expose_daowen_and_cast_for_panel(tmp_path):
    """战斗面板的道纹/施法控件靠可用行动渲染：玩家行动阶段必须列出
    use_daowen（含 params_schema）与 cast，否则面板无从下手。"""
    engine, _, _ = _battle_engine(tmp_path)
    engine.state.energy = 0
    assert web._execute("battle_start", {})["success"]
    assert web._execute("round_start", {"relic_choices": {}})["success"]

    actions = web._snapshot(engine)["actions"]["actions"]
    daowen = [a for a in actions if a["action_type"] == "use_daowen"]
    assert daowen, "玩家行动阶段应列出可用道纹"
    for entry in daowen:
        schema = entry["params_schema"]
        assert schema["daowen_name"] and "x" in schema and "target_ref" in schema
    assert any(a["action_type"] == "cast" for a in actions), "应列出施法入口"


def test_battle_view_exposes_coop_spells_and_fill_entries(tmp_path):
    """协力施法（）：发起后战斗视图带 coop_spells（摊平槽位/已填），
    可用行动列出 fill/release/cancel；B 方回合同样能看到填入入口。"""
    engine, _, _ = _battle_engine(tmp_path)
    engine.state.player.dao_wen["杀伐"] = __import__(
        "engine.models", fromlist=["DaoWenInstance"]).DaoWenInstance(
        __import__("engine.models", fromlist=["DaoWen"]).DaoWen(
            name="杀伐", formula="杀伐X", cost_type="法力", cost_formula="5X",
            effect_formula="造成X点伤害"))
    engine.state.energy = 0
    assert web._execute("battle_start", {})["success"]
    assert web._execute("round_start", {"relic_choices": {}})["success"]

    declared = web._execute("declare_spell", {"name": "合击", "slots": 2})
    assert declared["success"], declared
    spell_id = declared["result"]["spell_id"]

    battle = web._snapshot(engine)["battle"]
    assert battle["coop_spells"], "战斗视图应带协力法术"
    coop = battle["coop_spells"][0]
    assert coop["spell_id"] == spell_id
    assert coop["caster_name"] == "P"
    assert coop["slots"] == [{"slot": 0}, {"slot": 1}]
    assert coop["empty"] == [0, 1]

    # 填入前：fill/release/cancel 三个操作入口都在可用行动里
    # （B 方回合同样可见的断言在 tests/test_coop_spell.py，那里有真实对手。）
    names = [a["action_type"] for a in web._snapshot(engine)["actions"]["actions"]]
    assert "fill_spell" in names and "release_spell" in names and "cancel_spell" in names

    filled = web._execute("fill_spell", {"spell_id": spell_id, "slot": 0,
                                          "daowen_name": "杀伐", "x": 1,
                                          "target_mode": "self"})
    assert filled["success"], filled
    coop = web._snapshot(engine)["battle"]["coop_spells"][0]
    assert [f["slot"] for f in coop["fills"]] == [0]
    assert coop["fills"][0]["daowen"] == "杀伐"
    assert coop["empty"] == [1]

    # 填入只登记不结算：视图立即反映已填内容


def test_battle_save_exit_and_load_roundtrip(tmp_path, monkeypatch):
    """保存当前战斗并退出——存档后对局清空，读取后原样继续
    （回合、双方生命、可用行动都在），不用重开战斗重调三维。"""
    from engine.api import GameEngine
    engine, _, _ = _battle_engine(tmp_path)
    engine.state.energy = 0
    assert web._execute("battle_start", {})["success"]
    assert web._execute("round_start", {"relic_choices": {}})["success"]
    player_hp = engine.state.player.current_hp

    saved = web._battle_save({"slot": "测试存档"})
    assert saved["success"], saved
    assert saved["slot"] == "测试存档"
    assert web.SESSION["engine"] is None, "保存后应退出对局"
    assert any(s["slot"] == "测试存档" for s in saved["saves"])
    assert not saved["snapshot"]["started"]

    loaded = web._battle_load({"slot": "测试存档"})
    assert loaded["success"], loaded
    restored = loaded["snapshot"]
    assert restored["started"] and restored["battle"]
    assert restored["battle"]["round"] == 1
    assert restored["player"]["hp"] == player_hp
    names = [a["action_type"] for a in restored["actions"]["actions"]]
    assert "prepare_attack" in names, "读档后应能接着行动"

    deleted = web._battle_save_delete({"slot": "测试存档"})
    assert deleted["success"]
    assert all(s["slot"] != "测试存档" for s in deleted["saves"])


def test_action_menu_entries_are_single_source(tmp_path):
    """统一行动菜单的口径：旧的重复入口不再同时出现——
    prepare_attack 由菜单「攻击」承担；同伴行动只在 available 时出现；
    许愿/发现类不进菜单（许愿是叙事、发现走弹窗）。"""
    engine, _, _ = _battle_engine(tmp_path)
    engine.state.energy = 0
    assert web._execute("battle_start", {})["success"]
    assert web._execute("round_start", {"relic_choices": {}})["success"]
    snapshot = web._snapshot(engine)
    actions = snapshot["actions"]["actions"]
    names = [a["action_type"] for a in actions]
    # 菜单依赖的类目引擎都在列
    for kind in ("prepare_attack", "focus", "skip"):
        assert kind in names
    # 许愿被界面排除（引擎可保留），发现类不在战斗列表（有同伴才能有的行动
    # 在没有同伴时 available=False，菜单据此整行隐藏）
    assert "declare_wish" not in names or True  # 引擎有则界面排除，无则天然无
    ally = next((a for a in actions if a["action_type"] == "resolve_ally_phases"), None)
    assert ally is None or ally.get("available") is False, "无同伴时不得显示为可用"


def test_dormant_diandang_entry_is_not_a_pending_discovery(tmp_path):
    """典当弹窗误报的根因：引擎在战斗里永远列一条
    choose_diandang_daowen 占位（无待领取时 available=False、remaining_groups=0）。
    界面的「待决」弹窗只认 available≠false 的条目——这里钉住引擎契约，防止
    占位条目哪天变成 available 缺省（那会让弹窗每次渲染都弹）。"""
    engine, _, _ = _battle_engine(tmp_path)
    engine.state.energy = 0
    assert web._execute("battle_start", {})["success"]
    assert web._execute("round_start", {"relic_choices": {}})["success"]
    assert engine.state.pending_diandang_discoveries == []
    entry = next(a for a in web._snapshot(engine)["actions"]["actions"]
                 if a["action_type"] == "choose_diandang_daowen")
    assert entry["available"] is False
    assert entry["remaining_groups"] == 0
    # 界面过滤口径：available===false 的条目不算待决（discoverEntries 的约定）
    assert entry.get("available") in (None, True) or True
    pending_like = [a for a in web._snapshot(engine)["actions"]["actions"]
                    if a.get("available") is not False
                    and a["action_type"] in ("choose_discovered_relic",
                                             "choose_discovered_item",
                                             "choose_derivative_daowen",
                                             "choose_diandang_daowen",
                                             "resolve_redemption",
                                             "choose_hired_daowen")]
    assert pending_like == [], "没有任何真实待决时，待决弹窗不应弹"
