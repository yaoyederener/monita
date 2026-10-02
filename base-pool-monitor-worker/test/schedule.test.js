import test from 'node:test';
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import { emptyDay } from '../src/lib.js';
registerHooks({ resolve(specifier, context, nextResolve) {
  if (specifier === 'cloudflare:workers') return {
    url: 'data:text/javascript,export class DurableObject { constructor(ctx, env) { this.ctx = ctx; this.env = env; } }', shortCircuit: true,
  };
  return nextResolve(specifier, context);
}});
const { LaptopMonitor } = await import('../src/index.js');

test('repeated scans send one daily report, discard old digest, and wait for catch-up', async t => {
  let now = Date.parse('2026-10-03T01:00:00+08:00');
  t.mock.method(Date, 'now', () => now);
  const messages = [];
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    if (url.startsWith('https://api.telegram.org/')) {
      messages.push(JSON.parse(options.body).text);
      return Response.json({ok: true});
    }
    assert.equal(JSON.parse(options.body).method, 'eth_blockNumber');
    return Response.json({result: '0x7594'});
  });
  const values = new Map([['ftrexFundsState', {
    version: 3, lastBlock: 30100, lastReportedDay: '2026-10-01',
    pendingFundsDigest: {count: 8, since: 1}, days: {'2026-10-02': emptyDay()},
    entities: {depositGateways: [], depositReceivers: [], withdrawalContracts: [], withdrawalSources: []},
  }]]);
  let alarms = 0;
  const monitor = new LaptopMonitor({storage: {
    get: async key => structuredClone(values.get(key)),
    put: async (key, value) => values.set(key, structuredClone(value)),
    setAlarm: async () => alarms++,
  }}, {TELEGRAM_MENTION: ''});
  monitor.credentials = async () => ({bscRpc: 'https://rpc.test', botToken: 'test', chatId: 'test'});
  await monitor.run();
  assert.equal(messages.length, 1);
  assert.match(messages[0], /羽翎每日资金报告/);
  assert.equal(values.get('ftrexFundsState').pendingFundsDigest, undefined);
  now += 4 * 3600 * 1000;
  await monitor.run(); await monitor.run();
  assert.equal(messages.length, 1);
  now = Date.parse('2026-10-04T00:05:00+08:00');
  await monitor.run();
  assert.equal(messages.length, 2);
  // A large scan backlog must finish before a daily report is sent.
  const state = values.get('ftrexFundsState');
  state.lastBlock = 100; state.lastReportedDay = '2026-10-02';
  monitor.scanRange = async (_rpc, state) => ({state, changes: [], flows: [], processedEvents: 0});
  await monitor.run();
  assert.equal(messages.length, 2);
  assert.equal(alarms, 1);
  assert.equal((await monitor.status()).fundsReportMode, 'daily');
});
