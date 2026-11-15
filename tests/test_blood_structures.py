"""法力结构（血液结构）一期：构建门槛、逐击挂层与永焰/脆化/震爆/粘滞结算。

规则事实源：《规则正文·法力结构（血液结构）》（2026-11-10）。
- 构建声明在 prepare_attack 锁定（一次攻击一种结构，轮回者限定）；
- 承载＝攻击者当前[血限]的门槛校验（不扣减）；
- 每击造成≥1点伤害才 +1 层（空刀/闪避/招架/全吸收不构建）；
- 结构层 scope=BATTLE，[战终]清除。
"""
import os
import sys

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import Entity, StatusEffect
from tests.test_daowen_wiring import _engine, _monster


def _fast_player(engine):
    """抬高出手预算，便于一回合多次攻击声明。"""
    p = engine.state.player
    p.speed_limit = 6
    p.current_speed = 6
    p.add_status(StatusEffect(name="疯狂", value=5, remaining_rounds=-1, source="test"))
    return p


def _spiky_monster(engine, name="靶怪", hp=100):
    """带速度的怪物（能招架/闪避）。"""
    m = Entity(name=name, entity_type="怪物", blood_limit=hp, current_hp=hp,
               mana_limit=20, current_mana=20,
               speed_limit=4, current_speed=4,
               attack_count=2, attack_power=3)
    engine.state.enemies.append(m)
    return m


def _prepare(engine, structure=""):
    # 上一手攻击未把击数用完时 pending 仍开着；测试只关心每手提交的那几击，
    # 这里先清残留再开新手（每手攻击照常占一次出手）。
    engine.state.pending_attack = {}
    result = engine.execute_action("prepare_attack", {
        "actor_ref": "player:0", "blood_structure": structure})
    assert result["success"], result
    token = result["result"]["token"]
    return token, result["result"]


def _resolve(engine, token, hits):
    hits = [dict(hit, spell_choices=hit.get("spell_choices") or {"before": {}, "after": {}})
            for hit in hits]
    return engine.execute_action("resolve_attack", {"token": token, "hits": hits})


def _full_attack(engine, structure, real_hits, empty_fill=True):
    """整手攻击：real_hits 之后用空刀补满 hit_count，确保 pending 正常收口。"""
    token, options = _prepare(engine, structure)
    hits = [dict(h, spell_choices={"before": {}, "after": {}}) for h in real_hits]
    if empty_fill:
        hits += [{"target_ref": "enemy:0", "damage": 0, "dodge": False,
                  "parry": False, "blood_shadow": False,
                  "spell_choices": {"before": {}, "after": {}}}
                 for _ in range(int(options["hit_count"]) - len(hits))]
    r = _resolve(engine, token, hits)
    assert r["success"], r
    return r


# ---------------------------------------------------------------- 构建门槛

def test_structure_declaration_threshold_and_eligibility():
    """承载是门槛校验：血限不足拒绝、足够放行；非轮回者与未实装名拒绝。"""
    engine = _engine("struct_gate")
    p = _fast_player(engine)
    _spiky_monster(engine)
    # 血限11点×6=66，四张承载（10/10/15/15）全部可构建
    assert p.blood_limit >= 15
    token, options = _prepare(engine, "永焰")
    assert options["blood_structure"] == "永焰"
    names = {o["name"]: o for o in options["blood_structure_options"]}
    assert set(names) == {"永焰", "脆化", "震爆", "粘滞"}
    assert all(o["eligible"] for o in names.values()), names
    # 未实装结构拒绝（先清掉上一次的 pending 攻击）
    engine.state.pending_attack = {}
    r = engine.execute_action("prepare_attack", {"blood_structure": "流血"})
    assert not r["success"] and "未实装" in r["error"]
    # 门槛不足拒绝：压低血限到承载以下
    engine.state.pending_attack = {}
    p.blood_limit = 9
    r = engine.execute_action("prepare_attack", {"blood_structure": "永焰"})
    assert not r["success"] and "血限" in r["error"]
    # 门槛恰好等于承载：放行（只校验不扣减，血限不变）
    engine.state.pending_attack = {}
    p.blood_limit = 10
    r = engine.execute_action("prepare_attack", {"blood_structure": "永焰"})
    assert r["success"], r
    assert p.blood_limit == 10


def test_structure_rejected_for_non_reincarnator():
    """怪物不是学校毕业生：不能构建结构。"""
    engine = _engine("struct_monster")
    _spiky_monster(engine)
    m = engine.state.enemies[0]
    assert engine.combat.validate_structure_declaration(m, "永焰") is not None
    assert engine.combat.blood_structure_options(m) == []


# ---------------------------------------------------------------- 逐击挂层

def test_layers_stack_per_damaging_hit_only():
    """造成≥1点伤害的击逐击+1层；空刀、闪避、招架、全吸收不构建。"""
    engine = _engine("struct_layers")
    p = _fast_player(engine)
    m = _spiky_monster(engine)
    token, _ = _prepare(engine, "永焰")
    # 两击各5伤 → 2层；第三击空刀 → 不加层
    r = _resolve(engine, token, [
        {"target_ref": "enemy:0", "damage": 5, "dodge": False,
         "parry": False, "blood_shadow": False},
        {"target_ref": "enemy:0", "damage": 5, "dodge": False,
         "parry": False, "blood_shadow": False},
        {"target_ref": "enemy:0", "damage": 0, "dodge": False,
         "parry": False, "blood_shadow": False},
    ])
    assert r["success"], r
    hits = r["result"]["hits"]
    assert hits[0]["structure"]["layers"] == 1
    assert hits[1]["structure"]["layers"] == 2
    assert "structure" not in hits[2]
    assert engine.combat.structure_layers(m, "永焰") == 2
    # 闪避击：不构建
    token, _ = _prepare(engine, "永焰")
    m.current_speed = 4
    r = _resolve(engine, token, [
        {"target_ref": "enemy:0", "damage": 5, "dodge": True,
         "parry": False, "blood_shadow": False}])
    assert r["result"]["hits"][0]["dodge_success"] is True
    assert "structure" not in r["result"]["hits"][0]
    assert engine.combat.structure_layers(m, "永焰") == 2
    # 招架击：不构建
    token, _ = _prepare(engine, "永焰")
    r = _resolve(engine, token, [
        {"target_ref": "enemy:0", "damage": 5, "dodge": False,
         "parry": True, "blood_shadow": False}])
    assert r["result"]["hits"][0]["parry_success"] is True
    assert "structure" not in r["result"]["hits"][0]
    assert engine.combat.structure_layers(m, "永焰") == 2


def test_one_structure_per_attack_declaration():
    """一次攻击只能构建一种结构：同一 pending 内全部击都挂同一结构。"""
    engine = _engine("struct_single")
    p = _fast_player(engine)
    m = _spiky_monster(engine)
    token, _ = _prepare(engine, "脆化")
    r = _resolve(engine, token, [
        {"target_ref": "enemy:0", "damage": 4, "dodge": False,
         "parry": False, "blood_shadow": False}])
    assert r["result"]["hits"][0]["structure"]["structure"] == "脆化"
    assert engine.combat.structure_layers(m, "脆化") == 1
    assert engine.combat.structure_layers(m, "永焰") == 0


# ---------------------------------------------------------------- 永焰

def test_yongyan_burns_layers_each_round_start():
    """永焰：回始目标失去X点生命（直接失血）；多层按层数烧。"""
    engine = _engine("struct_yongyan")
    _fast_player(engine)
    m = _spiky_monster(engine)
    _full_attack(engine, "永焰", [
        {"target_ref": "enemy:0", "damage": 6, "dodge": False,
         "parry": False, "blood_shadow": False},
        {"target_ref": "enemy:0", "damage": 6, "dodge": False,
         "parry": False, "blood_shadow": False},
    ])
    assert engine.combat.structure_layers(m, "永焰") == 2
    hp_before = m.current_hp
    engine.state.combat_subphase = "await_round_start"
    engine.execute_action("round_start", {})
    assert m.current_hp == hp_before - 2
    # 层数不清空：下个回始继续烧
    hp_before = m.current_hp
    engine.state.combat_subphase = "await_round_start"
    engine.execute_action("round_start", {})
    assert m.current_hp == hp_before - 2


def test_yongyan_respects_shelter():
    """永焰是直接失血：目标【庇护】按次免疫（消耗免疫次数）。

    同时验证：被【庇护】整笔免疫的构建击没有真实降血，不挂层（build 前先
    给庇护时层数为0、庇护照常消耗；要测永焰与庇护的交互，构建后再给庇护）。
    """
    engine = _engine("struct_yongyan_shelter")
    _fast_player(engine)
    m = _spiky_monster(engine)
    m.add_status(StatusEffect(name="庇护", value=2, remaining_rounds=-1, source="test"))
    _full_attack(engine, "永焰", [
        {"target_ref": "enemy:0", "damage": 6, "dodge": False,
         "parry": False, "blood_shadow": False},
        {"target_ref": "enemy:0", "damage": 6, "dodge": False,
         "parry": False, "blood_shadow": False},
    ])
    # 庇护把两击全部免疫：无真实降血 → 不构建；庇护2次耗尽
    assert m.current_hp == 100
    assert engine.combat.structure_layers(m, "永焰") == 0
    assert not any(s.name == "庇护" for s in m.status_effects)
    # 重新构建后再给庇护：回始烧灼被庇护按次免疫
    engine.state.duel_turn = "player_side"
    _full_attack(engine, "永焰", [
        {"target_ref": "enemy:0", "damage": 6, "dodge": False,
         "parry": False, "blood_shadow": False},
        {"target_ref": "enemy:0", "damage": 6, "dodge": False,
         "parry": False, "blood_shadow": False},
    ])
    assert engine.combat.structure_layers(m, "永焰") == 2
    m.add_status(StatusEffect(name="庇护", value=2, remaining_rounds=-1, source="test"))
    hp_before = m.current_hp
    engine.state.combat_subphase = "await_round_start"
    engine.execute_action("round_start", {})
    assert m.current_hp == hp_before, "庇护应整笔免掉永焰烧灼"
    shelter = next(s for s in m.status_effects if s.name == "庇护")
    assert shelter.value == 1


# ---------------------------------------------------------------- 脆化

def test_cuihua_amplifies_next_damage_then_clears():
    """脆化：下一击伤害+X后清空；空刀不触发；之后的伤害不再加成。"""
    engine = _engine("struct_cuihua")
    _fast_player(engine)
    m = _spiky_monster(engine, hp=100)
    token, _ = _prepare(engine, "脆化")
    _resolve(engine, token, [
        {"target_ref": "enemy:0", "damage": 5, "dodge": False,
         "parry": False, "blood_shadow": False}])
    assert engine.combat.structure_layers(m, "脆化") == 1
    hp_after_build = m.current_hp
    assert hp_after_build == 100 - 5
    # 空刀：脆化不触发、不清空
    token, _ = _prepare(engine)
    _resolve(engine, token, [
        {"target_ref": "enemy:0", "damage": 0, "dodge": False,
         "parry": False, "blood_shadow": False}])
    assert engine.combat.structure_layers(m, "脆化") == 1
    assert m.current_hp == hp_after_build
    # 下一击：伤害+1并清空
    token, _ = _prepare(engine)
    r = _resolve(engine, token, [
        {"target_ref": "enemy:0", "damage": 5, "dodge": False,
         "parry": False, "blood_shadow": False}])
    assert m.current_hp == hp_after_build - 6
    assert engine.combat.structure_layers(m, "脆化") == 0
    assert "cuihua" not in str(r)


def test_cuihua_stacks_and_hits_daowen_damage():
    """脆化多层叠加；道纹伤害（统一伤害入口）同样触发。

    字面规则口径：构建击本身造成伤害，目标已有的脆化层在下一击就会触发——
    多段普攻会把自己的脆化层打出去（第一击+1层、第二击吃+1并清空）。
    要攒层就一击即止或用其他伤害来源后置触发。
    """
    engine = _engine("struct_cuihua_stack")
    p = _fast_player(engine)
    m = _spiky_monster(engine, hp=100)
    # 单击构建：+1层，本击不吃加成（层是结算后才挂上的）
    _full_attack(engine, "脆化", [
        {"target_ref": "enemy:0", "damage": 5, "dodge": False,
         "parry": False, "blood_shadow": False}])
    assert engine.combat.structure_layers(m, "脆化") == 1
    assert m.current_hp == 100 - 5
    # 直接再叠2层 → 3层；道纹【虚耗】伤害触发（统一伤害入口）
    engine.combat.apply_structure_layer(p, m, "脆化")
    engine.combat.apply_structure_layer(p, m, "脆化")
    assert engine.combat.structure_layers(m, "脆化") == 3
    hp_before = m.current_hp
    from engine.daowen import DaoWenEngine
    calc = DaoWenEngine.resolve("虚耗", 1)
    engine.combat._apply_hostile_damage(m, calc["target_damage"], "普通", p)
    # 12X伤害 + 脆化3层
    assert m.current_hp == hp_before - calc["target_damage"] - 3
    assert engine.combat.structure_layers(m, "脆化") == 0


# ---------------------------------------------------------------- 震爆

def test_zhenbao_detonates_on_real_parry():
    """震爆：真刀招架成立→招架免疫照旧+返还速度，但引爆层数受X点伤害。"""
    engine = _engine("struct_zhenbao")
    _fast_player(engine)
    m = _spiky_monster(engine, hp=100)
    token, _ = _prepare(engine, "震爆")
    _resolve(engine, token, [
        {"target_ref": "enemy:0", "damage": 5, "dodge": False,
         "parry": False, "blood_shadow": False}])
    assert engine.combat.structure_layers(m, "震爆") == 1
    hp_before = m.current_hp
    speed_before = m.current_speed
    token, _ = _prepare(engine)
    r = _resolve(engine, token, [
        {"target_ref": "enemy:0", "damage": 5, "dodge": False,
         "parry": True, "blood_shadow": False}])
    hit = r["result"]["hits"][0]
    assert hit["parry_success"] is True
    assert hit["damage_dealt"] == 0, "招架免疫本击伤害"
    assert hit["zhenbao"]["damage"] == 1
    assert hit["zhenbao"]["layers"] == 1
    assert m.current_hp == hp_before - 1
    assert m.current_speed == speed_before, "招架-1速度+返还1速度"
    assert engine.combat.structure_layers(m, "震爆") == 0


def test_zhenbao_detonates_on_empty_parry_and_stacks():
    """震爆：空刀招架同样引爆（宣告即触发）；多层一次全清、伤害=层数。"""
    engine = _engine("struct_zhenbao_empty")
    _fast_player(engine)
    m = _spiky_monster(engine, hp=100)
    token, _ = _prepare(engine, "震爆")
    _resolve(engine, token, [
        {"target_ref": "enemy:0", "damage": 5, "dodge": False,
         "parry": False, "blood_shadow": False},
        {"target_ref": "enemy:0", "damage": 5, "dodge": False,
         "parry": False, "blood_shadow": False},
        {"target_ref": "enemy:0", "damage": 5, "dodge": False,
         "parry": False, "blood_shadow": False},
    ])
    assert engine.combat.structure_layers(m, "震爆") == 3
    hp_before = m.current_hp
    token, _ = _prepare(engine)
    r = _resolve(engine, token, [
        {"target_ref": "enemy:0", "damage": 0, "dodge": False,
         "parry": True, "blood_shadow": False}])
    hit = r["result"]["hits"][0]
    assert hit["parry_success"] is False, "空刀招架不返还速度、不算成功招架"
    assert hit["note"] == "招架空刀：未返还速度"
    assert hit["zhenbao"]["damage"] == 3
    assert m.current_hp == hp_before - 3
    assert engine.combat.structure_layers(m, "震爆") == 0


def test_zhenbao_not_consumed_when_parry_blocked():
    """必中封锁招架：招架不成立，震爆不引爆。"""
    engine = _engine("struct_zhenbao_musthit")
    _fast_player(engine)
    m = _spiky_monster(engine, hp=100)
    token, _ = _prepare(engine, "震爆")
    _resolve(engine, token, [
        {"target_ref": "enemy:0", "damage": 5, "dodge": False,
         "parry": False, "blood_shadow": False}])
    hp_before = m.current_hp
    r = engine.combat.resolve_attack(
        engine.state.player, m, dodge=False, parry=True,
        allocated_damage=5, is_must_hit=True)
    assert r["parry_success"] is False
    assert "zhenbao" not in r
    assert m.current_hp == hp_before - 5
    assert engine.combat.structure_layers(m, "震爆") == 1


# ---------------------------------------------------------------- 粘滞

def test_nianzhi_dodge_costs_extra_speed_then_clears():
    """粘滞：闪避成功付1+X点速度并清空；速度不足1+X时不能宣告闪避。"""
    engine = _engine("struct_nianzhi")
    _fast_player(engine)
    m = _spiky_monster(engine, hp=100)
    token, _ = _prepare(engine, "粘滞")
    _resolve(engine, token, [
        {"target_ref": "enemy:0", "damage": 5, "dodge": False,
         "parry": False, "blood_shadow": False},
        {"target_ref": "enemy:0", "damage": 5, "dodge": False,
         "parry": False, "blood_shadow": False},
    ])
    assert engine.combat.structure_layers(m, "粘滞") == 2
    assert engine.combat.structure_dodge_extra(m) == 2
    # 速度4 > 粘滞2：可闪避；成功后付1+2=3点速度，层数清空
    token, _ = _prepare(engine)
    r = _resolve(engine, token, [
        {"target_ref": "enemy:0", "damage": 5, "dodge": True,
         "parry": False, "blood_shadow": False}])
    hit = r["result"]["hits"][0]
    assert hit["dodge_success"] is True
    assert hit["nianzhi"]["extra_speed_paid"] == 2
    assert m.current_speed == 4 - 3
    assert engine.combat.structure_layers(m, "粘滞") == 0


def test_nianzhi_gate_blocks_dodge_declaration():
    """粘滞承载期速度不足：defender选项与结算闸门都拒绝闪避、不扣速度。"""
    engine = _engine("struct_nianzhi_gate")
    _fast_player(engine)
    m = _spiky_monster(engine, hp=100)
    token, _ = _prepare(engine, "粘滞")
    _resolve(engine, token, [
        {"target_ref": "enemy:0", "damage": 5, "dodge": False,
         "parry": False, "blood_shadow": False},
        {"target_ref": "enemy:0", "damage": 5, "dodge": False,
         "parry": False, "blood_shadow": False},
    ])
    m.current_speed = 2
    token, options = _prepare(engine)
    target_option = options["target_options"][0]
    assert target_option["can_dodge"] is False, "速度2 ≤ 1+粘滞2，不可宣告闪避"
    r = _resolve(engine, token, [
        {"target_ref": "enemy:0", "damage": 5, "dodge": True,
         "parry": False, "blood_shadow": False}])
    hit = r["result"]["hits"][0]
    assert hit["dodge_success"] is False
    assert "粘滞" in hit["dodge_fail_reason"]
    assert m.current_speed == 2, "闪避失败不扣速度"
    assert engine.combat.structure_layers(m, "粘滞") == 2


# ---------------------------------------------------------------- 生命周期

def test_structures_cleared_at_battle_end():
    """结构层 scope=BATTLE：[战终]清除（层数不跨战斗）。"""
    engine = _engine("struct_cleanup")
    _fast_player(engine)
    m = _spiky_monster(engine)
    _full_attack(engine, "永焰", [
        {"target_ref": "enemy:0", "damage": 5, "dodge": False,
         "parry": False, "blood_shadow": False}])
    assert engine.combat.structure_layers(m, "永焰") == 1
    # 战终要求敌方全部命零：判死但保留在列表里（cleanup 遍历 state.enemies）
    m.is_alive = False
    engine.state.combat_subphase = "await_round_start"
    r = engine.execute_action("battle_end", {})
    assert r["success"], r
    assert engine.combat.structure_layers(m, "永焰") == 0


def test_structures_are_continuous_effects_for_liudong():
    """裁定③：结构层按持续Y类——在【流动】/【遗忘】候选（非过期状态）之列。"""
    engine = _engine("struct_liudong_class")
    _fast_player(engine)
    m = _spiky_monster(engine)
    token, _ = _prepare(engine, "永焰")
    _resolve(engine, token, [
        {"target_ref": "enemy:0", "damage": 5, "dodge": False,
         "parry": False, "blood_shadow": False}])
    status = next(s for s in m.status_effects if s.name == "永焰")
    assert not status.is_expired
    assert status.remaining_rounds == -1, "结构无持续时间（-1=本场内持续）"
    # 回响不可延长（无正数持续回合）
    assert not engine.combat._huixiang_extendable(status)
