"""Isolated FLGNS OTC expiry refunds + FLGNS/RM second-stage fee split.

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

from deploy import (ROOT, KEEPER, FLGNS, RECIPIENT40, SPLITTER, MARKETS, rpc,
                    selector, read, require, atomic_json, verify, verify_splitter,
                    validate_plan, plan_digest)

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


def scan_fees(plan, deployment, state, end):
    income = {token: int(state["income"][token]) for token in TRACKED}
    start = state["scannedTo"] + 1
    markets = {entry["address"].lower(): entry for entry in plan["markets"]}
    if start > end:
        return income
    for first in range(start, end + 1, 1000):
        last = min(end, first + 999)
        logs = rpc("eth_getLogs", [{"address": list(markets), "fromBlock": hex(first), "toBlock": hex(last), "topics": [FILLED]}])
        require(isinstance(logs, list), "Missing logs response")
        seen = set()
        receipts = {}
        for log in logs:
            require(not log.get("removed", False), "Removed log; retry after finality")
            identifier = (log["transactionHash"], log["logIndex"])
            require(identifier not in seen, "Duplicate RPC log")
            seen.add(identifier)
            address = log["address"].lower()
            require(address in markets and len(log["topics"]) == 3 and log["topics"][0] == FILLED, "Unexpected Filled log")
            entry = markets[address]
            number = int(log["blockNumber"], 16)
            require(first <= number <= last, "Log outside requested scan range")
            require(number >= deployment["markets"][entry["name"]]["block"], "Predeployment Filled log")
            event = words(log["data"])
            require(len(event) == 4, "Malformed Filled event")
            amount, _, fee, _ = event
            require(fee == amount * 200 // 10000, "Unexpected fee formula")
            order_id = int(log["topics"][1], 16)
            order = decode(["(address,bool,bool,uint64,uint256,uint256,uint256)"], call_bytes(address, selector("getOrder(uint256)") + pad(order_id), log["blockNumber"]))[0]
            token = FLGNS if order[1] else entry["token"]
            if token not in TRACKED:
                continue
            share = fee - fee * 6000 // 10000
            if share == 0:
                continue
            # Confirm an actual matching transfer from this market, not a refund,
            # a transfer from an unrelated sender, or a pre-existing balance.
            tx_hash = log["transactionHash"]
            receipt = receipts.get(tx_hash)
            if receipt is None:
                receipt = rpc("eth_getTransactionReceipt", [tx_hash])
                require(receipt and int(receipt["status"], 16) == 1 and receipt["blockHash"] == log["blockHash"] and receipt["transactionHash"] == tx_hash and receipt["blockNumber"] == log["blockNumber"], "Filled receipt missing/reorganized")
                receipts[tx_hash] = receipt
            preceding = [l for l in receipt["logs"] if int(l["logIndex"], 16) < int(log["logIndex"], 16)]
            require(preceding, "Filled event without preceding fee transfer")
            transfer = preceding[-1]
            require(transfer["address"].lower() == token and transfer["topics"] == [TRANSFER, "0x" + pad(address), "0x" + pad(RECIPIENT40)] and int(transfer["data"], 16) == share,
                    "40% fee receipt mismatch; refusing to count it")
            income[token] += share
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


def execute(actions, plan, journal_path):
    require(os.environ.get("GITHUB_ACTIONS") == "true", "Execution only allowed in GitHub Actions")
    account = Account.from_key(os.environ["OTC_KEEPER_KEY"])
    require(account.address.lower() == KEEPER, "Wrong keeper signer")
    journal = json.loads(journal_path.read_text()) if journal_path.exists() else {"planSha256": plan_digest(plan), "transactions": []}
    require(journal["planSha256"] == plan_digest(plan), "Keeper journal plan mismatch")
    # A previous ambiguous submission must settle before any new transaction.
    for previous in journal["transactions"]:
        if not previous.get("confirmed"):
            receipt_check(previous, previous["action"])
            require(previous.get("confirmed"), "Previous keeper transaction is pending/unknown; do not send replacement")
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--plan", default=str(ROOT / "deployment-plan.json"))
    parser.add_argument("--deployment", default=str(ROOT / "deployment.json"))
    parser.add_argument("--state", default=str(ROOT / "keeper-state.json"))
    parser.add_argument("--journal", default=str(ROOT / "keeper-transactions.json"))
    parser.add_argument("--artifact", default=str(ROOT.parent / "contracts/build/FlgnsOtcEscrow.artifact.json"))
    args = parser.parse_args()
    plan = json.loads(Path(args.plan).read_text())
    deployment = json.loads(Path(args.deployment).read_text())
    artifact = json.loads(Path(args.artifact).read_text())
    require(int(rpc("eth_chainId", []), 16) == 6714, "Wrong network")
    validate_plan(plan, Path(args.artifact))
    require(deployment["planSha256"] == plan_digest(plan), "Deployment and plan differ")
    require(set(deployment["markets"]) == set(MARKETS), "Four verified deployments required")
    end = int(rpc("eth_blockNumber", []), 16) - CONFIRMATIONS
    scan_hash = block_hash(end)
    verify_splitter(hex(end))
    for entry in plan["markets"]:
        deployed = deployment["markets"][entry["name"]]
        require(deployed["verified"] and deployed["address"] == entry["address"] and deployed["block"] <= end, "Deployment not confirmed")
        require(block_hash(deployed["block"]) == deployed["blockHash"], "Deployment reorganized")
        verify(entry["address"], entry["token"], artifact, hex(end))
    state_path = Path(args.state)
    state = json.loads(state_path.read_text()) if state_path.exists() else {"planSha256": plan_digest(plan), "scannedTo": min(d["block"] for d in deployment["markets"].values()) - 1, "income": {t: "0" for t in TRACKED}}
    require(state["planSha256"] == plan_digest(plan) and set(state["income"]) == set(TRACKED), "Wrong keeper state")
    require(state["scannedTo"] <= end, "Checkpoint is ahead of finalized head")
    if state.get("blockHash"):
        require(block_hash(state["scannedTo"]) == state["blockHash"], "Checkpoint reorganized; preserve evidence and rebuild from deployment")
    income = scan_fees(plan, deployment, state, end)
    splits, blocked = split_actions(plan, income, end)
    actions = expired_orders(plan, end) + splits
    require(block_hash(end) == scan_hash, "Scan head reorganized")
    print(json.dumps({"mode": "execute" if args.execute else "dry-run", "throughBlock": end, "actions": actions, "blocked": blocked}, indent=2), flush=True)
    if not args.execute:
        return
    execute(actions, plan, Path(args.journal))
    require(not blocked, "; ".join(blocked))
    require(block_hash(end) == scan_hash, "Scan head reorganized during execution; checkpoint not advanced")
    atomic_json(state_path, {"planSha256": plan_digest(plan), "scannedTo": end, "blockHash": scan_hash, "income": {token: str(amount) for token, amount in income.items()}, "observedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})


if __name__ == "__main__":
    main()
