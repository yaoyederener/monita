import unittest
from datetime import datetime, timezone

import ibs_high_frequency_stats as stats


class HighFrequencyStatsTests(unittest.TestCase):
    def setUp(self):
        self.old_values = (
            stats.MIN_TOTAL_TRADES,
            stats.MIN_SIDE_TRADES,
            stats.MIN_ONE_WAY_TRADES,
        )
        stats.MIN_TOTAL_TRADES = 6
        stats.MIN_SIDE_TRADES = 2
        stats.MIN_ONE_WAY_TRADES = 5

    def tearDown(self):
        stats.MIN_TOTAL_TRADES, stats.MIN_SIDE_TRADES, stats.MIN_ONE_WAY_TRADES = self.old_values

    @staticmethod
    def event(index, address, side, ibs, usdt, timestamp):
        return {
            "id": f"0x{index}:0", "address": address, "side": side,
            "ibs_raw": str(ibs), "usdt_raw": str(usdt),
            "timestamp": timestamp, "tx_hash": f"0x{index}",
            "block_number": index, "log_index": 0,
        }

    def test_aggregate_calculates_real_lp_net_flow(self):
        address = "0x1111111111111111111111111111111111111111"
        events = [
            self.event(1, address, "BUY", 100, 1000, 100),
            self.event(2, address, "SELL", 120, 1300, 220),
        ]
        row = stats.aggregate_addresses(events)[0]
        self.assertEqual(300, row["net_sell_usdt_raw"])
        self.assertEqual(2, row["total_count"])
        self.assertEqual(2, row["avg_interval_minutes"])

    def test_balanced_fast_trader_is_double_sided_turnover(self):
        address = "0x1111111111111111111111111111111111111111"
        events = []
        for index in range(6):
            side = "BUY" if index % 2 == 0 else "SELL"
            events.append(self.event(index + 1, address, side, 100, 1000, 100 + index * 600))
        row = stats.aggregate_addresses(events)[0]
        self.assertEqual("🔄 高频双向周转", stats.frequency_label(row))

    def test_one_way_seller_is_kept_in_ranking(self):
        address = "0x1111111111111111111111111111111111111111"
        events = [self.event(i, address, "SELL", 100, 1000, 100 + i) for i in range(1, 6)]
        row = stats.aggregate_addresses(events)[0]
        self.assertEqual("🔴 高频卖出", stats.frequency_label(row))
        self.assertEqual("🔴 高频单向卖出", stats.suspicion_label(row))

    def test_high_frequency_net_buyer_is_not_an_abnormal_sell_account(self):
        address = "0x1111111111111111111111111111111111111111"
        events = []
        for index in range(6):
            side = "BUY" if index % 2 == 0 else "SELL"
            usdt = 1100 if side == "BUY" else 1000
            events.append(self.event(index + 1, address, side, 100, usdt, 100 + index * 60))
        row = stats.aggregate_addresses(events)[0]
        self.assertIsNone(stats.suspicion_label(row))

    def test_trim_removes_old_and_duplicate_events(self):
        address = "0x1111111111111111111111111111111111111111"
        now = 100000
        old = self.event(1, address, "BUY", 1, 1, now - 25 * 3600)
        current = self.event(2, address, "SELL", 1, 1, now - 60)
        result = stats.trim_events([old, current, dict(current)], now, 24)
        self.assertEqual(["0x2:0"], [x["id"] for x in result])

    def test_summary_explains_frequency_is_not_arbitrage(self):
        address = "0x1111111111111111111111111111111111111111"
        events = []
        for index in range(6):
            side = "BUY" if index % 2 == 0 else "SELL"
            usdt = 24 * 10**18 if side == "BUY" else 25 * 10**18
            events.append(self.event(index + 1, address, side, 10**18, usdt, 100 + index * 60))
        all_rows = stats.aggregate_addresses(events)
        high_rows = stats.qualifying_rows(all_rows)
        message = stats.build_summary(high_rows, all_rows, 18, 18, int(datetime.now(timezone.utc).timestamp()))
        self.assertIn("异常账户统计", message)
        self.assertIn("需深度画像确认", message)
        self.assertIn("交易 6 笔", message)


if __name__ == "__main__":
    unittest.main()
