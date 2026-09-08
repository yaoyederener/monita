import { DurableObject } from "cloudflare:workers";
import {
  ADMIN_TOPICS,
  TRANSFER_TOPIC,
  classifyPairChange,
  decodeTransfer,
  escapeHtml,
  formatMoney,
  formatTokenAmount,
  normalizePairs,
  normalizeSecurity,
  toFiniteNumber,
} from "./lib.js";

const TOKEN_DISPLAY = "0xB095274743941e953c746F9C228DA9c18Bb6ec29";
const ZERO_ADDRESS = "0x0000000000000000000000000000000000000000";
const BASESCAN = "https://basescan.org";
const DEX_API = "https://api.dexscreener.com/token-pairs/v1/base";
const GOPLUS_API = "https://api.gopluslabs.io/api/v1/token_security/8453";

export class LaptopMonitor extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
  }

  async run() {
    validateEnvironment(this.env);
    const credentials = await this.credentials();
    const token = this.env.TOKEN_ADDRESS.toLowerCase();
    const settings = {
      minLiquidityUsd: toFiniteNumber(this.env.MIN_LIQUIDITY_USD, 10_000),
      largeTransferTokens: toFiniteNumber(this.env.LARGE_TRANSFER_TOKENS, 1_000_000),
      priceAlertPercent: toFiniteNumber(this.env.PRICE_ALERT_PERCENT, 30),
    };

    const [latestBlock, rawPairs] = await Promise.all([
      rpc(this.env, "eth_blockNumber", []),
      fetchJson(`${DEX_API}/${token}`),
    ]);
    const latest = Number(BigInt(latestBlock));
    const pairs = normalizePairs(rawPairs, token);
    const initialized = await this.ctx.storage.get("initialized");

    if (!initialized) {
      for (const pair of pairs) await this.ctx.storage.put(`pair:${pair.address}`, pair);
      await this.ctx.storage.put({ initialized: true, lastBlock: latest, startedAt: Date.now() });
      await sendTelegram(
        this.env,
        `${this.env.TELEGRAM_MENTION}\n🟢 <b>LAPTOP Base 链监控已启动</b>\n` +
          `合约：<code>${TOKEN_DISPLAY}</code>\n` +
          `扫描：每分钟，链上区块补查已启用\n` +
          `当前交易池：${pairs.length} 个`,
        credentials,
      );
      return { initialized: true, latestBlock: latest, pairs: pairs.length };
    }

    const storedLastBlock = toFiniteNumber(await this.ctx.storage.get("lastBlock"), latest - 1);
    const fromBlock = Math.min(storedLastBlock + 1, latest);
    const logs = await rpc(this.env, "eth_getLogs", [
      {
        address: token,
        fromBlock: toHex(fromBlock),
        toBlock: toHex(latest),
      },
    ]);

    await this.processLogs(Array.isArray(logs) ? logs : [], settings, credentials);
    const security = pairs.length > 0 ? await fetchSecurity(token) : null;
    await this.processPairs(pairs, security, settings, credentials);
    if (security) await this.processSecurity(security, credentials);
    await this.ctx.storage.put("lastBlock", latest);
    await this.ctx.storage.put("lastRunAt", Date.now());
    return { latestBlock: latest, logs: logs.length, pairs: pairs.length };
  }

  async processLogs(logs, settings, credentials) {
    const minimum = BigInt(Math.trunc(settings.largeTransferTokens)) * 10n ** 18n;
    for (const log of logs) {
      const topic0 = String(log?.topics?.[0] || "").toLowerCase();
      const eventId = `${log.transactionHash}:${log.logIndex}`;
      if (await this.ctx.storage.get(`seen:${eventId}`)) continue;

      const adminLabel = ADMIN_TOPICS.get(topic0);
      if (adminLabel) {
        await sendTelegram(
          this.env,
          `${this.env.TELEGRAM_MENTION}\n🔴 <b>${escapeHtml(adminLabel)}</b>\n` +
            `交易：<a href="${BASESCAN}/tx/${escapeHtml(log.transactionHash)}">BaseScan</a>\n` +
            `合约：<code>${TOKEN_DISPLAY}</code>`,
          credentials,
        );
        await this.ctx.storage.put(`seen:${eventId}`, true);
        continue;
      }

      if (topic0 !== TRANSFER_TOPIC) continue;
      const transfer = decodeTransfer(log);
      if (!transfer || transfer.amount < minimum) continue;
      const direction =
        transfer.from === ZERO_ADDRESS
          ? "增发/跨链铸造"
          : transfer.to === ZERO_ADDRESS
            ? "销毁/跨链转出"
            : "大额转账";
      await sendTelegram(
        this.env,
        `${this.env.TELEGRAM_MENTION}\n🟠 <b>LAPTOP ${direction}</b>\n` +
          `数量：<b>${formatTokenAmount(transfer.amount)} LAPTOP</b>\n` +
          `发送：<code>${escapeHtml(transfer.from)}</code>\n` +
          `接收：<code>${escapeHtml(transfer.to)}</code>\n` +
          `交易：<a href="${BASESCAN}/tx/${escapeHtml(transfer.txHash)}">BaseScan</a>`,
        credentials,
      );
      await this.ctx.storage.put(`seen:${eventId}`, true);
    }
  }

  async processPairs(pairs, security, settings, credentials) {
    for (const pair of pairs) {
      const key = `pair:${pair.address}`;
      const previous = await this.ctx.storage.get(key);
      const changes = classifyPairChange(previous, pair, settings);
      if (changes.length === 0) continue;

      const title = pairTitle(changes);
      const status =
        pair.liquidityUsd >= settings.minLiquidityUsd && pair.trades5m > 0
          ? "🟢 已有流动性和成交"
          : pair.liquidityUsd > 0
            ? "🟡 已加池，等待成交验证"
            : "🔴 空池/尚无可用流动性";
      const securityLines = security
        ? `\n买税：${security.buyTax} ｜ 卖税：${security.sellTax}\n` +
          `池手续费：${security.poolFee} ｜ 风险：${security.flags.join("、") || "暂未发现"}`
        : "\n税率/卖出测试：等待安全接口更新";
      const link = /^https:\/\/(www\.)?dexscreener\.com\//i.test(pair.url)
        ? pair.url
        : `${BASESCAN}/address/${pair.address}`;
      const quoteWarning = pair.trustedQuote
        ? "✅ 官方 WETH/USDC 配对"
        : `⚠️ 非官方 WETH/USDC 配对：${escapeHtml(pair.counterSymbol)}`;

      await sendTelegram(
        this.env,
        `${this.env.TELEGRAM_MENTION}\n🚨 <b>${escapeHtml(title)}</b>\n` +
          `状态：${status}\n` +
          `DEX：${escapeHtml(pair.dexId)} ｜ 交易对：LAPTOP/${escapeHtml(pair.counterSymbol)}\n` +
          `${quoteWarning}\n` +
          `流动性：<b>${formatMoney(pair.liquidityUsd)}</b>\n` +
          `价格：${formatMoney(pair.priceUsd)}\n` +
          `FDV：${formatMoney(pair.fdv)} ｜ 流通市值：${formatMoney(pair.marketCap)}\n` +
          `5分钟：买 ${pair.buys5m} / 卖 ${pair.sells5m}，成交额 ${formatMoney(pair.volume5m)}` +
          securityLines +
          `\n池地址：<code>${escapeHtml(pair.address)}</code>\n` +
          `<a href="${escapeHtml(link)}">查看交易池</a> ｜ <a href="${BASESCAN}/address/${TOKEN_DISPLAY}">核对合约</a>`,
        credentials,
      );
      await this.ctx.storage.put(key, pair);
    }
  }

  async processSecurity(security, credentials) {
    const previous = await this.ctx.storage.get("security");
    const serialized = JSON.stringify(security);
    if (!previous) {
      await this.ctx.storage.put("security", serialized);
      return;
    }
    if (previous === serialized) return;
    const old = JSON.parse(previous);
    const important =
      old.buyTax !== security.buyTax ||
      old.sellTax !== security.sellTax ||
      old.poolFee !== security.poolFee ||
      JSON.stringify(old.flags) !== JSON.stringify(security.flags);
    if (important) {
      await sendTelegram(
        this.env,
        `${this.env.TELEGRAM_MENTION}\n🔴 <b>LAPTOP 交易安全数据发生变化</b>\n` +
          `买税：${old.buyTax} → <b>${security.buyTax}</b>\n` +
          `卖税：${old.sellTax} → <b>${security.sellTax}</b>\n` +
          `池手续费：${old.poolFee} → <b>${security.poolFee}</b>\n` +
          `当前风险：${security.flags.join("、") || "暂未发现"}`,
        credentials,
      );
    }
    await this.ctx.storage.put("security", serialized);
  }

  async status() {
    return {
      configured: Boolean(await this.ctx.storage.get("telegramCredentials")),
      initialized: Boolean(await this.ctx.storage.get("initialized")),
      startedAt: (await this.ctx.storage.get("startedAt")) || null,
      lastRunAt: (await this.ctx.storage.get("lastRunAt")) || null,
      lastBlock: (await this.ctx.storage.get("lastBlock")) || null,
    };
  }

  async configure({ botToken, chatId }) {
    validateCredentials(botToken, chatId);
    await this.ctx.storage.put("telegramCredentials", { botToken, chatId });
    return { configured: true };
  }

  async credentials() {
    const credentials = await this.ctx.storage.get("telegramCredentials");
    if (!credentials) throw new Error("Telegram credentials are not configured");
    validateCredentials(credentials.botToken, credentials.chatId);
    return credentials;
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const monitor = env.MONITOR.getByName(env.TOKEN_ADDRESS.toLowerCase());
    if (url.pathname === "/bootstrap" && request.method === "POST") {
      try {
        await verifyGitHubOidc(request);
        const body = await request.json();
        await monitor.configure({ botToken: body?.botToken, chatId: body?.chatId });
        return Response.json({ ok: true, configured: true });
      } catch (error) {
        console.error(JSON.stringify({ event: "bootstrap_error", error: errorMessage(error) }));
        return Response.json(
          { ok: false, error: "configuration rejected", reason: errorMessage(error) },
          { status: 403 },
        );
      }
    }
    if (url.pathname !== "/" && url.pathname !== "/health") {
      return new Response("Not Found", { status: 404 });
    }
    try {
      const status = await monitor.status();
      return Response.json({
        ok: true,
        service: "LAPTOP Base pool monitor",
        token: TOKEN_DISPLAY,
        mention: env.TELEGRAM_MENTION,
        schedule: "every minute",
        ...status,
      });
    } catch (error) {
      console.error(JSON.stringify({ event: "status_error", error: errorMessage(error) }));
      return Response.json({ ok: false, error: "status unavailable" }, { status: 503 });
    }
  },

  async scheduled(controller, env) {
    try {
      const result = await env.MONITOR.getByName(env.TOKEN_ADDRESS.toLowerCase()).run();
      console.log(JSON.stringify({ event: "monitor_success", cron: controller.cron, ...result }));
    } catch (error) {
      console.error(JSON.stringify({ event: "monitor_error", error: errorMessage(error) }));
      throw error;
    }
  },
};

function validateEnvironment(env) {
  for (const key of ["TOKEN_ADDRESS", "TELEGRAM_MENTION", "RPC_URL"]) {
    if (!env[key]) throw new Error(`Missing environment value: ${key}`);
  }
}

async function rpc(env, method, params) {
  const response = await fetch(env.RPC_URL, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: crypto.randomUUID(), method, params }),
    signal: AbortSignal.timeout(15_000),
  });
  if (!response.ok) throw new Error(`Base RPC HTTP ${response.status}`);
  const payload = await response.json();
  if (payload.error) throw new Error(`Base RPC ${payload.error.code}: ${payload.error.message}`);
  return payload.result;
}

async function fetchJson(url) {
  const response = await fetch(url, {
    headers: { accept: "application/json" },
    signal: AbortSignal.timeout(15_000),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status} from ${new URL(url).hostname}`);
  return response.json();
}

async function fetchSecurity(token) {
  const payload = await fetchJson(`${GOPLUS_API}?contract_addresses=${token}`);
  return normalizeSecurity(payload, token);
}

async function sendTelegram(env, html, credentials) {
  const response = await fetch(`https://api.telegram.org/bot${credentials.botToken}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      chat_id: credentials.chatId,
      text: html,
      parse_mode: "HTML",
      disable_web_page_preview: true,
    }),
    signal: AbortSignal.timeout(15_000),
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`Telegram HTTP ${response.status}: ${detail.slice(0, 300)}`);
  }
}

function validateCredentials(botToken, chatId) {
  if (!/^\d{5,}:[A-Za-z0-9_-]{20,}$/.test(String(botToken || ""))) {
    throw new Error("Invalid Telegram bot token");
  }
  if (!/^-?\d{5,}$/.test(String(chatId || ""))) throw new Error("Invalid Telegram chat ID");
}

async function verifyGitHubOidc(request) {
  const authorization = request.headers.get("authorization") || "";
  if (!authorization.startsWith("Bearer ")) throw new Error("Missing bearer token");
  const token = authorization.slice(7);
  const parts = token.split(".");
  if (parts.length !== 3) throw new Error("Invalid JWT");
  const header = JSON.parse(new TextDecoder().decode(base64UrlDecode(parts[0])));
  const claims = JSON.parse(new TextDecoder().decode(base64UrlDecode(parts[1])));
  if (header.alg !== "RS256" || !header.kid) throw new Error("Unsupported JWT header");

  const discovery = await fetchJson("https://token.actions.githubusercontent.com/.well-known/openid-configuration");
  const jwks = await fetchJson(discovery.jwks_uri);
  const jwk = jwks.keys?.find((key) => key.kid === header.kid && key.kty === "RSA");
  if (!jwk) throw new Error("Unknown signing key");
  const key = await crypto.subtle.importKey(
    "jwk",
    jwk,
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["verify"],
  );
  const valid = await crypto.subtle.verify(
    "RSASSA-PKCS1-v1_5",
    key,
    base64UrlDecode(parts[2]),
    new TextEncoder().encode(`${parts[0]}.${parts[1]}`),
  );
  if (!valid) throw new Error("Invalid JWT signature");

  const now = Math.floor(Date.now() / 1000);
  const audience = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
  if (claims.iss !== "https://token.actions.githubusercontent.com") throw new Error("Invalid issuer");
  if (!audience.includes("laptop-base-pool-monitor")) throw new Error("Invalid audience");
  if (!claims.exp || claims.exp < now - 30 || (claims.nbf && claims.nbf > now + 30)) {
    throw new Error("Expired JWT");
  }
  if (claims.repository !== "yaoyederener/monita" || claims.ref !== "refs/heads/main") {
    throw new Error("Invalid repository identity");
  }
  if (claims.sub !== "repo:yaoyederener/monita:ref:refs/heads/main") throw new Error("Invalid subject");
  if (
    claims.workflow_ref !==
    "yaoyederener/monita/.github/workflows/deploy-base-pool-monitor.yml@refs/heads/main"
  ) {
    throw new Error("Invalid workflow identity");
  }
}

function base64UrlDecode(value) {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=");
  return Uint8Array.from(atob(padded), (character) => character.charCodeAt(0));
}

function pairTitle(changes) {
  if (changes.includes("new")) return "LAPTOP 新交易池出现";
  if (changes.includes("liquidity-removed")) return "LAPTOP 流动性大幅撤出";
  if (changes.includes("liquidity-added")) return "LAPTOP 流动性大幅增加";
  if (changes.includes("liquidity-live")) return "LAPTOP 池子已注入流动性";
  if (changes.includes("first-trade")) return "LAPTOP 已出现首批成交";
  return "LAPTOP 价格大幅变化";
}

function toHex(value) {
  return `0x${Math.max(0, Math.trunc(value)).toString(16)}`;
}

function errorMessage(error) {
  return error instanceof Error ? error.message : String(error);
}
