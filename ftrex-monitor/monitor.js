import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";

const TIMEZONE = process.env.MONITOR_TIMEZONE || "Asia/Shanghai";
const ALERT_THRESHOLD = Number(process.env.ALERT_IMBALANCE_PCT || 35);
const FORCE_REPORT = process.env.FORCE_REPORT === "true";
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
    hourKey: `${values.year}-${values.month}-${values.day}T${values.hour}`,
  };
}

async function loadState() {
  try {
    const state = JSON.parse(await fs.readFile(STATE_FILE, "utf8"));
    return {
      seen: Array.isArray(state.seen) ? state.seen : [],
      days: state.days && typeof state.days === "object" ? state.days : {},
      recentTrades: Array.isArray(state.recentTrades) ? state.recentTrades : [],
      lastAlertAt: Number(state.lastAlertAt) || 0,
      lastHourlyReportKey: state.lastHourlyReportKey || "",
      lastDailyReportDay: state.lastDailyReportDay || "",
    };
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
    return {
      seen: [],
      days: {},
      recentTrades: [],
      lastAlertAt: 0,
      lastHourlyReportKey: "",
      lastDailyReportDay: "",
    };
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
    fresh.push({ ...trade, observedAt: now.getTime() });
  }

  const increment = summarize(fresh);
  for (const side of ["BUY", "SELL"]) {
    state.days[day][side].count += increment[side].count;
    state.days[day][side].amount += increment[side].amount;
    state.days[day][side].notional += increment[side].notional;
  }
  state.seen = [...seen].filter((key) => key.startsWith(`${day}|`)).slice(-20_000);
  state.recentTrades = [...state.recentTrades, ...fresh].filter(
    (trade) => Number(trade.observedAt) >= now.getTime() - 25 * 60 * 60_000,
  );
  for (const oldDay of Object.keys(state.days).sort().slice(0, -7)) {
    delete state.days[oldDay];
  }
  const recentHour = state.recentTrades.filter(
    (trade) => Number(trade.observedAt) >= now.getTime() - 60 * 60_000,
  );
  return { day, fresh, recentHour, daySummary: state.days[day] };
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

const signedMoney = (value) => `${Number(value) >= 0 ? "+" : ""}${money(value)}`;

const priceText = (value) => {
  const price = Number(value);
  return Number.isFinite(price) ? price.toLocaleString("en-US", { maximumFractionDigits: 12 }) : "--";
};

const ratio = (first, second) =>
  Number(second) > 0 ? (Number(first) / Number(second)).toFixed(2) : "--";

function strengthLabel(imbalance) {
  if (imbalance >= 5) return "买盘较强";
  if (imbalance <= -5) return "卖盘较强";
  return "相对均衡";
}

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

function hourlyReport(snapshot, update) {
  const recent = summarize(update.recentHour);
  const depth = snapshot.depth;
  const dayNet = update.daySummary.BUY.notional - update.daySummary.SELL.notional;
  const hourNet = recent.BUY.notional - recent.SELL.notional;
  return [
    "📊 FTR/USDT 每小时监控",
    "",
    `⏰ 时间：${timeParts(snapshot.capturedAt).dateTime}（北京时间）`,
    `💰 最新价：${priceText(snapshot.ticker.close)} USDT`,
    `📈 24小时成交额：${money(snapshot.ticker.turnover)} USDT`,
    "",
    "📖 当前20档盘口",
    `🟢 买盘挂单：${money(depth.bid.notional)} USDT`,
    `🔴 卖盘挂单：${money(depth.ask.notional)} USDT`,
    `⚖️ 买卖比例：${ratio(depth.bid.notional, depth.ask.notional)} : 1`,
    `${depth.imbalance >= 0 ? "📈" : "📉"} 盘口失衡：${depth.imbalance >= 0 ? "+" : ""}${depth.imbalance.toFixed(2)}%（${strengthLabel(depth.imbalance)}）`,
    "",
    "🕐 最近1小时成交",
    `🟢 主动买入：${money(recent.BUY.notional)} USDT / ${recent.BUY.count}笔`,
    `🔴 主动卖出：${money(recent.SELL.notional)} USDT / ${recent.SELL.count}笔`,
    `${hourNet >= 0 ? "✅" : "🔻"} 净主动买入：${signedMoney(hourNet)} USDT`,
    "",
    "📅 今日累计成交",
    `🟢 主动买入：${money(update.daySummary.BUY.notional)} USDT / ${update.daySummary.BUY.count}笔`,
    `🔴 主动卖出：${money(update.daySummary.SELL.notional)} USDT / ${update.daySummary.SELL.count}笔`,
    `${dayNet >= 0 ? "✅" : "🔻"} 净主动买入：${signedMoney(dayNet)} USDT`,
  ].join("\n");
}

function alertReport(snapshot) {
  const depth = snapshot.depth;
  const buyAlert = depth.imbalance >= 0;
  const stronger = buyAlert ? depth.bid.notional : depth.ask.notional;
  const weaker = buyAlert ? depth.ask.notional : depth.bid.notional;
  return [
    `⚠️ FTR/USDT ${buyAlert ? "买盘增强" : "卖压"}预警`,
    "",
    `⏰ 时间：${timeParts(snapshot.capturedAt).dateTime}（北京时间）`,
    `💰 最新价：${priceText(snapshot.ticker.close)} USDT`,
    "",
    `🟢 买盘挂单：${money(depth.bid.notional)} USDT`,
    `🔴 卖盘挂单：${money(depth.ask.notional)} USDT`,
    `⚖️ ${buyAlert ? "买盘是卖盘" : "卖盘是买盘"}的：${ratio(stronger, weaker)}倍`,
    `${buyAlert ? "📈" : "📉"} 盘口失衡：${buyAlert ? "+" : ""}${depth.imbalance.toFixed(2)}%`,
    "",
    `触发原因：${buyAlert ? "买盘" : "卖盘"}失衡超过${ALERT_THRESHOLD}%`,
    `提示：挂单可能随时撤销，此预警不代表一定${buyAlert ? "上涨" : "下跌"}。`,
  ].join("\n");
}

function dailyReport(day, summary) {
  const buy = summary.BUY?.notional || 0;
  const sell = summary.SELL?.notional || 0;
  const net = buy - sell;
  const conclusion =
    net > 0
      ? "今日主动买入金额高于主动卖出金额。"
      : net < 0
        ? "今日主动卖出金额高于主动买入金额。"
        : "今日主动买入与主动卖出金额基本相同。";
  return [
    "📅 FTR/USDT 每日成交总结",
    "",
    `日期：${day}（北京时间）`,
    `收盘参考价：${priceText(summary.lastPrice)} USDT`,
    `24小时成交额：${money(summary.turnover)} USDT`,
    "",
    `🟢 今日主动买入：${money(buy)} USDT / ${summary.BUY?.count || 0}笔`,
    `🔴 今日主动卖出：${money(sell)} USDT / ${summary.SELL?.count || 0}笔`,
    `${net >= 0 ? "✅" : "🔻"} 净主动买入：${signedMoney(net)} USDT`,
    `⚖️ 主买/主卖比例：${ratio(buy, sell)}`,
    "",
    `结论：${conclusion}`,
  ].join("\n");
}

const state = await loadState();
const snapshot = await collect();
const update = addNewTrades(state, snapshot.trades, snapshot.capturedAt);
const latestPrice = Number(snapshot.ticker.close);
const latestTurnover = Number(snapshot.ticker.turnover);
if (Number.isFinite(latestPrice)) update.daySummary.lastPrice = latestPrice;
if (Number.isFinite(latestTurnover)) update.daySummary.turnover = latestTurnover;
update.daySummary.updatedAt = snapshot.capturedAt.getTime();

const now = Date.now();
const pendingDailyDay = Object.keys(state.days)
  .filter((day) => day < update.day && day > state.lastDailyReportDay)
  .sort()
  .at(-1);
if (pendingDailyDay) {
  await sendTelegram(dailyReport(pendingDailyDay, state.days[pendingDailyDay]));
  state.lastDailyReportDay = pendingDailyDay;
}

const currentHourKey = timeParts(snapshot.capturedAt).hourKey;
if (FORCE_REPORT || state.lastHourlyReportKey !== currentHourKey) {
  await sendTelegram(hourlyReport(snapshot, update));
  state.lastHourlyReportKey = currentHourKey;
}

if (
  Math.abs(snapshot.depth.imbalance) >= ALERT_THRESHOLD &&
  now - state.lastAlertAt >= 15 * 60_000
) {
  await sendTelegram(alertReport(snapshot));
  state.lastAlertAt = now;
}
await saveState(state);
