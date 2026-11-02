# FLGNS OTC V2 — bilateral 0.4% settlement

This independent version implements the requested 0.4% charge on **each side's received asset**. V1 source and build files remain untouched. V1 contracts have no administrator or fee setter, so their orders and allowances remain associated with their original contracts. This directory contains local source and reproducible compilation evidence, not a deployment claim.

## Exact amounts

`amount` and `paid` remain gross raw token units. For each fill:

```
paid          = ceil(amount * wantTotal / giveTotal)
takerFee      = floor(amount * 40 / 10000)
makerFee      = floor(paid   * 40 / 10000)
taker receives amount - takerFee       in giveToken
maker receives paid   - makerFee      in wantToken
taker spends   paid                   in wantToken (no fee added on top)
```

Both fees are split independently:

```
fee60 = floor(fee * 6000 / 10000)
fee40 = fee - fee60
```

60% recipient: `0xd07Fe622d0D1628780853b31C21B9ABAf96B9c00`.
40% recipient (SOURCE): `0xedb9c6900a11d2dd37da6b59f3604c35a39db1e6`.
Indivisible split remainders go to SOURCE; no fee remains in escrow. Each fee is rounded separately on every partial fill. Splitting fills may lower aggregate fees and may raise gross payment through the retained per-fill ceiling rule. Raw amounts below 250 produce zero fee. No zero-amount fee transfer is called.

A 1,000 FLGNS for 2,000 DAI trade pays 996 FLGNS to the taker and 1,992 DAI to the maker. The escrow sends 2.4 FLGNS/1.6 FLGNS to 60%/SOURCE. The taker sends 4.8 DAI/3.2 DAI to 60%/SOURCE as part of the same 2,000 DAI payment, not an additional payment.

FLGNS and TOKEN_B allowlist, constructor, order tuple, callable method signatures, duration bounds, pagination, cancellation and expiry behavior remain the same as V1. `FEE_BPS()` is 40. LGNS/RM use 9-decimal test amounts and FLGNS/DAI/AiLGNS use 18-decimal test amounts; contract arithmetic always uses raw units.

## Interface and receipt ordering

`create(bool,uint256,uint256,uint64)`, `fill(uint256,uint256,uint256)`, `cancel(uint256)`, `refundExpired(uint256[])`, `getOrder(uint256)`, `getOrders(uint256,uint256)`, `ordersCount()` and all original constants have unchanged signatures. Order tuple stays:

```
(address maker, bool sellFlgns, bool open, uint64 expiry,
 uint256 giveTotal, uint256 wantTotal, uint256 giveLeft)
```

Per-fill token calls occur in this order:

1. wantToken `transferFrom(taker, maker, paid - makerFee)`.
2. wantToken `transferFrom(taker, recipient60, makerFee60)` if nonzero.
3. wantToken `transferFrom(taker, SOURCE, makerFee - makerFee60)` if makerFee is nonzero.
4. giveToken `transfer(taker, amount - takerFee)` from escrow.
5. giveToken `transfer(recipient60, takerFee60)` if nonzero.
6. giveToken `transfer(SOURCE, takerFee - takerFee60)` if takerFee is nonzero.
7. Original `Filled(uint256 indexed id, address indexed taker, uint256 amount, uint256 paid, uint256 fee, uint256 giveLeft)`; `fee` remains **takerFee** for old indexers.
8. New `MakerFeePaid(uint256 indexed id, address indexed maker, address indexed token, uint256 grossPayment, uint256 fee)`; token is wantToken, grossPayment is paid, fee is makerFee, emitted even for zero makerFee.
9. Original `Closed` only if the order is fully filled.

Both fee assets must be tracked. `Filled` alone describes only the giveToken fee. SOURCE's external distribution system must distinguish trading proceeds from the individual fee legs. If taker equals SOURCE, the wantToken SOURCE fee leg is a self-transfer and provides **zero new SOURCE balance**; it must not be counted as new distributable revenue. Matching a recipient alone cannot identify a fee when maker/taker is also a fee recipient. Token Transfer log behavior belongs to the external token, so a distribution verifier should fail closed when expected evidence is missing or ambiguous.

## Safety properties and limits

Every incoming transfer uses `from = msg.sender`; no victim allowance can be used by another caller. Every transferFrom checks both exact payer debit and exact recipient receipt. For a self-transfer, the transfer is still called and must leave that account's balance unchanged. This supports maker=taker and maker/taker=fee recipient while rejecting unexpected self-transfer deductions. Each outgoing transfer checks both exact escrow debit and exact recipient receipt. All six legs and order updates are one atomic, reentrancy-locked transaction. Failures roll back earlier payments, allowances and order changes; a failing leg cannot consume another order's backing.

There is no administrator, upgrade, fee setter, token approval, sweep, arbitrary call, or general withdrawal. Cancellation/expiry returns the entire unfilled escrow to the maker without charging either fee. External token pauses, blacklists, upgrades or transfer behavior changes can still make operations fail; exact-balance checks reject abnormal behavior but cannot force a token to transfer. The retained checked multiplication/ceiling math can revert for impractically large inputs; no silent overflow is allowed. Local mock tests do not establish live token behavior or future implementation behavior.

## Reproduce and evidence

- Compile: `node work/flgns-otc/contracts/v2/compile.cjs`.
- Test: `sh work/flgns-otc/tests/v2/run-tests.sh`.
- Compiler: local `0.8.24+commit.e11b9ed9`, optimizer 200, EVM Paris, no viaIR, default IPFS metadata.
- Build: `build/standard-input.json`, `build/standard-output.json`, `build/FlgnsOtcEscrowV2.abi.json`, `build/FlgnsOtcEscrowV2.artifact.json`.
- Creation bytecode 6,861 bytes; runtime template 6,448 bytes. TOKEN_B immutable slots are recorded in the artifact.
- Actual test log: `../../tests/v2/logs/forge-test.log`.
- Result: **32 tests passed, 0 failed**, including two fuzz tests with 256 cases each.

Coverage includes four markets in both directions, actual 9/18-decimal magnitudes, bilateral fee conservation, per-fill rounding, one-unit remainder, full and partial fills, 32 maker/taker/fee-recipient overlap combinations across both directions, price limits, caller-only allowances, no-charge cancellation/expiry, batch refund rollback, false-return/short receipt/extra payer debit on each payment leg, short receipt/extra escrow debit on each outgoing leg, backing of other orders in both assets, abnormal deposit debit, zero fee, self-transfer anomalies, and callbacks from incoming and outgoing legs attempting reentry.
