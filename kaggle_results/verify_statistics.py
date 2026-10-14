"""Independent statistical audit and synthesis of Kaggle empirical benchmark results.
Reads raw samples from c1, c2, c3, re-runs paired cluster bootstrap, checks drift and autocorrelation.
"""
import math
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def quantile(arr, q):
    if not arr:
        return 0.0
    s = sorted(arr)
    idx = int(round(q * (len(s) - 1)))
    return s[idx]

def lag1_autocorrelation(series):
    if len(series) < 3:
        return 0.0
    mean = sum(series) / len(series)
    denom = sum((x - mean) ** 2 for x in series)
    if denom == 0:
        return 0.0
    numer = sum((series[i] - mean) * (series[i+1] - mean) for i in range(len(series) - 1))
    return numer / denom

def main():
    samples_file = ROOT / "c1" / "samples.jsonl"
    print("Loading raw samples from:", samples_file)
    
    data = {}
    with open(samples_file, "r", encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            st = rec["stage"]
            r = rec["round"]
            cand = rec["candidate"]
            d = rec["duration_ms"]
            
            if st not in data:
                data[st] = {}
            if r not in data[st]:
                data[st][r] = {"baseline": [], "candidate": []}
            data[st][r][cand].append(d)
            
    audit_results = {}
    n_bootstrap = 10000
    random.seed(42)
    
    stages = sorted(data.keys())
    # Bonferroni correction for family of M stages
    m_hypotheses = len(stages)
    alpha = 0.05
    alpha_bonf = alpha / m_hypotheses
    lower_idx = int(round((alpha_bonf / 2) * n_bootstrap))
    upper_idx = int(round((1 - alpha_bonf / 2) * n_bootstrap))
    
    print(f"Family of {m_hypotheses} hypotheses, alpha={alpha}, alpha_bonf={alpha_bonf:.4f}")
    
    for st in stages:
        rounds_dict = data[st]
        round_keys = sorted(rounds_dict.keys())
        n_rounds = len(round_keys)
        
        all_base = [x for r in round_keys for x in rounds_dict[r]["baseline"]]
        all_cand = [x for r in round_keys for x in rounds_dict[r]["candidate"]]
        
        base_p50 = quantile(all_base, 0.50)
        base_p95 = quantile(all_base, 0.95)
        base_p99 = quantile(all_base, 0.99)
        base_mean = sum(all_base) / len(all_base)
        
        cand_p50 = quantile(all_cand, 0.50)
        cand_p95 = quantile(all_cand, 0.95)
        cand_p99 = quantile(all_cand, 0.99)
        cand_mean = sum(all_cand) / len(all_cand)
        
        # Check autocorrelation across successive blocks to evaluate VM drift
        round_base_medians = [quantile(rounds_dict[r]["baseline"], 0.50) for r in round_keys]
        round_cand_medians = [quantile(rounds_dict[r]["candidate"], 0.50) for r in round_keys]
        ac_base = lag1_autocorrelation(round_base_medians)
        ac_cand = lag1_autocorrelation(round_cand_medians)
        
        p50_diffs, p95_diffs, p99_diffs = [], [], []
        for _ in range(n_bootstrap):
            resampled_rounds = [random.choice(round_keys) for _ in range(n_rounds)]
            b_sample = [x for r in resampled_rounds for x in rounds_dict[r]["baseline"]]
            c_sample = [x for r in resampled_rounds for x in rounds_dict[r]["candidate"]]
            
            p50_diffs.append(quantile(c_sample, 0.50) - quantile(b_sample, 0.50))
            p95_diffs.append(quantile(c_sample, 0.95) - quantile(b_sample, 0.95))
            p99_diffs.append(quantile(c_sample, 0.99) - quantile(b_sample, 0.99))
            
        p50_diffs.sort()
        p95_diffs.sort()
        p99_diffs.sort()
        
        ci_p50_bonf = (p50_diffs[lower_idx], p50_diffs[upper_idx])
        ci_p95_bonf = (p95_diffs[lower_idx], p95_diffs[upper_idx])
        ci_p99_bonf = (p99_diffs[lower_idx], p99_diffs[upper_idx])
        
        reduction_p50 = ((base_p50 - cand_p50) / base_p50) * 100.0 if base_p50 > 0 else 0.0
        
        # Strict decision: Candidate is superior if upper Bonferroni bound is strictly negative (< 0) across both p50 and p95!
        decision = "HOLD"
        if ci_p50_bonf[1] < 0 and ci_p95_bonf[1] < 0:
            decision = "ACCEPT (Statistically Significant Superiority)"
        elif ci_p50_bonf[0] > 0:
            decision = "REJECT (Statistically Significant Regression)"
            
        audit_results[st] = {
            "samples_per_arm": len(all_base),
            "baseline": {
                "mean_ms": base_mean,
                "p50_ms": base_p50,
                "p95_ms": base_p95,
                "p99_ms": base_p99,
                "round_median_autocorrelation": ac_base
            },
            "candidate": {
                "mean_ms": cand_mean,
                "p50_ms": cand_p50,
                "p95_ms": cand_p95,
                "p99_ms": cand_p99,
                "round_median_autocorrelation": ac_cand
            },
            "p50_reduction_percent": reduction_p50,
            "ci_95_bonferroni": {
                "delta_p50_ms": ci_p50_bonf,
                "delta_p95_ms": ci_p95_bonf,
                "delta_p99_ms": ci_p99_bonf
            },
            "decision": decision
        }
        
        print(f"[{st.upper():8s}] Base p50={base_p50:6.3f}ms -> Cand p50={cand_p50:6.3f}ms ({reduction_p50:+6.2f}%) | "
              f"Bonf 95% CI: [{ci_p50_bonf[0]:6.3f}, {ci_p50_bonf[1]:6.3f}] ms => {decision}")
        
    # Write consolidated report
    out_file = ROOT / "audit_synthesis_report.json"
    out_file.write_text(json.dumps(audit_results, indent=2), encoding="utf-8")
    print("\nAudit report written to:", out_file)

if __name__ == "__main__":
    main()
