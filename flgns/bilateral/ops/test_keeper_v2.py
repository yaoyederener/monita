"""Offline regressions: fixed V1/V2 income, receipts, backfill, and checkpoints.

All networking/signing in these tests is mocked. No environment secret is read.
"""
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from eth_abi import encode
import keeper

MAKER = "0x1111111111111111111111111111111111111111"
TAKER = "0x2222222222222222222222222222222222222222"
OLD = "0x3333333333333333333333333333333333333333"
NEW = "0x4444444444444444444444444444444444444444"
TX = "0x" + "ab" * 32
HASH = "0x" + "cd" * 32


def topic(value):
    return "0x" + keeper.pad(value)


def data(values):
    return "0x" + "".join(keeper.pad(value) for value in values)


class KeeperV2Tests(unittest.TestCase):
    def setUp(self):
        self.plan = {"markets": [{"name": "rm", "token": keeper.MARKETS["rm"], "address": OLD}],
                     "splitBaseline": {token: "0" for token in keeper.TRACKED}}
        self.v2plan = {"markets": [{"name": "rm", "token": keeper.MARKETS["rm"], "address": NEW}]}
        self.deployment = {"markets": {"rm": {"block": 10}}}
        self.v2deployment = {"markets": {"rm": {"block": 50}}}
        self.state = {"planSha256": keeper.plan_digest(self.plan), "scannedTo": 9,
                      "income": {token: "0" for token in keeper.TRACKED}}

    def fixture(self, version=2, selling=True, maker=MAKER, taker=TAKER,
                amount=10000, give_total=10000, want_total=5000,
                block=75, start_index=0, order_id=0, approval=False):
        escrow = NEW if version == 2 else OLD
        entry = {"name": "rm", "token": keeper.MARKETS["rm"], "address": escrow}
        paid = (amount * want_total + give_total - 1) // give_total
        fee = amount * (40 if version == 2 else 200) // 10000
        order = (maker, selling, False, 1, give_total, want_total, 0)
        filled = {"address": escrow, "transactionHash": TX, "logIndex": "0x0", "blockNumber": hex(block),
                  "blockHash": HASH, "topics": [keeper.FILLED, topic(order_id), topic(taker)],
                  "data": data([amount, paid, fee, give_total - amount])}
        # Construct the receipt independently of the production parser. These
        # six calls follow the reviewed Solidity execution order, not a helper
        # imported from the implementation being tested.
        give_token = keeper.FLGNS if selling else keeper.MARKETS["rm"]
        want_token = keeper.MARKETS["rm"] if selling else keeper.FLGNS
        maker_fee = paid * 40 // 10000 if version == 2 else 0
        expected = [(want_token, taker, maker, paid - maker_fee)]
        if maker_fee:
            if maker_fee * 6000 // 10000:
                expected.append((want_token, taker, keeper.RECIPIENT60, maker_fee * 6000 // 10000))
            expected.append((want_token, taker, keeper.RECIPIENT40, maker_fee - maker_fee * 6000 // 10000))
        expected.append((give_token, escrow, taker, amount - fee))
        if fee:
            if fee * 6000 // 10000:
                expected.append((give_token, escrow, keeper.RECIPIENT60, fee * 6000 // 10000))
            expected.append((give_token, escrow, keeper.RECIPIENT40, fee - fee * 6000 // 10000))
        logs = []
        def append(address, topics, values):
            log = {"address": address, "transactionHash": TX, "logIndex": hex(start_index + len(logs)),
                   "blockNumber": hex(block), "blockHash": HASH, "topics": topics, "data": data(values)}
            logs.append(log)
            return log
        for index, (token, sender, recipient, value) in enumerate(expected):
            append(token, [keeper.TRANSFER, topic(sender), topic(recipient)], [value])
            if approval and index == 0:
                append(token, ["0x" + "ef" * 32, topic(sender), topic(escrow)], [123])
        filled["logIndex"] = hex(start_index + len(logs))
        logs.append(filled)
        if version == 2:
            append(escrow, [keeper.MAKER_FEE, topic(order_id), topic(maker), topic(want_token)], [paid, maker_fee])
        receipt = {"status": "0x1", "transactionHash": TX, "blockHash": HASH,
                   "blockNumber": hex(block), "logs": logs}
        return filled, receipt, order

    def scan(self, filled, receipt, order, state=None, mutate=None, include_v1=False):
        state = self.state if state is None else state
        before = copy.deepcopy(state)
        queries = []
        receipts = []
        def network(method, params):
            if method == "eth_getLogs":
                query = params[0]
                queries.append(query)
                if filled["address"] not in query["address"]:
                    return []
                if not int(query["fromBlock"], 16) <= int(filled["blockNumber"], 16) <= int(query["toBlock"], 16):
                    return []
                return mutate([filled]) if mutate else [filled]
            if method == "eth_getTransactionReceipt":
                receipts.append(params[0])
                return receipt
            raise AssertionError(method)
        observations = {}
        with patch.object(keeper, "rpc", side_effect=network), patch.object(keeper, "call_bytes", return_value=encode(
                ["(address,bool,bool,uint64,uint256,uint256,uint256)"], [order])):
            income = keeper.scan_fees(self.plan, self.deployment, state, 200,
                self.v2plan if not include_v1 else None,
                self.v2deployment if not include_v1 else None, observations)
        self.assertEqual(state, before)
        return income, observations, queries, receipts

    def test_v2_two_tokens_maker_and_taker_both_counted_both_directions(self):
        for selling in (True, False):
            with self.subTest(selling=selling):
                income, observations, _, _ = self.scan(*self.fixture(selling=selling))
                self.assertEqual(income[keeper.FLGNS], 16 if selling else 8)
                self.assertEqual(income[keeper.MARKETS["rm"]], 8 if selling else 16)
                self.assertEqual(sum(observations["selfTransferFeeSkippedRaw"].values()), 0)

    def test_v1_two_percent_preserved_without_maker_fee(self):
        income, _, _, _ = self.scan(*self.fixture(version=1, block=20), include_v1=True)
        self.assertEqual(income[keeper.FLGNS], 80)
        self.assertEqual(income[keeper.MARKETS["rm"]], 0)

    def test_source_maker_net_payment_is_not_counted_as_fee(self):
        income, _, _, _ = self.scan(*self.fixture(maker=keeper.RECIPIENT40))
        self.assertEqual(income[keeper.MARKETS["rm"]], 8)
        self.assertEqual(income[keeper.FLGNS], 16)

    def test_source_taker_self_transfer_fee_validated_but_not_income(self):
        income, observations, _, _ = self.scan(*self.fixture(taker=keeper.RECIPIENT40))
        self.assertEqual(income[keeper.MARKETS["rm"]], 0)
        self.assertEqual(income[keeper.FLGNS], 16)
        self.assertEqual(observations["selfTransferFeeSkippedRaw"][keeper.MARKETS["rm"]], 8)
        self.assertEqual(len(observations["selfTransferFeeLegs"]), 1)

    def test_source_both_sides_and_r60_overlaps_preserve_exact_legs(self):
        for maker, taker in [(keeper.RECIPIENT40, keeper.RECIPIENT40),
                             (keeper.RECIPIENT60, keeper.RECIPIENT60), (MAKER, MAKER)]:
            with self.subTest(maker=maker, taker=taker):
                income, _, _, _ = self.scan(*self.fixture(maker=maker, taker=taker))
                self.assertEqual(income[keeper.FLGNS], 16)
                self.assertEqual(income[keeper.MARKETS["rm"]], 0 if taker == keeper.RECIPIENT40 else 8)

    def test_transfer_from_approval_logs_are_allowed_between_exact_transfer_legs(self):
        income, _, _, _ = self.scan(*self.fixture(approval=True))
        self.assertEqual(income[keeper.FLGNS], 16)

    def test_first_v2_backfill_covers_deployment_even_when_v1_checkpoint_is_later(self):
        state = copy.deepcopy(self.state)
        state["scannedTo"] = 100
        state["income"][keeper.FLGNS] = "77"
        income, _, queries, _ = self.scan(*self.fixture(block=75), state=state)
        self.assertEqual([int(query["fromBlock"], 16) for query in queries], [101, 50])
        self.assertEqual(income[keeper.FLGNS], 93)

    def test_migrated_checkpoint_does_not_recount_historical_v2_income(self):
        state = copy.deepcopy(self.state)
        state.update({"scannedTo": 100, "bilateralPlanSha256": keeper.plan_digest(self.v2plan)})
        state["income"][keeper.FLGNS] = "77"
        income, _, queries, receipts = self.scan(*self.fixture(block=75), state=state)
        self.assertEqual([int(query["fromBlock"], 16) for query in queries], [101, 101])
        self.assertEqual(income[keeper.FLGNS], 77)
        self.assertEqual(receipts, [])

    def test_price_ceiling_and_fee_rounding_are_independent(self):
        income, _, _, _ = self.scan(*self.fixture(amount=251, give_total=1001, want_total=1000))
        self.assertEqual(income, {keeper.FLGNS: 1, keeper.MARKETS["rm"]: 1})
        filled, receipt, order = self.fixture(amount=251, give_total=1001, want_total=1000)
        filled["data"] = data([251, 250, 1, 750])
        with self.assertRaisesRegex(RuntimeError, "rounding mismatch"):
            self.scan(filled, receipt, order)

    def test_zero_fee_has_no_artificial_transfer_requirement(self):
        income, _, _, _ = self.scan(*self.fixture(amount=1, give_total=1, want_total=1))
        self.assertEqual(income, {token: 0 for token in keeper.TRACKED})

    def test_v2_cannot_accept_v1_fee_formula(self):
        filled, receipt, order = self.fixture()
        filled["data"] = data([10000, 5000, 200, 0])
        with self.assertRaisesRegex(RuntimeError, "Unexpected fee formula"):
            self.scan(filled, receipt, order)

    def test_wrong_transfer_or_missing_fee_transfer_fails_closed(self):
        for change in ("wrong-sender", "wrong-amount", "missing"):
            with self.subTest(change=change):
                filled, receipt, order = self.fixture()
                fee_transfer = receipt["logs"][2]
                if change == "wrong-sender":
                    fee_transfer["topics"][1] = topic(OLD)
                elif change == "wrong-amount":
                    fee_transfer["data"] = data([999])
                else:
                    receipt["logs"].remove(fee_transfer)
                with self.assertRaisesRegex(RuntimeError, "transfer sequence"):
                    self.scan(filled, receipt, order)

    def test_missing_duplicate_or_wrong_maker_event_fails_closed(self):
        for change in ("missing", "duplicate", "wrong-fee", "wrong-token", "wrong-id"):
            with self.subTest(change=change):
                filled, receipt, order = self.fixture()
                event = receipt["logs"][-1]
                if change == "missing":
                    receipt["logs"].pop()
                elif change == "duplicate":
                    duplicate = copy.deepcopy(event)
                    duplicate["logIndex"] = hex(int(event["logIndex"], 16) + 1)
                    receipt["logs"].append(duplicate)
                elif change == "wrong-fee":
                    event["data"] = data([5000, 21])
                elif change == "wrong-token":
                    event["topics"][3] = topic(keeper.FLGNS)
                else:
                    event["topics"][1] = topic(99)
                with self.assertRaisesRegex(RuntimeError, "MakerFeePaid"):
                    self.scan(filled, receipt, order)

    def test_duplicate_rpc_filled_does_not_double_credit(self):
        state = copy.deepcopy(self.state)
        with self.assertRaisesRegex(RuntimeError, "Duplicate RPC Filled"):
            self.scan(*self.fixture(), state=state, mutate=lambda logs: logs + logs)
        self.assertEqual(state, self.state)

    def test_duplicate_receipt_logindex_is_rejected(self):
        filled, receipt, order = self.fixture()
        receipt["logs"].insert(1, copy.deepcopy(receipt["logs"][0]))
        with self.assertRaisesRegex(RuntimeError, "Duplicate or unsorted"):
            self.scan(filled, receipt, order)

    def test_filled_in_rpc_must_match_receipt_exactly(self):
        filled, receipt, order = self.fixture()
        rpc_log = copy.deepcopy(filled)
        rpc_log["data"] = data([10000, 5000, 200, 0])
        with self.assertRaisesRegex(RuntimeError, "exact receipt log"):
            self.scan(rpc_log, receipt, order)

    def test_one_receipt_multiple_partial_fills_use_distinct_transfers_and_one_fetch(self):
        first, receipt, order = self.fixture(amount=5000)
        second, second_receipt, _ = self.fixture(amount=5000, start_index=len(receipt["logs"]))
        receipt["logs"].extend(second_receipt["logs"])
        count = []
        def network(method, params):
            if method == "eth_getLogs":
                return [second, first] if NEW in params[0]["address"] else []
            count.append(method)
            return receipt
        with patch.object(keeper, "rpc", side_effect=network), patch.object(keeper, "call_bytes", return_value=encode(
                ["(address,bool,bool,uint64,uint256,uint256,uint256)"], [order])):
            income = keeper.scan_fees(self.plan, self.deployment, self.state, 200, self.v2plan, self.v2deployment)
        self.assertEqual(income, {keeper.FLGNS: 16, keeper.MARKETS["rm"]: 8})
        self.assertEqual(count, ["eth_getTransactionReceipt"])

    def test_reused_transfer_log_is_rejected_even_with_matching_values(self):
        filled, receipt, order = self.fixture()
        used = {keeper.log_identity(receipt["logs"][0])}
        observations = {"selfTransferFeeSkippedRaw": {token: 0 for token in keeper.TRACKED}, "selfTransferFeeLegs": []}
        with self.assertRaisesRegex(RuntimeError, "Transfer log reused"):
            keeper.credit_fill(self.v2plan["markets"][0], filled, order, 2, receipt, used, observations)

    def test_rpc_failure_or_reorg_never_treated_as_zero(self):
        for error in (RuntimeError("RPC unavailable"),):
            with patch.object(keeper, "rpc", side_effect=error):
                with self.assertRaisesRegex(RuntimeError, "unavailable"):
                    keeper.scan_fees(self.plan, self.deployment, self.state, 200, self.v2plan, self.v2deployment)
        filled, receipt, order = self.fixture()
        receipt["blockHash"] = "0x" + "00" * 32
        with self.assertRaisesRegex(RuntimeError, "missing/reorganized"):
            self.scan(filled, receipt, order)

    def test_split_still_uses_original_baseline_and_two_independent_floors(self):
        self.plan["splitBaseline"] = {token: "99" for token in keeper.TRACKED}
        with patch.object(keeper, "read", return_value=100):
            actions, blocked = keeper.split_actions(self.plan, {token: 1 for token in keeper.TRACKED}, 100)
        self.assertEqual((actions, blocked), ([], []))
        self.plan["splitBaseline"] = {token: "0" for token in keeper.TRACKED}
        with patch.object(keeper, "read", return_value=0):
            actions, blocked = keeper.split_actions(self.plan, {keeper.FLGNS: 2, keeper.MARKETS["rm"]: 0}, 100)
        self.assertEqual(blocked, [])
        self.assertEqual(actions[0]["amount"], "2")

    def test_all_legacy_expired_orders_are_still_paginated(self):
        total = 1201
        def page(address, calldata, block):
            start = int(calldata[10:74], 16)
            return encode(["(address,bool,bool,uint64,uint256,uint256,uint256)[]"], [[
                (MAKER, True, start + offset == 0, 10, 1, 1, 1)
                for offset in range(min(200, total - start))]])
        with patch.object(keeper, "rpc", return_value={"timestamp": "0x14"}), patch.object(keeper, "read", return_value=total), patch.object(keeper, "call_bytes", side_effect=page):
            actions = keeper.expired_orders(self.plan, 100)
        self.assertEqual(actions[0]["ids"], [0])

    def run_main(self, folder, execute=True, fail_scan=False, blocked=False, reorg_after=False):
        for filename, value in [("old-plan.json", self.plan), ("old-deployment.json", {}), ("old-artifact.json", {}),
                                 ("new-plan.json", self.v2plan), ("new-deployment.json", {}), ("new-artifact.json", {}),
                                 ("state.json", self.state)]:
            (folder / filename).write_text(json.dumps(value))
        argv = ["keeper.py", "--plan", str(folder / "old-plan.json"), "--deployment", str(folder / "old-deployment.json"),
                "--artifact", str(folder / "old-artifact.json"), "--bilateral-plan", str(folder / "new-plan.json"),
                "--bilateral-deployment", str(folder / "new-deployment.json"), "--bilateral-artifact", str(folder / "new-artifact.json"),
                "--state", str(folder / "state.json"), "--journal", str(folder / "journal.json")]
        if execute:
            argv.append("--execute")
        scan_income = {token: 8 for token in keeper.TRACKED}
        def scan(*args):
            if fail_scan:
                raise RuntimeError("RPC unavailable")
            args[-1].update({"selfTransferFeeSkippedRaw": {token: 1 for token in keeper.TRACKED}, "selfTransferFeeLegs": []})
            return scan_income
        hashes = [HASH, HASH, "reorganized" if reorg_after else HASH]
        with patch.object(sys, "argv", argv), patch.object(keeper, "rpc", side_effect=lambda method, params: "0x1a3a" if method == "eth_chainId" else "0x100"), patch.object(keeper, "validate_plan"), patch.object(keeper, "validate_v2_plan"), patch.object(keeper, "verify_deployment"), patch.object(keeper, "verify_splitter"), patch.object(keeper, "block_hash", side_effect=hashes), patch.object(keeper, "scan_fees", side_effect=scan), patch.object(keeper, "split_actions", return_value=([], ["approval missing"] if blocked else [])), patch.object(keeper, "expired_orders", return_value=[]) as refunds, patch.object(keeper, "execute") as submit, redirect_stdout(io.StringIO()):
            keeper.main()
        return refunds, submit

    def test_successful_main_migrates_original_ledger_checkpoint_and_new_digest_once(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            refunds, submit = self.run_main(folder)
            saved = json.loads((folder / "state.json").read_text())
            self.assertEqual(saved["planSha256"], keeper.plan_digest(self.plan))
            self.assertEqual(saved["bilateralPlanSha256"], keeper.plan_digest(self.v2plan))
            self.assertEqual(saved["scannedTo"], 240)
            self.assertEqual(saved["schemaVersion"], 2)
            self.assertEqual(saved["income"], {token: "8" for token in keeper.TRACKED})
            self.assertEqual(refunds.call_args_list[0].args[0], self.plan)
            self.assertEqual(refunds.call_args_list[1].args[0], self.v2plan)
            self.assertEqual(submit.call_args.args[1], self.plan)

    def test_failed_scan_blocked_split_or_reorg_keeps_checkpoint_bytes_unchanged(self):
        for kwargs, message in [({"fail_scan": True}, "unavailable"), ({"blocked": True}, "approval missing"),
                                ({"reorg_after": True}, "during execution")]:
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                with self.assertRaisesRegex(RuntimeError, message):
                    self.run_main(folder, **kwargs)
                self.assertEqual(json.loads((folder / "state.json").read_text()), self.state)

    def test_dry_run_never_saves_state_or_calls_execute(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            _, submit = self.run_main(folder, execute=False)
            self.assertEqual(json.loads((folder / "state.json").read_text()), self.state)
            submit.assert_not_called()

    def test_wrong_bilateral_state_digest_is_rejected(self):
        state = copy.deepcopy(self.state)
        state["bilateralPlanSha256"] = "other"
        with self.assertRaisesRegex(RuntimeError, "Wrong bilateral keeper state"):
            self.scan(*self.fixture(), state=state)

    def test_execute_guard_precedes_key_access(self):
        with patch.object(keeper.os.environ, "get", return_value="false"), patch.object(keeper.Account, "from_key") as signer:
            with self.assertRaisesRegex(RuntimeError, "only allowed"):
                keeper.execute([], self.plan, Path("not-used.json"), self.v2plan)
            signer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
