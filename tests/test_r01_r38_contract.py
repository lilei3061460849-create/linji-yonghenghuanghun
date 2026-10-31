"""R01-R38 裁定契约：每组覆盖正常、边界与非法输入。"""
from __future__ import annotations

import json
import os
import sys

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine, TWISTED_TOOL_LIBRARY
from engine.combat import CombatEngine
from engine.daowen import DaoWenEngine
from engine.dice import DiceEngine
from engine.models import Consumable, DaoWen, DaoWenInstance, Entity, GameState, Relic, StatusEffect
from sim.optional_actions import round_start_relic_choices


def _engine(tmp_path, seed: int = 7) -> GameEngine:
    return GameEngine(
        db_path=str(tmp_path / "r.db"),
        save_dir=str(tmp_path / "saves"),
        sealed_candidate_path=str(tmp_path / "sealed.json"),
        death_book_path=str(tmp_path / "death.md"),
        rng_seed=seed,
    )


def _full_setup(engine: GameEngine, region: str = "罪孽都市") -> GameEngine:
    assert engine.execute_action("setup_attributes", {
        "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })["success"]
    # 新开局流程：finish_initial_daowen 会先显式选择开局遗物，再选初始道纹
    assert finish_initial_daowen(engine)["success"]
    assert "杀伐" in engine.state.player.dao_wen
    assert len(engine.state.relics) == 1
    assert engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})["success"]
    result = engine.execute_action("setup_choose_region", {"region": region})
    assert result["success"]
    return engine


def _controlled_combat(engine: GameEngine) -> tuple[Entity, Entity]:
    player = Entity("轮回者", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=100, current_mana=100, speed_limit=9, current_speed=9,
                    attack_count=1, attack_power=2)
    monster = Entity("甲怪", "怪物", blood_limit=100, current_hp=100,
                     attack_count=2, attack_power=4)
    engine.state.player = player
    engine.state.enemies = [monster]
    engine.state.phase = "in_combat"
    engine.state.current_round = 1
    return player, monster


def _dw(entity: Entity, name: str, x: int = 1) -> None:
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""),
        x_value=x,
    )


# R01-R10：阶段、目标、员工/还债与雕塑

def test_r01_r10_normal_setup_debt_and_zero_attack_employee(tmp_path):
    engine = _full_setup(_engine(tmp_path))
    # R05：20点全投血限，攻击次数允许为0。
    result = engine.execute_action("pre_battle_action", {
        "sub_action": "雇佣", "name": "守门人", "blood_alloc": 20, "atk_bundles": 0,
    })
    assert result["success"]
    assert engine.state.employees[-1].attack_count == 0
    choices = result["result"]["discovered_daowen_choices"]
    assert engine.execute_action("choose_hired_daowen", {
        "name": "守门人", "daowen": choices[0],
    })["success"]

    debtor = Entity("欠债者", "员工", blood_limit=30, current_hp=30,
                    attack_count=1, attack_power=1, shards=-10,
                    is_debt_bound=True, is_deployed=True)
    engine.state.employees.append(debtor)
    before_blacklist = engine.state.blacklist_level
    engine.state.shards = 10
    paid = engine.execute_action("repay_debt_employee", {"name": "欠债者"})
    assert paid["success"] and paid["result"]["departed"]
    assert engine.state.blacklist_level == before_blacklist


def test_r01_r10_boundary_converted_xiaozai_is_not_out_of_combat_daowen(tmp_path):
    engine = _full_setup(_engine(tmp_path))
    player = engine.state.player
    _dw(player, "消灾")  # 模拟旧存档残留；不得因此恢复已转换的道纹入口
    engine.state.shards = 10
    retired = engine.execute_action("use_daowen", {"daowen_name": "消灾", "x": 1})
    assert not retired["success"]
    assert engine.state.shards == 10

    _dw(player, "杀伐")
    denied = engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 1})
    # 默认局内，局外须条文明示；逆命筹消耗品不再占用道纹白名单。
    assert not denied["success"] and "局外发动须条文明示" in denied["error"]
    assert engine.state.shards == 10


def test_r01_r10_illegal_phase_and_missing_target_are_atomic(tmp_path):
    engine = _full_setup(_engine(tmp_path))
    assert not engine.execute_action("attack", {"target_selections": []})["success"]
    assert not engine.execute_action("battle_start", {"relic_choices": {}})["success"]

    player, monster = _controlled_combat(engine)
    _dw(player, "杀伐")
    mana, used = player.current_mana, player.actions_used_this_round
    bad = engine.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 2})
    assert not bad["success"] and "显式指定目标" in bad["error"]
    assert engine.state.player.current_mana == mana
    assert engine.state.player.actions_used_this_round == used

    # R10：任意非轮回者均适用雕塑，轮回者不适用。
    # DM裁定 2026-09-10：雕塑触发条件为攻次与攻力**都**为0
    friend = Entity("零攻朋友", "朋友", blood_limit=20, current_hp=20,
                    attack_count=0, attack_power=0)
    engine.state.friends.append(friend)
    settled = engine.combat.settle_victory_paths()
    assert any(x.get("monster") == "零攻朋友" for x in settled)


# R11-R17：两阶段怪物决策与显式可选参数

def test_r11_r17_normal_two_stage_monster_choices(tmp_path):
    engine = _engine(tmp_path)
    player, monster = _controlled_combat(engine)
    friend = Entity("友军", "朋友", blood_limit=40, current_hp=40,
                    speed_limit=2, current_speed=2, attack_count=1, attack_power=1)
    engine.state.friends.append(friend)
    _dw(monster, "杀伐", 2)
    engine.state.current_round = 2

    prepared = engine.execute_action("prepare_monster_phase", {})
    actor = prepared["result"]["actors"][0]
    assert [o["name"] for o in actor["daowen_options"]] == ["杀伐"]
    assert {t["name"] for t in actor["attack_target_options"]} == {"轮回者", "友军"}

    # 单行动契约：一次出手只能是道纹，或一次攻击；「道纹＋攻击」打包提交被拒。
    friend_hp = friend.current_hp
    result = engine.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"],
        "choices": [{
            "actor_ref": "enemy:0",
            "daowen": {"name": "杀伐", "target_ref": "friend:0", "dodge": False, "blood_shadow": False},
            "attack_actions": [],
        }],
    })
    assert result["success"], result.get("error")
    assert friend.current_hp == friend_hp - 4, "杀伐X=2 → 生命-4（不是伤害，闪避与否都直接掉血）"

    # 下一回合改走攻击：每段命中各自带显式闪避选择（玩家闪、友军不闪）。
    assert engine.execute_action("skip", {"actor_ref": "player:0"})["success"]
    assert engine.execute_action("round_end", {})["success"]
    assert engine.execute_action("round_start", {
        "relic_choices": round_start_relic_choices(engine)})["success"]
    assert engine.execute_action("skip", {"actor_ref": "player:0"})["success"]
    prepared2 = engine.execute_action("prepare_monster_phase", {})
    assert prepared2["success"], prepared2.get("error")
    result2 = engine.execute_action("resolve_monster_phase", {
        "token": prepared2["result"]["token"],
        "choices": [{
            "actor_ref": "enemy:0",
            "daowen": None,
            "attack_actions": [{"hits": [
                {"target_ref": "player:0", "dodge": True, "blood_shadow": False,
                 "spell_choices": {"before": {}, "after": {}}},
                {"target_ref": "friend:0", "dodge": False, "blood_shadow": False,
                 "spell_choices": {"before": {}, "after": {}}},
            ]}],
        }],
    })
    assert result2["success"], result2.get("error")
    assert player.current_speed == 8, "闪避一段 → 扣 1 点当前速度"
    assert friend.current_hp < friend_hp - 4, "未闪避的命中照常扣血"


def test_r11_r17_boundary_relic_and_first_aid_require_explicit_choices(tmp_path):
    # 2026-10-13：【三相残韵盘】删除，战始显式决策用剩下的【猩红果实】覆盖。
    # 2026-10-14：【猩红果实】重做为被动，改用【炉心坠】覆盖（须显式提交 heart_name）。
    engine = _full_setup(_engine(tmp_path))
    engine.state.relics = [Relic("炉心坠", "")]
    engine.state.consumables = [
        Consumable("衰老龙心", "", 3, 3, kind="dragon_heart", dragon_heart_type="衰老")]
    engine.state.energy = 0
    missing = engine.execute_action("battle_start", {"relic_choices": {}})
    assert not missing["success"]
    assert engine.state.current_battle == 0
    ok = engine.execute_action("battle_start", {
        "relic_choices": {"炉心坠": {"heart_name": "衰老龙心"}},
    })
    assert ok["success"]
    assert engine.execute_action("round_start", {"relic_choices": {}})["success"]

    engine.state.consumables.append(Consumable(
        "急救箱", TWISTED_TOOL_LIBRARY["急救箱"][1], current_uses=2, max_uses=2,
    ))
    engine.state.player.status_effects = [
        StatusEffect("坏死", -1, 1), StatusEffect("蒙蔽", 2, 1),
    ]
    uses = engine.state.consumables[-1].current_uses
    missing_status = engine.execute_action("consume_item", {"name": "急救箱"})
    assert not missing_status["success"]
    assert engine.state.consumables[-1].current_uses == uses
    chosen = engine.execute_action("consume_item", {"name": "急救箱", "remove_status": "蒙蔽"})
    assert chosen["success"]
    assert engine.state.player.has_status("坏死") and not engine.state.player.has_status("蒙蔽")


def test_r11_r17_illegal_incomplete_monster_submission_rolls_back(tmp_path):
    engine = _engine(tmp_path)
    player, _ = _controlled_combat(engine)
    prepared = engine.execute_action("prepare_monster_phase", {})
    hp = player.current_hp
    bad = engine.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"], "choices": [],
    })
    assert not bad["success"]
    assert engine.state.player.current_hp == hp
    assert engine.state.pending_monster_phase["token"] == prepared["result"]["token"]


def test_r11_r17_aoe_and_control_dodge_are_explicit(tmp_path):
    engine = _engine(tmp_path)
    player, monster = _controlled_combat(engine)
    friend = Entity("友军", "朋友", blood_limit=40, current_hp=40,
                    speed_limit=2, current_speed=2, attack_count=1, attack_power=1)
    engine.state.friends.append(friend)
    _dw(monster, "波及", 2)
    engine.state.current_round = 2
    prepared = engine.execute_action("prepare_monster_phase", {})
    actor = prepared["result"]["actors"][0]
    option = actor["daowen_options"][0]
    assert option["dodge_submission"] == "per_target"
    # 单行动契约：一笔提交只能是道纹，这里不再附带攻击块
    # （dodge_targets 的合法性校验与是否附带攻击无关）。
    attacks = []
    incomplete = [{"actor_ref": "enemy:0",
                   "daowen": {"name": "波及", "dodge": False, "blood_shadow": False},
                   "attack_actions": attacks}]
    hp_before = player.current_hp
    bad = engine.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"], "choices": incomplete,
    })
    assert not bad["success"] and player.current_hp == hp_before
    assert not player.has_status("波及")

    duplicated = [{"actor_ref": "enemy:0",
                   "daowen": {"name": "波及", "dodge": False, "dodge_targets": [
                       {"target_ref": "player:0", "dodge": False, "blood_shadow": False},
                       {"target_ref": "player:0", "dodge": True, "blood_shadow": False},
                   ]},
                   "attack_actions": attacks}]
    duplicate_result = engine.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"], "choices": duplicated,
    })
    assert not duplicate_result["success"] and player.current_hp == hp_before

    complete = [{"actor_ref": "enemy:0",
                 "daowen": {"name": "波及", "dodge": False, "dodge_targets": [
                     {"target_ref": "player:0", "dodge": False, "blood_shadow": False},
                     {"target_ref": "friend:0", "dodge": True, "blood_shadow": False},
                 ]},
                 "attack_actions": attacks}]
    ok = engine.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"], "choices": complete,
    })
    assert ok["success"]
    # 未闪避者被标记；闪避者速度-1且不被标记
    assert player.has_status("波及")
    assert friend.current_hp == 40 and friend.current_speed == 1
    assert not friend.has_status("波及")


def test_r11_r17_impact_dodge_targets_are_bound_to_prepare_snapshot(tmp_path):
    engine = _engine(tmp_path)
    player, monster = _controlled_combat(engine)
    monster.attack_count = 0
    _dw(monster, "波及", 2)          # 面板固定 X=2（2026-10-09 起 X 下限 repealed，X=1 也合法）
    early_friend = Entity("早友军", "朋友", blood_limit=40, current_hp=40,
                          speed_limit=2, current_speed=2, attack_count=1, attack_power=1)
    engine.state.friends.append(early_friend)
    engine.state.current_round = 2
    prepared = engine.execute_action("prepare_monster_phase", {})
    token = prepared["result"]["token"]
    option = prepared["result"]["actors"][0]["daowen_options"][0]
    assert [target["ref"] for target in option["dodge_target_options"]] == ["player:0", "friend:0"]

    # prepare后新增的实体不是本次快照目标：不能额外提交为波及目标。
    late_friend = Entity("迟到友军", "朋友", blood_limit=40, current_hp=40,
                         speed_limit=2, current_speed=2, attack_count=1, attack_power=1)
    engine.state.friends.append(late_friend)
    illegal = engine.execute_action("resolve_monster_phase", {
        "token": token,
        "choices": [{
            "actor_ref": "enemy:0",
            "daowen": {"name": "波及", "dodge": False, "dodge_targets": [
                {"target_ref": "friend:1", "dodge": False, "blood_shadow": False},
                {"target_ref": "player:0", "dodge": False, "blood_shadow": False},
            ]},
            "attack_actions": [],
        }],
    })
    assert not illegal["success"]
    assert (player.current_hp, late_friend.current_hp) == (100, 40)
    assert not late_friend.has_status("波及")
    assert engine.state.pending_monster_phase["token"] == token

    resolved = engine.execute_action("resolve_monster_phase", {
        "token": token,
        "choices": [{
            "actor_ref": "enemy:0",
            "daowen": {"name": "波及", "dodge": False, "dodge_targets": [
                {"target_ref": "player:0", "dodge": False, "blood_shadow": False},
                {"target_ref": "friend:0", "dodge": False, "blood_shadow": False},
            ]},
            "attack_actions": [],
        }],
    })
    assert resolved["success"]
    assert player.has_status("波及") and early_friend.has_status("波及")
    assert not late_friend.has_status("波及")


def test_r11_r17_aoe_dodge_does_not_leak_into_next_resolution(tmp_path):
    engine = _engine(tmp_path)
    player, first = _controlled_combat(engine)
    second = Entity("乙怪", "怪物", blood_limit=100, current_hp=100,
                    speed_limit=2, current_speed=2, attack_count=0, attack_power=1)
    first.speed_limit = first.current_speed = 2
    engine.state.enemies.append(second)
    _dw(player, "波及", 2)      # 面板固定 X=2 → 每次同时提交甲/乙（2026-10-09 起 X 下限 repealed）
    _dw(player, "杀伐", 1)

    def _end_round():
        """单行动契约：一轮一次出手；敌方向【跳过】即双方耗尽，可回终开新回合。"""
        assert engine.execute_action("skip", {"actor_ref": "enemy:0"})["success"]
        assert engine.execute_action("round_end", {})["success"]
        assert engine.execute_action("round_start", {
            "relic_choices": round_start_relic_choices(engine)})["success"]

    # 第一次：甲怪闪避不被标记，乙怪被标记（同一笔提交里逐目标闪避互不影响）
    one = engine.execute_action("use_daowen", {
        "daowen_name": "波及", "x": 2, "dodge_targets": [
            {"target_ref": "enemy:0", "dodge": True, "blood_shadow": False},
            {"target_ref": "enemy:1", "dodge": False, "blood_shadow": False},
        ],
    })
    assert one["success"], one.get("error")
    assert not first.has_status("波及") and first.current_speed == 1
    assert second.has_status("波及")
    _end_round()

    two = engine.execute_action("use_daowen", {
        "daowen_name": "波及", "x": 2, "dodge_targets": [
            {"target_ref": "enemy:0", "dodge": False, "blood_shadow": False},
            {"target_ref": "enemy:1", "dodge": False, "blood_shadow": False},
        ],
    })
    assert two["success"], two.get("error")
    assert first.has_status("波及")

    # 三次结算互相独立：本回合各行动不残留旧闪避状态
    assert "_skip_aoe_names" not in vars(engine.combat)
    _end_round()
    # 杀伐打乙怪：甲怪有波及标记 → 数值平分（杀伐1X → 1点，甲0乙1）
    three = engine.execute_action("use_daowen", {
        "daowen_name": "杀伐", "x": 1, "target_ref": "enemy:1",
        "dodge": False, "blood_shadow": False,
    })
    assert three["success"], three
    assert first.current_hp + second.current_hp == 100 + 99   # 合计承受1点（杀伐1→X²=1）


def test_r11_r17_targets_must_come_from_prepare_and_fail_atomically(tmp_path):
    engine = _engine(tmp_path)
    player, monster = _controlled_combat(engine)
    _dw(monster, "杀伐", 1)
    engine.state.current_round = 2
    prepared = engine.execute_action("prepare_monster_phase", {})
    token = prepared["result"]["token"]

    # prepare完成后才出现的实体即使能被当前状态解析，也不属于本次合法目标快照。
    late_friend = Entity("迟到友军", "朋友", blood_limit=40, current_hp=40,
                         speed_limit=2, current_speed=2, attack_count=1, attack_power=1)
    engine.state.friends.append(late_friend)
    hp_before = (player.current_hp, late_friend.current_hp)
    mutation_before = monster.mutation_count
    attacks_on_player = [{"hits": [
        {"target_ref": "player:0", "dodge": False, "blood_shadow": False, "spell_choices": {"before": {}, "after": {}}},
        {"target_ref": "player:0", "dodge": False, "blood_shadow": False, "spell_choices": {"before": {}, "after": {}}},
    ]}]

    bad_daowen_target = engine.execute_action("resolve_monster_phase", {
        "token": token,
        "choices": [{
            "actor_ref": "enemy:0",
            "daowen": {"name": "杀伐", "target_ref": "friend:0", "dodge": False, "blood_shadow": False},
            "attack_actions": attacks_on_player,
        }],
    })
    assert not bad_daowen_target["success"]
    assert (player.current_hp, late_friend.current_hp) == hp_before
    assert monster.mutation_count == mutation_before
    assert engine.state.pending_monster_phase["token"] == token

    bad_attack_target = engine.execute_action("resolve_monster_phase", {
        "token": token,
        "choices": [{
            "actor_ref": "enemy:0",
            "daowen": None,
            "attack_actions": [{"hits": [
                {"target_ref": "friend:0", "dodge": False, "blood_shadow": False, "spell_choices": {"before": {}, "after": {}}},
                {"target_ref": "player:0", "dodge": False, "blood_shadow": False, "spell_choices": {"before": {}, "after": {}}},
            ]}],
        }],
    })
    assert not bad_attack_target["success"]
    assert (player.current_hp, late_friend.current_hp) == hp_before
    assert monster.mutation_count == mutation_before
    assert engine.state.pending_monster_phase["token"] == token


def test_r11_r17_repository_has_no_legacy_automatic_monster_policy():
    """防回退：生产计算层和模拟调用方都不得保留旧固定优先级/自动闪避入口。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    combat_source = open(os.path.join(root, "engine", "combat.py"), encoding="utf-8").read()
    assert "MONSTER_ACTIVATE_PRIORITY" not in combat_source
    assert "def run_monster_phase" not in combat_source
    assert "def _monster_activate(" not in combat_source
    for folder in ("engine", "sim"):
        for base, _, files in os.walk(os.path.join(root, folder)):
            for filename in files:
                if not filename.endswith(".py"):
                    continue
                source = open(os.path.join(base, filename), encoding="utf-8").read()
                assert 'execute_action("monster_phase"' not in source


# R18-R24：发现与事件约束

def test_r18_r24_normal_named_event_item_and_explicit_tool_discovery(tmp_path):
    engine = _engine(tmp_path, seed=2)
    engine.state.player = Entity("P", "轮回者", blood_limit=50, current_hp=50,
                                 speed_limit=5, current_speed=5)
    engine.state.phase = "pre_battle"
    engine.state.current_region = "扭曲都市"
    engine.event_pool.current = "血肉温室"
    result = engine.execute_action("resolve_event", {"event": "血肉温室", "option_id": 1})
    assert result["success"]
    assert [r.name for r in engine.state.relics] == ["猩红果实"]
    assert len(engine.state.pending_item_choices) == 3
    available = engine.get_available_actions()
    assert available["phase"] == "消耗品发现待选"
    assert len(available["actions"]) == 3
    choice = engine.state.pending_item_choices[1]
    assert engine.execute_action("choose_discovered_item", {"item_name": choice})["success"]
    assert [c.name for c in engine.state.consumables] == [choice]


def test_r18_r24_boundary_payment_and_random_source(tmp_path):
    engine = _engine(tmp_path, seed=3)
    engine.state.player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    engine.state.phase = "pre_battle"
    engine.state.current_region = "罪孽都市"
    engine.state.shards = 0
    engine.event_pool.current = "遗落的赌局"
    result = engine.execute_action("resolve_event", {
        "event": "遗落的赌局", "option_id": 1, "x": 25,
    })
    assert result["success"]
    history = engine.dice.get_history()
    assert history and history[-1]["pool_name"] == "event_lost_gamble_shards"
    assert engine.state.shards in (50, -50)


def test_r18_r24_illegal_wrong_event_and_bad_discovery_choice(tmp_path):
    engine = _engine(tmp_path)
    engine.state.player = Entity("P", "轮回者", blood_limit=30, current_hp=30)
    engine.state.phase = "pre_battle"
    engine.event_pool.current = "祭坛"
    wrong = engine.execute_action("resolve_event", {"event": "无名冢", "option_id": 3})
    assert not wrong["success"]
    assert engine.event_pool.current == "祭坛"

    engine.state.pending_item_choices = ["急救箱", "储能电池"]
    engine.state.pending_item_source = "test"
    bad = engine.execute_action("choose_discovered_item", {"item_name": "高爆手雷"})
    assert not bad["success"]
    assert engine.state.pending_item_choices == ["急救箱", "储能电池"]


# R25-R31：工具、战终与取整

def test_r25_normal_energy_battery(tmp_path):
    engine = _engine(tmp_path)
    player, _ = _controlled_combat(engine)
    player.current_mana = 0
    engine.state.consumables.append(Consumable("储能电池", "", 1, 1))
    battery = engine.execute_action("consume_item", {"name": "储能电池"})
    assert battery["success"] and player.current_mana == 12


def test_r25_r31_boundary_flight_grenade_and_all_character_cleanup(tmp_path):
    engine = _engine(tmp_path)
    player, monster = _controlled_combat(engine)
    _dw(monster, "飞行", 2)  # 仅持有而未发动，不算飞行。
    engine.state.consumables.extend([
        Consumable("反怪物电击枪", "", 1, 1),
        Consumable("高爆手雷", "", 1, 1),
    ])
    shot = engine.execute_action("consume_item", {"name": "反怪物电击枪", "target": "甲怪"})
    assert shot["result"]["flying_bonus"] == 0
    nade = engine.execute_action("consume_item", {"name": "高爆手雷", "target": "甲怪"})
    assert nade["success"]
    assert engine.combat._monster_attack_actions(monster, set()) == 1
    assert max(0, monster.attack_count - monster.get_status_value("手雷减攻")) == 1

    friend = Entity("F", "朋友", blood_limit=50, current_hp=40,
                    speed_limit=3, current_speed=1, attack_count=1, attack_power=1)
    friend.healed_this_battle = 5
    friend.status_effects.append(StatusEffect("坏死", -1, 1))
    friend.shield = 9
    engine.state.friends.append(friend)
    monster.current_hp = 0
    monster.is_alive = False
    ended = engine.execute_action("battle_end", {})
    assert ended["success"]
    cleaned = engine.state.friends[0]
    assert cleaned.current_hp == 35 and cleaned.shield == 0
    assert cleaned.current_speed == cleaned.speed_limit and not cleaned.status_effects


def test_r25_r31_illegal_sculpture_and_live_battle_end(tmp_path):
    engine = _engine(tmp_path)
    _, monster = _controlled_combat(engine)
    sculpture = Consumable("雕像", "", 2, 2, kind="sculpture")
    engine.state.consumables.append(sculpture)
    bad = engine.execute_action("consume_item", {"name": "雕像", "mode": "damage", "target": "不存在"})
    assert not bad["success"]
    assert engine.state.consumables[0].current_uses == 2
    live = engine.execute_action("battle_end", {})
    assert not live["success"] and "存活敌人" in live["error"]


# R32-R38：持续语义与死斗

def test_r32_r38_normal_deform_robbery_and_transform_restore(tmp_path):
    """2026-10-11 用户令：【衰败】删除，原「衰败发动不立即掉血、回始扣10%」段落移除。"""
    engine = _engine(tmp_path)
    player, monster = _controlled_combat(engine)

    monster.attack_count, monster.attack_power = 2, 3
    monster.add_status(StatusEffect("畸变", 1, 1))
    before_limit = monster.blood_limit
    engine.combat.round_end()
    assert monster.blood_limit == before_limit - 6

    monster.shards = 3
    engine.state.shards = 0
    robbery = DaoWenEngine.resolve("抢劫", 1, target=monster, caster=player)
    speed = monster.current_speed
    engine.combat.apply_daowen_effect("抢劫", robbery, player, monster)
    assert monster.shards == 0 and engine.state.shards == 3 and monster.current_speed == speed
    monster.current_speed = 4
    engine.combat.apply_daowen_effect("抢劫", robbery, player, monster)
    assert monster.current_speed == 3  # 无碎片时才失去X速度
    assert engine.state.shards == 3

    # 【变形】2026-09-17 用户令重做：互换**当前速度 ↔ 当前法力**
    # （旧版写遗留字段 attack_power / attack_count，属性统一后对轮回者无效）
    # 本夹具 speed_limit=9 / mana_limit=100，故 5↔2 互换不会被上限钳掉。
    player.current_speed, player.current_mana = 5, 2
    transformed = DaoWenEngine.resolve("变形", 1, caster=player, target=player)
    engine.combat.apply_daowen_effect("变形", transformed, player, player)
    assert (player.current_speed, player.current_mana) == (2, 5)
    engine.combat.round_end()
    assert (player.current_speed, player.current_mana) == (5, 2), "持续结束后还原互换前的速度/法力"

    # R33：定型只锁攻击力/攻击次数；速度变化仍合法，且被挡的变形不挂空状态。
    player.add_status(StatusEffect("定型", 2, 1))
    speed_before = player.current_speed
    # 2026-10-25：【减速】遗物化，改用同样改当前速度的【超频】（此处只验证速度变化合法）
    boost = DaoWenEngine.resolve("超频", 1, target=player, caster=monster)
    engine.combat.apply_daowen_effect("超频", boost, monster, player)
    assert player.current_speed == speed_before + 1
    # 被【定型】挡下的变形：不再改写任何面板（旧断言查的是遗留字段 attack_power/attack_count）
    speed_after_slow, mana_after_slow = player.current_speed, player.current_mana
    engine.combat.apply_daowen_effect("变形", transformed, player, player)
    assert (player.current_speed, player.current_mana) == (speed_after_slow, mana_after_slow)
    assert not player.has_status("变形")


def test_r32_r38_boundary_complete_tie_alternates_round_first(tmp_path):
    engine = _engine(tmp_path, seed=11)
    player = Entity("挑战者", "轮回者", blood_limit=60, current_hp=60,
                    mana_limit=10, current_mana=10, speed_limit=6, current_speed=6,
                    attack_count=3, attack_power=1)
    engine.state.player = player
    engine.state.current_region = "罪孽都市"
    snapshot = engine._serialize_full_character()
    snapshot["player"]["name"] = "守擂者"
    with open(engine.sealed_candidate_path, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, ensure_ascii=False)
    result = engine._trigger_final_crown()
    assert result["outcome"] == "duel_start"
    assert result["complete_tie_alternating"] is True
    assert result["first_mover"] in ("player_side", "opponent_side")
    first = engine.state.duel_round_first
    engine.execute_action("round_start", {"relic_choices": {}})
    assert engine.state.duel_turn == first
    engine.state.combat_subphase = "await_round_end"  # 边界测试跳过双方出手，仅验证完全平局首手轮换
    engine.execute_action("round_end", {})
    engine.execute_action("round_start", {"relic_choices": {}})
    assert engine.state.duel_turn != first


def test_r32_r38_illegal_non_tie_does_not_enable_tie_rotation_and_remainder_continues(tmp_path):
    engine = _engine(tmp_path)
    player, opponent = _controlled_combat(engine)
    opponent.entity_type = "轮回者"
    engine.state.in_final_duel = True
    engine.state.duel_tie_alternating = False
    engine.state.duel_turn = "player_side"
    player.actions_used_this_round = 0
    opponent.actions_used_this_round = opponent.action_count
    engine._advance_duel_turn()
    assert engine.state.duel_turn == "player_side"  # 对方耗尽后，己方余下出手继续。

    # 非平局不能伪造开启轮换；round_start保持原流程。
    engine.state.duel_round_first = "opponent_side"
    engine.execute_action("round_start", {"relic_choices": {}})
    assert engine.state.duel_turn == "player_side"
