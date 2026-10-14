"""Reproducible unit economics with declared inputs and Decimal arithmetic.
This is NOT a fitted estimate: all prices and request shares are assumptions.
Usage: python model/cost_model.py model/inputs.json
"""
from __future__ import annotations
import json, sys
from decimal import Decimal, localcontext
from pathlib import Path

def D(x):
    return Decimal(str(x))

def model(p: dict) -> dict:
    with localcontext() as c:
        c.prec = 40
        n = D(p['client_requests'])
        assert n > 0
        a = D(p['deduped_origin_attempts'])
        assert a >= 0
        fixed = D(p['server_provisioning_usd']) + D(p['storage_logs_monitoring_usd'])
        bandwidth = D(p['egress_gb']) * D(p['egress_usd_per_gb'])
        proxy = D(p['proxy_billable_gb']) * D(p['proxy_usd_per_gb'])
        browser = D(p['browser_compute_usd'])
        other = a * D(p['other_variable_usd_per_origin_attempt'])
        total = fixed + bandwidth + proxy + browser + other
        norm = D(1_000_000) / n
        cost_million = total * norm
        retail = D(p['gross_sale_price_per_million_usd'])
        return { 'label':p['label'], 'request_count':str(n),
            'origin_attempts':str(a),
            'attempts_per_client_request':str(a/n),
            'fixed_usd':str(fixed), 'egress_usd':str(bandwidth),
            'proxy_usd':str(proxy), 'browser_usd':str(browser),
            'other_usd':str(other), 'total_period_cogs_usd':str(total),
            'cogs_usd_per_million':str(cost_million),
            'gross_margin_fraction':str((retail-cost_million)/retail) if retail>0 else None,
            'gross_profit_per_million_usd':str(retail-cost_million),
            'amortization_monthly_rps':str(n/(D(p['period_days'])*D(86400)))
        }

if __name__ == '__main__':
    path = Path(sys.argv[1]) if len(sys.argv)>1 else Path(__file__).with_name('inputs.json')
    print(json.dumps(model(json.loads(path.read_text())), indent=2))
