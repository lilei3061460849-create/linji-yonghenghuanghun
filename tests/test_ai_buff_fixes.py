"""
pytest - AI 战术表死码修复（2026-08-18）

修复背景：
1. try_buff 旧版只硬编码固执，导致全部 role=buff 道纹（切割/贯穿/增殖/活血/
   爆裂/超频/龙鳞/滑翔/分裂/招魂）授予后 0 发动（死码）。
2. try_debuff 的 X≥2 门槛对代价型道纹（cost≤0，_x_for 恒为1，如畸变/逆鳞）
   永不满足；衰败被错标为 nuke 且按默认 dmg_per_x=2 错价，从未被选中。
   （2026-10-11 用户令：【衰败】已删除，对应的 `test_shuaibai_*` 用例随之移除；
   「分类必须来自预演」这条不变量仍由本文件的代价型削弱与【瓦解】两个用例守着。）
3. （2026-10-03：【尸爆】删除）同类的「分类必须来自预演而非人工标签」回归改用【瓦解】。

覆盖：正常路径 / 边界条件 / 错误输入
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine  # noqa: E402
from engine.ai_tactics import TacticalAI  # noqa: E402
from engine.models import DaoWen, DaoWenInstance, Entity  # noqa: E402
from tests.setup_support import finish_initial_daowen  # noqa: E402


def _engine(tmp_path, seed=4):
    e = GameEngine(db_path=str(tmp_path / "buff_fix.db"), rng_seed=seed)
    e.execute_action("setup_attributes",
                     {"name": "贾凡", "blood_points": 7, "speed_points": 8, "mana_points": 10})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    e.execute_action("setup_choose_region", {"region": "龙心谷"})
    e.state.energy = 0
    choices = {}
    # 2026-10-13：【三相残韵盘】删除，战始显式决策预填不再需要。
    e.execute_action("battle_start", {"relic_choices": choices})
    e.execute_action("round_start", {})
    return e


def _give(entity, name, cost_type="消耗"):
    entity.dao_wen[name] = DaoWenInstance(DaoWen(
        name=name, formula="", cost_type=cost_type, cost_formula="X", effect_formula=""))


# ---------- 正常路径 ----------

def test_retired_guanchuan_is_not_cast_even_when_enemy_shielded(tmp_path):
    """贯穿已转破障晶；不能让AI为旧道纹制造发动路径。"""
    e=_engine(tmp_path);_give(e.state.player,"贯穿");e.state.enemies[0].shield=10
    ai=TacticalAI(e)
    assert ai._probe("贯穿") is None
    assert ai.try_buff() is None


def test_relicized_longlin_is_not_an_active_daowen_buff(tmp_path):
    """龙鳞是固定遗物，不再由AI主动发动同名道纹。"""
    e=_engine(tmp_path);_give(e.state.player,"龙鳞")
    assert TacticalAI(e)._probe("龙鳞") is None


def test_shufu_cooldown_debuff_casts_at_x1(tmp_path):
    """束缚（冷却型debuff）：X≥2门槛应对代价型豁免，X=1即可发动"""
    e = _engine(tmp_path)
    _give(e.state.player, "束缚", cost_type="冷却")
    ai = TacticalAI(e)
    r = ai.try_debuff()
    assert r is not None, "冷却型削弱应以X=1发动，不得被X≥2门槛卡死"


# 2026-10-11 用户令：`test_shuaibai_reclassified_as_debuff_and_casts` 已删除——它整条建立在被删机制上，不是改断言能救的。



def test_retired_wajie_has_no_preview_or_ai_cast_path(tmp_path):
    e=_engine(tmp_path);_give(e.state.player,"瓦解")
    assert TacticalAI(e)._probe("瓦解") is None


# ---------- 边界条件 ----------

def test_quansu_recast_marker_is_battle_local(tmp_path):
    """全速标记按场次隔离：上一场用过不封锁下一场"""
    e = _engine(tmp_path)
    _give(e.state.player, "全速", cost_type="冷却")
    ai = TacticalAI(e)
    ai.used[f"buff:全速:{e.state.current_battle - 1}"] = 1  # 模拟上一场已用
    r = ai.try_buff()
    assert r is not None, "全速的每场一次标记不得跨场封锁"


def test_buff_not_cast_without_enemies(tmp_path):
    """无敌人时不挂增益（不浪费出手）"""
    e = _engine(tmp_path)
    _give(e.state.player, "龙鳞")
    for m in e.state.enemies:
        m.is_alive = False
    ai = TacticalAI(e)
    assert ai.try_buff() is None


def test_buff_once_per_battle(tmp_path):
    """每张增益每场至多一次"""
    e = _engine(tmp_path)
    _give(e.state.player, "全速")
    e.state.enemies[0].shield = 10
    ai = TacticalAI(e)
    assert ai.try_buff() is not None
    e.state.enemies[0].shield = 10
    again = ai.try_buff()
    assert again is None or "全速" not in str(again), "同场不得重复挂全速"


# ---------- 错误输入 ----------

def test_unknown_daowen_probe_returns_none(tmp_path):
    """未持有/不存在的道纹预演归纳返回None，不抛异常"""
    e = _engine(tmp_path)
    ai = TacticalAI(e)
    assert ai._probe("不存在的道纹") is None
