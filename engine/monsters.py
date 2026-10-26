"""
怪物池解析与出怪(战始抽怪)系统
从全副本索引指向的独立副本文档中解析每副本12只怪物的固定面板，供[战始]随机抽取。
"""
from __future__ import annotations
import re
from pathlib import Path
from typing import Optional

from .dungeons import load_dungeon_documents
# 每只怪物出厂自带的遗物【痛苦的偏爱】——常量与授予都在 engine/models.py
# （授予点在 Entity.__post_init__，覆盖所有构造路径），此处仅再导出便于就近引用。
from .models import MONSTER_MANA_RELIC


# 单个道纹词条：名字 2~4 个汉字 + **可选**的数字 X。
# 「再生4」→ 固定 X=4；「再生」→ X 待发动时自选（x=None）。
_DAOWEN_TOKEN = re.compile(r'^([一-鿿]{2,4})(\d+)?$')


def _parse_daowen_field(dw_str: str) -> dict[str, int | None]:
    """解析面板的道纹段，**同时兼容带 X 与不带 X 两种写法**。

    2026-09-16 用户令：道纹 X 不再写死在面板上，改由怪物 AI 在发动时自选，
    上限只受[法限]或代价限制。

    返回值 None 表示该道纹面板没写 X、需由发动方自选；整数表示面板写死的固定 X。
    保留固定 X 分支是为了让面板迁移可以**逐本进行**——改到一半时新旧写法混用
    也不会读不出道纹（旧写法直接解析成空字典会让怪物变白板，属于改坏）。
    """
    out: dict[str, int | None] = {}
    for token in re.split(r'[，,、]', dw_str or ""):
        token = token.strip()
        if not token:
            continue
        mt = _DAOWEN_TOKEN.match(token)
        if not mt:
            continue
        name, digits = mt.group(1), mt.group(2)
        out[name] = int(digits) if digits else None
    return out


def parse_monster_pool(index_path: str | Path) -> dict:
    """从索引登记的副本文档解析怪物池。
    返回 {region: [{"name","attack_count","attack_power","blood_limit","dao_wen"}, ...]}"""
    documents = load_dungeon_documents(index_path)
    # 2026-09-16 用户令：怪物/微光者面板与轮回者同口径，写作「名字（[血限]/[法限]/[速限]，道纹…）」。
    # 旧格式「攻次×攻力/血限」废止——[攻击次数]=[当前速度]、[攻击力]=[当前法力] 对全体生效后，
    # 攻次/攻力已不是独立面板，再单独印一遍属于冗余且会与上限打架。
    pat = re.compile(r'^([\u4e00-\u9fff\w·]+)[（(](\d+)/(\d+)/(\d+)(?:[，,]([^)）\n]*))?[）)]')
    pools: dict[str, list[dict]] = {}
    for region, document in documents.items():
        lines = document.split("\n")
        header = region + "怪物池"
        hidx = next((i for i, l in enumerate(lines) if l.startswith(header)), None)
        if hidx is None:
            pools[region] = []
            continue
        monsters = []
        for j in range(hidx + 1, len(lines)):
            m = pat.match(lines[j].strip())
            if m and len(monsters) < 12:
                name = m.group(1).strip()
                # 新口径：血限 / 法限 / 速限（与轮回者「[血限]/[法限]/[速限]」同序）
                hp, mana_limit, speed_limit = int(m.group(2)), int(m.group(3)), int(m.group(4))
                dw_str = m.group(5) or ""
                dao_wen = _parse_daowen_field(dw_str)
                monsters.append({
                    "name": name, "blood_limit": hp,
                    "mana_limit": mana_limit, "speed_limit": speed_limit,
                    # 兼容旧键：攻次=速限、攻力=法限（统一换算后二者不再是独立面板）
                    "attack_count": speed_limit, "attack_power": mana_limit,
                    "dao_wen": dao_wen, "region": region,
                })
                if len(monsters) >= 12:
                    break
            elif monsters and not m:
                break
        pools[region] = monsters
    return pools


# 出怪配方（2026-09-28 用户令）的三个常量：N 的上界、T_i 的上界、一阶的场数偏置。
# 正文口径见 推演规范.md「[战始]：抽取出怪」与 AI_EXPERIENCE.md「当前有效的工程约束」。
MONSTER_POOL_LIMIT = 12      # 每副本怪物池 12 种，也是配方 N=随机(1,12) 的上界
SPAWN_WAVE_GAP_LIMIT = 5     # T_i=随机(1,5)：上一波之后最多等待 5 回合
SPAWN_TIER1_BATTLE_BIAS = 3  # 一阶把旧「战斗场数-3」保留为 N 的上界（见 compute_draw_cap）
# 一阶上界的下界。2026-10-12 用户令：出怪不再固定数量，前四场「恒为1只」的确定段
# 去掉，改为 N=随机(1,2)；第 6 场起的上界梯度（3/4/5/…）保持不变。
SPAWN_TIER1_MIN_CAP = 2

# 阶级推进总开关（2026-09-28 用户令）：
# 机制代码完整保留，但默认关闭——玩家通过一阶死斗不会自动解锁二阶/五阶→无尽。
# 未来五阶副本【启示录】正文与怪物池全部接入后，再把此开关拨为 True 启用自动推进。
# 测试可通过 `engine.monsters.set_progression_enabled(True/False)` 临时覆盖。
PROGRESSION_ENABLED = False


def set_progression_enabled(value: bool) -> None:
    """测试用：临时开关阶级推进。生产代码不要调用。"""
    global PROGRESSION_ENABLED
    PROGRESSION_ENABLED = bool(value)


def compute_draw_cap(battle_number: int, tier: int = 1) -> int:
    """本场怪物总数 N 的随机上界（配方式出怪，2026-09-28 用户令，按阶级递增）。

    * 一阶：上界沿用旧「数量 = 战斗场数 - 3」，因此旧七场序列 `1/1/1/1/2/3/4`
      从"确定值"变成"上限"（N=随机(1,上限)）；上界再被怪物池规模 12 夹住，
      使第 8 场以后（无尽/自定义长局）不会突破「N=随机(1,12)」的正文口径。
      2026-10-12 用户令「修改出怪公式，不再像现在这样固定数量，而是全随机」，
      按用户选定的最小改动口径：**只去掉前四场「恒为1只」的确定段**——下界从 1 抬到 2，
      于是第 1~5 场一律 N=随机(1,2)，第 6 场起仍是 3/4/5/…（梯度与上界都不动）。
    * 二阶及以上：上界固定为怪物池规模 12，即 N=随机(1,12)。
    旧口径里"数量=战斗场数-3"是**精确值**；本函数只保留它作为**上界**的语义。
    """
    if int(tier) <= 1:
        # 下界 2：2026-10-12 用户令，前四场不再恒为 1 只（详见本函数 docstring）。
        return min(MONSTER_POOL_LIMIT,
                   max(SPAWN_TIER1_MIN_CAP, int(battle_number) - SPAWN_TIER1_BATTLE_BIAS))
    return MONSTER_POOL_LIMIT


def _roll_upper(dice, pool_name: str, upper: int, context: str) -> int:
    """在 1..upper 中随机一个整数。

    正式随机源唯一：走 `DiceEngine.auto_roll`（记录种子与序号，可复现），
    不引入模块级 random。`upper <= 1` 时不掷骰（随机数恒为 1，不占随机流）。
    """
    if upper <= 1:
        return 1
    roll = dice.auto_roll(pool_name, list(range(1, upper + 1)), context=context)
    return int(roll["selected"])


def roll_spawn_plan(dice, battle_number: int, tier: int = 1) -> dict:
    """出怪配方（2026-09-28 用户令，取代"数量=战斗场数-3"的确定值口径）。

    ```text
    N=随机(1,上界)，S=随机(1,N)，R_i=随机(1,N-S-ΣR_j)，T_i=随机(1,5)，直至 S+ΣR_i=N
    ```

    * N＝本场怪物总数（上界见 `compute_draw_cap`：一阶按场数且不低于2、二阶及以上为12）；
    * S＝[战始]（R1）首发数量；若仍有未出场怪物，则等待 T_i 回合后增援 R_i 只，
      R_i 不超过剩余未出场数量，循环到全部怪物入场（一波可进多只）。
    * 增援回合相对**上一波**累计（首发波记为 R1），因此第 i 波在
      `R1 + T_1 + … + T_i` 回合的[回始]进场。
    返回 `{"total": N, "first_count": S, "waves": [{"arrive_round": r, "count": c}, …],
    "queue_rounds": [每一只待增援怪物的进场回合]}`。
    """
    cap = compute_draw_cap(battle_number, tier)
    total = _roll_upper(dice, f"spawn_total_{battle_number}", cap,
                        f"出怪配方·本场怪物总数N(第{battle_number}场,上限{cap})")
    first_count = _roll_upper(dice, f"spawn_first_count_{battle_number}", total,
                              f"出怪配方·战始首发数量S(第{battle_number}场)")
    waves: list[dict] = []
    queue_rounds: list[int] = []
    remaining = total - first_count
    cursor_round = 1  # 首发波在 R1 进场，T_i 从它往后累计
    index = 0
    while remaining > 0:
        index += 1
        count = _roll_upper(dice, f"spawn_reinforce_count_{battle_number}_{index}", remaining,
                            f"出怪配方·第{index}波增援数量R_i(剩余{remaining})")
        gap = _roll_upper(dice, f"spawn_reinforce_wait_{battle_number}_{index}",
                          SPAWN_WAVE_GAP_LIMIT,
                          f"出怪配方·第{index}波增援等待回合T_i(1~{SPAWN_WAVE_GAP_LIMIT})")
        cursor_round += gap
        waves.append({"arrive_round": cursor_round, "count": count})
        queue_rounds.extend([cursor_round] * count)
        remaining -= count
    return {"total": total, "first_count": first_count,
            "waves": waves, "queue_rounds": queue_rounds, "cap": cap}


def scale_monster_def_for_endless(monster_def: dict, cycle: int) -> dict:
    """无尽模式强度递增（2026-09-28 用户令）：第 C 轮把怪物面板整体抬高。

    C<=1 时原样返回（无尽首轮＝基线，与实盘口径逐位相同）；C>=2 起
    [血限]×(100+25(C-1))% 向上取整、[法限]/[速限]各 +3(C-1)。
    只改**本场出怪用的定义副本**，不回写 `monster_pool`，因此跨场/跨轮回不会复利。
    """
    if int(cycle) <= 1:
        return monster_def
    factor = 100 + 25 * (int(cycle) - 1)
    bump = 3 * (int(cycle) - 1)
    scaled = dict(monster_def)
    scaled["blood_limit"] = -(-int(monster_def["blood_limit"]) * factor // 100)  # 向上取整
    scaled["mana_limit"] = int(monster_def["mana_limit"]) + bump
    scaled["speed_limit"] = int(monster_def["speed_limit"]) + bump
    # 兼容旧键：攻次=速限、攻力=法限（与 make_monster_entity 同口径）
    scaled["attack_count"] = scaled["speed_limit"]
    scaled["attack_power"] = scaled["mana_limit"]
    return scaled


def merge_monster_pools(monster_pools: dict) -> list[dict]:
    """无尽模式怪物池（2026-09-28 用户令：怪物池包含所有副本）。

    按 `副本索引.md` 登记的已实现副本顺序合并全部怪物池；同名怪物只保留先出现的一只，
    避免"同一种怪在合并池里被抽到的概率翻倍"。草案副本不进入运行时，因此不参与合并。
    """
    merged: list[dict] = []
    seen: set[str] = set()
    for pool in (monster_pools or {}).values():
        for monster_def in pool:
            name = monster_def.get("name", "")
            if name in seen:
                continue
            seen.add(name)
            merged.append(monster_def)
    return merged


# 当前偏爱池。效果定义与偏爱数量规则以 Issue #66 为准；每只怪物按当前副本阶位
# 从池中无放回随机抽取，使用引擎骰子记录每一次选择。
MONSTER_PREFERENCE_POOL = (
    "痛苦的偏爱", "贪婪的偏爱", "永恒的偏爱", "饥荒的偏爱", "时间的偏爱",
)


# 怪物随机遗物池（2026-10-25 用户令：X阶怪物 = X种随机偏爱 + X种随机遗物）。
# 只收「效果由逐实体判定结算」的遗物：这些判定读持有者自身的 relics，
# 换到怪物身上照样成立。依赖局外／事件／玩家专属入口的遗物（同魂笔、血契、
# 无所求、忘忧香、灾厄轮盘、蚀命刻尺、失速冥灯、衰变沙漏、蚀界残钟）
# 不在池内——怪物抽到也不会结算，等于空遗物。
# 【共生血囊】2026-10-25 用户令入池：它的判定锚在「创建者持有该遗物」，
# 怪物成为复制体创建者时同样成立。
MONSTER_RELIC_POOL = (
    "疯狂", "减速", "癫狂之脑", "千荆甲", "活血衣", "增生药剂", "承露盏",
    "守夜灯", "避风铃", "回锋刀", "窥隙法匣", "逐影胫骨", "逆血龙鳞",
    "噬伤血囊", "倍速齿轮", "寂灭面具", "沉重枷锁", "共生血囊",
)


def assign_monster_relics(entity, tier: int, dice) -> list[str]:
    """2026-10-25 用户令：X阶怪物额外获得 X 种随机遗物（与偏爱各自独立抽取）。

    随机流单独一条：种子取「引擎种子 + 怪物 runtime_id + 标签」，
    不再从共享骰子流里取数——否则每多抽一件遗物都会把后续出怪、
    事件与增援的随机结果整体推移，等于悄悄改掉所有既有随机契约。
    同种子同怪物重放结果一致。
    """
    import random as _random
    from .api import GameEngine
    from .models import Relic
    effects = dict(GameEngine.RELIC_DEFS)
    count = min(len(MONSTER_RELIC_POOL), max(0, int(tier)))
    remaining = [name for name in MONSTER_RELIC_POOL
                 if not any(relic.name == name for relic in entity.relics)]
    selected: list[str] = []
    # 种子用「引擎种子 + 阶位 + 怪物名」：runtime_id 是 uuid，拿它当种子会让
    # 同种子两次运行抽出不同遗物，直接破坏「同种子可复现」这条契约。
    rng = _random.Random(f"{getattr(dice, '_seed', 0)}:{int(tier)}:{entity.name}:monster_relic")
    for _ in range(count):
        if not remaining:
            break
        choice = remaining.pop(rng.randrange(len(remaining)))
        selected.append(choice)
        entity.relics.append(Relic(name=choice, effect=effects.get(choice, ""),
                                   tags=["怪物随机遗物"]))
    return selected


def assign_monster_preferences(entity, tier: int, dice) -> list[str]:
    """为怪物配置与副本阶位相同数量的不同偏爱。"""
    count = min(len(MONSTER_PREFERENCE_POOL), max(1, int(tier)))
    remaining = list(MONSTER_PREFERENCE_POOL)
    selected: list[str] = []
    for index in range(count):
        roll = dice.auto_roll(
            f"monster_preference_{entity.runtime_id}_{index + 1}",
            remaining,
            context=f"{entity.name}·第{index + 1}种固有偏爱（副本{tier}阶）",
        )
        choice = str(roll["selected"])
        selected.append(choice)
        remaining.remove(choice)
    entity.preferences = selected
    # 「痛苦的偏爱」沿用原有[回始]效果，但只在该偏爱被抽中时授予；
    # 不能再让一阶怪物固定拥有它、同时又额外抽取一项偏爱。
    from .models import MONSTER_MANA_RELIC, MONSTER_MANA_RELIC_EFFECT, Relic
    if "痛苦的偏爱" in selected:
        if not any(relic.name == MONSTER_MANA_RELIC for relic in entity.relics):
            entity.relics.append(Relic(
                name=MONSTER_MANA_RELIC, effect=MONSTER_MANA_RELIC_EFFECT,
                tags=["怪物偏爱"]))
    else:
        entity.relics = [relic for relic in entity.relics
                         if relic.name != MONSTER_MANA_RELIC]
    assign_monster_relics(entity, tier, dice)
    return selected


def make_monster_entity(monster_def: dict):
    """按怪物定义构造Entity（延迟导入避免循环依赖）"""
    from .models import Entity, DaoWen, DaoWenInstance
    # 2026-09-16 用户令：怪物与轮回者同口径，持有[速限]/[法限]；[攻击次数]=[当前速度]、[攻击力]=[当前法力]。
    # 战始当前值给满，[回始]由 combat.round_start 再次回满（怪物专属；微光者不回满）。
    _speed_limit = monster_def.get("speed_limit", monster_def.get("attack_count", 0))
    _mana_limit = monster_def.get("mana_limit", monster_def.get("attack_power", 0))
    m = Entity(name=monster_def["name"], entity_type="怪物",
               blood_limit=monster_def["blood_limit"], current_hp=monster_def["blood_limit"],
               attack_count=_speed_limit, attack_power=_mana_limit,
               speed_limit=_speed_limit, mana_limit=_mana_limit)
    m.current_speed = _speed_limit
    m.current_mana = _mana_limit
    # 遗物【痛苦的偏爱】由 Entity.__post_init__ 统一授予，此处不再重复挂载。
    from .daowen import normalize_daowen_name
    for dw_name, x in monster_def["dao_wen"].items():
        dw_name = normalize_daowen_name(dw_name)  # 旧面板【血债】读入即映射【连击】
        # x 为 None → 面板未写死 X，标记 x_free 由发动方自选（2026-09-16 用户令）；
        # x 为整数 → 旧面板的固定 X，原样保留。
        m.dao_wen[dw_name] = DaoWenInstance(
            dao_wen=DaoWen(name=dw_name, formula="", cost_type="", cost_formula="", effect_formula=""),
            x_value=(x if x is not None else 0), x_free=(x is None))
    from .gamedata import ORIGINAL_MONSTER_DAOWEN
    if any(name in ORIGINAL_MONSTER_DAOWEN for name in m.dao_wen):
        m._had_monster_daowen = True
    # 注意：不在此设 is_flying。飞行是状态，由消耗品（血翼／烬翼）的飞行选项结算。
    # （2026-09-15 用户令已删除"登场回合不出道纹"的白板限制）。
    return m
