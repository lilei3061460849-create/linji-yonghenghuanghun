"""【增殖】迁为扭曲都市专属道纹的归属、获取与面板回归。"""
from pathlib import Path

from engine.daowen import (
    DaoWenEngine, ResonanceEngine,
)
from engine.gamedata import (
    REGION_EXCLUSIVE_DAOWEN,
    SHAFA_LOOP_DAOWEN,
    learnability_error,
    learnable_daowen_pool,
)

ROOT = Path(__file__).resolve().parents[1]


def test_zengzhi_leaves_generic_core_and_enters_only_twisted_region_pool():
    assert "增殖" not in SHAFA_LOOP_DAOWEN
    assert "增殖" in REGION_EXCLUSIVE_DAOWEN["扭曲都市"]
    assert [
        region for region, pool in REGION_EXCLUSIVE_DAOWEN.items() if "增殖" in pool
    ] == ["扭曲都市"]

    # 2026-11-02 四面体大改：增殖不再住在通用环里，旧占位槽也随之作废；
    # 它现在只在扭曲都市·四面体二里与 饥饿／扭曲／坏死 相连。
    generic_nodes = ResonanceEngine.nodes_of_region("通用")
    assert "增殖" not in generic_nodes
    assert not any("增殖" in (source, target)
                   for source, _, target in ResonanceEngine.edges_of_region("通用"))

    twisted = ResonanceEngine.tetrahedra_of_region("扭曲都市")["扭曲都市·四面体二"]
    assert ("增殖", "反转", "饥饿") in twisted
    assert ("增殖", "转换", "扭曲") in twisted
    assert ("增殖", "曲解", "坏死") in twisted


def test_zengzhi_is_not_in_generic_learning_pool_outside_twisted_city():
    known = set(DaoWenEngine.list_all())
    for region in ("", "罪孽都市", "龙心谷", "乱葬岗", "沉沦海"):
        assert "增殖" not in learnable_daowen_pool(
            owned=set(), region=region, known=known)
        error = learnability_error("增殖", owned=set(), region=region, known=known)
        assert error == "【增殖】是扭曲都市专属道纹，当前副本无法习得"


def test_twisted_learning_still_requires_an_existing_region_exclusive():
    known = set(DaoWenEngine.list_all())
    gated = learnability_error(
        "增殖", owned=set(), region="扭曲都市", known=known)
    assert gated == "【增殖】须先经残韵获得本副本一种专属道纹后才能学习"
    assert learnability_error(
        "增殖", owned={"超频"}, region="扭曲都市", known=known) is None
    assert "增殖" in learnable_daowen_pool(
        owned={"超频"}, region="扭曲都市", known=known)


def test_zengzhi_resonance_is_bidirectional_between_new_neighbors():
    # 四面体二内：增殖 反转⇄饥饿、转换⇄扭曲、曲解⇄坏死，三条边都双向可走。
    assert ResonanceEngine.find_transformation("增殖", "反转") == "饥饿"
    assert ResonanceEngine.find_transformation("增殖", "转换") == "扭曲"
    assert ResonanceEngine.find_transformation("增殖", "曲解") == "坏死"
    assert ResonanceEngine.find_transformation("饥饿", "反转") == "增殖"
    assert ResonanceEngine.find_transformation("扭曲", "转换") == "增殖"
    assert ResonanceEngine.find_transformation("坏死", "曲解") == "增殖"
    forward = ResonanceEngine.apply_resonance(
        "增殖", "反转", caster_has_daowen=True, target_has_daowen=True)
    reverse = ResonanceEngine.apply_resonance(
        "饥饿", "反转", caster_has_daowen=True, target_has_daowen=True)
    assert forward["success"] and forward["target"] == "饥饿"
    assert reverse["success"] and reverse["target"] == "增殖"


def test_other_dungeon_monster_panels_no_longer_carry_zengzhi():
    expected_removals = {
        "乱葬岗.md": ("蛆冢",),
        "荒疫古城.md": ("拓城巨像", "万灵残躯"),
        "巴别塔.md": ("石阶守", "默祷人"),
    }
    for filename, monster_names in expected_removals.items():
        lines = (ROOT / "副本" / filename).read_text(encoding="utf-8").splitlines()
        for monster_name in monster_names:
            panel = next(line for line in lines if line.startswith(f"{monster_name}（"))
            assert "增殖" not in panel, panel

    twisted_lines = (ROOT / "副本" / "扭曲都市.md").read_text(encoding="utf-8").splitlines()
    sack = next(line for line in twisted_lines if line.startswith("血肉巨囊（"))
    assert "增殖" in sack
