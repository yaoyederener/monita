import test from "node:test";
import assert from "node:assert/strict";
import {
  addNewTrades,
  analyzeDepth,
  appendSnapshot,
  evaluateSignal,
} from "../core.js";

const blankState = () => ({
  seen: [],
  days: {},
  recentTrades: [],
  snapshots: [],
  signal: { direction: null, count: 0, lastAt: 0 },
});

test("uses exchange execution time and handles midnight", () => {
  const state = blankState();
  const capturedAt = new Date("2026-09-08T16:02:00Z"); // 00:02 in Shanghai
  const update = addNewTrades(
    state,
    [{ side: "BUY", price: 2, amount: 3, time: "23:59:00" }],
    capturedAt,
    "Asia/Shanghai",
  );
  assert.equal(update.fresh.length, 1);
  assert.equal(update.fresh[0].executionDay, "2026-09-08");
  assert.equal(update.windows[5].length, 1);
  assert.equal(state.days["2026-09-08"].BUY.notional, 6);
});

test("does not count the same visible trade twice", () => {
  const state = blankState();
  const trades = [{ side: "SELL", price: 2, amount: 3, time: "10:00:00" }];
  const first = addNewTrades(state, trades, new Date("2026-09-09T02:00:10Z"));
  const second = addNewTrades(state, trades, new Date("2026-09-09T02:01:00Z"));
  assert.equal(first.fresh.length, 1);
  assert.equal(second.fresh.length, 0);
  assert.equal(state.days["2026-09-09"].SELL.count, 1);
});

test("calculates near-market depth, spread, and slippage", () => {
  const depth = analyzeDepth(
    {
      bidItems: [
        { price: 9.9, amount: 100 },
        { price: 9.8, amount: 100 },
      ],
      askItems: [
        { price: 10.1, amount: 100 },
        { price: 10.2, amount: 100 },
      ],
    },
    100,
  );
  assert.equal(depth.bestBid, 9.9);
  assert.equal(depth.bestAsk, 10.1);
  assert.ok(Math.abs(depth.spreadPct - 2) < 1e-9);
  assert.equal(depth.bands["1"].bid.notional, 990);
  assert.equal(depth.bands["1"].ask.notional, 1010);
  assert.ok(depth.buySlippage.percent < 0.001);
  assert.ok(depth.sellSlippage.percent < 0.001);
});

test("requires three fresh confirmations plus a second signal", () => {
  const state = blankState();
  const start = Date.parse("2026-09-09T02:00:00Z");
  let signal;
  for (let index = 0; index < 3; index += 1) {
    const at = new Date(start + index * 5 * 60_000);
    const depth = analyzeDepth({
      bidItems: [{ price: 1, amount: 1_000 }],
      askItems: [{ price: 1.001, amount: 100 }],
    });
    const snapshot = { capturedAt: at, ticker: { close: 1 + index * 0.03 }, depth };
    const trades = [
      { side: "BUY", price: 1, amount: 100, executedAt: at.getTime() - 10_000 },
      { side: "SELL", price: 1, amount: 1, executedAt: at.getTime() - 10_000 },
    ];
    signal = evaluateSignal(state, snapshot, { 5: trades }, {
      imbalanceThreshold: 35,
      confirmations: 3,
      priceMoveThreshold: 2,
      spreadThreshold: 99,
      depthDropThreshold: 99,
    });
    appendSnapshot(state, snapshot);
  }
  assert.equal(signal.count, 3);
  assert.equal(signal.severity, "HIGH");
  assert.ok(signal.evidence.some((item) => item.includes("主动买入")));
});

test("breaks the confirmation chain after a stale interval", () => {
  const state = blankState();
  const firstAt = new Date("2026-09-09T02:00:00Z");
  const depth = analyzeDepth({
    bidItems: [{ price: 1, amount: 1_000 }],
    askItems: [{ price: 1.001, amount: 100 }],
  });
  const first = { capturedAt: firstAt, ticker: { close: 1 }, depth };
  evaluateSignal(state, first, { 5: [] });
  appendSnapshot(state, first);
  const later = { capturedAt: new Date(firstAt.getTime() + 30 * 60_000), ticker: { close: 1 }, depth };
  const signal = evaluateSignal(state, later, { 5: [] });
  assert.equal(signal.count, 1);
});
