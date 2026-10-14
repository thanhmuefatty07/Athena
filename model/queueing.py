"""M/M/c queue ONLY under Poisson arrivals and exponential service times.
Use actual measurements/simulation for bursty origins and heavy-tailed browsers.
"""
from __future__ import annotations
import math

def erlang_c(arrival_rps: float, service_rps_per_worker: float, workers: int) -> dict:
    if workers < 1 or service_rps_per_worker <= 0 or arrival_rps < 0:
        raise ValueError('Invalid parameters')
    a = arrival_rps / service_rps_per_worker
    rho = a / workers
    if rho >= 1:
        return {'stable':False, 'utilization':rho, 'p_wait':1.0,
                'mean_queue_ms': math.inf, 'p95_queue_ms':math.inf}
    # Log-sum-exp avoids factorial overflow when workers is large.
    log_terms=[i*math.log(a)-math.lgamma(i+1) if a>0 else (0. if i==0 else -math.inf)
               for i in range(workers)]
    tail=(workers*math.log(a)-math.lgamma(workers+1)-math.log1p(-rho)
          if a>0 else -math.inf)
    top=max([tail,*log_terms])
    den=sum(math.exp(v-top) for v in log_terms)+math.exp(tail-top)
    p_wait=math.exp(tail-top)/den
    spare=workers*service_rps_per_worker-arrival_rps
    mean_queue_s=p_wait/spare
    p95_queue_s=max(0., math.log(p_wait/0.05)/spare) if p_wait>0.05 else 0.
    return {'stable':True,'utilization':rho,'p_wait':p_wait,
            'mean_queue_ms':mean_queue_s*1000,
            'p95_queue_ms':p95_queue_s*1000}

if __name__=='__main__':
    import json
    for w in (4,8,16,32): print(w,json.dumps(erlang_c(50,10,w),indent=None))
