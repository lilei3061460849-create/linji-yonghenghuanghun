"""用户裁定：定型按正文；坏死5X；勾魂不再回始扣法力。"""
from pathlib import Path
from engine.daowen import DaoWenEngine


def test_dingxing_uses_current_main_rule():
    result = DaoWenEngine.resolve("定型", 3)
    assert result["cost"] == 6 and result["duration"] == 3
    assert "无法被改变" in result["effect"]
    assert "无法增加" not in Path("副本/扭曲都市.md").read_text()


def test_necrosis_is_five_x():
    for x in (1, 2, 7):
        result = DaoWenEngine.resolve("坏死", x)
        assert result["cost"] == 5 * x and result["duration"] == x
    assert "【坏死】X：支付：法力5X" in Path("规则正文.md").read_text()


def test_old_gouhun_drain_description_is_removed():
    text = Path("副本/乱葬岗.md").read_text()
    assert "失去2X点当前法力" not in text
    assert "勾魂X：支付：法力X。使[目标]无法获得[法力]，持续X。" in text


def test_drafts_are_not_runtime_implementations():
    import pytest
    from engine.gamedata import UNIMPLEMENTED_DAOWEN, learnable_daowen_pool
    DaoWenEngine.register_all()
    for name in ("流动", "遗忘", "出土", "篡改"):
        assert name not in DaoWenEngine._registry
        with pytest.raises(ValueError, match="未知道纹"):
            DaoWenEngine.resolve(name, 1)
    # A caller accidentally including draft names in 'known' must not enable learning.
    pool = learnable_daowen_pool(owned={"大梦"}, region="沉沦海",
                                known=set(DaoWenEngine._registry) | set(UNIMPLEMENTED_DAOWEN))
    assert not set(UNIMPLEMENTED_DAOWEN).intersection(pool)


def test_retired_anger_is_not_granted_as_transformed_daowen():
    from engine.gamedata import MONSTER_TRANSFORM_DAOWEN
    DaoWenEngine.register_all()
    assert set(DaoWenEngine.TRANSFORMED_DAOWEN) == MONSTER_TRANSFORM_DAOWEN
    assert "愤怒" not in DaoWenEngine.TRANSFORMED_DAOWEN
    assert "愤怒" not in DaoWenEngine._registry
    assert "### 怒火针" in Path("物品索引.md").read_text()
