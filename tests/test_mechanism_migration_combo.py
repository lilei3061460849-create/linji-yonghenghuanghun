"""组合机制行为验证（最终行为验证阶段新增）。

本文件不是迁移测试的替代品，而是迁移测试的**交叉盲区补强**：
每个迁移测试文件（test_*_migration.py）只验证单个机制 + 相邻一个机制的顺序。
本文件把 13 个已迁移机制放在**同场战斗的组合场景**里验证：

  - 完整回合一：回始 6 机制 -> 伤害管线（加害/叠鳞甲）-> 死亡 -> 焦黑发丝 -> 回终（畸变·结算）
  - 事件流形状（类型序列 / parent_event_id 链 / 数量）
  - 多实体类型（轮回者/朋友/员工/怪物）
  - RNG 确定性（同 seed 事件流逐字节一致；机制不得扰动 RNG）
  - 封印边界（BATTLE_START 双机制同时被封印）
  - 极端数值（0 HP / 超杀 / 血限1 / 法力 0 / 法力满）

全部复用既有参考实现/断言模式，不引入新框架。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.combat import CombatEngine
from engine.combat_events import CombatEventType
from engine.dice import DiceEngine
from engine.mechanisms import MECHANISMS, Phase
from engine.models import Entity, GameState, Relic, StatusEffect


# 2026-09-13 全局上限：当前法力不得超过[法限]。本文件多条用例验的是
# "某效果给了多少法力"，池子若开局就满，增量会被上限吃掉而看不出触发。
# 故夹具把[法限]放宽到 200，当前法力仍为 50 —— 被测的是增量，不是封顶。
def _player(hp=100, bl=None, mana=50, mana_limit=200, speed=5, speed_limit=20,
            entity_type="轮回者"):
    return Entity("P", entity_type, blood_limit=bl if bl is not None else hp,
                  current_hp=hp, mana_limit=mana_limit, current_mana=mana,
                  speed_limit=speed_limit, current_speed=speed)


def _monster(name="M", hp=50, bl=None, shards=0, ac=0, ap=0, entity_type="怪物"):
    return Entity(name, entity_type, blood_limit=bl if bl is not None else hp,
                  current_hp=hp, shards=shards, attack_count=ac, attack_power=ap)


def _arena(player=None, enemies=None, friends=None, employees=None,
           shards=0, relics=None, sealed=None):
    state = GameState(phase="in_combat", combat_subphase="player_actions")
    state.player = player or _player()
    state.enemies = enemies or [_monster()]
    state.friends = friends or []
    state.employees = employees or []
    state.shards = shards
    state.relics = relics or []
    state.sealed_relics.update(sealed or {})
    return state, CombatEngine(state, DiceEngine())


def _stream_types(combat):
    return [e.event_type for e in combat.event_stream]


def _add(entity, name, value=1, rounds=-1, source="x"):
    entity.add_status(StatusEffect(name=name, remaining_rounds=rounds,
                                   value=value, source=source))


# ==================== 1. 完整回合一（跨相位组合） ====================

def test_full_battle_cycle_all_mechanism_types_fire():
    """同场战斗中：回始5机制 + 伤害加减区 + 两个事件机制 + 回终结算全部生效。

    覆盖机制：洞察·结算/畸变·标记（ROUND_START；自愈、狂暴·标记、衰败已随道纹删除）、
    加害/叠鳞甲（INCOMING_ADJUST）、洗劫·夺碎片/焦黑发丝（事件）、畸变·结算（ROUND_END）。
    """
    state, combat = _arena(shards=0, relics=[Relic("焦黑发丝", "")])
    player = state.player
    # 玩家：回始机制（衰败已删，回始只剩 洞察·结算）
    player._dongcha_pending = 4
    _add(player, "狂暴", 1)
    _add(player, "洗劫", 2)
    # 敌人：伤害加减区 + 回终畸变
    enemy = state.enemies[0]
    enemy.shards = 20
    _add(enemy, "加害", 2)
    enemy.relics.append(Relic("叠鳞甲", ""))
    _add(enemy, "畸变", 1)
    enemy.attack_count = 2
    enemy.attack_power = 3

    # 回始：洞察（狂暴·标记 2026-10-03、自愈与衰败 2026-10-11 随道纹删除）
    # （DM裁定 2026-09-09：法力一池制，mana_refill 已不在回始管道里）
    res = combat.round_start()
    p_types = [e.get("type") for e in res["effects"] if e.get("entity") == "P"]
    assert p_types == ["dongcha_mana"], f"回始顺序: {p_types}"

    # 回始数值链（P: hp100 满血；自愈与衰败均已删除，回始不再改动生命）
    assert player.current_hp == 100, f"P hp={player.current_hp}"
    # mana: 50(初始) + 50(回填) + 4(洞察) = 104（无不朽之躯 -> 不钳制）
    # 一池制：回始少了 +法限50 的回填，故 104 → 54
    assert player.current_mana == 54, f"P mana={player.current_mana}"

    # 玩家打敌人 1 点：加害 +2 -> 3，叠鳞甲 -1 -> 实伤 2；洗劫按实伤 2 夺碎片
    dmg = combat._apply_hostile_damage(enemy, 1, source=player)
    assert dmg["actual_damage"] == 2, f"加害+叠鳞甲调整后实伤={dmg['actual_damage']}"
    assert enemy.shards == 18, f"洗劫夺 2 碎片: {enemy.shards}"
    assert state.shards == 2

    # 击杀敌人 -> 焦黑发丝 +2 速度
    speed_before = player.current_speed
    combat._apply_hostile_damage(enemy, enemy.current_hp + 10, source=player)
    assert enemy.is_alive is False
    assert player.current_speed == speed_before + 2, "焦黑发丝必须在组合场景生效"

    # 回终：畸变·结算条目（此时敌人已死 -> 不产生条目；玩家无畸变）
    res_end = combat.round_end()
    deform = [e for e in res_end["effects"] if e.get("type") == "deform_blood_limit_loss"]
    assert deform == [], "已死实体不结算畸变"


def test_full_cycle_enemy_survives_to_round_end_jibian_settles():
    """敌人未死时回终畸变·结算照常扣血限；且回始的畸变·标记预告与结算数值一致。"""
    state, combat = _arena()
    enemy = state.enemies[0]
    enemy.blood_limit = 50
    enemy.current_hp = 50
    enemy.attack_count = 2
    enemy.attack_power = 3
    _add(enemy, "畸变", 1)

    # 回始标记：blood_loss = 原始乘积 2*3=6
    res_start = combat.round_start()
    pending = next((e for e in res_start["effects"] if e.get("type") == "deform_pending"
                    and e.get("entity") == "M"), None)
    assert pending is not None and pending["blood_loss"] == 6

    # 回终结算：max(0, 2*3)=6 -> 血限 50->44
    res_end = combat.round_end()
    deform = next((e for e in res_end["effects"] if e.get("type") == "deform_blood_limit_loss"
                   and e.get("entity") == "M"), None)
    assert deform is not None and deform["blood_loss"] == 6 and deform["blood_limit_after"] == 44
    assert enemy.blood_limit == 44 and enemy.current_hp == 44


# ==================== 2. 事件流形状（parent_event_id 链 / 顺序 / 数量） ====================

def test_damage_death_event_chain_shape_and_order():
    """一次致死伤害的事件流：DAMAGE_APPLIED -> ENTITY_DIED，死亡事件 parent 指向伤害事件。"""
    state, combat = _arena(relics=[Relic("焦黑发丝", "")])
    player = state.player
    enemy = state.enemies[0]
    enemy.blood_limit = 10
    enemy.current_hp = 10
    _add(player, "洗劫", 2)
    enemy.shards = 6

    combat._apply_hostile_damage(enemy, 10, source=player)

    stream = combat.event_stream
    types = _stream_types(combat)
    assert types == [CombatEventType.DAMAGE_APPLIED, CombatEventType.ENTITY_DIED], \
        f"事件类型序列: {types}"

    dmg_ev = stream[0]
    died_ev = stream[1]
    assert died_ev.ctx.get("parent_event_id") == dmg_ev.ctx.get("event_id"), \
        "ENTITY_DIED 的 parent_event_id 必须指向 DAMAGE_APPLIED 事件"
    assert dmg_ev.ctx.get("subtype") == "normal", f"伤害 subtype={dmg_ev.ctx.get('subtype')}"
    assert died_ev.ctx.get("subtype") == "hp_zero"


def test_multi_hit_three_damage_events_each_steals():
    """多段攻击：每段伤害各发一次 DAMAGE_APPLIED、各触发一次洗劫夺碎片。"""
    state, combat = _arena(shards=0)
    player = state.player
    enemy = state.enemies[0]
    enemy.shards = 30
    _add(player, "洗劫", 2)

    for _ in range(3):
        combat._apply_hostile_damage(enemy, 4, source=player)

    dmg_events = [e for e in combat.event_stream
                  if e.event_type == CombatEventType.DAMAGE_APPLIED]
    assert len(dmg_events) == 3, f"多段攻击事件数: {len(dmg_events)}"
    assert enemy.shards == 18, f"三段各夺 4: {enemy.shards}"
    assert state.shards == 12


# ==================== 3. 多实体类型 ====================

def test_friend_and_employee_round_start_mechanisms_fire():
    """朋友/员工（已部署）与轮回者同场：回始机制对各实体独立结算。"""
    friend = Entity("F", "朋友", blood_limit=100, current_hp=100,
                    mana_limit=50, current_mana=50)
    # 2026-10-11：【衰败】已删。回始相位现只剩 洞察·结算（只对轮回者结算）与 畸变·标记，
    # 朋友侧已无任何回始机制可触发——这里改为钉住这个事实，而不是假造一个机制。
    employee = Entity("W", "员工", blood_limit=80, current_hp=50,
                      mana_limit=50, current_mana=50, is_deployed=True)
    state, combat = _arena(friends=[friend], employees=[employee])
    state.player = _player(hp=100)

    res = combat.round_start()

    f_types = [e.get("type") for e in res["effects"] if e.get("entity") == "F"]
    assert f_types == [], f"衰败删除后朋友侧不应再有回始机制条目: {f_types}"
    assert friend.current_hp == 100, f"朋友 hp={friend.current_hp}"

    w_types = [e.get("type") for e in res["effects"] if e.get("entity") == "W"]
    # 【自愈】已删除：员工不再因回始回血，生命保持 50
    assert "self_heal" not in w_types, f"自愈已删除，不应出现: {w_types}"
    assert employee.current_hp == 50, f"员工 hp={employee.current_hp}"

    # 未部署的员工不进入战场循环
    employee2 = Entity("W2", "员工", blood_limit=80, current_hp=80,
                       mana_limit=50, current_mana=50, is_deployed=False)
    state2, combat2 = _arena(employees=[employee2])
    combat2.round_start()
    assert employee2.current_hp == 80, "未部署员工不得结算回始机制"


def test_monster_type_only_for_entity_type_conditions():
    """entity_type 条件：洞察只认轮回者；焦黑发丝只认怪物死亡。"""
    state, combat = _arena()
    player = state.player
    # 怪物挂着洞察 pending：不能触发（entity_type 门闩）
    enemy = state.enemies[0]
    enemy._dongcha_pending = 9
    res = combat.round_start()
    m_types = [e.get("type") for e in res["effects"] if e.get("entity") == "M"]
    assert "dongcha_mana" not in m_types, m_types
    assert enemy.current_mana == 0 and getattr(enemy, "_dongcha_pending", 0) == 9, \
        "怪物不得结算轮回者机制"

    # 玩家死亡不触发焦黑发丝（只认怪物）
    state.relics = [Relic("焦黑发丝", "")]
    sp_before = player.current_speed
    combat._apply_hostile_damage(player, 200, source=enemy)
    assert player.is_alive is False
    assert player.current_speed == sp_before, "轮回者死亡不得触发焦黑发丝"


# ==================== 4. INCOMING_ADJUST 真实伤害管线 ====================

def test_incoming_adjust_real_damage_pipeline_matrix():
    """加害/叠鳞甲在真实伤害管线中的组合矩阵（含 0 / 封底 / 代价伤害）。"""
    cases = [
        ((), 8, "普通", 8),                           # 无调整
        ((("加害", 2),), 8, "普通", 10),              # 只有加害
        ((("叠鳞甲", 1),), 8, "普通", 7),             # 遗物固定减1
        ((("叠鳞甲", 1),), 1, "普通", 0),             # 最低为0
        ((("加害", 2), ("叠鳞甲", 1)), 8, "普通", 9), # 加害先于叠鳞甲
        ((("加害", 5), ("叠鳞甲", 1)), 8, "普通", 12),
        ((("加害", 2), ("叠鳞甲", 1)), 8, "代价", 8),# 两者都不调整代价
        ((("加害", 2),), 0, "普通", 0),               # amount<=0 不改
    ]
    for statuses, raw, dtype, expected in cases:
        state, combat = _arena()
        enemy = state.enemies[0]
        for name, val in statuses:
            if name == "叠鳞甲":
                enemy.relics.append(Relic(name, ""))
            else:
                _add(enemy, name, val)
        # 用 combat._apply_hostile_damage 的 damage_type 参数走真实管线
        detail = combat._apply_hostile_damage(
            enemy, raw, source=state.player, damage_type=dtype)
        actual = detail["actual_damage"]
        assert actual == expected, \
            f"statuses={statuses} raw={raw} dtype={dtype}: actual={actual} expected={expected}"


# ==================== 5. RNG 确定性 ====================

def test_same_seed_identical_event_stream_and_state():
    """同 seed 跑同一场景：事件流与实体终态逐项一致（机制不得扰动 RNG）。"""
    def run(seed):
        state, combat = _arena(shards=0, relics=[Relic("焦黑发丝", "")])
        player = state.player
        _add(player, "洗劫", 2)
        enemy = state.enemies[0]
        enemy.blood_limit = 20
        enemy.current_hp = 20
        enemy.shards = 9
        _add(enemy, "衰败", 2)
        _add(enemy, "畸变", 1)
        enemy.attack_count = 2
        enemy.attack_power = 3
        combat.dice = DiceEngine(seed=seed)
        combat.round_start()
        combat._apply_hostile_damage(enemy, 4, source=player)
        combat.round_end()
        return (
            [(e.event_type.value, e.actor_name, e.target_name, e.data)
             for e in combat.event_stream],
            (player.current_hp, player.current_mana, player.current_speed,
             player.current_speed, state.shards),
            (enemy.current_hp, enemy.blood_limit, enemy.shards, enemy.is_alive),
        )

    a = run(777)
    b = run(777)
    assert a == b, "同 seed 两次运行必须完全一致"
    # 不同 seed 也应产生一致的机制结果（机制本身不消费 RNG；唯一随机源是骰子）
    c = run(778)
    assert a[1] == c[1] and a[2] == c[2], "机制结果不得依赖 seed（机制不吃随机数）"


# ==================== 6. 封印边界 ====================

def test_battle_start_both_relics_sealed_no_trigger():
    """缄默面具 + 帮派令同时被封印：战始不产生任何日志与状态。"""
    state, combat = _arena(relics=[Relic("缄默面具", ""), Relic("帮派令", "")],
                           sealed={"缄默面具": 1, "帮派令": 2})
    player = state.player
    state.event_modifiers["silent_mask_x"] = 3

    logs = combat.process_relics("battle_start", {"relic_choices": {}})
    assert logs == [], f"封印后日志: {logs}"
    assert player.current_mana == 50, "封印的缄默面具不得加法力"
    assert not player.has_status("洗劫"), "封印的帮派令不得授予洗劫"


def test_battle_start_partial_seal_one_fires_one_skips():
    """帮派令被封印、缄默面具未封印：战始只激活面具，后续法限增量翻倍。"""
    state, combat = _arena(relics=[Relic("缄默面具", ""), Relic("帮派令", "")],
                           sealed={"帮派令": 2})
    player = state.player

    logs = combat.process_relics("battle_start", {"relic_choices": {}})
    assert logs == ["缄默面具：后续法限增加量翻倍"], logs
    assert not player.has_status("洗劫"), "封印的帮派令不得授予洗劫"
    limit_before = player.mana_limit
    assert combat.process_relics("battle_end") == []
    assert player.mana_limit == limit_before, "面具激活与战终都不直接加法限"
    player.mana_limit += 2
    assert player.mana_limit == limit_before + 4
    assert not player.has_status("洗劫"), "封印的帮派令仍不得授予洗劫"


# ==================== 7. 极端数值边界 ====================

def test_extreme_zero_hp_and_overkill():
    """0 HP 起始 / 超杀：伤害管线与事件机制保持既有一致语义。"""
    # 0 HP 起始的敌人：伤害后负值被钳制到 0，死亡事件照常
    state, combat = _arena(relics=[Relic("焦黑发丝", "")])
    enemy = state.enemies[0]
    enemy.blood_limit = 1
    enemy.current_hp = 0
    sp_before = state.player.current_speed
    combat._apply_hostile_damage(enemy, 100, source=state.player)
    assert enemy.current_hp == 0 and enemy.is_alive is False
    assert state.player.current_speed == sp_before + 2
    assert [e.event_type for e in combat.event_stream] == \
        [CombatEventType.DAMAGE_APPLIED, CombatEventType.ENTITY_DIED]


def test_extreme_blood_limit_one_and_mana_bounds():
    """法力 0 / 法力满 / 勾魂：回始机制在边界值下的行为。"""
    # 2026-10-11：原「血限1 + 衰败 → 命零」边界子段随【衰败】删除而移除
    # （衰败是它唯一的驱动机制）。回始相位现为 洞察·结算 → 畸变·标记，
    # 两者都不直接扣血，血限1 的命零边界改由伤害管线用例覆盖。

    # 勾魂（2026-10-08 用户令）：无法获得[法力]；挂状态/回始都不动已有法力
    state2, combat2 = _arena()
    p2 = state2.player
    p2.current_mana = 7
    _add(p2, "勾魂", 1, rounds=2)
    res2 = combat2.round_start()
    assert [e for e in res2["effects"] if e.get("type") == "mana_refill_blocked"] == []
    assert p2.current_mana == 7, f"回始不动已有法力: {p2.current_mana}"
    assert p2.can_gain_mana() is False, "勾魂期间无法获得法力"
    assert p2.gain_mana(4) == 0
    assert p2.current_mana == 7, f"增益应被压死，实剩 {p2.current_mana}"
    assert p2.spend_mana(2) is True
    assert p2.current_mana == 5, f"勾魂不改消耗倍率，实剩 {p2.current_mana}"

    # 法力满 + 洞察 pending：
    #   2026-09-13 用户裁定「所有属性不得超过其上限」全局化后，
    #   持不持有【不朽之躯】结果相同——都封顶到[法限]。
    state3, combat3 = _arena()
    p3 = state3.player
    p3.mana_limit = 50    # 本例专验封顶，显式把[法限]收回 50
    p3.current_mana = 50  # 已满
    p3._dongcha_pending = 20
    combat3._dispatch_phase(Phase.ROUND_START, target=p3)
    assert p3.current_mana == p3.mana_limit, f"2026-09-13 全局上限：无不朽之躯也封顶到法限: {p3.current_mana}"

    #   有不朽之躯（side_has 识别的是遗物/初拥/龙族条目，不是状态）-> 同样钳制到法限
    state4, combat4 = _arena(relics=[Relic("不朽之躯", "")])
    p4 = state4.player
    p4.mana_limit = 50
    p4.current_mana = 50
    p4._dongcha_pending = 20
    combat4._dispatch_phase(Phase.ROUND_START, target=p4)
    assert p4.current_mana == 50, f"不朽之躯必须钳制: {p4.current_mana}"




# ==================== 8. 事件流黄金序列（类型/数量/顺序全量锁定） ====================

def test_golden_event_sequence_scripted_round():
    """脚本化回合的事件流黄金序列：精确锁定 事件类型/数量/顺序。

    场景：回始（玩家有洗劫）-> 打 5 点 -> 再打 10 点致死 -> 回终。

    2026-10-11：【衰败】已删除，回始不再产生 tick 伤害，事件流少一条 DAMAGE_APPLIED，
    数值链由 15→13→8→0 改为 15→10→0。
    """
    state, combat = _arena(shards=0, relics=[Relic("焦黑发丝", "")])
    player = state.player
    enemy = state.enemies[0]
    enemy.blood_limit = 15
    enemy.current_hp = 15
    enemy.shards = 20
    _add(player, "洗劫", 2)

    combat.round_start()                  # 衰败已删：回始不再造伤，hp 仍 15
    combat._apply_hostile_damage(enemy, 5, source=player)   # 洗劫夺 5 -> hp 10（DAMAGE_APPLIED #1）
    combat._apply_hostile_damage(enemy, 10, source=player)  # 致死 -> hp 0（DAMAGE_APPLIED #2, ENTITY_DIED）
    combat.round_end()

    stream = [(e.event_type.value, e.actor_name, e.target_name) for e in combat.event_stream]
    expected = [
        ("damage_applied", "P", "M"),    # #1 玩家打 5
        ("damage_applied", "P", "M"),    # #2 玩家打 10
        ("entity_died", "P", "M"),       # 死亡（actor=击杀者）
    ]
    assert stream == expected, f"事件流: {stream}"

    # 事件数量精确
    assert len([e for e in combat.event_stream
                if e.event_type == CombatEventType.DAMAGE_APPLIED]) == 2
    assert len([e for e in combat.event_stream
                if e.event_type == CombatEventType.ENTITY_DIED]) == 1
    # 死亡事件的 parent 链指向最后一次伤害
    dmg_last = [e for e in combat.event_stream
                if e.event_type == CombatEventType.DAMAGE_APPLIED][1]   # 衰败 tick 已删，只剩 2 条
    died = next(e for e in combat.event_stream
                if e.event_type == CombatEventType.ENTITY_DIED)
    assert died.ctx.get("parent_event_id") == dmg_last.ctx.get("event_id")
    # 洗劫总量 = 5 + 10 = 15（夺碎片按每次实伤）
    assert state.shards == 15, f"洗劫总量: {state.shards}"
    assert enemy.shards == 5, f"剩余: {enemy.shards}"  # 20-5-10


def test_multi_death_jiaohheifasi_fires_per_monster():
    """两个怪物先后死亡：焦黑发丝按怪物数量各触发一次（+2 速度 × 2）。"""
    state, combat = _arena(relics=[Relic("焦黑发丝", "")])
    player = state.player
    m1 = state.enemies[0]
    m1.blood_limit = 5
    m1.current_hp = 5
    m2 = Entity("M2", "怪物", blood_limit=5, current_hp=5)
    state.enemies = [m1, m2]

    sp0 = player.current_speed
    combat._apply_hostile_damage(m1, 10, source=player)
    assert player.current_speed == sp0 + 2, f"第一只死亡后速度={player.current_speed}"
    combat._apply_hostile_damage(m2, 10, source=player)
    assert player.current_speed == sp0 + 4, f"第二只死亡后速度={player.current_speed}"

    died = [e for e in combat.event_stream if e.event_type == CombatEventType.ENTITY_DIED]
    assert len(died) == 2, "两个死亡事件"
    assert len([e for e in died if e.target_name == "M"]) == 1
    assert len([e for e in died if e.target_name == "M2"]) == 1


def test_gangpailing_wash_steals_in_same_battle():
    """帮派令战始授予洗劫 -> 本场战斗伤害立即夺碎片（跨相位组合：BATTLE_START -> DAMAGE_APPLIED）。"""
    state, combat = _arena(relics=[Relic("帮派令", "")], shards=0)
    player = state.player
    enemy = state.enemies[0]
    enemy.shards = 10

    # 战始：帮派令触发
    logs = combat.process_relics("battle_start", {"relic_choices": {}})
    assert logs == ["帮派令：获得洗劫3"]
    assert player.has_status("洗劫")

    # 本场第一次伤害立即夺碎片
    combat._apply_hostile_damage(enemy, 4, source=player)
    assert enemy.shards == 6, f"夺 4: {enemy.shards}"
    assert state.shards == 4
