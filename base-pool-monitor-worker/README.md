# LAPTOP Base Pool Monitor

Cloudflare Worker that checks the Base token below every minute and sends important events to Telegram.

- Token: `0xB095274743941e953c746F9C228DA9c18Bb6ec29`
- Telegram mention: `@juzhangniubi666`
- Runtime: Cloudflare Worker Cron Trigger
- State and deduplication: one SQLite-backed Durable Object

## Alerts

- New DEX pool and first usable liquidity
- First trades, large liquidity additions/removals, and large price moves
- Buy tax, sell tax, pool fee, and GoPlus risk-field changes
- Transfers of at least 1,000,000 LAPTOP
- Ownership, LayerZero peer, preCrime, and inspector changes

The monitor uses the exact token contract address. It does not treat similarly named tokens as LAPTOP.

## Secrets

Keep these in GitHub Actions Secrets. Never commit their values.

- `BOT_TOKEN`
- `CHAT_ID`

The workflow obtains a short-lived GitHub OIDC identity and sends the two existing
secrets only to this Worker's `/bootstrap` endpoint. The Worker verifies the exact
repository, branch, and workflow before storing them in its Durable Object. No
long-lived Cloudflare API token is required.

## Deploy and operate

```bash
npm install
npm test
npx wrangler deploy
```

The live Worker uses the Cron Trigger `* * * * *`. It sends one startup message to
Telegram on initialization. The public `/health` endpoint exposes only non-secret
status.
