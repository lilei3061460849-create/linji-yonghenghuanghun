"""消耗品通用被动【自愈X】契约测试（2026-10-14 用户令）。

- 自愈X＝恢复X点当前耐久，不超过上限；固定点数外，无限肉块80%是比例特例；
- 时机：战终（battle_end）＋目标死亡时（target_death：持有者本场出手选中过的目标死亡；
  2026-10-14 用户裁定，没打过的不算；命中与否不影响“选中”）；
- 只看选中集合，不限敌我；满耐久触发记 restored=0；一尸一结（幂等）。
- 合成 kind 仅验证机制形状（A（5/10）式举例），不落地新物品。
"""
from engine.api import GameEngine
from engine.combat import CombatEngine
from engine.combat_events import CombatEventType
from engine.consumables import (
    CONSUMABLE_ZIYU_TRIGGERS,
    ZIYU_TRIGGER_BATTLE_END,
    ZIYU_TRIGGER_TARGET_DEATH,
    note_action_targets,
)
from engine.dice import DiceEngine
from engine.enums import CombatSubphase
from engine.models import Consumable, Entity, GameState

from tests.setup_support import begin_battle, begin_round, finish_initial_daowen


def _engine(tmp_path) -> GameEngine:
    return GameEngine(
        db_path=str(tmp_path / "rulings.db"),
        save_dir=str(tmp_path / "saves"),
        sealed_candidate_path=str(tmp_path / "sealed.json"),
        death_book_path=str(tmp_path / "death.md"),
        rng_seed=19,
    )


def _open(engine):
    """走完开局三步（残韵/副本/遗物），phase 到局外，可 begin_battle。"""
    engine.execute_action("setup_choose_resonance", {"resonance_type": "曲解"})
    setup = engine.execute_action("setup_choose_region", {"region": "龙心谷"})
    if (setup.get("result") or {}).get("relic_choices"):
        engine.execute_action("choose_discovered_relic",
                              {"relic_name": setup["result"]["relic_choices"][0]})


def _arena(enemy_hp: int = 8):
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    player = Entity("P", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=50, current_mana=50, speed_limit=10, current_speed=10)
    enemy = Entity("M", "怪物", blood_limit=enemy_hp, current_hp=enemy_hp)
    state.player = player
    state.enemies = [enemy]
    return state, CombatEngine(state, DiceEngine()), player, enemy


def _kill(combat, player, target, amount=20):
    combat._apply_hostile_damage(target, amount, source=player, ctx={
        "timing": "player_action", "source": "杀伐", "source_type": "daowen",
        "actor": player, "target": target, "mechanic": "damage", "subtype": "daowen",
        "amount": amount, "tags": {"daowen"},
    })


def test_battle_end_flat_zi_yu_caps_at_max(tmp_path, monkeypatch):
    """正常路径：[战终]自愈2封顶（4/5→5/5，restored=1），result 键为 zi_yu_logs。"""
    monkeypatch.setitem(CONSUMABLE_ZIYU_TRIGGERS, "test_charm",
                        [(ZIYU_TRIGGER_BATTLE_END, 2)])
    engine = _engine(tmp_path)
    engine.execute_action("setup_attributes", {
        "name": "测试者", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    finish_initial_daowen(engine)
    item = Consumable(name="测试护符", effect="战终自愈2",
                      current_uses=4, max_uses=5, kind="test_charm")
    engine.state.consumables.append(item)
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value

    end = engine.execute_action("battle_end", {})

    assert end["success"]
    assert (item.current_uses, item.max_uses) == (5, 5)
    assert "infinite_meat_logs" not in end["result"]
    logs = end["result"]["zi_yu_logs"]
    assert [(l["item"], l["trigger"], l["restored"]) for l in logs] == [
        ("测试护符", ZIYU_TRIGGER_BATTLE_END, 1)]


def test_battle_end_full_item_logs_zero_restore(tmp_path, monkeypatch):
    """边界：满耐久触发不溢出，照样记一条 restored=0 的日志（与旧口径一致）。"""
    monkeypatch.setitem(CONSUMABLE_ZIYU_TRIGGERS, "test_charm",
                        [(ZIYU_TRIGGER_BATTLE_END, 2)])
    engine = _engine(tmp_path)
    engine.execute_action("setup_attributes", {
        "name": "测试者", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    finish_initial_daowen(engine)
    item = Consumable(name="测试护符", effect="战终自愈2",
                      current_uses=5, max_uses=5, kind="test_charm")
    engine.state.consumables.append(item)
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value

    end = engine.execute_action("battle_end", {})

    assert end["success"]
    assert (item.current_uses, item.max_uses) == (5, 5)
    logs = end["result"]["zi_yu_logs"]
    assert [l["restored"] for l in logs if l["item"] == "测试护符"] == [0]


def test_infinite_meat_percent_path_via_table(tmp_path):
    """回归：无限肉块 80% 已损走通用表（1/4：已损3→ceil(2.4)=3→4/4）。"""
    engine = _engine(tmp_path)
    engine.execute_action("setup_attributes", {
        "name": "测试者", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    finish_initial_daowen(engine)
    meat = Consumable(name="无限肉块", effect="战终自愈80%已损耐久",
                      current_uses=1, max_uses=4, kind="infinite_meat")
    engine.state.consumables.append(meat)
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value

    end = engine.execute_action("battle_end", {})

    assert end["success"]
    assert (meat.current_uses, meat.max_uses) == (4, 4)
    assert [l["restored"] for l in end["result"]["zi_yu_logs"]
            if l["item"] == "无限肉块"] == [3]


def _fang(state, uses=2):
    item = Consumable(name="测试獠牙", effect="目标死亡时自愈1",
                      current_uses=uses, max_uses=5, kind="test_fang")
    state.consumables.append(item)
    return item


def _zi_yu_events(combat):
    return [e for e in combat.event_stream
            if e.event_type == CombatEventType.CONSUMABLE_ZIYU]


def test_target_death_selected_enemy_fires(monkeypatch):
    """正常路径：选中过的敌方死亡→自愈1（2/5→3/5），并发事件。"""
    monkeypatch.setitem(CONSUMABLE_ZIYU_TRIGGERS, "test_fang",
                        [(ZIYU_TRIGGER_TARGET_DEATH, 1)])
    state, combat, player, enemy = _arena()
    item = _fang(state)
    note_action_targets(state, player, [enemy])

    _kill(combat, player, enemy)

    assert not enemy.is_alive
    assert (item.current_uses, item.max_uses) == (3, 5)
    events = _zi_yu_events(combat)
    assert len(events) == 1 and events[0].data["restored"] == 1


def test_target_death_unselected_enemy_does_not_fire(monkeypatch):
    """核心反例：没选中过的敌方死亡→不触发（仍是 2/5，无事件）。"""
    monkeypatch.setitem(CONSUMABLE_ZIYU_TRIGGERS, "test_fang",
                        [(ZIYU_TRIGGER_TARGET_DEATH, 1)])
    state, combat, player, enemy = _arena()
    item = _fang(state)

    _kill(combat, player, enemy)

    assert not enemy.is_alive
    assert (item.current_uses, item.max_uses) == (2, 5)
    assert _zi_yu_events(combat) == []


def test_target_death_selected_friend_fires(monkeypatch):
    """字面口径：只看选中集合不限敌我——选中过的友方死亡也触发。"""
    monkeypatch.setitem(CONSUMABLE_ZIYU_TRIGGERS, "test_fang",
                        [(ZIYU_TRIGGER_TARGET_DEATH, 1)])
    state, combat, player, enemy = _arena()
    friend = Entity("F", "朋友", blood_limit=8, current_hp=8)
    state.friends.append(friend)
    item = _fang(state)
    note_action_targets(state, player, [friend])

    friend.current_hp = 0
    assert combat._check_hp_zero_death(friend) is True

    assert not friend.is_alive
    assert (item.current_uses, item.max_uses) == (3, 5)
    assert len(_zi_yu_events(combat)) == 1


def test_target_death_idempotent_one_corpse_one_settle(monkeypatch):
    """幂等：同一尸体重复宣告死亡，只结算一次自愈（2/5→3/5，一个事件）。"""
    monkeypatch.setitem(CONSUMABLE_ZIYU_TRIGGERS, "test_fang",
                        [(ZIYU_TRIGGER_TARGET_DEATH, 1)])
    state, combat, player, enemy = _arena()
    item = _fang(state)
    note_action_targets(state, player, [enemy])

    _kill(combat, player, enemy)
    combat._on_entity_death(enemy)
    combat._check_hp_zero_death(enemy)

    assert (item.current_uses, item.max_uses) == (3, 5)
    assert len(_zi_yu_events(combat)) == 1


def test_note_action_targets_ignores_non_player_and_dedupes():
    """单元：朋友代选不记；重复选中去重。"""
    state, combat, player, enemy = _arena()
    friend = Entity("F", "朋友", blood_limit=8, current_hp=8)
    state.friends.append(friend)

    note_action_targets(state, friend, [enemy])
    assert state.player_selected_targets == []
    note_action_targets(state, player, [enemy, enemy, None])
    assert state.player_selected_targets == [enemy.runtime_id]


def test_resolve_attack_records_selection_and_death_fires(tmp_path, monkeypatch):
    """集成：真实出手选中→集合落笔→其死亡触发自愈（全链）。"""
    monkeypatch.setitem(CONSUMABLE_ZIYU_TRIGGERS, "test_fang",
                        [(ZIYU_TRIGGER_TARGET_DEATH, 1)])
    engine = _engine(tmp_path)
    engine.execute_action("setup_attributes", {
        "name": "测试者", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    finish_initial_daowen(engine)
    _open(engine)
    assert begin_battle(engine)["success"]
    assert begin_round(engine)["success"]
    item = _fang(engine.state)
    foe = engine.state.enemies[0]

    prepared = engine.execute_action("prepare_attack", {"actor_ref": "player:0"})
    assert prepared["success"], prepared
    opt = prepared["result"]["target_options"][0]
    selected_target_id = foe.runtime_id
    hits = [{"target_ref": opt["ref"], "dodge": False, "parry": False,
             "blood_shadow": False, "spell_choices": {"before": {}, "after": {}}}
            for _ in range(prepared["result"]["hit_count"])]
    resolved = engine.execute_action("resolve_attack", {
        "token": prepared["result"]["token"], "hits": hits})
    assert resolved["success"], resolved

    # 目标可能在resolve_attack内死亡并从存活目标映射移除，因此用攻击前捕获的身份核验。
    assert selected_target_id in engine.state.player_selected_targets

    # 选中过的怪经统一管线死亡→自愈触发。
    foe.current_hp = 0
    assert engine.combat._check_hp_zero_death(foe) is True
    assert (item.current_uses, item.max_uses) == (3, 5)


def test_battle_start_clears_selection(tmp_path):
    """正常路径：战始清空选中集合（上场选过的死了不算）。"""
    engine = _engine(tmp_path)
    engine.execute_action("setup_attributes", {
        "name": "测试者", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    finish_initial_daowen(engine)
    _open(engine)
    engine.state.player_selected_targets.append("stale-ref")

    started = begin_battle(engine)

    assert started["success"], started
    assert engine.state.player_selected_targets == []
