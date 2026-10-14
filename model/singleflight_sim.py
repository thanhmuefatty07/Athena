"""Synthetic singleflight savings: same-key arrivals overlap only while fetch is active.
Uses seeded synthetic trace; not a measured workload or a production capacity model.
"""
import random

def simulate(n=100_000, url_count=1000, arrival_rps=2000.,
             fetch_ms=80., seed=2026):
    random.seed(seed)
    active_until = {}
    t = 0.
    fetched = 0
    joined = 0
    for _ in range(n):
        t += random.expovariate(arrival_rps)
        url = random.randrange(url_count)
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
