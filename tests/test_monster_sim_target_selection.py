"""修复验证（2026-08-21）：sim 侧怪物道纹目标选择（最优策略解析器）。

背景：sim 解析器一律取 target_options[0]（=玩家），导致 再生/增殖 等增益
道纹被怪物打给玩家（实测：眼树再生奶玩家、血肉巨囊增殖给玩家加血限），
污染模拟数据。修复：按道纹目标类型选择——
SELF→自身；HOSTILE→玩家；波及等 per_target 走 dodge_targets；
无合法 SELF 目标时回退到合法目标而非强行选择玩家。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import Entity, DaoWen, DaoWenInstance
from sim.monster_targets import (
    MONSTER_HOSTILE_DAOWEN,
    MONSTER_SELF_DAOWEN,
    pick_monster_daowen_target,
)
from tests.setup_support import finish_initial_daowen


def _engine(tmp_path):
    e = GameEngine(db_path=str(tmp_path / "t.db"), rng_seed=7,
                   sealed_candidate_path=str(tmp_path / "s.json"))
    e.execute_action("setup_attributes", {
        "name": "模拟者", "blood_points": 11, "speed_points": 8, "mana_points": 6})
    finish_initial_daowen(e)
    e.execute_action("setup_choose_resonance", {"resonance_type": "反转"})
    e.execute_action("setup_choose_region", {"region": "扭曲都市"})
    return e


def _monster_with(name, daowen: dict):
    # 2026-09-16：怪物[法限]即法力池；1 点付不起杀伐2/勾魂2，抬到 20。
    m = Entity(name=name, entity_type="怪物", blood_limit=200, current_hp=100,
               attack_count=1, attack_power=20)
    for dw, x in daowen.items():
        m.dao_wen[dw] = DaoWenInstance(
            DaoWen(name=dw, formula="", cost_type="消耗", cost_formula="X",
                   effect_formula=""), x_value=x)
    return m


def _prepared_option(e, daowen_name):
    """让怪物进入可发动 daowen_name 的状态并返回 prepare 中的对应 option。"""
    e.state.phase = "in_combat"
    e.state.combat_subphase = "player_actions"
    e.state.current_round = 2
    e.combat.reset_monster_activation()
    prepared = e.combat.prepare_monster_phase()
    actor = next(a for a in prepared["actors"] if a["actor_ref"] == "enemy:0")
    return next(o for o in actor["daowen_options"] if o["name"] == daowen_name), actor


def test_zaisheng_defaults_to_self(tmp_path):
    """再生（SELF）默认自奶：目标=怪物自身。"""
    e = _engine(tmp_path)
    m = _monster_with("奶怪", {"再生": 2})
    e.state.enemies.append(m)
    opt, actor = _prepared_option(e, "再生")
    assert opt["requires_target"]
    assert pick_monster_daowen_target(e, actor["actor_ref"], opt) == "enemy:0"


def test_zengzhi_defaults_to_self(tmp_path):
    """增殖（SELF）默认给自己（加血限），而不是给玩家。"""
    e = _engine(tmp_path)
    m = _monster_with("巨囊", {"增殖": 3})
    e.state.enemies.append(m)
    opt, actor = _prepared_option(e, "增殖")
    assert pick_monster_daowen_target(e, actor["actor_ref"], opt) == "enemy:0"


def test_hostile_defaults_to_player(tmp_path):
    """HOSTILE（杀伐/勾魂/减速）默认攻击/削弱玩家。"""
    e = _engine(tmp_path)
    # 2026-10-11 用户令：【衰败】已删除，HOSTILE 样本改用仍存的 减速
    m = _monster_with("攻击怪", {"杀伐": 2, "勾魂": 2, "减速": 2})
    e.state.enemies.append(m)
    for name in ("杀伐", "勾魂", "减速"):
        opt, actor = _prepared_option(e, name)
        assert pick_monster_daowen_target(e, actor["actor_ref"], opt) == "player:0", name


def test_self_daowen_no_legal_self_falls_back_not_forced_to_player(tmp_path):
    """SELF 道纹自身不在合法目标中时：回退到合法目标，而不是强行选择玩家。"""
    # 构造一个自身不在 target_options 的 SELF 场景：直接构造 option 验证回退逻辑
    option = {
        "name": "再生",
        "resolves_as": "再生",
        "requires_target": True,
        "target_options": [{"ref": "enemy:1", "name": "另一只怪"},
                           {"ref": "player:0", "name": "玩家"}],
    }
    picked = pick_monster_daowen_target(None, "enemy:0", option)
    assert picked == "enemy:1", "SELF 自身不可选时应回退到第一个合法目标，而非玩家"


def test_wave_multi_target_uses_dodge_targets_not_helper(tmp_path):
    """波及（多目标）走 dodge_targets 且排除自身，不经过单目标 helper。

    面板固定 X=2，场上有 2 个合法目标（2026-10-09 起 X 下限 repealed，
    X=1 也合法——prepare 不再暴露 min_x 字段，下限恒为 1）。
    """
    e = _engine(tmp_path)
    m = _monster_with("波怪", {"波及": 2})
    e.state.enemies.append(m)
    e.state.friends.append(Entity("陪标友军", "朋友", blood_limit=40, current_hp=40,
                                  speed_limit=2, current_speed=2, attack_count=1, attack_power=1))
    opt, actor = _prepared_option(e, "波及")
    assert opt["dodge_submission"] == "per_target"
    refs = [t["ref"] for t in opt["dodge_target_options"]]
    assert "enemy:0" not in refs, "波及 dodge_targets 应排除施法者自身"
    assert "player:0" in refs and "friend:0" in refs, "两个合法目标都应在选项里"
    assert "min_x" not in opt, "2026-10-09：X_MIN 拔除后 prepare 不再暴露 min_x 字段"


def test_classification_sets_cover_all_dungeon_daowens():
    """SELF/HOSTILE 分类覆盖全部可能出现在怪物面板上的需目标道纹（防漏分类）。"""
    from engine.daowen import DaoWenEngine
    import inspect
    DaoWenEngine.register_all()
    needs_target = {
        name for name, fn in DaoWenEngine._registry.items()
        if "target" in inspect.signature(fn).parameters
    }
    unclassified = needs_target - MONSTER_SELF_DAOWEN - MONSTER_HOSTILE_DAOWEN
    # 通用/区域道纹里允许未分类（默认走玩家优先），但至少不因分类缺失而崩溃；
    # 以下为当前怪物池里实际出现的需目标道纹，必须全部已分类。
    # 2026-10-11 起本名单只列**现存**道纹：已删除的（衰败/借力/无神/嫁祸/活血/
    # 尸爆/爆裂/眩晕/坠落/滑翔/滋养/兴奋/自愈/镇尸/逼债）不再出现
    # （2026-10-12【失忆】已回退为【点金】；2026-10-13【逼债】删除，
    # 顶替者【典当】是自身发动无 target 参，不进本需目标名单）。
    critical = {"再生", "增殖", "庇护", "全力", "杀伐", "勾魂", "冥气",
                "减速", "必中", "飞行", "狂暴", "弱化",
                "坏死", "定型", "僵化", "变形", "退化", "加害", "龙鳞",
                "逆鳞", "裂变", "背负", "伤痕", "点金",
                "豪夺", "清算", "赎金", "假钞", "消灾", "封印", "波及",
                # 2026-10-14：【勒索】（点金反转分叉，打手/看门犬承载）进池。
                "勒索",
                "寄生", "蒙蔽", "愤怒", "全速", "无力",
                "洞察", "急速", "加速", "自食", "招魂",
                "缄默", "瓦解", "分裂", "贯穿", "固执", "连击"}
    missing = critical - MONSTER_SELF_DAOWEN - MONSTER_HOSTILE_DAOWEN
    assert not missing, f"以下需目标道纹未分类：{sorted(missing)}"
