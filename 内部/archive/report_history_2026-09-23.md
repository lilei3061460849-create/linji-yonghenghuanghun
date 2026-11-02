# 报告：正文内容调整 + 【第一杯】重做 + 巴别塔草案修订（2026-09-23）

> 本文件是 **2026-09-23 的历史任务记录**，不是当前状态或规则事实源。
> 上一轮性能测量报告及依赖的旧实验数据已清理；当前实现与表现以仓库现行代码和测试为准。
> 本文保留【第一杯】当时的改制过程和巴别塔设计修改背景；未实现草案本体仍以 `副本/巴别塔.md` 为准。
> 分支 `arena/01a0ba24-linji-disiyuzhou`，基线 `2612fa8`。全部命令在仓库根目录用 `.venv/bin/python` 执行。

---

## 〇、一句话结论

本轮是**规则变更轮**（用户明文令），不是引擎重构：改了 4 处正文（世界观一句话、怪物准则一条、
【第一杯】整条、巴别塔三条），并把 5 项设计放进待办。
**【第一杯】从"免疫癌变"改成"受到的[回复]与失去的生命翻倍"**——这是本轮唯一的行为变更，
引擎侧只加了**两条倍率事实源**并在既有结算入口注入，没有新增结算路径、没有动任何数值公式。

---

## 一、改了什么

### 1.1 正文（用户令）

| # | 文件 | 位置 | 改动 |
| --- | --- | --- | --- |
| 1 | `故事文档.md` | 世界观·轮回的本质 | 「越是**理性、冷漠**的角色，越没有资格进入轮回」→「越是**无情**的角色，越没有资格进入轮回」 |
| 2 | `AI_EXPERIENCE.md` | 规则正文·怪物准则 6 | 「拥有智慧，~~且没有道德底线~~，同样会向"某人"祈求许愿」——删除"怪物会背叛"/无道德底线的定性（**员工背叛机制本身不动**，见待办①） |
| 3 | `物品索引.md` | 未实现副本草案遗物·第一杯 | 重做（见 1.2） |
| 4 | `副本/巴别塔.md` | 傲慢X / 真诚X | 傲慢X：消耗5X。［目标］的声明**永远视为错误**，持续X。真诚X：消耗10X。［目标］的声明**永远视为正确**，持续X。（其余六条道纹未动） |
| 5 | `副本/巴别塔.md` | 专属行动·否认 | 改为「消耗1点精力。失忆1。选择一种本次轮回中已经遭遇过的怪物，否认其存在。」+ 代码实现方式（从本次轮回怪物池永久移除） |

> 巴别塔在 `副本索引.md` 里仍是**未实现草案**（不进入事件池/怪物池/游戏流程），
> 因此第 4、5 项是纯正文改动，引擎未改；实现细节进待办（见第三节）。

### 1.2 【第一杯】重做（本轮唯一行为变更）

```text
旧：获得的每一次[回复]额外+50%；每个[回终]若本回合未获得过[回复]，流血10；
    你不再受到"癌变"事件的影响。
新：你受到的[回复]与失去的生命翻倍。
```

设计意图（写进 `物品索引.md` 的条目注）：它不是"治疗更强"，而是把治疗与伤势**一起**放大——
别人给你回多少、你自己流多少血，都按双倍算账（【血债】【透支】【衰老】这类代价因此翻倍，
而"回复翻倍"让癌变线来得更快）。三件旧效果**全部废止**：+50% 回复、[回终]流血10、免疫癌变。

**引擎落地（只加两条倍率事实源，不新增结算路径）**

| 文件 | 改动 |
| --- | --- |
| `engine/models.py`（`GameState`） | 新增 `FIRST_CUP / FIRST_CUP_MULTIPLIER / heal_multiplier(entity) / life_loss_multiplier(entity)`——两条倍率的**唯一事实源**，都走既有 `side_has`（朋友/员工不继承） |
| `engine/models.py`（`_apply_heal_inner`） | 回复入口第一行乘 `heal_multiplier`：回复明细、`total_healed`（癌变累计）、`HEAL_APPLIED` 事件三处天然一致 |
| `engine/models.py`（`Entity.take_damage`） | 新增 `life_loss_multiplier` 参数，作用在**格挡与【固执】压帽之后**：翻倍的是最终失去的生命，盾该吸多少还是多少；`actual_damage` 报的是翻倍后的真实损失 |
| `engine/combat_parts/damage_death.py` | 伤害汇点 `_apply_hostile_damage_inner` 注入倍率；`_raw_hp_loss`（爆裂反射/赌命/血影）同样翻倍 |
| `engine/combat_parts/cost_payment.py` | 流血代价：代价语义仍按 X（血誓戒照旧按 X 结算），**失去的生命**翻倍；失血总账记翻倍后的数值 |
| `engine/combat_parts/daowen_effect.py` | 规则明写「[血限]及当前生命同时 -NX」的结算：当前生命那部分翻倍、[血限]不翻倍 |
| `engine/combat_hooks.py` | 爆裂反噬（Hook 只拿得到 state）同样翻倍——倍率事实源就在 state 上，不需要给 Hook 塞引擎 |
| `engine/combat.py` / `engine/api.py` | 赤族诅咒流血、买路财"以生命补足差额"两处同口径注入；买路财的"不会致死"前置校验按翻倍后的实际支出校验 |
| `engine/combat_parts/monster_life.py` | 删除 `check_cancer` 里的「持有【第一杯】→ 跳过癌变」分支（旧条文已废止） |

**口径边界（明确不翻倍的两类，已在物品索引与规则文本里写明）**

1. **[血限]被压低导致的当前生命封顶**（`_apply_blood_limit_change` 的 clamp）：那是"上限变低"的封顶后果，
   不是一次有数值的"失去生命"，所以只压到新[血限]，不按翻倍再补一刀。
2. **把当前生命直接置 0 的命零类效果**（癌变 / 崩解 / 雕塑等）：它们带的是判定而不是数值，翻倍无从作用
   （且置 0 幂等）。过量失血照常命零，不会出现"0 血存活"。

**影响范围说明**：【第一杯】当前**不在运行期遗物池**（`engine/api.py::RELIC_DEFS` 的 11 件里没有它；
获取途径是未实现副本《永夜庭》草案的"旧日回响·一"事件），所以这次重做对现有实盘对局与 AI 决策
**没有影响**（`sim/behavior_trace.py` 指纹不变）。引擎里它原本就有实现（旧条文只有"免疫癌变"那一条），
本次按新条文同步，等到永夜庭实装即可直接用。

**规则修改：是。** 本轮唯一的规则变更是【第一杯】重做，且完全按用户令执行；
其余三处是纯文本（世界观措辞、怪物准则定性删除、巴别塔草案条文）。
除【第一杯】以外，**没有任何数值/公式被改动**：`git diff 2612fa8..HEAD -- engine/` 里除倍率注入点外
逐行都是原实现。

---

## 二、测试与验证

### 2.1 新增

`tests/test_first_cup_rework.py`（17 例）——新规则的正反两面：

| 覆盖 | 例 |
| --- | --- |
| 回复翻倍 | 持有者翻倍（面板/明细/累计回复量三处一致）、翻倍仍受[血限]封顶、朋友不继承、无遗物者与旧口径逐位相同 |
| 失去的生命翻倍 | 伤害、格挡交互（盾按原伤害吸收 4、落地 6 翻倍为 12）、流血代价、直接失血、爆裂反噬、[血限]/当前生命同时扣减的道纹 |
| 不再免疫癌变 | 持有者照样癌变、阈值本身未动（仍是 2×[血限]）、回复翻倍 ⇒ 更快撞线 |
| 明确不翻倍 | 血限压顶不补刀、过量失血照常命零（0 血存活即失败） |
| 预演同口径 | `ActionPreview` 里同样翻倍（否则 AI 会按一半伤害决策），且预演零污染 |

### 2.2 改写（规则变了，旧断言编码的是旧规则）

引擎维护规则第 9 条禁止"测试跑不过就改答案"；本轮属于**用户令改规则**，因此旧断言按新规则重写，
并在用例里注明旧规则已废止：

| 文件 | 原断言（旧规则） | 现断言（新规则） |
| --- | --- | --- |
| `tests/test_engine.py` | 持有第一杯 `check_cancer` 返回 None | 回复翻倍（5→10）+ 持有者照样癌变 |
| `tests/test_relic_fixes.py` | 同上 + 朋友不继承免疫 | 重命名为 `test_first_cup_doubles_heal_and_no_longer_blocks_cancer`；朋友不继承**倍率** |
| `tests/test_redemption_and_setup.py` | `test_moneybag_blocks_player_cancer` / `..._does_not_protect_friends` | `test_first_cup_no_longer_blocks_player_cancer` / `..._doubling_does_not_apply_to_friends` |

### 2.3 结果

```bash
.venv/bin/python -m pytest tests -q --no-header
# → 8 failed, 1739 passed, 3 xfailed in 150.92s
```

* 8 项失败与本轮**无关**且逐条同名（既有 AI/mock 类：`test_ai_basic_attack_candidate::test_at_one_by_one_daowen_still_wins`、
  `test_ai_tactics::test_ai_can_declare_parry_under_lethal_threat`、`test_build_learner::test_valid_and_invalid_are_separated`、
  `test_unified_ai::test_ai_player_is_the_combat_and_high_level_entrypoint`、`test_win_only_ai` ×4），
  与上一轮基线逐条相同（用 `diff` 比对失败名单为空）；
* pass 数 1722 → **1739**（+17，全部来自新增的 `tests/test_first_cup_rework.py`）；
* 文档一致性用例（`tests/test_rule_sources.py`、`test_document_structure.py`、`test_item_index.py`、
  `test_f7_doc_consistency.py`、`test_dungeon_index.py`）全绿；
* `sim/behavior_trace.py` 指纹不变（【第一杯】不在运行期遗物池，AI 对局路径不受影响）。

---

## 三、待办（用户令，本轮不做）

设计层改动，明确列为待办；本轮**不实现、不改引擎**。

### ① 出怪公式改为配方式

```text
N=随机(1,12)，S=随机(1,N)，R_i=随机(1,N-S-ΣR_j)，T_i=随机(1,5)，直至 S+ΣR_i=N
```

* N 为本场怪物总数，S 为战始首发数量；若仍有未出场怪物，则等待 T_i 回合后增援 R_i 只，
  R_i 不超过剩余未出场怪物数量，循环至全部怪物入场。
* 现状：`README.md`「[战始]抽取出怪（数量=战斗场数-3，最低为1）」+ 固定波次（R1 出第1只，R4/R7/R10 各增援1只）。

### ② 五阶之后：无尽模式

通过 5 阶后进入无尽模式：怪物池包含所有副本，强度越来越高，精力越来越少，无法探索。

### ③ 巴别塔大改：声明作为触发条件

道纹的声明应作为**触发条件**，而非道纹本身；配套新法器：

* **公正天平**：进入巴别塔自动获得，任何人出手前必须声明，正确有奖，错误有罚。
* 待确认：现有法器为【不公天平】（`物品索引.md#不公天平`，塔顶终音三选一），
  与新条目【公正天平】名称不同——是新法器，还是【不公天平】改名，待裁定。
* 待确认：新的【否认】是否同时调整"已见过的怪物总数/已击杀的怪物总数"
  （旧条文是各 -10，新条文只写"从怪物池永久移除"），已在该副本条目下标注留待裁定。

### ④ 四阶副本：怠惰之罪·温柔乡

* 所有怪物的攻击改为**目标提供等量回复**；
* 该副本所有怪物自带【？？的偏爱】：**你免疫凡庸**。

### ⑤ 「怪物会背叛」这一条（《怪物准则》）的后续处理

本轮只按用户令删掉了《怪物准则》里"没有道德底线"（即"怪物会背叛"）的定性（第 1.1 节 #2）。
`README.md` 的自动触发清单、`[战终]`流水线与引擎的【员工背叛】机制（`suppress_rebellion` /
`appease_rebellion` / `negotiate_rebellion`）**都还在**——用户本轮明确选择"只删该处表述"。
若后续要整套删除，属于规则变更，需要用户令 + 引擎/测试同步（`tests/test_rebellion.py` 等）。

---

## 四、复现命令

```bash
cd /home/user/linji-disiyuzhou
export PY=.venv/bin/python

# ① 本轮新测试：17 passed
$PY -m pytest tests/test_first_cup_rework.py -q --no-header

# ② 被规则变更波及的三个既有文件：76 passed
$PY -m pytest tests/test_relic_fixes.py tests/test_redemption_and_setup.py tests/test_engine.py -q --no-header

# ③ 全量：8 failed（既有 AI 夹具）/ 1739 passed / 3 xfailed
$PY -m pytest tests -q --no-header

# ④ 文档一致性（改正文后必跑）
$PY -m pytest tests/test_rule_sources.py tests/test_document_structure.py tests/test_item_index.py \
    tests/test_f7_doc_consistency.py tests/test_dungeon_index.py -q --no-header

# ⑤ 行为指纹未变（【第一杯】不在运行期遗物池）
$PY sim/behavior_trace.py
```

---

## 五、附：本轮涉及的文件

| 路径 | 作用 |
| --- | --- |
| `故事文档.md` | 世界观一句话（理性、冷漠 → 无情） |
| `AI_EXPERIENCE.md` | 怪物准则 6 删除"没有道德底线"；手操经验·关键机制口径同步【第一杯】新条文 |
| `物品索引.md` | 【第一杯】重做条文 + 设计意图与口径边界 |
| `副本/巴别塔.md` | 傲慢X/真诚X 改为"目标的声明永远视为错误/正确"；否认改为"从怪物池永久移除" |
| `sim/prompt_audit.py` | 审计清单条目同步（"第一杯免疫癌变" → "第一杯回复与失血翻倍"） |
| `engine/models.py` | 两条倍率事实源 + 回复入口 + `take_damage` 倍率参数 |
| `engine/combat_parts/damage_death.py` | 伤害汇点 / 直接失血注入倍率 |
| `engine/combat_parts/cost_payment.py` | 流血代价：代价按 X、生命损失翻倍；失血总账记真实损失 |
| `engine/combat_parts/daowen_effect.py` | [血限]/当前生命同时扣减的结算注入倍率 |
| `engine/combat_parts/monster_life.py` | 删除【第一杯】免疫癌变分支 |
| `engine/combat_hooks.py` | 爆裂反噬注入倍率 |
| `engine/combat.py` / `engine/api.py` | 赤族诅咒流血 / 买路财生命补足同口径 |
| `tests/test_first_cup_rework.py` | 新规则 17 例 |
| `tests/test_engine.py`、`tests/test_relic_fixes.py`、`tests/test_redemption_and_setup.py` | 旧断言按新规则重写 |
| `tests/test_combat_hooks_architecture.py` | 测试替身 `MockState` 补齐 `life_loss_multiplier`/`heal_multiplier` 两个新接口（Hook 现在从 state 读倍率） |
| 已清理的旧版性能测量报告及结果 | 过期测量不再作为当前性能/规则证据 |
