import unittest
from datetime import datetime, timedelta, timezone

from ibs_wallet_profiler import Trade, classify, fifo_holding_hours, round_trip_metrics


class WalletProfilerTests(unittest.TestCase):
    def test_fifo_holding_time_matches_buys_to_sells(self):
        start = datetime(2026, 6, 1, tzinfo=timezone.utc)
        trades = [
            Trade("0x1", 1, 1, "BUY", 100, 1000, start),
            Trade("0x2", 2, 1, "SELL", 40, 500, start + timedelta(hours=6)),
            Trade("0x3", 3, 1, "SELL", 60, 700, start + timedelta(hours=30)),
        ]
        self.assertEqual([6, 30], [round(x) for x in fifo_holding_hours(trades)])

    def test_protocol_address_wins_classification(self):
        category, reasons, _ = classify(
            "0x6025FC9840Cc4e282125a74F4b00dC5038A8058f", False, [], 0, 0, None
        )
        self.assertIn("平台/协议地址", category)
        self.assertTrue(reasons)

    def test_fast_round_trips_are_arbitrage(self):
        start = datetime(2026, 6, 1, tzinfo=timezone.utc)
        trades = []
        for i in range(3):
            trades.extend([
                Trade(f"0xb{i}", i * 2, 1, "BUY", 100, 1000, start + timedelta(days=i)),
                Trade(f"0xs{i}", i * 2 + 1, 1, "SELL", 100, 1010, start + timedelta(days=i, hours=2)),
            ])
        category, _, assessment = classify("0x1111111111111111111111111111111111111111", False, trades, 0, 0, start)
        self.assertIn("疑似套利", category)
        self.assertIn("正利润闭环", assessment)

    def test_unfunded_early_seller_is_only_suspicious_not_asserted(self):
        trades = [Trade("0x1", 1, 1, "SELL", 100, 1000, datetime(2026, 4, 25, tzinfo=timezone.utc))]
        category, reasons, _ = classify("0x1111111111111111111111111111111111111111", False, trades, 1, 0, None)
        self.assertEqual("协议来源IBS变现地址", category)
        self.assertTrue(reasons)

    def test_protocol_funded_fast_trader_is_not_called_arbitrage(self):
        start = datetime(2026, 6, 1, tzinfo=timezone.utc)
        trades = []
        for i in range(3):
            trades.extend([
                Trade(f"0xb{i}", i * 2, 1, "BUY", 100, 1000, start + timedelta(days=i)),
                Trade(f"0xs{i}", i * 2 + 1, 1, "SELL", 150, 1600, start + timedelta(days=i, hours=2)),
            ])
        category, _, assessment = classify(
            "0x1111111111111111111111111111111111111111", False, trades, 500, 0, start
        )
        self.assertEqual("协议来源IBS变现地址", category)
        self.assertIn("未证实套利", assessment)

    def test_round_trip_profit_ignores_unmatched_transferred_inventory(self):
        start = datetime(2026, 6, 1, tzinfo=timezone.utc)
        trades = [
            Trade("0xb", 1, 1, "BUY", 100, 1000, start),
            Trade("0xs", 2, 1, "SELL", 300, 3300, start + timedelta(hours=1)),
        ]
        metrics = round_trip_metrics(trades)
        self.assertEqual(100, metrics["matched_ibs_raw"])
        self.assertEqual(100, metrics["matched_pnl_raw"])


if __name__ == "__main__":
    unittest.main()
import unittest
from datetime import datetime, timedelta, timezone

from ibs_wallet_profiler import Trade, classify, fifo_holding_hours


class WalletProfilerTests(unittest.TestCase):
    def test_fifo_holding_time_matches_buys_to_sells(self):
        start = datetime(2026, 6, 1, tzinfo=timezone.utc)
        trades = [
            Trade("0x1", 1, 1, "BUY", 100, 1000, start),
            Trade("0x2", 2, 1, "SELL", 40, 500, start + timedelta(hours=6)),
            Trade("0x3", 3, 1, "SELL", 60, 700, start + timedelta(hours=30)),
        ]
        self.assertEqual([6, 30], [round(x) for x in fifo_holding_hours(trades)])

    def test_protocol_address_wins_classification(self):
        category, reasons = classify(
            "0x6025FC9840Cc4e282125a74F4b00dC5038A8058f", False, [], 0, None
        )
        self.assertIn("平台/协议地址", category)
        self.assertTrue(reasons)

    def test_fast_round_trips_are_arbitrage(self):
        start = datetime(2026, 6, 1, tzinfo=timezone.utc)
        trades = []
        for i in range(3):
            trades.extend([
                Trade(f"0xb{i}", i * 2, 1, "BUY", 100, 1000, start + timedelta(days=i)),
                Trade(f"0xs{i}", i * 2 + 1, 1, "SELL", 100, 1010, start + timedelta(days=i, hours=2)),
            ])
        category, _ = classify("0x1111111111111111111111111111111111111111", False, trades, 0, start)
        self.assertEqual("高频套利地址", category)

    def test_unfunded_early_seller_is_only_suspicious_not_asserted(self):
        trades = [Trade("0x1", 1, 1, "SELL", 100, 1000, datetime(2026, 4, 25, tzinfo=timezone.utc))]
        category, reasons = classify("0x1111111111111111111111111111111111111111", False, trades, 1, None)
        self.assertEqual("关联/老鼠仓高疑似", category)
        self.assertGreaterEqual(len(reasons), 2)


if __name__ == "__main__":
    unittest.main()
