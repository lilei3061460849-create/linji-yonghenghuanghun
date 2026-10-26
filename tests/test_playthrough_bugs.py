"""手操通关(seed 2026081202)暴露的四条引擎回归锁。

1. 残韵打在敌人道纹上：永久改写敌人持有，施法者同时永久获得变化后道纹
2. 回始获得等同当前法限的法力（加法）；战始先清零再结算遗物
3. 原初X借到的【杀伐】等非优先队列道纹必须在怪物回合发动（按原版2X，无×3）
4. 残骸：恢复20生命并获得异变10

每条均覆盖正常路径 / 边界 / 错误输入。
"""
import os
import math
import sys

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import Consumable, DaoWen, DaoWenInstance, Entity, Relic


def finish_round(engine):
    engine.state.combat_subphase = "await_round_end"
    return engine.execute_action("round_end", {})


# 加点 11/8/6 在 DM裁定 2026-09-10 口径下的面板：1属性点=6[血限]、2属性点=1[速限]=1[法限]。
# 以下期望值一律由此推导，不再硬编码——旧口径（1点=1速限=2法限）下的 14/8 已作废。
_BLOOD_P, _SPEED_P, _MANA_P = 11, 8, 6
EXP_BLOOD = _BLOOD_P * 6        # 66
EXP_SPEED = _SPEED_P // 2       # 4
EXP_MANA = _MANA_P // 2         # 3


def _engine(suffix: str) -> GameEngine:
    engine = GameEngine(db_path=f"data/test_playthrough_{suffix}.db", rng_seed=1)
    engine.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": "罪孽都市"})
    optional = set()   # 2026-10-13：【三相残韵盘】删除，无需规避
    choice = next((n for n in setup["result"]["relic_choices"] if n not in optional),
                  setup["result"]["relic_choices"][0])
    engine.execute_action("choose_discovered_relic", {"relic_name": choice})
    engine.state.energy = 0
    return engine


def _give_daowen(entity: Entity, name: str, x: int = 1, tags=None) -> None:
    entity.dao_wen[name] = DaoWenInstance(
        DaoWen(name=name, formula="", cost_type="消耗", cost_formula="X",
               effect_formula="", tags=list(tags or [])),
        x_value=x,
    )


def _put_enemy(engine: GameEngine, monster: Entity) -> None:
    engine.state.enemies.clear()
    engine.state.monster_reinforcements.clear()
    engine.state.enemies.append(monster)


def _advance_to_active_round(engine: GameEngine) -> None:
    """计算单元测试直接构造第2回合玩家行动阶段，避免攻击力0的夹具触发雕塑。"""
    engine.state.current_round = 2
    engine.state.combat_subphase = "player_actions"


def _resolve_prepared_monsters(engine: GameEngine, daowen_name: str | None = None):
    prepared = engine.execute_action("prepare_monster_phase", {})
    choices = []
    for actor in prepared["result"]["actors"]:
        dao = None
        if daowen_name is not None:
            option = next(o for o in actor["daowen_options"] if o["name"] == daowen_name)
            dao = {"name": daowen_name, "dodge": False, "blood_shadow": False, "trigger_spell_choices": {}}
            if option["requires_target"]:
                dao["target_ref"] = "player:0"
            if option["dodge_submission"] == "per_target":
                from sim.monster_targets import pick_wave_dodge_targets
                dao["dodge_targets"] = pick_wave_dodge_targets(option)
        attacks = [{"hits": [{"target_ref": "player:0", "dodge": False, "blood_shadow": False, "spell_choices": {"before": {}, "after": {}}}
                              for _ in range(actor["base_hits_per_attack"])]}
                   for _ in range(actor["base_attack_actions"])]
        choices.append({"actor_ref": actor["actor_ref"], "daowen": dao,
                        "attack_actions": attacks})
    return engine.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"], "choices": choices,
    })


# ========================================================================
# 1. 残韵打在敌人道纹上
# ========================================================================

def test_resonance_on_enemy_grants_dest_and_rewrites_next_activation():
    """正常路径：反转敌方【全力】→ 敌人永久变为【弱化】，贾凡同时永久获得【弱化】。

    2026-10-03：原用例把敌方【狂暴】反转成其转化分支；【狂暴】删除后该边不存在，
    改用仍有效的 全力→(反转)→弱化。
    """
    engine = _engine("res_happy")
    engine.state.resonance["反转"] = 1
    engine.execute_action("battle_start", {})
    monster = Entity(name="通缉犯", entity_type="怪物", blood_limit=80, current_hp=80,
                     attack_count=1, attack_power=5, mana_limit=11, current_mana=11,
                     speed_limit=4, current_speed=4)
    _give_daowen(monster, "全力", x=1)
    monster._had_monster_daowen = True
    _put_enemy(engine, monster)
    engine.execute_action("round_start", {})

    r = engine.execute_action("use_resonance", {
        "source_daowen": "全力", "resonance_type": "反转", "target_ref": "enemy:0",
    })
    assert r["success"], r
    assert r["granted_daowen"] == "弱化"
    assert "弱化" in engine.state.player.dao_wen
    assert "全力" not in monster.dao_wen
    assert "弱化" in monster.dao_wen
    assert engine.state.resonance["反转"] == 0

    finish_round(engine)
    engine.execute_action("round_start", {})
    prepared = engine.execute_action("prepare_monster_phase", {})
    actor = prepared["result"]["actors"][0]
    option = next(o for o in actor["daowen_options"] if o["name"] == "弱化")
    assert option, "改写后的【加速】应成为怪物可发动道纹"
    phase = engine.execute_action("resolve_monster_phase", {
        "token": prepared["result"]["token"],
        "choices": [{
            "actor_ref": actor["actor_ref"],
            "daowen": {"name": "弱化", "target_ref": "player:0",
                       "dodge": False, "blood_shadow": False, "trigger_spell_choices": {}},
            # 2026-10-25：击数＝[攻击次数]＝当前速度，按 prepare 契约提交完整命中数
            "attack_actions": [{"hits": [{"target_ref": "player:0", "dodge": False,
                                          "blood_shadow": False,
                                          "spell_choices": {"before": {}, "after": {}}}
                                         for _ in range(actor["base_hits_per_attack"])]}],
        }],
    })
    assert phase["success"], phase
    details = phase["result"]["details"]
    activated = [d for d in details if d.get("daowen_activated") == "弱化"]
    assert activated, f"应发动永久改写后的弱化: {details}"
    assert engine.state.player.has_status("弱化"), "改写后的道纹必须真实生效"
    assert "弱化" in monster.dao_wen and "全力" not in monster.dao_wen


def test_resonance_no_duplicate_when_caster_already_owns_dest():
    """边界：施法者已持有变化后道纹则不重复获得；目标道纹仍永久变化。"""
    engine = _engine("res_bound")
    engine.state.resonance["反转"] = 1
    _give_daowen(engine.state.player, "弱化")
    engine.execute_action("battle_start", {})
    monster = Entity(name="唯一全力", entity_type="怪物", blood_limit=80, current_hp=80,
                     attack_count=1, attack_power=4)
    _give_daowen(monster, "全力", x=1)
    _give_daowen(monster, "全力", x=1)
    monster._had_monster_daowen = True
    _put_enemy(engine, monster)
    engine.execute_action("round_start", {})

    r = engine.execute_action("use_resonance", {
        "source_daowen": "全力", "resonance_type": "反转",
    })
    assert r["success"], r
    assert r["granted_daowen"] is None
    assert list(engine.state.player.dao_wen).count("弱化") == 1
    assert "全力" not in monster.dao_wen
    assert "弱化" in monster.dao_wen


def test_resonance_fails_without_holder_or_stock():
    """错误输入：目标没有源道纹 / 没有残韵库存 → 失败且不消耗。"""
    engine = _engine("res_invalid")
    engine.state.resonance["反转"] = 1
    engine.execute_action("battle_start", {})
    monster = Entity(name="白板怪", entity_type="怪物", blood_limit=50, current_hp=50,
                     attack_count=1, attack_power=1)
    _put_enemy(engine, monster)
    engine.execute_action("round_start", {})

    r = engine.execute_action("use_resonance", {
        "source_daowen": "狂暴", "resonance_type": "反转", "target_ref": "enemy:0",
    })
    assert r["success"] is False
    assert engine.state.resonance["反转"] == 1
    assert not ({"狂暴"} & set(engine.state.player.dao_wen)), "已删道纹不得出现"

    engine.state.resonance["反转"] = 0
    r2 = engine.execute_action("use_resonance", {
        "source_daowen": "杀伐", "resonance_type": "反转",
    })
    assert r2["success"] is False
    assert "杀伐" in engine.state.player.dao_wen


# ========================================================================
# 2. 法力一池制（DM裁定 2026-09-09）：[战始]直接给满法限（先给满再结算遗物），
#    [回始]不回填，[回终]/[敌回终]不清空，[战终]复原。
#    守夜灯 2026-10-25 用户令改到[回终]：获得等同当前回合数的法力。
# ========================================================================

def test_zhesu_overflow_survives_first_round_start():
    """正常路径：超池法力在回始不被回填/钳制。

    2026-09-13 用户裁定「所有属性不得超过其上限」后，超池本身已不可能出现，
    本条改守「回始不回填」这件仍然成立的事。
    2026-10-12 用户令删掉【血契】的「[回始]流血4X换X法力」后，原本挂在这里的
    第二段（血契流血换法力）随之移除——那半功能与道纹【透支】重合，已不存在。
    """
    engine = _engine("mana_happy")
    p = engine.state.player
    assert p.mana_limit == EXP_MANA and p.speed_limit == EXP_SPEED
    engine.execute_action("battle_start", {"relic_choices": {}})
    assert p.current_mana == EXP_MANA
    p.spend_mana(2)
    engine.execute_action("round_start", {})
    assert p.current_mana == EXP_MANA - 2, f"一池制：回始不回填，应仍{EXP_MANA - 2}，实{p.current_mana}"



def test_round_start_does_not_refill_mana():
    """边界（DM裁定 2026-09-09）：一池制下[回始]不回填，残量原样保留、跨回合不被清。"""
    engine = _engine("mana_bound")
    p = engine.state.player
    engine.execute_action("battle_start", {})
    assert p.current_mana == p.mana_limit == EXP_MANA, "战始给满一池"
    p.current_mana = 3
    engine.execute_action("round_start", {})
    assert p.current_mana == 3, f"回始不得回填，实{p.current_mana}"

    finish_round(engine)
    assert p.current_mana == 3, f"回终不得清空，实{p.current_mana}"
    engine.execute_action("round_start", {})
    assert p.current_mana == 3, f"下一回始仍是3，实{p.current_mana}"


def test_no_zhesu_means_no_bonus_mana():
    """错误输入/对照：无加法力遗物时战始只有满池、不额外加法力、不扣速度。"""
    engine = _engine("mana_invalid")
    p = engine.state.player
    engine.execute_action("battle_start", {})
    assert p.current_mana == p.mana_limit == EXP_MANA
    assert p.current_speed == EXP_SPEED


def test_shouyedeng_bonus_is_its_own_slice():
    """正常路径（2026-10-25 用户令）：守夜灯[回终]授予等同当前回合数的法力，授予的法力不再清空；
    一池制下回始本身仍不回填。"""
    engine = _engine("lamp_happy")
    p = engine.state.player
    engine.state.relics.append(Relic(name="守夜灯", effect="[回终]获得等同于当前回合数的法力"))
    engine.execute_action("battle_start", {})
    engine.execute_action("round_start", {})
    round_no = engine.state.current_round
    # 满池时授予会被[法限]全额吃掉，先花干净才看得到授予量。
    p.spend_mana(p.current_mana)
    finish_round(engine)
    assert p.current_mana == min(EXP_MANA, round_no), (
        f"第{round_no}回合回终应授予{round_no}点（受[法限]{EXP_MANA}截断），实{p.current_mana}")
    base = p.current_mana
    engine.execute_action("round_start", {})
    assert p.current_mana == base, f"回始不回填，实{p.current_mana}"


def test_shouyedeng_refills_a_spent_pool_up_to_limit():
    """边界：守夜灯只把花掉的池子回充，且填不过[法限]。"""
    engine = _engine("lamp_bound")
    p = engine.state.player
    engine.state.relics.append(Relic(name="守夜灯", effect="[回终]获得等同于当前回合数的法力"))
    engine.execute_action("battle_start", {"relic_choices": {}})
    engine.state.current_round = 10  # 远超法限，验证截断
    p.spend_mana(p.current_mana)
    engine.combat._grant_shouyedeng(p)
    assert p.current_mana == EXP_MANA, (
        f"回终+10（受[法限]{EXP_MANA}截断），实{p.current_mana}")
    # 满池后再授予：溢出丢弃，不会超过上限。
    engine.combat._grant_shouyedeng(p)
    assert p.current_mana == EXP_MANA, "满池再授予应溢出丢弃"


def test_no_shouyedeng_means_no_round_start_bonus():
    """错误输入/对照：未持有守夜灯时法力全程只有战始那一池，回始不加、回终不清。"""
    engine = _engine("lamp_invalid")
    p = engine.state.player
    engine.execute_action("battle_start", {})
    engine.execute_action("round_start", {})
    assert p.current_mana == EXP_MANA
    finish_round(engine)
    assert p.current_mana == EXP_MANA


# ========================================================================
# 3. 原初X 借用的杀伐必须在怪物回合发动
# ========================================================================

def test_borrowed_shaifa_fires_in_monster_phase():
    """正常路径：困境怪借杀伐2后，怪物回合按 X²=4 打向轮回者。"""
    engine = _engine("evo_happy")
    engine.execute_action("battle_start", {})
    # 2026-09-16：怪物[法力]即[攻击力]。杀伐2 需 2 点法力，故给足 2 点——
    # 引擎先结算道纹、后结算普攻，花光这 2 点后普攻为 0，总伤害仍只等于杀伐的 X²。
    monster = Entity(name="困境怪", entity_type="怪物", blood_limit=120, current_hp=30,
                     attack_count=1, attack_power=0, mana_limit=2, current_mana=2)
    _put_enemy(engine, monster)
    _advance_to_active_round(engine)
    ev = engine.execute_action("declare_evolution", {
        "monster": "困境怪", "daowen": "杀伐", "x": 2,
    })
    assert ev["success"], ev
    assert "原初借用" in monster.dao_wen["杀伐"].dao_wen.tags

    _advance_to_active_round(engine)
    hp_before = engine.state.player.current_hp
    phase = _resolve_prepared_monsters(engine, "杀伐")
    details = phase["result"]["details"]
    borrowed = [d for d in details if d.get("resolves_as") == "杀伐"]
    assert borrowed, f"应发动借用杀伐: {details}"
    assert borrowed[0]["daowen_activated"] == "杀伐"
    assert engine.state.player.current_hp == hp_before - 4, (
        f"杀伐2→X²=4（2026-09-13，原 5X=10），"
        f"HP应{hp_before}→{hp_before - 4}，实{engine.state.player.current_hp}")


def test_borrowed_shaifa_x1_deals_two():
    """边界：借用杀伐X=1，伤害 X²=1。"""
    engine = _engine("evo_bound")
    engine.execute_action("battle_start", {})
    # 2026-09-16：怪物[法力]即[攻击力]。杀伐2 需 2 点法力，故给足 2 点——
    # 引擎先结算道纹、后结算普攻，花光这 2 点后普攻为 0，总伤害仍只等于杀伐的 X²。
    # 杀伐X=1 只需 1 点法力；给 2 点的话放完还剩 1 点、普攻会多打 1 点，断言就会差 1。
    monster = Entity(name="困境怪", entity_type="怪物", blood_limit=120, current_hp=30,
                     attack_count=1, attack_power=0, mana_limit=1, current_mana=1)
    _put_enemy(engine, monster)
    _advance_to_active_round(engine)
    assert engine.execute_action("declare_evolution", {
        "monster": "困境怪", "daowen": "杀伐", "x": 1,
    })["success"]
    _advance_to_active_round(engine)
    hp_before = engine.state.player.current_hp
    _resolve_prepared_monsters(engine, "杀伐")
    assert engine.state.player.current_hp == hp_before - 1   # 杀伐1→X²=1（原 5X=5）


def test_unevolved_monster_does_not_cast_shaifa():
    """错误输入/对照：未进化的怪物即使玩家持有杀伐，也不会发动杀伐。"""
    engine = _engine("evo_invalid")
    engine.execute_action("battle_start", {})
    monster = Entity(name="普通怪", entity_type="怪物", blood_limit=80, current_hp=80,
                     attack_count=1, attack_power=0)
    _put_enemy(engine, monster)
    _advance_to_active_round(engine)
    hp_before = engine.state.player.current_hp
    phase = _resolve_prepared_monsters(engine)
    details = phase["result"]["details"]
    assert not any("杀伐" in str(d.get("daowen_activated", "")) for d in details)
    assert engine.state.player.current_hp == hp_before


# ========================================================================
# 4. 残骸：回 20 生命 + 异变 10
# ========================================================================

def test_canhai_heals_twenty_and_adds_mutation_ten():
    """正常路径：残血使用残骸，回复20并获得异变10，耐久归零。"""
    engine = _engine("canhai_happy")
    p = engine.state.player
    p.current_hp = 25
    engine.state.consumables.append(Consumable(
        name="残骸", effect="局内使用恢复20生命并获得异变10", current_uses=1, max_uses=1))
    r = engine.execute_action("consume_item", {"name": "残骸"})
    assert r["success"], r
    assert p.current_hp == 45
    assert r["result"]["heal"]["actual_heal"] == 20
    assert p.mutation_count == 10
    assert engine.state.consumables[0].is_depleted


def test_canhai_at_full_hp_tracks_overheal():
    """边界：满血残骸实回复0，过量20按原值计入累计恢复量（双倍机制已删）。"""
    engine = _engine("canhai_bound")
    p = engine.state.player
    assert p.current_hp == p.blood_limit == EXP_BLOOD
    engine.state.consumables.append(Consumable(
        name="残骸", effect="局内使用恢复20生命并获得异变10", current_uses=1, max_uses=1))
    r = engine.execute_action("consume_item", {"name": "残骸"})
    assert r["success"]
    assert p.current_hp == EXP_BLOOD
    assert r["result"]["heal"]["actual_heal"] == 0
    assert r["result"]["heal"]["overheal"] == 20
    assert p.total_healed == 20
    assert p.mutation_count == 10
    assert p.is_alive


def test_canhai_missing_item_fails():
    """错误输入：没有残骸时拒绝，不改生命、不加异变。"""
    engine = _engine("canhai_invalid")
    p = engine.state.player
    p.current_hp = 25
    r = engine.execute_action("consume_item", {"name": "残骸"})
    assert r["success"] is False
    assert p.current_hp == 25
    assert p.mutation_count == 0


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
