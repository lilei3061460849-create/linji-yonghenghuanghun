"""生产运行时仍需共享的最小规则集合。

怪物面板、遗物与法术不在这里重复维护：它们分别从副本文档、物品正文和
死者之书读取，或由对应运行时注册表提供。
"""

REGION_EXCLUSIVE_DAOWEN = {
    "扭曲都市": {"变形", "定型", "超频", "坏死"},  # 【畸变】【退化】已遗物化，闭环留「？？」
    # 2026-10-13 用户令：删【逼债】，【典当】顶替其闭环格。
    "罪孽都市": {"点金", "典当", "豪夺", "清算", "抢劫", "假钞", "勒索"},  # 2026-10-14 +勒索
    "龙心谷": set(),  # 【加害】已转换为消耗品【灼罪钉】，闭环旧节点留「？？」。  # 【逆鳞】【伤痕】已遗物化，闭环留「？？」
    # 乱葬岗删纹与面板空位保持登记；稳定后再补位。
    "乱葬岗": {"分裂", "瓦解", "勾魂", "招魂"},  # 【缄默】【冥气】已遗物化，闭环留「？？」
    # 2026-10-27：沉沦海专属池已登记；【流动】【遗忘】【回响】尚未接入运行时。
    "沉沦海": {"流动", "波及", "遗忘", "沉溺", "回响"},
}

# 种族专属道纹：不计入任何副本专属学习池；怪物面板既有配置不受分类影响，普通残韵不能授予。
RACE_EXCLUSIVE_DAOWEN = {
    "鲛族": {"唤潮", "蜃景"},
}

# 血族专属道纹集合当前为空；原【消灾】已转换为消耗品【逆命筹】。
BLOODLINE_EXCLUSIVE_DAOWEN = frozenset()

ORIGINAL_MONSTER_DAOWEN = {"全力", "必中"}  # 2026-10-25 用户令：【疯狂】【减速】遗物化；【飞行】是状态

# 副本阶级（道纹递增机制已于 2026-09-16 用户令废止；阶级仍用于出怪公式上界等）
REGION_TIERS = {
    "罪孽都市": 1, "扭曲都市": 1, "龙心谷": 1,
    "乱葬岗": 2, "永夜庭": 2, "沉沦海": 2,
    "荒疫古城": 3,
    "巴别塔": 4,
}

MONSTER_TRANSFORM_DAOWEN = {
    # 2026-10-03 删【兴奋】【滑翔】【坠落】；2026-10-08 删一条无残韵路径的孤儿转化道纹；
    # 2026-10-11 删【无神】【借力】【衰败】；2026-10-27【愤怒】转为消耗品【怒火针】
    "弱化", "全速",
}

# 杀伐闭环 8 个可获得道纹（固执、贯穿原位留空）；开局【发现】初始道纹只从这里抽。
# （2026-08-21：冲击改名波及；删除缓慢、慈悲、切割）
SHAFA_LOOP_DAOWEN = (
    "杀伐", "再生", "庇护", "连击",
    "增殖", "透支", "束缚", "封印",
)

# 原始 ∪ 转化。学习门禁与残韵归属用此并集。救赎只看原始怪物道纹（全力／必中），不含转化。
MONSTER_DAOWEN = ORIGINAL_MONSTER_DAOWEN | MONSTER_TRANSFORM_DAOWEN

# 所属副本尚未接入运行时，但计算已实现；局外学习仍按专属道纹拒绝。
UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN = {
    "净化": "荒疫古城",
}

# 已有闭环/规则草案节点，但尚未注册运行时效果；不可通过学习或残韵获取。
UNIMPLEMENTED_DAOWEN = {
    "流动": "沉沦海",
    "遗忘": "沉沦海",
    "回响": "沉沦海",
}

# 初拥之夜选项5授予的血族专属道纹；不进入局外学习或残韵获取池。
FIRST_EMBRACE_DAOWEN = frozenset({"真理"})


def learnability_error(name, *, owned, region, known) -> str | None:
    """局外【学习】口径的单名校验（2026-10-13 从 api._pre_battle_xuexi 原样抽出）。

    返回 None = 可学；否则返回拒绝文案（逐字沿用旧实现）。
    【典当】的发现池与【学习】同口径，共用本函数——改学习门禁时两处一起变。
    """
    if name in UNIMPLEMENTED_DAOWEN:
        owner = UNIMPLEMENTED_DAOWEN[name]
        return f"【{name}】是{owner}专属道纹，尚未接入运行时，当前不能学习或通过残韵获得"
    if name not in known:
        return f"未知道纹: {name}"
    if name in owned:
        return f"已经掌握道纹: {name}"
    if name in FIRST_EMBRACE_DAOWEN:
        return f"【{name}】是初拥之夜专属道纹，只能通过初拥之夜获得"
    race_owner = next((race for race, pool in RACE_EXCLUSIVE_DAOWEN.items() if name in pool), None)
    if race_owner is not None:
        return f"【{name}】是{race_owner}专属道纹，只能通过{race_owner}专属机制获得"
    if name in BLOODLINE_EXCLUSIVE_DAOWEN:
        return f"【{name}】是血族专属道纹，只能通过血族专属机制获得"
    owner = next((r for r, pool in REGION_EXCLUSIVE_DAOWEN.items() if name in pool), None)
    if owner is not None:
        if owner != region:
            return f"【{name}】是{owner}专属道纹，当前副本无法习得"
        if not (REGION_EXCLUSIVE_DAOWEN[owner] & set(owned)):
            return f"【{name}】须先经残韵获得本副本一种专属道纹后才能学习"
    if name in MONSTER_TRANSFORM_DAOWEN:
        return f"【{name}】是怪物转化道纹，只能由自身已有道纹经残韵获得"
    # 2026-10-07 用户令：原始怪物道纹不再是人类禁区（残韵双向 + 可永久持有），
    # 局外【学习】不再以「人类无法承受」为由拒绝。
    unimpl_owner = UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN.get(name)
    if unimpl_owner is not None:
        return f"【{name}】是{unimpl_owner}专属道纹，当前副本无法习得"
    return None


def learnable_daowen_pool(*, owned, region, known) -> list:
    """学习口径下可获得的道纹名（排序稳定；调用方抽样前不再过滤）。"""
    return sorted(n for n in known if learnability_error(n, owned=owned, region=region, known=known) is None)
