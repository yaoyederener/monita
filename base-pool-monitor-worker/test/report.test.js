import test from 'node:test';
import assert from 'node:assert/strict';
import { addFlow, addInternalTransfer, emptyDay, topAddresses, topDepositor } from '../src/lib.js';
import { dailyReport, percentageChange } from '../src/report.js';
const A = '0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
const B = '0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';
const unit = 10n ** 18n;
const flow = (day, type, user, value) => addFlow(day, type, {user, amount: value * unit, txHash: '0x1'});
const deposit = (day, user, value) => flow(day, 'deposit', user, value);
const state = {lastBlock: 123, entities: {depositGateways: [], depositReceivers: [], withdrawalContracts: [], withdrawalSources: []}};
const render = (day, context = {}) => dailyReport({TELEGRAM_MENTION: ''}, '2026-10-03', day, state, true, context);

test('ranks gross daily totals for both directions, combining repeat addresses exactly', () => {
  const day = emptyDay();
  deposit(day, A, 300n); deposit(day, B, 500n); deposit(day, A.toUpperCase().replace('0X', '0x'), 300n);
  flow(day, 'withdrawal', A, 400n); flow(day, 'withdrawal', B, 500n); flow(day, 'withdrawal', A, 300n);
  assert.deepEqual(topAddresses(day, 'deposit').rows[0], {user: A, amount: String(600n * unit), count: 2});
  assert.deepEqual(topAddresses(day, 'withdrawal').rows[0], {user: A, amount: String(700n * unit), count: 2});
  assert.equal(day.largestDeposit.user, B);
  assert.equal(day.withdrawalUsers.length, 2);
  assert.match(render(day), /转入 TOP10/);
  assert.match(render(day), /转出 TOP10/);
  assert.equal(topDepositor(JSON.parse(JSON.stringify(day))).leader.amount, String(600n * unit));
});

test('legacy partial address totals never produce a misleading top ten', () => {
  const day = emptyDay();
  delete day.depositsByAddress; delete day.withdrawalsByAddress; delete day.internalTransfersComplete;
  day.deposit = String(800n * unit); day.depositCount = 3;
  day.withdrawal = String(400n * unit); day.withdrawalCount = 2;
  deposit(day, A, 100n); flow(day, 'withdrawal', A, 100n);
  assert.equal(topAddresses(day, 'deposit').complete, false);
  assert.equal(topAddresses(day, 'withdrawal').complete, false);
  assert.deepEqual(topAddresses(day, 'withdrawal').rows, []);
  assert.match(render(day), /暂无法提供完整排行/);
  assert.match(render(day), /内部调拨：升级前记录不完整，暂不可用/);
});

test('top ten has ten distinct addresses and stable ordering for equal totals', () => {
  const day = emptyDay();
  for (let i = 12; i >= 1; i--) deposit(day, `0x${i.toString(16).padStart(40, '0')}`, 500n);
  const rows = topAddresses(day, 'deposit').rows;
  assert.equal(rows.length, 10);
  assert.equal(new Set(rows.map(row => row.user)).size, 10);
  assert.equal(rows[0].user, '0x0000000000000000000000000000000000000001');
  assert.equal(rows[9].user, '0x000000000000000000000000000000000000000a');
  assert.match(render(emptyDay()), /转入 TOP10：\n无/);
});

test('same-slot comparison, signed net flow, balances and internal transfers render correctly', () => {
  const day = emptyDay();
  deposit(day, A, 150n); flow(day, 'withdrawal', B, 200n);
  addInternalTransfer(day, {amount: 80n * unit});
  const report = render(day, {
    walletBalance: String(900n * unit), scannedAt: Date.parse('2026-10-03T09:00:00+08:00') / 1000,
    previousSnapshot: {deposit: String(100n * unit), withdrawal: String(400n * unit), walletBalance: String(1000n * unit)},
  });
  assert.match(report, /净流入：-50\.00 USDT/);
  assert.match(report, /较昨日：转入\+50\.00%，转出-50\.00%/);
  assert.match(report, /已知钱包余额：900\.00 USDT/);
  assert.match(report, /较昨日余额变化：-100\.00 USDT（-10\.00%）/);
  assert.match(report, /内部调拨：80\.00 USDT/);
  assert.match(report, /数据截至：2026-10-03 09:00:00｜区块 123/);
  assert.equal(day.deposit, String(150n * unit));
  assert.equal(day.withdrawal, String(200n * unit));
});

test('missing and zero comparison bases are explicit and large values avoid floating point loss', () => {
  assert.equal(percentageChange(1n, null), '暂无昨日同次通知数据');
  assert.equal(percentageChange(1n, 0n), '不适用（昨日为0）');
  assert.equal(percentageChange(0n, 0n), '持平');
  assert.equal(percentageChange(123456789012345678901234567890n, 123456789012345678901234567890n), '0.00%');
  assert.match(render(emptyDay()), /已知钱包余额：暂不可用/);
});

test('two full top-ten lists fit Telegram message length', () => {
  const day = emptyDay();
  for (let i = 1; i <= 10; i++) {
    const address = `0x${i.toString(16).padStart(40, '0')}`;
    deposit(day, address, 123456789012345678901234n);
    flow(day, 'withdrawal', address, 123456789012345678901234n);
  }
  const visibleText = render(day).replace(/<[^>]*>/g, '');
  assert.ok(visibleText.length < 4096);
});
