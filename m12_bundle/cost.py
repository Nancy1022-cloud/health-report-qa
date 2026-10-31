"""Price estimates with model/region checks; never replace missing usage with zero."""

from urllib.parse import urlsplit

from m12_bundle.analysis import usage_fields


def beijing_endpoint(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ''
    return parsed.scheme == 'https' and (host == 'dashscope.aliyuncs.com' or host.endswith('.cn-beijing.maas.aliyuncs.com'))


def estimate(call, prices):
    fields = usage_fields(call.get('usage'), call['prefix'])
    tiers = prices['models'].get(call.get('model'))
    if not fields['usage_known'] or not tiers or call.get('region') != prices['region']:
        return None
    tier = next((t for t in tiers if fields['input_tokens'] <= t['max_input_tokens']), None)
    if tier is None:
        return None
    return (fields['input_tokens'] * tier['input_per_million'] +
            fields['output_tokens'] * tier['output_per_million']) / 1000000


def costs(calls, prices):
    values = [estimate(call, prices) for call in calls]
    return {'currency': prices['currency'], 'price_checked_at': prices['checked_at'],
            'price_source': prices['price_source'], 'method': prices['method'],
            'priced_calls': sum(value is not None for value in values), 'all_calls': len(values),
            'known_cost_subtotal_yuan': round(sum(value for value in values if value is not None), 8),
            'estimated_total_yuan': round(sum(values), 8) if values and all(v is not None for v in values) else None,
            'billing_reconciled': False, 'note': 'List-price resource estimate, not the account bill.'}
