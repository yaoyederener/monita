import test from "node:test";
import assert from "node:assert/strict";
import {
  TRANSFER_TOPIC,
  classifyPairChange,
  decodeTransfer,
  formatMoney,
  formatTax,
  normalizePairs,
  normalizeSecurity,
} from "../src/lib.js";

const TOKEN = "0xb095274743941e953c746f9c228da9c18bb6ec29";

test("normalizes a matching DEX pair", () => {
  const pairs = normalizePairs(
    [
      {
        pairAddress: "0x1111111111111111111111111111111111111111",
        dexId: "aerodrome",
        baseToken: { address: TOKEN, symbol: "LAPTOP" },
        quoteToken: { address: "0x4200000000000000000000000000000000000006", symbol: "WETH" },
        priceUsd: "0.01",
        liquidity: { usd: 100000 },
        txns: { m5: { buys: 3, sells: 2 } },
      },
    ],
    TOKEN,
  );
  assert.equal(pairs.length, 1);
  assert.equal(pairs[0].trades5m, 5);
  assert.equal(pairs[0].liquidityUsd, 100000);
  assert.equal(pairs[0].counterSymbol, "WETH");
  assert.equal(pairs[0].trustedQuote, true);
});

test("classifies removal and price movement", () => {
  const changes = classifyPairChange(
    { liquidityUsd: 100000, priceUsd: 1, trades5m: 2 },
    { liquidityUsd: 60000, priceUsd: 1.5, trades5m: 1 },
    { minLiquidityUsd: 10000, priceAlertPercent: 30 },
  );
  assert.deepEqual(changes, ["liquidity-removed", "price-move"]);
});

test("decodes a large ERC-20 transfer", () => {
  const transfer = decodeTransfer({
    topics: [
      TRANSFER_TOPIC,
      `0x${"0".repeat(24)}${"1".repeat(40)}`,
      `0x${"0".repeat(24)}${"2".repeat(40)}`,
    ],
    data: "0xde0b6b3a7640000",
    transactionHash: `0x${"a".repeat(64)}`,
    logIndex: "0x1",
  });
  assert.equal(transfer.amount, 10n ** 18n);
  assert.equal(transfer.from, `0x${"1".repeat(40)}`);
});

test("keeps unknown tax distinct from zero tax", () => {
  assert.equal(formatTax(""), "未知");
  assert.equal(formatTax("0"), "0.00%");
  assert.equal(formatMoney(1250000), "$1.25M");
});

test("normalizes GoPlus security flags", () => {
  const security = normalizeSecurity(
    {
      result: {
        [TOKEN]: {
          buy_tax: "0.01",
          sell_tax: "0.02",
          is_in_dex: "1",
          cannot_sell: "1",
          holder_count: "12",
        },
      },
    },
    TOKEN,
  );
  assert.equal(security.buyTax, "1.00%");
  assert.equal(security.sellTax, "2.00%");
  assert.deepEqual(security.flags, ["无法卖出"]);
});
