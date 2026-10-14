"""Wilson interval with z=1.959963984540054 for independent Bernoulli observations.
Warning: origin-level/campaign-level correlation needs clustered intervals.
"""
from math import sqrt, log, ceil

def wilson(successes: int, total: int, z: float = 1.959963984540054):
    if total <= 0 or not (0 <= successes <= total): raise ValueError('Invalid count')
    p=successes/total
    denom=1+z*z/total
    center=(p+z*z/(2*total))/denom
    half=z/denom*sqrt(p*(1-p)/total+z*z/(4*total*total))
    return (max(0.,center-half), min(1.,center+half))

def min_perfect_trials_for_lower_success(target:float=0.999,confidence:float=0.95):
    if not (0<target<1 and 0<confidence<1): raise ValueError('Bad probability')
    # One-sided exact Clopper-Pearson bound given zero failures.
    return ceil(log(1-confidence)/log(target))

if __name__=='__main__':
    print('n_min=',min_perfect_trials_for_lower_success())
    print('Wilson(995/1000)=',wilson(995,1000))
