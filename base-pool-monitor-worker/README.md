# FTREX BNB Chain Funds Monitor

Cloudflare Worker that measures FTREX platform-wide BSC-USDT deposits and withdrawals directly from confirmed on-chain business events. It sends a Beijing-time daily report to Telegram and immediately mentions `@juzhangniubi666` when a new related gateway, receiver, withdrawal contract, source vault, or operator wallet is cross-verified.

Five-minute scans keep the ledger current. Funds statistics are sent only once
per Beijing calendar day, after midnight and after scanning catches up, covering
the previous day. The former three-hour digest is disabled, including any pending
digest saved before the upgrade. Address-system change alerts remain immediate.
Manual `/run` requests still explicitly request a current-day snapshot.

The daily report includes the address with the largest **sum of daily deposits**,
its complete address, USDT total, and deposit count. Multiple deposits from the
same address are combined using exact integer arithmetic. Withdrawals do not
reduce gross deposits. The largest single deposit is shown separately. Ties are
labelled and one tied address is displayed. Days saved before this upgrade lack
per-address totals: their ranking is marked unavailable rather than guessed;
complete rankings begin with the next full Beijing day. No historical all-time
deposit total is claimed.

Confirmed starting points:

- Deposit gateway: `0x00000000110e73585338df0e7f91bf70ed3bd4c4`
- Deposit receiver: `0xa0277eb181577b712813b8f0a11b931bd82fef4a`
- Withdrawal contract/source: `0x301173ccf602050c0bbdd36b6af9cf59d0000000`
- Withdrawal operator: `0x6e1469c12a996376c4aff61daa25741ef97bbceb`
- Asset: BSC-USDT `0x55d398326f99059ff775485246999027b3197955`

The legacy Worker name and Durable Object class are intentionally retained so the existing Cloudflare object and Telegram configuration can be reused. LAPTOP token monitoring remains removed; this scheduled job runs only the FTREX funds monitor.

The first run backfills 500,000 blocks (enough to close the previous Beijing day) and uses Durable Object alarms to continue in bounded batches without exceeding Cloudflare's per-invocation subrequest limit. After catch-up, the five-minute Cron Trigger keeps the ledger current.

Secrets (`BOT_TOKEN`, `CHAT_ID`, `BSC_RPC`, optional `BSCSCAN_KEY`) stay in GitHub Actions and are sent to the Worker only through a repository-, branch-, and workflow-bound GitHub OIDC request. The public `/health` endpoint never returns secret values.
