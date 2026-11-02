"""Four bilateral 0.4% immutable markets. Default verifies a fixed public plan.

--broadcast requires GITHUB_ACTIONS=true and an already saved plan; no private key
or signed transaction is printed/persisted. Run under concurrency otc-refund-keeper.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.request

from eth_account import Account
from eth_utils import keccak
import rlp

ROOT = Path(__file__).resolve().parent
RPCS = ["https://rpc2.anubispace.org", "https://rpc3.anubispace.org", "https://rpc.anubispace.org"]
KEEPER = "0x89dd06754bb239b6c4c92bc47c8fa72889084e27"
FLGNS = "0xffffbe730e2cde54d8928d36f547e41deb66aaaa"
RECIPIENT60 = "0xd07fe622d0d1628780853b31c21b9abaf96b9c00"
RECIPIENT40 = "0xedb9c6900a11d2dd37da6b59f3604c35a39db1e6"
SPLITTER = "0xeee877c1b6285b82890bdcd027d2715f658ecf23"
MARKETS = {
    "dai": "0x83fd06f0846d9d90b3016bf670efe2e0b11cde14",
    "lgns": "0x4d1d808a081fdac440703b3765fc61f8028c06b8",
    "ailgns": "0x256edb652db43e721ec6a12b6b51b6967d3bb8cb",
    "rm": "0x2e045e1380f5f5b6e7550328910a7377c9fce59d",
}
MAX_TOTAL_COST = 2 * 10 ** 18


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def rpc(method, params):
    last = None
    for url in RPCS:
        try:
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
            req = urllib.request.Request(url, body, {"Content-Type": "application/json", "User-Agent": "FLGNS-ops/1"})
            with urllib.request.urlopen(req, timeout=25) as response:
                result = json.load(response)
            if "error" in result:
                raise RuntimeError(str(result["error"]))
            require("result" in result, "Missing RPC result")
            return result["result"]
        except Exception as error:
            last = error
    raise RuntimeError(f"RPC {method} failed: {last}")


def selector(signature):
    return "0x" + keccak(text=signature)[:4].hex()


def read(address, signature, block="latest", args=""):
    value = rpc("eth_call", [{"to": address, "data": selector(signature) + args}, block])
    require(value != "0x", f"Empty getter: {signature}")
    return int(value, 16)


def expected_runtime(artifact, token):
    code = bytearray.fromhex(artifact["deployedBytecode"][2:])
    value = bytes.fromhex(token[2:].rjust(64, "0"))
    for slots in artifact["immutableReferences"].values():
        for slot in slots:
            require(slot["length"] == 32, "Unsupported immutable width")
            code[slot["start"]:slot["start"] + 32] = value
    return "0x" + code.hex()


def verify(address, token, artifact, block="latest"):
    require(rpc("eth_getCode", [address, block]).lower() == expected_runtime(artifact, token).lower(), "Runtime mismatch")
    for signature, value in {
        "TOKEN_B()": int(token, 16), "FLGNS()": int(FLGNS, 16),
        "FEE_RECIPIENT_60()": int(RECIPIENT60, 16), "FEE_RECIPIENT_40()": int(RECIPIENT40, 16),
        "FEE_BPS()": 40, "FEE_SHARE_60_BPS()": 6000,
    }.items():
        require(read(address, signature, block) == value, f"Getter mismatch: {signature}")


def verify_splitter(block="latest"):
    expected = (ROOT / "expected-splitter-runtime.hex").read_text().strip().lower()
    require(rpc("eth_getCode", [SPLITTER, block]).lower() == expected, "Existing splitter runtime changed")
    for signature, value in {"SOURCE()": RECIPIENT40, "PAYEE1()": "0xe8782615e32557f257dd88d70b1eb0d7f47b91c3",
                             "PAYEE2()": "0xd53a00bb90bb606f10d888419a14eca779f17285", "PAYEE3()": RECIPIENT40,
                             "keeper()": KEEPER}.items():
        require(read(SPLITTER, signature, block) == int(value, 16), "Splitter address getter mismatch: " + signature)
    for signature, value in {"BPS1()": 3000, "BPS2()": 4000, "BPS3()": 3000}.items():
        require(read(SPLITTER, signature, block) == value, "Splitter ratio mismatch")


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def artifact_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plan_digest(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def new_plan(artifact, artifact_path):
    latest = int(rpc("eth_getTransactionCount", [KEEPER, "latest"]), 16)
    nonce = int(rpc("eth_getTransactionCount", [KEEPER, "pending"]), 16)
    require(latest == nonce, "Keeper has pending transaction; plan later")
    price = int(rpc("eth_gasPrice", []), 16)
    block = rpc("eth_getBlockByNumber", ["latest", False])
    plans = []
    for offset, (name, token) in enumerate(MARKETS.items()):
        data = artifact["bytecode"] + token[2:].rjust(64, "0")
        gas = (int(rpc("eth_estimateGas", [{"from": KEEPER, "data": data, "value": "0x0"}]), 16) * 130 + 99) // 100
        address = "0x" + keccak(rlp.encode([bytes.fromhex(KEEPER[2:]), nonce + offset]))[-20:].hex()
        require(rpc("eth_getCode", [address, "latest"]) == "0x", "Predicted address already occupied")
        plans.append({"name": name, "token": token, "nonce": nonce + offset, "gas": gas, "gasPrice": price, "address": address})
    cost = sum(p["gas"] * p["gasPrice"] for p in plans)
    require(cost <= MAX_TOTAL_COST, "Four-market maximum gas exceeds 2 DAI")
    require(int(rpc("eth_getBalance", [KEEPER, "latest"]), 16) >= cost, "Insufficient gas balance")
    baseline = {token: str(read(SPLITTER, "totalSplit(address)", block["number"], token[2:].rjust(64, "0"))) for token in [FLGNS, MARKETS["rm"]]}
    return {"version": 2, "feeBps": 40, "feeMode": "both-received-assets", "chainId": 6714, "deployer": KEEPER,
            "artifactSha256": artifact_hash(artifact_path), "anchorBlock": int(block["number"], 16),
            "anchorHash": block["hash"], "maximumCostWei": str(cost), "hardCapWei": str(MAX_TOTAL_COST),
            "splitter": SPLITTER, "splitBaseline": baseline, "markets": plans}


def validate_plan(plan, artifact_path):
    require(plan.get("version") == 2 and plan.get("feeBps") == 40 and plan.get("feeMode") == "both-received-assets", "Wrong fee version in plan")
    require(plan["chainId"] == 6714 and plan["deployer"].lower() == KEEPER, "Wrong chain/deployer in plan")
    require(plan["artifactSha256"] == artifact_hash(artifact_path), "Artifact changed after plan")
    require(len(plan["markets"]) == 4, "Plan must contain exactly four markets")
    require(plan["splitter"] == SPLITTER and set(plan["splitBaseline"]) == {FLGNS, MARKETS["rm"]}, "Missing splitter baseline")
    require(rpc("eth_getBlockByNumber", [hex(plan["anchorBlock"]), False])["hash"] == plan["anchorHash"], "Plan anchor reorganized")
    for token, value in plan["splitBaseline"].items():
        require(read(SPLITTER, "totalSplit(address)", hex(plan["anchorBlock"]), token[2:].rjust(64, "0")) == int(value), "Splitter baseline mismatch")
    first_nonce = plan["markets"][0]["nonce"]
    for index, (entry, (name, token)) in enumerate(zip(plan["markets"], MARKETS.items())):
        require(entry["name"] == name and entry["token"] == token, "Unexpected market")
        require(entry["nonce"] == first_nonce + index, "Noncontiguous nonce plan")
        expected = "0x" + keccak(rlp.encode([bytes.fromhex(KEEPER[2:]), entry["nonce"]]))[-20:].hex()
        require(entry["address"] == expected and entry["gas"] > 0 and entry["gasPrice"] > 0, "Invalid fixed plan")
    cost = sum(p["gas"] * p["gasPrice"] for p in plan["markets"])
    require(cost == int(plan["maximumCostWei"]) and cost <= MAX_TOTAL_COST and int(plan["hardCapWei"]) == MAX_TOTAL_COST, "Gas budget mismatch")


def recover(entry, p, artifact):
    receipt = rpc("eth_getTransactionReceipt", [entry["tx"]])
    if not receipt:
        require(not entry.get("verified"), "Previously verified receipt disappeared; stop for reorganization/RPC review")
        return False
    require(int(receipt["status"], 16) == 1, "Deployment reverted; do not allocate replacement nonces automatically")
    require(receipt["contractAddress"].lower() == p["address"], "Wrong deployed address")
    transaction = rpc("eth_getTransactionByHash", [entry["tx"]])
    require(transaction and transaction["from"].lower() == KEEPER and transaction["to"] is None and int(transaction["nonce"], 16) == p["nonce"], "Wrong deployment transaction")
    require(transaction["input"].lower() == (artifact["bytecode"] + p["token"][2:].rjust(64, "0")).lower(), "Wrong constructor transaction")
    require(int(transaction["gasPrice"], 16) == p["gasPrice"] and int(transaction["gas"], 16) == p["gas"], "Deployment gas changed")
    verify(p["address"], p["token"], artifact, receipt["blockNumber"])
    verify(p["address"], p["token"], artifact)
    entry.update({"verified": True, "block": int(receipt["blockNumber"], 16), "blockHash": receipt["blockHash"],
                  "gasUsed": int(receipt["gasUsed"], 16), "runtimeKeccak256": "0x" + keccak(bytes.fromhex(expected_runtime(artifact, p["token"])[2:])).hex()})
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--broadcast", action="store_true")
    parser.add_argument("--refresh-unused-plan", action="store_true", help="Explicitly refresh only a never-broadcast plan under the shared signer lock")
    parser.add_argument("--artifact", default=str(ROOT.parent / "contracts/build/FlgnsOtcEscrowV2.artifact.json"))
    parser.add_argument("--plan", default=str(ROOT / "deployment-plan.json"))
    parser.add_argument("--output", default=str(ROOT / "deployment.json"))
    args = parser.parse_args()
    artifact_path, plan_path, output_path = Path(args.artifact), Path(args.plan), Path(args.output)
    artifact = json.loads(artifact_path.read_text())
    require(artifact["compiler"].startswith("0.8.24+"), "Wrong compiler")
    require(artifact["settings"]["evmVersion"] == "paris" and artifact["settings"]["optimizer"] == {"enabled": True, "runs": 200}, "Wrong compiler settings")
    require(int(rpc("eth_chainId", []), 16) == 6714, "Wrong network")
    verify_splitter()
    if not plan_path.exists():
        require(not args.broadcast, "Prepare and review a saved plan before broadcast")
        require(not output_path.exists(), "Deployment journal exists without plan; recover original plan")
        atomic_json(plan_path, new_plan(artifact, artifact_path))
    plan = json.loads(plan_path.read_text())
    validate_plan(plan, artifact_path)
    if args.refresh_unused_plan:
        require(not args.broadcast, "Refresh must finish and persist before the separate broadcast step")
        require(not output_path.exists(), "A transaction journal exists; recover it, never allocate replacement nonces")
        for entry in plan["markets"]:
            require(rpc("eth_getCode", [entry["address"], "latest"]) == "0x", "Old planned contract exists; recover original journal")
        current = int(rpc("eth_getTransactionCount", [KEEPER, "latest"]), 16)
        pending = int(rpc("eth_getTransactionCount", [KEEPER, "pending"]), 16)
        require(current == pending, "Keeper transaction pending; do not refresh plan")
        if current != plan["markets"][0]["nonce"]:
            plan = new_plan(artifact, artifact_path)
            atomic_json(plan_path, plan)
            validate_plan(plan, artifact_path)
    digest = plan_digest(plan)
    state = json.loads(output_path.read_text()) if output_path.exists() else {"chainId": 6714, "deployer": KEEPER, "artifactSha256": plan["artifactSha256"], "planSha256": digest, "markets": {}}
    require(state["planSha256"] == digest and state["artifactSha256"] == plan["artifactSha256"], "Journal belongs to another plan")
    require(set(state["markets"]).issubset(MARKETS), "Unexpected deployment journal market")
    for p in plan["markets"]:
        entry = state["markets"].get(p["name"])
        if entry:
            require(entry["token"] == p["token"] and entry["address"] == p["address"] and entry["nonce"] == p["nonce"], "Journal altered")
            recover(entry, p, artifact)
            if args.broadcast:
                atomic_json(output_path, state)
        else:
            require(rpc("eth_getCode", [p["address"], "latest"]) == "0x", "Address occupied without journal; recover original journal")
    print(json.dumps({"mode": "broadcast" if args.broadcast else "read-only", "planSha256": digest, "plan": plan, "markets": state["markets"]}, indent=2), flush=True)
    if not args.broadcast:
        return
    require(os.environ.get("GITHUB_ACTIONS") == "true", "Broadcast only permitted inside GitHub Actions")
    account = Account.from_key(os.environ["OTC_KEEPER_KEY"])
    require(account.address.lower() == KEEPER, "Wrong signer")
    for p in plan["markets"]:
        entry = state["markets"].get(p["name"])
        if entry and entry.get("verified"):
            continue
        require(int(rpc("eth_chainId", []), 16) == 6714, "Wrong network")
        data = artifact["bytecode"] + p["token"][2:].rjust(64, "0")
        tx = {"chainId": 6714, "nonce": p["nonce"], "gas": p["gas"], "gasPrice": p["gasPrice"], "value": 0, "data": data}
        signed = account.sign_transaction(tx)
        tx_hash = "0x" + keccak(signed.raw_transaction).hex()
        if entry:
            require(entry["tx"] == tx_hash, "Recovery hash mismatch")
            known = rpc("eth_getTransactionByHash", [tx_hash])
        else:
            known = None
            entry = {"token": p["token"], "address": p["address"], "nonce": p["nonce"], "tx": tx_hash, "verified": False}
            state["markets"][p["name"]] = entry
            atomic_json(output_path, state)
        if not known:
            require(int(rpc("eth_getTransactionCount", [KEEPER, "latest"]), 16) == p["nonce"] and int(rpc("eth_getTransactionCount", [KEEPER, "pending"]), 16) == p["nonce"], "Nonce changed; original plan cannot be reused")
            require(int(rpc("eth_estimateGas", [{"from": KEEPER, "data": data, "value": "0x0"}]), 16) <= p["gas"], "Saved gas limit too small")
            require(int(rpc("eth_getBalance", [KEEPER, "latest"]), 16) >= sum(q["gas"] * q["gasPrice"] for q in plan["markets"] if q["nonce"] >= p["nonce"]), "Insufficient remaining gas")
            returned = rpc("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()])
            require(returned.lower() == tx_hash.lower(), "Returned transaction hash mismatch")
            print(f"Submitted {p['name']}: {tx_hash}", flush=True)
        for _ in range(120):
            if recover(entry, p, artifact):
                atomic_json(output_path, state)
                break
            time.sleep(2)
        require(entry.get("verified"), f"Receipt pending: {tx_hash}; rerun same saved plan/journal")
        print(f"Verified {p['name']}: {p['address']}", flush=True)
    require(sum(e["gasUsed"] * next(p["gasPrice"] for p in plan["markets"] if p["name"] == n) for n, e in state["markets"].items()) <= MAX_TOTAL_COST, "Actual gas budget exceeded")


if __name__ == "__main__":
    main()
