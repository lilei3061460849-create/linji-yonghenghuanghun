"""“造物”术语与旧存档兼容回归。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.api import GameEngine
from engine.enums import EntityType
from engine.models import DaoWen, DaoWenInstance, Entity


def test_legacy_entity_type_migrates_during_save_load_without_rewriting_build(tmp_path):
    engine = GameEngine(db_path=str(tmp_path / "source.db"), save_dir=str(tmp_path),
                        rng_seed=101, autosave=False)
    engine.state.player = Entity("轮回者", "轮回者", blood_limit=100, current_hp=100)
    legacy = Entity("旧档造物", "造物", blood_limit=20, current_hp=7,
                    mana_limit=9, current_mana=4,
                    speed_limit=3, current_speed=2,
                    attack_count=3, attack_power=9)
    legacy.dao_wen["庇护"] = DaoWenInstance(
        DaoWen(name="庇护", formula="", cost_type="消耗", cost_formula="X",
               effect_formula=""), x_value=2)
    # 模拟新版迁移前写出的 pickle：构造后故意放回旧实体类型，验证真实读档边界。
    legacy.entity_type = "临时朋友"
    engine.state.temp_friends = [legacy]
    assert engine.save_game("legacy-creation")["success"] is True

    restored = GameEngine(db_path=str(tmp_path / "restored.db"), save_dir=str(tmp_path),
                          rng_seed=102, autosave=False)
    result = restored.load_game("legacy-creation")
    assert result["success"] is True, result
    loaded = restored.state.temp_friends[0]
    assert loaded.entity_type == "造物"
    assert loaded.current_hp == 7 and loaded.blood_limit == 20
    assert loaded.current_mana == 4 and loaded.mana_limit == 9
    assert loaded.current_speed == 2 and loaded.speed_limit == 3
    assert set(loaded.dao_wen) == {"庇护"}
    assert loaded.dao_wen["庇护"].x_value == 2


def test_legacy_enum_value_resolves_to_creation():
    assert EntityType("临时朋友") is EntityType.CREATION
    assert EntityType.CREATION.value == "造物"
