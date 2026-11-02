"""回归：沉沦海道纹去重与【波及】迁移。"""
from pathlib import Path
import re

from engine.daowen import ResonanceEngine, RELOCATED_DAOWEN_SLOTS
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

    generic = ResonanceEngine.edges_of_region("通用")
    assert not any("波及" in (src, dst) for src, _, dst in generic)
    assert not any("增殖" in (src, dst) for src, _, dst in generic)

    tetra = ResonanceEngine.tetrahedra_of_region("沉沦海")
    assert set(tetra) == {"沉沦海·四面体一", "沉沦海·四面体二"}
    assert set(tetra["沉沦海·四面体一"]) == {
        ("回响", "反转", "遗忘"), ("流动", "反转", "波及"),
        ("回响", "转换", "波及"), ("遗忘", "转换", "流动"),
        ("回响", "曲解", "流动"), ("遗忘", "曲解", "波及"),
    }
    assert set(tetra["沉沦海·四面体二"]) == {
        ("大梦", "反转", "沉溺"), ("冰冻", "反转", "涌潮"),
        ("大梦", "转换", "涌潮"), ("沉溺", "转换", "冰冻"),
        ("大梦", "曲解", "冰冻"), ("沉溺", "曲解", "涌潮"),
    }


def test_deleted_sunken_sea_daowen_and_panel_tags_are_gone():
    dungeon = (ROOT / "副本" / "沉沦海.md").read_text(encoding="utf-8")
    index = (ROOT / "全道纹索引.md").read_text(encoding="utf-8")
    assert "映照" not in dungeon
    assert "扩散" not in dungeon
    assert "映照" not in index
    assert "扩散" not in index
    assert "道纹数量8" in dungeon  # 2026-11-02 新增【涌潮】
    indexed_count = len(re.findall(r"^### [\u4e00-\u9fff]{2}$", index, re.M))
    assert f"本索引收录 {indexed_count} 条道纹" in index
    assert "【流动】【遗忘】须按X选择持续状态" in index
    assert "通用核心（四面体一·二）8" in index  # 通用四面体一·二共 8 节点
    assert "扭曲都市专属（8）" in index  # 补【饥饿】【扭曲】
    assert "沉沦海专属（8）" in index  # 补【涌潮】
    assert "鲛族专属（2）" in index
    assert "褪鳞蟹（264/18/3，唤潮，庇护，遗物【减速】）" in dungeon
    assert "珊瑚囚徒（276/20/3，唤潮，蜃景，连击，庇护）" in dungeon
