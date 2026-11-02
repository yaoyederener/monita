"""Combined immutable V1/V2 FLGNS OTC refunds and FLGNS/RM fee ledger.

Default is dry-run. --execute is permitted only in GitHub Actions. Read errors
abort the run; checkpoints only advance after a complete scan and completed
actions. Lost scan state is rebuilt from deployment, not from current balances.
The old keeper remains solely responsible for DAI/LGNS/AiLGNS.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time

from eth_account import Account
from eth_utils import keccak, to_checksum_address
from eth_abi import decode

from legacy_deploy import (ROOT, KEEPER, FLGNS, RECIPIENT60, RECIPIENT40, SPLITTER, MARKETS, rpc,
                    selector, read, require, atomic_json, verify, verify_splitter,
                    validate_plan, plan_digest)
from deploy import verify as verify_v2, validate_plan as validate_v2_plan

MAKER_FEE = "0x" + keccak(text="MakerFeePaid(uint256,address,address,uint256,uint256)").hex()
ESCROW_EVENTS = {"0x" + keccak(text=s).hex() for s in [
    "Created(uint256,address,bool,uint256,uint256,uint64)",
    "Filled(uint256,address,uint256,uint256,uint256,uint256)",
    "MakerFeePaid(uint256,address,address,uint256,uint256)",
    "Closed(uint256,address,uint256,bool)",
]}
FILLED = "0x" + keccak(text="Filled(uint256,address,uint256,uint256,uint256,uint256)").hex()
TRANSFER = "0x" + keccak(text="Transfer(address,address,uint256)").hex()
SPLIT = "0x" + keccak(text="Split(address,uint256,uint256,uint256,uint256,address)").hex()
TRACKED = {FLGNS: "FLGNS", MARKETS["rm"]: "RM"}
CONFIRMATIONS = 16
MAX_RUN_COST = 10 ** 17  # <= 0.1 Anubis gas DAI per invocation.


def pad(value):
    return (value[2:] if isinstance(value, str) else hex(value)[2:]).rjust(64, "0")


def call_bytes(address, data, block):
    value = rpc("eth_call", [{"to": address, "data": data}, block])
    require(value != "0x", "Empty eth_call result")
    return bytes.fromhex(value[2:])


def words(data):
    raw = bytes.fromhex(data[2:])
    require(len(raw) % 32 == 0, "Malformed event data")
    return [int.from_bytes(raw[i:i + 32], "big") for i in range(0, len(raw), 32)]


def block_hash(number):
    return rpc("eth_getBlockByNumber", [hex(number), False])["hash"]


def topic_address(topic):
    require(isinstance(topic, str) and len(topic) == 66 and topic.startswith("0x") and int(topic[2:26], 16) == 0,
            "Malformed indexed address")
    return "0x" + topic[-40:].lower()


def log_identity(log):
    return log["transactionHash"].lower(), int(log["logIndex"], 16)


def normalized_log(log):
    return (log["address"].lower(), tuple(t.lower() for t in log["topics"]), log["data"].lower(),
            log_identity(log), log["blockHash"].lower(), int(log["blockNumber"], 16))


def checked_receipt(log, cache):
    tx_hash = log["transactionHash"].lower()
    if tx_hash not in cache:
        receipt = rpc("eth_getTransactionReceipt", [tx_hash])
        require(receipt and int(receipt["status"], 16) == 1 and
                receipt["blockHash"].lower() == log["blockHash"].lower() and
                receipt["transactionHash"].lower() == tx_hash and
                int(receipt["blockNumber"], 16) == int(log["blockNumber"], 16),
                "Filled receipt missing/reorganized")
        require(isinstance(receipt.get("logs"), list), "Missing receipt logs")
        indices = [int(item["logIndex"], 16) for item in receipt["logs"]]
        require(indices == sorted(set(indices)), "Duplicate or unsorted receipt logIndex")
        for item in receipt["logs"]:
            require(not item.get("removed", False) and item["transactionHash"].lower() == tx_hash and
                    item["blockHash"].lower() == receipt["blockHash"].lower() and
                    int(item["blockNumber"], 16) == int(receipt["blockNumber"], 16),
                    "Receipt log block/transaction mismatch")
        cache[tx_hash] = receipt
    receipt = cache[tx_hash]
    matching = [item for item in receipt["logs"] if int(item["logIndex"], 16) == int(log["logIndex"], 16)]
    require(len(matching) == 1 and normalized_log(matching[0]) == normalized_log(log),
            "Filled log is not the exact receipt log")
    return receipt


def transfer_tuple(log):
    require(len(log["topics"]) == 3 and log["topics"][0].lower() == TRANSFER and len(words(log["data"])) == 1,
            "Malformed Transfer log")
    return log["address"].lower(), topic_address(log["topics"][1]), topic_address(log["topics"][2]), words(log["data"])[0]


def fee_transfers(entry, log, order, version):
    amount, paid, fee, give_left = words(log["data"])
    maker, selling, _, _, give_total, want_total, _ = order
    maker = maker.lower()
    require(give_total > 0 and want_total > 0 and 0 < amount <= give_total and 0 <= give_left <= give_total - amount,
            "Filled/order quantities inconsistent")
    require(paid == (amount * want_total + give_total - 1) // give_total,
            "Filled gross payment rounding mismatch")
    bps = 200 if version == 1 else 40
    require(fee == amount * bps // 10000, "Unexpected fee formula")
    taker = topic_address(log["topics"][2])
    escrow = entry["address"].lower()
    give_token, want_token = (FLGNS, entry["token"]) if selling else (entry["token"], FLGNS)
    expected = []
    def append(token, sender, recipient, value, fee_leg=False):
        expected.append((token.lower(), sender.lower(), recipient.lower(), value, fee_leg))
    maker_fee = paid * 40 // 10000 if version == 2 else 0
    append(want_token, taker, maker, paid - maker_fee)
    if maker_fee:
        share60 = maker_fee * 6000 // 10000
        if share60:
            append(want_token, taker, RECIPIENT60, share60)
        append(want_token, taker, RECIPIENT40, maker_fee - share60, True)
    append(give_token, escrow, taker, amount - fee)
    if fee:
        share60 = fee * 6000 // 10000
        if share60:
            append(give_token, escrow, RECIPIENT60, share60)
        append(give_token, escrow, RECIPIENT40, fee - share60, True)
    return expected, (maker, want_token.lower(), paid, maker_fee)


def credit_fill(entry, log, order, version, receipt, used_transfers, observations):
    expected, maker_values = fee_transfers(entry, log, order, version)
    escrow = entry["address"].lower()
    logs = receipt["logs"]
    position = next(i for i, item in enumerate(logs) if log_identity(item) == log_identity(log))
    # MakerFeePaid is emitted directly after Filled, with no external call in between.
    # Check every maker event for this pinned escrow, including duplicate/orphan events.
    if version == 2:
        for index, item in enumerate(logs):
            if item["address"].lower() == escrow and item["topics"] and item["topics"][0].lower() == MAKER_FEE:
                require(index > 0 and logs[index - 1]["address"].lower() == escrow and
                        logs[index - 1]["topics"][0].lower() == FILLED,
                        "Orphan or duplicate MakerFeePaid event")
        require(position + 1 < len(logs), "Missing MakerFeePaid event")
        maker_event = logs[position + 1]
        maker, token, paid, maker_fee = maker_values
        require(maker_event["address"].lower() == escrow and len(maker_event["topics"]) == 4 and
                maker_event["topics"][0].lower() == MAKER_FEE and
                maker_event["topics"][1].lower() == log["topics"][1].lower() and
                topic_address(maker_event["topics"][2]) == maker and
                topic_address(maker_event["topics"][3]) == token and
                words(maker_event["data"]) == [paid, maker_fee], "MakerFeePaid receipt mismatch")
    # transferFrom can also emit Approval, so compare the exact trailing Transfer
    # sequence rather than requiring adjacent raw log positions. Restrict to this
    # fill after the last escrow event; wrappers may transfer tokens before entry.
    boundary = max((i for i in range(position) if logs[i]["address"].lower() == escrow and
                    logs[i]["topics"] and logs[i]["topics"][0].lower() in ESCROW_EVENTS), default=-1)
    candidates = [item for item in logs[boundary + 1:position] if item["topics"] and
                  item["topics"][0].lower() == TRANSFER and
                  item["address"].lower() in {FLGNS, entry["token"].lower()}]
    require(len(candidates) >= len(expected), "Missing fee receipt transfer sequence")
    selected = candidates[-len(expected):]
    require([transfer_tuple(item) for item in selected] == [item[:4] for item in expected],
            "Exact fill fee receipt transfer sequence mismatch")
    credit = {token: 0 for token in TRACKED}
    for transfer, want in zip(selected, expected):
        identifier = log_identity(transfer)
        require(identifier not in used_transfers, "Transfer log reused across fills")
        used_transfers.add(identifier)
        token, sender, recipient, value, is_fee = want
        if is_fee and token in TRACKED:
            require(recipient == RECIPIENT40, "Fee receipt destination mismatch")
            if sender == RECIPIENT40:
                # SOURCE-to-SOURCE fee transfers do not add spendable revenue.
                observations["selfTransferFeeSkippedRaw"][token] += value
                observations["selfTransferFeeLegs"].append({"token": token, "amount": str(value),
                    "transactionHash": transfer["transactionHash"], "logIndex": transfer["logIndex"]})
            else:
                credit[token] += value
    return credit


def scan_fees(plan, deployment, state, end, bilateral_plan=None, bilateral_deployment=None, observations=None):
    income = {token: int(state["income"][token]) for token in TRACKED}
    require(all(amount >= 0 for amount in income.values()), "Negative keeper income")
    if observations is None:
        observations = {}
    observations.setdefault("selfTransferFeeSkippedRaw", {token: int(state.get("selfTransferFeeSkippedRaw", {}).get(token, 0)) for token in TRACKED})
    observations.setdefault("selfTransferFeeLegs", [])
    groups = [(plan, deployment, 1, state["scannedTo"] + 1)]
    if bilateral_plan is not None:
        require(bilateral_deployment is not None, "Missing bilateral deployment")
        digest = plan_digest(bilateral_plan)
        require(state.get("bilateralPlanSha256") in (None, digest), "Wrong bilateral keeper state")
        # A V1 checkpoint may already be beyond V2 deployment. Always backfill all
        # V2 history on the first successful migration, never just the last N blocks.
        v2_start = state["scannedTo"] + 1 if state.get("bilateralPlanSha256") else min(
            item["block"] for item in bilateral_deployment["markets"].values())
        groups.append((bilateral_plan, bilateral_deployment, 2, v2_start))
    seen_events, used_transfers, receipts = set(), set(), {}
    for group_plan, group_deployment, version, start in groups:
        markets = {entry["address"].lower(): entry for entry in group_plan["markets"]}
        require(len(markets) == len(group_plan["markets"]), "Duplicate market address")
        for first in range(start, end + 1, 1000):
            last = min(end, first + 999)
            logs = rpc("eth_getLogs", [{"address": list(markets), "fromBlock": hex(first), "toBlock": hex(last), "topics": [FILLED]}])
            require(isinstance(logs, list), "Missing logs response")
            for log in sorted(logs, key=lambda item: (int(item["blockNumber"], 16), int(item["logIndex"], 16))):
                require(not log.get("removed", False), "Removed log; retry after finality")
                identifier = log_identity(log)
                require(identifier not in seen_events, "Duplicate RPC Filled log")
                seen_events.add(identifier)
                address = log["address"].lower()
                require(address in markets and len(log["topics"]) == 3 and log["topics"][0].lower() == FILLED, "Unexpected Filled log")
                entry = markets[address]
                number = int(log["blockNumber"], 16)
                require(first <= number <= last, "Log outside requested scan range")
                require(number >= group_deployment["markets"][entry["name"]]["block"], "Predeployment Filled log")
                require(len(words(log["data"])) == 4, "Malformed Filled event")
                order_id = int(log["topics"][1], 16)
                order = decode(["(address,bool,bool,uint64,uint256,uint256,uint256)"], call_bytes(address, selector("getOrder(uint256)") + pad(order_id), log["blockNumber"]))[0]
                receipt = checked_receipt(log, receipts)
                credit = credit_fill(entry, log, order, version, receipt, used_transfers, observations)
                for token, value in credit.items():
                    income[token] += value
    return income


def expired_orders(plan, end):
    block = rpc("eth_getBlockByNumber", [hex(end), False])
    timestamp = int(block["timestamp"], 16)
    actions = []
    for entry in plan["markets"]:
        address = entry["address"]
        count = read(address, "ordersCount()", hex(end))
        expired = []
        for first in range(0, count, 200):
            orders = decode(["(address,bool,bool,uint64,uint256,uint256,uint256)[]"], call_bytes(address, selector("getOrders(uint256,uint256)") + pad(first) + pad(200), hex(end)))[0]
            require(len(orders) == min(200, count - first), "Incomplete order page")
            expired.extend(first + offset for offset, order in enumerate(orders) if order[2] and order[3] <= timestamp)
        for first in range(0, len(expired), 20):
            ids = expired[first:first + 20]
            data = selector("refundExpired(uint256[])") + pad(32) + pad(len(ids)) + "".join(pad(i) for i in ids)
            actions.append({"kind": "refund", "market": entry["name"], "to": address, "data": data, "ids": ids})
    return actions


def split_actions(plan, income, end):
    result = []
    blocked = []
    for token, symbol in TRACKED.items():
        baseline = int(plan["splitBaseline"][token])
        total = read(SPLITTER, "totalSplit(address)", hex(end), pad(token))
        done = total - baseline
        require(done >= 0, "totalSplit below fixed baseline")
        require(done <= income[token], "totalSplit exceeds new OTC fee income: another bot/manual split or lost/incorrect scan; stop")
        pending = income[token] - done
        # Two independently floored transfers; retained source share takes dust.
        outgoing = pending * 3000 // 10000 + pending * 4000 // 10000
        allowance = read(token, "allowance(address,address)", hex(end), pad(RECIPIENT40) + pad(SPLITTER))
        balance = read(token, "balanceOf(address)", hex(end), pad(RECIPIENT40))
        print(json.dumps({"token": symbol, "newOtcIncomeRaw": str(income[token]), "alreadySplitRaw": str(done), "pendingRaw": str(pending), "outgoingRaw": str(outgoing), "allowanceRaw": str(allowance)}))
        if pending == 0:
            continue
        if allowance < outgoing:
            blocked.append(f"SOURCE must approve existing splitter for {symbol}; no automatic approval")
            continue
        if balance < outgoing:
            blocked.append(f"SOURCE {symbol} balance below required outgoing amount")
            continue
        result.append({"kind": "split", "token": token, "amount": str(pending), "previousTotalSplit": str(total), "to": SPLITTER,
                       "data": selector("split(address,uint256)") + pad(token) + pad(pending)})
    return result, blocked


def receipt_check(journal, action):
    receipt = rpc("eth_getTransactionReceipt", [journal["tx"]])
    if receipt is None:
        return None
    require(int(receipt["status"], 16) == 1, "Keeper transaction reverted; inspect before retry")
    tx = rpc("eth_getTransactionByHash", [journal["tx"]])
    require(tx and tx["from"].lower() == KEEPER and tx["to"].lower() == journal["to"] and tx["input"].lower() == journal["data"].lower() and int(tx["value"], 16) == 0, "Keeper transaction receipt mismatch")
    if action["kind"] == "split":
        matching = [l for l in receipt["logs"] if l["address"].lower() == SPLITTER and l["topics"] == [SPLIT, "0x" + pad(action["token"]), "0x" + pad(KEEPER)]]
        require(len(matching) == 1 and words(matching[0]["data"])[0] == int(action["amount"]), "Split event mismatch")
    journal["confirmed"] = True
    journal["receiptBlock"] = int(receipt["blockNumber"], 16)
    return receipt


def execute(actions, plan, journal_path, bilateral_plan=None):
    require(os.environ.get("GITHUB_ACTIONS") == "true", "Execution only allowed in GitHub Actions")
    account = Account.from_key(os.environ["OTC_KEEPER_KEY"])
    require(account.address.lower() == KEEPER, "Wrong keeper signer")
    journal = json.loads(journal_path.read_text()) if journal_path.exists() else {"planSha256": plan_digest(plan), "transactions": []}
    require(journal["planSha256"] == plan_digest(plan), "Keeper journal plan mismatch")
    bilateral_digest = plan_digest(bilateral_plan) if bilateral_plan is not None else None
    require(journal.get("bilateralPlanSha256") in (None, bilateral_digest), "Keeper bilateral journal plan mismatch")
    # A previous ambiguous submission must settle before any new transaction.
    for previous in journal["transactions"]:
        if not previous.get("confirmed"):
            receipt_check(previous, previous["action"])
            require(previous.get("confirmed"), "Previous keeper transaction is pending/unknown; do not send replacement")
            atomic_json(journal_path, journal)
    if bilateral_digest is not None and journal.get("bilateralPlanSha256") is None:
        journal["bilateralPlanSha256"] = bilateral_digest
        atomic_json(journal_path, journal)
    spent = 0
    for action in actions:
        require(int(rpc("eth_chainId", []), 16) == 6714, "Wrong network")
        verify_splitter()
        nonce = int(rpc("eth_getTransactionCount", [KEEPER, "latest"]), 16)
        require(nonce == int(rpc("eth_getTransactionCount", [KEEPER, "pending"]), 16), "Keeper pending transaction; shared concurrency lock required")
        if action["kind"] == "split":
            current = read(SPLITTER, "totalSplit(address)", "latest", pad(action["token"]))
            require(current == int(action["previousTotalSplit"]), "FLGNS/RM split counter changed since scan; rescan next run")
            amount = int(action["amount"])
            outgoing = amount * 3000 // 10000 + amount * 4000 // 10000
            require(read(action["token"], "allowance(address,address)", "latest", pad(RECIPIENT40) + pad(SPLITTER)) >= outgoing, "SOURCE allowance changed")
            require(read(action["token"], "balanceOf(address)", "latest", pad(RECIPIENT40)) >= outgoing, "SOURCE balance changed")
        price = int(rpc("eth_gasPrice", []), 16)
        estimate = int(rpc("eth_estimateGas", [{"from": KEEPER, "to": action["to"], "data": action["data"], "value": "0x0"}]), 16)
        gas = (estimate * 130 + 99) // 100
        spent += gas * price
        require(spent <= MAX_RUN_COST, "Keeper per-run 0.1 DAI gas cap exceeded")
        require(int(rpc("eth_getBalance", [KEEPER, "latest"]), 16) >= gas * price, "Insufficient keeper gas")
        tx = {"chainId": 6714, "nonce": nonce, "to": to_checksum_address(action["to"]), "data": action["data"], "gas": gas, "gasPrice": price, "value": 0}
        signed = account.sign_transaction(tx)
        tx_hash = "0x" + keccak(signed.raw_transaction).hex()
        record = {"tx": tx_hash, "nonce": nonce, "to": action["to"], "data": action["data"], "confirmed": False, "action": action}
        journal["transactions"].append(record)
        atomic_json(journal_path, journal)
        returned = rpc("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()])
        require(returned.lower() == tx_hash.lower(), "Wrong returned tx hash")
        for _ in range(90):
            if receipt_check(record, action):
                atomic_json(journal_path, journal)
                break
            time.sleep(2)
        require(record.get("confirmed"), f"Pending keeper transaction {tx_hash}; keep journal")
        print(json.dumps({"kind": action["kind"], "tx": tx_hash, "confirmed": True}))


def verify_deployment(plan, deployment, artifact, end, verifier):
    require(deployment["planSha256"] == plan_digest(plan), "Deployment and plan differ")
    require(set(deployment["markets"]) == set(MARKETS), "Four verified deployments required")
    for entry in plan["markets"]:
        deployed = deployment["markets"][entry["name"]]
        require(deployed["verified"] and deployed["address"] == entry["address"] and deployed["block"] <= end,
                "Deployment not confirmed")
        require(block_hash(deployed["block"]) == deployed["blockHash"], "Deployment reorganized")
        verifier(entry["address"], entry["token"], artifact, hex(end))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--plan", default=str(ROOT / "v1-deployment-plan.json"))
    parser.add_argument("--deployment", default=str(ROOT / "v1-deployment.json"))
    parser.add_argument("--state", default=str(ROOT / "keeper-state.json"))
    parser.add_argument("--journal", default=str(ROOT / "keeper-transactions.json"))
    parser.add_argument("--artifact", default=str(ROOT.parent / "contracts/build/FlgnsOtcEscrow.artifact.json"))
    parser.add_argument("--bilateral-plan", default=str(ROOT / "bilateral-deployment-plan.json"))
    parser.add_argument("--bilateral-deployment", default=str(ROOT / "bilateral-deployment.json"))
    parser.add_argument("--bilateral-artifact", default=str(ROOT.parent / "contracts/build/FlgnsOtcEscrowV2.artifact.json"))
    args = parser.parse_args()
    plan = json.loads(Path(args.plan).read_text())
    deployment = json.loads(Path(args.deployment).read_text())
    artifact = json.loads(Path(args.artifact).read_text())
    bilateral_plan = json.loads(Path(args.bilateral_plan).read_text())
    bilateral_deployment = json.loads(Path(args.bilateral_deployment).read_text())
    bilateral_artifact = json.loads(Path(args.bilateral_artifact).read_text())
    require(int(rpc("eth_chainId", []), 16) == 6714, "Wrong network")
    validate_plan(plan, Path(args.artifact))
    validate_v2_plan(bilateral_plan, Path(args.bilateral_artifact))
    require(not {entry["address"].lower() for entry in plan["markets"]}.intersection(
        entry["address"].lower() for entry in bilateral_plan["markets"]), "V1 and V2 market addresses overlap")
    end = int(rpc("eth_blockNumber", []), 16) - CONFIRMATIONS
    scan_hash = block_hash(end)
    verify_splitter(hex(end))
    verify_deployment(plan, deployment, artifact, end, verify)
    verify_deployment(bilateral_plan, bilateral_deployment, bilateral_artifact, end, verify_v2)
    state_path = Path(args.state)
    state = json.loads(state_path.read_text()) if state_path.exists() else {
        "planSha256": plan_digest(plan),
        "scannedTo": min(item["block"] for item in deployment["markets"].values()) - 1,
        "income": {token: "0" for token in TRACKED}}
    require(state["planSha256"] == plan_digest(plan) and set(state["income"]) == set(TRACKED), "Wrong keeper state")
    require(state.get("bilateralPlanSha256") in (None, plan_digest(bilateral_plan)), "Wrong bilateral keeper state")
    require(state["scannedTo"] <= end, "Checkpoint is ahead of finalized head")
    if state.get("blockHash"):
        require(block_hash(state["scannedTo"]) == state["blockHash"], "Checkpoint reorganized; preserve evidence and rebuild from deployment")
    observations = {}
    income = scan_fees(plan, deployment, state, end, bilateral_plan, bilateral_deployment, observations)
    splits, blocked = split_actions(plan, income, end)
    old_refunds = expired_orders(plan, end)
    new_refunds = expired_orders(bilateral_plan, end)
    for action in old_refunds:
        action["version"] = 1
    for action in new_refunds:
        action["version"] = 2
    actions = old_refunds + new_refunds + splits
    require(block_hash(end) == scan_hash, "Scan head reorganized")
    print(json.dumps({"mode": "execute" if args.execute else "dry-run", "throughBlock": end,
        "firstBilateralBackfill": not bool(state.get("bilateralPlanSha256")),
        "income": {token: str(value) for token, value in income.items()},
        "selfTransferFeeSkippedRaw": {token: str(value) for token, value in observations["selfTransferFeeSkippedRaw"].items()},
        "selfTransferFeeLegs": observations["selfTransferFeeLegs"], "actions": actions, "blocked": blocked}, indent=2), flush=True)
    if not args.execute:
        return
    execute(actions, plan, Path(args.journal), bilateral_plan)
    require(not blocked, "; ".join(blocked))
    require(block_hash(end) == scan_hash, "Scan head reorganized during execution; checkpoint not advanced")
    atomic_json(state_path, {"schemaVersion": 2, "planSha256": plan_digest(plan),
        "bilateralPlanSha256": plan_digest(bilateral_plan), "scannedTo": end, "blockHash": scan_hash,
        "income": {token: str(amount) for token, amount in income.items()},
        "selfTransferFeeSkippedRaw": {token: str(value) for token, value in observations["selfTransferFeeSkippedRaw"].items()},
        "observedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})


if __name__ == "__main__":
    main()
