"""龙族起源事件「埋骨之地」（龙心谷专属）契约测试。

事件（副本/龙心谷.md）：
- 选项1 继承龙骨：记入12点代价累积（原「12龙性」已按 2026-10-23 用户令废除）
- 选项2 拾取龙心残骸：获得【××龙心】（耐久6，可抵消6点同类型代价）
- 选项3 掩埋龙骨：无事发生

覆盖：正常路径（选项1/2）/ 边界（重复获得龙心合并、代价累积）/ 错误输入（非法选项、草案事件不进运行时）。
"""
import os
import sys

from tests.setup_support import finish_initial_daowen
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.events import parse_events


def _engine(suffix: str, region: str = "龙心谷") -> GameEngine:
    engine = GameEngine(db_path=f"data/test_dragon_origin_{suffix}.db", rng_seed=1)
    engine.execute_action("setup_attributes", {
        "name": "贾凡", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": region})
    engine.execute_action("choose_discovered_relic", {"relic_name": setup["result"]["relic_choices"][0]})
    engine.state.phase = "pre_battle"
    return engine


def _set_event(engine, name="埋骨之地"):
    engine.event_pool.current = name


def _resolve(engine, option_id, **extra):
    return engine.execute_action("resolve_event", {
        "event": engine.event_pool.current, "option_id": option_id, **extra,
    })


# ---------- 正常路径 ----------

def test_normal_option1_grants_dragon_nature():
    """正常路径：选项1「继承龙骨」记入12点代价累积（龙性货币已废除）。"""
    engine = _engine("nature")
    _set_event(engine)
    assert engine.state.dragon_cost_points == 0
    r = _resolve(engine, 1)
    assert r["success"], r
    assert "代价累积+12" in r["result"]["applied"][0]
    assert engine.state.dragon_cost_points == 12, f"应记入12点，实{engine.state.dragon_cost_points}"


def test_normal_option2_grants_dragon_heart_consumable():
    """正常路径：选项2「拾取龙心残骸」获得衰老龙心(6/6)。"""
    engine = _engine("heart")
    _set_event(engine)
    r = _resolve(engine, 2)
    assert r["success"], r
    heart = next((c for c in engine.state.consumables if c.kind == "dragon_heart"), None)
    assert heart is not None, "应获得龙心消耗品"
    assert heart.name == "衰老龙心" and heart.current_uses == 6 and heart.max_uses == 6
    assert heart.dragon_heart_type == "衰老"


def test_normal_option3_is_reject():
    """正常路径：选项3「掩埋龙骨」无事发生，不改变资源。"""
    engine = _engine("reject")
    _set_event(engine)
    shards_before = engine.state.shards
    r = _resolve(engine, 3)
    assert r["success"], r
    assert "无事发生" in r["result"]["applied"]
    assert engine.state.dragon_cost_points == 0
    assert engine.state.shards == shards_before


# ---------- 边界条件 ----------

def test_boundary_repeated_heart_merges_durability():
    """边界：再次拾取龙心时按消耗品合并规则累加耐久（6+6=12/12）。"""
    engine = _engine("merge")
    _set_event(engine)
    _resolve(engine, 2)
    _set_event(engine)
    r = _resolve(engine, 2)
    assert r["success"], r
    heart = next(c for c in engine.state.consumables if c.kind == "dragon_heart")
    assert heart.current_uses == 12 and heart.max_uses == 12, \
        f"同名龙心应合并为12/12，实{heart.current_uses}/{heart.max_uses}"


def test_boundary_nature_accumulates_without_true_dragon_heart():
    """边界：未持有真龙之心时，代价累积照常记入，但不能兑换。"""
    engine = _engine("accum")
    _set_event(engine)
    _resolve(engine, 1)
    assert "真龙之心" not in engine.state.artifacts_owned
    assert engine.state.dragon_cost_points == 12, "累积应可保留，等待真龙之心兑换"


# ---------- 错误输入 / 非法配置 ----------

def test_error_draft_origin_events_not_in_runtime():
    """错误输入：巴别塔/沉沦海草案事件不得进入运行时事件池（未实现副本）。"""
    events = parse_events("副本索引.md")
    assert "埋骨之地" in events
    assert events["埋骨之地"]["region"] == "龙心谷"
    assert "剥落的鳞" not in events, "巴别塔草案事件不得进入运行时"
    assert "最初的泪" not in events, "沉沦海草案事件不得进入运行时"


def test_error_invalid_option_rejected():
    """错误输入：不存在的选项被拒绝，事件不推进。"""
    engine = _engine("badopt")
    _set_event(engine)
    r = _resolve(engine, 99)
    assert not r["success"]
    assert engine.event_pool.current == "埋骨之地", "非法选项不应推进事件"
