import unittest
from datetime import datetime, timedelta, timezone

from ibs_wallet_profiler import Trade, anomaly_alert_due, anomaly_reasons, classify, compact_pending_by_address, fifo_holding_hours, round_trip_metrics


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

    def test_ordinary_seller_does_not_trigger_anomaly_alert(self):
        profile = {
            "category": "普通交易地址", "buy_count": 1, "sell_count": 1,
            "buy_usdt_raw": 1000, "sell_usdt_raw": 1100,
            "protocol_sold_est_raw": 0, "protocol_sale_proceeds_est": 0,
            "external_sold_est_raw": 0, "unpriced_sale_proceeds": 0,
        }
        self.assertEqual([], anomaly_reasons(profile, 18, 18))

    def test_high_frequency_net_seller_is_anomaly(self):
        scale = 10**18
        profile = {
            "category": "高频交易地址（未证实套利）", "buy_count": 10, "sell_count": 12,
            "buy_usdt_raw": 10_000 * scale, "sell_usdt_raw": 12_000 * scale,
            "protocol_sold_est_raw": 0, "protocol_sale_proceeds_est": 0,
            "external_sold_est_raw": 0, "unpriced_sale_proceeds": 0,
        }
        reasons = anomaly_reasons(profile, 18, 18)
        self.assertTrue(any("净USDT流出" in reason for reason in reasons))
        self.assertTrue(anomaly_alert_due(profile, None, reasons, datetime.now(timezone.utc)))

    def test_pending_sales_are_compacted_to_one_profile_per_address(self):
        address = "0x1111111111111111111111111111111111111111"
        pending = [
            {"event_id": "a", "address": address, "block_number": 1, "log_index": 0, "tx_hash": "0xa", "ibs_raw": "1", "usdt_raw": "1"},
            {"event_id": "b", "address": address, "block_number": 2, "log_index": 0, "tx_hash": "0xb", "ibs_raw": "2", "usdt_raw": "2"},
        ]
        compacted = compact_pending_by_address(pending)
        self.assertEqual(1, len(compacted))
        self.assertEqual("0xb", compacted[0]["tx_hash"])
        self.assertEqual(["a", "b"], compacted[0]["event_ids"])


if __name__ == "__main__":
    unittest.main()
