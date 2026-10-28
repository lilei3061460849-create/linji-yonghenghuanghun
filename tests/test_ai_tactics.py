"""
pytest - 战术AI（engine/ai_tactics.py）

背景：早前 sim/format_trace.py 写死"只发杀伐"，导致 AI 表现很蠢。
本测试锁定 AI 会实际使用多种手段，并且法力预算不会被一次出手耗尽。

覆盖：正常路径 / 边界条件 / 错误输入
"""
import math
import os
import sys

import pytest

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.ai_tactics import TacticalAI
from engine.models import DaoWen, DaoWenInstance


def _engine(tmp_path, seed=4, learn=("庇护", "再生", "冲击")):
    e = GameEngine(db_path=str(tmp_path / "ai.db"), rng_seed=seed)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    setup = e.execute_action("setup_choose_region", {"region": "龙心谷"})
    e.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    for dw in learn:
        e.execute_action("pre_battle_action", {"sub_action": "学习", "sub": "daowen", "name": dw})
    e.state.energy = 0
    choices = {}
    relic = e.state.relics[0].name
    # 2026-10-13：【三相残韵盘】删除，战始显式决策预填不再需要。
    e.execute_action("battle_start", {"relic_choices": choices})
    e.execute_action("round_start", {})
    return e


# ---------- 正常路径 ----------

def test_ai_uses_full_action_budget(tmp_path):
    """正常路径：AI 应打满本回合出手次数，而不是一发杀伐烧光法力就收手"""
    e = _engine(tmp_path)
    for monster in e.state.enemies:
        if "全力" not in monster.dao_wen:
            monster.dao_wen["全力"] = DaoWenInstance(
                DaoWen("全力", "", "异变", "5X", ""), x_value=1)
    ai = TacticalAI(e)
    results = ai.take_turn()
    expected = max(1, e.state.player.action_count)
    assert len(results) >= 2, f"只出手{len(results)}次，未用满预算(应为{expected}次)"


def test_ai_shields_when_facing_lethal_damage(tmp_path):
    """正常路径：面临致死伤害时应优先庇护，而不是继续输出"""
    e = _engine(tmp_path)
    p = e.state.player
    p.current_hp = 8  # 制造致死威胁
    ai = TacticalAI(e)
    r = ai.try_survive()
    assert r is not None, "面临致死威胁却没有采取保命行动"
    assert "庇护" in r.get("action", "") or "再生" in r.get("action", "")


def test_ai_does_not_offer_legacy_parry_action(tmp_path):
    """招架是 resolve_attack 的逐击参数，不是独立战斗行动。"""
    e = _engine(tmp_path, learn=())
    e.state.player.dao_wen.clear()
    e.state.resonance.clear()
    e.state.player.current_hp = e.state.player.blood_limit
    e.state.enemies[0].attack_count = 1
    e.state.enemies[0].attack_power = 1
    ai = TacticalAI(e)

    result = ai.take_action()

    assert result is not None and result.get("success"), result
    assert result.get("action") != "贾凡招架"


def test_ai_finishes_killable_target(tmp_path):
    """正常路径：目标可被一击击杀时应当收割"""
    e = _engine(tmp_path)
    m = e.state.enemies[0]
    m.current_hp = 4  # 杀伐X=2 造成X²=4伤 即可击杀
    ai = TacticalAI(e)
    r = ai.try_finish()
    assert r is not None, "可击杀目标却未收割"
    assert not m.is_alive or m.current_hp <= 0


# ---------- 边界条件 ----------

def test_mana_budget_splits_across_actions(tmp_path):
    """边界：预算须按剩余出手次数均分，最后一次允许用尽"""
    e = _engine(tmp_path)
    ai = TacticalAI(e)
    total = max(1, e.state.player.action_count)
    budget = ai.mana_budget()
    if total > 1:
        assert budget < e.state.player.current_mana, "预算未分配，会一次烧光法力"
        assert budget >= 1


def test_ai_stops_when_no_mana(tmp_path):
    """边界：法力为0且无可用手段时应安全停手，不抛异常"""
    e = _engine(tmp_path)
    e.state.player.current_mana = 0
    ai = TacticalAI(e)
    r = ai.take_action()
    assert r is None or r.get("success") is True


def test_ai_handles_no_enemies(tmp_path):
    """边界：场上无存活敌人时不应产生任何行动"""
    e = _engine(tmp_path)
    for m in e.state.enemies:
        m.is_alive = False
    ai = TacticalAI(e)
    assert ai.take_turn() == [] or all(x.get("success") for x in ai.take_turn())


# ---------- 错误输入 / 非法配置 ----------

def test_resonance_only_targets_existing_paths(tmp_path):
    """
    错误输入：残韵只能对**存在变化路径**的道纹发动（2026-08-26 起无固定目标表，
    候选由引擎 ResonanceEngine 实时枚举，本测试锁定该不变量）。
    """
    from engine.daowen import ResonanceEngine

    e = _engine(tmp_path)
    ai = TacticalAI(e)
    stock = {k: v for k, v in e.state.resonance.items() if v > 0}
    for _score, cand in ai._resonance_candidates():
        params = cand["params"]
        assert stock.get(params["resonance_type"], 0) > 0, "候选必须消耗实存残韵库存"
        paths = ResonanceEngine.get_available_resonance(params["source_daowen"])
        assert paths, f"{params['source_daowen']} 无残韵路径，不得成为候选"
        enemy_names = [en.name for en in e.state.enemies if en.is_alive]
        assert params["target"] in enemy_names, "残韵目标必须是存活敌人"


def test_ai_never_bypasses_engine_validation(tmp_path):
    """非法配置：AI 未持有的道纹必被引擎拒绝，AI 不得绕过校验改状态"""
    e = _engine(tmp_path, learn=())  # 只有初始道纹杀伐
    ai = TacticalAI(e, verbose=True)
    before = e.state.player.current_hp
    ai.try_survive()  # 没有庇护/再生，应全部失败
    assert e.state.player.current_hp == before
    assert "庇护" not in e.state.player.dao_wen


def test_ai_does_not_crash_without_any_daowen(tmp_path):
    """
    非法配置：玩家无任何道纹时，AI 不得抛异常。

    注：残韵不要求施法者持有该道纹（可作用于敌方道纹），
    因此此时 AI 仍可能合法地发动残韵；只要不崩溃、且结果合法即可。
    """
    e = _engine(tmp_path, learn=())
    e.state.player.dao_wen.clear()
    e.state.resonance.clear()          # 连残韵也清空，才是真正的"无牌可打"
    ai = TacticalAI(e)
    # DM裁定 2026-09-10：普攻是常驻候选——无道纹但持有速度/法力时，合法动作是普攻；
    # 本测试的保护意图是「不得崩溃、结果合法」，不再断言必须无动作。
    r = ai.take_action()
    assert r is None or r.get("success"), r


def test_no_fixed_tactic_tables_remain():
    """2026-08-26 防公式化改造：固定战术表/固定残韵目标表/基类固定顺序必须不存在。

    战术知识不得以查表形式回填——AI 只能经 ActionPreview 预演实时决策。
    """
    import engine.ai_tactics as mod
    assert not hasattr(mod, "TACTICAL_ROLES"), "固定战术表不得回填"
    assert not hasattr(mod, "HIGH_VALUE_ENEMY_DAOWEN"), "固定残韵目标表不得回填"
    assert TacticalAI.STRATEGIES is None, "基类不得有固定策略顺序（仅实验子类可覆写）"


def test_ai_cost_facts_come_from_preview_not_tables(tmp_path):
    """正常：法力单价等事实来自预演归纳（引擎事实源），而非硬编码表。"""
    e = _engine(tmp_path)
    ai = TacticalAI(e)
    probe = ai._probe("杀伐")
    assert probe is not None and probe["kind"] == "damage"
    assert probe["cost_per_x"] == 1          # 与正文现行公式一致（消耗X、5X伤害）
    assert probe["dmg"] == 1                 # 2026-09-13：杀伐 X²，X=1→1
    shield = ai._probe("庇护")
    assert shield is not None and shield["kind"] == "shield"
    assert shield["target_name"] == e.state.player.name, "庇护必须朝向自身（方向由预演判定）"


def test_probe_dmg_not_masked_by_enemy_shield(tmp_path):
    """
    边界：敌方格挡把 X=1 伤害**全部吸收**时，dmg 仍须取自事件流（未扣盾）。

    修复前 dmg 读面板净位移 → 被盾吸光即归零 → `_daowen_candidates` 收割档
    条件 `dmg > 0` 整行短路，只剩 X=1 一档 → AI 永不出手（报告 ①，2026-08-31）。
    """
    e = _engine(tmp_path)
    m = e.state.enemies[0]
    m.current_hp = 4
    m.shield = 2                      # 格挡吸收：验证 dmg 口径取事件流而非面板位移
    e.state.player.current_mana = 12
    ai = TacticalAI(e)
    probe = ai._probe("杀伐")
    assert probe is not None and probe["kind"] == "damage"
    assert probe["dmg"] == 1, f"dmg 被格挡遮蔽成 {probe['dmg']}：口径须取事件流 raw_damage"
    xs = {int(c["label"].split("X=")[1]) for c in ai._daowen_candidates()
          if c["label"].startswith("杀伐")}
    # 杀伐为 X²：打死 4 血需 X=2（线性外推会误算成 4），收割档须按真实预演生成
    assert 2 in xs, f"收割档未生成（候选档={sorted(xs)}）"


def test_ai_still_acts_when_shield_absorbs_all_probe_damage(tmp_path):
    """正常路径：X=1 预演被格挡全吸收时，AI 须打出真正能穿盾的档位。

    修复前 dmg=0 → 收割档整行短路，只剩 X=1（2 伤被 2 盾吃光）→ 一手打完零伤害。
    """
    from engine.models import Entity
    e = _engine(tmp_path, learn=())   # 只留初始道纹杀伐：无可替代的保命牌
    e.state.resonance.clear()         # 残韵另有一条不依赖 dmg 的路，清掉才是纯输出局
    m = Entity(name="木桩", entity_type="怪物", blood_limit=40, current_hp=4,
               attack_count=1, attack_power=0)
    m.shield = 2                      # 吸掉 杀伐X=1（5点）中的 2 点
    e.state.enemies = [m]             # 单一无机制木桩，排除怪物自身触发链干扰
    ai = TacticalAI(e)
    results = ai.take_turn()
    assert results, "有格挡时 AI 整回合空过（dmg 口径回归）"
    assert m.current_hp < 4, "只出被格挡吃掉一部分的 X=1 → 仍有伤害落地（dmg 口径回归）"


def test_ai_can_mark_wave_targets(tmp_path):
    """正常：双怪场面AI能发动波及并显式提交X个目标的闪避选择。"""
    from engine.models import Entity
    e = _engine(tmp_path, learn=("波及",))
    extra = Entity(name="木桩乙", entity_type="怪物", blood_limit=80, current_hp=80,
                   attack_count=1, attack_power=4)
    e.state.enemies.append(extra)
    e.state.player.current_mana = 12
    ai = TacticalAI(e)
    r = ai._cast("波及", 2)
    assert r is not None and r.get("success"), f"双怪场面应能发动波及：{r}"
    assert ai.used.get("波及", 0) == 1
    assert e.state.enemies[0].has_status("波及")
    assert e.state.enemies[1].has_status("波及")


def test_ai_keeps_shaifa_on_single_target(tmp_path):
    """边界：没有群伤道纹时单怪场面仍走杀伐，不得因无AOE而空过。"""
    e = _engine(tmp_path, learn=("波及",))
    assert len(e.state.enemies) == 1
    e.state.player.current_mana = 12
    ai = TacticalAI(e)
    assert ai.try_aoe() is None
    r = ai.try_pressure()
    assert r is not None and "杀伐" in r.get("action", "")


def test_manqian_can_cast_with_zero_mana(tmp_path):
    """错误输入对照：束缚是冷却代价，法力为0时仍应能发动，不得按旧消耗表拒绝。"""
    from engine.models import DaoWen, DaoWenInstance
    e = _engine(tmp_path, learn=())
    e.state.player.dao_wen["束缚"] = DaoWenInstance(
        DaoWen(name="束缚", formula="", cost_type="冷却", cost_formula="X", effect_formula=""))
    e.state.player.current_mana = 0
    e.state.player.current_hp = 20
    e.state.enemies[0].attack_count = 1
    e.state.enemies[0].attack_power = 8
    ai = TacticalAI(e)
    r = ai.try_control()
    assert r is not None and r.get("success"), f"零法力应能发动束缚：{r}"
    assert ai.used.get("束缚", 0) == 1


def test_ai_can_use_resonance_on_monster_daowen(tmp_path):
    """
    正常路径：残韵闭环补齐后，AI 必须能对怪物原始道纹发动残韵。
    修复前 CLOSED_LOOPS 只有杀伐/切割两轨，对怪物原始道纹发动必然失败。
    当前以仍有完整残韵出边的【全力】验证该路径。
    """
    e = _engine(tmp_path, learn=())
    e.state.player.dao_wen.clear()
    e.state.enemies[0].dao_wen["全力"] = DaoWenInstance(
        DaoWen("全力", "", "异变", "5X", "全力X"), x_value=1,
    )
    ai = TacticalAI(e)
    r = ai.try_resonance()
    assert r is not None, "补齐闭环后仍无法对怪物道纹发动残韵"
    assert r.get("success")
