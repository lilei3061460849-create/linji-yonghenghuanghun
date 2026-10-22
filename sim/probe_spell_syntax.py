import sys
sys.path.insert(0,'.'); sys.path.insert(0,'tests')
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
