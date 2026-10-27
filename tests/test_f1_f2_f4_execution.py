"""F 裁定执行守护（2026-10-14，见 `三类物品审计.md` F1/F2/F4）。

- F1：【猩红果实】重做为纯被动（生命可超血限），旧效果移交道纹【增殖】
  （增殖X = 衰老X → 目标无限血限持续X）。
- F2：【余火印】删除（extra 整段删，不并入）；【搏命】删除，疲惫换法力并入【透支】
  四通道（流血4X/衰老2X/枯竭X/疲惫X 四选一 → X法力，touzhi_cost 指定，缺省流血）；
  龙心谷事件「逆行者·接过余火」改授【透支】道纹。
- F4：镇魂铁钉/替死鬼/赶尸铃/黄符 处理器补齐。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.enums import TriggerTiming
from engine.models import Consumable, DaoWen, DaoWenInstance, Entity, Relic, Spell
from tests.setup_support import finish_initial_daowen


def _setup():
    engine = GameEngine(rng_seed=42)
    engine.execute_action("setup_attributes", {"blood_points": 11, "speed_points": 6, "mana_points": 8})
    finish_initial_daowen(engine)
    engine.state.current_region = "罪孽都市"
    engine.state.phase = "in_combat"
    engine.state.relics = []
    return engine


def _grant(engine, names):
    for n in names:
        engine.state.player.dao_wen[n] = DaoWenInstance(
            DaoWen(name=n, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))


def _add_monster(engine, hp=100):
    m = Entity(name="靶怪", entity_type="怪物", blood_limit=hp, current_hp=hp,
               attack_count=1, attack_power=5)
    engine.state.enemies.append(m)
    return m


# ==================== F1：增殖 ====================

def test_zengzhi_cast_pays_aging_without_pending():
    engine = _setup()
    player = engine.state.player
    _grant(engine, ["增殖"])
    m = _add_monster(engine)
    hp0 = player.current_hp
    r = engine.execute_action("use_daowen", {"daowen_name": "增殖", "x": 2, "target_ref": "enemy:0"})
    assert r["success"], r
    assert player.blood_limit == hp0 - 2  # 衰老X 即付
    assert m.blood_limit == 100  # 战终前不涨
    assert "zengzhi_pending" not in engine.state.event_modifiers
    assert m.has_status("无限血限")


def test_zengzhi_settles_at_battle_end_and_stacks():
    engine = _setup()
    player = engine.state.player
    _grant(engine, ["增殖"])
    f = Entity("友", "朋友", blood_limit=30, current_hp=30)
    engine.state.friends = [f]
    m = _add_monster(engine)
    assert engine.execute_action(
        "use_daowen", {"daowen_name": "增殖", "x": 1, "target_ref": "friend:0"})["success"]
    assert engine.execute_action(
        "use_daowen", {"daowen_name": "增殖", "x": 2, "target_ref": "friend:0"})["success"]
    m.is_alive = False
    engine.state.enemies = []
    end = engine.execute_action("battle_end", {})
    assert end["success"], end
    assert end["result"]["zengzhi_settled"] == []
    assert f.blood_limit == 30
    assert not f.has_status("无限血限")


def test_zengzhi_settle_fizzles_on_dead_target_and_immortal_body():
    engine = _setup()
    _grant(engine, ["增殖"])
    m = _add_monster(engine)
    assert engine.execute_action(
        "use_daowen", {"daowen_name": "增殖", "x": 1, "target_ref": "enemy:0"})["success"]
    m.is_alive = False  # 战终前死亡 → 落空
    engine.state.enemies = []
    end = engine.execute_action("battle_end", {})
    assert end["success"]
    assert end["result"]["zengzhi_settled"] == []

    # 不朽之躯：结算时阻挡
    engine2 = _setup()
    _grant(engine2, ["增殖"])
    p2 = engine2.state.player
    engine2.state.relics = [Relic("不朽之躯", "")]
    m2 = _add_monster(engine2)
    assert engine2.execute_action(
        "use_daowen", {"daowen_name": "增殖", "x": 1, "target_ref": "player:0"})["success"]
    m2.is_alive = False
    engine2.state.enemies = []
    end2 = engine2.execute_action("battle_end", {})
    assert end2["result"]["zengzhi_settled"] == []
    assert not p2.has_status("无限血限")


def test_zengzhi_pending_void_on_escape():
    engine = _setup()
    _grant(engine, ["增殖"])
    _add_monster(engine)
    assert engine.execute_action(
        "use_daowen", {"daowen_name": "增殖", "x": 1, "target_ref": "enemy:0"})["success"]
    assert "zengzhi_pending" not in engine.state.event_modifiers
    from engine.models import StatusEffect
    engine.state.player.add_status(StatusEffect(name="疯狂", value=4, remaining_rounds=-1, source="test"))
    engine.state.player.actions_used_this_round = 0
    esc = engine.execute_action("declare_escape", {})
    assert esc["success"], esc
    assert "zengzhi_pending" not in engine.state.event_modifiers


def test_scarlet_relic_has_no_battle_start_decision():
    """猩红重做后：战始无决策（空 choices 直接通过），戦始不扣血。"""
    engine = _setup()
    player = engine.state.player
    engine.state.relics = [Relic("猩红果实", "")]
    engine.combat.validate_battle_start_relic_choices({})
    logs = engine.combat.process_relics(TriggerTiming.BATTLE_START, {"relic_choices": {}})
    assert player.current_hp == player.blood_limit, logs
    assert not any("猩红" in log for log in logs)


# ==================== F2：透支·四通道 ====================
# 2026-10-14 追裁定：【余火印】extra 整段删除（yuhuo_heart_name 不再存在）；
# 【搏命】删除并入【透支】，透支X = 四选一（流血4X/衰老2X/枯竭X/疲惫X）→ X法力。

def _cast_touzhi(cost_channel=None, x=1, extra=None):
    engine = _setup()
    player = engine.state.player
    _grant(engine, ["透支"])
    player.current_mana = 0
    _add_monster(engine)
    params = {"daowen_name": "透支", "x": x}
    if cost_channel is not None:
        params["touzhi_cost"] = cost_channel
    if extra:
        params.update(extra)
    snap = {"hp": player.current_hp, "bl": player.blood_limit,
            "ml": player.mana_limit, "sp": player.current_speed}
    r = engine.execute_action("use_daowen", params)
    return engine, player, snap, r


def test_touzhi_defaults_to_bleed_channel():
    """正常路径：缺省通道=流血4X → X法力。"""
    engine, player, snap, r = _cast_touzhi()
    assert r["success"], r
    assert player.current_hp == snap["hp"] - 4
    assert player.current_mana == 1
    assert player.blood_limit == snap["bl"]  # 其它通道不动


def test_touzhi_aging_channel_costs_blood_limit():
    """正常路径：衰老通道 衰老2X → X法力。"""
    engine, player, snap, r = _cast_touzhi("衰老")
    assert r["success"], r
    assert player.blood_limit == snap["bl"] - 2
    assert player.current_hp == snap["bl"] - 2  # 血限动词走统一入口：生命随血限封顶
    assert player.current_mana == 1


def test_touzhi_exhaust_channel_costs_mana_limit():
    """正常路径：枯竭通道 枯竭X → X法力。"""
    engine, player, snap, r = _cast_touzhi("枯竭")
    assert r["success"], r
    assert player.mana_limit == snap["ml"] - 1
    assert player.current_mana == 1  # 先扣法限再涨法力，1 点仍到账


def test_touzhi_fatigue_channel_costs_speed():
    """正常路径：疲惫通道（原【搏命】）疲惫X → X法力，1:1。"""
    engine, player, snap, r = _cast_touzhi("疲惫")
    assert r["success"], r
    assert player.current_speed == snap["sp"] - 1
    assert player.current_mana == 1
    types = [fx.get("type") for fx in r["execution"]["effects"]]
    assert "fatigue_cost" in types and "mana_gain" in types


def test_touzhi_heart_offsets_same_channel_cost():
    """正常路径：龙心通用抵扣同类代价（衰老龙心抵衰老通道）。"""
    engine = _setup()
    player = engine.state.player
    _grant(engine, ["透支"])
    player.current_mana = 0
    engine.state.consumables = [
        Consumable("衰老龙心", "", 5, 5, kind="dragon_heart", dragon_heart_type="衰老")]
    _add_monster(engine)
    bl0 = player.blood_limit
    r = engine.execute_action("use_daowen", {"daowen_name": "透支", "x": 1,
                                             "touzhi_cost": "衰老", "dragon_heart_use": 1})
    assert r["success"], r
    fx = next(e for e in r["execution"]["effects"] if e["type"] == "aging_cost")
    assert fx["dragon_heart_offset"] == 1
    assert player.blood_limit == bl0 - 1, "2点代价被抵消1点，实付1点"
    assert engine.state.consumables[0].current_uses == 4
    assert player.current_mana == 1


def test_touzhi_rejects_illegal_channel_whole_cast():
    """错误输入：非法通道整单拒绝，代价与法力都不结算。"""
    engine, player, snap, r = _cast_touzhi("异变")
    assert not r["success"]
    assert player.current_hp == snap["hp"]
    assert player.current_mana == 0


def test_touzhi_fatigue_channel_cannot_outpace_chaopin():
    """核心约束（原【搏命】永动护栏）：透支·疲惫 + 超频的闭环必须净亏速度。

    倍率 k=1（疲惫X→X法力）：卖 X 速度得 X 法力，经超频买回 X/2 速度，
    净变化恒负，不得永动。改倍率前先看这条。
    """
    from engine.daowen import DaoWenEngine
    DaoWenEngine.register_all()
    for x in (1, 2, 4, 8):
        sold = DaoWenEngine.resolve("透支", x, target=None, caster=None, cost_channel="疲惫")
        assert sold["cost_type"] == "疲惫" and sold["cost_speed"] == x
        assert sold["mana_gain"] == x
        mana = sold["mana_gain"]
        bought = 0
        while DaoWenEngine.resolve("超频", bought + 1, target=None,
                                   caster=None)["cost"] <= mana:
            bought += 1
        net = bought - sold["cost_speed"]
        assert net < 0, (f"透支·疲惫{x}卖{sold['cost_speed']}速度换{mana}法力，"
                         f"经超频买回{bought}速度，净{net:+d}——非负即可无限永动")



def test_nixingzhe_grants_touzhi_daowen():
    e = GameEngine(db_path="/tmp/test_f2_nixingzhe.db", rng_seed=1)
    e.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    e.execute_action("setup_choose_region", {"region": "龙心谷"})
    e.state.phase = "pre_battle"
    e.event_pool.current = "逆行者"
    assert "透支" not in e.state.player.dao_wen
    hp0 = e.state.player.current_hp
    r = e.execute_action("resolve_event", {"event": "逆行者", "option_id": 2})
    assert r["success"], r
    assert e.state.player.current_hp == hp0 - 10
    assert "透支" in e.state.player.dao_wen


# ==================== F4：四件处理器 ====================

def test_zhenhun_nail_shackles_and_optional_zero_speed():
    engine = _setup()
    engine.state.consumables = [Consumable("镇魂铁钉", "束缚", 3, 3)]
    m = _add_monster(engine)
    r = engine.execute_action("consume_item", {"name": "镇魂铁钉", "target_ref": "enemy:0"})
    assert r["success"], r
    assert m.has_status("束缚")
    assert engine.state.consumables[0].current_uses == 2
    assert not engine.combat.can_act(m)
    # 非轮回者目标拒绝速度归零
    r = engine.execute_action(
        "consume_item", {"name": "镇魂铁钉", "target_ref": "enemy:0", "zero_speed": True})
    assert not r["success"]

    # 轮回者目标：额外-1耐久，速度归零
    engine2 = _setup()
    engine2.state.consumables = [Consumable("镇魂铁钉", "束缚", 3, 3)]
    foe = Entity("敌对轮回者", "轮回者", blood_limit=60, current_hp=60,
                 speed_limit=5, current_speed=5, attack_count=1, attack_power=5)
    engine2.state.enemies = [foe]
    r = engine2.execute_action(
        "consume_item", {"name": "镇魂铁钉", "target_ref": "enemy:0", "zero_speed": True})
    assert r["success"], r
    assert foe.current_speed == 0
    assert engine2.state.consumables[0].current_uses == 1


def test_tishigui_transfers_lethal_damage_to_attacker():
    engine = _setup()
    player = engine.state.player
    player.current_hp = 10
    engine.state.consumables = [Consumable("替死鬼", "转移致死伤", 1, 1)]
    m = _add_monster(engine)
    d = engine.combat._apply_hostile_damage(player, 50, "普通", m)
    assert player.current_hp == 10
    assert m.current_hp == 50
    assert engine.state.consumables[0].current_uses == 0
    assert d["tishigui_transfer"]["transferred"] == 50

    # 非致死不触发
    engine2 = _setup()
    p2 = engine2.state.player
    p2.current_hp = 60
    engine2.state.consumables = [Consumable("替死鬼", "转移致死伤", 1, 1)]
    m2 = _add_monster(engine2)
    d2 = engine2.combat._apply_hostile_damage(p2, 50, "普通", m2)
    assert p2.current_hp == 10
    assert "tishigui_transfer" not in d2
    assert engine2.state.consumables[0].current_uses == 1


def test_ganshi_bell_summons_two_and_dissolves_at_battle_end():
    engine = _setup()
    engine.state.consumables = [Consumable("赶尸铃", "召唤", 1, 1)]
    _add_monster(engine)
    r = engine.execute_action("consume_item", {"name": "赶尸铃"})
    assert r["success"], r
    assert [(t.blood_limit, t.attack_power, t.attack_count) for t in engine.state.temp_friends] == [
        (36, 1, 4), (36, 1, 4)]
    assert all(t.entity_type == "临时朋友" for t in engine.state.temp_friends)
    engine.state.enemies[0].is_alive = False
    engine.state.enemies = []
    assert engine.execute_action("battle_end", {})["success"]
    assert engine.state.temp_friends == []

    # 局外拒绝
    engine2 = _setup()
    engine2.state.phase = "pre_battle"
    engine2.state.consumables = [Consumable("赶尸铃", "召唤", 1, 1)]
    r = engine2.execute_action("consume_item", {"name": "赶尸铃"})
    assert not r["success"]


def _huangfu_setup(spell_trigger="瞬发"):
    engine = _setup()
    player = engine.state.player
    player.spells.append(Spell(
        name="试火", required_daowen=["杀伐"], trigger_condition=spell_trigger,
        effect_flow="发动杀伐X于目标", rank=1, trigger=spell_trigger))
    engine.state.consumables = [Consumable("黄符", "刻印用法术", 1, 1)]
    return engine


def test_huangfu_imprint_and_deliver_and_command_cast():
    engine = _huangfu_setup()
    player = engine.state.player
    player.current_mana = 10
    r = engine.execute_action("consume_item", {"name": "黄符", "imprint_spell": "试火"})
    assert r["success"], r
    assert player.current_mana == 9  # 刻印耗法力=阶级
    assert engine.state.consumables[0].current_uses == 1  # 刻印不耗耐久

    engine.state.phase = "in_combat"
    f = Entity("阿友", "朋友", blood_limit=40, current_hp=40, mana_limit=10, current_mana=10,
               speed_limit=5, current_speed=5, attack_count=2, attack_power=3)
    engine.state.friends = [f]
    m = _add_monster(engine)
    r = engine.execute_action("consume_item", {"name": "黄符", "target_ref": "friend:0"})
    assert r["success"], r
    assert [sp.name for sp in f.spells] == ["试火"]
    assert (f.runtime_id, "试火") in engine.state.huangfu_grants()

    r = engine.execute_action("command_ally", {
        "ally_ref": "friend:0", "instruction": "发动 试火 打 靶怪",
        "spell_steps": [{"x": 1, "dodge": False, "trigger_spell_choices": {}}]})
    assert r["success"], r
    assert m.current_hp < 100
    assert f.current_mana == 8  # 1黄符能源 + 1杀伐步骤消耗
    assert "杀伐" not in f.dao_wen  # 临时授予已收回


def test_huangfu_rejects_unlearned_reactive_and_double_imprint():
    engine = _huangfu_setup()
    engine.state.player.current_mana = 10
    r = engine.execute_action("consume_item", {"name": "黄符", "imprint_spell": "不存在"})
    assert not r["success"]

    engine2 = _huangfu_setup(spell_trigger="受到伤害前")
    engine2.state.player.current_mana = 10
    r = engine2.execute_action("consume_item", {"name": "黄符", "imprint_spell": "试火"})
    assert not r["success"] and "瞬发" in r["error"]

    engine3 = _huangfu_setup()
    engine3.state.player.current_mana = 10
    assert engine3.execute_action("consume_item", {"name": "黄符", "imprint_spell": "试火"})["success"]
    r = engine3.execute_action("consume_item", {"name": "黄符", "imprint_spell": "试火"})
    assert not r["success"]


def test_huangfu_grant_expires_at_battle_end():
    engine = _huangfu_setup()
    engine.state.player.current_mana = 10
    assert engine.execute_action("consume_item", {"name": "黄符", "imprint_spell": "试火"})["success"]
    engine.state.phase = "in_combat"
    f = Entity("阿友", "朋友", blood_limit=40, current_hp=40, mana_limit=10, current_mana=10,
               speed_limit=5, current_speed=5, attack_count=2, attack_power=3)
    engine.state.friends = [f]
    m = _add_monster(engine)
    assert engine.execute_action("consume_item", {"name": "黄符", "target_ref": "friend:0"})["success"]
    assert f.spells and engine.state.huangfu_grants()
    m.is_alive = False
    engine.state.enemies = []
    assert engine.execute_action("battle_end", {})["success"]
    assert f.spells == [] and engine.state.huangfu_grants() == set()
