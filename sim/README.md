# sim/ —— 一次性探针与实验脚本

这里是**研究/验证工具**，不是游戏本体：`pytest` 只收集 `tests/`，CI 里只有
`run_open_world_replay.py` 与 `gen_rules.py --check` 是必跑项。生产代码
（`engine/`、`webapp/`）**不得** import 本目录（`tests/test_production_isolation.py`
会拦；2026-11-03 已把 `monster_phase_submit.py` 里的两个纯函数搬进 `engine/`）。

公共助手统一放 `sim/common.py`（`best_cultivate_tier` / `_resolve_monster_turn_smart` /
`learn_spell_probe_shortcut`），新脚本请直接 import，不要再各抄一份——
`tests/test_sim_common_single_source.py` 会盯。

## 目录索引

> 2026-11-03 体检结论：**没有**按子目录物理拆分。17 个 `probe_*.py` 里混着 4 种不同
> 的 `sys.path` 写法（`ROOT`、`dirname` 两次、`.`、`ROOT/"sim"`），其中一部分还按
> 裸模块名 import 同目录脚本；搬进 `sim/probes/` 会**静默**改坏这些一次性工具，
> 收益只是目录好看。故改为"索引化"：分类见下，文件名保持不动。


### 一次性探针（验证某个机制/管线是否真的接线）（17）

| 文件 | 说明 |
| --- | --- |
| `probe_arena_head2head.py` | 对抗性配对探针：训练权重 / 默认权重 / 纯随机，三者同场对打。 |
| `probe_arena_weight_divergence.py` | 对照探针：两套权重到底有没有改变怪物的实际行为？ |
| `probe_attack_only_cap.py` | 实验探针（2026-09-10）：一池制法力 + 攻次=速/攻力=法 的新经济下，普攻流上限在哪里。 |
| `probe_boba_min_x.py` | 探针：验【波及】X=1 合法（2026-10-09 用户令 repealed 了 2026-10-03 的 X≥2 下限）。 |
| `probe_combo_full.py` | （无 docstring） |
| `probe_custom_spell_triggers.py` | 真实引擎冒烟：逐一实测各触发时机能否在自创法术上真正触发。 |
| `probe_damage_attribution.py` | 探针：轮回者实际输出里，普攻 / 道纹 / 法术 / 遗物 各占多少，以及道纹使用分布。 |
| `probe_damage_pipeline_triggers.py` | 真实引擎冒烟：逐一实测【受到伤害后】【失去生命前】两个伤害管线内挂接点 |
| `probe_daowen_cost_text_drift.py` | 对照探针：道纹「代价文本」与「引擎实扣」的一致性扫描。 |
| `probe_free_action_compositions.py` | 固定行动构成的对照探针（规则实验：怪物行动自由化）。 |
| `probe_global_trigger_spells.py` | 真实引擎冒烟：逐一实测【战始/战终/回始/回终/敌回始/敌回终】6种全局时点 |
| `probe_new_relics.py` | 探针：5 件新遗物的实测表现（2026-10-03 用户令）。 |
| `probe_region_choice_synergy.py` | 探针：开局拿到【回锋刀】/【避风铃】后，AI 会不会为了【超频】【搏命】进扭曲都市？ |
| `probe_resonance_targeting.py` | 探针：残韵去向 + 各副本通关率基线/对照。 |
| `probe_spell_candidates.py` | （无 docstring） |
| `probe_spell_syntax.py` | （无 docstring） |
| `probe_trigger_condition_syntax.py` | 触发条件标准语法 —— 穷举同义写法的真实引擎实测。 |

### 其它工具（45）

| 文件 | 说明 |
| --- | --- |
| `ally_targets.py` | 微光者（[朋友]/[员工]）发动道纹时自选 X 的决策器（实现已迁到 production）。 |
| `alt_path_test.py` | 乱葬岗多路径胜利实测：毒奶流(癌变) / 蒙蔽流(凡庸) / 石化流(雕塑) / 封印流(直接移出)。 |
| `alt_win_paths_probe.py` | 另类胜利路径探针：验证穷举报告的方法论盲区。 |
| `audit_dungeons.py` | 副本草案面板合规审计（只读）。 |
| `audit_monsters.py` | 怪物面板合规审计工具（只读，不修改任何文件） |
| `balance_sim.py` | 实战平衡模拟器 |
| `behavior_trace.py` | 行为轨迹探针：固定种子跑 3 场完整 PvE，把结果规范化后打成一个 SHA256。 |
| `blade_session.py` | 白刃战双操推演驱动器：用户操作轮回者，AI 操作敌方。 |
| `breed_cohort_profile.py` | 养蛊胜者档案器：静态快照事实 + 参考死斗性格 + 循环赛战术/战绩（报告.md 表格的数据源）。 |
| `breed_parallel_scan.py` | 并行养蛊扫描器（2026-09-10）：当前引擎下 7 通关变稀有，单进程顺序扫描太慢。 |
| `combo_loop_audit.py` | 一阶逆天组合与隐藏循环审计。 |
| `design_intent_audit.py` | 设计意图符合性审计（2026-08-23 续「还债路径断点」教训）。 |
| `dominance_test.py` | 支配性检验：判断游戏是否存在"固定最优解公式化打法"。 |
| `effect_chain_audit.py` | 效果链审计工具：静态调用图 + 运行时嵌套深度实测。 |
| `eval_knowledge_level.py` | 新知识库水平测试：同一 种子×副本 网格下对照 4 组 build。 |
| `event_exploration_audit.py` | 事件探索收益审计(2026-08-26)——只读审计,不修改任何生产代码。 |
| `format_trace.py` | 按 推演规范《六、战斗推演格式》生成合规战报。 |
| `generate_report.py` | （无 docstring） |
| `handplay_dungeon_with_winner.py` | 用「真实的一阶胜者」真手操乱葬岗（二阶）。 |
| `legacy_mentor.py` | 《死者之书》遗言 → 战术 AI 的「参考桥」（sim 层，2026-09-10 DM 裁定实装）。 |
| `manual_handplay.py` | 真实手操推演引擎与战报生成器： |
| `monster_phase_submit.py` | 兼容薄壳：实现已搬到 `engine/monster_phase_submit.py`。 |
| `monster_relic_probe.py` | 原型验证：给怪物配备"反制型遗物"，能否打破公式化打法？ |
| `monster_targets.py` | 怪物道纹目标选择的共享辅助（sim 侧"最优策略"解析器专用，非引擎规则）。 |
| `optional_actions.py` | 可选遗物/法器策略的 sim 侧入口（实现已迁到 production）。 |
| `perf_bench.py` | 性能基准：GameEngine 初始化 / ActionPreview / TacticalAI 单次决策。 |
| `personality_diversity_experiment.py` | 千人千面·长期分化验证实验(2026-08-26)。 |
| `pick_best_report.py` | 批量平衡工具：按既定标准从多次轮回中挑出一份写入指定文件。 |
| `produce_open_world_winners.py` | Generate auditable first-tier winners from full open-world runs. |
| `produce_real_winners.py` | Legacy seven-battle combat-only sampler. |
| `real_playthrough.py` | Real full playthrough via GameEngine.execute_action public API only. |
| `reapply_lessons_after_reset.py` | 在知识库重置后恢复方法论提示与有证据的经验，不回填旧版成绩。 |
| `relic_winrate.py` | 用当前真实 GameEngine 比较 11 件开局遗物。 |
| `self_preserve_experiment.py` | 自保时钟实验：132 局前后对照 + 「回复打在谁身上」归因审计。 |
| `start_cycle_showcase.py` | 开始一次轮回：真实引擎开局 + 真实战斗结算，逐时点展示本轮实现的『新反应法术』实战触发。 |
| `story_context.py` | 故事文档「剧情存档」的自动整理：索引、条目调阅与角色知识视图。 |
| `stress_test.py` | 压力测试驱动器（2026-08-19，禁止修改战斗逻辑阶段）。 |
| `tactics_knowledge.py` | 战术知识库工具（战报驱动：根据对局结果持续更新优化战术，淘汰过时打法）。 |
| `test_chenglu_blood_splash.py` | 测试【承露盏】+【再生/透支/杀伐】+三阶法术【血溅五步】。 |
| `threshold_evidence.py` | 保险丝阈值的实测依据（Phase 5）。 |
| `tournament.py` | 淘汰赛：让胜者池两两死斗，直到只剩最后一人。 |
| `trace_run.py` | 完整轮回战报生成器：从第1场跑到通关/死亡，记录开局、每场局外行动、遗物、逐回合战斗。 |
| `victory_path_tournament.py` | 胜利路径锦标赛（2026-08-22 DM裁定：把各种胜利方法的利弊和胜率都列出来）。 |
| `wage_settlement.py` | 共享员工工资结算助手。 |
| `win_only_ai.py` | 胜负唯一计分 AI（用户裁定 2026-09-10：胜 +1 / 败 −1，无论什么手段什么战术； |

### 回放与回归脚本（2）

| 文件 | 说明 |
| --- | --- |
| `run_open_world_replay.py` | Run and export one deterministic, engine-backed open-world game. |
| `run_sim.py` | 7场全通关率模拟器（一阶副本） |

### 对局模拟与对比（批量跑局、产出统计/战报）（17）

| 文件 | 说明 |
| --- | --- |
| `authentic_asymmetric_duel.py` | 真正不对称、真正各具世界观与流派底牌的跨副本巅峰死斗： |
| `b19_duel_lab.py` | 第十九批 Part3：死斗专项实验室（与PVE学习完全解耦；禁止用PVE均分做代理）。 |
| `breed_and_duel.py` | 『养蛊』：用当前引擎真实跑一阶 7 场，抓出通关胜者，再用两胜者做第 8 场死斗。 |
| `duel_chronicle.py` | 死斗逐手实录器（2026-09-10）：只记**真实结算**的每个动作，预演世界一律排除。 |
| `duel_common.py` | 死斗对称交替驱动（修复守擂方机制性必胜 bug 后的公平驱动）。 |
| `duel_dialogue.py` | PvP 死斗对白渲染（2026-08-30）。 |
| `duel_diff_trace.py` | 死斗逐次 diff 追踪器（诊断工具）。 |
| `duel_pvp.py` | 真正的 PvP 死斗驱动（守擂方按轮回者规则行动）。 |
| `duel_records.py` | 死斗详细过程记录器：把每一局死斗的逐手过程 + 双方终局账面写成 markdown（报告.md 数据源）。 |
| `generate_duel_perspective_report.py` | 真实死斗冒烟：生成一份同时含【当事人视角】与【后台审计数据】的完整死斗战报。 |
| `generate_duel_report.py` | 真正不对称、真正各具世界观与流派底牌的巅峰死斗推演与战报生成器： |
| `high_iq_duel.py` | 高智商、高烈度、真正机制博弈的跨副本死斗推演系统： |
| `inter_dungeon_duel.py` | 跨副本跨流派真实手操推演与死斗巅峰决战： |
| `monster_arena.py` | 养蛊场：让怪物互斗，用演化把「通用怪物 AI」的权重练出来。 |
| `multi_reincarnation_duel.py` | 多世代真实手操推演与死斗胜者经验沉淀系统： |
| `produce_duel_winners.py` | 产出「经历过死斗」的二阶候选人（用户要求）。 |
| `run_wen_duel.py` | 真实引擎七场清场与封存候选人死斗运行器。 |

### 生成器（产出索引/知识库等派生文件）（5）

| 文件 | 说明 |
| --- | --- |
| `build_learner.py` | 自学习流派优化器：让 AI 通过多次轮回自己发现哪些道纹组合 1+1>2。 |
| `build_winrate.py` | 流派胜率对比：测试不同道纹组合（杀伐系 / 波及系 / 各副本专属）的实战表现。 |
| `gen_ai_context.py` | 生成第四宇宙剧情优先的混合 AI 上下文。 |
| `gen_daowen_index.py` | 生成 全道纹索引.md（一次性工具：道纹数据源变更后重跑即可）。 |
| `gen_rules.py` | 从结构化事实源（data/rules/*.toml）生成《死者之书.md》的「## 规则」节。 |

## 怎么跑

```bash
python3 sim/gen_rules.py --check          # 规则正文 ↔ 引擎常量（CI 必跑）
python3 sim/run_open_world_replay.py      # 开放世界整局回放（CI 必跑，产物在 data/open_world_replay/）
python3 sim/probe_damage_pipeline_triggers.py   # 例：某个探针
```

> 回放产物里的 `collected_by` 等字段是运行时 uuid，**每次重跑都会变**，
> 所以 `data/open_world_replay/*` 不适合当逐字节基准；比对请用回放校验测试
> （`tests/test_open_world_replay_validation.py`）。

