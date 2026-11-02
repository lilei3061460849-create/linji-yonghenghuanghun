# A25 结案：清理模拟脚本的血契旧主动逻辑

日期：2026-10-13。
交付分支：`arena/ab573270-linji-disiyuzhou`；沿用 PR #58。

## 原待办

血契的回始流血4X换X法力早已删除，但部分 sim 脚本仍将它列入需要主动交互的遗物回避集合；旧手操驱动还生成无效的血契回始决策。规则已确定，本项只清理残留，不重新设计效果。

## 已完成

- 从 `sim/alt_win_paths_probe.py`、`sim/daowen_combo_tournament.py`、`sim/combo_loop_audit.py`、`sim/exhaustive_combo_winrate.py` 的交互遗物回避集合中移除血契，发现候选不再因此误跳过它。
- 删除 `sim/handplay_20260821.py::round_start_choices` 中流血换法力的旧血契决策及虚假效果日志。
- 同步可选行动入口与对应测试的当前功能说明。
- 保留血契在合法遗物池中的位置、现有代价平分实验和正式引擎的朋友/员工平分规则；未修改游戏运行规则。
- 从《报告.md》的待办表中移除 A25，结案记录归本文件；未改其他待裁定项目和历史实战数据。

## 验证

新增 `tests/test_sim_blood_pact_cleanup.py` 的8项回归，覆盖四处发现过滤器、遗物池保留、真实手操函数不再生成血契决策、其他回始遗物仍触发及封印判定。

修复前：新增回归7 failed / 1 passed，准确检出了待办残留。
修复后，执行：

```bash
.venv/bin/python -m pytest tests/test_sim_blood_pact_cleanup.py tests/test_blood_pact_and_effect_scope.py tests/test_optional_actions.py tests/test_relic_choice_removal.py -q --tb=short
```

结果：37 passed（0.52秒）；报告选择与文档结构另外9项通过，合计46项定向验证通过。未运行全量回归、学习器或七场长时间模拟。
