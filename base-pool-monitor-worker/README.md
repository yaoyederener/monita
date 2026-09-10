# FTREX BNB Chain Funds Monitor

Cloudflare Worker that measures FTREX platform-wide BSC-USDT deposits and withdrawals directly from confirmed on-chain business events. It sends a Beijing-time daily report to Telegram and immediately mentions `@juzhangniubi666` when a new related gateway, receiver, withdrawal contract, source vault, or operator wallet is cross-verified.

Confirmed starting points:

- Deposit gateway: `0x00000000110e73585338df0e7f91bf70ed3bd4c4`
- Deposit receiver: `0xa0277eb181577b712813b8f0a11b931bd82fef4a`
- Withdrawal contract/source: `0x301173ccf602050c0bbdd36b6af9cf59d0000000`
- Withdrawal operator: `0x6e1469c12a996376c4aff61daa25741ef97bbceb`
- Asset: BSC-USDT `0x55d398326f99059ff775485246999027b3197955`

The legacy Worker name and Durable Object class are intentionally retained so the existing Cloudflare object and Telegram configuration can be reused. LAPTOP token monitoring remains removed; this scheduled job runs only the FTREX funds monitor.

Secrets (`BOT_TOKEN`, `CHAT_ID`, `BSC_RPC`, optional `BSCSCAN_KEY`) stay in GitHub Actions and are sent to the Worker only through a repository-, branch-, and workflow-bound GitHub OIDC request. The public `/health` endpoint never returns secret values.
