"""扭曲都市专属行动【改造】（由【维修】改做）。

规则：选择2/3/4件不同名消耗品，使其全部效果叠加到其中一个消耗品中。
- 档位＝参与件数（2件 0碎片／3件 5碎片／4件 12碎片），另耗 1 点精力；
- 效果与耐久一起叠加：被并入条目的当前耐久与上限进目标条目，被并入的条目从持有中移除；
- 使用合成条目时逐条结算全部来源效果，最后只扣它自己的 1 点耐久（耐久池共用）；
- 被动效果（龙心、万钧印）随条目携带、不参与手动结算，但持有判定仍然看得到。
"""
import dataclasses
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.models import Consumable, Entity
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
    assert (pump.current_uses, pump.max_uses) == (6, 6), "耐久与上限一起叠加"
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
    assert item.current_uses == 5, "耐久已合并（3+3），一次使用只扣共用池的1点"


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


def test_wanjunyin_still_triggers_after_being_merged(tmp_path):
    """被动效果不因改造失效：万钧印并进另一件后，被攻击仍能改道攻击者自己。

    万钧印本身不能手动使用，但它作为来源随条目携带；触发时扣的是合成后的共用耐久池。
    """
    engine = _engine(tmp_path)
    engine.state.consumables = [_tool("万钧印"), _tool("储能电池")]
    assert _gaizao(engine, 2, ["万钧印", "储能电池"], "储能电池")["success"]
    host = engine.state.consumables[0]
    assert host.name == "储能电池"
    assert host.current_uses == 3 + 3, "耐久池合并"
    assert engine.combat._wanjunyin_holder(engine.state.player)

    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "player_actions"
    player = engine.state.player
    enemy = Entity("靶怪", "怪物", blood_limit=100, current_hp=100,
                   attack_count=1, attack_power=10)
    engine.state.enemies = [enemy]
    before = enemy.current_hp

    engine.combat.resolve_attack(enemy, player, hit_index=0, is_must_hit=True,
                                 dodge=False, allocated_damage=10,
                                 wanjunyin=True)
    assert enemy.current_hp == before - 10, "改道：这一击打回攻击者自己"
    assert host.current_uses == 5, "触发消耗的是合成条目的共用耐久池"


def test_wing_and_sculpture_sources_use_per_source_params(tmp_path):
    """需要选择的效果照常可用：用 source_params 按来源名分别提交模式。"""
    engine = _engine(tmp_path)
    wing = Consumable(name="血翼", effect="", current_uses=1, max_uses=1, kind="wing")
    engine.state.consumables = [wing, _tool("储能电池")]
    assert _gaizao(engine, 2, ["血翼", "储能电池"], "血翼")["success"]

    engine.state.phase = "in_combat"
    host = engine.state.consumables[0]
    host.current_uses = 3
    result = engine.execute_action("consume_item", {
        "name": "血翼",
        "source_params": {"血翼": {"mode": "fly"}},
    })
    assert result["success"], result.get("error")
    by_name = {entry["name"]: entry for entry in result["result"]["sources"]}
    assert by_name["血翼"]["success"], by_name["血翼"]
    assert by_name["储能电池"]["success"], "未单独提交的普通来源照常结算"


def test_carried_dragon_heart_offsets_cost(tmp_path):
    """龙心被并进别的条目后仍按耐久抵扣对应类型的代价。"""
    engine = _engine(tmp_path)
    heart = Consumable(name="生命龙心", effect="消耗Y点耐久可抵消Y点生命代价",
                       current_uses=5, max_uses=5, kind="dragon_heart",
                       dragon_heart_type="生命")
    engine.state.consumables = [heart, _tool("储能电池")]
    assert _gaizao(engine, 2, ["生命龙心", "储能电池"], "储能电池")["success"]
    host = engine.state.consumables[0]
    assert host.source_named("生命龙心") is not None
    assert engine.combat._dragon_heart_matches(host, "生命")


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


def test_wanjunyin_redirect_is_a_respond_window_choice(tmp_path):
    """按物品索引=持有者选择：应对窗口显式选「万钧印改道」才把这一击打回攻击者。"""
    engine = _engine(tmp_path)
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = "player_actions"
    engine.state.duel_turn = "opponent_side"
    player = engine.state.player
    enemy = Entity("靶怪", "怪物", blood_limit=100, current_hp=100,
                   attack_count=2, attack_power=10)
    engine.state.enemies = [enemy]
    engine.state.consumables = [_tool("万钧印")]
    assert engine.execute_action("prepare_attack", {"actor_ref": "enemy:0"})["success"]
    assert engine.execute_action("attack_declare_hit",
                                 {"target_ref": "player:0", "damage": 10})["success"]
    step = engine.execute_action("attack_respond_hit", {"choice": "wanjunyin"})
    assert step["success"], step
    assert enemy.current_hp == 90, "改道：这一击打回攻击者自己"
    assert player.current_hp == player.blood_limit
    assert engine.state.consumables[0].current_uses == 2
