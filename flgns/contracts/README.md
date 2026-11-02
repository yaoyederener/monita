# FLGNS OTC contract candidate

This is an isolated derivative of `work/lending-audit-20261101/source/ailgns-data/contracts/AilgnsOtcEscrow.sol`. It does not alter the existing source, website, deployments, repository, or live funds. No deployment, transaction, signature, private key, or secret was used by this contract build/test task.

One `FlgnsOtcEscrow(address tokenB)` deployment serves each of FLGNS/DAI, FLGNS/LGNS, FLGNS/AiLGNS, and FLGNS/RM. The constructor accepts exactly those four B addresses; FLGNS and the two fee recipients are fixed constants. `deployments.example.json` is a constructor manifest with **null deployment addresses**, not evidence of deployment.

## Settlement and fee behavior

The original escrow, maker-directed payment, partial fills, order expiry, maker cancellation, third-party expiry refunds, incoming transfer helper, and reentrancy lock are retained. The outgoing helper is strengthened to verify both exact escrow debit and exact recipient receipt. There is no administrator, upgrade, arbitrary transfer, token approval, or general call entry point. The only incoming transfers use `from = msg.sender`; a spender cannot use another wallet's existing token allowance through this contract.

`amount` is the gross quantity of the maker's escrow asset, in raw token units:

```
makerPayment = ceil(amount * wantTotal / giveTotal)
fee          = floor(amount * 200 / 10000)
takerReceipt = amount - fee
fee60        = floor(fee * 6000 / 10000)
fee40        = fee - fee60
```

The 60% amount goes directly to `0xd07Fe622d0D1628780853b31C21B9ABAf96B9c00`. The remaining fee goes directly to SOURCE `0xedb9c6900a11d2dd37da6b59f3604c35a39db1e6`. Every indivisible split remainder goes to SOURCE; no fee balance is retained by escrow. A one-unit total fee therefore gives zero units to the 60% recipient and one unit to SOURCE. If total fee is zero, neither fee transfer is attempted.

The fee asset is the asset received by the taker: FLGNS when `sellFlgns == true`, and the selected B asset when `false`. The maker receives the full calculated payment in the other asset. Fee rounding and upward payment rounding happen separately for every partial fill, as in the baseline; splitting fills can change aggregate rounding. A 10,003-unit order filled as 49 + 50 + 151 + 9,753 charges 199 total fee units rather than the 200 units of one complete fill.

All payment, taker payout, 60% payout, 40% payout, and order updates share one transaction. Any reverting or false-returning transfer rolls back all preceding transfers and order changes. Every outgoing leg also checks `escrowBefore - escrowAfter == amount` and `recipientAfter - recipientBefore == amount`, with guarded subtraction, and rejects silent short receipts or additional escrow debits with `TRANSFER_MISMATCH`. This prevents an abnormal payout from consuming another order's escrow backing. Cancellation and expiry refunds use the same strengthened helper, charge no fee, and send the remaining escrow only to the maker. An expiry-refund batch is atomic: one failing refund also reverts earlier refunds in that batch; callers can retry separate IDs.

SOURCE's second-level 40%/30%/30% distribution remains the responsibility of the existing distribution system. The OTC has no SOURCE allowance, pull, sweep, or distribution function.

## Integration interface

| Signature | Selector |
|---|---|
| `create(bool,uint256,uint256,uint64)` | `0x9128a740` |
| `fill(uint256,uint256,uint256)` | `0x6fe4a448` |
| `cancel(uint256)` | `0x40e58ee5` |
| `refundExpired(uint256[])` | `0x34bfca5c` |
| `ordersCount()` | `0x35daa731` |
| `getOrder(uint256)` | `0xd09ef241` |
| `getOrders(uint256,uint256)` | `0x8f72fc77` |
| `FLGNS()` | `0x93080cd0` |
| `TOKEN_B()` | `0x499f712e` |
| `FEE_BPS()` | `0xbf333f2c` |
| `FEE_RECIPIENT_60()` | `0x50abfee1` |
| `FEE_RECIPIENT_40()` | `0x48b4ebda` |
| `FEE_SHARE_60_BPS()` | `0x3363c2a7` |

Order tuples are `(address maker, bool sellFlgns, bool open, uint64 expiry, uint256 giveTotal, uint256 wantTotal, uint256 giveLeft)`. `getOrder(id)` returns one tuple; `getOrders(from,count)` returns a dynamic array of tuples. Duration is **uint64**, 600–2,592,000 seconds. The false direction deposits B and receives FLGNS. The approval spender is the selected market's escrow address. Approvals must be sent by the connected maker/taker for the exact amount required, then `create` or `fill` is sent from that same wallet. There is no `approve` function on the escrow.

Use `build/FlgnsOtcEscrow.abi.json` as the authoritative ABI. All calculations use raw units; token decimals must be read separately. In particular LGNS and RM use 9 decimals and FLGNS/AiLGNS/DAI use 18 in the independently observed Anubis block 18,052,527; the escrow does not assume any decimal count.

## Reproduce locally

Run `./compile.cjs` from this directory (or run it using Node). It uses the existing local `../../ailgns-audit/soljson.cjs` version `0.8.24+commit.e11b9ed9`, optimizer 200, EVM Paris, no `viaIR`, default IPFS metadata. It emits the standard JSON input/output, ABI, artifact, creation bytecode, runtime template, immutable references, method identifiers, and storage layout in `build/`.

Run `../tests/run-tests.sh` to copy this exact candidate into the isolated test harness and execute the existing local Foundry binary against the same local soljson. The harness includes a local copy of the previously cached forge-std library and does not download dependencies. Test logs are saved in `../tests/logs/forge-test.log`. Tests use local mock ERC-20 code at the configured addresses, not live funds or a chain fork.

22 tests pass, including two fuzz tests with 256 cases each. Coverage includes all four markets in both directions, 2% fee asset and amounts, 60/40 conservation, one-unit fee remainder, partial-fill rounding, full/partial cancellation, expiry boundary/duplicates, failed payment and all three output legs, atomic failed refund batches, taxed incoming transfer rejection, callback reentrancy, caller-only allowances, price limits, constructor allowlist, and pagination. Additional tests force true-returning output transfers to deliver one unit less or debit escrow one extra unit on each taker/60%/40% output leg. They verify full rollback of maker payment, earlier payouts, allowance, both orders, cancellation, and batch refunds, while preserving another order's backing.

## Checks before any deployment

This is a local contract candidate. Token semantics on Anubis must be checked before deployment. `_pullExact` checks incoming receipt; `_pushExact` checks both outgoing debit and receipt, in addition to successful ERC-20 return data. Taxed or abnormal transfers are deliberately rejected rather than silently underpaying users or consuming other orders. This protects accounting but cannot force an external token to permit transfers: a future blacklist, pause, rebase, or upgrade can make fills and refunds revert and leave deposits unavailable until the token permits exact transfers again. The mocks prove atomic rollback for the modeled failures, not all behavior of the live token contracts or their future upgrades. In particular the RM implementation is unverified at the checked block; ordinary EOA-transfer simulations do not prove all contract-originated paths or future behavior.

The original checked multiplication/ceiling formula, per-fill rounding, range pagination arithmetic, and uint64 timestamp behavior are retained. No unsolicited math, governance, rescue, or downstream distributor changes are included. The runtime artifact has a `TOKEN_B` immutable placeholder; actual deployed runtime differs at the recorded immutable locations. Compare using those references and the selected constructor address.
