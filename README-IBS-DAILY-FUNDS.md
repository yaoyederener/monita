# IBS Daily Funds Ledger

This monitor keeps a Beijing-time daily USDT ledger for the IBS/USDT pair and
the project's configured treasury and RBS contracts.

## Reported figures

- IBS/USDT buy and sell counts, IBS volume, and USDT flow from Pair `Swap` logs.
- LP opening/closing USDT balance, actual balance change, and the non-trading
  adjustment between actual balance change and swap net flow.
- Combined USDT balances and external flows for Safety Treasury, BTCB Treasury,
  and Worldpool Treasury.
- Combined USDT balances and external flows for RBS Stabilizer and RBS Executor.
- Combined LP + treasury + RBS opening/closing balance and net daily depletion.
- Internal transfers between monitored addresses, kept separate so they are not
  counted twice as project spending.

The workflow scans every 15 minutes and sends the completed previous-day report
after 00:10 Asia/Shanghai. A manual workflow run sends an immediate report for
the current day to verify Telegram delivery and the accounting output.

## Accounting definitions

- Trade net flow = buy-side USDT into LP minus sell-side USDT out of LP.
- Trade depletion = `max(sell USDT - buy USDT, 0)`.
- LP actual change = closing LP USDT balance minus opening LP USDT balance.
- Non-trading adjustment = LP actual change minus trade net flow.
- Project net depletion = `max(total opening balance - total closing balance, 0)`
  across LP, treasury, and RBS.

All balances are on-chain BEP-20 USDT balances. They are not a substitute for
internal protocol accounting or liabilities that are not represented by those
balances.
