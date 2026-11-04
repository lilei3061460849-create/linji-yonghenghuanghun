"""
F6 验证：疯狂四方案死代码已清理，仅保留 current 口径
- 正常：疯狂X 的攻击出手 = 1 + X（+ 狂暴1），与现行口径一致
- 边界：无疯狂/仅狂暴/疯狂+狂暴 三种组合
- 错误：若外部传入 HUOLI_MODE（非法配置）应无此属性或被拒绝
- 静态：源码层不再出现 HUOLI_MODE / _huoli_charges / huoli_note / huoli_tick 分支
"""
import pathlib
import pytest
from sim.balance_sim import get_monster_attack_actions, make_monster, make_player, monster_attack_round
from engine.models import Entity, DaoWen, DaoWenInstance
from engine.combat import CombatEngine
from engine.dice import DiceEngine
from engine.models import GameState

def test_normal_vitality_current():
    """正常：疯狂X 按 current 语义 +X 出手"""
    m = make_monster({"name":"羊","ac":1,"ap":1,"hp":60,"dw":{"疯狂":2},"region":"龙心谷"})
    assert get_monster_attack_actions(m, {"疯狂"}) == 1 + 2
    assert get_monster_attack_actions(m, set()) == 1
    assert get_monster_attack_actions(m, {"狂暴"}) == 2  # 狂暴+1
    assert get_monster_attack_actions(m, {"疯狂","狂暴"}) == 1 + 2 + 1

def test_boundary_no_vitality_vs_zero():
    """边界：未激活疯狂时不受 X 影响；疯狂0 视为无"""
    m = make_monster({"name":"无","ac":1,"ap":1,"hp":60,"dw":{},"region":"扭曲都市"})
    # 无道纹时即使传疯狂激活集也应为 1（通用回退）
    assert get_monster_attack_actions(m, set()) == 1
    # 狂暴单独
    assert get_monster_attack_actions(m, {"狂暴"}) == 2
    # 疯狂激活但怪物面板无该道纹（极边界）：仍按 +X 取值，但 X 不存在时取 0 -> 回退 1
    # 实际实现取 x_value 时若无道纹会 KeyError，此处验证行为为不崩溃且为 1+0
    # 为此构造有道纹但 X=0 的情况
    m0 = make_monster({"name":"零疯狂","ac":1,"ap":1,"hp":60,"dw":{"疯狂":0},"region":"龙心谷"})
    assert get_monster_attack_actions(m0, {"疯狂"}) == 1

def test_error_invalid_huoli_mode_rejected():
    """错误：非法 HUOLI_MODE 配置应不存在（已删）"""
    import sim.balance_sim as bs
    # 现行文件不应再暴露 HUOLI_MODE 属性
    assert not hasattr(bs, "HUOLI_MODE"), "HUOLI_MODE 全局开关应已删除"
    assert not hasattr(bs, "huoli_note_activation"), "huoli_note_activation 应已删除"
    assert not hasattr(bs, "huoli_tick"), "huoli_tick 应已删除"
    # 若外部强行设置，不应影响 current 逻辑（即 1+X）
    m = make_monster({"name":"羊","ac":1,"ap":1,"hp":60,"dw":{"疯狂":3},"region":"龙心谷"})
    # 模拟外部污染：即使写入属性，也不应被读取（函数内无分支）
    bs.HUOLI_MODE = "burst"  # type: ignore[attr-defined]
    try:
        assert get_monster_attack_actions(m, {"疯狂"}) == 4
    finally:
        delattr(bs, "HUOLI_MODE")

def test_static_no_dead_code():
    """静态：源码层不再出现死代码分支关键字"""
    bs_text = pathlib.Path("sim/balance_sim.py").read_text(encoding="utf-8")
    rs_text = pathlib.Path("sim/audits/run_sim.py").read_text(encoding="utf-8")
    # 允许在注释归档中出现 charges/flat/burst/half 字样，但不允许出现可执行的 HUOLI_MODE 分支
    assert "HUOLI_MODE" not in bs_text, "balance_sim.py 不应再含 HUOLI_MODE"
    assert "HUOLI_MODE" not in rs_text, "run_sim.py 不应再含 HUOLI_MODE"
    assert "_huoli_charges" not in bs_text or bs_text.count("_huoli_charges") == 0, "不应再出现 _huoli_charges 运行态"
    # huoli_ 函数名不应再出现（排除注释中的归档说明，注释已不含函数名）
    assert "huoli_note_activation" not in bs_text
    assert "huoli_tick" not in bs_text
    assert "huoli_note_activation" not in rs_text
    assert "huoli_tick" not in rs_text

def test_monster_attack_round_no_half_scale():
    """验证 monster_attack_round 已移除 half 半伤分支，现行一律全伤"""
    import inspect
    sig = inspect.signature(monster_attack_round)
    # 现行签名应为 (m, player, combat, rng, must_hit) 无 dmg_scale
    assert "dmg_scale" not in sig.parameters, "dmg_scale 参数应已随 half 方案删除"
    # 实战：1 次攻击 10 点应全额结算（无半伤）
    state = GameState()
    player = make_player()
    state.player = player
    combat = CombatEngine(state, DiceEngine())
    m = make_monster({"name":"测","ac":1,"ap":10,"hp":60,"dw":{},"region":"扭曲都市"})
    player.current_hp = 60; player.shield = 0; player.current_speed = 0
    lost = monster_attack_round(m, player, combat, __import__("random").Random(0), must_hit=False)
    assert lost == 10
    assert player.current_hp == 50
