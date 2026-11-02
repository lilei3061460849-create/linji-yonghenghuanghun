# 六项挂起机制迁移审计（2026-10-13）

范围仅为内部迁移，不改数值、选择协议、封存口径或阵营规则。下表先登记映射，验收后补结果。

| 机制 | when | target / condition | effect | priority / state |
| --- | --- | --- | --- | --- |
| 血族血脉 | ROUND_END_POST_MEDIOCRITY：凡庸后原第二实体循环 | SELF；轮回者且 side_has（保留旧代码不另查封存） | 有伤害则原 apply_heal；否则统一 cost 流血20，原玩家平分选择透传 | 10；原 damage_dealt_this_round、无新状态 |
| 赤族诅咒 | 同上，逐实体原序 | SELF；赤族且存活 | 模型 take_damage(20,代价)，原生命损失来源及命零判定；不引入支付遗物联动 | 20；无新状态 |
| 皮衣·获得格挡 | ROUND_START_PREPARE：回始原最前段 | PLAYER；原 side_has | 先 pop 原 event_modifiers 键，再按持有者决定是否 gain_shield | 10；原 leather_shield_next |
| 皮衣·记录 | ROUND_END_TRACKING：状态 tick / 龙躯计数后、原计账清理前 | PLAYER；原 side_has | 按原 HP loss 事件优先、旧计账后备；写原 modifier | 10；原存储、不改封存处理 |
| 防弹插板 | BATTLE_START_AFTER_CHOICES：烙痕钉之后原盟友循环 | SELF；仅 friends+employees 原列表，存活且实体自持 | shield +15，每实体一条原日志 | 10；无新状态 |
| 龙族血脉·乘区 | 已有 MULTIPLIER_ADJUST，经原 Hook 桥 | 受伤目标；来源 side_has、非怪物、非代价 | amount ×2；斩杀仍在旧硬控制流 | 10；无新状态 |
| 三相残韵盘·战终 | 已有 BATTLE_END，缄默面具之后 | PLAYER；原本局遗物和封存判定，已消耗残韵 | 原另外两种各+1和原日志 | 10；复用已随 combat_runtime 存档/回滚的 sanxiang，不另造机制私有状态 |

## 通用抽象与消费者

- 后段分发仍是已有 `_dispatch_phase`，新增时点必须只有一个原位调用点；凡庸后的相位有血族/赤族两个消费者，不能挪到旧 ROUND_END。
- 回始前段有事件首回合格挡/皮衣两个消费者；回终追踪窗口有皮衣/原计账清理两个消费者。
- `direct_damage` 是已有 Entity.take_damage 的薄包装，消费者为赤族惩罚和正式流血付款入口。区别于伤害管线、raw hp_loss、代价总线，保持既有语义。
- `mapping` 仅封装现有字典 pop/set/add：皮衣的 modifier 与三相的 resonance 共用；`reset_fields` 复用原回始/回终计账清理。
- 相位附加事实透传复用 TriggerContext.picks：血族的平分选择、三相已经消耗的残韵两个消费者。
- 只补已有 Hook 适配器的 MULTIPLIER 方法；按相位过滤，不能让龙族乘区在 incoming 再执行一次。
- 临时机制状态不迁入未序列化的 Mechanism.state_of；目前 sanxiang 已在真实存档与事务快照中，旧台账的“未序列化”描述过时。

验证要求：参考实现 sweep、封存/死亡/来源上下文、顺序、只触发一次、存档恢复、广泛回归和旧执行路径扫描。A24 用户已说“不用管”；不得为制造全绿改其断言。

## 定向验收

66项新增参考/边界/唯一执行/真实存档回归通过（0.25秒）；含原回血与惩罚上下文、事件父子链、封存/未部署员工、首回合/皮衣追踪清理、乘区不在 incoming 双发。另组合现有回合/死亡/遗物/上下文/死斗测试283项通过（3.15秒）。

首次整合回归：2115 passed、3 failed、7 skipped、14 deselected、6 xfailed（59.28秒）。三条失败均为旧机制成员列表未包含新增龙族乘区/三相战终，已按真实相位过滤/列表更新；相关88项再次通过。最终整合回归见下，不称完整长时基准通过。

## 最终整合验收

**2118 passed / 7 skipped / 14 deselected / 6 xfailed / 0 failed**（58.41秒）。全模块收集，只排除学习器14项完整 play/fitness/evaluate_build 长时模拟；A24原断言未改、未运行，完整长时基准不宣称通过。规则与 AI_CONTEXT 一致性、文档结构、差异空白检查通过。
