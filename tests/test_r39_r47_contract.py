"""R39-R47 与 D01-D07 重新审计契约：正常、边界、非法输入。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.combat import CombatEngine
from engine.daowen import DaoWenEngine
from engine.dice import DiceEngine
from engine.models import Consumable, DaoWen, DaoWenInstance, Entity, GameState, Relic, Spell


def _engine(tmp_path, seed=17):
    return GameEngine(
        db_path=str(tmp_path / "rules.db"), save_dir=str(tmp_path / "saves"),
        death_book_path=str(tmp_path / "death.md"),
        sealed_candidate_path=str(tmp_path / "sealed.json"), rng_seed=seed,
    )


def _combat(engine, *, attack_count=1, enemy_attack_count=0):
    # DM裁定 2026-09-10：轮回者攻次=当前速度、攻力=当前法力，写 attack_count/attack_power
    # 面板无效。把 attack_count 参数映射到当前速度，参数才能继续驱动攻次。
    # 速限保留 6（疲惫/闪避要留余量），只把**当前速度**映射为攻次。
    # 当前法力保持 20：多条断言依赖它（如缄默面具 +40 法力 → 60）；
    # 代价是攻力=当前法力=20，普攻伤害断言按 20 计。
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=20, current_mana=20,
                    speed_limit=6, current_speed=attack_count,
                    attack_count=attack_count, attack_power=5)
    enemy = Entity("M", "怪物", blood_limit=100, current_hp=100,
                   attack_count=enemy_attack_count, attack_power=3)
    engine.state.player = player
    engine.state.enemies = [enemy]
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "player_actions"
    engine.state.current_round = 1
    return player, enemy


def _decline_spells(option):
    return {timing: {spell["spell_name"]: {"use": False}
                     for spell in option.get("spell_options", {}).get(timing, [])}
            for timing in ("before", "after")}


def _resolve_empty_monster_phase(engine):
    prepared = engine.execute_action("prepare_monster_phase", {})
    assert prepared["success"], prepared
    choices = []
    for actor in prepared["result"]["actors"]:
        target_option = actor["attack_target_options"][0] if actor["attack_target_options"] else None
        attacks = [{"hits": [{
            "target_ref": target_option["ref"], "dodge": False, "blood_shadow": False,
            "spell_choices": _decline_spells(target_option),
        } for _ in range(actor["base_hits_per_attack"])]}
                   for _ in range(actor["base_attack_actions"])]
        choices.append({"actor_ref": actor["actor_ref"], "daowen": None,
                        "attack_actions": attacks})
    return engine.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"], "choices": choices,
    })


# R39：战斗子阶段

def test_r39_normal_subphase_lifecycle(tmp_path):
    engine = _engine(tmp_path); _combat(engine)
    engine.state.combat_subphase = "await_round_start"
    assert engine.execute_action("round_start", {"relic_choices": {}})["success"]
    assert engine.state.combat_subphase == "player_actions"
    assert _resolve_empty_monster_phase(engine)["success"]
    assert engine.state.combat_subphase == "await_round_end"
    assert engine.execute_action("round_end", {})["success"]
    assert engine.state.combat_subphase == "await_round_start"


def test_r39_boundary_empty_enemy_phase_still_advances(tmp_path):
    engine = _engine(tmp_path); _combat(engine); engine.state.enemies = []
    result = _resolve_empty_monster_phase(engine)
    assert result["success"] and engine.state.combat_subphase == "await_round_end"


def test_r39_illegal_repeated_transitions_are_atomic(tmp_path):
    engine = _engine(tmp_path); player, _ = _combat(engine)
    engine.state.combat_subphase = "await_round_start"
    assert engine.execute_action("round_start", {"relic_choices": {}})["success"]
    before = (engine.state.current_round, player.current_mana)
    denied = engine.execute_action("round_start", {"relic_choices": {}})
    assert not denied["success"] and (engine.state.current_round, player.current_mana) == before
    assert not engine.execute_action("round_end", {})["success"]


# R40：两阶段攻击

def test_r40_normal_attack_prepare_resolve(tmp_path):
    engine = _engine(tmp_path); player, enemy = _combat(engine)
    prepared = engine.execute_action("prepare_attack", {"actor_ref": "player:0"})
    option = prepared["result"]["target_options"][0]
    result = engine.execute_action("resolve_attack", {
        "token": prepared["result"]["token"], "hits": [{
            "target_ref": option["ref"], "dodge": False, "blood_shadow": False,
            "spell_choices": _decline_spells(option),
        }],
    })
    assert result["success"] and enemy.current_hp == 80   # 攻力=当前法力=20（原按 attack_power=5 计）
    assert player.actions_used_this_round == 1


def test_r40_boundary_zero_hit_attack_consumes_one_action(tmp_path):
    engine = _engine(tmp_path); player, _ = _combat(engine, attack_count=0)
    prepared = engine.execute_action("prepare_attack", {"actor_ref": "player:0"})
    assert prepared["result"]["hit_count"] == 0
    result = engine.execute_action("resolve_attack", {
        "token": prepared["result"]["token"], "hits": [],
    })
    assert result["success"] and player.actions_used_this_round == 1


def test_r40_illegal_unbound_and_outside_snapshot_rejected(tmp_path):
    engine = _engine(tmp_path); player, enemy = _combat(engine)
    assert not engine.execute_action("attack", {})["success"]
    assert not engine.execute_action("dodge_decision", {
        "attacker": "P", "target": "M", "dodge": False,
    })["success"]
    prepared = engine.execute_action("prepare_attack", {"actor_ref": "player:0"})
    hp = enemy.current_hp
    bad = engine.execute_action("resolve_attack", {
        "token": prepared["result"]["token"], "hits": [{
            "target_ref": "friend:99", "dodge": False, "blood_shadow": False,
            "spell_choices": {"before": {}, "after": {}},
        }],
    })
    assert not bad["success"] and enemy.current_hp == hp and player.actions_used_this_round == 0
    assert engine.state.pending_attack["token"] == prepared["result"]["token"]


# R41：可执行 action schema

def test_r41_normal_advertised_setup_action_executes(tmp_path):
    engine = _engine(tmp_path)
    action = engine.get_available_actions()["actions"][0]
    assert action["action_type"] == "setup_attributes"
    result = engine.execute_action(action["action_type"], {
        "name": "P", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    assert result["success"]


def test_r41_boundary_daowen_max_uses_real_cost(tmp_path):
    engine = _engine(tmp_path); player, _ = _combat(engine)
    player.current_mana = 4
    player.dao_wen["杀伐"] = DaoWenInstance(DaoWen("杀伐", "", "消耗", "X", ""))
    action = next(a for a in engine.get_available_actions()["actions"]
                  if a.get("action_type") == "use_daowen")
    assert action["params_schema"]["x"]["maximum"] == 4


def test_r41_illegal_legacy_action_names_not_advertised(tmp_path):
    engine = _engine(tmp_path); _combat(engine)
    actions = engine.get_available_actions()["actions"]
    names = {entry.get("action_type") for entry in actions}
    assert None not in names
    assert not ({"daowen", "spell", "consumable", "attack", "dodge_decision"} & names)
    dispatcher = (Path(__file__).resolve().parents[1] / "engine" / "api.py").read_text(encoding="utf-8")
    assert all(f'action_type == "{name}"' in dispatcher for name in names)


# R42：显式法术反应

def _spell_combat():
    state = GameState()
    player = Entity("P", "轮回者", blood_limit=60, current_hp=60,
                    mana_limit=20, current_mana=20, speed_limit=3, current_speed=3)
    player.dao_wen["庇护"] = DaoWenInstance(DaoWen("庇护", "", "消耗", "X", ""))
    player.spells = [Spell("后发制人", ["庇护"], "受到伤害前", "")]
    enemy = Entity("M", "怪物", blood_limit=50, current_hp=50, attack_count=1, attack_power=8)
    state.player = player; state.enemies = [enemy]
    return state, player, enemy, CombatEngine(state, DiceEngine(seed=3))


def test_r42_normal_explicit_spell_x_resolves():
    state, player, enemy, combat = _spell_combat()
    result = combat.resolve_attack(enemy, player, spell_choices={
        "before": {"后发制人": {"use": True, "steps": [
            {"x": 4, "target_ref": "player:0", "dodge": False},
        ]}}, "after": {},
    })
    assert result["spell_logs"] and player.current_hp == 60 and player.current_mana == 16


def test_r42_boundary_explicit_decline_takes_damage():
    _, player, enemy, combat = _spell_combat()
    result = combat.resolve_attack(enemy, player, spell_choices={
        "before": {"后发制人": {"use": False}}, "after": {},
    })
    assert result["damage_dealt"] == 8 and player.current_hp == 52


def test_r42_illegal_missing_spell_choice_has_no_side_effect():
    _, player, enemy, combat = _spell_combat()
    with pytest.raises(ValueError, match="逐一覆盖"):
        combat.resolve_attack(enemy, player, spell_choices={"before": {}, "after": {}})
    assert (player.current_hp, player.current_mana, enemy.current_hp) == (60, 20, 50)


def test_r42_target_daowen_trigger_is_explicit(tmp_path):
    engine = _engine(tmp_path); player, opponent = _combat(engine)
    opponent.entity_type = "轮回者"; opponent.mana_limit = opponent.current_mana = 20
    player.dao_wen["杀伐"] = DaoWenInstance(DaoWen("杀伐", "", "消耗", "X", ""))
    for name in ("必中", "杀伐", "连击"):   # 2026-10-03：【坠落】删除 → 改用【必中】
        opponent.dao_wen[name] = DaoWenInstance(DaoWen(name, "", "消耗", "X", ""))
    opponent.spells = [Spell("咎由自取", ["杀伐", "连击"], "目标发动道纹前", "")]
    result = engine.execute_action("use_daowen", {
        "daowen_name": "杀伐", "x": 1, "target_ref": "enemy:0",
        "dodge": False, "blood_shadow": False,
        # 2026-10-03：【坠落】删除后【咎由自取】只剩 杀伐→连击 两步
        "trigger_spell_choices": {"enemy:0": {"咎由自取": {"use": True, "steps": [
            {"x": 1, "target_ref": "player:0", "dodge": False},
            {"x": 1, "target_ref": "player:0", "dodge": False},
        ]}}},
    })
    assert result["success"] and result["trigger_spell_logs"]
    assert player.current_hp == 99 and opponent.current_hp == 99   # 杀伐1→X²=1（2026-09-13）


# R43：确定性事件

def _event_engine(tmp_path, name, region="扭曲都市"):
    engine = _engine(tmp_path)
    engine.state.player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                                 speed_limit=10, current_speed=10)
    engine.state.phase = "pre_battle"; engine.state.current_region = region
    engine.state.shards = 100; engine.event_pool.current = name
    return engine


def test_r43_normal_doctor_event_adds_employee(tmp_path):
    engine = _event_engine(tmp_path, "医生")
    result = engine.execute_action("resolve_event", {"event": "医生", "option_id": 3})
    assert result["success"] and engine.state.shards == 90
    assert [(e.name, e.attack_count, e.attack_power, e.blood_limit) for e in engine.state.employees] == [
        ("医生", 1, 1, 50)]
    if engine.state.pending_item_choices:
        assert engine.execute_action("choose_discovered_item", {
            "item_name": engine.state.pending_item_choices[0],
        })["success"]
    upgraded = engine.execute_action("upgrade_doctor", {"mode": "attack_power"})
    assert upgraded["success"] and engine.state.shards == 85
    assert engine.state.employees[0].attack_power == 3


def test_r43_boundary_arena_modifier_registered_once(tmp_path):
    engine = _event_engine(tmp_path, "地下角斗场", "罪孽都市")
    result = engine.execute_action("resolve_event", {"event": "地下角斗场", "option_id": 1})
    assert result["success"]
    assert engine.state.event_modifiers["arena_health_percent"] == 20
    assert engine.state.event_modifiers["arena_double_loot"] is True
    assert engine.event_pool.current is None


def test_r43_illegal_creative_event_waits_for_dm_without_cost(tmp_path):
    engine = _event_engine(tmp_path, "绝望来电")
    hp = engine.state.player.current_hp
    result = engine.execute_action("resolve_event", {"event": "绝望来电", "option_id": 1})
    assert result["success"] and result["completed"] is False
    assert engine.state.player.current_hp == hp and engine.event_pool.current == "绝望来电"
    assert result["interrupt"]["interrupt_type"] == "未见场景"


def test_r43_all_implemented_event_options_have_a_runtime_route(tmp_path):
    catalog_root = tmp_path / "catalog"; catalog_root.mkdir()
    catalog = _engine(catalog_root).event_pool.events
    checked = 0
    for event_index, (event_name, event) in enumerate(catalog.items()):
        if event["region"] not in ("通用", "扭曲都市", "罪孽都市", "龙心谷"):
            continue
        for option in event["options"]:
            root = tmp_path / f"event_{event_index}_{option['id']}"; root.mkdir()
            engine = _engine(root, seed=event_index * 10 + option["id"])
            player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                            mana_limit=100, current_mana=100, speed_limit=100, current_speed=100)
            for name in set(engine.SPELL_REGISTRY) | {"杀伐", "切割", "庇护", "再生", "连击"}:
                player.dao_wen[name] = DaoWenInstance(DaoWen(name, "", "消耗", "X", ""))
            engine.state.player = player; engine.state.phase = "pre_battle"
            engine.state.current_region = event["region"] if event["region"] != "通用" else "罪孽都市"
            engine.state.shards = 1000; engine.state.energy = 20
            engine.state.friends = [Entity("F", "朋友", blood_limit=50, current_hp=50,
                                           attack_count=2, attack_power=2)]
            engine.state.relics = [Relic("测试遗物", "")]
            engine.event_pool.current = event_name
            text = option["text"]
            params = {"event": event_name, "option_id": option["id"], "x": 1, "wager": 1,
                      "target_ref": "friend:0", "friend_ref": "friend:0", "friend": "F",
                      "relic_name": "测试遗物", "spell_names": list(engine.SPELL_REGISTRY)[:2],
                      "resonance_type": "转换", "daowen_names": ["杀伐"]}
            if "自选一件遗物" in text:
                engine._init_relic_pool(); params["relic_name"] = engine.state.relics_pool[0].name
            if event_name == "过路商人" and text.startswith("遗物交易"):
                merchant = engine._initialize_merchant_inventory()
                params.update({
                    "trade_direction": "buy",
                    "relic_name": merchant.relics[0].name,
                    "price": 0,
                    "confirmed": True,
                })
            if "48点恢复量" in text:
                params["heal_allocations"] = [{"target_ref": "player:0", "amount": 48}]
            if "失忆" in text:
                import re
                match = re.search(r"失忆(\d+)", text)
                params["daowen_names"] = list(player.dao_wen)[:int(match.group(1)) if match else 1]
            result = engine.execute_action("resolve_event", params)
            assert result["success"], (event_name, option["id"], result)
            assert not result.get("result", {}).get("instructions"), (event_name, option["id"], result)
            checked += 1
    assert checked == 86  # 当前已接入的事件选项数；删除/迁移事件后同步更新此契约


# R44：统一向上取整

def test_r44_normal_guard_lamp_ceil(tmp_path):
    engine = _engine(tmp_path); player, _ = _combat(engine)
    player.mana_limit = 5; player.current_mana = 0
    engine.state.relics = [Relic("守夜灯", "")]
    engine.combat.round_start({})
    # DM裁定 2026-09-09：一池制，[回始]不回填（法限5 也不再自动补满）；
    # 2026-10-15：守夜灯改为每次[回始]授予 ceil(法限*50%)=ceil(2.5)=3。
    assert player.current_mana == 3
    player.current_mana = 0
    granted = engine.combat._grant_shouyedeng(player)
    assert granted["gained"] == 3
    assert player.current_mana == 3
    player.current_mana = 0
    granted_again = engine.combat._grant_shouyedeng(player)
    assert granted_again["gained"] == 3
    assert player.current_mana == 3


def test_r44_boundary_slow_one_stays_one(tmp_path):
    engine = _engine(tmp_path); player, enemy = _combat(engine)
    player.current_speed = 1
    engine.combat.apply_daowen_effect("减速", {"x": 1, "speed_halved": True, "duration": 1}, enemy, player)
    assert player.current_speed == 1


def test_r44_illegal_no_negative_from_zero(tmp_path):
    engine = _engine(tmp_path); player, enemy = _combat(engine)
    player.current_speed = 0
    engine.combat.apply_daowen_effect("减速", {"x": 1, "speed_halved": True, "duration": 1}, enemy, player)
    assert player.current_speed == 0


# R45：具名消耗品

def test_r45_normal_fake_note_applies_before_durability(tmp_path):
    engine = _engine(tmp_path); player, _ = _combat(engine)
    engine.state.consumables = [Consumable("假钞贴", "使用后获得20[假碎片]", 2, 2)]
    result = engine.execute_action("consume_item", {"name": "假钞贴"})
    assert result["success"] and engine.state.fake_shards == 20
    assert engine.state.consumables[0].current_uses == 1


def test_r45_boundary_red_spring_requires_exact_eight(tmp_path):
    engine = _event_engine(tmp_path, "医生")
    engine.event_pool.current = None  # 本测试只覆盖物品；待结算事件会按全局门禁阻止其它行动。
    engine.state.consumables = [Consumable("赤泉囊", "", 6, 6)]
    engine.state.player.current_hp = 50
    result = engine.execute_action("consume_item", {
        "name": "赤泉囊", "heal_allocations": [{"target_ref": "player:0", "amount": 8}],
    })
    assert result["success"] and engine.state.player.current_hp == 58
    assert engine.state.event_modifiers["red_spring_battle_losses"] == 2


def test_r45_illegal_unknown_effect_does_not_consume(tmp_path):
    engine = _engine(tmp_path); _combat(engine)
    engine.state.consumables = [Consumable("未知物", "无法解析", 2, 2)]
    result = engine.execute_action("consume_item", {"name": "未知物"})
    assert not result["success"] and engine.state.consumables[0].current_uses == 2


def test_r45_all_current_named_consumable_handlers(tmp_path):
    engine = _engine(tmp_path); player, enemy = _combat(engine)
    player.current_hp = 80; enemy.shield = 20
    items = [
        Consumable("穿甲弹", "", 2, 2), Consumable("洗劫面具", "", 2, 2),
        Consumable("龙血瓶", "", 10, 10), Consumable("绝息淤泥", "", 1, 1),
        Consumable("活性土壤", "", 1, 1),
    ]
    engine.state.consumables = items
    shot = engine.execute_action("consume_item", {"name": "穿甲弹", "target_ref": "enemy:0"})
    assert shot["success"] and enemy.current_hp == 85 and enemy.shield == 20
    mask = engine.execute_action("consume_item", {"name": "洗劫面具"})
    assert mask["success"] and engine.combat.bizhong_remaining(player) == 2
    bottle = engine.execute_action("consume_item", {
        "name": "龙血瓶", "amount": 3, "target_ref": "player:0",
    })
    assert bottle["success"] and player.current_hp == 83 and items[2].current_uses == 7
    mud = engine.execute_action("consume_item", {"name": "绝息淤泥"})
    assert mud["success"] and engine.state.event_modifiers["escape_at_battle_end"] is True
    engine.state.combat_subphase = "await_round_start"
    engine.state.current_round = 0
    player.current_mana = 5
    soil = engine.execute_action("consume_item", {
        "name": "活性土壤", "x": 5, "dm_approved": True,
        "friend": {"name": "芽", "attack_count": 0, "attack_power": 0, "blood_limit": 6},
    })
    assert soil["success"] and engine.state.friends[-1].name == "芽" and player.current_mana == 0


# R46：事件遗物与统一触发

def test_r46_normal_event_relic_battle_start_matrix(tmp_path):
    # 攻次=当前速度：本条要测闪避/避风铃（当前速度归零才+15格挡），
    # 必须留出速度余量，否则第一击闪避就归零、把避风铃误触发进断言。
    engine = _engine(tmp_path); player, enemy = _combat(engine, attack_count=6)
    friend = Entity("F", "朋友", blood_limit=30, current_hp=30)
    engine.state.friends = [friend]
    friend.relics = [Relic("防弹插板", "")]
    heart = Consumable("衰老龙心", "", 3, 3, kind="dragon_heart", dragon_heart_type="衰老")
    engine.state.consumables = [heart]
    # 2026-10-11 用户令：【苍白之花】改为纯被动（当前法力可超出[法限]），
    # 不再是战始可选遗物——它从这张矩阵里退场，速度也不再被它的疲惫5 压低。
    # 2026-10-14 用户令：【猩红果实】同样重做为被动（当前生命可超出[血限]），
    # 旧战始流血10 已移交道纹【增殖】——它也退场，生命不再被压到 90。
    names = ["缄默面具", "帮派令", "负岳索", "炉心坠", "烙痕钉"]
    engine.state.relics = [Relic(name, "", tags=["事件"]) for name in names]
    engine.state.event_modifiers["silent_mask_x"] = 2
    choices = {
        "负岳索": {"target_ref": "friend:0"}, "炉心坠": {"heart_name": "衰老龙心"},
        "烙痕钉": {"target_ref": "enemy:0"},
    }
    engine.combat.validate_battle_start_relic_choices(choices)
    logs = engine.combat.process_relics("battle_start", {"relic_choices": choices})
    # 2026-09-13 全局上限：缄默面具 +40 被[法限]截断，法力停在上限而非 60。
    assert logs and (player.current_hp, player.current_speed) == (100, 6)
    assert player.current_mana == player.mana_limit
    assert player.has_status("洗劫") and friend.has_status("负岳索") and friend.shield == 15
    assert heart.current_uses == 13 and engine.state.event_modifiers["brand_nail_target_ref"] == "enemy:0"
    enemy_hp = enemy.current_hp
    engine.combat._pay_bleed_cost(player, 1)
    assert enemy.current_hp == enemy_hp - 10  # 烙痕钉
    friend.current_hp = 20; friend.shield = 0
    engine.combat._apply_hostile_damage(friend, 5, source=enemy)
    # DM裁定（2026-08-19）：100 −1(上一步的流血代价，同时触发了烙痕钉) +5(负岳索等量回复)，
    # 按[血限]封顶回 100（2026-10-14：猩红退场后起点从 90 回到 100）。
    assert friend.current_hp == 15 and player.current_hp == 100 and not friend.has_status("负岳索")
    player.dao_wen["连击"] = DaoWenInstance(DaoWen("连击", "", "流血", "X", ""))
    blocked = engine.execute_action("use_daowen", {
        "daowen_name": "连击", "x": 1, "target_ref": "enemy:0",
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    })
    assert not blocked["success"] and "缄默面具" in blocked["error"]


def test_r46_event_relic_round_and_battle_end_effects(tmp_path):
    # 2026-10-14：【余火印】整段删除（extra 方案废弃），回始段只剩【皮衣】；
    # 【猩红果实】重做为被动，战终段改由道纹【增殖】的战终待决覆盖。
    engine = _engine(tmp_path); player, _ = _combat(engine)
    engine.state.relics = [Relic("皮衣", "")]
    player.hp_lost_this_round = 7
    engine.state.combat_subphase = "await_round_end"
    assert engine.execute_action("round_end", {})["success"]
    started = engine.execute_action("round_start", {"relic_choices": {}})
    assert started["success"] and player.shield == 7

    engine.state.enemies = []
    engine.state.event_modifiers["zengzhi_pending"] = [{"target_ref": "player:0", "amount": 4}]
    before = (player.blood_limit, engine.state.energy)
    ended = engine.execute_action("battle_end", {})
    assert ended["success"]
    assert ended["result"]["zengzhi_settled"] == [{"target": player.name, "increase": 4}]
    assert (player.blood_limit, engine.state.energy) == (before[0] + 4, before[1])


def test_r46_boundary_death_dodge_dragon_and_might_triggers(tmp_path):
    # 攻次=当前速度：本条要测闪避/避风铃（当前速度归零才+15格挡），
    # 必须留出速度余量，否则第一击闪避就归零、把避风铃误触发进断言。
    engine = _engine(tmp_path); player, enemy = _combat(engine, attack_count=6)
    friend = Entity("F", "朋友", blood_limit=30, current_hp=30)
    engine.state.friends = [friend]
    engine.state.relics = [Relic("避风铃", ""), Relic("回锋刀", ""),
                           Relic("焦黑发丝", ""), Relic("龙威", "", tags=["龙族"])]
    engine.combat._spend_dodge_speed(player, "enemy:0")
    assert player.shield == 3 and enemy.current_hp == 97
    engine.state.relics.append(Relic("龙族血脉", "", tags=["龙族"]))
    # 2026-09-13 全局上限：焦黑发丝的 +2 需要[速限]有空间，否则被上限吃掉。
    player.speed_limit = max(player.speed_limit, player.current_speed + 2)
    before_speed = player.current_speed
    engine.combat._apply_hostile_damage(enemy, 1, source=player)
    assert not enemy.is_alive and player.current_speed == before_speed + 2
    enemy2 = Entity("M2", "怪物", blood_limit=20, current_hp=20, attack_count=1, attack_power=1)
    enemy2.dao_wen["杀伐"] = DaoWenInstance(DaoWen("杀伐", "", "", "", ""), 1)
    engine.state.enemies = [enemy2]
    engine.state.current_round = 2
    prepared = engine.combat.prepare_monster_phase()["actors"][0]
    hostile_targets = prepared["daowen_options"][0]["target_options"]
    assert not any(target["ref"] == "friend:0" for target in hostile_targets)
    assert [target["ref"] for target in prepared["attack_target_options"]] == ["player:0"]


def test_r46_illegal_missing_event_relic_choices_is_atomic(tmp_path):
    engine = _engine(tmp_path); player, _ = _combat(engine)
    # 2026-10-11：苍白之花退场后改用【负岳索】（仍必须显式提交 target_ref）驱动原子性
    engine.state.relics = [Relic("猩红果实", ""), Relic("负岳索", "")]
    engine.state.friends = [Entity("F", "朋友", blood_limit=30, current_hp=30)]
    before = (player.current_hp, player.current_speed)
    with pytest.raises(ValueError):
        engine.combat.validate_battle_start_relic_choices({"猩红果实": {"use": False}})
    assert (player.current_hp, player.current_speed) == before


# R47：完整版本化存档

def test_r47_normal_full_round_trip(tmp_path):
    engine = _engine(tmp_path); player, enemy = _combat(engine)
    player.current_hp = 73; player.dao_wen["杀伐"] = DaoWenInstance(DaoWen("杀伐", "", "消耗", "X", ""))
    engine.state.relics = [Relic("皮衣", "")]
    engine.state.event_modifiers["leather_shield_next"] = 9
    assert engine.save_game("full")["success"]
    player.current_hp = 1; engine.state.relics.clear(); engine.state.event_modifiers.clear()
    loaded = engine.load_game("full")
    assert loaded["success"] and engine.state.player.current_hp == 73
    assert [relic.name for relic in engine.state.relics] == ["皮衣"]
    assert engine.state.event_modifiers["leather_shield_next"] == 9
    assert engine.combat.state is engine.state and engine.combat.dice is engine.dice


def test_r47_boundary_pending_and_rng_round_trip(tmp_path):
    engine = _engine(tmp_path, seed=9); _combat(engine)
    roll = engine.dice.auto_roll("before_save", ["a", "b"], context="test")
    prepared = engine.execute_action("prepare_attack", {"actor_ref": "player:0"})
    token = prepared["result"]["token"]
    engine.save_game("pending")
    engine.state.pending_attack = {}; engine.dice.auto_roll("mutated", [1, 2], context="test")
    assert engine.load_game("pending")["success"]
    assert engine.state.pending_attack["token"] == token
    assert engine.dice.get_history()[-1]["pool_name"] == "before_save"
    assert roll["selected"] in ("a", "b")


def test_r47_illegal_corrupt_save_is_atomic(tmp_path):
    engine = _engine(tmp_path); player, _ = _combat(engine)
    path = Path(engine.save_dir) / "save_bad.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"format": "linji-save", "version": engine.SAVE_FORMAT_VERSION,
                                "payload": "%%%"}), encoding="utf-8")
    before = (id(engine.state), player.current_hp)
    result = engine.load_game("bad")
    assert not result["success"] and (id(engine.state), engine.state.player.current_hp) == before


def test_d01_d07_obsolete_paths_removed_or_reused():
    root = Path(__file__).resolve().parents[1]
    assert not (root / "engine" / "battle_flow.py").exists()
    dice_source = (root / "engine" / "dice.py").read_text(encoding="utf-8")
    api_source = (root / "engine" / "api.py").read_text(encoding="utf-8")
    gamedata = (root / "engine" / "gamedata.py").read_text(encoding="utf-8")
    assert "class EventPool" not in dice_source and "class RandomRequest" not in dice_source
    assert "random_number" not in api_source and "request_random" not in api_source
    assert "CombatSubphase" in api_source and "ActionPhase" in (root / "engine" / "combat.py").read_text(encoding="utf-8")
    assert "MONSTER_POOLS" not in gamedata and "RELIC_POOL" not in gamedata and "SPELL_LIBRARY" not in gamedata
