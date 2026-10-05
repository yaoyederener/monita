# FTREX BNB Chain Funds Monitor

Cloudflare Worker that measures FTREX BNB Chain USDT flows from on-chain business events and the verified USDT transfer path. Existing wallet discovery, Durable Object storage and Telegram credentials are retained.

Five-minute scans keep the ledger current. Scheduled Telegram reports run **twice per Beijing calendar day, at 09:00 and 21:00 (Asia/Shanghai)**, after scanning catches up. Each report covers that day's 00:00 through the displayed scanned block timestamp. It compares with the previous day's same notification slot, never with a full previous day. Polls, restarts and overlapping scans deduplicate the saved notification slot. Backlogged scans delay the report; missed older slots are not replayed as a burst. No midnight, three-hour digest or immediate address-change notification is sent. Address discovery still updates the monitored set.

The selected report contains network/asset, timezone, cutoff, external inflow and outflow amounts/counts/address counts, signed net inflow, same-slot changes, known treasury balance and its change, internal transfers, inflow TOP10 and outflow TOP10, and links to the largest single inflow/outflow as the large-transfer reference. No arbitrary large-transfer threshold is assumed.

Both rankings combine an address's gross daily amount using exact integer arithmetic and list up to ten distinct addresses. Internal transfers between verified platform addresses are counted separately and never treated as user deposits or withdrawals. The known treasury balance sums distinct deposit receivers and withdrawal source vaults at the same scanned block; it is not a platform-wide asset or solvency claim.

Legacy stored days may lack withdrawal address totals or internal-transfer coverage. Those fields show unavailable rather than a partial ranking or a false zero; complete data starts with the next full Beijing day. Same-slot percentage and balance comparisons become available after the first day of snapshots. An absent balance response shows unavailable. A stale chain head delays the report. Yesterday's zero baseline is explicitly marked instead of producing an infinite percentage.

Authenticated POST `/run` advances scanning without an extra notification; a JSON body `{ "report": true }` explicitly requests a manual snapshot. `/health` exposes `fundsReportMode: "twice_daily"`, the two report times and the last notified slot, without secret values.

Confirmed starting points:

- Deposit gateway: `0x00000000110e73585338df0e7f91bf70ed3bd4c4`
- Deposit receiver: `0xa0277eb181577b712813b8f0a11b931bd82fef4a`
- Withdrawal contract/source: `0x301173ccf602050c0bbdd36b6af9cf59d0000000`
- Withdrawal operator: `0x6e1469c12a996376c4aff61daa25741ef97bbceb`
- Asset: BSC-USDT `0x55d398326f99059ff775485246999027b3197955`

The legacy Worker name and Durable Object class are intentionally retained so the existing Cloudflare object and Telegram configuration can be reused. LAPTOP token monitoring remains removed; this scheduled job runs only the FTREX funds monitor.

The first run backfills 500,000 blocks (enough to close the previous Beijing day) and uses Durable Object alarms to continue in bounded batches without exceeding Cloudflare's per-invocation subrequest limit. After catch-up, the five-minute Cron Trigger keeps the ledger current.

Secrets (`BOT_TOKEN`, `CHAT_ID`, `BSC_RPC`, optional `BSCSCAN_KEY`) stay in GitHub Actions and are sent to the Worker only through a repository-, branch-, and workflow-bound GitHub OIDC request. The public `/health` endpoint never returns secret values.
