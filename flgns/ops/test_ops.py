"""Offline regressions for operation-specific accounting and safety boundaries."""
import copy
import os
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

from eth_abi import encode
import deploy
import keeper


class OpsTests(unittest.TestCase):
    def setUp(self):
        self.address = "0x1111111111111111111111111111111111111111"
        self.plan = {"splitBaseline": {t: "0" for t in keeper.TRACKED}, "markets": [{"name": "rm", "token": deploy.MARKETS["rm"], "address": self.address}]}
        self.deployment = {"markets": {"rm": {"block": 10}}}

    def test_split_uses_two_independent_floors(self):
        # 2 units: both outgoing portions floor to zero; ceil(70%) would demand 2.
        income = {deploy.FLGNS: 2, deploy.MARKETS["rm"]: 0}
        with patch.object(keeper, "read", return_value=0):
            actions, blocked = keeper.split_actions(self.plan, income, 100)
        self.assertEqual(blocked, [])
        self.assertEqual(actions[0]["amount"], "2")
        self.assertEqual(len(actions), 1)

    def test_split_total_above_income_is_a_failure(self):
        with patch.object(keeper, "read", return_value=11):
            with self.assertRaisesRegex(RuntimeError, "exceeds new OTC"):
                keeper.split_actions(self.plan, {t: 10 for t in keeper.TRACKED}, 100)

    def test_fixed_split_baseline_is_subtracted(self):
        plan = copy.deepcopy(self.plan)
        plan["splitBaseline"] = {t: "99" for t in keeper.TRACKED}
        with patch.object(keeper, "read", return_value=100):
            actions, blocked = keeper.split_actions(plan, {t: 1 for t in keeper.TRACKED}, 100)
        self.assertEqual((actions, blocked), ([], []))

    def test_unapproved_fees_are_reported_and_not_sent(self):
        with patch.object(keeper, "read", return_value=0):
            actions, blocked = keeper.split_actions(self.plan, {t: 100 for t in keeper.TRACKED}, 100)
        self.assertEqual(actions, [])
        self.assertEqual(len(blocked), 2)
        self.assertTrue(all("approve" in issue for issue in blocked))

    def scan_fixture(self, token, sell_flgns):
        share = 8
        log = {"address": self.address, "transactionHash": "0xabc", "logIndex": "0x3", "blockNumber": "0x14", "blockHash": "0xhash", "topics": [keeper.FILLED, "0x" + keeper.pad(0), "0x" + keeper.pad(deploy.KEEPER)], "data": "0x" + "".join(keeper.pad(n) for n in [1000, 500, 20, 0])}
        transfer = {"address": token, "logIndex": "0x2", "topics": [keeper.TRANSFER, "0x" + keeper.pad(self.address), "0x" + keeper.pad(deploy.RECIPIENT40)], "data": "0x" + keeper.pad(share)}
        receipt = {"status": "0x1", "blockHash": "0xhash", "blockNumber": "0x14", "transactionHash": "0xabc", "logs": [transfer, log]}
        order = encode(["(address,bool,bool,uint64,uint256,uint256,uint256)"], [(deploy.KEEPER, sell_flgns, False, 1, 1000, 500, 0)])
        state = {"scannedTo": 9, "income": {t: "0" for t in keeper.TRACKED}}
        return log, receipt, order, state

    def test_only_actual_new_market_fee_transfer_is_credited(self):
        for token, selling in [(deploy.FLGNS, True), (deploy.MARKETS["rm"], False)]:
            log, receipt, order, state = self.scan_fixture(token, selling)
            def mock_rpc(method, params):
                return [log] if method == "eth_getLogs" else receipt
            with patch.object(keeper, "rpc", side_effect=mock_rpc), patch.object(keeper, "call_bytes", return_value=order):
                income = keeper.scan_fees(self.plan, self.deployment, state, 20)
            self.assertEqual(income[token], 8)
            self.assertEqual(state["scannedTo"], 9)
            self.assertEqual(state["income"][token], "0")

    def test_refund_or_wrong_fee_transfer_is_never_credited(self):
        log, receipt, order, state = self.scan_fixture(deploy.FLGNS, True)
        receipt["logs"][0]["data"] = "0x" + keeper.pad(999)
        with patch.object(keeper, "rpc", side_effect=lambda method, params: [log] if method == "eth_getLogs" else receipt), patch.object(keeper, "call_bytes", return_value=order):
            with self.assertRaisesRegex(RuntimeError, "fee receipt mismatch"):
                keeper.scan_fees(self.plan, self.deployment, state, 20)
        self.assertEqual(state["scannedTo"], 9)

    def test_log_rpc_failure_is_not_an_empty_page(self):
        _, _, _, state = self.scan_fixture(deploy.FLGNS, True)
        with patch.object(keeper, "rpc", side_effect=RuntimeError("RPC unavailable")):
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                keeper.scan_fees(self.plan, self.deployment, state, 20)
        self.assertEqual(state["income"][deploy.FLGNS], "0")

    def test_refund_pagination_does_not_drop_old_orders(self):
        total = 1201
        def page(address, data, block):
            start = int(data[10:74], 16)
            count = min(200, total - start)
            return encode(["(address,bool,bool,uint64,uint256,uint256,uint256)[]"], [[(deploy.KEEPER, True, start + i == 0, 10, 1, 1, 1) for i in range(count)]])
        with patch.object(keeper, "rpc", return_value={"timestamp": "0x14"}), patch.object(keeper, "read", return_value=total), patch.object(keeper, "call_bytes", side_effect=page):
            actions = keeper.expired_orders(self.plan, 100)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["ids"], [0])

    def test_execution_requires_actions_before_reading_key(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}, clear=True), patch.object(keeper.Account, "from_key") as signer:
            with self.assertRaisesRegex(RuntimeError, "only allowed"):
                keeper.execute([], {}, Path("not-used.json"))
            signer.assert_not_called()

    def test_pending_journal_never_signs_or_broadcasts_again(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = {}
            journal_path = Path(directory) / "journal.json"
            journal_path.write_text(json.dumps({"planSha256": deploy.plan_digest(plan), "transactions": [{"tx": "0xabc", "confirmed": False, "action": {"kind": "refund"}}]}))
            account = Mock(address=deploy.KEEPER)
            with patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "OTC_KEEPER_KEY": "mock-only"}, clear=True), patch.object(keeper.Account, "from_key", return_value=account), patch.object(keeper, "receipt_check", return_value=None), patch.object(keeper, "rpc") as network:
                with self.assertRaisesRegex(RuntimeError, "pending/unknown"):
                    keeper.execute([], plan, journal_path)
                account.sign_transaction.assert_not_called()
                network.assert_not_called()

    def test_changed_split_counter_prevents_repeated_signing(self):
        with tempfile.TemporaryDirectory() as directory:
            action = {"kind": "split", "token": deploy.FLGNS, "previousTotalSplit": "0", "amount": "100", "to": deploy.SPLITTER, "data": "0x1234"}
            account = Mock(address=deploy.KEEPER)
            def network(method, params):
                if method == "eth_chainId":
                    return "0x1a3a"
                if method == "eth_getTransactionCount":
                    return "0x0"
                raise AssertionError("No estimate/send allowed after a changed split counter")
            with patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "OTC_KEEPER_KEY": "mock-only"}, clear=True), patch.object(keeper.Account, "from_key", return_value=account), patch.object(keeper, "rpc", side_effect=network), patch.object(keeper, "verify_splitter"), patch.object(keeper, "read", return_value=100):
                with self.assertRaisesRegex(RuntimeError, "counter changed"):
                    keeper.execute([action], {}, Path(directory) / "journal.json")
                account.sign_transaction.assert_not_called()

    def test_previously_verified_deployment_cannot_lose_receipt(self):
        with patch.object(deploy, "rpc", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "receipt disappeared"):
                deploy.recover({"tx": "0xabc", "verified": True}, {}, {})

    def test_reorganization_during_execute_does_not_save_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            plan = {"markets": [{"name": name, "token": token, "address": self.address} for name, token in deploy.MARKETS.items()]}
            deployment = {"planSha256": deploy.plan_digest(plan), "markets": {name: {"verified": True, "address": self.address, "block": 10, "blockHash": "old"} for name in deploy.MARKETS}}
            for name, value in [("plan.json", plan), ("deployment.json", deployment), ("artifact.json", {})]:
                (folder / name).write_text(json.dumps(value))
            args = ["keeper.py", "--execute", "--plan", str(folder / "plan.json"), "--deployment", str(folder / "deployment.json"), "--artifact", str(folder / "artifact.json"), "--state", str(folder / "state.json")]
            def head(method, params):
                return "0x1a3a" if method == "eth_chainId" else "0x64"
            # First head hash, 4 deployment hashes, before-execute head, after-execute head.
            hashes = ["old"] * 6 + ["reorganized"]
            with patch.object(sys, "argv", args), patch.object(keeper, "rpc", side_effect=head), patch.object(keeper, "validate_plan"), patch.object(keeper, "verify_splitter"), patch.object(keeper, "verify"), patch.object(keeper, "block_hash", side_effect=hashes), patch.object(keeper, "scan_fees", return_value={t: 0 for t in keeper.TRACKED}), patch.object(keeper, "expired_orders", return_value=[]), patch.object(keeper, "split_actions", return_value=([], [])), patch.object(keeper, "execute"), patch.object(keeper, "atomic_json") as save:
                with self.assertRaisesRegex(RuntimeError, "during execution"):
                    keeper.main()
                save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
