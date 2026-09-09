export const emptySummary = () => ({
  BUY: { count: 0, amount: 0, notional: 0 },
  SELL: { count: 0, amount: 0, notional: 0 },
});

export function summarize(trades) {
  const result = emptySummary();
  for (const trade of trades) {
    const side = trade.side === "SELL" ? "SELL" : "BUY";
    result[side].count += 1;
    result[side].amount += Number(trade.amount) || 0;
    result[side].notional += (Number(trade.price) || 0) * (Number(trade.amount) || 0);
  }
  return result;
}

export function timeParts(date, timeZone = "Asia/Shanghai") {
  const values = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone,
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
  const hour = values.hour === "24" ? "00" : values.hour;
  return {
    day: `${values.year}-${values.month}-${values.day}`,
    dateTime: `${values.year}-${values.month}-${values.day} ${hour}:${values.minute}:${values.second}`,
    hourKey: `${values.year}-${values.month}-${values.day}T${hour}`,
    secondsOfDay: Number(hour) * 3600 + Number(values.minute) * 60 + Number(values.second),
  };
}

function tradeSeconds(value) {
  const match = /^(\d{2}):(\d{2}):(\d{2})$/.exec(value || "");
  if (!match) return null;
  return Number(match[1]) * 3600 + Number(match[2]) * 60 + Number(match[3]);
}

export function attachExecutionTimes(trades, capturedAt, timeZone = "Asia/Shanghai") {
  const capturedSeconds = timeParts(capturedAt, timeZone).secondsOfDay;
  return trades
    .map((trade) => {
      const seconds = tradeSeconds(trade.time);
      if (seconds === null) return null;
      let ageSeconds = capturedSeconds - seconds;
      if (ageSeconds < -12 * 3600) ageSeconds += 24 * 3600;
      if (ageSeconds < 0 && ageSeconds >= -5 * 60) ageSeconds = 0;
      if (ageSeconds < 0) ageSeconds += 24 * 3600;
      const executedAt = capturedAt.getTime() - ageSeconds * 1000;
      return {
        ...trade,
        executedAt,
        executionDay: timeParts(new Date(executedAt), timeZone).day,
      };
    })
    .filter(Boolean);
}

export function addNewTrades(state, rawTrades, capturedAt, timeZone = "Asia/Shanghai") {
  const currentDay = timeParts(capturedAt, timeZone).day;
  state.days[currentDay] ||= emptySummary();
  const seen = new Set(state.seen);
  const occurrences = new Map();
  const fresh = [];
  const trades = attachExecutionTimes(rawTrades, capturedAt, timeZone);

  for (const trade of trades) {
    const base = [trade.time, trade.side, trade.price, trade.amount].join("|");
    const occurrence = (occurrences.get(base) || 0) + 1;
    occurrences.set(base, occurrence);
    const key = `${trade.executionDay}|${base}|${occurrence}`;
    if (seen.has(key)) continue;
    seen.add(key);
    fresh.push(trade);
    state.days[trade.executionDay] ||= emptySummary();
    const entry = state.days[trade.executionDay][trade.side];
    entry.count += 1;
    entry.amount += trade.amount;
    entry.notional += trade.price * trade.amount;
  }

  const retainedDays = new Set(Object.keys(state.days).sort().slice(-7));
  state.seen = [...seen]
    .filter((key) => retainedDays.has(key.slice(0, 10)))
    .slice(-20_000);
  state.recentTrades = [...state.recentTrades, ...fresh]
    .map((trade) => ({ ...trade, executedAt: Number(trade.executedAt || trade.observedAt) }))
    .filter((trade) => trade.executedAt >= capturedAt.getTime() - 25 * 60 * 60_000)
    .slice(-20_000);
  for (const oldDay of Object.keys(state.days).sort().slice(0, -7)) delete state.days[oldDay];

  return {
    day: currentDay,
    fresh,
    daySummary: state.days[currentDay],
    windows: Object.fromEntries(
      [5, 15, 60].map((minutes) => [
        minutes,
        state.recentTrades.filter(
          (trade) => trade.executedAt >= capturedAt.getTime() - minutes * 60_000,
        ),
      ]),
    ),
  };
}

const total = (items = []) =>
  items.reduce(
    (result, item) => {
      const price = Number(item.price);
      const amount = Number(item.amount);
      if (Number.isFinite(price) && Number.isFinite(amount) && price > 0 && amount > 0) {
        result.amount += amount;
        result.notional += price * amount;
      }
      return result;
    },
    { amount: 0, notional: 0 },
  );

function estimateSlippage(items, targetQuote, referencePrice, side) {
  if (!referencePrice || !Number.isFinite(targetQuote) || targetQuote <= 0) return null;
  let remainingBase = side === "SELL" ? targetQuote / referencePrice : Infinity;
  let remainingQuote = side === "BUY" ? targetQuote : Infinity;
  let filledBase = 0;
  let quote = 0;
  for (const item of items) {
    const price = Number(item.price);
    const amount = Number(item.amount);
    if (!(price > 0 && amount > 0)) continue;
    const fill = side === "BUY"
      ? Math.min(remainingQuote / price, amount)
      : Math.min(remainingBase, amount);
    filledBase += fill;
    quote += fill * price;
    remainingBase -= fill;
    remainingQuote -= fill * price;
    if (side === "BUY" ? remainingQuote <= 1e-8 : remainingBase <= 1e-12) break;
  }
  if ((side === "BUY" ? remainingQuote > 1e-8 : remainingBase > 1e-8) || filledBase <= 0) return null;
  const averagePrice = quote / filledBase;
  const percent = side === "BUY"
    ? ((averagePrice - referencePrice) / referencePrice) * 100
    : ((referencePrice - averagePrice) / referencePrice) * 100;
  return { averagePrice, percent: Math.max(0, percent) };
}

export function analyzeDepth(depth, slippageQuote = 1000) {
  const bids = (depth.bidItems || [])
    .map((item) => ({ price: Number(item.price), amount: Number(item.amount) }))
    .filter((item) => item.price > 0 && item.amount > 0)
    .sort((a, b) => b.price - a.price);
  const asks = (depth.askItems || [])
    .map((item) => ({ price: Number(item.price), amount: Number(item.amount) }))
    .filter((item) => item.price > 0 && item.amount > 0)
    .sort((a, b) => a.price - b.price);
  const bestBid = bids[0]?.price || 0;
  const bestAsk = asks[0]?.price || 0;
  const mid = bestBid && bestAsk ? (bestBid + bestAsk) / 2 : bestBid || bestAsk;
  const allBid = total(bids);
  const allAsk = total(asks);
  const bands = {};
  for (const percent of [0.5, 1, 2]) {
    const bid = total(bids.filter((item) => item.price >= mid * (1 - percent / 100)));
    const ask = total(asks.filter((item) => item.price <= mid * (1 + percent / 100)));
    const denominator = bid.notional + ask.notional;
    bands[String(percent)] = {
      bid,
      ask,
      imbalance: denominator ? ((bid.notional - ask.notional) / denominator) * 100 : 0,
    };
  }
  const denominator = allBid.notional + allAsk.notional;
  return {
    bid: allBid,
    ask: allAsk,
    bidLevels: bids.length,
    askLevels: asks.length,
    bestBid,
    bestAsk,
    mid,
    spreadPct: mid ? ((bestAsk - bestBid) / mid) * 100 : null,
    imbalance: denominator ? ((allBid.notional - allAsk.notional) / denominator) * 100 : 0,
    bands,
    slippageQuote,
    buySlippage: estimateSlippage(asks, slippageQuote, bestAsk, "BUY"),
    sellSlippage: estimateSlippage(bids, slippageQuote, bestBid, "SELL"),
  };
}

export function appendSnapshot(state, snapshot) {
  state.snapshots ||= [];
  state.snapshots.push({
    at: snapshot.capturedAt.getTime(),
    price: Number(snapshot.ticker.close),
    turnover: Number(snapshot.ticker.turnover),
    bid1: snapshot.depth.bands["1"].bid.notional,
    ask1: snapshot.depth.bands["1"].ask.notional,
    spreadPct: snapshot.depth.spreadPct,
  });
  state.snapshots = state.snapshots
    .filter((item) => Number(item.at) >= snapshot.capturedAt.getTime() - 48 * 60 * 60_000)
    .slice(-2_000);
}

export function changeForMinutes(snapshots, currentPrice, nowMs, minutes) {
  const target = nowMs - minutes * 60_000;
  const reference = [...snapshots]
    .filter((item) => Number(item.at) <= target && Number(item.price) > 0)
    .sort((a, b) => Number(b.at) - Number(a.at))[0];
  if (!reference || !(currentPrice > 0)) return null;
  return ((currentPrice - Number(reference.price)) / Number(reference.price)) * 100;
}

function dropPercent(previous, current) {
  return previous > 0 ? ((previous - current) / previous) * 100 : null;
}

export function evaluateSignal(state, snapshot, windows, options = {}) {
  const imbalanceThreshold = Number(options.imbalanceThreshold ?? 35);
  const confirmations = Number(options.confirmations ?? 3);
  const priceMoveThreshold = Number(options.priceMoveThreshold ?? 2);
  const spreadThreshold = Number(options.spreadThreshold ?? 1);
  const depthDropThreshold = Number(options.depthDropThreshold ?? 60);
  const now = snapshot.capturedAt.getTime();
  const band = snapshot.depth.bands["1"];
  const direction = band.imbalance >= imbalanceThreshold
    ? "BUY"
    : band.imbalance <= -imbalanceThreshold
      ? "SELL"
      : null;
  const previous = [...(state.snapshots || [])]
    .filter((item) => Number(item.at) < now)
    .sort((a, b) => Number(b.at) - Number(a.at))[0];
  const previousFresh = previous && now - Number(previous.at) <= 20 * 60_000;
  if (!direction) {
    state.signal = { direction: null, count: 0, lastAt: now };
    return { direction: null, count: 0, severity: "NONE", evidence: [] };
  }
  const priorSignal = state.signal || {};
  const continuous =
    previousFresh && priorSignal.direction === direction && now - Number(priorSignal.lastAt) <= 20 * 60_000;
  const count = continuous ? Number(priorSignal.count || 0) + 1 : 1;
  state.signal = { direction, count, lastAt: now };

  const five = summarize(windows[5] || []);
  const totalNotional = five.BUY.notional + five.SELL.notional;
  const net = five.BUY.notional - five.SELL.notional;
  const alignedShare = totalNotional > 0
    ? (direction === "BUY" ? five.BUY.notional : five.SELL.notional) / totalNotional
    : 0;
  const changes = Object.fromEntries(
    [5, 15, 60].map((minutes) => [
      minutes,
      changeForMinutes(state.snapshots || [], Number(snapshot.ticker.close), now, minutes),
    ]),
  );
  const priceAligned = changes[5] !== null &&
    (direction === "BUY" ? changes[5] >= priceMoveThreshold : changes[5] <= -priceMoveThreshold);
  const tradesAligned = alignedShare >= 0.65 && (direction === "BUY" ? net > 0 : net < 0);
  const currentSideDepth = direction === "BUY" ? band.ask.notional : band.bid.notional;
  const previousSideDepth = direction === "BUY" ? previous?.ask1 : previous?.bid1;
  const depthDropPct = previousFresh ? dropPercent(Number(previousSideDepth), currentSideDepth) : null;
  const depthDropped = depthDropPct !== null && depthDropPct >= depthDropThreshold;
  const spreadWide = snapshot.depth.spreadPct !== null && snapshot.depth.spreadPct >= spreadThreshold;
  const evidence = [
    `1%盘口失衡 ${band.imbalance >= 0 ? "+" : ""}${band.imbalance.toFixed(1)}%`,
  ];
  if (tradesAligned) evidence.push(`5分钟主动${direction === "BUY" ? "买入" : "卖出"}占比 ${(alignedShare * 100).toFixed(0)}%`);
  if (priceAligned) evidence.push(`5分钟价格${direction === "BUY" ? "上涨" : "下跌"} ${Math.abs(changes[5]).toFixed(2)}%`);
  if (depthDropped) evidence.push(`${direction === "BUY" ? "卖盘" : "买盘"}深度减少 ${depthDropPct.toFixed(1)}%`);
  if (spreadWide) evidence.push(`价差扩大至 ${snapshot.depth.spreadPct.toFixed(2)}%`);
  const corroborations = Number(tradesAligned) + Number(priceAligned) + Number(depthDropped) + Number(spreadWide);
  const severity = count >= confirmations && corroborations >= 1 ? "HIGH" : count >= confirmations ? "WATCH" : "NONE";
  return {
    direction,
    count,
    severity,
    evidence,
    changes,
    five,
    net,
    alignedShare,
    depthDropPct,
  };
}

export function largestTrade(trades) {
  return [...trades].sort((a, b) => b.price * b.amount - a.price * a.amount)[0] || null;
}
