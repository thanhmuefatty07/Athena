import json
import math
import random
import unittest
from decimal import Decimal
from pathlib import Path

from model.cost_model import model
from model.queueing import erlang_c
from model.singleflight_sim import simulate
from model.statistics import min_perfect_trials_for_lower_success, wilson


class NumericalContracts(unittest.TestCase):
    def test_declared_cost_scenario_is_exact(self):
        params = json.loads(Path(__file__).with_name('inputs.json').read_text())
        result = model(params)
        self.assertEqual(Decimal(result['cogs_usd_per_million']), Decimal('145.40'))
        self.assertEqual(Decimal(result['gross_profit_per_million_usd']), Decimal('-16.40'))

    def test_cost_rejects_invalid_domain_values(self):
        params = json.loads(Path(__file__).with_name('inputs.json').read_text())
        for key, value in [('client_requests', 0), ('period_days', 0),
                           ('egress_gb', '-1'), ('proxy_usd_per_gb', 'NaN')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                model({**params, key: value})

    def test_erlang_c_matches_mm1_closed_form(self):
        result = erlang_c(2, 5, 1)
        self.assertAlmostEqual(result['p_wait'], 0.4)
        self.assertAlmostEqual(result['mean_queue_ms'], 1000 * 2 / (5 * 3))
        self.assertAlmostEqual(result['p95_queue_ms'], 1000 * math.log(8) / 3)
        self.assertEqual(erlang_c(0, 5, 1)['p_wait'], 0)
        self.assertFalse(erlang_c(5, 5, 1)['stable'])

    def test_erlang_c_rejects_nonfinite_input(self):
        for arrival, service, workers in [(math.nan, 5, 1), (1, math.inf, 1), (1, 5, 1.5)]:
            with self.subTest(arrival=arrival, service=service), self.assertRaises(ValueError):
                erlang_c(arrival, service, workers)

    def test_erlang_c_matches_direct_stationary_definition(self):
        for workers in [2, 4, 8, 32]:
            for utilization in [0, 0.2, 0.8, 0.999999]:
                with self.subTest(workers=workers, utilization=utilization):
                    offered = workers * utilization
                    tail = offered ** workers / math.factorial(workers) / (1 - utilization)
                    normalizer = sum(offered ** i / math.factorial(i) for i in range(workers))
                    expected = tail / (normalizer + tail)
                    actual = erlang_c(offered, 1, workers)['p_wait']
                    self.assertTrue(math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-15))

    def test_high_capacity_probability_is_finite_and_bounded(self):
        result = erlang_c(9999, 1, 10_000)
        self.assertTrue(result['stable'])
        self.assertTrue(0 <= result['p_wait'] <= 1)
        self.assertTrue(math.isfinite(result['mean_queue_ms']))

    def test_simulation_does_not_mutate_process_random_state(self):
        random.seed(17)
        before = random.getstate()
        self.assertEqual(simulate(n=100), simulate(n=100))
        self.assertEqual(random.getstate(), before)

    def test_simulation_validates_parameters(self):
        for kwargs in [{'n': 0}, {'url_count': 0}, {'arrival_rps': math.nan}, {'fetch_ms': -1}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                simulate(**kwargs)

    def test_bernoulli_bounds_and_exact_trial_count(self):
        self.assertEqual(min_perfect_trials_for_lower_success(), 2995)
        low, high = wilson(995, 1000)
        self.assertAlmostEqual(low, 0.9883490446266244)
        self.assertAlmostEqual(high, 0.9978624644726755)
        for z in [-1, math.nan, math.inf]:
            with self.subTest(z=z), self.assertRaises(ValueError):
                wilson(1, 2, z)


if __name__ == '__main__':
    unittest.main()
