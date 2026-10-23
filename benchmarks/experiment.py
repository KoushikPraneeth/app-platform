"""Matched baseline/optimized suites. Capacity and cache evidence are kept separate."""
import argparse
import json
import statistics

from audit import OUT, now, save
from environment import psql
from runner import case, configure, guard, reset_data


def database_snapshot(label):
    if (OUT / (label + '-dataset.json')).exists():
        print('Preserve original saved database snapshot: ' + label, flush=True)
        return
    p = psql("SELECT row_to_json(t) FROM (SELECT count(*) AS rows, min(id) AS minimum_id, max(id) AS maximum_id, md5(string_agg(md5(concat_ws('|',id,username,community,content,upvotes,created_at)),'' ORDER BY id)) AS dataset_hash FROM items) t;")
    save(label + '-dataset.json', json.loads(p.stdout))
    plan = psql("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) SELECT id,username,community,content,upvotes,created_at,title,author FROM items ORDER BY created_at DESC LIMIT 25;")
    save(label + '-query-plan.json', json.loads(plan.stdout))
    stats = psql("SELECT row_to_json(t) FROM (SELECT numbackends,xact_commit,blks_read,blks_hit,tup_returned,tup_fetched,temp_files,deadlocks FROM pg_stat_database WHERE datname=current_database()) t;")
    save(label + '-database-statistics.json', json.loads(stats.stdout))


def calibrate(label, cache, mode):
    prior = OUT / (label + '-calibration.json')
    if prior.exists():
        saved = json.loads(prior.read_text())
        assert saved['highest_passing_tested_rate']
        print('Reuse completed calibration: ' + label, flush=True)
        return saved['highest_passing_tested_rate']
    configure(cache); reset_data(); results = []
    for rate in [5, 10, 25, 50, 100, 200, 400, 800]:
        result = case(label + '-cal-' + str(rate), rate, seconds=40, mode=mode, cache=cache, warmup=10)
        results.append(result)
        if not result['thresholds_passed'] or not result['measurement_valid']:
            break
    passing = [r['configuration']['offered_iterations_per_second'] for r in results if r['thresholds_passed'] and r['measurement_valid']]
    save(label + '-calibration.json', {'results': results, 'highest_passing_tested_rate': max(passing) if passing else None,
                                     'limit': 'Short calibration selects measured loads; it is not the sustained-capacity headline.'})
    if not passing:
        raise RuntimeError('Even the lowest offered rate missed predefined thresholds. Inspect generator, network and application before proceeding; do not quietly relax the SLO.')
    return max(passing)


def suite(label):
    guard(); configure(False); reset_data(); database_snapshot(label + '-initial')
    uncached_rate = calibrate(label + '-uncached', False, 'read')
    mixed_rate = calibrate(label + '-mixed', True, 'mixed')
    # Run both cache conditions at the SAME offered load and alternate their order.
    paired = []
    for repeat in range(1, 4):
        for cache in ([True, False] if repeat % 2 else [False, True]):
            configure(cache); reset_data()
            result = case(f'{label}-ab-{repeat}-' + ('on' if cache else 'off'), uncached_rate, seconds=120, mode='read', cache=cache)
            paired.append(result)
    sustained = []
    configure(True)
    for repeat in range(1, 4):
        reset_data()
        sustained.append(case(f'{label}-sustained-{repeat}', mixed_rate, seconds=180, mode='mixed', cache=True))
    matched = []
    if label == 'after':
        baseline = json.loads((OUT / 'before-suite.json').read_text())
        baseline_rate = baseline['sustained_mixed_offered_rate']
        if baseline_rate == mixed_rate:
            matched = sustained
        else:
            for repeat in range(1, 4):
                reset_data()
                matched.append(case(f'after-matched-{repeat}', baseline_rate, seconds=180, mode='mixed', cache=True))
    reset_data(); database_snapshot(label + '-final')
    report = {'label': label, 'finished_utc': now(), 'cache_ab_offered_rate': uncached_rate,
              'sustained_mixed_offered_rate': mixed_rate, 'cache_pairs': paired, 'sustained': sustained,
              'sustained_all_repeats_passed': all(r['thresholds_passed'] and r['measurement_valid'] for r in sustained),
              'sustained_median_successful_rps': statistics.median(r['successful_requests_per_second'] for r in sustained),
              'matched_baseline_load_results': matched,
              'limits': ['One AKS cluster and one external generator location', 'Synthetic 10,000-record fixture; 90/10 read/create mix',
                         'Short calibration selects the rate; repeat results determine whether it is sustainable',
                         'Cache A/B is a separate read-only experiment at identical offered load; include failures with latency']}
    save(label + '-suite.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('label', choices=['before', 'after'])
    suite(parser.parse_args().label)
