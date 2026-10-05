import test from 'node:test';
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import { emptyDay, reportSlotBeijing, addressTopic, DEPOSIT_TOPIC, WITHDRAW_TOPIC, TRANSFER_TOPIC, USDT } from '../src/lib.js';
registerHooks({ resolve(specifier, context, nextResolve) {
  if (specifier === 'cloudflare:workers') return {
    url: 'data:text/javascript,export class DurableObject { constructor(ctx, env) { this.ctx = ctx; this.env = env; } }', shortCircuit: true,
  };
  return nextResolve(specifier, context);
}});
const { LaptopMonitor } = await import('../src/index.js');
const A = '0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
const B = '0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';

function setup(t, initialTime = '2026-10-03T08:55:00+08:00') {
  let now = Date.parse(initialTime);
  const messages = [];
  const requests = [];
  t.mock.method(Date, 'now', () => now);
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    const body = JSON.parse(options.body);
    requests.push(body);
    if (url.startsWith('https://api.telegram.org/')) {
      messages.push(body.text);
      return Response.json({ok: true});
    }
    if (Array.isArray(body)) return Response.json(body.map(item => ({id: item.id, result: '0x8ac7230489e80000'})));
    if (body.method === 'eth_blockNumber') return Response.json({result: '0x7594'});
    if (body.method === 'eth_getBlockByNumber') return Response.json({result: {timestamp: `0x${Math.floor(now / 1000).toString(16)}`}});
    throw new Error(`Unexpected RPC: ${body.method}`);
  });
  const values = new Map([['ftrexFundsState', {
    version: 3, lastBlock: 30100, lastReportedDay: '2026-10-01',
    pendingFundsDigest: {count: 8, since: 1}, days: {'2026-10-03': emptyDay()},
    entities: {depositGateways: [], depositReceivers: [A], withdrawalContracts: [], withdrawalSources: [A, B], withdrawalOperators: []},
  }]]);
  let alarms = 0;
  const ctx = {storage: {
    get: async key => structuredClone(values.get(key)),
    put: async (key, value) => values.set(key, structuredClone(value)),
    setAlarm: async () => alarms++,
  }};
  const makeMonitor = () => {
    const monitor = new LaptopMonitor(ctx, {TELEGRAM_MENTION: ''});
    monitor.credentials = async () => ({bscRpc: 'https://rpc.test', botToken: 'test', chatId: 'test'});
    return monitor;
  };
  return {messages, requests, values, makeMonitor, setTime: value => {now = Date.parse(value);}, alarms: () => alarms};
}

test('reports at 09:00 and 21:00 only, with persistent deduplication and no midnight report', async t => {
  const h = setup(t);
  let monitor = h.makeMonitor();
  await monitor.run();
  assert.equal(h.messages.length, 0);
  assert.equal(h.values.get('ftrexFundsState').pendingFundsDigest, undefined);
  h.setTime('2026-10-03T09:00:00+08:00');
  await monitor.run(); await monitor.run();
  assert.equal(h.messages.length, 1);
  assert.match(h.messages[0], /FTR 链上资金日报/);
  assert.match(h.messages[0], /已知钱包余额：20\.00 USDT/);
  const balanceCalls = h.requests.find(Array.isArray);
  assert.equal(balanceCalls.length, 2); // Shared treasury address must not count twice.
  assert.ok(balanceCalls.every(call => call.params[1] === '0x7594'));
  h.setTime('2026-10-03T20:55:00+08:00');
  monitor = h.makeMonitor(); // A new object instance still deduplicates using saved state.
  await monitor.run();
  assert.equal(h.messages.length, 1);
  h.setTime('2026-10-03T21:00:00+08:00');
  await monitor.run(); await monitor.run();
  assert.equal(h.messages.length, 2);
  h.setTime('2026-10-04T00:05:00+08:00'); await monitor.run();
  assert.equal(h.messages.length, 2);
  h.setTime('2026-10-04T09:00:00+08:00'); await monitor.run();
  assert.equal(h.messages.length, 3);
  assert.match(h.messages[2], /较昨日：转入持平，转出持平/);
  assert.equal((await monitor.status()).fundsReportMode, 'twice_daily');
  assert.deepEqual((await monitor.status()).fundsReportTimes, ['09:00', '21:00']);
});

test('backlog delays a report until caught up and does not replay older missed slots', async t => {
  const h = setup(t, '2026-10-03T09:00:00+08:00');
  const state = h.values.get('ftrexFundsState'); state.lastBlock = 100;
  const monitor = h.makeMonitor();
  monitor.scanRange = async (_rpc, state) => ({state, changes: [], flows: [], processedEvents: 0});
  await monitor.run();
  assert.equal(h.messages.length, 0);
  assert.equal(h.alarms(), 1);
  const caughtUpState = h.values.get('ftrexFundsState'); caughtUpState.lastBlock = 30100;
  h.setTime('2026-10-03T21:05:00+08:00');
  await monitor.run();
  assert.equal(h.messages.length, 1);
  assert.equal(h.values.get('ftrexFundsState').lastReportedSlot, '2026-10-03T21:00');
});

test('overlapping scheduled scans produce one report', async t => {
  const h = setup(t, '2026-10-03T09:00:00+08:00');
  const monitor = h.makeMonitor();
  await Promise.all([monitor.run(), monitor.run(), monitor.run()]);
  assert.equal(h.messages.length, 1);
});

test('stale chain data does not generate a current report or consume the slot', async t => {
  const h = setup(t, '2026-10-03T09:00:00+08:00');
  const monitor = h.makeMonitor();
  t.mock.method(globalThis, 'fetch', async (_url, options) => {
    const body = JSON.parse(options.body);
    return Response.json({result: body.method === 'eth_blockNumber' ? '0x7594' : {timestamp: '0x1'}});
  });
  await assert.rejects(monitor.run(), /stale/);
  assert.equal(h.messages.length, 0);
  assert.equal(h.values.get('ftrexFundsState').lastReportedSlot, undefined);
});

test('Beijing slots use local calendar boundaries', () => {
  assert.equal(reportSlotBeijing(Date.parse('2026-10-03T08:59:59+08:00')), null);
  assert.equal(reportSlotBeijing(Date.parse('2026-10-03T09:00:00+08:00')), '2026-10-03T09:00');
  assert.equal(reportSlotBeijing(Date.parse('2026-10-03T20:59:59+08:00')), '2026-10-03T09:00');
  assert.equal(reportSlotBeijing(Date.parse('2026-10-03T21:00:00+08:00')), '2026-10-03T21:00');
  assert.equal(reportSlotBeijing(Date.parse('2026-10-04T00:00:00+08:00')), null);
});


test('scanning separates internal routing from external business flows and counts a transfer once', async t => {
  const receiver = A, source = B;
  const user = '0xcccccccccccccccccccccccccccccccccccccccc';
  const gateway = '0xdddddddddddddddddddddddddddddddddddddddd';
  const operator = '0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee';
  const unit = 10n ** 18n;
  const hex = value => `0x${BigInt(value).toString(16)}`;
  const h1 = `0x${'1'.repeat(64)}`, h2 = `0x${'2'.repeat(64)}`;
  const log = (address, topics, amount, txHash, index) => ({
    address, topics, data: hex(amount * unit), transactionHash: txHash,
    blockNumber: '0x64', logIndex: hex(index),
  });
  const depositLog = log(gateway, [DEPOSIT_TOPIC, addressTopic(user), addressTopic(USDT)], 30n, h1, 0);
  const depositTransfer = log(USDT, [TRANSFER_TOPIC, addressTopic(user), addressTopic(receiver)], 30n, h1, 1);
  const internalTransfer = log(USDT, [TRANSFER_TOPIC, addressTopic(receiver), addressTopic(source)], 70n, h1, 2);
  const withdrawalLog = log(source, [WITHDRAW_TOPIC, addressTopic(user), addressTopic(USDT)], 20n, h2, 0);
  const withdrawalTransfer = log(USDT, [TRANSFER_TOPIC, addressTopic(source), addressTopic(user)], 20n, h2, 1);
  const timestamp = Date.parse('2026-10-03T12:00:00+08:00') / 1000;
  t.mock.method(globalThis, 'fetch', async (_url, options) => {
    const body = JSON.parse(options.body);
    if (!Array.isArray(body)) {
      const topics = body.params[0].topics;
      let result = [];
      if (topics[0] === DEPOSIT_TOPIC) result = [depositLog];
      else if (topics[0] === WITHDRAW_TOPIC) result = [withdrawalLog];
      else if (Array.isArray(topics[1]) && Array.isArray(topics[2])) result = [internalTransfer];
      else if (topics[1] == null) result = [depositTransfer];
      else result = [withdrawalTransfer];
      return Response.json({result});
    }
    return Response.json(body.map(item => {
      const hash = item.params[0];
      const result = item.method === 'eth_getTransactionByHash' ? {hash, from: hash === h1 ? user : operator}
        : item.method === 'eth_getTransactionReceipt' ? {transactionHash: hash, status: '0x1', logs: hash === h1 ? [depositLog, depositTransfer, internalTransfer] : [withdrawalLog, withdrawalTransfer]}
        : {number: '0x64', timestamp: hex(timestamp)};
      return {id: item.id, result};
    }));
  });
  const state = {days: {}, entities: {
    depositGateways: [gateway], depositReceivers: [receiver], withdrawalContracts: [source],
    withdrawalSources: [source], withdrawalOperators: [operator],
  }};
  const monitor = new LaptopMonitor({storage: {}}, {});
  const result = await monitor.scanRange('https://rpc.test', state, {fromBlock: 100, toBlock: 100});
  const day = result.state.days['2026-10-03'];
  assert.equal(day.deposit, String(30n * unit));
  assert.equal(day.withdrawal, String(20n * unit));
  assert.equal(day.internalTransfer, String(70n * unit));
  assert.equal(day.depositCount, 1); assert.equal(day.withdrawalCount, 1);
  assert.equal(day.depositsByAddress[user].count, 1);
  assert.equal(day.withdrawalsByAddress[user].count, 1);
});
