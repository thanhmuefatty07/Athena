"""Paired offline release benchmarks. Run with python -m model.benchmark.
Module execution keeps model/statistics.py separate from Python's statistics library.
No network or production latency claims.
"""
import argparse
import hashlib
import json
import platform
import statistics
import subprocess
from pathlib import Path


def compare(baseline: Path, candidate: Path, rounds: int) -> dict:
    suffix = '.exe' if platform.system() == 'Windows' else ''
    cases = [('rank', 1000, 40), ('table', 5000, 40), ('parse', 1000, 20),
             ('export', 1000, 20), ('pipeline', 1000, 20)]
    generated = baseline.parent / 'benchmark-fixture.html'
    paragraphs = []
    for i in range(1000):
        term = 'tokio runtime' if i % 17 == 0 else 'unrelated topic'
        text = (f'Document {i}: CPU memory network latency throughput. ' +
                'ordinary multilingual tài liệu 内容 details and reproducible evidence ' * 8 + term)
        paragraphs.append(f"<p>{text}<a href='/source/{i}'>source</a></p>")
    generated.write_text(''.join(paragraphs), encoding='utf-8')
    many_links = baseline.parent / 'many-links.html'
    many_links.write_text('<p>' + ''.join(f"<a href='/a/{i}'>link {i} </a>" for i in range(20_000)) + '</p>',
                          encoding='utf-8')
    equivalent = []
    fixtures = [Path('fixtures/example.html').resolve(), generated.resolve(), many_links.resolve()]
    for fixture in fixtures:
        for query in [None, 'tokio runtime', 'tài 内容', 'H100 pricing']:
            outputs = []
            for directory in [baseline, candidate]:
                args = [str(directory / ('athena-core-v2' + suffix)), str(fixture), 'https://example.org/']
                if query is not None:
                    args.append(query)
                outputs.append(subprocess.run(args, check=True, capture_output=True, timeout=120).stdout)
            if outputs[0] != outputs[1]:
                raise ValueError(f'{fixture.name}, {query}: AAE bytes differ; review before benchmarking')
            equivalent.append({'fixture': fixture.name, 'query': query,
                               'sha256': hashlib.sha256(outputs[0]).hexdigest(),
                               'bytes': len(outputs[0])})
    results = []
    for stage, size, iterations in cases:
        samples = {'baseline': [], 'candidate': []}
        for trial in range(rounds):
            order = [('baseline', baseline), ('candidate', candidate)]
            if trial % 2:
                order.reverse()
            for label, directory in order:
                process = subprocess.run(
                    [str(directory / ('bench_core' + suffix)), stage, str(size), str(iterations)],
                    check=True, capture_output=True, text=True, timeout=120)
                samples[label].append(json.loads(process.stdout))
        sizes = {v['output_size'] for group in samples.values() for v in group}
        if len(sizes) != 1:
            raise ValueError(f'{stage}: output sizes differ; reject comparison')
        summary = {}
        for label, rows in samples.items():
            medians = [v['p50_ms'] for v in rows]
            summary[label] = {
                'median_p50_ms': statistics.median(medians),
                'min_p50_ms': min(medians), 'max_p50_ms': max(medians),
                'median_p95_ms': statistics.median(v['p95_ms'] for v in rows)}
        reduction = (1 - summary['candidate']['median_p50_ms'] /
                     summary['baseline']['median_p50_ms']) * 100
        results.append({'stage': stage, 'size': size, 'iterations_per_round': iterations,
                        'rounds': rounds, 'summary': summary,
                        'median_p50_reduction_percent': reduction, 'raw': samples})
    for stage, fixture, size, iterations in [('pipeline_no_query', generated, 1000, 20),
                                            ('many_links_no_query', many_links, 20_000, 10)]:
        samples = {'baseline': [], 'candidate': []}
        for trial in range(rounds):
            order = [('baseline', baseline), ('candidate', candidate)]
            if trial % 2:
                order.reverse()
            for label, directory in order:
                args = [str(directory / ('bench_html' + suffix)), str(fixture.resolve()),
                        'https://example.org/', str(iterations)]
                output = subprocess.run(args, check=True, capture_output=True, text=True, timeout=120)
                samples[label].append(json.loads(output.stdout))
        summary = {
            label: {'median_p50_ms': statistics.median(r['p50_ms'] for r in rows),
                    'median_p95_ms': statistics.median(r['p95_ms'] for r in rows),
                    'min_p50_ms': min(r['p50_ms'] for r in rows),
                    'max_p50_ms': max(r['p50_ms'] for r in rows)}
            for label, rows in samples.items()}
        results.append({'stage': stage, 'size': size,
                        'iterations_per_round': iterations, 'rounds': rounds,
                        'summary': summary, 'raw': samples,
                        'median_p50_reduction_percent': (1 - summary['candidate']['median_p50_ms'] /
                                                        summary['baseline']['median_p50_ms']) * 100})
    return {'scope': 'warm offline CPU stages; setup excluded; input generation fixed in bench_core',
            'platform': platform.platform(),
            'rustc': subprocess.run(['rustc', '--version', '--verbose'], check=True, capture_output=True,
                                   text=True, timeout=30).stdout,
            'source_revision': subprocess.run(['git', 'rev-parse', 'HEAD'], check=True, capture_output=True,
                                             text=True, timeout=30).stdout.strip(),
            'artifacts': {label: {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                                  for name in ['athena-core-v2' + suffix, 'bench_html' + suffix,
                                               'bench_core' + suffix, 'Cargo.lock']}
                          for label, directory in [('baseline', baseline), ('candidate', candidate)]},
            'fixtures': {fixture.name: {'bytes': fixture.stat().st_size,
                                       'sha256': hashlib.sha256(fixture.read_bytes()).hexdigest()}
                         for fixture in fixtures},
            'output_equivalence': equivalent, 'results': results}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('baseline', type=Path)
    parser.add_argument('candidate', type=Path)
    parser.add_argument('--rounds', type=int, default=7)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not 3 <= args.rounds <= 30:
        parser.error('rounds must be in 3..30')
    report = compare(args.baseline.resolve(), args.candidate.resolve(), args.rounds)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({r['stage']: r['summary'] for r in report['results']}, indent=2))
