"""癌变怪物吸收与【无限肉块】契约测试（2026-10-05 用户令）。

规则口径（规则正文·癌变）：
- 癌变不再为局外【休整】提供额外恢复量（原「每吸收一只+8」已废止，
  GameState.rest_heal_bonus 字段随之删除）；
- 每吸收一只癌变怪物获得【无限肉块】（1/1）：使用后恢复10生命，
  不计入本场癌变累计回复量；[战终]恢复80%已损耐久，向上取整；
- 癌变怪物不产碎片；轮回者/同伴癌变直接命零、不产生奖励。
"""
import math

from engine.api import GameEngine
from engine.enums import CombatSubphase
from engine.models import Entity, Consumable


from tests.setup_support import finish_initial_daowen


def _engine(tmp_path) -> GameEngine:
    return GameEngine(
        db_path=str(tmp_path / "rulings.db"),
        save_dir=str(tmp_path / "saves"),
        sealed_candidate_path=str(tmp_path / "sealed.json"),
        death_book_path=str(tmp_path / "death.md"),
        rng_seed=19,
    )


def _setup_player(engine: GameEngine) -> Entity:
    result = engine.execute_action("setup_attributes", {
        "name": "测试者", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    finish_initial_daowen(engine)
    assert result["success"]
    return engine.state.player


def _cancer_monster(engine: GameEngine, name: str = "癌变怪") -> tuple[Entity, dict]:
    monster = Entity(name, "怪物", blood_limit=100, current_hp=1,
                     attack_count=1, attack_power=1)
    engine.state.enemies.append(monster)
    monster.heal(250)
    result = engine.combat.check_cancer(monster)
    assert result is not None
    return monster, result


def test_monster_cancer_grants_meat_chunk_not_rest_bonus(tmp_path):
    """正常路径：癌变怪物被吸收→获得【无限肉块】（1/1），不再加成休整。"""
    engine = _engine(tmp_path)
    _setup_player(engine)
    first, first_result = _cancer_monster(engine, "甲")
    second, second_result = _cancer_monster(engine, "乙")

    assert not first.is_alive and not second.is_alive
    assert first_result["consumable"] == "无限肉块"
    assert second_result["consumable"] == "无限肉块"
    meats = [c for c in engine.state.consumables if c.kind == "infinite_meat"]
    assert len(meats) == 2
    for meat in meats:
        assert meat.current_uses == 1 and meat.max_uses == 1
        assert "恢复10生命" in meat.effect
        assert "战终恢复80%已损耐久" in meat.effect
    # 休整加成机制已删除：状态上不应再有 rest_heal_bonus 字段
    assert not hasattr(engine.state, "rest_heal_bonus")


def test_meat_chunk_heals_ten_and_excludes_cancer_accumulation(tmp_path):
    """正常路径：肉块恢复10生命，且不计入本场癌变累计回复量。"""
    engine = _engine(tmp_path)
    player = _setup_player(engine)
    engine.state.consumables.append(Consumable(
        name="无限肉块",
        effect="使用后恢复10生命，不计入癌变累计治疗量；战终恢复80%已损耐久",
        current_uses=1, max_uses=1, kind="infinite_meat"))
    player.current_hp = max(1, player.current_hp - 30)
    total_before = player.total_healed
    battle_before = player.healed_this_battle

    r = engine.execute_action("consume_item", {"name": "无限肉块"})

    assert r["success"], r
    assert r["result"]["heal"]["actual_heal"] == 10
    assert r["result"]["excluded_from_cancer_accumulation"] is True
    assert player.current_hp <= player.blood_limit
    assert player.total_healed == total_before, "肉块不得计入癌变累计"
    assert player.healed_this_battle == battle_before
    assert engine.state.consumables[0].current_uses == 0


def test_meat_chunk_restores_eighty_percent_durability_at_battle_end(tmp_path):
    """正常路径：[战终]恢复80%已损耐久（向上取整）。"""
    engine = _engine(tmp_path)
    _setup_player(engine)
    meat = Consumable(name="无限肉块",
                      effect="使用后恢复10生命，不计入癌变累计治疗量；战终恢复80%已损耐久",
                      current_uses=1, max_uses=1, kind="infinite_meat")
    engine.state.consumables.append(meat)
    meat.current_uses = 0  # 已用完：已损耐久=1
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value

    end = engine.execute_action("battle_end", {})

    assert end["success"]
    # ceil(1×0.8)=1 → 1/1 回满（这正是「无限」的由来）
    assert meat.current_uses == 1 and meat.max_uses == 1


def test_cancer_monster_gives_no_shards(tmp_path):
    """正常路径：癌变怪物不产碎片，且走永久离场通道。"""
    engine = _engine(tmp_path)
    _setup_player(engine)
    engine.state.phase = "in_combat"
    engine.state.combat_subphase = CombatSubphase.AWAIT_ROUND_END.value
    engine.state.current_battle = 1
    monster, _ = _cancer_monster(engine)
    shards_before = engine.state.shards

    end = engine.execute_action("battle_end", {})

    assert end["success"]
    assert engine.state.shards == shards_before
    assert {entry["name"] for entry in end["result"]["removed_via_alt_path"]} == {monster.name}


def test_rest_heal_equals_tier_base_after_cancer(tmp_path):
    """边界：吸收癌变怪物后，休整恢复量仍=档位基础值（无额外加成）。"""
    engine = _engine(tmp_path)
    player = _setup_player(engine)
    _cancer_monster(engine)
    engine.state.phase = "pre_battle"
    engine.state.energy = 3
    player.current_hp = 1

    expected_base = math.ceil(player.blood_limit * 0.2)
    rest = engine.execute_action("pre_battle_action", {
        "sub_action": "休整", "tier": 1,
        "heal_allocations": [{"target_ref": "player:0", "amount": expected_base}],
    })

    assert rest["success"], rest
    assert rest["result"]["base_heal_amount"] == expected_base
    assert rest["result"]["heal_amount"] == expected_base
    assert "rest_heal_bonus" not in rest["result"]


def test_character_cancer_gives_no_reward(tmp_path):
    """边界：轮回者癌变直接命零，不吸收进书、不产生肉块。"""
    engine = _engine(tmp_path)
    player = _setup_player(engine)
    player.total_healed = engine.combat.cancer_threshold_of(player)

    result = engine.combat.check_cancer(player)

    assert result["type"] == "cancer" and not player.is_alive
    assert engine.state.consumables == []
