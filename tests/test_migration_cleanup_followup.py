"""用户要求的迁移清理：不删能力、不把成本当效果。"""
from pathlib import Path
import pytest
from engine.monsters import make_monster_entity, parse_monster_pool
from engine.daowen import ResonanceEngine
from engine.custom_resources import validate_spec

ROOT = Path(__file__).resolve().parents[1]


def test_panel_relics_are_not_daowen_and_keep_effects(tmp_path):
    # 草案不能进入生产怪物池；用独立测试索引验证其面板解析，不改变实现状态。
    index = tmp_path / "副本索引.md"
    index.write_text(f"| 沉沦海 | 二阶 | 100 | 5 | [查看副本]({ROOT / '副本/沉沦海.md'}) |\n")
    pools = parse_monster_pool(index)
    relic_panels = [d for pool in pools.values() for d in pool if d.get("relics")]
    assert relic_panels
    for definition in relic_panels:
        assert not {"疯狂", "减速"} & definition["dao_wen"].keys()
        entity = make_monster_entity(definition)
        assert set(definition["relics"]) <= {r.name for r in entity.relics}


def test_legacy_panel_retypes_relic_without_losing_ability():
    m = make_monster_entity({"name": "旧面板", "blood_limit": 100,
                             "mana_limit": 5, "speed_limit": 2,
                             "dao_wen": {"疯狂": 3, "减速": 2, "杀伐": None}})
    assert set(m.dao_wen) == {"杀伐"}
    assert {"疯狂", "减速"} <= {r.name for r in m.relics}


def test_merfolk_rewards_are_independent_options_not_resonance_nodes():
    loop = ResonanceEngine.CLOSED_LOOPS["沉沦海闭环"]
    assert not any(n in {"唤潮", "蜃景"} for src, _, dst in loop for n in (src, dst))
    text = (ROOT / "副本/沉沦海.md").read_text()
    assert "随后5选1" in text
    assert "4.获得鲛族专属道纹【唤潮】" in text
    assert "5.获得鲛族专属道纹【蜃景】" in text


def test_cost_only_x_is_not_effect_scaling():
    with pytest.raises(ValueError, match="X"):
        validate_spec({"name": "伪缩", "category": "道纹", "uniqueness": "测试反例",
                       "cost": {"type": "法力", "amount": "X"},
                       "effects": [{"verb": "damage", "amount": 3}]})
