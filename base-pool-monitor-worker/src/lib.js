export const TRANSFER_TOPIC =
  "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef";
export const DEPOSIT_TOPIC =
  "0x5548c837ab068cf56a2c2479df0882a4922fd203edb7517321831d95078c5f62";
export const WITHDRAW_TOPIC =
  "0x9b1bfa7fa9ee420a16e124f794c35ac9f90472acc99140eb2f6447c714cad8eb";

export const USDT = "0x55d398326f99059ff775485246999027b3197955";
export const USDT_DECIMALS = 18;

export function toFiniteNumber(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

export function blockRanges(fromBlock, toBlock, chunkSize = 2_000, maxChunks = 10) {
  const start = Math.max(0, Math.trunc(toFiniteNumber(fromBlock, 0)));
  const end = Math.max(0, Math.trunc(toFiniteNumber(toBlock, 0)));
  const size = Math.max(1, Math.trunc(toFiniteNumber(chunkSize, 2_000)));
  const limit = Math.max(1, Math.trunc(toFiniteNumber(maxChunks, 10)));
  if (start > end) return [];
  const ranges = [];
  for (let cursor = start; cursor <= end && ranges.length < limit; cursor += size) {
    ranges.push({ fromBlock: cursor, toBlock: Math.min(end, cursor + size - 1) });
  }
  return ranges;
}

export function normalizeAddress(value) {
  const address = String(value || "").toLowerCase();
  return /^0x[0-9a-f]{40}$/.test(address) ? address : "";
}

export function addressTopic(address) {
  const normalized = normalizeAddress(address);
  if (!normalized) throw new Error("Invalid address");
  return `0x${"0".repeat(24)}${normalized.slice(2)}`;
}

export function addressFromTopic(topic) {
  const value = String(topic || "").toLowerCase();
  return /^0x[0-9a-f]{64}$/.test(value) ? `0x${value.slice(-40)}` : "";
}

export function decodeBusinessEvent(log, expectedTopic) {
  if (
    !Array.isArray(log?.topics) ||
    log.topics.length < 3 ||
    String(log.topics[0]).toLowerCase() !== expectedTopic
  ) {
    return null;
  }
  try {
    const user = addressFromTopic(log.topics[1]);
    const token = addressFromTopic(log.topics[2]);
    if (!user || !token) return null;
    return {
      contract: normalizeAddress(log.address),
      user,
      token,
      amount: BigInt(log.data || "0x0"),
      txHash: String(log.transactionHash || "").toLowerCase(),
      blockNumber: Number(BigInt(log.blockNumber || "0x0")),
      logIndex: Number(BigInt(log.logIndex || "0x0")),
    };
  } catch {
    return null;
  }
}

export function decodeTransfer(log) {
  if (
    !Array.isArray(log?.topics) ||
    log.topics.length < 3 ||
    String(log.topics[0]).toLowerCase() !== TRANSFER_TOPIC
  ) {
    return null;
  }
  try {
    const from = addressFromTopic(log.topics[1]);
    const to = addressFromTopic(log.topics[2]);
    if (!from || !to) return null;
    return {
      from,
      to,
      amount: BigInt(log.data || "0x0"),
      txHash: String(log.transactionHash || "").toLowerCase(),
      blockNumber: Number(BigInt(log.blockNumber || "0x0")),
      logIndex: Number(BigInt(log.logIndex || "0x0")),
    };
  } catch {
    return null;
  }
}

export function uniqueBy(items, keyFn) {
  const seen = new Set();
  return items.filter((item) => {
    const key = keyFn(item);
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

export function dayKeyBeijing(unixSeconds) {
  return new Date(Number(unixSeconds) * 1_000 + 8 * 60 * 60 * 1_000).toISOString().slice(0, 10);
}

export function currentDayBeijing(nowMs = Date.now()) {
  return new Date(nowMs + 8 * 60 * 60 * 1_000).toISOString().slice(0, 10);
}

export function previousDay(day) {
  const date = new Date(`${day}T00:00:00.000Z`);
  date.setUTCDate(date.getUTCDate() - 1);
  return date.toISOString().slice(0, 10);
}

export function emptyDay() {
  return {
    deposit: "0",
    withdrawal: "0",
    depositCount: 0,
    withdrawalCount: 0,
    depositUsers: [],
    withdrawalUsers: [],
    largestDeposit: null,
    largestWithdrawal: null,
  };
}

export function addFlow(day, type, event) {
  const target = day || emptyDay();
  const amount = BigInt(event.amount);
  const amountKey = type === "deposit" ? "deposit" : "withdrawal";
  const countKey = type === "deposit" ? "depositCount" : "withdrawalCount";
  const usersKey = type === "deposit" ? "depositUsers" : "withdrawalUsers";
  const largestKey = type === "deposit" ? "largestDeposit" : "largestWithdrawal";
  target[amountKey] = (BigInt(target[amountKey] || "0") + amount).toString();
  target[countKey] = Number(target[countKey] || 0) + 1;
  target[usersKey] = [...new Set([...(target[usersKey] || []), event.user])];
  if (!target[largestKey] || amount > BigInt(target[largestKey].amount)) {
    target[largestKey] = { amount: amount.toString(), txHash: event.txHash, user: event.user };
  }
  return target;
}

export function formatUnits(value, decimals = USDT_DECIMALS, fractionDigits = 2) {
  const amount = BigInt(value || 0);
  const negative = amount < 0n;
  const absolute = negative ? -amount : amount;
  const scale = 10n ** BigInt(decimals);
  const whole = absolute / scale;
  const fractionScale = 10n ** BigInt(Math.max(0, fractionDigits));
  const rounded = ((absolute % scale) * fractionScale + scale / 2n) / scale;
  const carry = rounded >= fractionScale ? 1n : 0n;
  const displayedFraction = rounded % fractionScale;
  const prefix = negative ? "-" : "";
  const wholeText = (whole + carry).toLocaleString("en-US");
  if (fractionDigits === 0) return `${prefix}${wholeText}`;
  return `${prefix}${wholeText}.${displayedFraction.toString().padStart(fractionDigits, "0")}`;
}

export function shortAddress(address) {
  const value = normalizeAddress(address);
  return value ? `${value.slice(0, 8)}…${value.slice(-6)}` : "未知";
}

export function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

export function pruneDays(days, keep = 10) {
  const entries = Object.entries(days || {}).sort(([a], [b]) => b.localeCompare(a));
  return Object.fromEntries(entries.slice(0, keep));
}

export function mergeFundsDigest(existing, flows, nowMs = Date.now()) {
  const digest = existing || {
    since: nowMs, count: 0, deposit: "0", withdrawal: "0",
    depositCount: 0, withdrawalCount: 0, details: [],
  };
  for (const flow of flows) {
    digest.count += 1;
    if (flow.type === "deposit") {
      digest.deposit = (BigInt(digest.deposit) + BigInt(flow.amount)).toString();
      digest.depositCount += 1;
    } else {
      digest.withdrawal = (BigInt(digest.withdrawal) + BigInt(flow.amount)).toString();
      digest.withdrawalCount += 1;
    }
  }
  digest.details = [...(digest.details || []), ...flows]
    .sort((a, b) => BigInt(a.amount) === BigInt(b.amount) ? 0 : BigInt(a.amount) > BigInt(b.amount) ? -1 : 1)
    .slice(0, 8);
  return digest;
}
