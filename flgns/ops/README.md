# FLGNS isolated operations

These files operate only the **four new FLGNS OTC deployments** on Anubis 6714.
They do not modify old OTCs, the existing fee bot, existing balances, or approvals.

## Deployment

1. Final reviewed Solidity build must be at `../contracts/build/FlgnsOtcEscrow.artifact.json`.
2. `python deploy.py` is the default **read-only chain** operation. It saves the
   public fixed `deployment-plan.json` when no plan exists. The plan pins four
   consecutive nonces, predicted addresses, exact artifact SHA256, fixed gas
   limits/prices, total maximum cost, anchor block/hash, and FLGNS/RM `totalSplit`
   baselines. Inspect and preserve this file; do not regenerate it after a partial deployment.
3. Copy this directory to `monita/flgns/ops`, artifacts to
   `monita/flgns/contracts/build`, and `flgns-deploy.yml` to `.github/workflows`.
   The script finds the artifact at that identical relative location.
4. The abandoned original plan remains evidence only. The **v2 workflow** uses
   `--plan flgns/ops/live-deployment-plan.json`. It generates the live plan only
   after acquiring the shared signer lock, uploads `flgns-deployment-v2-plan`
   successfully, then `deploy` calls `--broadcast` on that exact saved plan.
   Reruns first restore the latest `flgns-deployment-v2-journal` (live plan plus
   any deployment journal), falling back to v2-plan only when no v2 journal exists.
   It never imports the obsolete v1 journal or reallocates saved live nonces.
   Signing is allowed only with `GITHUB_ACTIONS=true`, using the
   existing `OTC_KEEPER_KEY` in the runner environment. Never retrieve that secret.
5. Every sent transaction hash is durably journaled before submission. Receipts,
   exact constructor data, nonce, gas limits/prices, deployed runtime and all fee
   getters are checked. The next market is not sent until the previous is verified.
   Unknown/reverted transactions or a changed nonce fail closed rather than
   allocating replacements. Resume with the **same plan and deployment journal**.
6. Four-market gas maximum is hard limited to **2 Anubis gas DAI** in total, not
   2 DAI on each retry. The saved plan's lower total is also fixed. No ETH value
   is transferred to any address. Deployment artifacts contain public data only.

All signing workflows must use concurrency group **`otc-refund-keeper`**, including
the existing keeper, to serialize the same signing account. Do not cancel running
jobs. A provider may reject a previously signed transaction after a gas-price
change; inspect it rather than replacing the saved plan automatically.

## New-market keeper

`python keeper.py --plan live-deployment-plan.json` is dry-run. `--execute` is accepted only inside GitHub Actions.
The `flgns-keeper.yml` template is manual-only until its schedule is explicitly enabled.

- Requires the original fixed plan plus a fully verified four-market deployment
  journal. Refuses unrelated contracts and checks original splitter runtime and
  hardcoded SOURCE, keeper, payees and 30/40/30 getters.
- Scans **all four markets**, with no last-N-order cap, and refunds expired open
  orders in batches. Refunds always go to the order's original maker.
- For **FLGNS and RM only**, counts actual new OTC `Filled` events after deployment
  and verifies the immediately preceding exact token transfer from that market to
  SOURCE. The share is `fee - floor(fee * 6000 / 10000)`, matching Solidity.
- Excludes balances already in SOURCE before deployment, unrelated deposits,
  maker refunds, and all pre-existing OTC contracts. DAI/LGNS/AiLGNS remain handled
  by the old bot; this new bot never invokes splits for those tokens.
- Uses the fixed `totalSplit` baseline. Already processed amount equals
  `totalSplit(now) - baseline`; it must never exceed cumulative new OTC income.
  A higher counter, read error, incomplete page, failed event check, reorganization,
  or receipt failure aborts without advancing the scan checkpoint.
- Pending tokens split through the existing immutable splitter only: **40% D53A…7285,
  30% e878…91c3, 30% retained by SOURCE edb9…b1e6**. Required allowance/balance is
  exactly `floor(amount*3000/10000) + floor(amount*4000/10000)`, not rounded 70%.
- SOURCE must approve **FLGNS and RM** to
  `0xeee877c1b6285b82890bdcd027d2715f658ecf23` with its own wallet. The keeper
  cannot grant that approval. A missing approval is reported; other permitted
  refunds can finish, but the checkpoint stays unchanged until the issue resolves.
- No other bot or manual operator may split FLGNS/RM through this splitter while
  this accounting is active. `totalSplit` is global per token. Adding these tokens
  to the old generic income bot would mix the accounting and must not be done.
- State has a confirmed block/hash and cumulative integer amounts; RPC failure is
  never treated as zero. If the cache is lost, a full rescan from deployment plus
  on-chain `totalSplit` prevents double distribution. Preserve the transaction
  journal; an ambiguous pending tx blocks new broadcasts until reconciled.
- Uses 16 confirmations and a **0.1 gas DAI per-run maximum**. Gas is paid by keeper,
  not by the splitter. No approvals, arbitrary withdrawals, old order actions,
  signatures from SOURCE, or transfers to new destinations are issued.

The workflow saves JSON state/journals as artifacts even when later steps fail.
Download journals for long-term retention before their 90-day expiry. A missing
deployment journal with occupied plan addresses deliberately prevents redeployment.
