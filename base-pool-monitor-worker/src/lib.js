export const TRANSFER_TOPIC =
  "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef";

export const ADMIN_TOPICS = new Map([
  [
    "0x8be0079c531659141344cd1fd0a4f28419497f9722a3daafe3b4186f6b6457e0",
    "所有权已变更",
  ],
  [
    "0x38d16b8cac22d99fc7c124b9cd0de2d3fa1faef420bfe791d8c362d765e22700",
    "所有权转移已发起",
  ],
  [
    "0x238399d427b947898edb290f5ff0f9109849b1c3ba196a42e35f00c50a54b98b",
    "LayerZero Peer 已变更",
  ],
  [
    "0xd48d879cef83a1c0bdda516f27b13ddb1b3f8bbac1c9e1511bb2a659c2427760",
    "LayerZero PreCrime 已变更",
  ],
  [
    "0xf0be4f1e87349231d80c36b33f9e8639658eeaf474014dee15a3e6a4d4414197",
    "LayerZero Inspector 已变更",
  ],
]);

const TRUSTED_QUOTE_ADDRESSES = new Set([
  "0x4200000000000000000000000000000000000006", // WETH
  "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", // native USDC on Base
]);

export function toFiniteNumber(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

export function formatMoney(value) {
  const number = toFiniteNumber(value, Number.NaN);
  if (!Number.isFinite(number)) return "未知";
  if (Math.abs(number) >= 1_000_000_000) return `$${(number / 1_000_000_000).toFixed(2)}B`;
  if (Math.abs(number) >= 1_000_000) return `$${(number / 1_000_000).toFixed(2)}M`;
  if (Math.abs(number) >= 1_000) return `$${(number / 1_000).toFixed(2)}K`;
  if (Math.abs(number) >= 1) return `$${number.toFixed(2)}`;
  if (number === 0) return "$0";
  return `$${number.toPrecision(4)}`;
}

export function formatTax(value) {
  if (value === "" || value === null || value === undefined) return "未知";
  const number = Number(value);
  return Number.isFinite(number) ? `${(number * 100).toFixed(2)}%` : "未知";
}

export function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

export function normalizePairs(rawPairs, tokenAddress) {
  if (!Array.isArray(rawPairs)) return [];
  const target = tokenAddress.toLowerCase();
  return rawPairs
    .filter((pair) => {
      const base = pair?.baseToken?.address?.toLowerCase();
      const quote = pair?.quoteToken?.address?.toLowerCase();
      return base === target || quote === target;
    })
    .map((pair) => {
      const baseAddress = String(pair?.baseToken?.address || "").toLowerCase();
      const tokenIsBase = baseAddress === target;
      const counterToken = tokenIsBase ? pair?.quoteToken : pair?.baseToken;
      const counterAddress = String(counterToken?.address || "").toLowerCase();
      const buys = toFiniteNumber(pair?.txns?.m5?.buys);
      const sells = toFiniteNumber(pair?.txns?.m5?.sells);
      return {
        address: String(pair.pairAddress || "").toLowerCase(),
        dexId: String(pair.dexId || "未知 DEX"),
        url: String(pair.url || ""),
        baseSymbol: String(pair?.baseToken?.symbol || "?"),
        quoteSymbol: String(pair?.quoteToken?.symbol || "?"),
        counterSymbol: String(counterToken?.symbol || "?"),
        counterAddress,
        trustedQuote: TRUSTED_QUOTE_ADDRESSES.has(counterAddress),
        tokenIsBase,
        priceUsd: tokenIsBase ? toFiniteNumber(pair.priceUsd) : 0,
        liquidityUsd: toFiniteNumber(pair?.liquidity?.usd),
        fdv: tokenIsBase ? toFiniteNumber(pair.fdv) : 0,
        marketCap: tokenIsBase ? toFiniteNumber(pair.marketCap) : 0,
        volume5m: toFiniteNumber(pair?.volume?.m5),
        buys5m: buys,
        sells5m: sells,
        trades5m: buys + sells,
        createdAt: toFiniteNumber(pair.pairCreatedAt),
      };
    })
    .filter((pair) => /^0x[0-9a-f]{40}$/.test(pair.address));
}

export function classifyPairChange(previous, current, settings) {
  if (!previous) return ["new"];
  const changes = [];
  const minLiquidity = toFiniteNumber(settings.minLiquidityUsd, 10_000);
  const priceThreshold = toFiniteNumber(settings.priceAlertPercent, 30);

  if (previous.liquidityUsd <= 0 && current.liquidityUsd > 0) {
    changes.push("liquidity-live");
  } else if (
    previous.liquidityUsd > 0 &&
    current.liquidityUsd <= previous.liquidityUsd * 0.8 &&
    previous.liquidityUsd - current.liquidityUsd >= minLiquidity
  ) {
    changes.push("liquidity-removed");
  } else if (
    current.liquidityUsd >= previous.liquidityUsd * 1.25 &&
    current.liquidityUsd - previous.liquidityUsd >= minLiquidity
  ) {
    changes.push("liquidity-added");
  }

  if (previous.trades5m === 0 && current.trades5m > 0) changes.push("first-trade");

  if (previous.priceUsd > 0 && current.priceUsd > 0) {
    const change = Math.abs(((current.priceUsd - previous.priceUsd) / previous.priceUsd) * 100);
    if (change >= priceThreshold) changes.push("price-move");
  }

  return changes;
}

export function normalizeSecurity(payload, tokenAddress) {
  const item = payload?.result?.[tokenAddress.toLowerCase()] || {};
  const flags = [];
  const flagNames = {
    cannot_buy: "无法买入",
    cannot_sell_all: "无法全部卖出",
    is_honeypot: "疑似蜜罐",
    is_blacklisted: "存在黑名单",
    transfer_pausable: "可暂停转账",
    trading_cooldown: "交易冷却",
    slippage_modifiable: "税率可修改",
    hidden_owner: "隐藏所有者",
    owner_change_balance: "所有者可改余额",
    selfdestruct: "可自毁",
  };
  for (const [key, label] of Object.entries(flagNames)) {
    if (String(item[key] || "0") === "1") flags.push(label);
  }
  if (String(item.cannot_sell || "0") === "1") flags.push("无法卖出");
  return {
    buyTax: formatTax(item.buy_tax),
    sellTax: formatTax(item.sell_tax),
    transferTax: formatTax(item.transfer_tax),
    poolFee: formatTax(item.pool_fee),
    holderCount: toFiniteNumber(item.holder_count),
    isInDex: String(item.is_in_dex || "0") === "1",
    flags,
  };
}

export function decodeTransfer(log) {
  if (!log?.topics || log.topics.length < 3 || log.topics[0]?.toLowerCase() !== TRANSFER_TOPIC) {
    return null;
  }
  try {
    return {
      from: `0x${log.topics[1].slice(-40)}`.toLowerCase(),
      to: `0x${log.topics[2].slice(-40)}`.toLowerCase(),
      amount: BigInt(log.data || "0x0"),
      txHash: String(log.transactionHash || ""),
      logIndex: String(log.logIndex || "0x0"),
    };
  } catch {
    return null;
  }
}

export function formatTokenAmount(amount, decimals = 18) {
  const scale = 10n ** BigInt(decimals);
  const whole = amount / scale;
  const fraction = ((amount % scale) * 100n) / scale;
  return `${whole.toLocaleString("en-US")}.${fraction.toString().padStart(2, "0")}`;
}
