"""洗劫只在持有状态下夺碎片；必中X=给自身上buff（2026-09-28 最终口径：你选中的目标无法闪避，持续X）。"""
import os
import sys

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.combat import CombatEngine
from engine.daowen import DaoWenEngine
from engine.dice import DiceEngine
from engine.models import DaoWen, DaoWenInstance, Entity, GameState, StatusEffect
from tests.monster_phase_support import resolve_monster_phase


def _engine(region="罪孽都市"):
    engine = GameEngine(rng_seed=1)
    engine.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    finish_initial_daowen(engine)
    engine.state.relics = []
    # 2026-10-13：遗物池删2件后随机发放漂移，本文件断言与开局遗物无关，清空钉死环境。
    engine.state.current_region = region
    engine.state.phase = "in_combat"
    engine.state.player.current_mana = 5
    engine.state.player.attack_power = 5
    return engine


def _monster(engine, *, name="靶怪", hp=500, atk=8, hits=1, shards=20, daowen=None):
    # 2026-10-25：怪物攻击并入玩家同一套面板后，击数＝[攻击次数]＝当前速度，
    # 故 speed_limit 必须与 hits 同值（旧面板两者可以各写一个数）。
    m = Entity(name=name, entity_type="怪物", blood_limit=hp, current_hp=hp,
               speed_limit=hits, current_speed=hits,
               attack_count=hits, attack_power=atk, shards=shards, fake_shards=0)
    m.relics = []
    if daowen:
        for n, x in daowen.items():
            inst = DaoWenInstance(
                DaoWen(name=n, formula=f"{n}X", cost_type="异变" if n == "必中" else "消耗",
                       cost_formula="X", effect_formula=""),
                x_value=x)
            m.dao_wen[n] = inst
    engine.state.enemies.append(m)
    return m


# ==================== 洗劫（保持旧口径）====================

def test_shaifa_without_xijie_does_not_steal():
    engine = _engine()
    m = _monster(engine, shards=20)
    engine.combat.reset_monster_activation()
    engine.state.current_round = 1
    shards0 = engine.state.shards
    engine.combat.resolve_attack(engine.state.player, m, hit_index=0,
                                 is_must_hit=True, dodge=False)
    assert m.shards == 20
    assert engine.state.shards == shards0, "没洗劫时玩家碎片不应增加"


def test_shaifa_with_xijie_status_steals():
    engine = _engine()
    player = engine.state.player
    m = _monster(engine, shards=20)
    player.current_mana = 5
    player.add_status(StatusEffect(name="洗劫", value=2, remaining_rounds=2, source=player.name))
    shards0 = engine.state.shards
    engine.combat.resolve_attack(player, m, hit_index=0, is_must_hit=True, dodge=False)
    assert m.shards == 15
    assert engine.state.shards - shards0 == 5, f"洗劫应偷5碎片，实际+{engine.state.shards - shards0}"


def test_xijie_expired_no_longer_steals():
    engine = _engine()
    player = engine.state.player
    m = _monster(engine, shards=20)
    player.current_mana = 5
    player.add_status(StatusEffect(name="洗劫", value=0, remaining_rounds=0, source=player.name))
    shards0 = engine.state.shards
    engine.combat.resolve_attack(player, m, hit_index=0, is_must_hit=True, dodge=False)
    assert m.shards == 20
    assert engine.state.shards == shards0


# ==================== 必中X（self-buff，持续X回合）====================

def test_bizhong_self_buff_makes_all_your_targets_unable_to_dodge():
    """对自己发动必中X=2：施法者进入必中姿态持续2回合——期间你选的任何目标都无法闪避。"""
    engine = _engine()
    player = engine.state.player
    player.current_speed = 1
    m = _monster(engine, hits=1, atk=8)
    engine.combat.reset_monster_activation()
    engine.state.current_round = 0
    engine.combat.round_start()
    # 玩家对自己施必中2（self-buff，不需要target）
    calc = DaoWenEngine.resolve("必中", 2)
    # 2026-10-12 用户令增强：效果键改名 bizhong_target_debuff（正式口径是给目标挂
    # debuff）；target=None 的内部调用仍退回 self-buff 老行为。
    assert "bizhong_target_debuff" in calc and calc["cost_mutation"] == 2
    engine.combat.apply_daowen_effect("必中", calc, player, target=None)
    assert player.has_status("必中")
    st = next(s for s in player.status_effects if s.name == "必中")
    assert st.remaining_rounds == 2
    assert player.mutation_count == 2
    # 玩家打怪，dodge=True也无法闪避（is_must_hit=False 但 attacker 有必中buff）
    hp0 = m.current_hp
    engine.combat.resolve_attack(player, m, hit_index=0, is_must_hit=False, dodge=True)
    assert m.current_hp < hp0, "必中姿态下目标无法闪避"
    # 手动tick两轮验证状态递减
    st.tick()
    assert st.remaining_rounds == 1 and player.has_status("必中")
    st.tick()
    assert st.remaining_rounds == 0
    player.status_effects = [s for s in player.status_effects if s.name != "必中" or s.remaining_rounds > 0]
    assert not player.has_status("必中")


def test_bizhong_refreshes_duration():
    """重复施放必中X=延长（取max），不叠加层数。"""
    combat = CombatEngine(GameState(), DiceEngine(seed=1))
    player = Entity(name="贾凡", entity_type="轮回者", blood_limit=60, current_hp=60)
    calc1 = DaoWenEngine.resolve("必中", 1)
    combat.apply_daowen_effect("必中", calc1, player, target=None)
    calc2 = DaoWenEngine.resolve("必中", 3)
    combat.apply_daowen_effect("必中", calc2, player, target=None)
    s = next(s for s in player.status_effects if s.name == "必中")
    assert s.remaining_rounds == 3
    assert player.mutation_count == 1 + 3


def test_bizhong_x_costs_x_mutation():
    calc = DaoWenEngine.resolve("必中", 5)
    assert calc["cost_mutation"] == 5


def test_no_bizhong_auto_dodge_still_works():
    """没必中buff时闪避照常成功。"""
    engine = _engine()
    player = engine.state.player
    player.current_speed = 4
    m = _monster(engine, hits=1, atk=8, daowen={"疯狂": 1})   # 2026-10-03：【狂暴】删除 →【疯狂】
    engine.combat.reset_monster_activation()
    engine.state.current_round = 0
    engine.combat.round_start()
    results = resolve_monster_phase(engine.combat, {m.name: "疯狂"}, dodge=True)
    hits = [d for d in results if d.get("attacker") == m.name]
    assert hits and hits[0].get("dodge_success") is True


def test_bizhong_does_not_affect_other_attackers():
    """只有必中持有者自己的攻击必中——队友打同一目标不享受必中。"""
    combat = CombatEngine(GameState(), DiceEngine(seed=1))
    A = Entity(name="A", entity_type="轮回者", blood_limit=60, current_hp=60)
    B = Entity(name="B", entity_type="轮回者", blood_limit=60, current_hp=60)
    T = Entity(name="T", entity_type="怪物", blood_limit=60, current_hp=60)
    calc = DaoWenEngine.resolve("必中", 2)
    combat.apply_daowen_effect("必中", calc, A, target=None)
    assert combat.caster_has_bizhong(A) if hasattr(combat, 'caster_has_bizhong') else A.has_status("必中")
    assert not B.has_status("必中"), "必中是self-buff，不影响队友"
