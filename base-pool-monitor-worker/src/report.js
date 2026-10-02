import { USDT, emptyDay, escapeHtml, formatUnits, shortAddress, topDepositor } from "./lib.js";

const BSCSCAN = "https://bscscan.com";

export function dailyReport(env, day, rawDay, state, partial = false) {
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
    topDepositorLine(data) +
    largestLine("最大单笔充值", data.largestDeposit) + largestLine("最大单笔提现", data.largestWithdrawal) +
    `地址状态：充值入口 ${state.entities.depositGateways.length}｜收款钱包 ${state.entities.depositReceivers.length}｜提现合约 ${state.entities.withdrawalContracts.length}｜出款金库 ${state.entities.withdrawalSources.length}\n` +
    `统计资产：BSC-USDT <code>${USDT}</code>`;
}

function largestLine(label, item) {
  return item ? `${label}：<b>${formatUnits(item.amount)} USDT</b>（${shortAddress(item.user)}） <a href="${BSCSCAN}/tx/${escapeHtml(item.txHash)}">交易</a>\n` : "";
}

function topDepositorLine(day) {
  const result = topDepositor(day);
  if (!result.complete) return "充值地址排行：升级前记录缺少逐地址合计，本日暂不排名；下一完整自然日开始提供。\n";
  if (!result.leader) return "当日累计充值最多地址：无充值。\n";
  const { user, amount, count } = result.leader;
  const tied = result.tiedCount > 1 ? `（共${result.tiedCount}个地址并列，展示其中一个）` : "";
  return `🏆 当日累计充值最多地址${tied}：<code>${escapeHtml(user)}</code>\n` +
    `累计充值：<b>${formatUnits(amount)} USDT</b>｜${count} 笔 ` +
    `<a href="${BSCSCAN}/address/${escapeHtml(user)}">查看地址</a>\n`;
}
