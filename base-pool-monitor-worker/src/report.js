import { emptyDay, escapeHtml, formatUnits, topAddresses } from "./lib.js";

const BSCSCAN = "https://bscscan.com";

export function dailyReport(env, day, rawDay, state, partial = false, context = {}) {
  const data = rawDay || emptyDay();
  const deposit = BigInt(data.deposit || "0");
  const withdrawal = BigInt(data.withdrawal || "0");
  const previous = context.previousSnapshot;
  const balance = context.walletBalance;
  const balanceText = balance == null ? "暂不可用" : `${formatUnits(balance)} USDT`;
  const balanceChange = balance != null && previous?.walletBalance != null
    ? `${formatUnits(BigInt(balance) - BigInt(previous.walletBalance))} USDT（${percentageChange(balance, previous.walletBalance)}）`
    : "暂无昨日同次通知数据";
  const cutoff = context.scannedAt ? `${beijingTime(context.scannedAt)}｜区块 ${state.lastBlock}` : `区块 ${state.lastBlock ?? "未知"}`;
  const mention = env.TELEGRAM_MENTION ? `${escapeHtml(env.TELEGRAM_MENTION)}\n` : "";
  return mention + `<b>【FTR 链上资金日报｜${escapeHtml(day)}】</b>\n\n` +
    `网络／币种：BNB Chain／USDT\n` +
    `统计时区：北京时间（Asia/Shanghai）\n` +
    `数据截至：${cutoff}\n\n` +
    `转入：${formatUnits(deposit)} USDT｜${data.depositCount || 0}笔｜${(data.depositUsers || []).length}个来源地址\n` +
    `转出：${formatUnits(withdrawal)} USDT｜${data.withdrawalCount || 0}笔｜${(data.withdrawalUsers || []).length}个接收地址\n` +
    `净流入：${formatUnits(deposit - withdrawal)} USDT\n` +
    `较昨日：转入${percentageChange(deposit, previous?.deposit)}，转出${percentageChange(withdrawal, previous?.withdrawal)}\n\n` +
    `已知钱包余额：${balanceText}\n` +
    `较昨日余额变化：${balanceChange}\n` +
    `内部调拨：${data.internalTransfersComplete === true ? `${formatUnits(data.internalTransfer || "0")} USDT（单独统计）` : "升级前记录不完整，暂不可用"}\n\n` +
    `转入 TOP10：\n${ranking(data, "deposit")}\n\n` +
    `转出 TOP10：\n${ranking(data, "withdrawal")}\n\n` +
    `大额转账（最大单笔参考）：\n${largestLine("转入", data.largestDeposit)}\n${largestLine("转出", data.largestWithdrawal)}`;
}

export function percentageChange(current, previous) {
  if (previous == null) return "暂无昨日同次通知数据";
  const base = BigInt(previous);
  const value = BigInt(current);
  if (base === 0n) return value === 0n ? "持平" : "不适用（昨日为0）";
  const difference = value - base;
  const absolute = difference < 0n ? -difference : difference;
  const basisPoints = (absolute * 10_000n + base / 2n) / base;
  return `${difference > 0n ? "+" : difference < 0n ? "-" : ""}${basisPoints / 100n}.${String(basisPoints % 100n).padStart(2, "0")}%`;
}

function ranking(day, type) {
  const result = topAddresses(day, type);
  if (!result.complete) return "升级前逐地址记录不完整，本日暂无法提供完整排行。";
  if (!result.rows.length) return "无";
  return result.rows.map((row, i) => `${i + 1}｜<a href="${BSCSCAN}/address/${escapeHtml(row.user)}">${escapeHtml(row.user)}</a>｜${formatUnits(row.amount)} USDT`).join("\n");
}

function largestLine(label, item) {
  return item ? `${label}：${formatUnits(item.amount)} USDT｜<a href="${BSCSCAN}/tx/${escapeHtml(item.txHash)}">交易</a>` : `${label}：无`;
}

function beijingTime(unixSeconds) {
  return new Date(Number(unixSeconds) * 1_000 + 8 * 60 * 60 * 1_000).toISOString().replace("T", " ").slice(0, 19);
}
