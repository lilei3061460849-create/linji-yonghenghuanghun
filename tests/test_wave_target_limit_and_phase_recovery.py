"""BUG-01/BUG-02 回归（2026-08-22）：怪物【波及】目标数限制 + 怪物阶段失败恢复。

BUG-01：【波及X】的X必须受合法目标数量限制——引擎不得给出永远无法结算的选项
        （原故障：龙心谷 熔岩蜥【波及3】solo场上只有1个合法目标，
        任何提交都被拒"必须为3个目标显式提交dodge_targets"）。
DM裁定2026-08-23（取代2026-08-22"不足X即过滤"方案）：面板X超过合法目标数时
        自适应降X（有效X=min(面板X,合法目标数)，见 prepare 的 wave_effective_x），
        与玩家侧 _max_legal_daowen_x 口径一致；仅合法目标数为0时才不提供。
2026-10-09 用户令：repeal 2026-10-03 的「波及 X≥2 下限」——X=1 合法，
        标记目标平分施法者道纹效果，单目标时全值落于该目标（X_MIN 装置整套拔除，
        daowen_effect 的分路门槛由「≥2 个目标」统一为「effective 非空即分路」）。
BUG-02：resolve_monster_phase 提交失败后战斗不得卡在怪物阶段——
        失败时状态整体回滚（零副作用），pending保持有效且同token可修正重交；
        所有失败/拦截路径都返回 recoverable/token/instruction，恢复路径明确可执行。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import DaoWen, DaoWenInstance, Entity
from sim.monster_targets import pick_wave_dodge_targets
from tests.setup_support import finish_initial_daowen


def _engine(tmp_path, seed: int = 20260822) -> GameEngine:
    return GameEngine(
        db_path=str(tmp_path / "w.db"),
        save_dir=str(tmp_path / "saves"),
        sealed_candidate_path=str(tmp_path / "sealed.json"),
        death_book_path=str(tmp_path / "death.md"),
        rng_seed=seed,
    )


def _full_setup(engine: GameEngine, region: str = "龙心谷") -> None:
    assert engine.execute_action("setup_attributes", {
        "name": "测试", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })["success"]
    assert finish_initial_daowen(engine)["success"]
    assert engine.execute_action("setup_choose_resonance", {"resonance_type": "反转"})["success"]
    assert engine.execute_action("setup_choose_region", {"region": region})["success"]


def _dw(entity: Entity, name: str, x: int = 1) -> None:
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""),
        x_value=x)


def _controlled_combat(engine: GameEngine, monsters: list[Entity]) -> Entity:
    player = Entity("轮回者", "轮回者", blood_limit=100, current_hp=100,
                    mana_limit=100, current_mana=100, speed_limit=9, current_speed=9,
                    attack_count=1, attack_power=2)
    engine.state.player = player
    engine.state.friends = []
    engine.state.employees = []
    engine.state.enemies = monsters
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "player_actions"
    engine.state.pending_monster_phase = {}
    engine.state.current_round = 2  # 跳过白板回合，怪物可发动道纹
    return player


def _magma_lizard() -> Entity:
    """龙心谷怪物池：熔岩蜥（血限234/法限14/速限3，加害2，狂暴3，波及3）。

    2026-09-16：怪物[法限]即法力池。道纹 X 每发动一次 +2（升级机制），波及会从
    X=3 涨到 5、7；原法限 6 到第 2 轮就付不起 2X=10 了，抬到 14（覆盖到 X=7）。
    本文件只断言波及的目标数与阶段恢复，不断言怪物伤害。
    """
    m = Entity("熔岩蜥", "怪物", blood_limit=234, current_hp=234,
               attack_count=3, attack_power=14)
    _dw(m, "加害", 2)
    _dw(m, "狂暴", 3)
    _dw(m, "波及", 3)
    return m


def _friend(name: str = "友军") -> Entity:
    return Entity(name, "朋友", blood_limit=40, current_hp=40,
                  speed_limit=2, current_speed=2, attack_count=1, attack_power=1)


def _prepared_monster_option(engine: GameEngine, actor_ref: str = "enemy:0") -> tuple:
    prepared = engine.execute_action("prepare_monster_phase", {})
    assert prepared["success"], prepared
    actor = next(a for a in prepared["result"]["actors"] if a["actor_ref"] == actor_ref)
    return prepared, actor


def _submission_for(actor: dict, option: dict | None = None) -> dict:
    """按 prepare 快照构造一次怪物阶段提交。

    option 缺省时提交「不发动道纹」；给了就按其合法目标/波及目标构造。
    目标类道纹必须带 target_ref，否则引擎以「目标不在prepare合法选项中」拒绝。
    """
    choice = {"actor_ref": actor["actor_ref"], "daowen": None,
              "attack_actions": _attack_block(actor)}
    if option is None:
        return choice
    dao: dict = {"name": option["name"], "dodge": False, "blood_shadow": False}
    if option.get("requires_target") and option.get("target_options"):
        dao["target_ref"] = option["target_options"][0]["ref"]
    dao["trigger_spell_choices"] = {
        h: {sp["spell_name"]: {"use": False} for sp in ss}
        for h, ss in option.get("trigger_spell_options", {}).items()}
    dao["dodge_targets"] = pick_wave_dodge_targets(option)
    choice["daowen"] = dao
    # 交替出手：一次提交＝一个行动，发动道纹时不得再捆绑攻击组。
    choice["attack_actions"] = []
    return choice


def _combat_prepared_actor(engine: GameEngine, actor_ref: str = "enemy:0") -> dict:
    """纯枚举（CombatEngine层，无pending副作用）——只用于检查prepare给出的选项。"""
    prepared = engine.combat.prepare_monster_phase()
    return next(a for a in prepared["actors"] if a["actor_ref"] == actor_ref)


def _decline(option) -> dict:
    return {timing: {sp["spell_name"]: {"use": False} for sp in option.get("spell_options", {}).get(timing, [])}
            for timing in ("before", "after")}


def _attack_block(actor: dict) -> list[dict]:
    tgt = actor["attack_target_options"][0]["ref"]
    to = next(t for t in actor["attack_target_options"] if t["ref"] == tgt)
    hits = [{"target_ref": tgt, "dodge": False, "blood_shadow": False,
             "spell_choices": _decline(to)} for _ in range(actor["base_hits_per_attack"])]
    return [{"hits": hits} for _ in range(actor["base_attack_actions"])]


def _legal_daowen(actor: dict) -> dict | None:
    if not actor["daowen_options"]:
        return None
    chosen = actor["daowen_options"][0]
    dao = {"name": chosen["name"], "dodge": False, "blood_shadow": False,
           "trigger_spell_choices": {h: {sp["spell_name"]: {"use": False} for sp in ss}
                                     for h, ss in chosen.get("trigger_spell_options", {}).items()}}
    if chosen["requires_target"]:
        dao["target_ref"] = chosen["target_options"][0]["ref"]
    if chosen["dodge_submission"] == "per_target":
        dao["dodge_targets"] = pick_wave_dodge_targets(chosen)
    return dao


def _pass_player_priority(engine: GameEngine) -> None:
    """交替出手：轮回者交出剩余优先权，让敌方/回终继续推进。"""
    passed = engine.execute_action("skip", {})
    assert passed["success"], passed


def _resolve_one_monster_action(engine: GameEngine, option: dict | None = None) -> dict:
    """让当前持有优先权的敌方结算一个行动（一步一个行动者）。"""
    prepared, actor = _prepared_monster_option(engine)
    return engine.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"],
        "choices": [_submission_for(actor, option)]})


def _snapshot(e: GameEngine) -> dict:
    """怪物阶段副作用快照：HP/速度/状态/碎片/round_used/事件流。"""
    m = e.state.enemies[0]
    return {
        "player": (e.state.player.current_hp, e.state.player.current_speed,
                   sorted(s.name for s in e.state.player.status_effects)),
        "friends": [(f.current_hp, sorted(s.name for s in f.status_effects))
                    for f in e.state.friends],
        "monster_hp": m.current_hp,
        "round_used": sorted(e.combat._monster_round_used(m)),
        "events": len(e.state.combat_events),
    }


# ============ BUG-01：【波及】合法目标数限制 ============

def test_wave_adaptive_clamp_solo(tmp_path):
    """solo 场上【波及3】只有 1 个合法目标：降 X 为 1，**照常给出并可结算**。

    规则沿革：
      * DM裁定 2026-08-23（取代 2026-08-22 过滤方案）：【波及】的实际目标数按
        合法目标数自适应降 X（min(面板X, 合法目标数)），不得给出永远无法结算的选项；
      * 2026-10-09 用户令：repeal 2026-10-03 的「波及 X≥2 下限」——X=1 完全合法，
        平分份数=目标数，单目标时全值由该目标承担（X_MIN 装置整套拔除）。
    净结果：solo 场的落地方式是「降 X 到 1 正常结算」——prepare 给出波及选项
    （wave_effective_x=1），怪物可以给唯一合法目标（玩家）种上波及标记。
    """
    e = _engine(tmp_path)
    _full_setup(e)
    _controlled_combat(e, [_magma_lizard()])
    prepared, actor = _prepared_monster_option(e)
    option = next((o for o in actor["daowen_options"] if o["name"] == "波及"), None)
    assert option is not None, f"solo场上波及降X=1应照常给出: {actor['daowen_options']}"
    assert option["x"] == 3 and option["wave_effective_x"] == 1, option
    assert len(option["dodge_target_options"]) == 1
    # 提交恰好 1 个目标（有效X=1）——必须真能结算成功，这才是"永不死锁"的实测
    choice = _submission_for(actor, option)
    assert len(choice["daowen"]["dodge_targets"]) == 1
    ok = e.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"], "choices": [choice]})
    assert ok["success"], ok
    assert e.state.player.has_status("波及"), "solo场波及X=1应给唯一合法目标（玩家）打上标记"


def test_wave_adaptive_clamp_scales_with_targets(tmp_path):
    """【波及3】：2个合法目标时降X为2；3个合法目标时全额X=3。"""
    e = _engine(tmp_path)
    _full_setup(e)
    _controlled_combat(e, [_magma_lizard()])
    e.state.friends.append(_friend("友军A"))
    actor = _combat_prepared_actor(e)
    option = next(o for o in actor["daowen_options"] if o["name"] == "波及")
    assert option["x"] == 3 and option["wave_effective_x"] == 2

    e.state.friends.append(_friend("友军B"))
    actor = _combat_prepared_actor(e)
    option = next(o for o in actor["daowen_options"] if o["name"] == "波及")
    assert option["x"] == 3 and option["wave_effective_x"] == 3
    assert len(option["dodge_target_options"]) == 3
    refs = {t["ref"] for t in option["dodge_target_options"]}
    assert refs == {"player:0", "friend:0", "friend:1"}
    assert option["dodge_submission"] == "per_target"
    # 降X口径下多交/少交仍被拒（恰好=有效X）
    pick = pick_wave_dodge_targets(option)
    assert len(pick) == 3


def test_wave_marks_exactly_x_targets(tmp_path):
    """候选数>X（双怪+玩家+2友军，波及2）：恰好提交2个，仅被提交者被标记。"""
    e = _engine(tmp_path)
    _full_setup(e)
    other = Entity("石背熊", "怪物", blood_limit=100, current_hp=100,
                   attack_count=1, attack_power=3)
    lizard = _magma_lizard()
    _dw(lizard, "波及", 2)  # 构造 候选4 > X=2
    _controlled_combat(e, [lizard, other])
    e.state.friends.append(_friend("友军A"))
    e.state.friends.append(_friend("友军B"))
    prepared, actor = _prepared_monster_option(e)
    option = next(o for o in actor["daowen_options"] if o["name"] == "波及")
    assert option["x"] == 2
    assert len(option["dodge_target_options"]) == 4  # 玩家+2友军+另一只怪

    dao = {"name": "波及", "dodge": False, "blood_shadow": False,
           "trigger_spell_choices": {h: {sp["spell_name"]: {"use": False} for sp in ss}
                                     for h, ss in option.get("trigger_spell_options", {}).items()},
           "dodge_targets": [
               {"target_ref": "player:0", "dodge": False, "blood_shadow": False},
               {"target_ref": "friend:0", "dodge": False, "blood_shadow": False}]}
    # 交替出手：先结算波及使用者（一步一个行动者），再让另一只怪行动。
    ok = e.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"],
        "choices": [{"actor_ref": "enemy:0", "daowen": dao, "attack_actions": []}]})
    assert ok["success"], ok
    assert e.state.player.has_status("波及")
    assert e.state.friends[0].has_status("波及")
    assert not e.state.friends[1].has_status("波及")
    assert not other.has_status("波及")

    _pass_player_priority(e)  # 交替出手：轮回者先交回优先权
    prepared2, other_actor = _prepared_monster_option(e, "enemy:1")
    ok = e.execute_action("resolve_monster_phase", {
        "token": prepared2["result"]["token"],
        "choices": [{"actor_ref": "enemy:1", "daowen": None,
                     "attack_actions": _attack_block(other_actor)}]})
    assert ok["success"], ok
    assert not other.has_status("波及"), "未被波及选中的怪不得因他人结算而被标记"


def test_wave_over_submit_rejected_and_rolled_back(tmp_path):
    """提交多于X个目标（候选全量提交）必须被拒且零副作用。"""
    e = _engine(tmp_path)
    _full_setup(e)
    other = Entity("石背熊", "怪物", blood_limit=100, current_hp=100,
                   attack_count=1, attack_power=3)
    lizard = _magma_lizard()
    _dw(lizard, "波及", 2)  # 候选4 > X=2，全量提交必然超X
    _controlled_combat(e, [lizard, other])
    e.state.friends.append(_friend("友军A"))
    e.state.friends.append(_friend("友军B"))
    prepared, actor = _prepared_monster_option(e)
    option = next(o for o in actor["daowen_options"] if o["name"] == "波及")
    before = _snapshot(e)

    # 全量提交4个候选（>X=2）→ 必须拒绝
    dao = {"name": "波及", "dodge": False, "blood_shadow": False,
           "trigger_spell_choices": {},
           "dodge_targets": [{"target_ref": t["ref"], "dodge": False, "blood_shadow": False}
                             for t in option["dodge_target_options"]]}
    bad = e.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"],
        "choices": [{"actor_ref": "enemy:0", "daowen": dao, "attack_actions": []}]})
    assert not bad["success"], bad
    assert "2个目标" in bad["error"], bad
    assert _snapshot(e) == before, "超提交被拒后必须零副作用"


# ============ BUG-02：失败提交后战斗不得卡死 ============

def test_failed_resolve_stays_resubmittable_with_same_token(tmp_path):
    """非法提交（少交命中）→ 失败带token/instruction；同token修正重交成功，回终畅通。"""
    e = _engine(tmp_path)
    _full_setup(e)
    _controlled_combat(e, [_magma_lizard()])
    prepared, actor = _prepared_monster_option(e)
    token = prepared["result"]["token"]
    before = _snapshot(e)

    # 非法提交：少交命中（一次提交＝一个行动，这里只交攻击组）。
    bad_choice = {"actor_ref": actor["actor_ref"], "daowen": None,
                  "attack_actions": _attack_block(actor)}
    bad_choice["attack_actions"][0]["hits"] = bad_choice["attack_actions"][0]["hits"][:-1]
    failed = e.execute_action("resolve_monster_phase",
                              {"token": token, "choices": [bad_choice]})
    assert failed["success"] is False
    assert failed.get("recoverable") is True
    assert failed.get("token") == token
    assert failed.get("instruction"), failed
    assert _snapshot(e) == before, "失败提交必须零副作用"

    # 期间其它行动被拦，但必须给出可执行的恢复指引
    locked = e.execute_action("round_end", {})
    assert locked["success"] is False
    assert locked.get("recoverable") is True
    assert locked.get("token") == token
    assert "resolve_monster_phase" in locked.get("instruction", "")

    # 关键：同一token修正后重交成功，回合推进
    good = {"actor_ref": actor["actor_ref"], "daowen": _legal_daowen(actor),
            "attack_actions": []}
    ok = e.execute_action("resolve_monster_phase",
                          {"token": token, "choices": [good]})
    assert ok["success"], ok
    _pass_player_priority(e)  # 轮回者仍有剩余出手，先交回优先权再回终
    assert e.execute_action("round_end", {})["success"]


def test_stale_token_replay_returns_valid_token(tmp_path):
    """旧token重放：失败且带回当前有效token；按该token重交成功。"""
    e = _engine(tmp_path)
    _full_setup(e)
    _controlled_combat(e, [_magma_lizard()])
    prepared, actor = _prepared_monster_option(e)
    token = prepared["result"]["token"]

    stale = e.execute_action("resolve_monster_phase",
                             {"token": "stale-token", "choices": []})
    assert stale["success"] is False
    assert stale.get("recoverable") is True
    assert stale.get("token") == token

    ok = e.execute_action("resolve_monster_phase", {
        "token": stale["token"],
        "choices": [{"actor_ref": actor["actor_ref"], "daowen": _legal_daowen(actor),
                     "attack_actions": []}]})
    assert ok["success"], ok


def test_user_original_playthrough_no_longer_stucks(tmp_path):
    """用户原流程回归：龙心谷 seed=20260822 熔岩蜥 solo，连续3回合不卡死。
    DM裁定2026-08-23后：波及3降X为1持续可选——每回合都专门发动波及验证可解算。
    （2026-10-09 用户令 repeal X≥2 下限后，solo 场波及 X=1 正式合法，
    每回合发动波及=给玩家种/摘标记，同样不得卡死。）"""
    e = _engine(tmp_path, seed=20260822)
    _full_setup(e)
    _controlled_combat(e, [_magma_lizard()])
    e.state.combat_subphase = "await_round_start"  # 从完整回合循环开始
    for _ in range(3):
        e.state.player.current_hp = e.state.player.blood_limit  # 夹具：避免死者之页中断干扰
        assert e.execute_action("round_start", {"relic_choices": {}})["success"]
        _pass_player_priority(e)  # 交替出手：回始由轮回者先手，交出优先权后敌方才能准备
        prepared, actor = _prepared_monster_option(e)
        # solo 场上【波及】降 X 为 1 持续可选（2026-10-09：X 下限 repealed）——
        # 每回合都专门发动波及验证可解算（标记在玩家身上逐回合「种/摘」切换）。
        option = next((o for o in actor["daowen_options"] if o["name"] == "波及"), None)
        assert option is not None and option["wave_effective_x"] == 1, \
            f"solo 回合应给出波及X=1: {actor['daowen_options']}"
        choice = _submission_for(actor, option)
        ok = e.execute_action("resolve_monster_phase", {
            "token": prepared["result"]["token"], "choices": [choice]})
        assert ok["success"], ok
        assert e.execute_action("round_end", {})["success"]


# ============ 玩家/指令侧：use_daowen 的波及X受目标数封顶（BUG-01 玩家侧） ============

def _wave_use_daowen_schema(engine: GameEngine, actor_ref: str = "") -> dict:
    """从可用行动中取 波及 的 use_daowen schema。"""
    actions = engine.get_available_actions().get("actions", [])
    for action in actions:
        schema = action.get("params_schema", {})
        if action.get("action_type") == "use_daowen" \
                and schema.get("daowen_name") == "波及" \
                and schema.get("actor_ref", "") == actor_ref:
            return action
    return {}


def test_use_daowen_schema_caps_wave_x_by_target_count(tmp_path):
    """玩家侧：schema 的X上限必须受合法目标数封顶——目标不足时不得给出无法发动的X。

    2026-10-09 用户令：波及 X≥2 下限 repealed，X 下限恒为 1——solo 场（1 个合法
    目标）X 档位就是 [1,1]，且 available=True（X=1 合法，单目标全值）。
    """
    e = _engine(tmp_path)
    _full_setup(e)
    player = _controlled_combat(e, [_magma_lizard()])
    _dw(player, "波及", 0)
    player.current_mana = 100  # 法力充足：X上限只能被目标数压住

    # solo：仅1个合法目标（怪物）→ X档位 [1,1]，X=1 合法可发动
    action = _wave_use_daowen_schema(e)
    assert action, "use_daowen schema 缺失"
    assert action["available"] is True
    assert action["params_schema"]["x"]["maximum"] == 1, action["params_schema"]
    assert action["params_schema"]["x"]["minimum"] == 1, action["params_schema"]

    # 2怪+1友军：3个合法目标 → X上限=3（档位 1..3 都可发动）
    e.state.enemies.append(Entity("石背熊", "怪物", blood_limit=100, current_hp=100,
                                  attack_count=1, attack_power=3))
    e.state.friends.append(_friend("友军A"))
    action = _wave_use_daowen_schema(e)
    assert action["available"] is True
    assert action["params_schema"]["x"]["maximum"] == 3, action["params_schema"]
    assert action["params_schema"]["x"]["minimum"] == 1, action["params_schema"]


def test_player_wave_cast_at_schema_max_succeeds(tmp_path):
    """按 schema 上限发动 波及（恰好提交X个目标）：成功且标记正确。"""
    e = _engine(tmp_path)
    _full_setup(e)
    player = _controlled_combat(e, [_magma_lizard()])
    _dw(player, "波及", 0)
    e.state.enemies.append(Entity("石背熊", "怪物", blood_limit=100, current_hp=100,
                                  attack_count=1, attack_power=3))
    player.current_mana = 100

    action = _wave_use_daowen_schema(e)
    x = action["params_schema"]["x"]["maximum"]  # =2（两只怪物）
    ok = e.execute_action("use_daowen", {"daowen_name": "波及", "x": x,
                                         "dodge": False, "blood_shadow": False,
                                         "trigger_spell_choices": {},
                                         "dodge_targets": [
                                             {"target_ref": "enemy:0", "dodge": False,
                                              "blood_shadow": False},
                                             {"target_ref": "enemy:1", "dodge": False,
                                              "blood_shadow": False}]})
    assert ok["success"], ok
    assert e.state.enemies[0].has_status("波及")
    assert e.state.enemies[1].has_status("波及")
    assert not player.has_status("波及")


def test_wave_single_mark_gets_full_value(tmp_path):
    """2026-10-09 重做核心语义：波及 X=1 标记单目标 → 施法者道纹全值落于该目标。

    与多目标同一条分路（平分份数=目标数，_divide_flat(v,1)=[v]）——
    杀伐3（9 点总伤）砸在唯一被标记的敌人身上必须是全额 9 点，而不是被"平分"成 0。
    未标记的第二只怪不受波及影响。
    """
    e = _engine(tmp_path)
    _full_setup(e)
    player = _controlled_combat(e, [_magma_lizard(),
                                    Entity("石背熊", "怪物", blood_limit=100, current_hp=100,
                                           attack_count=1, attack_power=3)])
    _dw(player, "波及", 0)
    _dw(player, "杀伐", 0)
    player.current_mana = 100
    marked = e.state.enemies[0]
    unmarked = e.state.enemies[1]

    ok = e.execute_action("use_daowen", {"daowen_name": "波及", "x": 1,
                                         "dodge": False, "blood_shadow": False,
                                         "trigger_spell_choices": {},
                                         "dodge_targets": [
                                             {"target_ref": "enemy:0", "dodge": False,
                                              "blood_shadow": False}]})
    assert ok["success"], ok
    assert marked.has_status("波及") and not unmarked.has_status("波及")

    # 交替出手：轮回者一回合只有1次出手额度；本用例需要同一回合两发，
    # 故按通用规则用【疯狂1】把额度提到2，并让怪物在两次施法之间正常行动一次。
    from engine.models import StatusEffect
    player.add_status(StatusEffect("疯狂", 1, 1, "test"))
    _resolve_one_monster_action(e)
    hp_before = marked.current_hp
    ok = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 3,
                                         "target_ref": "enemy:0",
                                         "dodge": False, "blood_shadow": False,
                                         "trigger_spell_choices": {}})
    assert ok["success"], ok
    assert marked.current_hp == hp_before - 9, \
        f"单标记应吃全值9点，实际 {hp_before - marked.current_hp} 点"
    assert unmarked.current_hp == 100, "未标记者不应受到波及扩散"


def test_commanded_wave_unavailable_when_targets_insufficient(tmp_path):
    """指令侧：朋友固定X=3的【波及】在目标不足时 available=False 且给出原因。"""
    e = _engine(tmp_path)
    _full_setup(e)
    _controlled_combat(e, [_magma_lizard()])
    friend = _friend("友军A")
    _dw(friend, "波及", 3)
    e.state.friends.append(friend)

    action = _wave_use_daowen_schema(e, actor_ref="friend:0")
    assert action, "指令 use_daowen schema 缺失"
    assert action["available"] is False, action
    assert "波及3需要3个目标" in action.get("reason", ""), action

    # 补足目标（2怪+友军自身外的存活角色=3）→ available=True
    e.state.enemies.append(Entity("石背熊", "怪物", blood_limit=100, current_hp=100,
                                  attack_count=1, attack_power=3))
    action = _wave_use_daowen_schema(e, actor_ref="friend:0")
    assert action["available"] is True, action


def test_placeholder_ai_casts_wave_without_rejection(tmp_path):
    """AI对战路径（PlaceholderBackend）：玩家持【波及】时占位AI能合法发动（恰好X个目标）。"""
    from engine.ai_player import AIPlayer, PlaceholderBackend
    e = _engine(tmp_path)
    _full_setup(e)
    player = _controlled_combat(e, [_magma_lizard(),
                                    Entity("石背熊", "怪物", blood_limit=100, current_hp=100,
                                           attack_count=1, attack_power=3)])
    _dw(player, "波及", 0)  # 玩家唯一道纹：占位AI必然轮到它
    player.current_mana = 100
    ai = AIPlayer(e, backend=PlaceholderBackend(), auto_validate=False)

    r = ai.play_turn()
    assert r["action"] == "use_daowen" and r["params"].get("daowen_name") == "波及", r["action"]
    assert r["result"]["success"], r["result"]
    marked = [m.name for m in e.state.enemies if m.has_status("波及")]
    assert len(marked) == 2, f"波及必须标记恰好X=2个目标: {marked}"


# ============ 怪物阶段义务可满足性：无攻击目标不卡死 ============

def test_monster_without_attack_targets_phase_still_settles(tmp_path):
    """solo对手飞行而怪物不飞：怪物无任何合法攻击目标。

    prepare必须把base_attack_actions置0（否则每击都需引用合法目标，
    提交永远失败——与【波及】目标数限制同族的"无法满足的义务"）。
    """
    e = _engine(tmp_path)
    _full_setup(e)
    player = _controlled_combat(e, [_magma_lizard()])
    player.is_flying = True  # 怪物不飞行 → 选不中玩家 → 无合法攻击目标

    prep = e.execute_action("prepare_monster_phase", {})
    # 怪物没有任何合法行动（无攻击目标、需目标道纹也无目标）时，prepare按【跳过】
    # 结束敌方回合：不得列出「0次攻击」的空行动者让调用方提交必被拒的义务。
    assert prep["success"], prep
    assert prep.get("skipped") is True, prep
    assert prep["result"]["actors"] == [], prep["result"]
    reasons = {entry["actor_ref"]: entry["reason"] for entry in prep["result"]["skipped"]}
    assert reasons.get("enemy:0") == "没有可支付的道纹或合法攻击目标", reasons
    # 跳过不卡死：轮回者交出优先权后轮次可以正常回终。
    _pass_player_priority(e)
    ended = e.execute_action("round_end", {})
    assert ended["success"], ended


# ============ pick_wave_dodge_targets：恰好X个、对侧优先 ============

def test_pick_wave_dodge_targets_exactly_x_hostiles_first():
    option = {
        "x": 2,
        "dodge_target_options": [
            {"ref": "player:0", "name": "轮回者"},
            {"ref": "enemy:1", "name": "另一只怪"},
            {"ref": "friend:0", "name": "友军"},
            {"ref": "enemy:2", "name": "第三只怪"},
        ],
    }
    picked = pick_wave_dodge_targets(option)
    assert [p["target_ref"] for p in picked] == ["player:0", "friend:0"]
    assert all(p["dodge"] is False and p["blood_shadow"] is False for p in picked)

    # 对侧不足X时以其余合法目标补齐
    option["x"] = 3
    picked = pick_wave_dodge_targets(option)
    assert [p["target_ref"] for p in picked] == ["player:0", "friend:0", "enemy:1"]
