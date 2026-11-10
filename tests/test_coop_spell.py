"""协力施法（2026-11-10 用户令）：一条法术开 N 个空位，场上角色逐格填道纹。

口径：
* 发起（declare_spell）不花出手；每格由填入者花 1 次出手；
* 填入只登记不结算，任意存活角色（双方都可以）都能填；
* 发动（release_spell）不再扣出手——出手已在填入时付清；空位＝空白直接跳过；
* 条件分支沿用瞬发法术 DSL：两臂全空整支跳过，只有「否则」有内容则取反条件；
* 战斗作用域：battle_start 清空上场残留。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from webapp import lab


@pytest.fixture
def roster_file(tmp_path, monkeypatch):
    path = tmp_path / "player_characters.json"
    monkeypatch.setenv("LINJI_CHARACTERS_FILE", str(path))
    return path


def _duel(roster_file):
    """A 方（杀伐/再生）vs 一只副本怪物的直接对战，回合已开始。"""
    catalog = lab.options(refresh=True)
    region = catalog["regions"][0]
    prepared = lab.duel({
        "a": {"name": "甲", "mode": "free", "blood_limit": 60, "speed_limit": 9,
              "mana_limit": 8, "daowen": ["杀伐", "再生"], "relics": [],
              "resonance": "转换"},
        "b": {"kind": "monster", "monsters": [region["monsters"][0]["name"]]},
        "region": region["name"], "direct": True,
    })
    engine = prepared["engine"]
    engine.execute_action("round_start", {"relic_choices": {}})
    return engine


def _enemy_daowen(engine):
    refs = engine.combat._combat_entity_refs()
    return next(iter(refs["enemy:0"].dao_wen))


def test_declare_costs_no_action_and_filler_pays_one(roster_file):
    """发起不花出手；填入者付 1 手；发动不再扣手。"""
    engine = _duel(roster_file)
    player = engine.state.player
    result = engine.execute_action("declare_spell", {"name": "合击", "slots": 2,
                                                     "cast_target_ref": "enemy:0"})
    assert result["success"], result
    spell_id = result["result"]["spell_id"]
    assert player.actions_used_this_round == 0

    filled = engine.execute_action("fill_spell", {"spell_id": spell_id, "slot": 0,
                                                  "daowen_name": "杀伐", "x": 1,
                                                  "target_mode": "target"})
    assert filled["success"], filled
    assert player.actions_used_this_round == 1

    engine.execute_action("skip", {"actor_ref": "enemy:0"})
    used = player.actions_used_this_round
    released = engine.execute_action("release_spell", {"spell_id": spell_id})
    assert released["success"], released
    assert player.actions_used_this_round == used, "发动不应再扣出手"
    assert released["result"]["execution_status"] == "completed"
    assert not [s for s in engine.state.open_spells if s["id"] == spell_id]


def test_cross_side_fill_and_release_flow(roster_file):
    """B 方轮回者也能往空位填自己的道纹；玩家没有的道纹借入结算、事后归还。"""
    # B 方带【庇护】（A 方没有）：才能真正测到借入/归还
    # （透支人人出生自带，借不借都分不出来；怪物道纹常带变形等实体替换副作用）。
    prepared = lab.duel({
        "a": {"name": "甲", "mode": "free", "blood_limit": 60, "speed_limit": 9,
              "mana_limit": 8, "daowen": ["杀伐", "再生"], "relics": [],
              "resonance": "转换"},
        "b": {"name": "乙", "mode": "free", "blood_limit": 60, "speed_limit": 2,
              "mana_limit": 8, "daowen": ["庇护"], "relics": [], "resonance": "转换"},
        "region": "罪孽都市", "direct": True,
    })
    engine = prepared["engine"]
    engine.execute_action("round_start", {"relic_choices": {}})
    player = engine.state.player
    assert "庇护" not in player.dao_wen

    spell_id = engine.execute_action("declare_spell", {"slots": 2,
                                                       "cast_target_ref": "enemy:0"}
                                     )["result"]["spell_id"]
    assert engine.execute_action("fill_spell", {
        "spell_id": spell_id, "slot": 0, "daowen_name": "杀伐", "x": 1,
        "target_mode": "target"})["success"]
    # A 填完轮到 B（轮回者填入付 1 手后换边）：B 把庇护填进空位1
    filled = engine.execute_action("fill_spell", {
        "spell_id": spell_id, "slot": 1, "actor_ref": "enemy:0",
        "daowen_name": "庇护", "x": 1, "target_mode": "self"})
    assert filled["success"], filled
    # B 填完换回 A；A 这一手已付掉（基础1手/回合）则推进到新回合再发动
    # ——协力法术跨回合保留，正是这条规则的存在意义。
    if engine.state.duel_turn != "player_side":
        assert engine.execute_action("skip", {"actor_ref": "enemy:0"})["success"]
    if engine.state.combat_subphase != "player_actions" or \
            engine.state.duel_turn != "player_side":
        assert engine.execute_action("round_end", {})["success"]
        engine.execute_action("round_start", {"relic_choices": {}})
    released = engine.execute_action("release_spell", {"spell_id": spell_id})
    assert released["success"], released
    coop = released["result"]["coop"]
    assert "杀伐" in coop["flow"] and "庇护" in coop["flow"], coop
    assert "庇护" in coop["temp_granted"], coop
    # 发动后在步骤间开敌方窗口：B 跳过窗口行动，法术续跑完最后一步并收尾。
    if engine.combat._spell_stack:
        assert engine.execute_action("skip", {"actor_ref": "enemy:0"})["success"]
    # 借来的道纹结算后归还：施法者不应凭空多出道纹
    assert "庇护" not in engine.state.player.dao_wen


def test_empty_slots_are_skipped_and_all_blank_release_is_rejected(roster_file):
    """空位＝空白直接跳过；全空则发动被拒绝并提示取消。"""
    engine = _duel(roster_file)
    spell_id = engine.execute_action("declare_spell", {"slots": 2,
                                                       "cast_target_ref": "enemy:0"}
                                     )["result"]["spell_id"]
    assert engine.execute_action("fill_spell", {
        "spell_id": spell_id, "slot": 0, "daowen_name": "杀伐", "x": 1,
        "target_mode": "target", "target_ref": "enemy:0"})["success"]
    # A 填完轮到 B：B 跳过（结束 B 方回合），A 拿回行动权再发动
    assert engine.execute_action("skip", {"actor_ref": "enemy:0"})["success"]
    released = engine.execute_action("release_spell", {"spell_id": spell_id})
    assert released["success"], released
    assert released["result"]["coop"]["flow"] == "发动杀伐X于目标"
    assert len(released["result"]["step_results"]) == 1, "空白格应被跳过"

    blank = engine.execute_action("declare_spell", {"slots": 1})["result"]["spell_id"]
    engine.execute_action("skip", {"actor_ref": "enemy:0"})
    refused = engine.execute_action("release_spell", {"spell_id": blank})
    assert not refused["success"] and "空白" in refused["error"]
    assert engine.execute_action("cancel_spell", {"spell_id": blank})["success"]


def test_if_branches_skip_blank_arms_and_negate_else_only(roster_file):
    """if 两臂全空→整支跳过；只有「否则」有内容→取反条件，顺序不变。"""
    engine = _duel(roster_file)
    template = [
        {"kind": "daowen"},
        {"kind": "if", "cond": "自身 生命 小于 100",
         "then": [{"kind": "daowen"}], "else": []},
    ]
    spell_id = engine.execute_action("declare_spell", {"slots": template,
                                                       "cast_target_ref": "enemy:0"}
                                     )["result"]["spell_id"]
    assert engine.execute_action("fill_spell", {
        "spell_id": spell_id, "slot": 0, "daowen_name": "杀伐", "x": 2,
        "target_mode": "target"})["success"]
    engine.execute_action("skip", {"actor_ref": "enemy:0"})
    released = engine.execute_action("release_spell", {"spell_id": spell_id})
    assert released["success"], released
    assert released["result"]["coop"]["flow"] == "发动杀伐X于目标"

    template2 = [{"kind": "if", "cond": "自身 法力 大于 100",
                  "then": [{"kind": "daowen"}], "else": [{"kind": "daowen"}]}]
    spell2 = engine.execute_action("declare_spell", {"slots": template2})["result"]["spell_id"]
    # flat 深度优先编号：then 格=0，else 格=1
    assert engine.execute_action("fill_spell", {
        "spell_id": spell2, "slot": 1, "daowen_name": "再生", "x": 1,
        "target_mode": "self"})["success"]
    engine.execute_action("skip", {"actor_ref": "enemy:0"})
    engine.execute_action("round_end", {})
    engine.execute_action("round_start", {"relic_choices": {}})
    released2 = engine.execute_action("release_spell", {"spell_id": spell2})
    assert released2["success"], released2
    assert released2["result"]["coop"]["flow"] == \
        "若非(自身 法力 大于 100)则（发动再生X于自身）"


def test_slot_template_validation_and_duplicate_fill(roster_file):
    """坏模板就地拒绝；同一格只能填一次；没持有的道纹不能填。"""
    engine = _duel(roster_file)
    bad = engine.execute_action("declare_spell", {"slots": [{"kind": "if", "cond": "乱写"}]})
    assert not bad["success"]
    zero = engine.execute_action("declare_spell", {"slots": []})
    assert not zero["success"]

    spell_id = engine.execute_action("declare_spell", {"slots": 1})["result"]["spell_id"]
    assert engine.execute_action("fill_spell", {
        "spell_id": spell_id, "slot": 0, "daowen_name": "再生", "x": 1,
        "target_mode": "self"})["success"]
    # A 填完轮到 B：B 跳过让回 A，才能继续做非法尝试（交替出手校验在前）
    assert engine.execute_action("skip", {"actor_ref": "enemy:0"})["success"]
    again = engine.execute_action("fill_spell", {
        "spell_id": spell_id, "slot": 0, "daowen_name": "再生", "x": 1,
        "target_mode": "self"})
    assert not again["success"] and "已被" in again["error"]
    wrong = engine.execute_action("fill_spell", {
        "spell_id": spell_id, "slot": 9, "daowen_name": "再生", "x": 1,
        "target_mode": "self"})
    assert not wrong["success"]
    not_mine = engine.execute_action("fill_spell", {
        "spell_id": spell_id, "slot": 0, "daowen_name": "不存在的道纹", "x": 1,
        "target_mode": "self"})
    assert not not_mine["success"]


def test_open_spells_are_cleared_on_new_battle(roster_file):
    """协力法术是战斗作用域：开新战斗时清空。"""
    engine = _duel(roster_file)
    assert engine.execute_action("declare_spell", {"slots": 1})["success"]
    assert engine.state.open_spells
    engine.execute_action("restart_battle", {})
    assert engine.state.open_spells == []


def test_available_actions_list_coop_entries_for_both_sides(roster_file):
    """A 方与 B 方回合都能看到协力行动；有发起时不再重复列发起。"""
    engine = _duel(roster_file)
    names = [a["action_type"] for a in engine.get_available_actions()["actions"]]
    assert "declare_spell" in names

    spell_id = engine.execute_action("declare_spell", {"slots": 1})["result"]["spell_id"]
    names = [a["action_type"] for a in engine.get_available_actions()["actions"]]
    assert "declare_spell" not in names, "已有未发动法术时不应再列发起"
    assert "fill_spell" in names and "release_spell" in names and "cancel_spell" in names

    engine.execute_action("round_end", {})
    engine.execute_action("round_start", {"relic_choices": {}})
    # 构造 B 方回合：A 方跳过（结束 A 方回合），协力行动仍在列表里
    ended = engine.execute_action("skip", {"actor_ref": "player:0"})
    assert ended["success"], ended
    names = [a["action_type"] for a in engine.get_available_actions()["actions"]]
    assert "fill_spell" in names
    fill = engine.execute_action("fill_spell", {
        "spell_id": spell_id, "slot": 0, "actor_ref": "enemy:0",
        "daowen_name": _enemy_daowen(engine), "x": 1, "target_mode": "self"})
    assert fill["success"], fill
