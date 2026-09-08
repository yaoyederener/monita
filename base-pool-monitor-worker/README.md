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

Set these as Cloudflare Worker secrets. Never commit their values.

- `BOT_TOKEN`
- `CHAT_ID`

## Deploy

```bash
npm install
npx wrangler secret put BOT_TOKEN
npx wrangler secret put CHAT_ID
npx wrangler deploy
```

The deployment sends one startup message to Telegram on the first Cron run. The public `/health` endpoint exposes only non-secret status.
