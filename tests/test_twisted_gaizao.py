"""扭曲都市专属行动【改造】（2026-11-01 用户令：由【维修】改做）。

规则：选择2/3/4件不同名消耗品，使其全部效果叠加到其中一个消耗品中。
- 档位＝参与件数（2件 0碎片／3件 5碎片／4件 12碎片），另耗 1 点精力；
- 只叠效果、不叠耐久：目标条目耐久与上限不变，被并入的条目从持有中移除；
- 使用合成条目时逐条结算全部来源效果，最后只扣它自己的 1 点耐久；
- 战斗中的被动／需额外模式的条目（万钧印、翼类、雕塑、龙心、被动触发型自定义）不参与。
"""
import dataclasses
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import Consumable
from tests.setup_support import finish_initial_daowen


def _engine(tmp_path, region="扭曲都市", seed=31):
    engine = GameEngine(
        db_path=str(tmp_path / "rulings.db"), save_dir=str(tmp_path / "saves"),
        sealed_candidate_path=str(tmp_path / "sealed.json"),
        death_book_path=str(tmp_path / "death.md"), rng_seed=seed,
    )
    engine.execute_action("setup_attributes", {
        "name": "改造测试", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": region})
    engine.execute_action("choose_discovered_relic", {
        "relic_name": setup["result"]["relic_choices"][0],
    })
    engine.state.shards = 100
    engine.state.energy = 3
    return engine


def _gaizao(engine, tier, names, target):
    return engine.execute_action("pre_battle_action", {
        "sub_action": "改造", "tier": tier, "names": names, "target": target})


def _tool(name):
    from engine.api import TWISTED_TOOL_LIBRARY
    uses, effect = TWISTED_TOOL_LIBRARY[name]
    return Consumable(name=name, effect=effect, current_uses=uses, max_uses=uses)


def test_gaizao_is_twisted_city_exclusive(tmp_path):
    """专属门禁：其它副本不能用【改造】，被拒时精力退还。"""
    engine = _engine(tmp_path, region="龙心谷")
    engine.state.consumables = [_tool("急救箱"), _tool("储能电池")]
    result = _gaizao(engine, 2, ["急救箱", "储能电池"], "急救箱")
    assert not result["success"]
    assert "扭曲都市" in result["error"]
    assert engine.state.energy == 3


def test_gaizao_stacks_all_effects_onto_one_item(tmp_path):
    """效果叠加：目标条目保留自身耐久，效果文本合并，其它条目消失。"""
    engine = _engine(tmp_path)
    pump, battery = _tool("备用血泵"), _tool("储能电池")
    engine.state.consumables = [pump, battery]

    result = _gaizao(engine, 2, ["备用血泵", "储能电池"], "备用血泵")
    assert result["success"], result.get("error")
    assert result["result"]["absorbed"] == ["储能电池"]
    assert result["result"]["shard_cost"] == 0        # 2件档免费
    assert (pump.current_uses, pump.max_uses) == (3, 3), "只叠效果，不叠耐久"
    assert "获得20点" in pump.effect and "12点法力" in pump.effect
    assert [item.name for item in engine.state.consumables] == ["备用血泵"]
    assert [source["name"] for source in pump.merged_sources] == ["备用血泵", "储能电池"]


def test_merged_item_executes_every_source_effect(tmp_path):
    """使用合成条目：逐条结算全部来源效果，并且只扣本条目 1 点耐久。"""
    engine = _engine(tmp_path, region="扭曲都市")
    engine.state.consumables = [_tool("备用血泵"), _tool("储能电池")]
    assert _gaizao(engine, 2, ["备用血泵", "储能电池"], "备用血泵")["success"]

    engine.state.phase = "in_combat"
    player = engine.state.player
    player.current_hp = max(1, player.blood_limit - 40)
    player.current_mana = 0
    player.mana_limit = 40          # 夹具法限只有3，先抬高才看得见储能电池的12点
    hp_before, mana_before = player.current_hp, player.current_mana

    used = engine.execute_action("consume_item", {"name": "备用血泵"})
    assert used["success"], used.get("error")
    assert player.current_mana == mana_before + 12, "储能电池的效果必须一并结算"
    assert player.current_hp == hp_before + 20, "备用血泵的回血必须一并结算"
    assert [entry["name"] for entry in used["result"]["sources"]] == ["备用血泵", "储能电池"]
    assert all(entry["success"] for entry in used["result"]["sources"])
    item = engine.state.consumables[0]
    assert item.current_uses == 2, "一次使用只扣合成条目自身的1点耐久"


def test_second_gaizao_absorbs_existing_merged_effects(tmp_path):
    """二次改造：已叠加过的条目再次并入时，中间效果不会丢。"""
    engine = _engine(tmp_path)
    engine.state.consumables = [_tool("备用血泵"), _tool("储能电池"), _tool("强光探照灯")]
    assert _gaizao(engine, 2, ["备用血泵", "储能电池"], "备用血泵")["success"]
    assert _gaizao(engine, 2, ["备用血泵", "强光探照灯"], "强光探照灯")["success"]
    target = next(item for item in engine.state.consumables if item.name == "强光探照灯")
    assert [source["name"] for source in target.merged_sources] == [
        "强光探照灯", "备用血泵", "储能电池"]
    assert "获得20点" in target.effect and "12点法力" in target.effect


def test_gaizao_rejects_passive_and_mode_dependent_items(tmp_path):
    """万钧印（被动触发）与翼类（每次选模式）不能参与改造。"""
    engine = _engine(tmp_path)
    engine.state.consumables = [_tool("万钧印"), _tool("储能电池")]
    blocked = _gaizao(engine, 2, ["万钧印", "储能电池"], "储能电池")
    assert not blocked["success"] and "万钧印" in blocked["error"]
    assert engine.state.energy == 3

    wing = Consumable(name="血翼", effect="", current_uses=1, max_uses=1, kind="wing")
    engine.state.consumables = [wing, _tool("储能电池")]
    blocked_wing = _gaizao(engine, 2, ["血翼", "储能电池"], "储能电池")
    assert not blocked_wing["success"]


def test_gaizao_result_survives_serialization(tmp_path):
    """合成条目随存档往返：merged_sources 与效果文本都保留。"""
    engine = _engine(tmp_path)
    engine.state.consumables = [_tool("备用血泵"), _tool("储能电池")]
    assert _gaizao(engine, 2, ["备用血泵", "储能电池"], "备用血泵")["success"]
    item = engine.state.consumables[0]
    restored = Consumable(**dataclasses.asdict(item))
    assert restored.merged_sources == item.merged_sources
    assert restored.effect == item.effect
    assert restored.to_dict()["merged_sources"] == item.merged_sources


def test_gaizao_shard_ladder_matches_item_count(tmp_path):
    """3件档 5 碎片、4件档 12 碎片；碎片不足时整笔拒绝且不改持有。"""
    engine = _engine(tmp_path)
    engine.state.consumables = [_tool("备用血泵"), _tool("储能电池"),
                                _tool("强光探照灯"), _tool("高爆手雷")]
    ok3 = _gaizao(engine, 3, ["备用血泵", "储能电池", "强光探照灯"], "备用血泵")
    assert ok3["success"] and ok3["result"]["shard_cost"] == 5
    assert engine.state.shards == 95

    engine.state.shards = 3
    engine.state.consumables = [_tool("备用血泵"), _tool("储能电池"), _tool("高爆手雷")]
    before = [item.name for item in engine.state.consumables]
    poor = _gaizao(engine, 3, ["备用血泵", "储能电池", "高爆手雷"], "备用血泵")
    assert not poor["success"] and "碎片" in poor["error"]
    assert [item.name for item in engine.state.consumables] == before
