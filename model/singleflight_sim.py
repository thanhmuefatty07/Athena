"""Synthetic singleflight savings: same-key arrivals overlap only while fetch is active.
Uses seeded synthetic trace; not a measured workload or a production capacity model.
"""
import math
import random

def simulate(n=100_000, url_count=1000, arrival_rps=2000.,
             fetch_ms=80., seed=2026):
    if (not isinstance(n, int) or isinstance(n, bool) or n < 1
            or not isinstance(url_count, int) or isinstance(url_count, bool) or url_count < 1
            or not math.isfinite(arrival_rps) or arrival_rps <= 0
            or not math.isfinite(fetch_ms) or fetch_ms < 0):
        raise ValueError('Invalid simulation parameters')
    rng = random.Random(seed)
    active_until = {}
    t = 0.
    fetched = 0
    joined = 0
    for _ in range(n):
        t += rng.expovariate(arrival_rps)
        url = rng.randrange(url_count)
        if active_until.get(url, -1.) > t:
            joined += 1
        else:
            fetched += 1
            active_until[url] = t + fetch_ms / 1000.
    return {'arrivals':n,'fetches':fetched,'joined':joined,
            'coalescing_ratio':joined/n, 'origin_calls_per_client_request':fetched/n}

if __name__=='__main__':
    import json
    for num in (100,1000,10000):
        print('url_count=',num,json.dumps(simulate(url_count=num)))
