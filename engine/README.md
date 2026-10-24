# 第四宇宙 · 临济·第四宇宙 游戏引擎

## 核心架构

```
AI（决策者）──→ GameEngine API ──→ 计算/随机数
                    │
                    ├── 遇到可判定情况 → 返回结果 → AI继续决策
                    │
                    └── 遇到特殊事件 → 抛出Interrupt → DM裁定
                                                      │
                                                      ├── 存入数据库
                                                      └── 下次自动匹配
```

## 核心原则

1. **AI是决策者，程序是事实源** — 所有数值计算、随机数必须通过引擎
2. **中断机制** — 程序无法判定时（许愿/逃跑等），抛出Interrupt等待DM
3. **先例数据库** — DM裁定存入SQLite，下次类似场景自动匹配
4. **自由规则（含自由控X）** — 道纹的X值由AI在合法范围内自由指定；「没被禁止的取值同样允许」，所以 X 不必单列一条规则

## 正文事实源

- `规则正文.md`：通用规则、通用道纹与通用事件的唯一精确事实源；`README.md` 只作入口与 AI/维护速览，`推演规范.md` 保存新手、战斗与战报的操作规范。
- `AI_CONTEXT.md`：由 `sim/gen_ai_context.py` 生成的外部 AI 单文件聚合物；便于 raw URL 消费，**不是**规则事实源。
- `AI_EXPERIENCE.md`：AI 知识库、协作规范、推演铁律与工程验证准则，不承载通用规则正文。
- `法术索引.md`：法术设计原则、三大法则、法术阶级规则、法术大全与自创法术标准语法的唯一完整说明。
- `死者之书.md`：法术大全数据的引擎事实源（`RuleSync.extract_spells_from_file` 读取此文件）与「## 死者之页」（生平＋遗言）格式；法术效果正文的可读性权威说明见 `法术索引.md`。
- `物品索引.md`：遗物、消耗品与法器。
- `副本索引.md`：副本清单；其链接的已实现副本文档进入运行时，未实现草案只参与文档校验。

`RuleSync()`默认同时跟踪 规则正文.md、死者之书.md、物品索引.md、副本索引.md 四份正文（`法术索引.md` 是展示层文档，不参与规则抽取）；README.md 仅入口，不得再从 README 推断通用规则、物品或副本怪物。

## 文件结构

```
engine/
├── __init__.py          # 包初始化

├── enums.py             # 枚举/常量（阶段、触发时点、代价类型等）
├── models.py            # 数据模型（Entity, Spell, Relic, GameState；Entity.is_proliferated = 癌变旧名增生 的兼容字段，is_cancer 为别名）
├── dice.py              # 随机数引擎（池系统，auto_roll 默认引擎自生成并结算，可传 seed 复现）
├── dungeons.py          # 副本清单解析（已实现/未实现状态）与运行时草案隔离
├── monsters.py          # 怪物池解析（从副本索引加载36怪物面板）与出怪（战始抽怪）公式
├── gamedata.py          # 静态数据（怪物池/遗物池/事件池常量，MONSTER_POOLS 等）
├── events.py            # 事件池（parse_events、EventPool，通用 10 + 三副本专属）
├── daowen.py            # 道纹公式入口（当前全部 DaoWenEngine.calculate_* 与 ResonanceEngine；增殖为道纹，癌变为机制，二者无关）
├── rule_engine.py       # 统一数值规则事实源/执行器（第一阶段：加害、龙鳞、固执受击伤害链；文案与运行共源）
├── combat.py            # 战斗计算引擎（伤害/回合/闪避/多路径 癌变/雕塑/还债，PROLIFERATION_THRESHOLD 为癌变阈值，CANCER_THRESHOLD 别名）
├── battle_report.py     # 战报渲染（推演格式逐回合输出）
├── ai_player.py         # 统一 AI 玩家入口（开局/事件/战斗/校验/长期记忆）；2026-09-29 起全部决策由 LLM backend 给出
├── ai_memory.py         # 当前轮回者的身世、经历、性格证据与遗言压缩
├── ai_rules.py         # 生产侧本地规则助手（闪避/遗物/法器/友方道纹X的启发式）。engine/ 生产路径唯一可用的"非 LLM 决策"来源，严禁 import ai_tactics/ai_preview/sim
├── ai_tactics.py        # 规则型战术 AI（TacticalAI）。游戏中已停用；2026-10-02 起与生产路径隔离，仅供 sim/probes/测试直接实例化（engine/ 不引用它）
├── ai_preview.py        # TacticalAI 的行动后果预演层；同属隔离的实验/模拟工具，生产路径不引用
├── dm_rulings.py        # DM 裁定库（SQLite + FTS，先例匹配）
├── document_sources.py  # 文档事实源文件名（规则正文/推演规范/README入口）
├── rule_sync.py         # 多事实源同步（规则正文/死者之书/物品索引/副本索引）
├── document_validation.py # Markdown标题、文件链接与锚点校验（含资源设计标准 11 项标记）
├── custom_resources.py  # 自定义资源 v1：JSON 声明 → 道纹注册表/Mechanism/consume_item
│                        #   复用 verbs/Phase/relic_active；校验四条设计标准；示例见 data/custom_resources/examples
├── death_book.py        # 《死者之书》死者之页读写（生平自动总结＋遗言自定义；审核后只改 ## 死者之页）
├── personality.py       # 角色性格特征（实例级，行为推断"先射箭后画靶"，命零即清除；不写模板不跨实例继承）
├── validator.py         # 规则校验器（20 条内置检查，违规入库 + 已迁移机制护栏）
├── mechanisms/          # 最小可行机制系统（MVP）：Verb/Mechanism/Trigger/Condition/Target
│                        #   已声明机制：加害（原 JiahaiHook）、龙鳞（原 LonglinHook）、固执（伤害规则见 rule_engine.py）；洞察·结算/畸变·标记（原 round_start 内嵌块，回始循环已全部声明化；自愈 2026-10-11、衰败 2026-10-11、狂暴·标记 2026-10-03 随道纹删除）、畸变·结算（原 round_end 内嵌块）、焦黑发丝（原 _on_entity_death 内嵌块）、洗劫·夺碎片（原伤害管线内嵌块）、帮派令/缄默面具（原 process_relics 战始 if）。新机制优先写声明层，勿回核心管线加 if。
└── api.py               # GameEngine 主类 — AI 唯一交互入口（含 TWISTED_TOOL_LIBRARY、TERMINAL_ARTIFACTS、FIRST_EMBRACE_OPTIONS 等）
```

> **2026-08-11 F7 订正**：五章「全程自动触发」已与「特殊事件（全局触发）」14 项对齐（补 凡庸/癌变/迷失/还债/雕塑/救赎）；「增生」全量更名为「癌变」（旧名 增生 保留为兼容字段 `is_proliferated`/`PROLIFERATION_THRESHOLD`/`proliferation`），「增殖」为独立道纹（血限+2X）二者无关。

## 机制系统

`engine/mechanisms/` 提供最小可行机制声明层，验证 Effect / Trigger / Condition / Target / Verb 思路：

- **Mechanism** = 【什么时候】when + 【对谁】target + 【满足什么条件】condition + 【做什么】effect + priority。
  定义全局唯一（`MECHANISMS`），机制自身状态按实体存放（`entity._mechanism_states[机制名]`）。
- **Trigger**：复用 `CombatEventType`（事件路径，经 `CombatEngine._emit` 分发）与 `Phase`（管线相位路径，
  经 `CombatHookManager` 既有分发路径执行）。不新建第二套事件枚举。
- **Condition**：`has_status / side_has / is_alive / entity_type / hp_at_least / events_this_round /
  entity_has_relic / player_run_relic_active / all_ / any_ / not_` 等最小组合子。
- **Target**：`SELF / TARGET / SOURCE / ALL / ALL_ALLIES / ALL_ENEMIES / RANDOM_ENEMY / DEAD_ENTITY`。
- **Verb**：damage / heal / hp_loss / blood_limit / cost / status / speed / shield / mutation /
  mana / shards / direct_damage / mapping / reset_fields / depart / execute——实现体全部是现有统一结算入口或旧数据操作的薄包装，不新增游戏逻辑。

当前 Mechanism 声明层含【加害】【龙鳞】【固执】三个受击伤害适配器；三者的实际条件、阶段、运算和玩家摘要由 `engine/rule_engine.py` 中的稳定 ID 规则定义驱动，并经 `MechanismHookAdapter` 接入原 Hook 路径。固定 priority（20/30/40）仅是缺少状态排序锚点时的后备；有效持续状态按当前道纹序列位置排序；缺少序列锚点时才回退到 priority。回始循环已全部声明化：
【洞察·结算】(30)、【畸变·标记】(60)——经 `_dispatch_phase(Phase.ROUND_START)`
回始原位分发（洞察经 2026-08-19 新增的通用 `mana` 动词：获得含不朽之躯钳制、失去下限 0）。
2026-10-13 核对：`MECHANISMS` 实际注册 25 个机制，回始相位只有上述两个；
【勾魂】并未注册为机制（本行旧文写的「勾魂(40)」是过期描述，已按实测删除）；
【自愈】【衰败】随道纹删除，【狂暴·标记】2026-10-03 随道纹删除。
【畸变·结算】（priority=10，血限走统一入口，lethal=True）——经
`_dispatch_phase(Phase.ROUND_END)` 回终第一循环顶部、凡庸 tick 之前原位分发（锚定语义，
见机制迁移台账）；【焦黑发丝】（priority=10，怪物命零→玩家速度+2）——第一个生产事件
机制：经 `TriggerBus` 订阅 `ENTITY_DIED`，`_emit` 显式传实体对象；【帮派令】（priority=10）
——经 `_dispatch_phase(Phase.BATTLE_START)` 战始原位分发，条件复用通用
`relic_active("帮派令")`。旧类/旧 if 均已删除。护栏：`validator.check_migrated_mechanism_guards()` 禁止已迁移机制
在核心管线重新出现同名硬编码分支。批量迁移见审计报告《机制系统化可扩展性审计》。

2026-10-13 六项挂起迁移：血族血脉/赤族诅咒在凡庸之后的独立逐实体相位；皮衣在回始最前段和回终追踪清理前；防弹插板在战始选择处理之后；龙族血脉乘区经原 Hook 桥且不在 incoming 重复执行；三相残韵盘战终复用已序列化的战斗运行态。原数值、封存口径、死亡/回复上下文和选择协议不变。映射与验证见 `../archive/mechanism_migration_2026-10-13.md`。

## 道纹统一数值规则系统（第一阶段）

`engine/rule_engine.py` 是本阶段三个伤害规则的统一事实源。每条定义包含稳定规则 ID、道纹/状态 ID、事件与阶段、目标角色、条件组、当前值读写语义、运算符与参数来源、排序后备值；`DaoWenEngine.calculate_*` 的摘要和 `sim/gen_daowen_index.py` 的索引条目直接读取同一份定义。`MechanismHookAdapter` 只作为旧 Hook 分发接口的兼容壳，不保留第二份数值公式。

本阶段只迁移【加害】（ADD）、【龙鳞】（减法并下限钳制为0）、【固执】（倍率前当前值上限为1，并在倍率后封顶最终失血为1），用于验证统一阶段中的不同运算及顺序涌现；其它道纹继续运行旧实现，按 `daowen_migration_status(name)` 显示 `legacy` 和统一的未迁移原因。不得让旧实现与规则适配器同时写同一个结算值。

伤害数值链字段不可互换：

- `original_damage`：CombatEngine 入口收到的原始伤害；`incoming_damage`：倍率等上游修正后、格挡前数值；
- `current_damage`：格挡后进入规则阶段的当前值。每条规则读取前序规则刚写回的值，不重读快照；中间值变成0仍继续遍历；
- `post_rule_damage`：规则链结束、失血倍率应用前的数值；`damage_after_life_loss_multiplier`：应用【第一杯】等倍率后的待扣数值；
- `shield_absorbed`：格挡吸收量；`actual_life_loss`：受击前后生命值差。`damage_after_life_loss_multiplier` 保留倍率后、最终封顶前的待扣数值；`actual_damage` 表示最终待扣数值，过量伤害时仍可能大于实际 HP 下降量。

规则阶段为「完成格挡处理后（包括无视格挡）、失血倍率前、扣生命值前」；代价伤害不进入本阶段，格挡完全吸收的非代价事件不调用持续状态调整。固执在该阶段先限制当前待结算伤害，并在失血倍率之后再次封顶，确保非代价受击的最终实际失血不超过1点；直接失血途径不进入这条受击规则。未经规则引擎接入的直接 `Entity.take_damage` 调用保留兼容分支，正式 CombatEngine 路径传入统一调整器，因此不会双算；规则/数值校验失败时已消耗的护盾会回滚。

数值契约：伤害输入保持 Python 任意精度整数；负整数沿用 no-op，不会反向治疗；`None`、`bool`、浮点数、NaN 与 ±Infinity明确拒绝，防止隐式转型和非标准 JSON 数值。失血倍率必须是至少为1的有限整数。状态 `duration=-1` 表示永久，与伤害数值域无关。伤害结算返回 `damage_resolution`、`rule_trace` 和独立的 `preview_rule_trace`；trace 记录实际执行与未执行条件，不伪造规则写入，超出运行时 JSON 整数转换阈值的 trace 数值以十进制字符串序列化。濒死保护预演和正式扣血使用不同 context，允许各执行一次但不会把预演当作第二次提交。规则 ID 在同一 context 内重复执行会报错，失败执行不会污染上下文；延迟事件只入队、不得在当前规则链递归触发。

扩展规范：先定义新的 Operator/Condition/ValueRef/Stage，再从唯一战斗入口接线；复杂效果可注册 `CUSTOM` 运算器或 `DEFER` 队列项，结果只能改写明确声明的值，条件、排序、trace、重入保险仍由统一执行器负责。新增阶段前必须核对原始/当前/最终数值边界、preview/commit、副作用次数、存档兼容与 JSON 表示，并给出旧路径停用点和回归测试；不以道纹名称写组合分支。

## AI交互流程

### 1. 获取状态
```python
state = engine.get_state()
# 返回：当前状态、可用行动、待处理中断、上次结果
# state["state"]["personality_traits"] 含存活角色的性格特征（结构化，实例级）
```

### 2. 执行行动
```python
result = engine.execute_action("use_daowen", {
    "daowen_name": "杀伐",
    "x": 5,
    "target": "千手蜈蚣"
})
```

### 3. 处理中断
```python
# 如果result中有interrupt，需要DM裁定
if result.get("interrupt"):
    # 提交DM裁定
    engine.submit_ruling(
        interrupt_type="许愿",
        ruling_text="利用蒸汽遮蔽视线",
        ruling_data={"effect": "怪物下回合无法选中目标"}
    )
```

### 4. 查询先例
```python
# AI可以在触发特殊事件前查询是否有先例
precedent = engine.check_precedent("许愿", {"target": "千手蜈蚣"})
if precedent["found"]:
    # 直接应用先例
    ...
```

### 5. 随机数
```python
# 游戏内实际随机行动（探索/共鸣/开局遗物等）均由引擎内部调用 DiceEngine.auto_roll()
# 直接生成随机数并结算，AI拿到的是已经确定的结果，无需再向玩家索要数字：
result = engine.execute_action("pre_battle_action", {"sub_action": "探索"})
# result["result"]["event"] 已经是引擎自动摇出的具体事件名

# 需要可复现的随机结果时（例如回归测试），在构造引擎时传入固定种子：
engine = GameEngine(rng_seed=12345)
```

## 角色性格特征

原则"先射箭，后画靶"：创建角色不预设人格；性格只能由实际发生的行为逐渐推断
（`update_personality`，每条证据=维度+方向±1+行为依据）；单次行为不贴死标签
（首条置信度封顶 0.30、强度减半），后续行为可持续修正（反向证据削置信度）。

```python
engine.update_personality(entity, "risk_preference", +1, evidence="第1场主动选择死斗")
engine.get_personality(entity)          # 结构化条目（value/strength/confidence/evidence_count/last_evidence）
engine.format_personality_for_ai(entity)  # 供 AI 使用的文字摘要（含"倾向非强制"提示）
engine.remove_personality(entity)       # 手工清除（幂等）
```

数据归属：`GameState.personality_traits`，键为 `Entity.runtime_id`（实例级，
同名不同实例互不共享）；不写入角色模板，不跨副本/轮回/实例继承。随 GameState
的 deepcopy 事务快照与 pickle 存档自然往返。命零时由统一死亡管线
`CombatEngine._on_entity_death` 自动清除——死亡角色的性格对 AI 与一切查询接口
均不可读。性格只是行为倾向参考，绝不构成对角色决策的强制规则。

## 撤退机制

已实现，详见 AI_EXPERIENCE.md（设计要点/取舍记录）。要点：仅[朋友]/[员工]适用，
`has_retreated`标记生效期为"本场战斗"，[战终]重置。

## 出手预算校验

已实现，详见 README.md 与 AI_EXPERIENCE.md。要点：`action_count`按entity_type分流公式；轮回者出手预算是**基础 2 次**再叠加修正（【疯狂】+X/【无力】-X/【蓄锐·增】+1），普攻占用一次主动出手但不消耗速度；已定义的自动触发法术【镇魔印】不占主动出手。AI 侧口径：出手预算=**动作槽位数量**，每个槽位提交一条决策，槽位用完就结束己方行动阶段（不写死"每回合固定 2 次"、不把多次独立行动打包成一条）。
消耗/不消耗出手的动作清单见下表备注。

## 最终的冠冕 / 第8场死斗

已实现，详见 README.md 与 AI_EXPERIENCE.md。要点：`GameEngine(sealed_candidate_path=...)`指定跨实例共享的
封存候选人JSON路径；[战终]第7场自动判定"封存"或"进入死斗"，无需额外调用。

## 三副本终音法器 / 初拥之夜 / 真龙之心

已实现，详见 README.md 与 AI_EXPERIENCE.md。要点：死斗胜利后按`current_region`从`GameEngine.TERMINAL_ARTIFACTS`
对应列表中选1件（`choose_terminal_artifact`），选到"猩红尖牙"会先强制触发初拥之夜
（`GameEngine.FIRST_EMBRACE_OPTIONS`9选1，`choose_first_embrace`）完成后才真正封存；
"真龙之心"解锁后进入龙族项目系统：`GameEngine.DRAGON_TRAITS` 7 件遗物 + `DRAGON_CONSUMABLE_TRAITS` 的消耗品【烬翼】。2026-10-23 起废除「龙性」货币：玩家实际支付的数值型【代价】自动记入 `state.dragon_cost_points`（只作兑换门槛，不可花用），满 12 点换 1 件。

## 行动类型一览

| 行动类型 | 说明 |
|---------|------|
| `setup_attributes` | 分配初始25属性点，并从杀伐闭环发现3种初始道纹候选 |
| `setup_choose_initial_daowen` | 从发现候选中显式选择1种作为初始道纹 |
| `resolve_redemption` | 救赎触发后选择接纳为待命员工（自定义员工名）或无视 |
| `setup_choose_resonance` | 选择初始残韵 |
| `setup_choose_region` | 选择副本；完成后默认下一步为 `world_initialize` |
| `world_initialize` | 默认副本入口：按当前副本区域初始化 21×21 开放地图 |
| `world_move` / `world_wait` / `world_interact` | 开放地图移动、等待与交互；地图遭遇使用带 `world_encounter_id` 的 `battle_start` |
| `world_suggest_ally` | 向朋友/员工/临时朋友**提建议**（follow/guard/explore/hold）；不改变其位置，是否照做由该单位按 `world.suggestion_acceptance_percent` 自行判定，拒绝时行为与守点原样保留。范围5格，守卫可带 `x`/`y` |
| `pre_battle_action` | 局外行动；旧线性模式仅在用户明确要求时使用 |
| `use_daowen` | 发动道纹（可指定[朋友]/[员工]的actor_ref听从指令，由其自身支付法力并消耗出手；初拥专属【真理】需提交target_ref与statement，冷却2场且不消耗出手） |
| `define_spell` | 统一自定义施法：用 `spell_name` 定义法术大全条目，或用 `spell` 定义新法术；所有法术类型均由此入口表达（1出手） |
| `cast` | 施法。`kind=daowen`（默认）=发动单个道纹；`kind=spell`=统一自定义施法；带 `flow` = **瞬发法术**：一次出手依次发动多种道纹（只扣1出手，每步都是一次发动道纹，逐步按真实法力结算，中断保留已结算步骤、出手不退，不写入角色法术列表）。见 法术索引.md §5.4 |
| `use_resonance` | 使用残韵 |
| `attack` | 普通攻击（attacker可指定为已部署[朋友]/[员工]，目标自动限定为对方阵营） |
| `deploy_employee` | 派遣[员工]出战(出战支援，消耗1出手) |
| `dismiss_employee` | 解雇[员工]（自由行动，无代价） |
| `pay_employee_wage` | 战终对某[员工]的工资做出pay/refuse决策 |
| `choose_hired_daowen` | 雇佣diy后，从3个发现的转化道纹候选中选择1个 |
| `declare_betrayal` | 背叛：由[朋友]／[员工]自己声明夺书（自选项，无阈值、不自动触发）；`names` 指名背叛者 |
| `suppress_rebellion` | 背叛·镇压：背叛者搬入state.enemies开战 |
| `resolve_rebellion_battle` | 镇压结算(outcome=victory/defeat) |
| `appease_rebellion` | 背叛·让利：全局工资+5，平息叛乱（仅对[员工]背叛有效） |
| `negotiate_rebellion` | 背叛·谈判：抛Interrupt交DM裁定 |
| `resolve_final_duel` | 第8场死斗结算；失败时触发【死者之页】中断，可选带`death_book_entry`作草稿 |
| `submit_ruling`（死者之页） | 轮回者命零后审核其页：`action=approve/edit/reject`；生平由引擎自动总结（`summarize_life`），玩家只自定义`text`（≤20字），通过或修改后写入`死者之书.md`的「## 死者之页」节 |
| `choose_terminal_artifact` | 死斗胜利后按副本领取终音法器(choice=序号) |
| `choose_first_embrace` | 初拥之夜9选1(choice=1~9，1~8限选1次，9可重复)；选项3 发放消耗品【血翼】3/3，不占遗物位 |
| `define_resource` | 声明一条自定义资源（道纹/遗物/法器/消耗品）；`spec` 或 `specs`，默认就地授予。这是「当场设计」的唯一入口，不占出手 |
| `activate_resource` | 主动激活自定义法器（默认不占出手；`costs_action=true` 才占） |
| `list_resources` | 列出已注册的自定义资源 |
| `use_black_card` | 黑金名片(罪孽都市终音)：敌方血限减半，等量碎片(允许负债≤50) |
| `use_crime_vault` | 罪业金库(罪孽都市终音)：消耗X碎片(≤2%当前碎片)换2X格挡 |
| `fire_godfather_revolver` | 教父左轮(罪孽都市终音)：对target打出30%血限×本场使用次数的必中伤害 |
| `select_shared_dragon_heart` | 共心环(龙心谷终音)：选定本场共享的龙心类型 |
| `declare_fuyuebei_toll` | 负岳碑(龙心谷终音)：预声明保护某[朋友]/[员工]下次撤退 |
| `unlock_dragon_trait` | 真龙之心：代价累积满12点解锁1种龙族遗物(trait)；trait=烬翼 时改发消耗品【烬翼】5/5。震岳龙躯为常驻被动，无独立 action |
| `consume_item` | 使用消耗品；翼类（血翼/烬翼）必须带 `mode`：血翼 `lifesteal`／`fly`／`flee`，烬翼 `suppress`／`fly`／`flee`，每次耗1点耐久 |
| `devour_monster` | 吞骸龙胃：吞噬已命零的怪物，回复12+可选龙心+6耐久 |
| `declare_tail_sacrifice` | 断尾求生：预声明命零时愿意移除的其他龙族遗物 |
| `enslave_as_chizu` | 初拥之夜·血族尖牙：衰老20，将生命低于自身的目标转化为赤族 |
| `blood_feast` | 初拥之夜·血食：命零一名赤族，回复等量生命 |
| `choose_diandang_daowen` | 领取【典当】发现：从当前组3选1里选1种获得，不消耗出手 |
| `prepare_attack` / `resolve_attack` | 两阶段攻击：逐击绑定目标、闪避、血影和法术反应 |
| `consume_item` | 使用消耗品 |
| `declare_wish` | 声明许愿：轮回者向"某人"祈求（消耗1出手） |
| `declare_escape` | 逃跑：一次付清5次出手，以当前状态强制脱离本场战斗（死斗/【不离】不可） |
| `declare_evolution` | 怪物进化：发动【原初X】借用原始怪物道纹（引擎直接结算） |
| `round_start` | 回始结算 |
| `round_end` | 回终结算 |
| `battle_start` | 战始（配方式出怪：N=随机(1,上界), S=随机(1,N), 增援 R_i/T_i 随机；上界一阶=max(1,战斗场数-3)、二阶及以上=12；从当前副本池随机抽取允许重复；战斗背景纯叙事不做机制化；无尽模式出怪池=所有副本合并、面板按轮次递增） |
| `battle_end` | 战终 |

## 运行

```bash
# 交互式命令行
python main.py

# API演示
python main.py --api

# 回归锁（不是对局本身）
python tests/test_engine.py
python -m pytest tests -q
```

用户说「测试」时，默认由 AI 通过 `GameEngine.execute_action` 逐步点选手操并按 README《六、战斗推演格式》写战报。禁止把 `TacticalAI`（已被隔离的实验工具）、`sim/pick_best_report.py`、sim 批量通关当作默认测试。`pytest` 只锁定回归。`报告.md` 只保留最新一次轮回记录。
