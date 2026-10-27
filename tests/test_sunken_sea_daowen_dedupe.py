"""回归：沉沦海道纹去重与【波及】迁移。"""
from pathlib import Path

from engine.daowen import ResonanceEngine
from engine.gamedata import REGION_EXCLUSIVE_DAOWEN, RACE_EXCLUSIVE_DAOWEN, SHAFA_LOOP_DAOWEN

ROOT = Path(__file__).resolve().parents[1]


def test_wave_moves_from_generic_loop_to_sunken_sea():
    assert "波及" not in SHAFA_LOOP_DAOWEN
    assert "波及" in REGION_EXCLUSIVE_DAOWEN["沉沦海"]
    assert "映照" not in REGION_EXCLUSIVE_DAOWEN["沉沦海"]
    assert "扩散" not in REGION_EXCLUSIVE_DAOWEN["沉沦海"]
    assert "唤潮" not in REGION_EXCLUSIVE_DAOWEN["沉沦海"]
    assert "蜃景" not in REGION_EXCLUSIVE_DAOWEN["沉沦海"]
    assert RACE_EXCLUSIVE_DAOWEN["鲛族"] == {"唤潮", "蜃景"}

    loops = ResonanceEngine.CLOSED_LOOPS
    shafa = loops["杀伐闭环"]
    assert ("连击", "转换", "增殖") in shafa
    assert not any("波及" in (src, dst) for src, _, dst in shafa)

    sea = loops["沉沦海闭环"]
    assert sea == [
        ("流动", "转换", "波及"),
        ("波及", "反转", "遗忘"),
        ("遗忘", "曲解", "唤潮"),
        ("唤潮", "反转", "蜃景"),
        ("蜃景", "曲解", "凝滞"),
        ("凝滞", "曲解", "回响"),
        ("回响", "转换", "流动"),
    ]


def test_deleted_sunken_sea_daowen_and_panel_tags_are_gone():
    dungeon = (ROOT / "副本" / "沉沦海.md").read_text(encoding="utf-8")
    index = (ROOT / "全道纹索引.md").read_text(encoding="utf-8")
    assert "映照" not in dungeon
    assert "扩散" not in dungeon
    assert "映照" not in index
    assert "扩散" not in index
    assert "道纹数量7" in dungeon
    assert "全部道纹（46种）" in index
    assert "杀伐闭环（通用核心）10" in index
    assert "沉沦海专属（5）" in index
    assert "鲛族专属（2）" in index
    assert "褪鳞蟹（264/18/3，唤潮，庇护，固执，减速）" in dungeon
    assert "珊瑚囚徒（276/20/3，唤潮，蜃景，固执，连击，庇护）" in dungeon
