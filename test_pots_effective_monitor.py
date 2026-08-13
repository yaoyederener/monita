import unittest
from datetime import datetime, timedelta, timezone

import pots_effective_monitor as monitor
from test_usdt_balance_monitor import make_snapshot


USDT_SCALE = 10**18
IBS_SCALE = 10**18


class EffectiveMonitorTests(unittest.TestCase):
    def test_interval_uses_immediately_previous_observation(self):
        now = datetime(2026, 8, 13, 12, 5, tzinfo=timezone.utc)
        previous_snapshot = make_snapshot(now - timedelta(minutes=5), 100)
        previous = monitor.snapshot_record(previous_snapshot)
        current = make_snapshot(now, 200, lp=9_990_000, rbs=1_490_000, safety=205_000, supply=1_001_000)
        change = monitor.interval_change(previous, current)
        self.assertEqual(change.elapsed_seconds, 300)
        self.assertEqual(change.lp_delta_raw, -10_000 * USDT_SCALE)
        self.assertEqual(change.rbs_delta_raw, -10_000 * USDT_SCALE)
        self.assertEqual(change.safety_delta_raw, 5_000 * USDT_SCALE)
        self.assertEqual(change.treasury_delta_raw, -15_000 * USDT_SCALE)
        self.assertEqual(change.supply_delta_raw, 1_000 * IBS_SCALE)

    def test_interval_migrates_state_without_lp_ibs(self):
        now = datetime(2026, 8, 13, 12, 5, tzinfo=timezone.utc)
        previous = monitor.snapshot_record(make_snapshot(now - timedelta(minutes=5), 100))
        previous.pop("lp_ibs_raw")
        current = make_snapshot(now, 200, lp=9_990_000)
        change = monitor.interval_change(previous, current)
        self.assertEqual(change.lp_ibs_delta_raw, 0)

    def test_runway_annualizes_stable_short_trend(self):
        days = monitor.runway_days(1_000_000 * USDT_SCALE, -10_000 * USDT_SCALE, 6 * 3600)
        self.assertEqual(days, monitor.Decimal("25"))
        self.assertIsNone(monitor.runway_days(1_000_000, 1, 300))

    def test_solvency_separates_spot_from_full_exit_price(self):
        current = make_snapshot(
            datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc),
            200,
            lp=10_000_000,
            rbs=1_500_000,
            safety=500_000,
            supply=900_000,
            price=25,
        )
        metrics = monitor.calculate_solvency(current)
        self.assertEqual(metrics.external_circulating_ibs, monitor.Decimal("400000"))
        self.assertEqual(metrics.spot_liability_usdt, monitor.Decimal("10000000"))
        self.assertLess(metrics.lp_full_exit_price, current.ibs_price_usdt)
        self.assertEqual(metrics.visible_backing_price, monitor.Decimal("13.333333333333333333333333333333333333333333333333333333333333333333333333333333"))
        self.assertAlmostEqual(float(metrics.spot_coverage_ratio), 12_000_000 / 10_000_000)

    def test_report_is_immediate_for_large_backup_drop_but_not_lp_noise(self):
        now = datetime(2026, 8, 13, 12, 5, tzinfo=timezone.utc)
        previous = monitor.snapshot_record(make_snapshot(now - timedelta(minutes=5), 100))
        lp_only = make_snapshot(now, 200, lp=9_999_999)
        due, _ = monitor.report_due(
            previous,
            lp_only,
            monitor.interval_change(previous, lp_only),
            last_report_at=now - timedelta(minutes=5),
        )
        self.assertFalse(due)
        rbs_change = make_snapshot(now, 200, rbs=1_399_999)
        due, reason = monitor.report_due(previous, rbs_change, monitor.interval_change(previous, rbs_change))
        self.assertTrue(due)
        self.assertIn("RBS", reason)

    def test_missing_report_timestamp_sends_baseline_then_uses_natural_hour(self):
        now = datetime(2026, 8, 13, 12, 59, tzinfo=timezone.utc)
        previous = monitor.snapshot_record(make_snapshot(now - timedelta(minutes=5), 100))
        current = make_snapshot(now, 200)
        change = monitor.interval_change(previous, current)

        due, reason = monitor.report_due(previous, current, change, last_report_at=None)
        self.assertTrue(due)
        self.assertEqual(reason, "建立通知基线")

        due, _ = monitor.report_due(
            previous,
            current,
            change,
            last_report_at=now - timedelta(minutes=59),
        )
        self.assertFalse(due)
        due, reason = monitor.report_due(
            previous,
            current,
            change,
            last_report_at=now - timedelta(minutes=60),
        )
        self.assertTrue(due)
        self.assertEqual(reason, "定时报告")

        next_hour = make_snapshot(
            datetime(2026, 8, 13, 17, 0, tzinfo=timezone.utc),
            300,
        )
        next_change = monitor.interval_change(monitor.snapshot_record(current), next_hour)
        due, reason = monitor.report_due(
            monitor.snapshot_record(current),
            next_hour,
            next_change,
            last_report_at=datetime(2026, 8, 13, 16, 34, tzinfo=timezone.utc),
        )
        self.assertTrue(due)
        self.assertEqual(reason, "定时报告")

    def test_risk_is_high_when_treasury_falls_while_supply_grows(self):
        now = datetime(2026, 8, 13, 12, 5, tzinfo=timezone.utc)
        previous = monitor.snapshot_record(make_snapshot(now - timedelta(minutes=5), 100))
        current = make_snapshot(now, 200, lp=9_900_000, supply=1_001_000)
        change = monitor.interval_change(previous, current)
        trend = monitor.ShortTrend(
            elapsed_seconds=1800,
            sample_intervals=6,
            lp_decrease_intervals=6,
            lp_delta_raw=-100_000 * USDT_SCALE,
            lp_delta_pct=monitor.Decimal("-1"),
            lp_ibs_delta_raw=4_000 * IBS_SCALE,
            rbs_delta_raw=0,
            safety_delta_raw=0,
            treasury_delta_raw=-100_000 * USDT_SCALE,
            supply_delta_raw=1_000 * IBS_SCALE,
        )
        label, reasons = monitor.classify_risk(current, change, trend)
        self.assertEqual(label, "🔴 高风险")
        self.assertIn("IBS增发同时LP资金减少", reasons)

    def test_message_is_simple_usdt_only(self):
        now = datetime(2026, 8, 13, 12, 5, tzinfo=timezone.utc)
        previous = monitor.snapshot_record(make_snapshot(now - timedelta(minutes=5), 100))
        current = make_snapshot(now, 200, lp=9_990_000, supply=1_001_000)
        observations = []
        for index in range(6):
            snap = make_snapshot(
                now - timedelta(minutes=30 - index * 5),
                100 + index,
                lp=10_020_000 - index * 5_000,
            )
            observations.append(monitor.snapshot_record(snap))
        trend = monitor.calculate_short_trend(observations, current)
        message = monitor.build_message(current, monitor.interval_change(previous, current), trend)
        self.assertIn("POTS资金安全监控", message)
        self.assertIn("可以退出的资金", message)
        self.assertIn("全部可见USDT", message)
        self.assertIn("短期趋势", message)
        self.assertNotIn("全部池外IBS", message)
        self.assertNotIn("清算价", message)
        self.assertNotIn("覆盖率", message)
        self.assertNotIn("24h", message)
        self.assertNotIn("大额买卖", message)
        self.assertNotIn("毛卖压", message)

    def test_short_trend_counts_each_observation_and_uses_30_minutes(self):
        now = datetime(2026, 8, 13, 12, 30, tzinfo=timezone.utc)
        records = []
        for index in range(6):
            snap = make_snapshot(
                now - timedelta(minutes=30 - index * 5),
                100 + index,
                lp=10_000_000 - index * 10_000,
            )
            records.append(monitor.snapshot_record(snap))
        current = make_snapshot(now, 200, lp=9_940_000)
        trend = monitor.calculate_short_trend(records, current)
        self.assertIsNotNone(trend)
        self.assertEqual(trend.sample_intervals, 6)
        self.assertEqual(trend.lp_decrease_intervals, 6)
        self.assertEqual(trend.lp_delta_raw, -60_000 * USDT_SCALE)

    def test_liquidity_removal_requires_both_reserves_to_fall_similarly(self):
        now = datetime(2026, 8, 13, 12, 30, tzinfo=timezone.utc)
        current = make_snapshot(now, 200, lp=9_000_000)
        current = current.__class__(**{**current.__dict__, "lp_ibs_raw": 450_000 * IBS_SCALE})
        trend = monitor.ShortTrend(
            elapsed_seconds=1800,
            sample_intervals=6,
            lp_decrease_intervals=6,
            lp_delta_raw=-1_000_000 * USDT_SCALE,
            lp_delta_pct=monitor.Decimal("-10"),
            lp_ibs_delta_raw=-50_000 * IBS_SCALE,
            rbs_delta_raw=0,
            safety_delta_raw=0,
            treasury_delta_raw=-1_000_000 * USDT_SCALE,
            supply_delta_raw=0,
        )
        self.assertTrue(monitor.liquidity_removal_suspected(trend, current))


if __name__ == "__main__":
    unittest.main()
