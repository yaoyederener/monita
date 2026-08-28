# IBS Daily Funds Ledger

This monitor keeps a Beijing-time daily ledger for the IBS/USDT pair, the
project's configured treasury (USDT and BTCB), and RBS contracts.

## Reported figures

- IBS/USDT buy and sell counts, IBS volume, and USDT flow from Pair `Swap` logs.
- LP opening/closing USDT balance, buy/sell result, other balance changes, and
  the total daily LP change in plain-language increase/decrease wording.
- Combined USDT and BTCB opening/closing balances for Safety Treasury, BTCB
  Treasury, and Worldpool Treasury.
- Current BTCB/USDT valuation from PancakeSwap, with Binance BTCUSDT as a
  fallback. The same current price is applied to opening and closing BTCB so the
  reported daily change reflects balance movement rather than BTC price moves.
- Combined USDT opening/closing balance and daily change for RBS Stabilizer and
  RBS Executor.
- Combined LP + treasury (including BTCB valuation) + RBS opening/closing value
  and daily change.

The Telegram report intentionally omits external inflow/outflow, internal
transfer, trade-depletion, and large-external-outflow lines. Transfer data is
still processed internally so opening and closing balances reconstruct
correctly.

The workflow scans every 15 minutes, sends at most one cumulative current-day
update in each Beijing clock hour, and sends the completed previous-day report
after 00:10 Asia/Shanghai. Clock-hour deduplication tolerates GitHub schedule
delays without accidentally stretching the notification interval to two hours.
A manual workflow run sends an immediate current-day report to verify Telegram
delivery and the accounting output.

## Accounting definitions

- Buy/sell result = buy-side USDT into LP minus sell-side USDT out of LP.
- Other LP balance change = total LP balance change minus the buy/sell result.
- LP daily change = closing LP USDT balance minus opening LP USDT balance.
- Project daily change = closing value minus opening value across LP, treasury
  USDT, treasury BTCB valued at the report-time price, and RBS.

All balances are on-chain BEP-20 USDT or BTCB balances. They are not a
substitute for internal protocol accounting or liabilities that are not
represented by those balances.
