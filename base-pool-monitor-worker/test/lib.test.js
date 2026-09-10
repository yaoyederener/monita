import test from "node:test";
import assert from "node:assert/strict";
import {
  DEPOSIT_TOPIC, TRANSFER_TOPIC, USDT, addFlow, addressTopic, blockRanges,
  dayKeyBeijing, decodeBusinessEvent, decodeTransfer, emptyDay, formatUnits, previousDay,
} from "../src/lib.js";

const topicAddress = (address) => `0x${"0".repeat(24)}${address.slice(2)}`;
const USER = "0x701876eb1b6cea82774f7041e092f7740624af81";

test("splits block gaps into bounded ranges", () => {
  assert.deepEqual(blockRanges(101, 5_100, 2_000), [
    { fromBlock: 101, toBlock: 2_100 },
    { fromBlock: 2_101, toBlock: 4_100 },
    { fromBlock: 4_101, toBlock: 5_100 },
  ]);
  assert.equal(blockRanges(1, 100_000, 2_000, 10).at(-1).toBlock, 20_000);
});

test("decodes the confirmed FTREX deposit event", () => {
  const event = decodeBusinessEvent({
    address: "0x00000000110e73585338df0e7f91bf70ed3bd4c4",
    topics: [DEPOSIT_TOPIC, topicAddress(USER), addressTopic(USDT)],
    data: "0x2a4a8d5b7a5e400000",
    transactionHash: `0x${"a".repeat(64)}`,
    blockNumber: "0x64",
    logIndex: "0x2",
  }, DEPOSIT_TOPIC);
  assert.equal(event.user, USER);
  assert.equal(event.token, USDT);
  assert.equal(event.blockNumber, 100);
});

test("decodes USDT transfer actors and amount", () => {
  const transfer = decodeTransfer({
    topics: [TRANSFER_TOPIC, topicAddress(USER), topicAddress("0xa0277eb181577b712813b8f0a11b931bd82fef4a")],
    data: "0xad78ebc5ac6200000",
    transactionHash: `0x${"b".repeat(64)}`,
    blockNumber: "0x1",
    logIndex: "0x1",
  });
  assert.equal(transfer.from, USER);
  assert.equal(formatUnits(transfer.amount), "200.00");
});

test("aggregates daily totals, users, and largest transaction", () => {
  const day = emptyDay();
  addFlow(day, "deposit", { amount: 200n * 10n ** 18n, user: USER, txHash: "0x1" });
  addFlow(day, "deposit", { amount: 300n * 10n ** 18n, user: USER, txHash: "0x2" });
  addFlow(day, "withdrawal", { amount: 150n * 10n ** 18n, user: "0x1111111111111111111111111111111111111111", txHash: "0x3" });
  assert.equal(formatUnits(day.deposit), "500.00");
  assert.equal(day.depositCount, 2);
  assert.equal(day.depositUsers.length, 1);
  assert.equal(day.largestDeposit.txHash, "0x2");
  assert.equal(formatUnits(day.withdrawal), "150.00");
});

test("uses Beijing calendar dates", () => {
  assert.equal(dayKeyBeijing(Date.parse("2026-09-07T23:31:01Z") / 1000), "2026-09-08");
  assert.equal(previousDay("2026-09-08"), "2026-09-07");
});

test("formats signed and rounded USDT values", () => {
  assert.equal(formatUnits(-1_200n * 10n ** 18n), "-1,200.00");
  assert.equal(formatUnits(1234567890000000000n), "1.23");
});
