"""相反的持续约束按后生效覆盖；不改变道纹自然窗口的结算顺序。

这里只登记已明确共享同一输出的约束。伤害算术/支付/离场并非自动判为相反；
尚未完成业务定义的道纹不能仅靠登记一个名字就冒充实装。
"""
STATUS_DOMAINS = {
    '全力': frozenset({'attack_power'}),
    '龙族利爪': frozenset({'attack_power'}),
    '全速': frozenset({'attack_count'}),
    '手雷减攻': frozenset({'attack_count'}),
    '定型': frozenset({'attack_power', 'attack_count'}),
}


def sequence(status, index):
    return getattr(status, 'effect_sequence', 0) or index + 1


def ordered_effects(entity, domain):
    return [status for _, status in sorted(
        ((sequence(status, index), status) for index, status in enumerate(entity.status_effects)
         if not status.is_expired and domain in STATUS_DOMAINS.get(status.name, ())),
        key=lambda item: item[0])]


def crossed_opposition(entity, existing, incoming):
    domains = STATUS_DOMAINS.get(incoming.name, frozenset())
    if not domains:
        return False
    existing_index = next(i for i, status in enumerate(entity.status_effects) if status is existing)
    prior = sequence(existing, existing_index)
    return any(not status.is_expired and status.name != incoming.name
               and domains.intersection(STATUS_DOMAINS.get(status.name, ()))
               and sequence(status, index) > prior
               for index, status in enumerate(entity.status_effects))


def resolve(entity, domain, initial, handlers):
    """只在同一明确冲突域内按施加先后折叠，不比较X/资源/∞。"""
    result = initial
    for status in ordered_effects(entity, domain):
        handler = handlers.get(status.name)
        if handler is not None:
            result = handler(status, result)
    return result
