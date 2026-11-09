"""生产运行时仍需共享的最小规则集合。

怪物面板、遗物与法术不在这里重复维护：它们分别从副本文档、物品正文和
死者之书读取，或由对应运行时注册表提供。
"""

REGION_EXCLUSIVE_DAOWEN = {
    # 2026-11-02 用户令·四面体大改：
    #   扭曲都市 += 【饥饿】【扭曲】；沉沦海 += 【涌潮】；荒疫古城 -= 【篡改】（移入通用）。
    #   遗物化占位（畸变/退化/缄默/冥气/加害/龙鳞/逆鳞/伤痕/裂变/背负）不再占闭环格。
    "扭曲都市": {"变形", "定型", "超频", "萎缩", "增殖", "坏死", "饥饿", "扭曲"},
    "罪孽都市": {"点金", "烁金", "假钞", "交易", "赦免", "贿赂", "豪夺", "典当"},
    # 2026-11-02 用户令：龙心谷四面体四节点全部实装（炽热/熔化/锻造 由草案升运行时）。
    # 2026-11-08 用户令：龙心谷/乱葬岗/荒疫古城各补第二四面体，四副本均满 8 纹。
    "龙心谷": {"锻造", "熔化", "炽热", "淬火", "百炼", "锈蚀", "重铸", "回炉"},
    "乱葬岗": {"分裂", "凋零", "勾魂", "招魂", "合体", "化尸", "立冢", "借尸"},
    "沉沦海": {"流动", "波及", "遗忘", "冰冻", "大梦", "沉溺", "回响", "涌潮"},
    # 2026-11-08：感染/重演/原初转正并新增病变，荒疫满 8 纹。【篡改】已移入通用。
    "荒疫古城": {"唯一", "净化", "出土", "催化", "感染", "重演", "原初", "病变"},
}

# 种族专属道纹：不计入任何副本专属学习池；怪物面板既有配置不受分类影响，普通残韵不能授予。
RACE_EXCLUSIVE_DAOWEN = {
    "鲛族": {"唤潮", "蜃景"},
}

# 血族专属道纹集合当前为空；原【消灾】已转换为消耗品【逆命筹】。
BLOODLINE_EXCLUSIVE_DAOWEN = frozenset()

ORIGINAL_MONSTER_DAOWEN = {"全力", "必中", "飞行"}  # 2026-10-29：飞行恢复为冷却X道纹

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
    "弱化", "强化", "全速", "压迫",
}

# 杀伐闭环的通用可获得道纹（固执、贯穿与迁往扭曲都市的增殖原位留空）；
# 开局【发现】初始道纹只从这里抽。
# 2026-11-01 用户令：【透支】移出闭环、改为任意角色出生自带，不再从发现池抽。
# （2026-08-21：冲击改名波及；删除缓慢、慈悲、切割）
SHAFA_LOOP_DAOWEN = (
    # 通用四面体一 + 通用四面体二 的全部节点（开局【发现】只从这 8 种里抽）。
    # 2026-11-07 曾随互换由【封印】换为【篡改】；2026-11-08 用户令再次调换，
    # 四面体二含【封印】，发现池随之换回【封印】。
    # 2026-11-09 用户令：【连击】的通用四面体一格由【脆弱】替换。
    "杀伐", "再生", "庇护", "脆弱",
    "操纵", "束缚", "迟滞", "封印",
)

# 原始 ∪ 转化。学习门禁与残韵归属用此并集。救赎只看原始怪物道纹（全力／必中），不含转化。
MONSTER_DAOWEN = ORIGINAL_MONSTER_DAOWEN | MONSTER_TRANSFORM_DAOWEN

# 所属副本尚未接入运行时，但计算已实现；局外学习仍按专属道纹拒绝。
UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN = {
    "不死": "永夜庭",
}

# 已有规则草案节点，但尚未注册运行时效果；不可通过学习或残韵获取。
# 2026-10-30用户选择：炽热/熔化/锻造只进入龙心谷正文，支付待定，保持未实装。
UNIMPLEMENTED_DAOWEN = {
    # 2026-11-02：龙心谷【炽热】【熔化】【锻造】已实装，草案清单清空。
}

# 血契、共罪、不死已有战斗结算，但永夜庭尚未作为可学习区域开放。
# 2026-11-02 用户令：永夜庭四面体 = 血契／共罪／不死／怜悯，四纹同按永夜庭专属门禁
# 拒绝局外学习（【怜悯】由通用移入永夜庭）。
# 2026-11-08 用户令：永夜庭补第二四面体（索取/逆血/永生/初拥），同按永夜庭门禁。
UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN["血契"] = "永夜庭"
UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN["共罪"] = "永夜庭"
UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN["怜悯"] = "永夜庭"
UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN["索取"] = "永夜庭"
UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN["逆血"] = "永夜庭"
UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN["永生"] = "永夜庭"
UNIMPLEMENTED_REGION_EXCLUSIVE_DAOWEN["初拥"] = "永夜庭"

# 初拥之夜选项5授予的血族专属道纹；不进入局外学习或残韵获取池。
FIRST_EMBRACE_DAOWEN = frozenset({"真理"})


def learnability_error(name, *, owned, region, known) -> str | None:
    """局外【学习】口径的单名校验（2026-10-13 从 api._pre_battle_xuexi 原样抽出）。

    返回 None = 可学；否则返回拒绝文案（逐字沿用旧实现）。
    【典当】的发现池与【学习】同口径，共用本函数——改学习门禁时两处一起变。
    """
    # 2026-11-09 用户令：衍生道纹只能由母本经【衍生发现】取得，不走学习/典当/发现池。
    from .derivatives import is_derivative
    if is_derivative(name):
        return f"【{name}】是衍生道纹，只能由母本支付【转换】+血限2+碎片3经衍生发现获得"
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
