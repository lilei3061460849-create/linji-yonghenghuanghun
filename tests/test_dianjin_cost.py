from tests.test_exclusive_zui_twisted import _setup, _grant, _add_monster


def test_dianjin_pays_amnesia_not_mana_and_gains_shards():
    e = _setup(mana=20)
    _grant(e, ["点金", "杀伐", "庇护"])
    _add_monster(e, shards=0)
    p = e.state.player
    mana = p.current_mana
    shards = e.state.shards
    # 失忆X必须显式给出要永久销毁的X种道纹（严禁按顺序代选）。
    r = e.execute_action("use_daowen", {
        "daowen_name": "点金", "x": 2, "forget_daowen": ["杀伐", "庇护"]})
    assert r["success"], r
    assert p.current_mana == mana
    assert e.state.shards == shards + 160
    assert len(p.dao_wen) == 1
    assert set(p.dao_wen) == {"点金"}


def test_dianjin_requires_explicit_forget_selection():
    """未提交 forget_daowen / 数量不符 / 道纹不存在 → 拒绝，且一分代价都不扣。"""
    e = _setup(mana=20)
    _grant(e, ["点金", "杀伐", "庇护"])
    _add_monster(e, shards=0)
    p = e.state.player
    before_shards = e.state.shards
    before_mana = p.current_mana
    before_daowen = list(p.dao_wen)

    missing = e.execute_action("use_daowen", {"daowen_name": "点金", "x": 2})
    assert not missing["success"] and "forget_daowen" in missing["error"], missing

    short = e.execute_action("use_daowen", {
        "daowen_name": "点金", "x": 2, "forget_daowen": ["杀伐"]})
    assert not short["success"] and "恰好" in short["error"], short

    unknown = e.execute_action("use_daowen", {
        "daowen_name": "点金", "x": 2, "forget_daowen": ["杀伐", "不存在"]})
    assert not unknown["success"] and "未持有" in unknown["error"], unknown

    for result in (missing, short, unknown):
        assert e.state.shards == before_shards and p.current_mana == before_mana
        assert list(p.dao_wen) == before_daowen
        assert p.actions_used_this_round == 0


def test_amnesia_named_cost_cannot_exceed_owned_daowen():
    e = _setup(mana=20)
    _grant(e, ["点金"])
    before = e.state.shards
    x = len(e.state.player.dao_wen) + 1
    r = e.execute_action("use_daowen", {"daowen_name": "点金", "x": x})
    assert not r["success"] and f"失忆{x}无法支付" in r["error"]
    assert e.state.shards == before and "点金" in e.state.player.dao_wen
