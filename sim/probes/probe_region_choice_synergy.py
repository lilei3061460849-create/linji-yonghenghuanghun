"""探针：开局拿到【回锋刀】/【避风铃】后，AI 会不会为了【超频】【搏命】进扭曲都市？

结论（2026-09-13 首测）：**不会，而且不止是"没选"，是压根没有这个决策。**
后续（同日修复，见 commit c563bfc 与本轮）：两条断链都已接上，本探针留作回归基线。

1) 选区曾是硬编码。engine/ai_player.py::PlaceholderBackend.decide 中
   `setup_choose_region` 固定返回"罪孽都市"，不读遗物/属性/任何状态。
   首测 29 个"开局确实拿到回锋刀或避风铃"的样本，选扭曲都市 0 次。
   **已修**：改为按已持有遗物与各区专属道纹的协同度打分，同样 29 个样本
   全部改选扭曲都市（29/29）；「买路财 → 罪孽都市」是数据驱动自发涌现的。

2) 即便身处扭曲都市，也不会定向获取这两个道纹。副本专属道纹必须先经残韵
   从本副本怪物身上转化获得，而 ai_tactics._resonance_candidates 的打分只看
   **敌方道纹的威胁度**（damage/control/debuff 权重 × 威胁占比），从不评估
   "转化结果对我的构筑有没有用"。
   **已修**：残韵打分补上"我得到了什么"这一半（专属道纹加权、已持有归零）。
   40 seed/区对照：专属道纹获取合计 16 → 25（+56%），种类 6 → 11。
   该收益项此前只在 TacticalAI 生效，WinOnlyAI（默认路径）把它丢弃；
   现已接入 WinOnlyAI 的提案裁剪排序（最终裁决仍只认 ±1 结局）。

协同本身是成立的：遗物在选区之前就已确定（见
engine/handlers/setup.py::handle_setup_choose_region 的前置校验），
【搏命】代价疲惫X → 回锋刀每失速1点打3伤、避风铃速度归零得15格挡，
信息与时序都允许做这个联动。

2026-10-14：【搏命】删除并入【透支】（通用核心）；选区协同改走产出边
（扭曲专属【超频】产速度），回锋刀/避风铃仍导向扭曲都市，行为保持。

用法：python3 sim/probes/probe_region_choice_synergy.py
"""

# --- 2026-11-04：脚本放进子目录后，不再靠"往上数几层"猜仓库根 ---
# 向上找到同时含 engine/ 与 sim/ 的目录就是仓库根；再附带 sim/ 让老式裸模块名
# import（如 `import balance_sim`）继续可用。搬动本文件不需要再改这几行。
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.abspath(__file__))
while _ROOT != _os.path.dirname(_ROOT) and not _os.path.isdir(_os.path.join(_ROOT, "engine")):
    _ROOT = _os.path.dirname(_ROOT)
for _p in (_ROOT, _os.path.join(_ROOT, "sim"), _os.path.join(_ROOT, "tests")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from engine.api import GameEngine
from engine.ai_player import PlaceholderBackend
import collections
picks=collections.Counter(); speedpicks=collections.Counter(); got=0
be=PlaceholderBackend()
for seed in range(1,61):
    e=GameEngine(db_path=f"/tmp/rpx_{seed}.db", rng_seed=seed)
    r=e.execute_action("setup_attributes",{"name":"探针","blood_points":11,"speed_points":8,"mana_points":6})
    if not r.get("success"): continue
    cands=e.get_state()["state"].get("pending_relic_choices") or []
    if not cands: continue
    pick=next((c for c in cands if c in ("回锋刀","避风铃")), None)
    if pick is None: continue
    got+=1
    e.execute_action("choose_discovered_relic",{"relic_name":pick})
    ch=e.get_state()["state"].get("pending_initial_daowen_choices") or []
    if ch: e.execute_action("setup_choose_initial_daowen",{"daowen_name":ch[0]})
    e.execute_action("setup_choose_resonance",{"resonance_type":"转换"})
    be.engine=e
    d=be.decide(e.get_state(), e.get_available_actions(), "")
    if d.action_type=="setup_choose_region":
        picks[(pick,d.params["region"])]+=1; speedpicks[d.params["region"]]+=1
print(f"开局拿到 回锋刀/避风铃 的样本数: {got}")
print("（遗物, AI选的副本）→次数")
for k,v in picks.most_common(): print("   ",k,"→",v)
print("\n选区分布:", dict(speedpicks))
print("扭曲都市占比:", f"{speedpicks.get('扭曲都市',0)}/{sum(speedpicks.values())}")