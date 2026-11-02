# 已废止假设与陈旧值档案

## D-01 波及「不足 X 即过滤」旧语义（已废止）

- 来源：AI_EXPERIENCE.md《2026-08-22 版本变更》第 1 条（BUG-01 根因治理）。
- 废止原因：第十四批 DM 裁定①「波及自适应降 X」正式替代（有效X=min(面板X, 合法目标数)）；证据：`tests/test_wave_target_limit_and_phase_recovery` 的回归覆盖与 README:467 裁定注记。旧实验档案已清理。
- 现行规则：以第十四批降 X 语义为准；本词条仅留历史证据，禁止据此实现或教学。

### 原文（2026-08-22，已废止）

### 1. 道纹的 X 必须受合法目标数限制（【波及】BUG-01 根因）
- 怪物侧：`prepare_monster_phase` 中【波及X】的合法目标（存活、可被选中、非自身）不足 X 时，该道纹**不进入 daowen_options**——prepare 不得给出永远无法结算的选项。
- 玩家侧：`use_daowen` 的 X 上限（`_max_legal_daowen_x`）在法力/代价之外，再按存活非自身角色数封顶；朋友/员工固定 X 的【波及】在目标不足时 `available=False` 并给出原因。
- 提交协议不变：【波及】必须显式提交**恰好 X 个**不重复目标的 dodge_targets（玩家侧 `use_daowen`，怪物侧两阶段）；候选全量提交（候选>X）会被拒绝，AI/驱动一律用 `pick_wave_dodge_targets`（恰好 X 个、对侧优先）。


