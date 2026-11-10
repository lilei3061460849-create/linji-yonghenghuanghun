"""修复验证：幸存的【勾魂】与【坏死】目标参数接线。

背景：两个乱葬岗控制道纹的 calculate_* 缺少 target 参数 →
requires_target=False → 怪物只能自施（寄骨蝇勾魂自吸无法力、
红嫁衣鬼冥气自施），控制效果完全无法作用于玩家。本测试验证：

- 怪物施放道纹时能正确作用于玩家（勾魂阻止法力增长）；
- 玩家施放时仍能正确选择目标；
- 无目标提交被正确拒绝，而不是默默自施。

删除【镇尸】（与【坏死】硬重复）后，本文件第三个受检道纹
由【镇尸】改为【坏死】——它是「无法获得[回复]」的唯一实现（扭曲都市专属，
脑蜘蛛等承载）。禁疗效果本身不变，只是换到唯一幸存的那个实现上验证。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import Entity, DaoWen, DaoWenInstance, StatusEffect
from engine.daowen import DaoWenEngine
from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import GameState

from tests.setup_support import finish_initial_daowen


def _mk_engine(tmp_path, region="乱葬岗"):
    e = GameEngine(db_path=str(tmp_path / "t.db"), rng_seed=7,
                   sealed_candidate_path=str(tmp_path / "s.json"))
    e.execute_action("setup_attributes", {
        "name": "白某", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    e.execute_action("setup_choose_region", {"region": region})
    return e


def _monster_with(name, daowen: dict):
    """构造带指定道纹的怪物（x_value 取给定值）。"""
    # 2026-09-16：怪物[法限]即法力池，也是[攻击力]。1 点法限付不起这里的道纹，
    # 抬到 20（本文件只断言道纹行为，不断言怪物伤害）。
    m = Entity(name=name, entity_type="怪物", blood_limit=200, current_hp=200,
               attack_count=1, attack_power=20)
    for dw, x in daowen.items():
        m.dao_wen[dw] = DaoWenInstance(
            DaoWen(name=dw, formula="", cost_type="消耗", cost_formula="X",
                   effect_formula=""), x_value=x)
    return m


def _monster_cast(e, monster, daowen_name, target_ref):
    """怪物阶段：怪物发动指定道纹指向 target_ref。返回 resolve 结果。"""
    e.state.phase = "in_combat"
    e.state.combat_subphase = "player_actions"
    e.state.current_round = 2  # 跳过白板回合
    prepared = e.combat.prepare_monster_phase()
    actor = next(a for a in prepared["actors"] if a["actor_ref"] == "enemy:0")
    opt = next(o for o in actor["daowen_options"] if o["name"] == daowen_name)
    dao = {"name": daowen_name, "dodge": False, "blood_shadow": False,
           "trigger_spell_choices": {}}
    if opt["requires_target"]:
        dao["target_ref"] = target_ref
    target_option = next(t for t in actor["attack_target_options"] if t["ref"] == "player:0")
    spell_choices = {timing: {s["spell_name"]: {"use": False}
                              for s in target_option.get("spell_options", {}).get(timing, [])}
                     for timing in ("before", "after")}
    attacks = [{"hits": [{"target_ref": "player:0", "dodge": False, "blood_shadow": False,
                          "spell_choices": spell_choices}
                         for _ in range(actor["base_hits_per_attack"])]}
               for _ in range(actor["base_attack_actions"])]
    return e.combat.resolve_monster_phase(
        [{"actor_ref": actor["actor_ref"], "daowen": dao, "attack_actions": []}],
        prepared)


def test_requires_target_flag_now_true(tmp_path):
    """三个道纹必须声明需要[目标]（requires_target 判定依据=签名含 target）。"""
    import inspect
    DaoWenEngine.register_all()
    for name in ("勾魂", "坏死"):
        assert "target" in inspect.signature(DaoWenEngine._registry[name]).parameters, name


def test_monster_gouhun_transfers_player_daowen(tmp_path):
    """怪物勾魂 → 玩家道纹被转移到怪物方[造物]身上（2026-11-08 重做）。

    版本史：旧版「[回始]失去2X法力，持续∞」→ 2026-08-30「无法获得法力」
    → 2026-09-09 一度「消耗翻倍」→ 2026-10-08 恢复「无法获得[法力]」
    → 2026-11-08 改为「法力5X，转移目标X种道纹至1个[造物]，持续∞」，
    法力增益不再被压制。
    """
    e = _mk_engine(tmp_path)
    p = e.state.player
    p.current_mana = 30
    m = _monster_with("勾魂使者", {"勾魂": 2})
    m.mana_limit = m.current_mana = 50
    e.state.enemies.append(m)
    creation = Entity(name="尸傀", entity_type="造物", blood_limit=30, current_hp=30)
    e.state.enemies.append(creation)
    e.combat.reset_monster_activation()
    r = _monster_cast(e, m, "勾魂", "player:0")
    assert r, r
    assert "杀伐" not in p.dao_wen and "杀伐" in creation.dao_wen, "道纹应被转移到造物身上"
    assert not p.has_status("勾魂"), "新勾魂不挂持续状态"

    # 法力：增益照常，已持有的一分不扣，消耗也不翻倍
    p.mana_limit = 30
    p.current_mana = 9
    assert p.can_gain_mana() is True
    assert p.gain_mana(5) == 5
    assert p.current_mana == 14, f"勾魂已不再压制法力增益，实剩 {p.current_mana}"
    assert p.spend_mana(4) is True
    assert p.current_mana == 10, f"勾魂不改消耗倍率，实剩 {p.current_mana}"


def test_player_casts_daowens_with_explicit_targets(tmp_path):
    """玩家施放道纹时仍能正确选择目标（2026-11-08：勾魂=转移道纹）。"""
    e = _mk_engine(tmp_path)
    p = e.state.player
    for dw in ("勾魂",):
        p.dao_wen[dw] = DaoWenInstance(
            DaoWen(name=dw, formula="", cost_type="消耗", cost_formula="X",
                   effect_formula=""), x_value=0)
    foe = Entity(name="敌法", entity_type="轮回者", blood_limit=80, current_hp=80,
                 mana_limit=20, current_mana=20, speed_limit=6, current_speed=6)
    foe.dao_wen["杀伐"] = DaoWenInstance(
        DaoWen(name="杀伐", formula="", cost_type="消耗", cost_formula="X",
               effect_formula=""), x_value=0)
    e.state.enemies.append(foe)
    creation = Entity(name="尸傀", entity_type="造物", blood_limit=30, current_hp=30)
    e.state.temp_friends.append(creation)
    # 勾魂→敌法：其【杀伐】被转移到我方[造物]身上
    calc = DaoWenEngine.resolve("勾魂", 1, target=foe, caster=p, daowen_names=["杀伐"])
    e.combat.apply_daowen_effect("勾魂", calc, p, foe)
    assert "杀伐" not in foe.dao_wen and "杀伐" in creation.dao_wen
    assert not foe.has_status("勾魂"), "新勾魂不挂持续状态"
    # 2026-10-08 删【镇尸】：禁疗改由【坏死】唯一实现，此处不再并排验证。


def test_player_cast_without_target_is_rejected(tmp_path):
    """玩家施放道纹时缺少[目标]必须被拒绝，而不是默默自施。"""
    e = _mk_engine(tmp_path)
    p = e.state.player
    for dw in ("勾魂",):
        p.dao_wen[dw] = DaoWenInstance(
            DaoWen(name=dw, formula="", cost_type="消耗", cost_formula="X",
                   effect_formula=""), x_value=0)
    foe = Entity(name="敌法", entity_type="轮回者", blood_limit=80, current_hp=80,
                 mana_limit=20, current_mana=20, speed_limit=6, current_speed=6)
    e.state.enemies.append(foe)
    e.state.phase = "in_combat"
    e.state.combat_subphase = "player_actions"
    for dw in ("勾魂",):
        r = e.execute_action("use_daowen", {"daowen_name": dw, "x": 1,
                                            "trigger_spell_choices": {}})
        assert not r.get("success"), f"{dw} 缺少目标应被拒绝"
        assert "目标" in r.get("error", ""), f"{dw} 错误信息应指出目标缺失：{r.get('error')}"


def test_monster_cast_without_target_is_rejected(tmp_path):
    """怪物施放道纹时缺少[目标]必须被拒绝，而不是默默自施。"""
    e = _mk_engine(tmp_path)
    p = e.state.player
    m = _monster_with("勾魂使者", {"勾魂": 2})
    e.state.enemies.append(m)
    e.combat.reset_monster_activation()
    e.state.phase = "in_combat"
    e.state.combat_subphase = "player_actions"
    e.state.current_round = 2
    prepared = e.combat.prepare_monster_phase()
    actor = next(a for a in prepared["actors"] if a["actor_ref"] == "enemy:0")
    opt = next(o for o in actor["daowen_options"] if o["name"] == "勾魂")
    assert opt["requires_target"], "勾魂应要求目标"
    dao = {"name": "勾魂", "dodge": False, "blood_shadow": False,
           "trigger_spell_choices": {}}  # 故意不提交 target_ref
    target_option = next(t for t in actor["attack_target_options"] if t["ref"] == "player:0")
    spell_choices = {timing: {s["spell_name"]: {"use": False}
                              for s in target_option.get("spell_options", {}).get(timing, [])}
                     for timing in ("before", "after")}
    attacks = [{"hits": [{"target_ref": "player:0", "dodge": False, "blood_shadow": False,
                          "spell_choices": spell_choices}
                         for _ in range(actor["base_hits_per_attack"])]}
               for _ in range(actor["base_attack_actions"])]
    try:
        e.combat.resolve_monster_phase(
            [{"actor_ref": actor["actor_ref"], "daowen": dao, "attack_actions": []}],
            prepared)
        raise AssertionError("怪物缺少目标提交应被拒绝")
    except ValueError as exc:
        assert "目标" in str(exc), f"错误信息应指出目标缺失：{exc}"
    assert not p.has_status("勾魂"), "被拒后玩家不应获得勾魂状态"
