"""M/M/c queue ONLY under Poisson arrivals and exponential service times.
Use actual measurements/simulation for bursty origins and heavy-tailed browsers.
"""
from __future__ import annotations
import math

def erlang_c(arrival_rps: float, service_rps_per_worker: float, workers: int) -> dict:
    if (not isinstance(workers, int) or isinstance(workers, bool) or workers < 1
            or not math.isfinite(service_rps_per_worker) or service_rps_per_worker <= 0
            or not math.isfinite(arrival_rps) or arrival_rps < 0):
        raise ValueError('Invalid parameters')
    a = arrival_rps / service_rps_per_worker
    rho = a / workers
    if rho >= 1:
        return {'stable':False, 'utilization':rho, 'p_wait':1.0,
                'mean_queue_ms': math.inf, 'p95_queue_ms':math.inf}
    # Erlang-B recurrence followed by C = B/(1-rho+rho*B); O(1) storage.
    blocking = 1.0
    for i in range(1, workers + 1):
        offered_blocked = a * blocking
        blocking = offered_blocked / (i + offered_blocked)
    p_wait = blocking / (1 - rho + rho * blocking)
    spare=workers*service_rps_per_worker-arrival_rps
    mean_queue_s=p_wait/spare
    p95_queue_s=max(0., math.log(p_wait/0.05)/spare) if p_wait>0.05 else 0.
    return {'stable':True,'utilization':rho,'p_wait':p_wait,
            'mean_queue_ms':mean_queue_s*1000,
            'p95_queue_ms':p95_queue_s*1000}

if __name__=='__main__':
    import json
    for w in (4,8,16,32): print(w,json.dumps(erlang_c(50,10,w),indent=None))
