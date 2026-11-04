
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

import sys
from engine.spell_dsl import parse_spell_definition, SpellDslError
from engine.daowen import DaoWenEngine
DaoWenEngine.register_all()
ALL=set(DaoWenEngine.list_all())
C=[
("战始蓄力","战始","发动透支X于自身→循环"),
("回始蓄力","回始","发动透支X于自身→发动再生X于自身→循环"),
("盾墙","受到伤害前","发动庇护X于自身→循环"),
("透支换盾","受到伤害前","发动透支X于自身→发动庇护X于自身→循环"),
("反击循环","失去生命后","发动再生X于自身→发动透支X于自身→发动杀伐X于攻击者→循环"),
("连击周天","失去生命后","发动再生X于自身→发动透支X于自身→发动连击X于攻击者→循环"),
("贯穿开场","战始","发动透支X于自身→发动贯穿X于自身"),
("增殖周天","失去生命后","发动再生X于自身→发动透支X于自身→发动增殖X于自身→循环"),
("条件止血","失去生命后","若生命<20 则 发动再生X于自身 否则 发动杀伐X于攻击者"),
("洞察流","敌回始","发动洞察X于任意目标"),
("固执保命","受到伤害前","发动固执X于自身"),
("束缚控场","敌回始","发动束缚X于任意目标"),
]
for n,t,f in C:
    try:
        p=parse_spell_definition(t,f,ALL)
        print(f"✅ {n:8s} | {p.trigger:8s} loop={str(p.loop):5s} | {f}")
    except SpellDslError as e:
        print(f"❌ {n:8s} | {e}")