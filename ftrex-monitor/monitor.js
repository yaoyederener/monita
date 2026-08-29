import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

const TIMEZONE = process.env.MONITOR_TIMEZONE || "Asia/Shanghai";
const ALERT_THRESHOLD = Number(process.env.ALERT_IMBALANCE_PCT || 35);
const BOT_TOKEN = process.env.BOT_TOKEN || "";
const CHAT_ID = process.env.CHAT_ID || "";
const STATE_FILE = path.resolve(".state/ftrex.json");
const TRADE_URL = "https://ftrex.io/zh/exchange/ftr_usdt/kline";
const DEPTH_URL =
  "https://ftrex.io/api/market/exchange-plate-mini?symbol=FTR_USDT";
const TICKER_URL =
  "https://ftrex.io/api/market/symbol-thumb-single?symbol=FTR_USDT";

if (!BOT_TOKEN || !CHAT_ID) {
  throw new Error("Missing BOT_TOKEN or CHAT_ID GitHub Secret");
}
if (!Number.isFinite(ALERT_THRESHOLD) || ALERT_THRESHOLD < 0) {
  throw new Error("ALERT_IMBALANCE_PCT must be a non-negative number");
}

function timeParts(date = new Date()) {
  const values = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: TIMEZONE,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    })
      .formatToParts(date)
      .filter((part) => part.type !== "literal")
      .map((part) => [part.type, part.value]),
  );
  return {
    day: `${values.year}-${values.month}-${values.day}`,
    dateTime: `${values.year}-${values.month}-${values.day} ${values.hour}:${values.minute}:${values.second}`,
  };
}

async function loadState() {
  try {
    const state = JSON.parse(await fs.readFile(STATE_FILE, "utf8"));
    return {
      seen: Array.isArray(state.seen) ? state.seen : [],
      days: state.days && typeof state.days === "object" ? state.days : {},
      lastAlertAt: Number(state.lastAlertAt) || 0,
    };
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
    return { seen: [], days: {}, lastAlertAt: 0 };
  }
}

async function saveState(state) {
  await fs.mkdir(path.dirname(STATE_FILE), { recursive: true });
  const temp = `${STATE_FILE}.tmp`;
  await fs.writeFile(temp, `${JSON.stringify(state, null, 2)}\n`, "utf8");
  await fs.rename(temp, STATE_FILE);
}

function emptySummary() {
  return {
    BUY: { count: 0, amount: 0, notional: 0 },
    SELL: { count: 0, amount: 0, notional: 0 },
  };
}

function summarize(trades) {
  const result = emptySummary();
  for (const trade of trades) {
    const side = trade.side === "SELL" ? "SELL" : "BUY";
    result[side].count += 1;
    result[side].amount += trade.amount;
    result[side].notional += trade.price * trade.amount;
  }
  return result;
}

function addNewTrades(state, trades, now) {
  const day = timeParts(now).day;
  state.days[day] ||= emptySummary();
  const seen = new Set(state.seen);
  const occurrences = new Map();
  const fresh = [];

  for (const trade of trades) {
    const base = [trade.time, trade.side, trade.price, trade.amount].join("|");
    const occurrence = (occurrences.get(base) || 0) + 1;
    occurrences.set(base, occurrence);
    const key = `${day}|${base}|${occurrence}`;
    if (seen.has(key)) continue;
    seen.add(key);
    fresh.push(trade);
  }

  const increment = summarize(fresh);
  for (const side of ["BUY", "SELL"]) {
    state.days[day][side].count += increment[side].count;
    state.days[day][side].amount += increment[side].amount;
    state.days[day][side].notional += increment[side].notional;
  }
  state.seen = [...seen].filter((key) => key.startsWith(`${day}|`)).slice(-20_000);
  for (const oldDay of Object.keys(state.days).sort().slice(0, -7)) {
    delete state.days[oldDay];
  }
  return { day, fresh, daySummary: state.days[day] };
}

function analyzeDepth(depth) {
  const total = (items = []) =>
    items.reduce(
      (result, item) => {
        const price = Number(item.price);
        const amount = Number(item.amount);
        if (Number.isFinite(price) && Number.isFinite(amount)) {
          result.amount += amount;
          result.notional += price * amount;
        }
        return result;
      },
      { amount: 0, notional: 0 },
    );
  const bids = depth.bidItems || [];
  const asks = depth.askItems || [];
  const bid = total(bids);
  const ask = total(asks);
  const denominator = bid.notional + ask.notional;
  return {
    bid,
    ask,
    bidLevels: bids.length,
    askLevels: asks.length,
    bestBid: Number(bids[0]?.price) || 0,
    bestAsk: Number(asks[0]?.price) || 0,
    imbalance:
      denominator > 0 ? ((bid.notional - ask.notional) / denominator) * 100 : 0,
  };
}

async function fetchJson(page, url) {
  return page.evaluate(async (target) => {
    const response = await fetch(target, { cache: "no-store" });
    if (!response.ok) throw new Error(`FTREX returned HTTP ${response.status}`);
    return response.json();
  }, url);
}

async function collect() {
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({
      locale: "zh-CN",
      timezoneId: TIMEZONE,
      viewport: { width: 1365, height: 900 },
    });
    const page = await context.newPage();
    await page.goto(TRADE_URL, { waitUntil: "domcontentloaded", timeout: 45_000 });
    const latest = page.getByRole("button", { name: "最新成交" });
    await latest.waitFor({ state: "visible", timeout: 30_000 });
    await latest.click();
    await page.locator(".recent-trades-border").nth(1).waitFor({ timeout: 30_000 });

    const [depth, ticker, trades] = await Promise.all([
      fetchJson(page, DEPTH_URL),
      fetchJson(page, TICKER_URL),
      page.locator(".recent-trades-border").evaluateAll((rows) =>
        rows
          .map((row) => {
            const values = row.innerText.trim().split(/\n+/);
            return {
              side: row.className.includes("text-trade-buy")
                ? "BUY"
                : row.className.includes("text-trade-sell")
                  ? "SELL"
                  : "UNKNOWN",
              price: Number(values[0]),
              amount: Number(values[1]),
              time: values[2] || "",
            };
          })
          .filter(
            (trade) =>
              trade.side !== "UNKNOWN" &&
              Number.isFinite(trade.price) &&
              Number.isFinite(trade.amount) &&
              /^\d{2}:\d{2}:\d{2}$/.test(trade.time),
          ),
      ),
    ]);
    return { capturedAt: new Date(), ticker, trades, depth: analyzeDepth(depth) };
  } finally {
    await browser.close();
  }
}

const money = (value) =>
  Number(value || 0).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });

async function sendTelegram(text) {
  const response = await fetch(`https://api.telegram.org/bot${BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: CHAT_ID, text, disable_web_page_preview: true }),
  });
  const result = await response.json();
  if (!response.ok || !result.ok) {
    throw new Error(`Telegram sendMessage failed: ${result.description || response.status}`);
  }
}

function report(snapshot, update) {
  const recent = summarize(update.fresh);
  const depth = snapshot.depth;
  const spread =
    depth.bestBid > 0 ? ((depth.bestAsk - depth.bestBid) / depth.bestBid) * 100 : 0;
  return [
    "📊 FTR/USDT 监控",
    `时间：${timeParts(snapshot.capturedAt).dateTime} (${TIMEZONE})`,
    `最新价：${snapshot.ticker.close ?? "--"} USDT`,
    `24h 成交额：${money(snapshot.ticker.turnover)} USDT`,
    "",
    `买盘：${money(depth.bid.notional)} USDT（${depth.bidLevels}档）`,
    `卖盘：${money(depth.ask.notional)} USDT（${depth.askLevels}档）`,
    `盘口失衡：${depth.imbalance.toFixed(2)}%`,
    `价差：${spread.toFixed(3)}%`,
    "",
    `本次新增主动买入：${money(recent.BUY.notional)} USDT / ${recent.BUY.count}笔`,
    `本次新增主动卖出：${money(recent.SELL.notional)} USDT / ${recent.SELL.count}笔`,
    `今日主动买入：${money(update.daySummary.BUY.notional)} USDT / ${update.daySummary.BUY.count}笔`,
    `今日主动卖出：${money(update.daySummary.SELL.notional)} USDT / ${update.daySummary.SELL.count}笔`,
    `今日净主动买入：${money(update.daySummary.BUY.notional - update.daySummary.SELL.notional)} USDT`,
  ].join("\n");
}

const state = await loadState();
const snapshot = await collect();
const update = addNewTrades(state, snapshot.trades, snapshot.capturedAt);
await sendTelegram(report(snapshot, update));

const now = Date.now();
if (
  Math.abs(snapshot.depth.imbalance) >= ALERT_THRESHOLD &&
  now - state.lastAlertAt >= 15 * 60_000
) {
  await sendTelegram(
    [
      "⚠️ FTR/USDT 盘口预警",
      `时间：${timeParts(snapshot.capturedAt).dateTime} (${TIMEZONE})`,
      `盘口失衡：${snapshot.depth.imbalance.toFixed(2)}%`,
      `买盘：${money(snapshot.depth.bid.notional)} USDT`,
      `卖盘：${money(snapshot.depth.ask.notional)} USDT`,
    ].join("\n"),
  );
  state.lastAlertAt = now;
}
await saveState(state);
