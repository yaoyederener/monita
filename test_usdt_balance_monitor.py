import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import usdt_balance_monitor as monitor


USDT_SCALE = 10**18
IBS_SCALE = 10**18


def swap_data(amount0_in, amount1_in, amount0_out, amount1_out):
    return b"".join(
        int(value).to_bytes(32, byteorder="big")
        for value in (amount0_in, amount1_in, amount0_out, amount1_out)
    )


def make_snapshot(
    when,
    block,
    lp=10_000_000,
    lp_ibs=500_000,
    lp_balance=None,
    rbs=1_500_000,
    safety=200_000,
    protocol=None,
    supply=1_000_000,
    backing=monitor.Decimal("11.7"),
    price=monitor.Decimal("20"),
    config_hash="config-a",
):
    lp_balance = lp if lp_balance is None else lp_balance
    protocol = lp_balance + rbs + safety if protocol is None else protocol
    return monitor.CurrentSnapshot(
        observed_at=when,
        block_time_utc=when.isoformat(),
        block_number=block,
        usdt_decimals=18,
        ibs_decimals=18,
        lp_usdt_raw=lp * USDT_SCALE,
        lp_ibs_raw=lp_ibs * IBS_SCALE,
        lp_balance_usdt_raw=lp_balance * USDT_SCALE,
        rbs_usdt_raw=rbs * USDT_SCALE,
        safety_usdt_raw=safety * USDT_SCALE,
        treasury_usdt_raw=(lp + rbs + safety) * USDT_SCALE,
        total_usdt_raw=(lp + rbs) * USDT_SCALE,
        protocol_usdt_raw=protocol * USDT_SCALE,
        ibs_total_supply_raw=supply * IBS_SCALE,
        ibs_dead_raw=0,
        ibs_circulating_raw=supply * IBS_SCALE,
        ibs_is_token0=True,
        ibs_price_usdt=price,
        market_cap_usdt=price * monitor.Decimal(supply),
        backing_per_ibs=backing,
        treasury_address="0xac739056e611d639aBEb0B9a87Da38Bd297Ba00E",
        tax_treasury_address="0xD9F49Fa9d8376041093DDA76Baaf02c3221f8702",
        protocol_addresses=("0x01", "0x02"),
        protocol_config_hash=config_hash,
    )


def make_metrics(
    rbs_delta,
    elapsed_days,
    total_change=0,
    backing_change=0,
    external=0,
    treasury_delta=None,
    supply_delta=0,
    start_block=100,
    end_block=200,
):
    treasury_delta_raw = (
        int(total_change * 1_000_000)
        if treasury_delta is None
        else treasury_delta * USDT_SCALE
    )
    treasury_change = (
        monitor.Decimal(str(total_change))
        if treasury_delta is None
        else monitor.Decimal(str(treasury_delta / 12_000_000))
    )
    return monitor.WindowMetrics(
        label="test",
        elapsed_days=monitor.Decimal(str(elapsed_days)),
        total_delta_raw=int(total_change * 1_000_000),
        total_change=monitor.Decimal(str(total_change)),
        treasury_delta_raw=treasury_delta_raw,
        treasury_change=treasury_change,
        rbs_delta_raw=rbs_delta * USDT_SCALE,
        rbs_change=monitor.Decimal(str(rbs_delta / 1_500_000)),
        supply_delta_raw=supply_delta * IBS_SCALE,
        supply_change=monitor.Decimal("0"),
        backing_change=monitor.Decimal(str(backing_change)),
        external_net_raw=external,
        start_block=start_block,
        end_block=end_block,
    )


class MonitorTests(unittest.TestCase):
    def test_legacy_rbs_state_is_preserved_for_migration(self):
        legacy = {
            "wallet": monitor.RBS_ADDRESS,
            "raw_balance": "1558901876095307582181485",
            "block_number": 114301586,
            "updated_at_utc": "2026-08-06T06:00:39+00:00",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state_path.write_text(json.dumps(legacy), encoding="utf-8")
            with patch.object(monitor, "STATE_FILE", state_path):
                loaded = monitor.load_state()
        self.assertIsNone(loaded["latest"])
        self.assertEqual(loaded["legacy_rbs_raw_balance"], legacy["raw_balance"])

    def test_protocol_balance_delta_excludes_internal_transfer(self):
        now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        previous = make_snapshot(now - timedelta(hours=1), 100)
        current = make_snapshot(
            now,
            200,
            lp=10_000_000,
            lp_balance=10_000_500,
            rbs=1_499_700,
            safety=200_300,
        )
        previous_record = monitor.record_from_snapshot(
            previous, monitor.FlowSummary(100, 100, False)
        )
        flow = monitor.summarize_interval(previous_record, current)
        self.assertTrue(flow.complete)
        self.assertEqual(flow.external_net_raw, 500 * USDT_SCALE)

    def test_lp_core_uses_reserve_but_perimeter_uses_token_balance(self):
        now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        snapshot = make_snapshot(now, 100, lp=10_000_000, lp_balance=10_000_123)
        self.assertEqual(snapshot.total_usdt_raw, 11_500_000 * USDT_SCALE)
        self.assertEqual(snapshot.treasury_usdt_raw, 11_700_000 * USDT_SCALE)
        self.assertEqual(snapshot.protocol_usdt_raw, 11_700_123 * USDT_SCALE)

    def test_market_data_uses_reserves_decimals_and_circulating_supply(self):
        price, market_cap = monitor.calculate_market_data(
            2_000_000 * 10**6,
            100_000 * IBS_SCALE,
            6,
            18,
            900_000 * IBS_SCALE,
        )
        self.assertEqual(price, monitor.Decimal("20"))
        self.assertEqual(market_cap, monitor.Decimal("18000000"))

    def test_market_data_rejects_empty_ibs_reserve(self):
        with self.assertRaisesRegex(RuntimeError, "IBS储备为0"):
            monitor.calculate_market_data(
                2_000_000 * USDT_SCALE, 0, 18, 18, 900_000 * IBS_SCALE
            )

    def test_protocol_config_change_rebuilds_flow_baseline(self):
        now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        previous = make_snapshot(now - timedelta(hours=1), 100, config_hash="old")
        current = make_snapshot(
            now, 200, protocol=12_700_000, config_hash="new"
        )
        previous_record = monitor.record_from_snapshot(
            previous, monitor.FlowSummary(100, 100, False)
        )
        flow = monitor.summarize_interval(previous_record, current)
        self.assertTrue(flow.config_changed)
        self.assertFalse(flow.complete)
        self.assertEqual(flow.external_net_raw, 0)
        self.assertEqual(monitor.report_due({"latest": previous_record}, current, flow)[1], "PROTOCOL_CONFIG_CHANGED")

    def test_large_rbs_balance_drop_is_critical(self):
        now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        previous = make_snapshot(now - timedelta(minutes=5), 100)
        current = make_snapshot(
            now,
            200,
            rbs=1_350_000,
            protocol=11_550_000,
        )
        previous_record = monitor.record_from_snapshot(
            previous, monitor.FlowSummary(100, 100, False)
        )
        with patch.object(monitor, "CRITICAL_OUTFLOW_USDT", monitor.Decimal("100000")):
            flow = monitor.summarize_interval(previous_record, current)
        self.assertEqual(len(flow.critical_events), 1)
        self.assertEqual(flow.critical_events[0]["name"], "RBS")
        self.assertEqual(flow.external_net_raw, -150_000 * USDT_SCALE)

    def test_24h_window_uses_combined_and_protocol_balances(self):
        end = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        start_snapshot = make_snapshot(
            end - timedelta(hours=24), 100, lp=10_000_000, rbs=1_500_000
        )
        end_snapshot = make_snapshot(
            end, 200, lp=10_100_000, rbs=1_450_000
        )
        start_record = monitor.record_from_snapshot(
            start_snapshot, monitor.FlowSummary(100, 100, False)
        )
        end_record = monitor.record_from_snapshot(
            end_snapshot, monitor.FlowSummary(101, 200, True)
        )
        metrics = monitor.calculate_window(
            [start_record, end_record], end_record, "24h", timedelta(hours=24)
        )
        self.assertIsNotNone(metrics)
        self.assertEqual(metrics.total_delta_raw, 50_000 * USDT_SCALE)
        self.assertEqual(metrics.external_net_raw, 50_000 * USDT_SCALE)
        self.assertEqual(metrics.rbs_delta_raw, -50_000 * USDT_SCALE)

    def test_window_adds_safety_to_treasury_without_changing_legacy_total(self):
        end = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        start = make_snapshot(end - timedelta(hours=24), 100, safety=200_000)
        current = make_snapshot(end, 200, safety=250_000)
        start_record = monitor.record_from_snapshot(
            start, monitor.FlowSummary(100, 100, False)
        )
        # Simulate a state record written before treasury_usdt_raw was introduced.
        start_record.pop("treasury_usdt_raw")
        current_record = monitor.record_from_snapshot(
            current, monitor.FlowSummary(101, 200, True)
        )
        metrics = monitor.calculate_window(
            [start_record, current_record], current_record, "24h", timedelta(hours=24)
        )
        self.assertIsNotNone(metrics)
        self.assertEqual(metrics.total_delta_raw, 0)
        self.assertEqual(metrics.treasury_delta_raw, 50_000 * USDT_SCALE)

    def test_window_rejects_stale_snapshot(self):
        end = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        stale = make_snapshot(end - timedelta(days=5), 100)
        current = make_snapshot(end, 200)
        stale_record = monitor.record_from_snapshot(
            stale, monitor.FlowSummary(100, 100, False)
        )
        current_record = monitor.record_from_snapshot(
            current, monitor.FlowSummary(101, 200, True)
        )
        metrics = monitor.calculate_window(
            [stale_record, current_record], current_record, "24h", timedelta(hours=24)
        )
        self.assertIsNone(metrics)

    def test_window_rejects_a_to_b_to_a_protocol_config_change(self):
        end = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        start = make_snapshot(end - timedelta(hours=24), 100, config_hash="config-a")
        middle = make_snapshot(
            end - timedelta(hours=12), 150, protocol=12_000_000, config_hash="config-b"
        )
        current = make_snapshot(end, 200, protocol=11_900_000, config_hash="config-a")
        records = [
            monitor.record_from_snapshot(start, monitor.FlowSummary(100, 100, False)),
            monitor.record_from_snapshot(
                middle, monitor.FlowSummary(101, 150, False, config_changed=True)
            ),
            monitor.record_from_snapshot(
                current, monitor.FlowSummary(151, 200, False, config_changed=True)
            ),
        ]
        metrics = monitor.calculate_window(
            records, records[-1], "24h", timedelta(hours=24)
        )
        self.assertIsNotNone(metrics)
        self.assertIsNone(metrics.external_net_raw)

    def test_zero_critical_threshold_disables_balance_drop_alert(self):
        now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        previous = make_snapshot(now - timedelta(minutes=5), 100)
        current = make_snapshot(now, 200, rbs=1_499_999, protocol=11_699_999)
        previous_record = monitor.record_from_snapshot(
            previous, monitor.FlowSummary(100, 100, False)
        )
        with patch.object(monitor, "CRITICAL_OUTFLOW_USDT", monitor.Decimal("0")):
            flow = monitor.summarize_interval(previous_record, current)
        self.assertEqual(flow.critical_events, [])

    def test_runway_uses_faster_24h_drawdown_even_if_7d_grew(self):
        metrics_24h = make_metrics(-100_000, 1)
        metrics_7d = make_metrics(70_000, 7)
        runway = monitor.estimate_rbs_runway(
            1_500_000 * USDT_SCALE, metrics_24h, metrics_7d
        )
        self.assertEqual(runway, monitor.Decimal("15"))

    def test_runway_uses_more_conservative_drawdown_rate(self):
        metrics_24h = make_metrics(-100_000, 1)
        metrics_7d = make_metrics(-350_000, 7)
        runway = monitor.estimate_rbs_runway(
            1_500_000 * USDT_SCALE, metrics_24h, metrics_7d
        )
        self.assertEqual(runway, monitor.Decimal("15"))

    def test_treasury_runway_uses_faster_recent_decline(self):
        metrics_24h = make_metrics(0, 1, treasury_delta=-200_000)
        metrics_7d = make_metrics(0, 7, treasury_delta=-700_000)
        runway = monitor.estimate_treasury_runway(
            12_000_000 * USDT_SCALE, metrics_24h, metrics_7d
        )
        self.assertEqual(runway, monitor.Decimal("60"))

    def test_sell_pressure_counts_only_net_ibs_sells_for_either_pair_order(self):
        token0_logs = [
            {"data": swap_data(100 * IBS_SCALE, 0, 0, 2_000 * USDT_SCALE)},
            {"data": swap_data(0, 1_000 * USDT_SCALE, 50 * IBS_SCALE, 0)},
            {
                "data": swap_data(
                    30 * IBS_SCALE,
                    20 * USDT_SCALE,
                    5 * IBS_SCALE,
                    620 * USDT_SCALE,
                )
            },
        ]
        sell_ibs, sell_usdt, count = monitor.summarize_sell_pressure_logs(
            token0_logs, True
        )
        self.assertEqual(sell_ibs, 125 * IBS_SCALE)
        self.assertEqual(sell_usdt, 2_600 * USDT_SCALE)
        self.assertEqual(count, 2)

        token1_logs = [
            {"data": swap_data(0, 40 * IBS_SCALE, 800 * USDT_SCALE, 0)}
        ]
        self.assertEqual(
            monitor.summarize_sell_pressure_logs(token1_logs, False),
            (40 * IBS_SCALE, 800 * USDT_SCALE, 1),
        )

    def test_safety_only_change_can_trigger_immediate_treasury_report(self):
        now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        previous = make_snapshot(now - timedelta(minutes=5), 100, safety=200_000)
        current = make_snapshot(now, 200, safety=350_000)
        previous_record = monitor.record_from_snapshot(
            previous, monitor.FlowSummary(100, 100, False)
        )
        flow = monitor.summarize_interval(previous_record, current)
        with patch.object(
            monitor, "IMMEDIATE_TOTAL_CHANGE_USDT", monitor.Decimal("100000")
        ):
            due, reason = monitor.report_due({"latest": previous_record}, current, flow)
        self.assertTrue(due)
        self.assertEqual(reason, "IMMEDIATE_TREASURY_CHANGE")

    def test_telegram_message_contains_new_and_existing_metrics(self):
        now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        snapshot = make_snapshot(now, 200)
        baseline = monitor.build_telegram_message(
            snapshot,
            None,
            None,
            monitor.FlowSummary(200, 200, False),
            "🔵 建立基线",
            ["等待至少24小时数据"],
            None,
            "BASELINE",
        )
        self.assertIn("当前国库资金", baseline)
        self.assertIn("Safety", baseline)
        self.assertIn("流通市值估算", baseline)
        self.assertIn("当前总量", baseline)
        self.assertIn("可见USDT覆盖", baseline)

        metrics_24h = make_metrics(
            -50_000,
            1,
            total_change=-0.01,
            backing_change=-0.01,
            external=-25_000 * USDT_SCALE,
            treasury_delta=-200_000,
            supply_delta=1_000,
        )
        pressure = monitor.SellPressure(
            101, 200, 2_000 * IBS_SCALE, 40_000 * USDT_SCALE, 12
        )
        message = monitor.build_telegram_message(
            snapshot,
            metrics_24h,
            None,
            monitor.FlowSummary(101, 200, True),
            "🟠 下行阶段",
            ["国库资金下降", "外部净流出"],
            monitor.Decimal("30"),
            "PERIODIC",
            treasury_runway_days=monitor.Decimal("60"),
            sell_pressure=pressure,
        )
        self.assertIn("国库静态可持续时间：约60.0天", message)
        self.assertIn("近24h净增发 +1,000.00 IBS", message)
        self.assertIn("近24h真实抛压 40,000.00 USDT", message)
        self.assertIn("LP+RBS 24h", message)
        self.assertIn("已知协议地址净流入", message)

    def test_growth_and_death_spiral_classification(self):
        growth_24 = make_metrics(15_000, 1, 0.01, 0.01, 1)
        growth_7 = make_metrics(45_000, 7, 0.05, 0.02, 1)
        self.assertEqual(
            monitor.classify_health(growth_24, growth_7, None)[0], "GROWTH"
        )

        bad_24 = make_metrics(-75_000, 1, -0.03, -0.02, -1)
        bad_7 = make_metrics(-300_000, 7, -0.12, -0.08, -1)
        self.assertEqual(
            monitor.classify_health(bad_24, bad_7, monitor.Decimal("6"))[0],
            "DEATH_SPIRAL_RISK",
        )

    def test_first_run_writes_new_state_without_sending_real_telegram(self):
        now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        snapshot = make_snapshot(now, 200)
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "data" / "usdt_balance.json"
            history_path = Path(temp_dir) / "data" / "usdt_flow_history.csv"
            with (
                patch.object(monitor, "STATE_FILE", state_path),
                patch.object(monitor, "HISTORY_FILE", history_path),
                patch.object(monitor, "connect_web3", return_value=object()),
                patch.object(monitor, "read_current_snapshot", return_value=snapshot),
                patch.object(monitor, "send_telegram") as send_telegram,
                patch.dict(
                    monitor.os.environ,
                    {"BSC_RPC": "https://example.invalid", "BOT_TOKEN": "x", "CHAT_ID": "y"},
                ),
            ):
                monitor.main()

            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["schema_version"], monitor.STATE_SCHEMA_VERSION)
            self.assertEqual(saved["latest"]["protocol_usdt_raw"], str(snapshot.protocol_usdt_raw))
            self.assertTrue(history_path.exists())
            send_telegram.assert_called_once()


if __name__ == "__main__":
    unittest.main()
