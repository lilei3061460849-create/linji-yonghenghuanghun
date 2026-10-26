"""战斗结算「单一真源」契约。

目标：同一条规则不在 AI / 战斗执行 / 战报 / 预览 / 测试里各实现一遍。

 1. 普通伤害（杀伐 → 生命变化）：返回数值 == 实体真实状态 == 战报渲染数；
 2. 多层触发：一次失血链上每层各结算一次，记录完整解释最终状态；
 3. 闪避：攻击 / 道纹 / 法术的整次判定失效，代价已付、效果不落地；
 4. 招架只在攻击的 resolve_attack 逐击结算；真刀免疫该击并返还速度，详见
    tests/test_blade_combat_rework.py；通用伤害入口不再执行招架调整；
 5. 格挡与数值型代价分离，代价不进入格挡或攻击防御通道；
 6. 残韵：A 道纹 → 转换 → B 道纹，且同名只保留一份；
 7. 自定义法术的一步与普通道纹共用核心规则（同 X 同数值）；
 8. 结构护栏：法力支付与道纹效果入口不得在多处重复实现。

预览 ↔ 正式执行的等价契约在 tests/test_action_preview_parity.py；其中的
`declare_parry` 样例只验证旧兼容入口会被一致拒绝。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from engine.api import GameEngine                       # noqa: E402
from engine.combat import CombatEngine                 # noqa: E402
from engine.dice import DiceEngine                      # noqa: E402
from engine.models import DaoWen, DaoWenInstance, Entity, GameState, Relic  # noqa: E402
from engine import battle_report as BR                  # noqa: E402
from tests.setup_support import (  # noqa: E402
    begin_battle, begin_round, finish_initial_daowen, strip_monster_relics,
)

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def _battle_engine(tmp_path, suffix: str, region: str = "龙心谷") -> GameEngine:
    """走完整开局流程进入战斗第一回合，返回引擎。"""
    e = GameEngine(db_path=str(tmp_path / f"sss_{suffix}.db"), rng_seed=1)
    e.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e, "杀伐")
    e.execute_action("setup_choose_resonance", {"resonance_type": "曲解"})
    setup = e.execute_action("setup_choose_region", {"region": region})
    if (setup.get("result") or {}).get("relic_choices"):
        e.execute_action("choose_discovered_relic",
                         {"relic_name": setup["result"]["relic_choices"][0]})
    assert begin_battle(e)["success"]
    assert begin_round(e)["success"]
    p = e.state.player
    p.mana_limit = 40
    p.current_mana = 40
    return e


def _ref(engine: GameEngine, entity: Entity) -> str:
    refs = engine.combat._combat_entity_refs()
    return next(r for r, ent in refs.items() if ent is entity)


def _give(ent: Entity, *names: str) -> None:
    for name in names:
        ent.dao_wen[name] = DaoWenInstance(DaoWen(
            name=name, formula="", cost_type="消耗", cost_formula="X", effect_formula=""))


def _arena(bl: int = 300, hp: int = 300, mana: int = 20, shield: int = 0,
           relics=()) -> tuple[GameState, CombatEngine, Entity, Entity]:
    """不跑完整开局的最小战斗场，用于规则层断言。"""
    state = GameState()
    state.rng_seed = 1
    dice = DiceEngine(seed=1)
    player = Entity(name="P", entity_type="轮回者", blood_limit=bl, current_hp=hp,
                    mana_limit=mana, current_mana=mana, speed_limit=5, current_speed=5,
                    attack_count=1, attack_power=0, shield=shield)
    enemy = Entity(name="E", entity_type="怪物", blood_limit=200, current_hp=200,
                   mana_limit=0, current_mana=0, speed_limit=4, current_speed=4,
                   attack_count=1, attack_power=10)
    state.player = player
    state.enemies = [enemy]
    state.relics = [Relic(name=n, effect="") for n in relics]
    return state, CombatEngine(state, dice), player, enemy


def _bleed_cost(combat: CombatEngine, ent: Entity, amount: int) -> None:
    """付一笔【流血】数值代价（不进入攻击防御或格挡通道）。"""
    combat.pay_numeric_cost(ent, "流血", amount, cost_context={
        "timing": "player_action", "source": "透支", "source_type": "daowen",
        "actor": ent, "target": ent, "mechanic": "cost",
        "subtype": "bleed", "amount": amount, "tags": {"active_payment"}})


# ===========================================================================
# 1. 普通伤害：杀伐 → 生命变化
# ===========================================================================

# 逐击攻击招架由 tests/test_blade_combat_rework.py 锁定；本文件验证通用伤害路径。

def test_plain_damage_numbers_match_the_state_and_the_report(tmp_path):
    """正常路径：杀伐X 打出的伤害，返回/实体/战报三处必须是同一组数。

    不写死 25 这类魔法数字——断言的是「三处一致」，规则数值怎么改都不会假绿。
    """
    e = _battle_engine(tmp_path, "plain")
    p, foe = e.state.player, e.state.enemies[0]
    foe.blood_limit = 500
    foe.current_hp = 500
    foe.current_speed = 0
    hp_before = foe.current_hp

    r = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 2,
                                        "target_ref": _ref(e, foe)})
    assert r["success"], r
    damages = [x for x in r["execution"]["effects"] if x["type"] == "damage"]
    assert damages, r["execution"]["effects"]
    d = damages[0]

    # 返回的数值 == 实体真实状态
    assert d["hp_before"] == hp_before
    assert d["hp_after"] == foe.current_hp
    assert hp_before - foe.current_hp == d["actual_damage"] + d.get("shield_absorbed", 0)
    assert d["actual_damage"] > 0, "X=2 的杀伐必须真的掉血"

    # 战报只翻译、不推导：渲染出的数字就是引擎给出的那组
    text = "\n".join(BR.format_player_action(1, p.name, r))
    assert str(d["hp_before"]) in text and str(d["hp_after"]) in text, text


def test_daowen_damage_is_not_double_counted_anywhere(tmp_path):
    """边界：同一笔伤害在 execution、失血总账、事件流里只记一次。"""
    e = _battle_engine(tmp_path, "counted_once")
    foe = e.state.enemies[0]
    foe.blood_limit = 500
    foe.current_hp = 500
    foe.current_speed = 0

    r = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 3,
                                        "target_ref": _ref(e, foe)})
    assert r["success"], r
    d = next(x for x in r["execution"]["effects"] if x["type"] == "damage")
    # 失血事件（失血总账的唯一写入点）记的数 == 实际掉的数：不多记也不少记
    assert d["hp_loss_ctx"]["amount"] == d["actual_damage"], (
        d["hp_loss_ctx"], d["actual_damage"])
    assert d["hp_loss_ctx"]["parent_event_id"] == d["ctx"]["event_id"], (
        "失血事件必须挂在本次伤害事件之下，因果链才连得起来")


# ===========================================================================
# 2. 多层触发
# ===========================================================================

def test_layered_trigger_chain_settles_each_layer_once(tmp_path):
    """攻击 →（失去生命后）再生自身 → 杀伐攻击者：三层各结算一次，且能解释最终状态。

    用的是真实反应窗口（CombatEngine._fire_auto_reaction）与真实法术构建入口
    （_build_custom_spell），不手搓模拟链。
    """
    e = _battle_engine(tmp_path, "layers")
    p, foe = e.state.player, e.state.enemies[0]
    strip_monster_relics(e)  # 怪物随机遗物（活血衣等）会额外回血，破坏总账等式
    _give(foe, "再生", "杀伐")
    foe.mana_limit = 40
    foe.current_mana = 40
    foe.blood_limit = 300
    foe.current_hp = 300
    p.blood_limit = 300
    p.current_hp = 300

    built = e._build_custom_spell(foe, {
        "name": "反咬", "required_daowen": ["再生", "杀伐"],
        "trigger_condition": "失去生命后",
        "effect_flow": "发动再生X于自身→发动杀伐X于攻击者"})
    assert "error" not in built, built
    foe.spells.append(built["spell"])

    prepared = e.execute_action("prepare_attack", {"actor_ref": "player:0"})
    assert prepared["success"], prepared
    opt = prepared["result"]["target_options"][0]
    steps = [{"daowen": "再生", "x": 1, "dodge": False, "target_ref": _ref(e, foe)},
             {"daowen": "杀伐", "x": 2, "dodge": False, "target_ref": _ref(e, p)}]
    hits = [{"target_ref": opt["ref"], "dodge": False, "blood_shadow": False,
             "spell_choices": {"before": {}, "after": {"反咬": {"use": True, "steps": steps}}}}
            for _ in range(prepared["result"]["hit_count"])]
    r = e.execute_action("resolve_attack", {"token": prepared["result"]["token"], "hits": hits})
    assert r["success"], r

    hits_result = r["result"]["hits"]
    assert hits_result, "至少结算一次命中"
    for hit in hits_result:
        logs = hit.get("spell_logs") or []
        daowen_seq = [log.get("daowen") for log in logs if isinstance(log, dict)]
        assert daowen_seq == ["再生", "杀伐"], (
            f"一次失血只应把这条链各触发一次，实际序列 {daowen_seq}")

    # 各层记录之和 == 最终状态的变化（能解释「为什么变成这样」）
    dealt = sum(h["damage_dealt"] for h in hits_result)
    healed = sum(eff.get("actual_heal", 0)
                 for h in hits_result for log in (h.get("spell_logs") or [])
                 if isinstance(log, dict)
                 for eff in ((log.get("execution") or {}).get("effects") or []))
    countered = sum(eff.get("actual_damage", 0)
                    for h in hits_result for log in (h.get("spell_logs") or [])
                    if isinstance(log, dict)
                    for eff in ((log.get("execution") or {}).get("effects") or [])
                    if eff.get("type") == "damage")
    assert foe.current_hp == 300 - dealt + healed, (foe.current_hp, dealt, healed)
    assert p.current_hp == 300 - countered, (p.current_hp, countered)
    assert healed > 0 and countered > 0, "第二层（再生）与第三层（杀伐）都必须真的结算了"


def test_auto_reaction_is_not_fired_twice_for_one_life_loss(tmp_path):
    """边界：一次失血只开一次「失去生命后」窗口（攻击路径与非攻击路径都不双发）。"""
    e = _battle_engine(tmp_path, "no_double_fire")
    p, foe = e.state.player, e.state.enemies[0]
    _give(p, "再生")
    p.blood_limit = 300
    p.current_hp = 300
    p.actions_used_this_round = 0
    defined = e.execute_action("define_spell", {"spell": {
        "name": "急救", "required_daowen": ["再生"],
        "trigger_condition": "失去生命后", "effect_flow": "发动再生X于自身"}})
    assert defined["success"], defined

    before = p.current_hp
    detail = e.combat._apply_hostile_damage(p, 30, source=foe)
    assert detail.get("actual_damage", 0) == 30
    # 「失去生命后」的窗口挂在失血事件上（_record_hp_loss_event 是唯一触发点）
    loss = detail["hp_loss_ctx"]
    assert loss["parent_event_id"] == detail["ctx"]["event_id"], (
        "失血事件必须挂在本次伤害事件之下")
    logs = loss.get("reaction_logs") or []
    heals = [eff for log in logs if isinstance(log, dict)
             for eff in ((log.get("execution") or {}).get("effects") or [])
             if eff.get("type") == "heal"]
    assert len(heals) == 1, f"一次失血只应把「失去生命后」结算一次，实际 {len(heals)} 次"
    # 最终生命 = 掉血 + 反应回复：状态变化能被链上的每一层完整解释
    assert p.current_hp == before - 30 + heals[0]["actual_heal"], (
        p.current_hp, before, heals[0])


# ===========================================================================
# 3. 闪避：整个解析失败
# ===========================================================================

def test_full_dodge_cancels_a_daowen_resolution(tmp_path):
    """正常路径：目标闪避 → 整次道纹解析失败（效果为空），代价照付。"""
    e = _battle_engine(tmp_path, "dodge_dw")
    p, foe = e.state.player, e.state.enemies[0]
    foe.blood_limit = 500
    foe.current_hp = 500
    foe.current_speed = 5
    mana_before = p.current_mana

    r = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 2,
                                        "target_ref": _ref(e, foe), "dodge": True})
    assert r["success"], r
    assert r["dodge"]["fully_dodged"] is True
    assert r["execution"]["effects"] == [], "闪避后不得有任何效果落地"
    assert foe.current_hp == 500, "闪避后目标生命不变"
    assert p.current_mana < mana_before, "闪避不退还已支付的法力"


def test_full_dodge_cancels_a_spell_step(tmp_path):
    """法术里的一步被闪避 → 该步 status=dodged，后续数值不落地。"""
    e = _battle_engine(tmp_path, "dodge_spell")
    foe = e.state.enemies[0]
    foe.blood_limit = 500
    foe.current_hp = 500
    foe.current_speed = 5

    r = e.execute_action("cast", {
        "flow": "发动杀伐X于目标",
        "steps": [{"x": 2, "dodge": True}],
        "target_ref": _ref(e, foe)})
    assert r["success"], r
    steps = r["result"]["step_results"]
    assert steps and steps[0]["status"] == "dodged"
    assert foe.current_hp == 500


def test_full_dodge_cancels_an_attack(tmp_path):
    """攻击的每一击都能被闪避：伤害为 0、只扣速度。"""
    e = _battle_engine(tmp_path, "dodge_atk")
    foe = e.state.enemies[0]
    foe.blood_limit = 500
    foe.current_hp = 500
    foe.current_speed = 9

    from tests.attack_support import resolve_attack
    r = resolve_attack(e, attacker_name="贾凡", target_selections=[0], dodge=True)
    assert r["success"], r
    hits = r["result"]["hits"]
    assert hits, "至少结算一次命中"
    for hit in hits:
        assert hit["dodge_success"] is True
        assert hit["damage_dealt"] == 0
        assert hit["hp_lost"] == 0
    assert foe.current_hp == 500


def test_dodge_needs_speed(tmp_path):
    """错误路径：速度不足时闪避不成立（不是静默无效）。"""
    e = _battle_engine(tmp_path, "dodge_no_speed")
    foe = e.state.enemies[0]
    foe.blood_limit = 500
    foe.current_hp = 500
    foe.current_speed = 0

    r = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 2,
                                        "target_ref": _ref(e, foe), "dodge": True})
    assert r["success"], r
    assert not r["dodge"].get("fully_dodged")
    assert foe.current_hp < 500, "速度不足 → 闪避失败 → 伤害照常落地"


# ===========================================================================
# 4. 逐击攻击招架的专门覆盖见 tests/test_blade_combat_rework.py。
# ===========================================================================

# ===========================================================================
# 5. 格挡 ≠ 攻击招架
# ===========================================================================

def test_generic_damage_uses_shield_without_attack_parry():
    """普通伤害走格挡通道；逐击攻击招架只在 resolve_attack 中处理。"""
    _, combat, player, enemy = _arena(bl=300, hp=300, shield=10)
    detail = combat._apply_hostile_damage(player, 50, source=enemy)
    assert detail["shield_absorbed"] == 10
    assert detail["actual_damage"] == 40
    assert player.current_hp == 260


def test_numeric_cost_bypasses_shield_and_attack_resolution():
    """边界：【代价】经独立支付通道，不消耗格挡，也不走逐击攻击结算。"""
    _, combat, player, _ = _arena(bl=300, hp=300, shield=25)

    _bleed_cost(combat, player, 12)

    assert player.current_hp == 288, "代价全额支付"
    assert player.shield == 25, "格挡不吸收代价"


# ===========================================================================
# 6. 残韵：A 道纹 → 转换 → B 道纹 + 同名唯一性
# ===========================================================================

def test_resonance_converts_holder_daowen_and_grants_one_copy(tmp_path):
    """正常路径：持有者的源道纹永久变为目标道纹，施法者获得一份；同名只留一份。"""
    e = _battle_engine(tmp_path, "reso")
    p, foe = e.state.player, e.state.enemies[0]
    _give(foe, "再生")
    assert e.state.resonance.get("曲解", 0) >= 1

    r = e.execute_action("use_resonance", {
        "source_daowen": "再生", "resonance_type": "曲解", "target_daowen": "庇护", "target_ref": _ref(e, foe)})
    assert r["success"], r
    dest = r["result"]["target"]
    assert dest == "庇护"
    assert "再生" not in foe.dao_wen, "源道纹必须消失"
    assert "庇护" in foe.dao_wen, "持有者变为转化后的道纹"
    assert "庇护" in p.dao_wen, "施法者同时获得转化后的道纹"
    assert r["granted_daowen"] == dest


def test_resonance_never_creates_a_duplicate_name(tmp_path):
    """边界：施法者已持有同名道纹时，只消耗残韵、不重复授予。"""
    e = _battle_engine(tmp_path, "reso_dup")
    p, foe = e.state.player, e.state.enemies[0]
    _give(p, "庇护")
    _give(foe, "再生")
    e.state.resonance["曲解"] = 2

    r = e.execute_action("use_resonance", {
        "source_daowen": "再生", "resonance_type": "曲解", "target_daowen": "庇护", "target_ref": _ref(e, foe)})
    assert r["success"], r
    assert r["granted_daowen"] is None, "已持有同名道纹时不得重复授予"
    assert list(p.dao_wen).count("庇护") == 1

    # 再转一次：持有者侧同样不得出现两份同名
    _give(foe, "再生")
    r2 = e.execute_action("use_resonance", {
        "source_daowen": "再生", "resonance_type": "曲解", "target_daowen": "庇护", "target_ref": _ref(e, foe)})
    assert r2["success"], r2
    assert list(foe.dao_wen).count("庇护") == 1, "持有者侧同名只保留一份"
    assert "再生" not in foe.dao_wen


def test_resonance_is_not_consumed_when_it_does_not_resolve(tmp_path):
    """错误路径：源道纹不存在 → 失败且不消耗残韵。"""
    e = _battle_engine(tmp_path, "reso_fail")
    stock_before = dict(e.state.resonance)
    r = e.execute_action("use_resonance", {
        "source_daowen": "不存在的道纹", "resonance_type": "曲解"})
    assert not r["success"], r
    assert e.state.resonance == stock_before, "未生效不得消耗残韵"


# ===========================================================================
# 7. 自定义法术 vs 普通道纹：同走核心规则
# ===========================================================================

def _one_daowen_hit(e: GameEngine, foe: Entity, x: int) -> dict:
    r = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": x,
                                        "target_ref": _ref(e, foe)})
    assert r["success"], r
    d = next(x2 for x2 in r["execution"]["effects"] if x2["type"] == "damage")
    return {"hp": foe.current_hp, "dmg": d["actual_damage"],
            "shield": d.get("shield_absorbed", 0),
            "mana": e.state.player.current_mana,
            "used": e.state.player.actions_used_this_round}


def _one_spell_hit(e: GameEngine, foe: Entity, x: int) -> dict:
    r = e.execute_action("cast", {"flow": "发动杀伐X于目标", "steps": [{"x": x}],
                                  "target_ref": _ref(e, foe)})
    assert r["success"], r
    d = next(x2 for x2 in r["result"]["steps"][0]["execution"]["effects"]
             if x2["type"] == "damage")
    return {"hp": foe.current_hp, "dmg": d["actual_damage"],
            "shield": d.get("shield_absorbed", 0),
            "mana": e.state.player.current_mana,
            "used": e.state.player.actions_used_this_round}


def test_spell_step_and_plain_daowen_produce_identical_settlement(tmp_path):
    """同一个道纹、同一个 X：走法术一步与直接发动，伤害/法力/出手逐项相同。"""
    base = _battle_engine(tmp_path, "parity_a")
    foe_a = base.state.enemies[0]
    foe_a.blood_limit = 500
    foe_a.current_hp = 500
    foe_a.current_speed = 0
    via_daowen = _one_daowen_hit(base, foe_a, 3)

    other = _battle_engine(tmp_path, "parity_b")
    foe_b = other.state.enemies[0]
    foe_b.blood_limit = 500
    foe_b.current_hp = 500
    foe_b.current_speed = 0
    via_spell = _one_spell_hit(other, foe_b, 3)

    assert via_daowen["dmg"] == via_spell["dmg"] > 0
    assert via_daowen["shield"] == via_spell["shield"]
    assert via_daowen["hp"] == via_spell["hp"]
    assert 40 - via_daowen["mana"] == 40 - via_spell["mana"], "法力支付口径必须一致"
    assert via_daowen["used"] == via_spell["used"], "出手消耗口径必须一致"


def test_spell_step_and_plain_daowen_share_the_mana_payment_rule(tmp_path):
    """边界：法力不足时两条路径都必须拒绝，且不扣任何法力。"""
    e = _battle_engine(tmp_path, "mana_gate")
    p, foe = e.state.player, e.state.enemies[0]
    foe.blood_limit = 500
    foe.current_hp = 500
    foe.current_speed = 0
    p.current_mana = 1
    hp_before = foe.current_hp

    r_dw = e.execute_action("use_daowen", {"daowen_name": "杀伐", "x": 5,
                                           "target_ref": _ref(e, foe)})
    assert not r_dw["success"] and "法力不足" in r_dw["error"]
    assert p.current_mana == 1 and foe.current_hp == hp_before

    r_sp = e.execute_action("cast", {"flow": "发动杀伐X于目标", "steps": [{"x": 5}],
                                     "target_ref": _ref(e, foe)})
    assert r_sp["success"], r_sp          # 施法动作本身发生了
    step = r_sp["result"]["step_results"][0]
    assert step["status"] == "interrupted"
    assert p.current_mana == 1 and foe.current_hp == hp_before, "法力不足不得部分扣费"


# ===========================================================================
# 8. 结构护栏：法力支付与道纹入口不得重复实现
# ===========================================================================

def _read(rel_path: str) -> str:
    return (ROOT / rel_path).read_text(encoding="utf-8")


def test_daowen_mana_payment_is_not_written_twice():
    """护栏：use_daowen 与法术单步共用 CombatEngine.pay_daowen_mana。

    历史 bug：api.py 内联了一份多带 `cost_mutation` 兜底的副本，与法术单步口径
    表面相同、实则不同（只是碰巧没有消耗类道纹缺 cost 键才没分叉）。
    """
    api_src = _read("engine/api.py")
    assert "pay_daowen_mana(" in api_src, "use_daowen 必须调用共享的法力支付口径"
    assert "calc.get(\"cost\", calc.get(\"cost_mutation\", 0))" not in api_src, (
        "use_daowen 不得再内联一份法力支付")


def test_shared_daowen_entry_points_are_actually_shared():
    """护栏：道纹效果结算只有一个公开入口，且两条路径都走它。"""
    api_src = _read("engine/api.py")
    spells_src = _read("engine/combat_parts/spells.py")
    assert api_src.count("apply_daowen_effect(") >= 1
    assert spells_src.count("apply_daowen_effect(") >= 1
    assert api_src.count("_daowen_step_preflight(") >= 1
    assert spells_src.count("_daowen_step_preflight(") >= 2  # 定义 + 单步调用
