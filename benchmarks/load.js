import http from 'k6/http';
import exec from 'k6/execution';
import { Counter, Rate, Trend } from 'k6/metrics';

const rate = Number(__ENV.RATE || 10);
const seconds = Number(__ENV.SECONDS || 180);
const warmup = Number(__ENV.WARMUP || 30);
const base = __ENV.BASE_URL;
const failures = new Rate('business_failures');
const successful = new Counter('successful_requests');
const latency = new Trend('successful_duration', true);
const hits = new Rate('cache_hit_fraction');
const cacheContract = new Rate('cache_contract_failures');
http.setResponseCallback(http.expectedStatuses(200, 201));

export const options = {
  scenarios: {
    warmup: { executor: 'constant-arrival-rate', rate, timeUnit: '1s', duration: `${warmup}s`, preAllocatedVUs: 100, maxVUs: 400, gracefulStop: '5s' },
    measured: { executor: 'constant-arrival-rate', rate, timeUnit: '1s', startTime: `${warmup + 5}s`, duration: `${seconds}s`, preAllocatedVUs: 100, maxVUs: 400, gracefulStop: '5s' },
  },
  thresholds: {
    'business_failures{scenario:measured}': ['rate<0.01'],
    'successful_duration{scenario:measured}': ['p(95)<200'],
    'cache_contract_failures{scenario:measured}': ['rate==0'],
    'successful_requests{scenario:measured}': ['count>0'],
    'cache_hit_fraction{scenario:measured}': ['rate>=0'],
    'dropped_iterations{scenario:measured}': [`count<${Math.max(1, Math.ceil(rate * seconds * 0.01))}`],
  },
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)'],
  systemTags: ['status', 'method', 'name', 'scenario', 'expected_response'],
  discardResponseBodies: false,
};

export default function () {
  const write = __ENV.MODE === 'mixed' && exec.scenario.iterationInTest % 10 === 9;
  const params = { headers: { Host: __ENV.HOST || 'benchmark.threadops.invalid' }, timeout: '5s', redirects: 0, tags: { name: write ? 'create-item' : 'list-items' } };
  let response;
  if (write) {
    params.headers['Content-Type'] = 'application/json';
    response = http.post(`${base}/api/items`, JSON.stringify({
      username: '@benchmark', community: 'r/general',
      content: `Benchmark ${__ENV.RUN_ID} ${exec.scenario.name} ${exec.scenario.iterationInTest} ` + 'x'.repeat(448),
    }), params);
  } else {
    response = http.get(`${base}/api/items`, params);
  }
  let valid = response.status === (write ? 201 : 200);
  try {
    const body = response.json();
    valid = valid && (write ? Number.isInteger(body.id) : Array.isArray(body.items) && body.items.length === 25);
  } catch (_) { valid = false; }
  failures.add(!valid);
  if (valid) { successful.add(1); latency.add(response.timings.duration); }
  if (!write && valid) {
    const cache = response.headers['X-Cache'];
    hits.add(cache === 'HIT');
    cacheContract.add(__ENV.CACHE_EXPECT === 'bypass' ? cache !== 'BYPASS' : !['HIT', 'MISS'].includes(cache));
  }
}

export function handleSummary(data) {
  return { '/results/summary.json': JSON.stringify(data, null, 2), stdout: JSON.stringify(data, null, 2) + '\n' };
}
