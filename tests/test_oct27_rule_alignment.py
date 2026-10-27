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
