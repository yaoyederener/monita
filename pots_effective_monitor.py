#!/usr/bin/env python3
"""POTS solvency-first monitor using only visible USDT assets.

Every run compares the current confirmed block with the immediately previous
observation. It intentionally avoids 24h/7d windows and ordinary trade alerts.
"""

from __future__ import annotations

import html
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import requests

from usdt_balance_monitor import (
    CurrentSnapshot,
    connect_web3,
    fmt_amount,
    fmt_signed_amount,
    read_current_snapshot,
    token_amount,
)


STATE_FILE = Path(os.getenv("POTS_EFFECTIVE_STATE_FILE", "data/pots_effective_state.json"))
TELEGRAM_TIMEOUT_SECONDS = int(os.getenv("TELEGRAM_TIMEOUT_SECONDS", "20"))
AMM_FEE_MULTIPLIER = Decimal(os.getenv("AMM_FEE_MULTIPLIER", "0.9975"))
REPORT_INTERVAL_MINUTES = int(os.getenv("REPORT_INTERVAL_MINUTES", "60"))
IMMEDIATE_TREASURY_CHANGE_USDT = Decimal(os.getenv("IMMEDIATE_TREASURY_CHANGE_USDT", "10000"))
IMMEDIATE_SUPPLY_CHANGE_IBS = Decimal(os.getenv("IMMEDIATE_SUPPLY_CHANGE_IBS", "1"))


@dataclass(frozen=True)
class IntervalChange:
    elapsed_seconds: int
    lp_delta_raw: int
    rbs_delta_raw: int
    safety_delta_raw: int
    treasury_delta_raw: int
    protocol_delta_raw: Optional[int]
    supply_delta_raw: int
    circulating_delta_raw: int


@dataclass(frozen=True)
class SolvencyMetrics:
    external_circulating_ibs: Decimal
    spot_liability_usdt: Decimal
    visible_backing_price: Decimal
    spot_coverage_ratio: Decimal
    fully_covered_ibs: Decimal
    fully_covered_ratio: Decimal
    spot_funding_gap_usdt: Decimal
    lp_full_exit_usdt: Decimal
    lp_full_exit_price: Decimal


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"schema_version": 1, "latest": None}
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    state.setdefault("schema_version", 1)
    state.setdefault("latest", None)
    return state


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    temp.replace(STATE_FILE)


def snapshot_record(current: CurrentSnapshot) -> dict[str, Any]:
    return {
        "timestamp_utc": current.observed_at.isoformat(),
        "block_number": current.block_number,
        "usdt_decimals": current.usdt_decimals,
        "ibs_decimals": current.ibs_decimals,
        "lp_usdt_raw": str(current.lp_usdt_raw),
        "rbs_usdt_raw": str(current.rbs_usdt_raw),
        "safety_usdt_raw": str(current.safety_usdt_raw),
        "treasury_usdt_raw": str(current.treasury_usdt_raw),
        "protocol_usdt_raw": str(current.protocol_usdt_raw),
        "protocol_config_hash": current.protocol_config_hash,
        "ibs_total_supply_raw": str(current.ibs_total_supply_raw),
        "ibs_circulating_raw": str(current.ibs_circulating_raw),
        "ibs_price_usdt": str(current.ibs_price_usdt),
        "backing_per_ibs": str(current.backing_per_ibs),
    }


def interval_change(previous: Optional[dict[str, Any]], current: CurrentSnapshot) -> Optional[IntervalChange]:
    if previous is None:
        return None
    previous_time = parse_time(previous.get("timestamp_utc"))
    if previous_time is None:
        return None
    if current.block_number < int(previous.get("block_number", 0)):
        raise RuntimeError("当前确认区块早于上次观察区块，本次不覆盖状态")
    elapsed_seconds = max(1, int((current.observed_at - previous_time).total_seconds()))
    same_boundary = previous.get("protocol_config_hash") == current.protocol_config_hash
    protocol_delta = (
        current.protocol_usdt_raw - int(previous["protocol_usdt_raw"])
        if same_boundary and previous.get("protocol_usdt_raw") is not None else None
    )
    return IntervalChange(
        elapsed_seconds=elapsed_seconds,
        lp_delta_raw=current.lp_usdt_raw - int(previous["lp_usdt_raw"]),
        rbs_delta_raw=current.rbs_usdt_raw - int(previous["rbs_usdt_raw"]),
        safety_delta_raw=current.safety_usdt_raw - int(previous["safety_usdt_raw"]),
        treasury_delta_raw=current.treasury_usdt_raw - int(previous["treasury_usdt_raw"]),
        protocol_delta_raw=protocol_delta,
        supply_delta_raw=current.ibs_total_supply_raw - int(previous["ibs_total_supply_raw"]),
        circulating_delta_raw=current.ibs_circulating_raw - int(previous["ibs_circulating_raw"]),
    )


def calculate_solvency(current: CurrentSnapshot) -> SolvencyMetrics:
    circulating = token_amount(current.ibs_circulating_raw, current.ibs_decimals)
    lp_ibs = token_amount(current.lp_ibs_raw, current.ibs_decimals)
    # IBS already held by the LP is inventory, not an external holder's sellable claim.
    external_circulating = max(Decimal(0), circulating - lp_ibs)
    treasury = token_amount(current.treasury_usdt_raw, current.usdt_decimals)
    spot_liability = external_circulating * current.ibs_price_usdt
    backing_price = treasury / circulating if circulating > 0 else Decimal(0)
    coverage = treasury / spot_liability if spot_liability > 0 else Decimal(0)
    covered_ibs = treasury / current.ibs_price_usdt if current.ibs_price_usdt > 0 else Decimal(0)
    covered_ratio = min(Decimal(1), covered_ibs / external_circulating) if external_circulating > 0 else Decimal(0)
    gap = max(Decimal(0), spot_liability - treasury)

    lp_usdt = token_amount(current.lp_usdt_raw, current.usdt_decimals)
    effective_input = external_circulating * AMM_FEE_MULTIPLIER
    full_exit_usdt = (
        lp_usdt * effective_input / (lp_ibs + effective_input)
        if lp_ibs + effective_input > 0 else Decimal(0)
    )
    full_exit_price = full_exit_usdt / external_circulating if external_circulating > 0 else Decimal(0)
    return SolvencyMetrics(
        external_circulating_ibs=external_circulating,
        spot_liability_usdt=spot_liability,
        visible_backing_price=backing_price,
        spot_coverage_ratio=coverage,
        fully_covered_ibs=covered_ibs,
        fully_covered_ratio=covered_ratio,
        spot_funding_gap_usdt=gap,
        lp_full_exit_usdt=full_exit_usdt,
        lp_full_exit_price=full_exit_price,
    )


def report_due(previous: Optional[dict[str, Any]], current: CurrentSnapshot, change: Optional[IntervalChange]) -> tuple[bool, str]:
    if previous is None or change is None:
        return True, "建立基线"
    if change.rbs_delta_raw != 0:
        return True, "RBS资金库发生变化"
    if change.safety_delta_raw != 0:
        return True, "Safety资金库发生变化"
    treasury_threshold_raw = int(IMMEDIATE_TREASURY_CHANGE_USDT * (Decimal(10) ** current.usdt_decimals))
    if abs(change.treasury_delta_raw) >= treasury_threshold_raw:
        return True, "国库变化达到即时门槛"
    supply_threshold_raw = int(IMMEDIATE_SUPPLY_CHANGE_IBS * (Decimal(10) ** current.ibs_decimals))
    if abs(change.supply_delta_raw) >= supply_threshold_raw:
        return True, "IBS供给发生重要变化"
    if change.elapsed_seconds >= REPORT_INTERVAL_MINUTES * 60:
        return True, "定时报告"
    return False, "尚未到报告时间且没有重要变化"


def daily_outflow_raw(delta_raw: int, elapsed_seconds: int) -> Optional[Decimal]:
    if delta_raw >= 0 or elapsed_seconds <= 0:
        return None
    return Decimal(-delta_raw) * Decimal(86400) / Decimal(elapsed_seconds)


def runway_days(current_raw: int, delta_raw: int, elapsed_seconds: int) -> Optional[Decimal]:
    daily = daily_outflow_raw(delta_raw, elapsed_seconds)
    if daily is None or daily <= 0:
        return None
    return Decimal(current_raw) / daily


def fmt_interval(seconds: int) -> str:
    minutes = Decimal(seconds) / Decimal(60)
    return f"{minutes:.1f}分钟" if minutes < 120 else f"{minutes / 60:.1f}小时"


def fmt_runway(days: Optional[Decimal], delta_raw: int) -> str:
    if delta_raw >= 0:
        return "本次未减少"
    if days is None:
        return "无法估算"
    if days > 999:
        return ">999天"
    return f"约{days:.1f}天"


def fmt_change(delta_raw: int, decimals: int) -> str:
    icon = "增加" if delta_raw > 0 else "减少" if delta_raw < 0 else "不变"
    return f"{icon} {fmt_amount(abs(delta_raw), decimals)}"


def classify_risk(current: CurrentSnapshot, change: Optional[IntervalChange], solvency: SolvencyMetrics) -> tuple[str, list[str]]:
    if change is None:
        return "🔵 建立即时基线", ["从下一次运行开始显示每次变化"]
    reasons: list[str] = []
    treasury_runway = runway_days(current.treasury_usdt_raw, change.treasury_delta_raw, change.elapsed_seconds)
    if change.treasury_delta_raw < 0:
        reasons.append("国库USDT减少")
    if change.rbs_delta_raw < 0:
        reasons.append("RBS资金库减少")
    if change.supply_delta_raw > 0:
        reasons.append("IBS继续增发")
    if change.treasury_delta_raw < 0 and change.supply_delta_raw > 0:
        reasons.append("资金减少与增发同时发生")
    if solvency.spot_coverage_ratio < Decimal("0.25"):
        reasons.append("按池内现价的USDT覆盖不足25%")
    if treasury_runway is not None and treasury_runway <= 7:
        reasons.append("按本次速度国库可支撑不足7天")
    if "资金减少与增发同时发生" in reasons or (treasury_runway is not None and treasury_runway <= 7):
        return "🔴 高风险", reasons
    if change.treasury_delta_raw < 0 or change.rbs_delta_raw < 0 or change.supply_delta_raw > 0:
        return "🟠 资金承压", reasons
    if change.treasury_delta_raw > 0 and change.supply_delta_raw <= 0:
        return "🟢 资金改善", ["国库USDT增加且IBS未增发"]
    return "🟡 暂时稳定", reasons or ["本次核心资金与IBS总量基本不变"]


def build_message(current: CurrentSnapshot, change: Optional[IntervalChange]) -> str:
    solvency = calculate_solvency(current)
    risk, reasons = classify_risk(current, change, solvency)
    udec, idec = current.usdt_decimals, current.ibs_decimals
    if change is None:
        delta_lp = delta_rbs = delta_safety = delta_treasury = "等待下一次"
        interval = "基线"
        issuance = "等待下一次"
        treasury_runway = rbs_runway = "等待下一次"
        daily_treasury = "等待下一次"
        protocol_flow = "等待下一次"
    else:
        interval = fmt_interval(change.elapsed_seconds)
        delta_lp = fmt_change(change.lp_delta_raw, udec)
        delta_rbs = fmt_change(change.rbs_delta_raw, udec)
        delta_safety = fmt_change(change.safety_delta_raw, udec)
        delta_treasury = fmt_change(change.treasury_delta_raw, udec)
        issuance_ibs = token_amount(change.supply_delta_raw, idec)
        issuance_value = issuance_ibs * current.ibs_price_usdt
        issuance = f"{issuance_ibs:+,.4f} IBS（现价约 {issuance_value:+,.2f} USDT）"
        tr = runway_days(current.treasury_usdt_raw, change.treasury_delta_raw, change.elapsed_seconds)
        rr = runway_days(current.rbs_usdt_raw, change.rbs_delta_raw, change.elapsed_seconds)
        treasury_runway = fmt_runway(tr, change.treasury_delta_raw)
        rbs_runway = fmt_runway(rr, change.rbs_delta_raw)
        daily = daily_outflow_raw(change.treasury_delta_raw, change.elapsed_seconds)
        daily_treasury = "本次没有净消耗" if daily is None else f"约 {token_amount(int(daily), udec):,.2f} USDT/天"
        protocol_flow = "地址边界变化，重新建立基线" if change.protocol_delta_raw is None else fmt_signed_amount(change.protocol_delta_raw, udec)

    reasons_text = "；".join(html.escape(x) for x in reasons)
    coverage_pct = solvency.spot_coverage_ratio * 100
    covered_pct = solvency.fully_covered_ratio * 100
    lines = [
        f"<b>{risk}｜POTS有效资金监控</b>",
        f"与上次报告间隔：{interval} ｜ 依据：{reasons_text}",
        "",
        "💵 <b>只看USDT资产</b>",
        f"LP：<b>{fmt_amount(current.lp_usdt_raw, udec)} USDT</b> ｜ 本次{delta_lp}",
        f"RBS：<b>{fmt_amount(current.rbs_usdt_raw, udec)} USDT</b> ｜ 本次{delta_rbs}",
        f"Safety：<b>{fmt_amount(current.safety_usdt_raw, udec)} USDT</b> ｜ 本次{delta_safety}",
        f"国库合计：<b>{fmt_amount(current.treasury_usdt_raw, udec)} USDT</b> ｜ 本次{delta_treasury}",
        f"已知协议边界净变化：{protocol_flow} USDT",
        "",
        "⏳ <b>按本次减少速度静态外推</b>",
        f"国库消耗速度：{daily_treasury} ｜ 可支撑 {treasury_runway}",
        f"RBS可支撑：{rbs_runway}",
        "",
        "🪙 <b>IBS供给与真实退出价格</b>",
        f"总量：{fmt_amount(current.ibs_total_supply_raw, idec, 4)} IBS",
        f"流通量代理：{fmt_amount(current.ibs_circulating_raw, idec, 4)} IBS",
        f"池外可退出量：{solvency.external_circulating_ibs:,.4f} IBS",
        f"本次增发：<b>{issuance}</b>",
        f"池内小额现价：<b>{current.ibs_price_usdt:,.4f} USDT/IBS</b>",
        f"全部池外IBS一次卖入现有LP：理论取出 <b>{solvency.lp_full_exit_usdt:,.2f} USDT</b>",
        f"对应平均清算价：<b>{solvency.lp_full_exit_price:,.4f} USDT/IBS</b>",
        "",
        "🛡 <b>USDT偿付能力</b>",
        f"每枚IBS可见USDT支撑：<b>{solvency.visible_backing_price:,.4f} USDT</b>",
        f"按当前池价计价的覆盖率：<b>{coverage_pct:.2f}%</b>",
        f"按现价可完全覆盖：{solvency.fully_covered_ibs:,.2f} IBS（{covered_pct:.2f}%池外流通量）",
        f"按现价完全兑付资金缺口：<b>{solvency.spot_funding_gap_usdt:,.2f} USDT</b>",
        "",
        "说明：池内现价只适合小额成交；平均清算价按池外IBS全部卖入恒定乘积AMM估算。国库合计仅含LP、RBS、Safety里的USDT，不把IBS自身当资产。可支撑天数是假设本次报告间隔内的消耗速度持续，不是保证。",
        f"确认区块：<code>{current.block_number}</code>",
    ]
    if current.lp_balance_usdt_raw != current.lp_usdt_raw:
        lines.append(f"⚠️ LP代币余额与储备相差 {fmt_signed_amount(current.lp_balance_usdt_raw - current.lp_usdt_raw, udec)} USDT")
    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, message: str) -> None:
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=TELEGRAM_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    if not response.json().get("ok"):
        raise RuntimeError(f"Telegram发送失败：{response.text}")


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量 {name}")
    return value


def main() -> None:
    rpc_url = require_env("BSC_RPC")
    token = require_env("BOT_TOKEN")
    chat_id = require_env("CHAT_ID")
    current = read_current_snapshot(connect_web3(rpc_url))
    state = load_state()
    change = interval_change(state.get("latest"), current)
    due, trigger = report_due(state.get("latest"), current, change)
    if not due:
        print(f"有效资金监控：{trigger}；区块{current.block_number}")
        return
    send_telegram(token, chat_id, build_message(current, change))
    state["latest"] = snapshot_record(current)
    state["updated_at_utc"] = now_utc().isoformat()
    save_state(state)
    print(f"有效资金监控成功：区块{current.block_number}，触发={trigger}，变化区间={'基线' if change is None else fmt_interval(change.elapsed_seconds)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"POTS有效资金监控失败：{exc}", flush=True)
        raise
