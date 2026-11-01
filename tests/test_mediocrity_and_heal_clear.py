"""【凡庸】特殊事件 与 [战终]清除回复 的回归测试

对应 规则正文：
  第500-501行 【凡庸】：任一角色连续五回合未出手／五回合未能使敌对角色生命减少，
               即触发；该角色全身炸裂[命零]，若为怪物则轮回者获得消耗品【自由结晶】(1/1)。
  第304行     [战终]：清除局内增益（包括回复、格挡、持续∞等）与减益（不包括代价）。
"""
import os
import sys

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.models import Entity  # noqa: E402
from engine.api import GameEngine  # noqa: E402

DB_DIR = "/tmp/linji_tests"
os.makedirs(DB_DIR, exist_ok=True)


def _monster(name="木桩", hp=200):
    return Entity(name=name, entity_type="怪物", blood_limit=hp, current_hp=hp,
                  speed_limit=0, current_speed=0, mana_limit=0, current_mana=0,
                  attack_count=1, attack_power=5)


def _engine(seed=7):
    e = GameEngine(db_path=f"{DB_DIR}/mediocrity.db", rng_seed=seed)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    e.execute_action("setup_choose_region", {"region": "罪孽都市"})
    return e


# ---------- 正常路径 ----------

def test_mediocrity_triggers_after_five_idle_rounds():
    """正常路径：连续五回合既未出手也未造成伤害 -> 触发【凡庸】命零"""
    e = _engine()
    cm = e.combat
    cm.state.enemies = [_monster()]
    m = cm.state.enemies[0]
    p = cm.state.player

    fired = None
    for r in range(1, 6):
        m.damage_dealt_this_round = 0
        m.actions_used_this_round = 0
        p.damage_dealt_this_round = 1      # 玩家有作为，不应被凡庸波及
        p.actions_used_this_round = 1
        res = cm.round_end()
        for ef in res.get("effects", []):
            if ef.get("type") == "mediocrity" and ef.get("entity") == m.name:
                fired = r

    assert fired == 5, f"【凡庸】应在第5个空转回合触发，实际={fired}"
    assert m.current_hp == 0 and not m.is_alive, "触发【凡庸】的怪物应[命零]"
    assert p.is_alive, "有作为的轮回者不应被【凡庸】波及"


def test_mediocrity_on_monster_grants_free_crystal():
    """正常路径：怪物因【凡庸】命零时，轮回者获得【自由结晶】(1/1)"""
    e = _engine()
    cm = e.combat
    cm.state.enemies = [_monster()]
    m = cm.state.enemies[0]
    before = len([c for c in cm.state.consumables if c.name == "自由结晶"])

    for _ in range(5):
        m.damage_dealt_this_round = 0
        m.actions_used_this_round = 0
        cm.state.player.damage_dealt_this_round = 1
        cm.state.player.actions_used_this_round = 1
        cm.round_end()

    crystals = [c for c in cm.state.consumables if c.name == "自由结晶"]
    assert len(crystals) == before + 1, "怪物触发【凡庸】应产出一件【自由结晶】"
    assert crystals[-1].current_uses == 1 and crystals[-1].max_uses == 1, "【自由结晶】应为 1/1"


# ---------- 边界条件 ----------

def test_idle_counter_resets_on_any_action():
    """边界：第5回合前只要出手过一次，计数归零，不触发【凡庸】"""
    e = _engine()
    cm = e.combat
    cm.state.enemies = [_monster()]
    m = cm.state.enemies[0]

    p = cm.state.player
    for r in range(1, 9):
        m.damage_dealt_this_round = 2      # 一直有伤害，隔离出"未出手"这一分支
        # 第4回合出手一次，打断连续空转
        m.actions_used_this_round = 1 if r == 4 else 0
        p.damage_dealt_this_round = 1   # 玩家保持有作为，隔离被测对象
        p.actions_used_this_round = 1
        res = cm.round_end()
        fired = [ef for ef in res.get("effects", [])
                 if ef.get("type") == "mediocrity" and ef.get("entity") == m.name]
        assert not fired, f"第{r}回合不应触发【凡庸】（计数应在第4回合归零）"
    # 第4回合打断后重新计数，到第8回合只积累了4个空转回合，仍不触发
    assert m.is_alive, "打断后重新计数，第8回合仍不该触发"
    assert m.no_action_rounds == 4


def test_mediocrity_non_reincarnator_fires_first():
    """正常：轮回者与怪物同时达阈值时，非轮回者先结算；怪物炸完战场清空，
    战斗胜负已定，轮回者的凡庸中断结算（DM裁定 2026-08-18）"""
    e = _engine()
    cm = e.combat
    cm.state.enemies = [_monster()]
    m = cm.state.enemies[0]
    p = cm.state.player
    friend = Entity(name="陪葬者", entity_type="朋友", blood_limit=30, current_hp=30,
                    attack_count=1, attack_power=1)
    cm.state.friends = [friend]

    fired = None
    interrupted = None
    for r in range(1, 6):
        for ent in (p, m, friend):
            ent.damage_dealt_this_round = 0
            ent.actions_used_this_round = 0
        res = cm.round_end()
        names = [ef.get("entity") for ef in res.get("effects", [])
                 if ef.get("type") == "mediocrity"]
        skips = [ef.get("entity") for ef in res.get("effects", [])
                 if ef.get("type") == "mediocrity_interrupted"]
        if names:
            fired = (r, names)
        if skips:
            interrupted = (r, skips)

    assert fired == (5, ["陪葬者", "木桩"]), \
        f"非轮回者优先结算；怪物炸完战场清空后轮回者不再炸裂，实际={fired}"
    assert interrupted == (5, ["贾凡"]), f"轮回者的凡庸应记为中断结算，实际={interrupted}"
    assert not m.is_alive and not friend.is_alive
    assert p.is_alive, "战场已清空，轮回者的凡庸应被中断，不得死亡"
    assert p.no_damage_rounds == 0 and p.no_action_rounds == 0, "中断者计数应清零"
    crystals = [c for c in cm.state.consumables if c.name == "自由结晶"]
    assert len(crystals) == 1, "怪物凡庸仍应产出自由结晶"
    assert e.state.last_death_cause != "mediocrity"


def test_mediocrity_interrupt_requires_cleared_battlefield():
    """边界：两只怪物只有一只达阈值时，剩余怪物仍存活（战斗未定），
    同拍达阈值的轮回者照常炸裂，不得援引中断裁定"""
    e = _engine()
    cm = e.combat
    cm.state.enemies = [_monster("空转怪"), _monster("勤快怪")]
    idle_m, busy_m = cm.state.enemies
    p = cm.state.player

    for _ in range(5):
        idle_m.damage_dealt_this_round = 0
        idle_m.actions_used_this_round = 0
        busy_m.damage_dealt_this_round = 3
        busy_m.actions_used_this_round = 1
        p.damage_dealt_this_round = 0
        p.actions_used_this_round = 0
        cm.round_end()

    assert not idle_m.is_alive, "空转怪应凡庸炸裂"
    assert busy_m.is_alive, "有作为的怪物不受波及"
    assert not p.is_alive, "战场未清空时，同拍达阈值的轮回者仍应炸裂"
    assert e.state.last_death_cause == "mediocrity"


def test_mediocrity_interrupt_covers_multiple_monsters():
    """边界：多只怪物同拍全部凡庸、战场清空后，轮回者与朋友都被中断豁免"""
    e = _engine()
    cm = e.combat
    cm.state.enemies = [_monster("怪甲"), _monster("怪乙")]
    p = cm.state.player
    friend = Entity(name="同伴", entity_type="朋友", blood_limit=30, current_hp=30,
                    attack_count=1, attack_power=1)
    cm.state.friends = [friend]

    for _ in range(5):
        for m in cm.state.enemies:
            m.damage_dealt_this_round = 0
            m.actions_used_this_round = 0
        p.damage_dealt_this_round = 0
        p.actions_used_this_round = 0
        # 朋友有出手且有伤害，不参与凡庸
        friend.damage_dealt_this_round = 1
        friend.actions_used_this_round = 1
        cm.round_end()

    assert all(not m.is_alive for m in cm.state.enemies), "两只怪都应凡庸炸裂"
    assert p.is_alive and friend.is_alive, "战场清空后我方无人炸裂"
    assert e.state.last_death_cause != "mediocrity"


def test_mediocrity_reincarnator_still_fires_when_alone():
    """边界：只有轮回者达阈值时，没有非轮回者也不阻挡其凡庸"""
    e = _engine()
    cm = e.combat
    cm.state.enemies = [_monster()]
    m = cm.state.enemies[0]
    p = cm.state.player

    for _ in range(5):
        p.damage_dealt_this_round = 0
        p.actions_used_this_round = 0
        m.damage_dealt_this_round = 3
        m.actions_used_this_round = 1
        cm.round_end()

    assert not p.is_alive, "单独达阈值的轮回者仍应凡庸"
    assert m.is_alive, "有作为的怪物不应被波及"


def test_mediocrity_priority_does_not_pull_unready_reincarnator():
    """错误输入：非轮回者优先不得把未满五回合的轮回者一并炸裂"""
    e = _engine()
    cm = e.combat
    cm.state.enemies = [_monster()]
    m = cm.state.enemies[0]
    p = cm.state.player
    for _ in range(4):
        m.damage_dealt_this_round = 0
        m.actions_used_this_round = 0
        p.damage_dealt_this_round = 1
        p.actions_used_this_round = 1
        cm.round_end()
    m.damage_dealt_this_round = 0
    m.actions_used_this_round = 0
    p.damage_dealt_this_round = 0
    p.actions_used_this_round = 0
    res = cm.round_end()
    names = [ef.get("entity") for ef in res.get("effects", [])
             if ef.get("type") == "mediocrity"]
    assert names == ["木桩"], f"只有满五回合的非轮回者应触发，实际={names}"
    assert not m.is_alive
    assert p.is_alive, "轮回者本拍才开始空转，不得被优先规则拖死"


def test_no_action_branch_triggers_even_if_damage_dealt():
    """边界：README 用"/"表示或——即便回回造成伤害，连续五回合未出手仍触发"""
    e = _engine()
    cm = e.combat
    cm.state.enemies = [_monster()]
    m = cm.state.enemies[0]

    for _ in range(6):
        m.damage_dealt_this_round = 3
        m.actions_used_this_round = 0
        cm.state.player.damage_dealt_this_round = 1
        cm.state.player.actions_used_this_round = 1
        cm.round_end()

    assert not m.is_alive, "连续五回合未出手，即便造成过伤害也应触发【凡庸】"
    assert m.no_damage_rounds == 0


# ---------- [战终] 清除回复类持续效果 ----------

def test_battle_end_clears_infinite_duration_buffs():
    """正常路径：[战终]清除局内持续效果（含持续∞的回复类增益）"""
    from engine.models import StatusEffect
    e = _engine()
    p = e.state.player
    p.status_effects.append(StatusEffect(name="测试增益", remaining_rounds=-1, value=2))
    p.status_effects.append(StatusEffect(name="杀伐", remaining_rounds=3, value=1))
    p.shield = 10

    e._action_battle_end({})

    names = [s.name for s in p.status_effects]
    assert "测试增益" not in names, "持续∞的局内增益应在[战终]清除"
    assert "杀伐" not in names, "局内持续增益应在[战终]清除"
    assert p.shield == 0, "格挡应在[战终]清空"


def test_battle_end_keeps_cost_status():
    """边界：[代价]不随[战终]清除（规则正文括注）"""
    from engine.models import StatusEffect
    e = _engine()
    p = e.state.player
    # 代价的现行名：生命／血限（旧名「流血」「衰老」已不参与结算）。
    p.status_effects.append(StatusEffect(name="生命", remaining_rounds=-1, value=3))
    p.status_effects.append(StatusEffect(name="血限", remaining_rounds=-1, value=2))
    p.status_effects.append(StatusEffect(name="庇护", remaining_rounds=2, value=1))

    e._action_battle_end({})

    names = [s.name for s in p.status_effects]
    assert "生命" in names and "血限" in names, "[代价]类不应被[战终]清除"
    assert "庇护" not in names, "非代价的局内增益应被清除"


def test_battle_end_reverts_in_battle_healing():
    """正常路径：战斗中[回复]的生命在[战终]吐出（跨场恢复只能靠局外【休整】）"""
    e = _engine()
    p = e.state.player
    p.blood_limit = 60
    p.current_hp = 30
    p.battle_start_hp = 30
    p.healed_this_battle = 0
    p.heal(20)
    assert p.current_hp == 50, "战斗中回复应即时生效"

    e._action_battle_end({})
    assert p.current_hp == 30, f"[战终]应吐出本场回复的生命，实际={p.current_hp}"
    assert p.healed_this_battle == 0


def test_battle_end_keeps_damage_taken():
    """边界：吐出回复时，战斗中受到的伤害如实保留，不被一并还原"""
    e = _engine()
    p = e.state.player
    p.blood_limit = 60
    p.current_hp = 40
    p.battle_start_hp = 40
    p.healed_this_battle = 0

    p.take_damage(25, "普通")   # 40 -> 15
    p.heal(20)                   # 15 -> 35
    assert p.current_hp == 35

    e._action_battle_end({})
    assert p.current_hp == 15, f"伤害应保留、只吐回复，实际={p.current_hp}"


def test_battle_end_heal_revert_never_kills():
    """错误输入/边界：吐出回复不得把轮回者压到[命零]，保底 1 点"""
    e = _engine()
    p = e.state.player
    p.blood_limit = 60
    p.current_hp = 5
    p.battle_start_hp = 60
    p.healed_this_battle = 0
    p.heal(3)                    # 5 -> 8，本场回复 3
    p.healed_this_battle = 50    # 人为放大，模拟大量回复后被打残

    e._action_battle_end({})
    assert p.current_hp == 1, f"吐出回复应保底 1 点，实际={p.current_hp}"
    assert p.is_alive, "[战终]吐出回复不应直接致死"


# ---------- 切割：失血同时扣除等量血限 ----------

def test_scar_cuts_blood_limit_with_life_loss():
    """正常路径：目标带伤痕时失去生命，血限同步扣除X（切割已删除，2026-08-21，伤痕保留同类机制）。"""
    from engine.models import StatusEffect
    e = _engine()
    cm = e.combat
    m = _monster(hp=200)
    cm.state.enemies = [m]
    p = cm.state.player
    m.add_status(StatusEffect(name="伤痕", remaining_rounds=-1, value=6))

    cm._apply_hostile_damage(m, 20, source=p)

    assert m.current_hp == 180
    assert m.blood_limit == 194


def test_removed_qiege_status_has_no_effect():
    """边界：切割道纹已删除（2026-08-21），残留切割状态不再触发任何血限扣除。"""
    from engine.models import StatusEffect
    e = _engine()
    cm = e.combat
    p = cm.state.player
    before = p.blood_limit
    m = _monster(hp=200)
    cm.state.enemies = [m]
    m.add_status(StatusEffect(name="切割", remaining_rounds=3, value=1))
    cm._apply_hostile_damage(m, 10, source=p)
    assert m.blood_limit == 200, "切割已删除：失血不扣除等量血限"


def test_removed_qiege_cannot_be_cast():
    """错误输入：切割已删除，无法持有或发动。"""
    e = _engine()
    from engine.models import DaoWen, DaoWenInstance
    e.state.player.dao_wen["切割"] = DaoWenInstance(
        DaoWen(name="切割", formula="", cost_type="消耗", cost_formula="3X", effect_formula=""))
    e.state.player.current_mana = 20
    e.state.phase = "in_combat"
    e.state.combat_subphase = "player_actions"
    r = e.execute_action("use_daowen", {
        "daowen_name": "切割", "x": 1,
        "dodge": False, "blood_shadow": False, "trigger_spell_choices": {},
    })
    assert not r["success"]
