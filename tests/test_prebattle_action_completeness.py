"""局外多档学习、共鸣与改造必须真实结算，不返回空指令。"""
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
        "name": "局外测试", "blood_points": 11, "speed_points": 8, "mana_points": 6,
    })
    finish_initial_daowen(engine)
    engine.execute_action("setup_choose_resonance", {"resonance_type": "转换"})
    setup = engine.execute_action("setup_choose_region", {"region": region})
    engine.execute_action("choose_discovered_relic", {
        "relic_name": setup["result"]["relic_choices"][0],
    })
    engine.state.shards = 100
    return engine


def test_available_prebattle_schemas_use_real_executable_parameter_names(tmp_path):
    engine = _engine(tmp_path)
    schemas = [action["params_schema"] for action in engine.get_available_actions()["actions"]
               if action["action_type"] == "pre_battle_action"]
    by_sub_action = {schema["sub_action"]: schema for schema in schemas}

    learning = by_sub_action["学习"]
    assert "mode" not in learning
    # 2026-09-16：法术免学习后，局外【学习】只剩转化道纹，自创法术改由
    # 战斗中的 define_spell 完成，故不再要求 spell / dm_approved 参数。
    assert {"sub", "tier", "names"} <= set(learning)
    assert "custom_spell" not in learning["sub"], "局外自创法术入口已取消"
    assert {"heal_allocations", "tier"} <= set(by_sub_action["休整"])
    assert {"allocations", "tier"} <= set(by_sub_action["修行"])
    assert "tier" in by_sub_action["探索"]
    assert {"tier", "names", "target"} <= set(by_sub_action["改造"])
    assert "维修" not in by_sub_action, "维修已按 改为【改造】"
    assert "additional" not in by_sub_action["改造"]


def test_learning_two_daowen_applies_all_names(tmp_path):
    """学习道纹多档：一次学习两种，全部落到玩家道纹上（法术已免学习）。"""
    engine = _engine(tmp_path)
    # 2026-10-12 新增【承载上限】(30X[血限]=X 种道纹)：本条要学 2 种、共 3 种，
    # 而夹具 11/8/6 → [血限]66 只有 2 种额度。抬到 90 得 3 种额度；只改本条、
    # 不动共享夹具，因为「休整」额度按血限百分比计，别处断言依赖 [血限]66。
    engine.state.player.blood_limit = 90
    # 开局仅持【杀伐】，一次学习 再生/庇护 两个转化道纹。
    r = engine.execute_action("pre_battle_action", {
        "sub_action": "学习", "sub": "daowen", "tier": 2,
        "names": ["再生", "庇护"],
    })
    assert r["success"], r.get("error")
    assert {"再生", "庇护"} <= set(engine.state.player.dao_wen)


def test_spell_learning_sub_is_retired_but_daowen_still_works(tmp_path):
    """法术免学习：sub=spell 作废且不产生效果；sub=daowen 仍正常。"""
    engine = _engine(tmp_path)
    before = set(engine.state.player.dao_wen)
    r = engine.execute_action("pre_battle_action", {
        "sub_action": "学习", "sub": "spell", "tier": 1, "names": ["先发制人"]})
    assert not r["success"], "法术已无需学习，旧入口应拒绝"
    assert engine.state.player.spells == []
    assert set(engine.state.player.dao_wen) == before



def test_resonance_choose_requires_second_energy_and_applies_cost(tmp_path):
    engine = _engine(tmp_path)
    engine._init_relic_pool()
    choice = engine.state.relics_pool[0].name
    engine.state.energy = 1
    before = (engine.state.energy, engine.state.shards, len(engine.state.relics))

    insufficient = engine.execute_action("pre_battle_action", {
        "sub_action": "共鸣", "tier": 2, "name": choice,
    })
    assert not insufficient["success"]
    assert (engine.state.energy, engine.state.shards, len(engine.state.relics)) == before

    engine.state.energy = 3
    success = engine.execute_action("pre_battle_action", {
        "sub_action": "共鸣", "tier": 2, "name": choice,
    })
    assert success["success"]
    assert engine.state.energy == 1 and engine.state.shards == 85
    assert any(relic.name == choice for relic in engine.state.relics)


def test_gaizao_merges_effects_into_target_and_charges_shards(tmp_path):
    """改造：把多件不同名消耗品的全部效果与耐久叠到其中一件，被并入的条目移除。"""
    engine = _engine(tmp_path)
    first = Consumable("甲", "回复5生命", current_uses=1, max_uses=3)
    second = Consumable("乙", "获得3格挡", current_uses=2, max_uses=3)
    third = Consumable("丙", "造成4伤害", current_uses=1, max_uses=1)
    engine.state.consumables = [first, second, third]
    engine.state.energy = 3

    merged = engine.execute_action("pre_battle_action", {
        "sub_action": "改造", "tier": 3, "names": ["甲", "乙", "丙"], "target": "乙",
    })
    assert merged["success"], merged.get("error")
    # 效果与耐久一起叠加：目标条目成为唯一的耐久池（2+1+1 当前 / 3+3+1 上限）
    assert (second.current_uses, second.max_uses) == (4, 7)
    # 顺序＝目标自身效果在前，其余按 names 顺序并入
    assert second.effect == "获得3格挡；回复5生命；造成4伤害"
    assert [source["name"] for source in second.merged_sources] == ["乙", "甲", "丙"]
    assert [item.name for item in engine.state.consumables] == ["乙"]
    assert engine.state.shards == 95          # 3件档价目 5 碎片


def test_gaizao_rejects_duplicate_names_and_foreign_target_atomically(tmp_path):
    """改造：同名、目标不在名单、件数与档位不符都在消耗精力前拒绝并退款。"""
    engine = _engine(tmp_path)
    engine.state.consumables = [Consumable("甲", "a"), Consumable("乙", "b")]
    engine.state.energy = 3

    duplicated = engine.execute_action("pre_battle_action", {
        "sub_action": "改造", "tier": 2, "names": ["甲", "甲"], "target": "甲"})
    assert not duplicated["success"] and "不同名" in duplicated["error"]

    foreign_target = engine.execute_action("pre_battle_action", {
        "sub_action": "改造", "tier": 2, "names": ["甲", "乙"], "target": "丙"})
    assert not foreign_target["success"] and "target" in foreign_target["error"]

    wrong_tier = engine.execute_action("pre_battle_action", {
        "sub_action": "改造", "tier": 4, "names": ["甲", "乙"], "target": "甲"})
    assert not wrong_tier["success"]

    assert engine.state.energy == 3, "被拒绝的改造必须退还精力"
    assert [item.name for item in engine.state.consumables] == ["甲", "乙"]


def test_rest_freely_splits_full_amount_without_combat_heal_tracking(tmp_path):
    engine = _engine(tmp_path)
    player = engine.state.player
    player.current_hp = 10
    player.total_healed = 4
    player.healed_this_battle = 3
    friend = Entity("朋友甲", "friend", blood_limit=30, current_hp=1,
                    total_healed=5, healed_this_battle=2)
    employee = Entity("员工甲", "employee", blood_limit=30, current_hp=2,
                      total_healed=6, healed_this_battle=1)
    engine.state.friends.append(friend)
    engine.state.employees.append(employee)
    before_tracking = [(entity.total_healed, entity.healed_this_battle)
                       for entity in (player, friend, employee)]

    rested = engine.execute_action("pre_battle_action", {
        "sub_action": "休整", "tier": 2,
        "heal_allocations": [
            {"target_ref": "player:0", "amount": 15},
            {"target_ref": "friend:0", "amount": 5},
            {"target_ref": "employee:0", "amount": 7},
        ],
    })

    assert rested["success"]
    # 2026-09-10 休整改制（二次裁定三档）：tier2 额度=玩家血限×40%（ceil，bl66→27），
    # 自由拆分、不计癌变追踪
    assert [player.current_hp, friend.current_hp, employee.current_hp] == [25, 6, 9]
    assert [(entity.total_healed, entity.healed_this_battle)
            for entity in (player, friend, employee)] == before_tracking
    assert engine.state.shards == 90


def test_rest_requires_exact_valid_allocation_and_is_atomic(tmp_path):
    engine = _engine(tmp_path)
    engine.state.player.current_hp = 10
    before = (engine.state.player.current_hp, engine.state.energy, engine.state.shards)

    incomplete = engine.execute_action("pre_battle_action", {
        "sub_action": "休整", "tier": 1,
        "heal_allocations": [{"target_ref": "player:0", "amount": 7}],
    })
    assert not incomplete["success"]
    assert (engine.state.player.current_hp, engine.state.energy, engine.state.shards) == before

    unknown_target = engine.execute_action("pre_battle_action", {
        "sub_action": "休整", "tier": 1,
        "heal_allocations": [{"target_ref": "friend:99", "amount": 8}],
    })
    assert not unknown_target["success"]
    assert (engine.state.player.current_hp, engine.state.energy, engine.state.shards) == before


def test_training_splits_tier_points_between_speed_and_mana(tmp_path):
    engine = _engine(tmp_path)
    player = engine.state.player
    before = (player.speed_limit, player.mana_limit)

    # 1属性点=1[速限]，2属性点=1[法限]
    trained = engine.execute_action("pre_battle_action", {
        "sub_action": "修行", "tier": 4,
        "allocations": {"speed_points": 2, "mana_points": 2},
    })

    assert trained["success"], trained
    assert (player.speed_limit, player.mana_limit) == (before[0] + 2, before[1] + 1)
    assert (player.current_speed, player.current_mana) == (player.speed_limit, player.mana_limit)
    assert trained["result"]["allocations"] == {"blood_points": 0, "speed_points": 2, "mana_points": 2}
    assert trained["result"]["gained"] == {"blood": 0, "speed": 2, "mana": 1}
    assert engine.state.shards == 35


def test_training_banks_points_when_no_allocations(tmp_path):
    """DM裁定 2026-09-10：属性点可存储——不传 allocations 就全部入池，随时再兑。

    这解掉了旧口径的死结：tier1 只给 1 点，而 2 点才买得到 1 单位，
    旧写法要么强制花掉（买不到东西）要么被拒（精力回滚 → 调用方空转）。
    """
    engine = _engine(tmp_path)
    player = engine.state.player
    bank_before = engine.state.attribute_points
    panel_before = (player.speed_limit, player.mana_limit)

    for _ in range(2):      # tier1 每次只给 1 点，攒两次才够 2 点一档
        r = engine.execute_action("pre_battle_action", {"sub_action": "修行", "tier": 1})
        assert r["success"], r
        assert (player.speed_limit, player.mana_limit) == panel_before, "存点不改面板"
        assert r["result"]["gained"] == {"blood": 0, "speed": 0, "mana": 0}
    assert engine.state.attribute_points == bank_before + 2

    # 攒到 2 点后随时兑换（自由动作，不耗精力/碎片）
    energy_before, shards_before = engine.state.energy, engine.state.shards
    d = engine.execute_action("redeem_attribute_points",
                              {"allocations": {"speed_points": 2, "mana_points": 0}})
    assert d["success"], d
    assert player.speed_limit == panel_before[0] + 2
    assert engine.state.attribute_points == bank_before
    assert (engine.state.energy, engine.state.shards) == (energy_before, shards_before), \
        "兑换不该消耗精力或碎片"


def test_redeem_rejects_odd_mana_and_overspend_and_in_combat(tmp_path):
    """边界：法限奇数点买不出半档（速限 1 点一档合法）；超额拒绝；战斗内拒绝。"""
    engine = _engine(tmp_path)
    engine.state.attribute_points = 3

    odd = engine.execute_action("redeem_attribute_points",
                                {"allocations": {"speed_points": 0, "mana_points": 1}})
    assert not odd["success"] and "偶数" in odd["error"]

    one_speed = engine.execute_action("redeem_attribute_points",
                                      {"allocations": {"speed_points": 1, "mana_points": 0}})
    assert one_speed["success"] and engine.state.attribute_points == 2, \
        "1 属性点正好兑 1 速限"

    over = engine.execute_action("redeem_attribute_points",
                                 {"allocations": {"speed_points": 4, "mana_points": 0}})
    assert not over["success"] and "属性点不足" in over["error"]

    engine.state.phase = "in_combat"
    combat = engine.execute_action("redeem_attribute_points",
                                   {"allocations": {"speed_points": 2, "mana_points": 0}})
    assert not combat["success"] and "局外" in combat["error"]


def test_training_rejects_invalid_split_atomically(tmp_path):
    engine = _engine(tmp_path)
    player = engine.state.player
    before = (player.speed_limit, player.mana_limit, player.current_speed,
              player.current_mana, engine.state.energy, engine.state.shards)

    mismatch = engine.execute_action("pre_battle_action", {
        "sub_action": "修行", "tier": 3,
        "allocations": {"speed_points": 1, "mana_points": 1},
    })
    assert not mismatch["success"]
    assert (player.speed_limit, player.mana_limit, player.current_speed,
            player.current_mana, engine.state.energy, engine.state.shards) == before

    boolean = engine.execute_action("pre_battle_action", {
        "sub_action": "修行", "tier": 1,
        "allocations": {"speed_points": True, "mana_points": 0},
    })
    assert not boolean["success"]
    assert (player.speed_limit, player.mana_limit, player.current_speed,
            player.current_mana, engine.state.energy, engine.state.shards) == before


def test_training_can_raise_blood_limit(tmp_path):
    """局外修行不再与开局初始分配区分，血限同样可以修上去。

    正常：1属性点=6血限，当前生命同步抬高；
    边界：存点后用自由动作 redeem_attribute_points 兑血限（血限按1点一档，不受偶数限制）；
    错误：【不朽之躯】在场时拒绝兑血限，而不是静默吞掉属性点。
    """
    engine = _engine(tmp_path)
    player = engine.state.player
    bl_before, hp_before = player.blood_limit, player.current_hp

    trained = engine.execute_action("pre_battle_action", {
        "sub_action": "修行", "tier": 3, "allocations": {"blood_points": 3}})
    assert trained["success"], trained
    assert trained["result"]["gained"] == {"blood": 18, "speed": 0, "mana": 0}
    assert player.blood_limit == bl_before + 18
    assert player.current_hp == hp_before + 18

    # 边界：奇数点也能兑血限（1点一档）；速限同样是 1 点一档，只有法限须偶数
    engine.state.attribute_points += 1
    redeemed = engine.execute_action("redeem_attribute_points",
                                     {"allocations": {"blood_points": 1}})
    assert redeemed["success"], redeemed
    assert player.blood_limit == bl_before + 24

    # 错误：不朽之躯使血限无法增加，兑血限必须被拒绝
    from engine.models import Relic
    engine.state.relics.append(Relic(name="不朽之躯", effect="[血限]无法增加"))
    engine.state.attribute_points += 2
    blocked = engine.execute_action("redeem_attribute_points",
                                    {"allocations": {"blood_points": 2}})
    assert blocked["success"] is False
    assert "不朽之躯" in blocked["error"]
    assert engine.state.attribute_points == 2, "被拒绝时属性点不得被扣掉"
