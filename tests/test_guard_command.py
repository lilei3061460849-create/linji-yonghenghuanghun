"""命令朋友/员工替你扛伤（护卫命令）测试。

引擎机制：背负X（龙心谷道纹）「选择目标，其下X次受到伤害由自身承担」；
伤害重定向在 combat._apply_hostile_damage 实装（龙心谷 F2）。
command_ally 支持「发动背负 打 轮回者/我」——道纹指令目标允许指向我方单位。

覆盖：正常路径 / 别名 / 边界 / 错误输入
"""
import json
import os
import sys
import tempfile

from tests.setup_support import finish_initial_daowen, grant_extra_actions
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance, Entity
from sim.build_learner import round_start_relic_choices


def _engine(suffix: str, region: str = "乱葬岗") -> GameEngine:
    e = GameEngine(db_path=f"data/test_guard_{suffix}.db", rng_seed=1,
                   sealed_candidate_path="/tmp/guard_test.json")
    e.execute_action("setup_attributes", {"name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": region})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    e.state.relics = []
    # 2026-10-13：遗物池删2件后随机发放漂移，本文件断言与开局遗物无关，清空钉死环境。
    return e


def _friend_beifu(name="岩行者", x=1, hp=54, atk_count=2, atk_power=4) -> Entity:
    fr = Entity(name, "朋友", blood_limit=hp, current_hp=hp,
                attack_count=atk_count, attack_power=atk_power)
    fr.dao_wen["背负"] = DaoWenInstance(
        DaoWen(name="背负", formula="", cost_type="", cost_formula="", effect_formula=""), x_value=x)
    return fr


def _start_battle_with(engine, monster):
    """构造一个「场上恰好是指定单怪、无增援」的战斗场景，用于护卫/指令类单步断言。

    2026-09-28 配方式出怪后，battle_start 会先清空 enemies 再按配方放 S 只首发 + 增援队列；
    本夹具用指定单怪替换掉配方放出来的首发与增援，避免依赖具体 S/T_i 的随机落点。
    """
    engine.state.energy = 0
    active = {r.name for r in engine.state.relics if engine.state.sealed_relics.get(r.name, 0) <= 0}
    bs = engine.execute_action("battle_start", {"relic_choices": {
        n: {"use": False} for n in ("猩红果实", "苍白之花") if n in active}})
    assert bs["success"], bs
    # 用测试指定单怪替换配方出怪（等价于「这场战斗固定就是这只怪」），并清空增援队列
    engine.state.enemies.clear()
    engine.state.enemies.append(monster)
    engine.state.monster_reinforcements = []
    engine.execute_action("round_start", {"relic_choices": round_start_relic_choices(engine)})


def _resolve_monster_phase(engine):
    from sim.build_learner import _resolve_monster_turn
    return _resolve_monster_turn(engine)


def test_command_guard_with_beifu_redirects_damage():
    """正常路径：命令岩行者「发动背负 打 轮回者」，怪物打玩家的伤害转给岩行者。"""
    e = _engine("guard_ok")
    fr = _friend_beifu()
    e.state.friends.append(fr)
    m = Entity("血僵", "怪物", blood_limit=270, current_hp=270,
               attack_count=4, attack_power=19)
    _start_battle_with(e, m)
    p = e.state.player
    hp_before = p.current_hp
    fr_hp_before = fr.current_hp

    r = e.execute_action("command_ally", {
        "ally_ref": "friend:0", "instruction": "发动背负 打 轮回者"})
    assert r["success"], r
    # 背负注册：岩行者._beifu_left >= 1 且目标=玩家
    assert getattr(fr, "_beifu_left", 0) >= 1
    assert getattr(fr, "_beifu_target", None) == p.runtime_id

    # 怪物阶段：血僵打玩家，伤害应转给岩行者
    mp = _resolve_monster_phase(e)
    assert mp["success"], mp
    # 玩家伤害承受显著小于怪物总输出（被转伤）
    player_lost = hp_before - p.current_hp
    fr_lost = fr_hp_before - fr.current_hp
    assert fr_lost > 0, "岩行者应替玩家承受伤害"
    assert player_lost < 4 * 19, "玩家承受应小于怪物全部输出（有转伤）"
    assert not p.is_alive or p.current_hp > 0


def test_command_guard_alias_me():
    """正常路径：指令用「我」别名也能指向玩家。"""
    e = _engine("guard_alias")
    fr = _friend_beifu()
    e.state.friends.append(fr)
    m = Entity("蛆冢", "怪物", blood_limit=270, current_hp=270,
               attack_count=6, attack_power=9)
    _start_battle_with(e, m)
    r = e.execute_action("command_ally", {
        "ally_ref": "friend:0", "instruction": "发动背负 打 我"})
    assert r["success"], r
    assert getattr(fr, "_beifu_target", None) == e.state.player.runtime_id


def test_command_guard_unknown_target_rejected():
    """错误输入：目标名不存在时拒绝且不改状态。"""
    e = _engine("guard_bad")
    fr = _friend_beifu()
    e.state.friends.append(fr)
    m = Entity("血僵", "怪物", blood_limit=270, current_hp=270,
               attack_count=4, attack_power=19)
    _start_battle_with(e, m)
    r = e.execute_action("command_ally", {
        "ally_ref": "friend:0", "instruction": "发动背负 打 不存在的人"})
    assert not r["success"]
    assert getattr(fr, "_beifu_left", 0) == 0


def test_command_guard_attack_still_enemy_only():
    """边界：攻击指令仍只能指向敌方（不能攻击自己人）。"""
    e = _engine("guard_atk")
    fr = _friend_beifu()
    e.state.friends.append(fr)
    m = Entity("血僵", "怪物", blood_limit=270, current_hp=270,
               attack_count=4, attack_power=19)
    _start_battle_with(e, m)
    r = e.execute_action("command_ally", {
        "ally_ref": "friend:0", "instruction": "攻击 轮回者"})
    assert not r["success"], "攻击指令不得指向我方"


# ========================================================================
# 护卫指令（无消耗强制挡伤）
# ========================================================================

def test_guard_command_forces_redirect_without_cost():
    """正常路径：命令盟友「护卫 X」→ 无消耗强制背负，怪物打玩家的伤害转给盟友。"""
    e = _engine("guard_cmd_ok")
    fr = _friend_beifu()
    fr.dao_wen.clear()  # 盟友无道纹也要能护卫（强制）
    e.state.friends.append(fr)
    m = Entity("血僵", "怪物", blood_limit=270, current_hp=270,
               attack_count=4, attack_power=19)
    _start_battle_with(e, m)
    p = e.state.player
    actions_before = fr.actions_used_this_round
    r = e.execute_action("command_ally", {
        "ally_ref": "friend:0", "instruction": "护卫 4"})
    assert r["success"], r
    assert getattr(fr, "_beifu_left", 0) == 4
    assert getattr(fr, "_beifu_target", None) == p.runtime_id
    # 无消耗：盟友出手次数不变
    assert fr.actions_used_this_round == actions_before

    hp0, frhp0 = p.current_hp, fr.current_hp
    mp = _resolve_monster_phase(e)
    assert mp["success"], mp
    player_lost = hp0 - p.current_hp
    fr_lost = frhp0 - fr.current_hp
    assert fr_lost > 0, "盟友应替玩家承受伤害"
    assert player_lost == 0, "护卫生效时玩家本回合应无伤"
    assert getattr(fr, "_beifu_left", 0) < 4, "每命中消耗1次护卫次数"


def test_guard_command_default_one():
    """正常路径：护卫缺省X=1，挡1次。"""
    e = _engine("guard_cmd_one")
    fr = _friend_beifu()
    e.state.friends.append(fr)
    m = Entity("血僵", "怪物", blood_limit=270, current_hp=270,
               attack_count=4, attack_power=19)
    _start_battle_with(e, m)
    r = e.execute_action("command_ally", {
        "ally_ref": "friend:0", "instruction": "护卫"})
    assert r["success"], r
    assert getattr(fr, "_beifu_left", 0) == 1


def test_ally_commands_rejected_outside_player_action_subphase():
    """回归：轮回者指挥不能在怪物阶段或回合结束阶段插队。"""
    e = _engine("guard_cmd_phase")
    fr = _friend_beifu()
    e.state.friends.append(fr)
    m = Entity("血僵", "怪物", blood_limit=270, current_hp=270,
               attack_count=1, attack_power=1)
    _start_battle_with(e, m)

    mp = _resolve_monster_phase(e)
    assert mp["success"], mp
    r = e.execute_action("command_ally", {
        "ally_ref": "friend:0", "instruction": "护卫 1"})
    assert not r["success"], r
    assert "子阶段" in r["error"]


def test_guard_command_bad_x_rejected():
    """错误输入：护卫次数非1~9整数时拒绝且不施加。"""
    e = _engine("guard_cmd_bad")
    fr = _friend_beifu()
    e.state.friends.append(fr)
    m = Entity("血僵", "怪物", blood_limit=270, current_hp=270,
               attack_count=4, attack_power=19)
    _start_battle_with(e, m)
    for bad in ("护卫 0", "护卫 10", "护卫 三", "护卫 abc"):
        r = e.execute_action("command_ally", {"ally_ref": "friend:0", "instruction": bad})
        assert not r["success"], bad
    assert getattr(fr, "_beifu_left", 0) == 0


# ========================================================================
# 自创法术（custom_spell）—— 文本→解析→实战触发
# ========================================================================

def _engine_custom(db_suffix, daowen_list):
    """自创法术用例：2026-09-16 起一律在战斗中自创，故这里直接开好战斗。"""
    from engine.models import DaoWen, DaoWenInstance, Entity
    e = _engine(db_suffix, region="罪孽都市")
    for dn in daowen_list:
        e.state.player.dao_wen[dn] = DaoWenInstance(
            DaoWen(name=dn, formula="", cost_type="消耗", cost_formula="X", effect_formula=""), x_value=0)
    _start_battle_with(e, Entity("靶怪", "怪物", blood_limit=999, current_hp=999,
                                 attack_count=1, attack_power=1))
    # 2026-10-25：法术阶数＝重数。带触发时机的法术至少 2 阶，多步更高，
    # 故夹具用【蓄锐·增】把出手预算补足。
    grant_extra_actions(e.state.player, 7)
    return e


def test_custom_spell_defined_in_battle_costs_tier_actions():
    """正常路径：战斗中 define_spell 自创成功、立即生效、按阶级消耗主动出手。

    2026-10-25 用户令：法术阶数＝重数——触发时机一重＋每个效果步骤各一重。
    本例【以杀养伤】= 受到伤害前＋杀伐＋再生 = 3 阶，故扣 3 次出手
    （旧口径按「用了几种道纹」计，只有 2 阶）。

    2026-09-16 用户裁定：法术不再需要【学习】，局外自创入口取消，三大法则
    由句式解析器硬性把关，故不再产生"未见场景"中断等 DM 裁定。
    """
    e = _engine_custom("custom_define", ["杀伐", "再生"])
    p = e.state.player
    used_before = p.actions_used_this_round
    definition = {"name": "以杀养伤", "required_daowen": ["杀伐", "再生"],
                  "trigger_condition": "受到伤害前",
                  "effect_flow": "发动杀伐X于攻击者→发动再生X于自身"}
    r = e.execute_action("define_spell", {"spell": definition})
    assert r["success"], r.get("error")
    assert r["result"]["wired"] is True
    assert [s.name for s in p.spells] == ["以杀养伤"]
    assert p.actions_used_this_round == used_before + 3, "3阶自创法术应消耗3次主动出手"
    assert not e._pending_interrupts, "新规则下自创不再卡DM裁定"


def test_custom_spell_requires_in_combat():
    """局外自定义入口已取消：pre_battle 提交必须被拒绝且不产生任何效果。"""
    from engine.models import DaoWen, DaoWenInstance
    e = _engine("custom_outside", region="罪孽都市")
    e.state.player.dao_wen["杀伐"] = DaoWenInstance(
        DaoWen(name="杀伐", formula="", cost_type="消耗", cost_formula="X", effect_formula=""), x_value=0)
    definition = {"name": "局外法术", "required_daowen": ["杀伐"],
                  "trigger_condition": "受到伤害前",
                  "effect_flow": "发动杀伐X于攻击者"}
    r = e.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "custom_spell",
                                               "spell": definition, "dm_approved": True})
    assert not r["success"]
    assert e.state.player.spells == []
    r2 = e.execute_action("define_spell", {"spell": definition})
    assert not r2["success"], "局外也不能用 define_spell"


def test_custom_spell_rejects_unknown_daowen_in_flow():
    """错误输入：required_daowen 含不存在的道纹 → 校验拦截。"""
    e = _engine_custom("custom_bad", ["杀伐"])
    definition = {"name": "假回复", "required_daowen": ["回复"],  # 无此道纹
                  "trigger_condition": "受到伤害前", "effect_flow": "发动回复X于自身"}
    r = e.execute_action("define_spell", {"spell": definition})
    assert not r["success"], "不存在道纹'回复'必须拒绝"
    assert e.state.player.spells == []


def test_custom_spell_rejects_missing_target_declaration():
    """句式错误提醒（2026-08-29新增）：缺少显式目标声明必须报错，不能静默成摆设。"""
    e = _engine_custom("custom_notarget", ["杀伐"])
    definition = {"name": "缺目标法术", "required_daowen": ["杀伐"],
                  "trigger_condition": "受到伤害前", "effect_flow": "发动杀伐X"}
    r = e.execute_action("define_spell", {"spell": definition})
    assert not r["success"]
    assert "句式错误" in r["error"] and "目标" in r["error"]


def test_custom_spell_rejects_unknown_trigger_wording():
    """句式错误提醒：完全无法识别的触发时机必须报错并给出可用词汇表。"""
    e = _engine_custom("custom_badtrigger", ["杀伐"])
    definition = {"name": "怪触发法术", "required_daowen": ["杀伐"],
                  "trigger_condition": "月圆之夜", "effect_flow": "发动杀伐X于攻击者"}
    r = e.execute_action("define_spell", {"spell": definition})
    assert not r["success"]
    assert "句式错误" in r["error"]


# ==================== sim 怪物 AI 决策路径回归（2026-08-19 修复） ====================

def test_alt_path_resolve_monster_turn_with_daowen_monster_no_nameerror():
    """回归：sim/alt_path_test.resolve_monster_turn 在“带道纹怪物”上不再 NameError。

    此前 sim/handplay_dungeon_with_winner.py:129/181 的
    `_pick_monster_daowen(engine, actor)` 使用未定义变量 engine（参数实际是 e），
    任何怪物在第 2 回合起（白板回合之后）持有可用道纹时，该决策路径必然崩溃。
    本测试固定验证带道纹怪物的决策路径，不把出怪配方随机性当作前置条件；
    经公共 API 走完整 resolve_monster_turn，断言不再 NameError 且怪物阶段正常结算。
    """
    from sim.alt_path_test import resolve_monster_turn

    save_dir = tempfile.mkdtemp(prefix="altpath")
    e = GameEngine(db_path=os.path.join(save_dir, "g.db"), rng_seed=1,
                   save_dir=save_dir)
    e.execute_action("setup_attributes", {"name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "扭曲都市"})
    e.execute_action("choose_discovered_relic",
                     {"relic_name": setup["result"]["relic_choices"][0]})
    # 该回归只验证带道纹怪物的决策，不依赖随机开局遗物的反噬/改靶行为。
    e.state.relics = []
    e.state.energy = 0
    bs = e.execute_action("battle_start", {"relic_choices": {}})
    assert bs.get("success"), bs
    assert bs.get("enemies"), "必须出怪"
    # 出怪配方可能随机给出不同怪物，测试只关心道纹怪物决策路径。
    # 固定使用实际生成的一只怪，并给它挂上脑蜘蛛测试所需的三种道纹。
    monster = e.state.enemies[0]
    for daowen_name in ("坏死", "强化", "减速"):
        monster.dao_wen[daowen_name] = DaoWenInstance(
            DaoWen(name=daowen_name, formula="", cost_type="消耗",
                   cost_formula="X", effect_formula=""), x_value=1)
    monster._had_monster_daowen = True
    e.state.enemies = [monster]
    e.state.monster_reinforcements = []

    # 第 1 回合（白板：怪物只普攻不出道纹）
    assert e.execute_action("round_start", {"relic_choices": {}})["success"]
    mp1 = resolve_monster_turn(e, [])
    assert mp1.get("success"), f"第1回合怪物阶段失败: {mp1.get('error')}"
    assert e.execute_action("round_end", {})["success"]

    # 第 2 回合：怪物可发动道纹（脑蜘蛛 坏死/强化/减速）
    assert e.execute_action("round_start", {"relic_choices": {}})["success"]
    assert e.state.current_round == 2, "必须已进入第2回合"
    # 可见性断言用 combat.prepare_monster_phase（纯枚举、不写状态、不消耗资源），
    # 避免与 resolve_monster_turn 内部 prepare 的 API 级 pending 冲突。
    options = [o["name"] for a in e.combat.prepare_monster_phase()["actors"]
               for o in (a.get("daowen_options") or [])]
    assert options, "第2回合怪物必须暴露道纹选项（白板回合已过）"

    mp2 = resolve_monster_turn(e, [])   # 修复前此处 NameError
    assert mp2.get("success"), f"第2回合怪物阶段失败: {mp2.get('error')}"
    assert not e.state.pending_monster_phase, "怪物阶段必须已结算完成"
    # 怪物道纹分支实际走到：结算条目存在
    details = (mp2.get("result") or {}).get("details") or []
    assert details, "第2回合怪物必须实际行动（普攻或道纹）"


# ==================== P1: 怪物道纹选择按 round_used 过滤（2026-08-19） ====================

def _pick_engine(suffix):
    e = _engine(suffix)
    m = Entity("测试怪", "怪物", blood_limit=100, current_hp=100,
               attack_count=1, attack_power=5)
    e.state.enemies.append(m)
    return e, m


def _pick_actor():
    return {"actor_ref": "enemy:0", "daowen_options": [
        {"name": "赎金", "requires_target": True, "target_options": [{"ref": "player:0"}]},
        {"name": "减速", "requires_target": True, "target_options": [{"ref": "player:0"}]},
        {"name": "蒙蔽", "requires_target": True, "target_options": [{"ref": "player:0"}]},
    ]}


def test_pick_monster_daowen_uses_round_used_not_activated():
    """候选过滤必须依据本回合已使用集合 round_used，而不是跨回合 activated。

    同一道纹即使已在 activated（此前回合激活过）也可在本回合再次选择；
    一旦进入本回合 round_used 就必须排除。
    """
    from sim.handplay_dungeon_with_winner import _pick_monster_daowen
    e, m = _pick_engine("pick_ru")
    actor = _pick_actor()

    # activated 含全部候选（跨回合持续激活），但 round_used 为空 → 仍可正常选择
    e.combat._monster_activated[id(m)] = {"赎金", "减速", "蒙蔽"}
    pick = _pick_monster_daowen(e, actor)
    assert pick is not None, "activated 不应阻止跨回合再次选择"

    # 本回合用过 赎金 → 只排除 赎金（多道纹混合时仅排除 round_used）
    e.combat._monster_round_used(m).add("赎金")
    pick = _pick_monster_daowen(e, actor)
    assert pick is not None and pick["name"] != "赎金", "同回合不得重复选择已用道纹"

    # 全部候选本回合已用 → 返回 None（纯攻击，绝不退回 opts[0]）
    e.combat._monster_round_used(m).update({"减速", "蒙蔽"})
    assert _pick_monster_daowen(e, actor) is None, "候选耗尽必须选择不发动道纹"


def test_pick_monster_daowen_cross_round_reuse():
    """同一道纹跨回合可再次选择：round_used 随回合重置后重新可选。"""
    from sim.handplay_dungeon_with_winner import _pick_monster_daowen
    e, m = _pick_engine("pick_cross")
    actor = _pick_actor()
    # 本回合已用 赎金 → 排除
    e.combat._monster_round_used(m).add("赎金")
    pick1 = _pick_monster_daowen(e, actor)
    assert pick1 is not None and pick1["name"] != "赎金"
    # 跨回合：current_round 变化后 round_used 自动清空 → 赎金重新可选
    e.state.current_round += 1
    assert _pick_monster_daowen(e, actor) is not None
