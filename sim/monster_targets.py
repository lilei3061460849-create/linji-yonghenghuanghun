#!/usr/bin/env python3
"""怪物道纹目标选择的共享辅助（sim 侧"最优策略"解析器专用，非引擎规则）。
`pick_wave_dodge_targets` 的实现已迁到 engine/ai_rules.py（生产侧也用），
本模块再导出。`pick_monster_daowen_x` 等预演评分只服务模拟，仍留在 sim。
背景（2026-08-21 修复）：此前的解析器一律取 target_options[0]（=玩家），
导致 再生/增殖 等增益道纹被怪物打给玩家，污染模拟数据（实测：眼树再生奶玩家、
血肉巨囊增殖给玩家加血限）。规则本身允许怪物错误选择目标；但"最优策略"模拟器
必须按目标类型理性选择：
  SELF    → 自身（增益/回复/自强化）
  HOSTILE → 玩家（输出/控制/削弱）
  波及等 per_target 道纹走 dodge_targets，不走本函数。
本模块是叶子模块（不 import 其它 sim 模块），供各解析器/测试直接引用，
避免 build_learner ↔ duel_common 的循环导入。
"""
from typing import Optional
MONSTER_SELF_DAOWEN = {
    "庇护", "再生", "固执", "疯狂", "全力", "龙鳞",
    "狂暴", "必中", "超频", "急速", "加速", "飞行", "自食", "招魂", "变形",
    "净化", "消灾", "增殖", "假钞",
    # 怪物面板实测补充（2026-08-21 分类覆盖审计）：
    "分裂",  # 分裂：命零创造自身造物 → 自用
    "逆鳞",  # 逆鳞：目标受伤积层→下次伤害加成 → 怪物自用（自强化）
    "背负",  # 背负：目标受伤由自身承担 → 怪物无友方，自施=无操作（不帮玩家）
}
MONSTER_HOSTILE_DAOWEN = {
    "杀伐", "连击", "减速", "束缚", "僵化", "蒙蔽", "弱化", "愤怒", "全速", "无力", "点金", "清算", "赎金", "波及", "勒索",
    "定型", "畸变", "坏死", "退化", "加害", "裂变", "伤痕", "冥气",
    "勾魂", "缄默", "瓦解", "寄生", "封印", "贯穿",
    "洞察",
    "豪夺",  # 抵扣：封印目标一件遗物 → 削玩家（怪物面板实测补充）
}
# ---------------------------------------------------------------------------
# 怪物道纹优先级分组（DM裁定2026-08-18；2026-08-22 起全仓唯一口径）：
#   1.自保 → 2.输出 → 3.控制/削弱 → 4.机制（最后手段）
# 背景：此前该分组在 build_learner / duel_common / handplay_dungeon_with_winner /
# pick_best_report / produce_real_winners 五处各抄一份，2026-08-21 版本变更
# （冲击改名波及、删除缓慢/慈悲/切割）后其中三处仍引用已删除道纹，导致怪物把
# 新道纹（如【波及】）误归"机制组"当最后手段。现全部收拢到本模块，
# 新增道纹只改这里；未知道纹按"机制组"兜底，绝不因表外道纹导致选择异常。
# 分组口径：按道纹对**施法怪物**的战术价值划分（自保/自强化=自保；对敌压力=
# 输出或控制；纯自身机制=机制）。目标选择仍由 pick_monster_daowen_target 负责。
MONSTER_DAOWEN_SELF = {
    # 原始（自用）
    "疯狂",
    # 转化（自用增益/续航）
    "自食", "急速", "加速", "洞察", "寄生",
    # 副本专属（自保/自强化/自经济）
    "超频", "假钞", "龙鳞", "逆鳞", "背负",
    # 2026-10-13：【典当】（失忆换发现）是自身向收益，归自保组；【逼债】删除。
    "典当",
}
MONSTER_DAOWEN_OUTPUT = {
    # 原始
    "狂暴", "全力",
    # 转化
    # 杀伐闭环（怪物可经事件/原初持有）
    "杀伐", "连击", "波及",
    # 副本专属（对敌压力）
    "加害", "裂变", "失忆", "赎金",
    # 2026-10-14：【勒索】mirror 逼债旧位（对敌经济压力，归输出组）。
    "勒索",
}
MONSTER_DAOWEN_CONTROL = {
    # 原始
    "减速",
    # 转化
    "愤怒", "弱化", "无力", "全速", "蒙蔽",     # 杀伐闭环
    "束缚", "封印",
    # 副本专属
    "僵化", "定型", "畸变", "坏死", "退化", "伤痕", "冥气", "勾魂",     "瓦解", "缄默", "清算", "豪夺",
}
MONSTER_DAOWEN_MECH = {
    # 原始（自身机制）
    "必中", "飞行",
    # 转化（自身机制）
    # 副本专属（纯机制：无直接对敌伤害/控制收益）
    "变形", "分裂", "消灾", "招魂",
}
def monster_daowen_group(name: str) -> int:
    """道纹优先级组：0=自保 1=输出 2=控制 3=机制（表外道纹按机制兜底）。"""
    if name in MONSTER_DAOWEN_SELF:
        return 0
    if name in MONSTER_DAOWEN_OUTPUT:
        return 1
    if name in MONSTER_DAOWEN_CONTROL:
        return 2
    return 3
def pick_monster_daowen_option(cands: list[dict], *, player_low: bool = False,
                               monster_low: bool = False,
                               blocked: bool = False) -> Optional[dict]:
    """按 DM裁定2026-08-18 的固定优先级为怪物选道纹（全仓唯一入口）。
    cands：prepare 列出的合法道纹选项（调用方应已排除本回合已激活项；
    若 cands 为空而原选项非空，由调用方回退首个选项）。
    固定优先级：自保→输出→控制→机制（最后手段）。上下文修正：
      - monster_low：怪物半血以下，强制优先自保；
      - player_low：玩家半血以下，优先输出/控制（收割窗口）；
      - blocked：怪物已连续≥2回合未能让敌方掉血（如被格挡全吸收），
        满足裁定原文"完全无法对敌方造成任何影响"的字面情形→允许动用机制组。
    """
    if not cands:
        return None
    if monster_low:
        self_cands = [o for o in cands if monster_daowen_group(o["name"]) == 0]
        if self_cands:
            return self_cands[0]
    if player_low:
        kill_cands = [o for o in cands
                      if monster_daowen_group(o["name"]) in (1, 2)]
        if kill_cands:
            return kill_cands[0]
    if blocked:
        mech_cands = [o for o in cands if monster_daowen_group(o["name"]) == 3]
        if mech_cands:
            return mech_cands[0]
    return min(cands, key=lambda o: monster_daowen_group(o["name"]))
def pick_monster_daowen_x(engine, monster, option, choice_tpl: dict, token: str) -> int:
    """x_free 道纹：用**战术预演评分**为怪物挑一个 X（2026-09-16 用户令，选案 C）。
    面板不写死 X，能开多大只受[法限]或代价限制——但"能开多大"不等于"该开多大"。
    X 越大代价越高（法力即[攻击力]，异变更是会把自己推向【崩解】），
    因此逐个候选档位真实预演一遍，按后果评分取最优。
    实现要点：
    - 预演走 engine/ai_preview.py 的 ActionPreview（deepcopy state 后真实执行再丢弃），
      不复制任何伤害/反伤规则，预演与结算口径天然一致
    - 评分走 TacticalAI._score_candidate，用 actor=monster 把视角翻到怪物侧
      （_split_diff 按名字在 diff["enemies"] 里找自己，怪物侧同样成立）
    - 只取 1 / 中档 / 上限 三个代表值，不枚举全部 X——预演要 deepcopy 整个
      state，全枚举在长模拟里开销过大
    - 评分返回 None 表示该档位会把自己玩死（如异变逼近崩解线），直接跳过
    - 全部候选都不可用时回退到上限，与引擎侧缺 x 的回退口径一致
    choice_tpl 是已拼装好的完整提交模板（含攻击块），这里只替换 daowen["x"]；
    保留攻击块是因为引擎要求 attack_actions 的逐击命中数必须提交完整，
    只提交道纹会被拒（攻击部分的后果在各候选间是常量，不影响 X 之间的比较）。
    """
    import copy
    max_x = int(option.get("max_x") or option.get("x") or 1)
    if max_x <= 1:
        return max(1, max_x)
    from engine.ai_preview import ActionPreview
    from engine.ai_tactics import TacticalAI
    foes = [x for x in (engine.state.get_all_player_side() or []) if x.is_alive]
    ai = TacticalAI(engine, actor=monster, enemies=foes)
    preview = ActionPreview(engine)
    best, best_score = max_x, None
    for x in sorted({1, max(1, max_x // 2), max_x}):
        trial = copy.deepcopy(choice_tpl)
        trial["daowen"]["x"] = x
        out = preview.preview("resolve_monster_phase",
                              {"token": token, "choices": [trial]})
        res = out.get("result") or {}
        if not res.get("success"):
            continue
        score = ai._score_candidate(out.get("diff") or {},
                                    "%sX=%d" % (option.get("name", "?"), x))
        if score is None:
            continue
        score += _persistent_duration_value(engine, monster, option, x, ai)
        if best_score is None or score > best_score:
            best_score, best = score, x
    return best
def _persistent_duration_value(engine, monster, option, x, ai) -> float:
    """跨回合生效的**时长型**道纹（duration == X）补一项期望收益。
    典型：【狂暴】「异变+5X，回始发动一轮额外攻击，持续X回合」。效果从**下一
    回合**才兑现，单步预演只看得到异变代价、一分收益都没有，于是恒选 X=1。
    但按回合摊薄，X=1 与 X=9 的异变单价完全相同（都是 5/回合），真正的差别在
    **道纹出手位**——X=1 每回合都要重放、占掉一次道纹机会；X=9 一次买断九回合。
    收益按"每回合产出 × 有效回合数 × 折价"估：
      · 每回合产出 = 攻次 × 攻力（该 buff 额外一轮攻击的伤害）
      · 有效回合数 = min(X, 预期剩余回合)——打不完那么多回合，多买的时长是浪费
      · 折价 0.5：战斗进程不确定（怪物可能先死、目标可能换），保守折扣
    只作用于 duration == X 的道纹；【加害】这类 duration=-1 的持续状态不在此列
    （它的 +X 在**当回合**的攻击里就已结算，预演看得到，不需要补）。
    """
    try:
        from engine.daowen import DaoWenEngine
        calc = DaoWenEngine.resolve(option.get("name", ""), x, caster=monster)
    except Exception:
        return 0.0
    if int(calc.get("duration") or 0) != x:
        return 0.0
    per_round = monster.effective_attack_count() * monster.effective_attack_power()
    if per_round <= 0:
        return 0.0
    try:
        incoming = max(1.0, float(ai.incoming_damage()))
    except Exception:
        incoming = 1.0
    expected_rounds = monster.current_hp / incoming
    effective = min(float(x), expected_rounds)
    return 0.5 * per_round * effective
# 波及X 的「恰好选 X 个目标」助手已迁到 production（engine/ai_rules.py），
# 因为 PlaceholderBackend 提交怪物阶段时也在用它；此处再导出保持 sim 路径可用。
from engine.ai_rules import pick_wave_dodge_targets  # noqa: E402,F401
def pick_monster_daowen_target(engine, actor_ref: str, option: dict) -> str:
    """按道纹目标类型为怪物选择道纹目标（最优策略，不依赖 target_options[0]）。
    SELF 类：优先自身；自身不在合法目标中时回退到第一个合法目标
    （不强行选择玩家）。
    其余（HOSTILE/未分类）：优先玩家（威胁最高）；玩家不可选时回退到
    第一个合法目标。
    """
    name = option.get("resolves_as") or option["name"]
    targets = option["target_options"] or []
    if not targets:
        return ""
    if name in MONSTER_SELF_DAOWEN:
        for t in targets:
            if t["ref"] == actor_ref:
                return t["ref"]
        return targets[0]["ref"]
    for t in targets:
        if t["ref"] == "player:0":
            return t["ref"]
    return targets[0]["ref"]
