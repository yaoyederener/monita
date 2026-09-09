import fs from "node:fs/promises";
import path from "node:path";
import { chromium } from "playwright";
import {
  addNewTrades,
  analyzeDepth,
  appendSnapshot,
  evaluateSignal,
  largestTrade,
  summarize,
  timeParts,
} from "./core.js";

const TIMEZONE = process.env.MONITOR_TIMEZONE || "Asia/Shanghai";
const FORCE_REPORT = process.env.FORCE_REPORT === "true";
const BOT_TOKEN = process.env.BOT_TOKEN || "";
const CHAT_ID = process.env.CHAT_ID || "";
const MENTION = process.env.TELEGRAM_MENTION || "@juzhangniubi666";
const STATE_FILE = path.resolve(".state/ftrex.json");
const TRADE_URL = "https://ftrex.io/zh/exchange/ftr_usdt/kline";
const DEPTH_URL = "https://ftrex.io/api/market/exchange-plate-mini?symbol=FTR_USDT";
const TICKER_URL = "https://ftrex.io/api/market/symbol-thumb-single?symbol=FTR_USDT";
const OPTIONS = {
  imbalanceThreshold: numberEnv("ALERT_IMBALANCE_PCT", 35),
  confirmations: numberEnv("HIGH_CONFIRMATIONS", 3),
  priceMoveThreshold: numberEnv("HIGH_PRICE_MOVE_PCT", 2),
  spreadThreshold: numberEnv("SPREAD_ALERT_PCT", 1),
  depthDropThreshold: numberEnv("DEPTH_DROP_PCT", 60),
  staleMinutes: numberEnv("DATA_STALE_MINUTES", 20),
  slippageQuote: numberEnv("SLIPPAGE_QUOTE_USDT", 1000),
};

if (!BOT_TOKEN || !CHAT_ID) throw new Error("Missing BOT_TOKEN or CHAT_ID GitHub Secret");

function numberEnv(name, fallback) {
  const value = Number(process.env[name] ?? fallback);
  if (!Number.isFinite(value) || value < 0) throw new Error(`${name} must be a non-negative number`);
  return value;
}

function defaultState() {
  return {
    seen: [],
    days: {},
    recentTrades: [],
    snapshots: [],
    signal: { direction: null, count: 0, lastAt: 0 },
    lastHighAlertAt: 0,
    lastWatchAlertAt: 0,
    lastGapAlertAt: 0,
    lastFailureAlertAt: 0,
    consecutiveFailures: 0,
    lastSuccessAt: 0,
    lastHourlyReportKey: "",
    lastDailyReportDay: "",
  };
}

async function loadState() {
  try {
    const saved = JSON.parse(await fs.readFile(STATE_FILE, "utf8"));
    const defaults = defaultState();
    return {
      ...defaults,
      ...saved,
      seen: Array.isArray(saved.seen) ? saved.seen : [],
      days: saved.days && typeof saved.days === "object" ? saved.days : {},
      recentTrades: Array.isArray(saved.recentTrades) ? saved.recentTrades : [],
      snapshots: Array.isArray(saved.snapshots) ? saved.snapshots : [],
      signal: saved.signal && typeof saved.signal === "object" ? saved.signal : defaults.signal,
    };
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
    return defaultState();
  }
}

async function saveState(state) {
  await fs.mkdir(path.dirname(STATE_FILE), { recursive: true });
  const temp = `${STATE_FILE}.tmp`;
  await fs.writeFile(temp, `${JSON.stringify(state, null, 2)}\n`, "utf8");
  await fs.rename(temp, STATE_FILE);
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
    return {
      capturedAt: new Date(),
      ticker,
      trades,
      depth: analyzeDepth(depth, OPTIONS.slippageQuote),
    };
  } finally {
    await browser.close();
  }
}

const money = (value) =>
  Number(value || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const signedMoney = (value) => `${Number(value) >= 0 ? "+" : ""}${money(value)}`;
const percent = (value) => value === null || value === undefined
  ? "数据不足"
  : `${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(2)}%`;
const priceText = (value) => {
  const price = Number(value);
  return Number.isFinite(price) ? price.toLocaleString("en-US", { maximumFractionDigits: 12 }) : "--";
};
const ratio = (first, second) => Number(second) > 0 ? (Number(first) / Number(second)).toFixed(2) : "--";
const timeText = (date) => `${timeParts(date, TIMEZONE).dateTime}（北京时间）`;
const windowSummary = (trades) => {
  const summary = summarize(trades);
  return { ...summary, net: summary.BUY.notional - summary.SELL.notional };
};

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

function depthLine(label, band) {
  return `${label} 买 ${money(band.bid.notional)} / 卖 ${money(band.ask.notional)} USDT，失衡 ${percent(band.imbalance)}`;
}

function slippageText(result) {
  return result ? `${result.percent.toFixed(2)}%` : "深度不足";
}

function largestText(trade) {
  return trade
    ? `${trade.side === "BUY" ? "买" : "卖"} ${money(trade.price * trade.amount)} USDT（${trade.time}）`
    : "无新成交";
}

function hourlyReport(snapshot, update, changes, gapMinutes) {
  const five = windowSummary(update.windows[5]);
  const fifteen = windowSummary(update.windows[15]);
  const hour = windowSummary(update.windows[60]);
  const depth = snapshot.depth;
  const hasBaseline = gapMinutes !== null;
  const stale = hasBaseline && gapMinutes > OPTIONS.staleMinutes;
  const conclusion = !hasBaseline
    ? "正在建立连续样本，暂不判断买卖方向。"
    : stale
    ? "数据不连续，暂停给出买卖方向判断。"
    : Math.abs(depth.bands["1"].imbalance) < 15 && Math.abs(hour.net) < 100
      ? "盘口与主动成交暂时接近平衡。"
      : depth.bands["1"].imbalance > 0 && hour.net > 0
        ? "买盘深度和主动成交同向偏强，继续观察是否持续。"
        : depth.bands["1"].imbalance < 0 && hour.net < 0
          ? "卖压和主动成交同向偏弱，注意流动性风险。"
          : "盘口与真实成交方向不一致，暂不追单。";
  return [
    "📊 FTR/USDT 每小时监控",
    `⏰ ${timeText(snapshot.capturedAt)}`,
    `💰 最新价：${priceText(snapshot.ticker.close)} USDT`,
    `📈 涨跌：5m ${percent(changes[5])}｜15m ${percent(changes[15])}｜1h ${percent(changes[60])}`,
    `💵 24h成交额：${money(snapshot.ticker.turnover)} USDT`,
    "",
    "📖 实时盘口",
    `买一/卖一：${priceText(depth.bestBid)} / ${priceText(depth.bestAsk)}`,
    `价差：${percent(depth.spreadPct)}`,
    depthLine("0.5%内：", depth.bands["0.5"]),
    depthLine("1%内：", depth.bands["1"]),
    depthLine("2%内：", depth.bands["2"]),
    `成交 ${money(depth.slippageQuote)} USDT 预计滑点：买 ${slippageText(depth.buySlippage)}｜卖 ${slippageText(depth.sellSlippage)}`,
    "",
    "🔥 主动成交净额",
    `5m ${signedMoney(five.net)}｜15m ${signedMoney(fifteen.net)}｜1h ${signedMoney(hour.net)} USDT`,
    `1h 主买 ${money(hour.BUY.notional)} / 主卖 ${money(hour.SELL.notional)} USDT`,
    `1h 最大单：${largestText(largestTrade(update.windows[60]))}`,
    "",
    `🛰️ 数据状态：${!hasBaseline ? "建立基线中" : stale ? `异常（上次采集距今 ${gapMinutes.toFixed(0)} 分钟）` : "正常"}`,
    `🧭 综合判断：${conclusion}`,
    "说明：挂单可随时撤销，监控信号不等于投资建议。",
  ].join("\n");
}

function signalReport(snapshot, signal, high) {
  const directionText = signal.direction === "BUY" ? "买盘增强" : "卖压增强";
  const riskTitle = signal.direction === "SELL" ? "高风险预警" : "高强度动量预警";
  const depth = snapshot.depth;
  return [
    ...(high ? [MENTION] : []),
    `${high ? "🚨" : "⚠️"} FTR/USDT ${high ? riskTitle : "盘口观察"}`,
    `⏰ ${timeText(snapshot.capturedAt)}`,
    `💰 最新价：${priceText(snapshot.ticker.close)} USDT`,
    `📈 5分钟涨跌：${percent(signal.changes[5])}`,
    `🔥 5分钟主动成交：买 ${money(signal.five.BUY.notional)} / 卖 ${money(signal.five.SELL.notional)} USDT`,
    `净主动买入：${signedMoney(signal.net)} USDT`,
    `📖 买一/卖一：${priceText(depth.bestBid)} / ${priceText(depth.bestAsk)}（价差 ${percent(depth.spreadPct)}）`,
    depthLine("1%内：", depth.bands["1"]),
    `连续确认：${signal.count}/${OPTIONS.confirmations} 次`,
    `触发依据：${signal.evidence.join("；")}`,
    `结论：${directionText}，${high ? "多项指标已交叉确认，请立即复核盘口与成交。" : "尚未得到成交或价格充分确认。"}`,
    "提示：该通知只反映公开市场数据，不构成买卖建议。",
  ].join("\n");
}

function dailyReport(day, summary) {
  const buy = summary.BUY?.notional || 0;
  const sell = summary.SELL?.notional || 0;
  const net = buy - sell;
  return [
    "📅 FTR/USDT 每日成交总结",
    `日期：${day}（北京时间）`,
    `收盘参考价：${priceText(summary.lastPrice)} USDT`,
    `24小时成交额：${money(summary.turnover)} USDT`,
    `🟢 主动买入：${money(buy)} USDT / ${summary.BUY?.count || 0}笔`,
    `🔴 主动卖出：${money(sell)} USDT / ${summary.SELL?.count || 0}笔`,
    `${net >= 0 ? "✅" : "🔻"} 净主动买入：${signedMoney(net)} USDT`,
    `主买/主卖比例：${ratio(buy, sell)}`,
  ].join("\n");
}

function failureReport(state, error, now) {
  const lastValid = state.lastSuccessAt ? timeText(new Date(state.lastSuccessAt)) : "暂无";
  return [
    MENTION,
    "🛑 FTR/USDT 监控数据失效",
    `⏰ ${timeText(now)}`,
    `连续失败：${state.consecutiveFailures} 次`,
    `最后有效数据：${lastValid}`,
    `错误摘要：${String(error.message || error).slice(0, 240)}`,
    "已停止输出买卖方向结论；请检查 FTREX 页面/API 或 GitHub Actions。",
  ].join("\n");
}

function gapReport(gapMinutes, now) {
  return [
    MENTION,
    "🛰️ FTR/USDT 监控采集间隔异常",
    `⏰ ${timeText(now)}`,
    `本次与上次成功采集间隔：${gapMinutes.toFixed(0)} 分钟`,
    `预期不超过：${OPTIONS.staleMinutes} 分钟`,
    "缺口期间成交可能未被完整记录，本轮暂停给出买卖方向结论。",
  ].join("\n");
}

const state = await loadState();
let snapshot;
try {
  snapshot = await collect();
} catch (error) {
  const now = new Date();
  state.consecutiveFailures = Number(state.consecutiveFailures || 0) + 1;
  if (
    state.consecutiveFailures >= 3 &&
    now.getTime() - Number(state.lastFailureAlertAt || 0) >= 60 * 60_000
  ) {
    await sendTelegram(failureReport(state, error, now));
    state.lastFailureAlertAt = now.getTime();
  }
  await saveState(state);
  console.warn(`FTREX collection failed (${state.consecutiveFailures}):`, error);
  process.exit(0);
}

const nowMs = snapshot.capturedAt.getTime();
const priorFailures = Number(state.consecutiveFailures || 0);
const gapMinutes = state.lastSuccessAt ? (nowMs - Number(state.lastSuccessAt)) / 60_000 : null;
state.consecutiveFailures = 0;
state.lastSuccessAt = nowMs;

const update = addNewTrades(state, snapshot.trades, snapshot.capturedAt, TIMEZONE);
const latestPrice = Number(snapshot.ticker.close);
const latestTurnover = Number(snapshot.ticker.turnover);
if (Number.isFinite(latestPrice)) update.daySummary.lastPrice = latestPrice;
if (Number.isFinite(latestTurnover)) update.daySummary.turnover = latestTurnover;
update.daySummary.updatedAt = nowMs;

const signal = evaluateSignal(state, snapshot, update.windows, OPTIONS);
const changes = signal.changes || { 5: null, 15: null, 60: null };
appendSnapshot(state, snapshot);

if (
  gapMinutes !== null &&
  gapMinutes > OPTIONS.staleMinutes &&
  nowMs - Number(state.lastGapAlertAt || 0) >= 60 * 60_000
) {
  await sendTelegram(gapReport(gapMinutes, snapshot.capturedAt));
  state.lastGapAlertAt = nowMs;
}

if (priorFailures >= 3) {
  await sendTelegram([
    MENTION,
    "✅ FTR/USDT 监控已恢复",
    `⏰ ${timeText(snapshot.capturedAt)}`,
    `中断前连续失败：${priorFailures} 次`,
    "当前已重新取得价格、盘口和成交数据。",
  ].join("\n"));
}

const dataFresh = gapMinutes === null || gapMinutes <= OPTIONS.staleMinutes;
if (
  dataFresh &&
  signal.severity === "HIGH" &&
  nowMs - Number(state.lastHighAlertAt || 0) >= 30 * 60_000
) {
  await sendTelegram(signalReport(snapshot, signal, true));
  state.lastHighAlertAt = nowMs;
} else if (
  dataFresh &&
  signal.severity === "WATCH" &&
  nowMs - Number(state.lastWatchAlertAt || 0) >= 60 * 60_000
) {
  await sendTelegram(signalReport(snapshot, signal, false));
  state.lastWatchAlertAt = nowMs;
}

const pendingDailyDay = Object.keys(state.days)
  .filter((day) => day < update.day && day > state.lastDailyReportDay)
  .sort()
  .at(-1);
if (pendingDailyDay) {
  await sendTelegram(dailyReport(pendingDailyDay, state.days[pendingDailyDay]));
  state.lastDailyReportDay = pendingDailyDay;
}

const currentHourKey = timeParts(snapshot.capturedAt, TIMEZONE).hourKey;
if (FORCE_REPORT || state.lastHourlyReportKey !== currentHourKey) {
  await sendTelegram(hourlyReport(snapshot, update, changes, gapMinutes));
  state.lastHourlyReportKey = currentHourKey;
}

await saveState(state);
