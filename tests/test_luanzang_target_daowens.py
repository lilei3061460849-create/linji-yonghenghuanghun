"""修复验证：幸存的【勾魂】与【坏死】目标参数接线。

背景：两个乱葬岗控制道纹的 calculate_* 缺少 target 参数 →
requires_target=False → 怪物只能自施（寄骨蝇勾魂自吸无法力、
红嫁衣鬼冥气自施），控制效果完全无法作用于玩家。本测试验证：

- 怪物施放道纹时能正确作用于玩家（勾魂阻止法力增长）；
- 玩家施放时仍能正确选择目标；
- 无目标提交被正确拒绝，而不是默默自施。

2026-10-08 用户令删除【镇尸】（与【坏死】硬重复）后，本文件第三个受检道纹
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


def test_monster_gouhun_blocks_player_mana_gain(tmp_path):
    """怪物勾魂 → 玩家获得勾魂状态，持续X回合**无法获得法力**（不扣已有法力）。

    版本史：旧版「[回始]失去2X法力，持续∞」→ 2026-08-30「无法获得法力」
    → 2026-09-09 因法力改一池制一度改为「消耗翻倍」
    → 2026-10-08 用户令：恢复「无法获得[法力]」，翻倍撤销。
    """
    e = _mk_engine(tmp_path)
    p = e.state.player
    p.current_mana = 30
    m = _monster_with("勾魂使者", {"勾魂": 2})
    e.state.enemies.append(m)
    e.combat.reset_monster_activation()
    r = _monster_cast(e, m, "勾魂", "player:0")
    assert r, r
    assert p.has_status("勾魂"), "勾魂应挂在玩家身上"

    # 勾魂期间：增益被压死，已持有的法力一分不扣，消耗也不翻倍
    p.current_mana = 9
    assert p.can_gain_mana() is False
    assert p.gain_mana(5) == 0
    assert p.current_mana == 9, f"勾魂期间不得获得法力，实剩 {p.current_mana}"
    assert p.spend_mana(4) is True
    assert p.current_mana == 5, f"勾魂不改消耗倍率，实剩 {p.current_mana}"



def test_monster_huaisi_blocks_player_heal(tmp_path):
    """怪物坏死 → 玩家无法获得[回复]（再生治疗被阻止）。

    2026-10-08 用户令删除【镇尸】后，本用例改挂【坏死】——扭曲都市专属，
    脑蜘蛛等承载，是「无法获得[回复]」的唯一实现。
    """
    e = _mk_engine(tmp_path, region="扭曲都市")
    p = e.state.player
    p.current_hp = 40
    p.dao_wen["再生"] = DaoWenInstance(
        DaoWen(name="再生", formula="", cost_type="消耗", cost_formula="X",
               effect_formula=""), x_value=0)
    m = _monster_with("脑蜘蛛", {"坏死": 2})
    e.state.enemies.append(m)
    e.combat.reset_monster_activation()
    r = _monster_cast(e, m, "坏死", "player:0")
    assert r, r
    assert p.has_status("坏死"), "坏死应挂在玩家身上"
    hp_after_cast = p.current_hp  # 怪物阶段包含1次1伤攻击：40→39
    # 玩家对自身发动再生4（应回复12）→ 被坏死阻止（单目标路径：直接跳过治愈，无heal条目）
    calc = DaoWenEngine.resolve("再生", 4, target=p, caster=p)
    res = e.combat.apply_daowen_effect("再生", calc, p, p)
    heals = [x for x in res.get("effects", []) if x.get("type") == "heal"]
    assert not heals, "再生应被坏死阻止（不应产生治愈条目）"
    assert p.current_hp == hp_after_cast, "玩家生命不应增加（应维持怪物攻击后的数值）"


def test_player_casts_daowens_with_explicit_targets(tmp_path):
    """玩家施放道纹时仍能正确选择目标。"""
    e = _mk_engine(tmp_path)
    p = e.state.player
    for dw in ("勾魂",):
        p.dao_wen[dw] = DaoWenInstance(
            DaoWen(name=dw, formula="", cost_type="消耗", cost_formula="X",
                   effect_formula=""), x_value=0)
    foe = Entity(name="敌法", entity_type="轮回者", blood_limit=80, current_hp=80,
                 mana_limit=20, current_mana=20, speed_limit=6, current_speed=6)
    e.state.enemies.append(foe)
    # 勾魂→敌法（法力被挂勾魂）
    calc = DaoWenEngine.resolve("勾魂", 2, target=foe, caster=p)
    e.combat.apply_daowen_effect("勾魂", calc, p, foe)
    assert foe.has_status("勾魂")
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
