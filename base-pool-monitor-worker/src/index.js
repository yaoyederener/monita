import { DurableObject } from "cloudflare:workers";
import {
  DEPOSIT_TOPIC, TRANSFER_TOPIC, USDT, WITHDRAW_TOPIC, addFlow, addressTopic,
  blockRanges, currentDayBeijing, dayKeyBeijing, decodeBusinessEvent, decodeTransfer,
  emptyDay, escapeHtml, formatUnits, normalizeAddress, previousDay, pruneDays,
  shortAddress, uniqueBy,
} from "./lib.js";

const BSCSCAN = "https://bscscan.com";
// Keep the old object name so the already configured Telegram credentials remain available.
const INSTANCE_NAME = "0xb095274743941e953c746f9c228da9c18bb6ec29";
// Covers more than two days even at BSC's faster block cadence.
const INITIAL_LOOKBACK_BLOCKS = 500_000;
const INITIAL_ENTITIES = Object.freeze({
  depositGateways: ["0x00000000110e73585338df0e7f91bf70ed3bd4c4"],
  depositReceivers: ["0xa0277eb181577b712813b8f0a11b931bd82fef4a"],
  withdrawalContracts: ["0x301173ccf602050c0bbdd36b6af9cf59d0000000"],
  withdrawalSources: ["0x301173ccf602050c0bbdd36b6af9cf59d0000000"],
  withdrawalOperators: ["0x6e1469c12a996376c4aff61daa25741ef97bbceb"],
});

export class LaptopMonitor extends DurableObject {
  async run({ forceReport = false } = {}) {
    const credentials = await this.credentials();
    const latest = Number(BigInt(await rpc(credentials.bscRpc, "eth_blockNumber", [])));
    let state = await this.loadState(latest);
    const shouldNotifyFlows = state.realtimeReady === true && latest - state.lastBlock <= 5_000;
    // Five chunks stay safely below the 50-subrequest limit, including OIDC and Telegram calls.
    const ranges = blockRanges(state.lastBlock + 1, latest, 2_000, 5);
    const operatorTransactions = ranges.length && credentials.bscscanKey
      ? await fetchOperatorTransactions(credentials.bscscanKey, state.entities.withdrawalOperators,
          ranges[0].fromBlock, ranges.at(-1).toBlock)
      : [];
    const changes = [];
    const flows = [];
    let processedEvents = 0;

    for (const range of ranges) {
      const operatorHashes = operatorTransactions
        .filter((tx) => Number(tx.blockNumber) >= range.fromBlock && Number(tx.blockNumber) <= range.toBlock)
        .map((tx) => String(tx.hash).toLowerCase());
      const result = await this.scanRange(credentials.bscRpc, state, range, operatorHashes);
      state = result.state;
      state.lastBlock = range.toBlock;
      state.lastRunAt = Date.now();
      processedEvents += result.processedEvents;
      changes.push(...result.changes);
      flows.push(...result.flows);
      state.days = pruneDays(state.days);
      await this.ctx.storage.put("ftrexFundsState", state);
    }

    if (changes.length) await sendTelegram(credentials, addressChangeMessage(this.env, changes));
    if (shouldNotifyFlows && flows.length) {
      await sendTelegram(credentials, realtimeFundsMessage(this.env, flows));
    }
    const caughtUp = state.lastBlock >= latest;
    if (caughtUp) {
      const today = currentDayBeijing();
      const yesterday = previousDay(today);
      if (state.lastReportedDay !== yesterday) {
        await sendTelegram(credentials, dailyReport(this.env, yesterday, state.days[yesterday], state));
        state.lastReportedDay = yesterday;
      }
      if (forceReport) {
        await sendTelegram(credentials, dailyReport(this.env, today, state.days[today], state, true));
      }
    } else if (forceReport) {
      await sendTelegram(credentials, backfillMessage(this.env, state, latest));
    }

    state.lastRunAt = Date.now();
    state.lastError = "";
    state.consecutiveErrors = 0;
    state.realtimeReady = caughtUp;
    await this.ctx.storage.put("ftrexFundsState", state);
    if (!caughtUp) await this.ctx.storage.setAlarm(Date.now() + 30_000);
    return {
      latestBlock: latest, scannedThrough: state.lastBlock,
      remainingBlocks: Math.max(0, latest - state.lastBlock), ranges: ranges.length,
      processedEvents, changes: changes.length, caughtUp,
      notifiedFlows: shouldNotifyFlows ? flows.length : 0,
    };
  }

  async scanRange(rpcUrl, state, range, operatorHashes = []) {
    const common = { fromBlock: toHex(range.fromBlock), toBlock: toHex(range.toBlock) };
    const [depositLogs, withdrawalLogs, receiverTransfers, sourceTransfers] = await Promise.all([
      getLogs(rpcUrl, { ...common, address: state.entities.depositGateways,
        topics: [DEPOSIT_TOPIC, null, addressTopic(USDT)] }),
      getLogs(rpcUrl, { ...common, address: state.entities.withdrawalContracts,
        topics: [WITHDRAW_TOPIC, null, addressTopic(USDT)] }),
      getLogs(rpcUrl, { ...common, address: USDT,
        topics: [TRANSFER_TOPIC, null, state.entities.depositReceivers.map(addressTopic)] }),
      getLogs(rpcUrl, { ...common, address: USDT,
        topics: [TRANSFER_TOPIC, state.entities.withdrawalSources.map(addressTopic)] }),
    ]);

    const candidateHashes = [...new Set([
      ...uniqueBy([...depositLogs, ...withdrawalLogs, ...receiverTransfers, ...sourceTransfers],
        (log) => String(log.transactionHash || "").toLowerCase())
        .map((log) => String(log.transactionHash).toLowerCase()),
      ...operatorHashes,
    ])];
    const [transactions, receipts] = await Promise.all([
      rpcBatch(rpcUrl, candidateHashes.map((hash) => ["eth_getTransactionByHash", [hash]])),
      rpcBatch(rpcUrl, candidateHashes.map((hash) => ["eth_getTransactionReceipt", [hash]])),
    ]);
    const txByHash = new Map(transactions.filter(Boolean).map((tx) => [tx.hash.toLowerCase(), tx]));
    const receiptByHash = new Map(receipts.filter(Boolean).map((item) => [item.transactionHash.toLowerCase(), item]));
    const discoveredDepositLogs = [];
    const discoveredWithdrawalLogs = [];
    for (const receipt of receipts.filter(Boolean)) {
      for (const log of receipt.logs || []) {
        const topic0 = String(log?.topics?.[0] || "").toLowerCase();
        if (topic0 === DEPOSIT_TOPIC && decodeBusinessEvent(log, DEPOSIT_TOPIC)?.token === USDT) discoveredDepositLogs.push(log);
        if (topic0 === WITHDRAW_TOPIC && decodeBusinessEvent(log, WITHDRAW_TOPIC)?.token === USDT) discoveredWithdrawalLogs.push(log);
      }
    }

    const deposits = uniqueBy([...depositLogs, ...discoveredDepositLogs]
      .map((log) => decodeBusinessEvent(log, DEPOSIT_TOPIC)).filter((event) => event?.token === USDT),
    (event) => `${event.txHash}:${event.logIndex}`);
    const withdrawals = uniqueBy([...withdrawalLogs, ...discoveredWithdrawalLogs]
      .map((log) => decodeBusinessEvent(log, WITHDRAW_TOPIC)).filter((event) => event?.token === USDT),
    (event) => `${event.txHash}:${event.logIndex}`);
    const blockNumbers = [...new Set([...deposits, ...withdrawals].map((event) => event.blockNumber))];
    const blocks = await rpcBatch(rpcUrl, blockNumbers.map((block) => ["eth_getBlockByNumber", [toHex(block), false]]));
    const timestampByBlock = new Map(blocks.filter(Boolean).map((block) =>
      [Number(BigInt(block.number)), Number(BigInt(block.timestamp))]));

    const changes = [];
    const flows = [];
    const processed = new Set();
    for (const [type, events] of [["deposit", deposits], ["withdrawal", withdrawals]]) {
      for (const event of events) {
        const id = `${type}:${event.txHash}:${event.logIndex}`;
        if (processed.has(id)) continue;
        processed.add(id);
        const receipt = receiptByHash.get(event.txHash);
        const tx = txByHash.get(event.txHash);
        const transfers = (receipt?.logs || []).filter((log) => normalizeAddress(log.address) === USDT)
          .map(decodeTransfer).filter(Boolean);
        let platformAddress = event.contract;
        if (type === "deposit") {
          addEntity(state, "depositGateways", event.contract, changes, "充值入口合约", event.txHash);
          const match = transfers.find((item) => item.from === event.user && item.amount === event.amount);
          if (match) {
            platformAddress = match.to;
            addEntity(state, "depositReceivers", match.to, changes, "充值收款钱包", event.txHash);
          }
        } else {
          addEntity(state, "withdrawalContracts", event.contract, changes, "提现业务合约", event.txHash);
          const match = transfers.find((item) => item.to === event.user && item.amount === event.amount);
          if (match) {
            platformAddress = match.from;
            addEntity(state, "withdrawalSources", match.from, changes, "提现出款金库", event.txHash);
          }
          addEntity(state, "withdrawalOperators", normalizeAddress(tx?.from), changes, "提现操作钱包", event.txHash);
        }
        const timestamp = timestampByBlock.get(event.blockNumber);
        if (!timestamp) throw new Error(`Missing timestamp for block ${event.blockNumber}`);
        const day = dayKeyBeijing(timestamp);
        state.days[day] = addFlow(state.days[day] || emptyDay(), type, event);
        state.lastFlowAt = timestamp * 1_000;
        state.lastFlowType = type;
        state.lastFlowTx = event.txHash;
        flows.push({
          type, amount: event.amount.toString(), user: event.user, txHash: event.txHash,
          timestamp, platformAddress,
        });
      }
    }
    return { state, changes, flows, processedEvents: processed.size };
  }

  async loadState(latestBlock) {
    const existing = await this.ctx.storage.get("ftrexFundsState");
    if (existing?.version === 3) {
      if (existing.realtimeReady === undefined) {
        existing.realtimeReady = latestBlock - existing.lastBlock <= 5_000;
      }
      return existing;
    }
    return {
      version: 3, startedAt: Date.now(), lastRunAt: null,
      lastBlock: Math.max(0, latestBlock - INITIAL_LOOKBACK_BLOCKS), lastReportedDay: null,
      lastError: "", consecutiveErrors: 0, realtimeReady: false,
      entities: structuredClone(INITIAL_ENTITIES), days: {},
    };
  }

  async status() {
    const state = await this.ctx.storage.get("ftrexFundsState");
    const credentials = await this.ctx.storage.get("telegramCredentials");
    const today = currentDayBeijing();
    const todayData = state?.days?.[today] || emptyDay();
    return {
      configured: Boolean(credentials?.botToken && credentials?.chatId && credentials?.bscRpc),
      initialized: Boolean(state), startedAt: state?.startedAt || null,
      lastAttemptAt: (await this.ctx.storage.get("ftrexLastAttemptAt")) || null,
      lastRunAt: state?.lastRunAt || null, lastBlock: state?.lastBlock || null,
      lastError: state?.lastError || null, consecutiveErrors: state?.consecutiveErrors || 0,
      realtimeAlerts: state?.realtimeReady === true,
      lastFlowAt: state?.lastFlowAt || null,
      lastFlowType: state?.lastFlowType || null,
      lastFlowTx: state?.lastFlowTx || null,
      today: state ? {
        day: today,
        deposit: formatUnits(todayData.deposit), depositCount: todayData.depositCount || 0,
        withdrawal: formatUnits(todayData.withdrawal), withdrawalCount: todayData.withdrawalCount || 0,
      } : null,
      entities: state ? Object.fromEntries(Object.entries(state.entities).map(([key, values]) => [key, values.length])) : null,
    };
  }

  async noteAttempt() { await this.ctx.storage.put("ftrexLastAttemptAt", Date.now()); }
  async alarm() {
    try {
      const result = await this.run();
      if (!result.caughtUp) await this.ctx.storage.setAlarm(Date.now() + 30_000);
    } catch (error) {
      await this.noteError(errorMessage(error));
      console.error(JSON.stringify({ event: "ftrex_backfill_error", error: errorMessage(error) }));
      await this.ctx.storage.setAlarm(Date.now() + 60_000);
    }
  }
  async noteError(message) {
    const state = await this.ctx.storage.get("ftrexFundsState");
    if (!state) return;
    state.lastError = String(message).slice(0, 300);
    state.consecutiveErrors = Number(state.consecutiveErrors || 0) + 1;
    await this.ctx.storage.put("ftrexFundsState", state);
  }
  async configure({ botToken, chatId, bscRpc, bscscanKey }) {
    validateCredentials(botToken, chatId, bscRpc);
    await this.ctx.storage.put("telegramCredentials", { botToken, chatId, bscRpc, bscscanKey: String(bscscanKey || "") });
    return { configured: true };
  }
  async credentials() {
    const credentials = await this.ctx.storage.get("telegramCredentials");
    if (!credentials) throw new Error("Monitor credentials are not configured");
    validateCredentials(credentials.botToken, credentials.chatId, credentials.bscRpc);
    return credentials;
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const monitor = env.MONITOR.getByName(INSTANCE_NAME);
    if (url.pathname === "/bootstrap" && request.method === "POST") {
      try {
        await verifyGitHubOidc(request);
        const body = await request.json();
        await monitor.configure({ botToken: body?.botToken, chatId: body?.chatId,
          bscRpc: body?.bscRpc, bscscanKey: body?.bscscanKey });
        return Response.json({ ok: true, configured: true });
      } catch (error) {
        console.error(JSON.stringify({ event: "bootstrap_error", error: errorMessage(error) }));
        return Response.json({ ok: false, error: "configuration rejected" }, { status: 403 });
      }
    }
    if (url.pathname === "/run" && request.method === "POST") {
      try {
        await verifyGitHubOidc(request);
        return Response.json({ ok: true, ...await monitor.run({ forceReport: true }) });
      } catch (error) {
        await monitor.noteError(errorMessage(error));
        return Response.json({ ok: false, error: errorMessage(error) }, { status: 503 });
      }
    }
    if (url.pathname !== "/" && url.pathname !== "/health") return new Response("Not Found", { status: 404 });
    return Response.json({
      ok: true, service: "FTREX BNB Chain USDT funds monitor", token: USDT,
      mention: env.TELEGRAM_MENTION, timezone: "Asia/Shanghai",
      schedule: "every 5 minutes; daily report after Beijing midnight", ...await monitor.status(),
    });
  },
  async scheduled(controller, env) {
    const monitor = env.MONITOR.getByName(INSTANCE_NAME);
    await monitor.noteAttempt();
    try {
      console.log(JSON.stringify({ event: "ftrex_funds_success", cron: controller.cron, ...await monitor.run() }));
    } catch (error) {
      await monitor.noteError(errorMessage(error));
      console.error(JSON.stringify({ event: "ftrex_funds_error", error: errorMessage(error) }));
      // Deliberately do not throw: transient RPC failures must not create failure-email noise.
    }
  },
};

function addEntity(state, key, address, changes, label, txHash) {
  if (!address || state.entities[key].includes(address)) return;
  state.entities[key].push(address);
  changes.push({ key, address, label, txHash });
}

function addressChangeMessage(env, changes) {
  const lines = uniqueBy(changes, (item) => `${item.key}:${item.address}`).map((item) =>
    `• ${escapeHtml(item.label)}：<code>${escapeHtml(item.address)}</code>\n  <a href="${BSCSCAN}/tx/${escapeHtml(item.txHash)}">验证交易</a>`);
  return `${env.TELEGRAM_MENTION}\n🚨 <b>羽翎链上地址体系发生变化</b>\n${lines.join("\n")}\n\n已由同一业务事件和 USDT 资金路径交叉验证，并自动纳入后续统计。`;
}

function realtimeFundsMessage(env, flows) {
  const deposits = flows.filter((flow) => flow.type === "deposit");
  const withdrawals = flows.filter((flow) => flow.type === "withdrawal");
  const depositTotal = deposits.reduce((sum, flow) => sum + BigInt(flow.amount), 0n);
  const withdrawalTotal = withdrawals.reduce((sum, flow) => sum + BigInt(flow.amount), 0n);
  const net = depositTotal - withdrawalTotal;
  const netLabel = net > 0n ? "本轮净流入" : net < 0n ? "本轮净流出" : "本轮持平";
  const details = flows.slice(0, 8).map((flow) => {
    const icon = flow.type === "deposit" ? "🟢 充值" : "🔴 提现";
    const route = flow.type === "deposit"
      ? `${shortAddress(flow.user)} → ${shortAddress(flow.platformAddress)}`
      : `${shortAddress(flow.platformAddress)} → ${shortAddress(flow.user)}`;
    return `${icon} <b>${formatUnits(flow.amount)} USDT</b>｜${route} ` +
      `<a href="${BSCSCAN}/tx/${escapeHtml(flow.txHash)}">交易</a>`;
  });
  const omitted = flows.length > details.length
    ? `\n其余 ${flows.length - details.length} 笔已计入本轮合计和每日统计。`
    : "";
  const latestTimestamp = Math.max(...flows.map((flow) => flow.timestamp));
  return `${env.TELEGRAM_MENTION}\n💸 <b>羽翎链上资金动态</b>\n` +
    `时间：${formatBeijingTime(latestTimestamp)}（北京时间）\n\n` +
    `🟢 充值合计：<b>${formatUnits(depositTotal)} USDT</b>｜${deposits.length} 笔\n` +
    `🔴 提现合计：<b>${formatUnits(withdrawalTotal)} USDT</b>｜${withdrawals.length} 笔\n` +
    `⚖️ ${netLabel}：<b>${formatUnits(net < 0n ? -net : net)} USDT</b>\n\n` +
    `${details.join("\n")}${omitted}`;
}

function dailyReport(env, day, rawDay, state, partial = false) {
  const data = rawDay || emptyDay();
  const deposit = BigInt(data.deposit || "0");
  const withdrawal = BigInt(data.withdrawal || "0");
  const net = deposit - withdrawal;
  const netLabel = net > 0n ? "净流入" : net < 0n ? "净流出" : "持平";
  return `${env.TELEGRAM_MENTION}\n📊 <b>${partial ? "羽翎今日资金快照（截至当前）" : "羽翎每日资金报告"}</b>\n` +
    `日期：<b>${day}</b>（北京时间）\n\n` +
    `🟢 充值：<b>${formatUnits(deposit)} USDT</b>｜${data.depositCount || 0} 笔｜${(data.depositUsers || []).length} 个地址\n` +
    `🔴 提现：<b>${formatUnits(withdrawal)} USDT</b>｜${data.withdrawalCount || 0} 笔｜${(data.withdrawalUsers || []).length} 个地址\n` +
    `⚖️ ${netLabel}：<b>${formatUnits(net < 0n ? -net : net)} USDT</b>\n` +
    largestLine("最大充值", data.largestDeposit) + largestLine("最大提现", data.largestWithdrawal) +
    `地址状态：充值入口 ${state.entities.depositGateways.length}｜收款钱包 ${state.entities.depositReceivers.length}｜提现合约 ${state.entities.withdrawalContracts.length}｜出款金库 ${state.entities.withdrawalSources.length}\n` +
    `统计资产：BSC-USDT <code>${USDT}</code>`;
}

function largestLine(label, item) {
  return item ? `${label}：<b>${formatUnits(item.amount)} USDT</b>（${shortAddress(item.user)}） <a href="${BSCSCAN}/tx/${escapeHtml(item.txHash)}">交易</a>\n` : "";
}

function backfillMessage(env, state, latest) {
  const remaining = Math.max(0, latest - state.lastBlock);
  return `${env.TELEGRAM_MENTION}\n🟡 <b>羽翎链上资金监控已启动</b>\n` +
    `正在补扫最近约一天的 BNB Chain 区块，尚余 ${remaining.toLocaleString("en-US")} 个区块。\n` +
    `补扫完成后发送准确日报；地址或合约发生变化会立即通知。`;
}

function formatBeijingTime(unixSeconds) {
  return new Date(Number(unixSeconds) * 1_000 + 8 * 60 * 60 * 1_000)
    .toISOString().replace("T", " ").slice(0, 19);
}

async function getLogs(rpcUrl, filter) {
  if (Array.isArray(filter.address) && !filter.address.length) return [];
  const result = await rpc(rpcUrl, "eth_getLogs", [filter]);
  return Array.isArray(result) ? result : [];
}

async function fetchOperatorTransactions(apiKey, operators, fromBlock, toBlock) {
  try {
    const groups = await Promise.all(operators.map(async (address) => {
      const url = new URL("https://api.etherscan.io/v2/api");
      for (const [key, value] of Object.entries({
        chainid: "56", module: "account", action: "txlist", address,
        startblock: String(fromBlock), endblock: String(toBlock), page: "1", offset: "1000",
        sort: "asc", apikey: apiKey,
      })) url.searchParams.set(key, value);
      const payload = await fetchJson(url.toString());
      if (payload.status === "0" && /no transactions/i.test(String(payload.message) + String(payload.result))) return [];
      if (!Array.isArray(payload.result)) throw new Error(`Explorer response: ${payload.message || "invalid result"}`);
      return payload.result.filter((tx) => normalizeAddress(tx.from) === address && tx.isError !== "1");
    }));
    return uniqueBy(groups.flat(), (tx) => String(tx.hash || "").toLowerCase());
  } catch (error) {
    console.error(JSON.stringify({ event: "operator_discovery_error", error: errorMessage(error) }));
    return [];
  }
}

async function rpc(rpcUrl, method, params) {
  const response = await fetch(rpcUrl, { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: crypto.randomUUID(), method, params }), signal: AbortSignal.timeout(20_000) });
  if (!response.ok) throw new Error(`BSC RPC HTTP ${response.status}`);
  const payload = await response.json();
  if (payload.error) throw new Error(`BSC RPC ${payload.error.code}: ${payload.error.message}`);
  return payload.result;
}

async function rpcBatch(rpcUrl, calls) {
  if (!calls.length) return [];
  const body = calls.map(([method, params], index) => ({ jsonrpc: "2.0", id: index + 1, method, params }));
  const response = await fetch(rpcUrl, { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify(body), signal: AbortSignal.timeout(25_000) });
  if (!response.ok) throw new Error(`BSC RPC batch HTTP ${response.status}`);
  const payload = await response.json();
  if (!Array.isArray(payload)) throw new Error("BSC RPC batch response was not an array");
  const byId = new Map(payload.map((entry) => [entry.id, entry]));
  return body.map((request) => {
    const entry = byId.get(request.id);
    if (entry?.error) throw new Error(`BSC RPC ${entry.error.code}: ${entry.error.message}`);
    return entry?.result ?? null;
  });
}

async function sendTelegram(credentials, html) {
  const response = await fetch(`https://api.telegram.org/bot${credentials.botToken}/sendMessage`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: credentials.chatId, text: html, parse_mode: "HTML", disable_web_page_preview: true }),
    signal: AbortSignal.timeout(15_000),
  });
  if (!response.ok) throw new Error(`Telegram HTTP ${response.status}`);
}

function validateCredentials(botToken, chatId, bscRpc) {
  if (!/^\d{5,}:[A-Za-z0-9_-]{20,}$/.test(String(botToken || ""))) throw new Error("Invalid Telegram bot token");
  if (!/^-?\d{5,}$/.test(String(chatId || ""))) throw new Error("Invalid Telegram chat ID");
  let url;
  try { url = new URL(String(bscRpc || "")); } catch { throw new Error("Invalid BSC RPC URL"); }
  if (url.protocol !== "https:") throw new Error("BSC RPC must use HTTPS");
}

async function verifyGitHubOidc(request) {
  const authorization = request.headers.get("authorization") || "";
  if (!authorization.startsWith("Bearer ")) throw new Error("Missing bearer token");
  const parts = authorization.slice(7).split(".");
  if (parts.length !== 3) throw new Error("Invalid JWT");
  const header = JSON.parse(new TextDecoder().decode(base64UrlDecode(parts[0])));
  const claims = JSON.parse(new TextDecoder().decode(base64UrlDecode(parts[1])));
  if (header.alg !== "RS256" || !header.kid) throw new Error("Unsupported JWT header");
  const discovery = await fetchJson("https://token.actions.githubusercontent.com/.well-known/openid-configuration");
  const jwks = await fetchJson(discovery.jwks_uri);
  const jwk = jwks.keys?.find((key) => key.kid === header.kid && key.kty === "RSA");
  if (!jwk) throw new Error("Unknown signing key");
  const key = await crypto.subtle.importKey("jwk", jwk,
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
  if (!await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, base64UrlDecode(parts[2]),
    new TextEncoder().encode(`${parts[0]}.${parts[1]}`))) throw new Error("Invalid JWT signature");
  const now = Math.floor(Date.now() / 1000);
  const audience = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
  if (claims.iss !== "https://token.actions.githubusercontent.com" || !audience.includes("laptop-base-pool-monitor")) throw new Error("Invalid token identity");
  if (!claims.exp || claims.exp < now - 30 || (claims.nbf && claims.nbf > now + 30)) throw new Error("Expired JWT");
  if (claims.repository !== "yaoyederener/monita" || claims.ref !== "refs/heads/main") throw new Error("Invalid repository identity");
  if (claims.workflow_ref !== "yaoyederener/monita/.github/workflows/deploy-base-pool-monitor.yml@refs/heads/main") throw new Error("Invalid workflow identity");
}

async function fetchJson(url) {
  const response = await fetch(url, { signal: AbortSignal.timeout(15_000) });
  if (!response.ok) throw new Error(`HTTP ${response.status} from ${new URL(url).hostname}`);
  return response.json();
}

function base64UrlDecode(value) {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=");
  return Uint8Array.from(atob(padded), (character) => character.charCodeAt(0));
}
function toHex(value) { return `0x${Math.max(0, Math.trunc(value)).toString(16)}`; }
function errorMessage(error) { return error instanceof Error ? error.message : String(error); }
