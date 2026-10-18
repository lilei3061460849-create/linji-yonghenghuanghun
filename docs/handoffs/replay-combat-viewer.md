# 战局回放开发交接（历史记录）

> 当前状态来源：PR #67 及其合并后的 `main`。本文保留首版实现边界与验证背景，不能把下方的历史“下一步”当作当前待办；新工作请使用 GitHub Issues/PR。

- 目标：实现手机优先的极简战局回放。战斗双方保持在左右两个角色格中，地图下方按真实事件顺序展示攻击、闪避、伤害、治疗、状态变化和死亡。
- 分支：`replay/combat-viewer`
- 基线：`main` 起始提交 `603fbdcb157cb920a560d4aa2a38b48ce7f1b86b`
- PR：https://github.com/lilei3061460849-create/linji-yonghenghuanghun/pull/67
- 已实现：`engine/replay.py` 可将 `GameEngine` 或 `GameState` 中的既有 `combat_events` 导出为 UTF-8 JSON；`replay/index.html` 提供手机响应式事件回放页；`replay/README.md` 记录使用方式；`tests/test_replay.py` 覆盖事件顺序、JSON 安全转换、空事件流和导出文件。
- 设计边界：不改战斗规则；回放不重跑 AI、随机数或伤害；首版尚未接入地图坐标、世界边界收缩与 MP4 导出。
- 验证：在临时工作目录中，4 个回放导出专项测试通过；Python 语法检查通过；播放器脚本的 Node 语法检查通过。尚未在完整仓库环境中运行 pytest 全套，也未进行手机浏览器实机验证。
- 下一步：在实际游戏运行代码中调用 `from engine.replay import export_replay; export_replay(engine, "replay.json")`，然后将 `replay/index.html` 放在手机可访问的静态网页环境中并导入 JSON。后续再接地图状态和移动事件。
