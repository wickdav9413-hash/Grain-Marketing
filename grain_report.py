"""
Daily Grain & Livestock Market Report.

Fetches recent prices for corn, soybeans, wheat, milk, live cattle (beef),
lean hogs, and sheep/lamb, computes technical indicators (SMA, RSI, MACD,
Bollinger Bands), generates buy/sell signals with confidence levels and
1-/7-day forecasts, then emails the report.

Data sources (in priority order):
  1. Yahoo Finance chart API via requests (with cookie/crumb auth)
  2. yfinance library (fallback)
  3. Yahoo Finance CSV download endpoint (last resort)

Email: SMTP (Gmail app password expected).

Environment variables (all required unless noted):
    SMTP_HOST        default: smtp.gmail.com
    SMTP_PORT        default: 587
    SMTP_USERNAME    Gmail address that sends the report
    SMTP_PASSWORD    Gmail App Password (NOT the normal account password)
    EMAIL_FROM       default: SMTP_USERNAME
    EMAIL_TO         default: wickdav9413@gmail.com

This script is NOT financial advice. Signals are mechanical and based on
technical indicator rules. Always do your own research.
"""
from __future__ import annotations

import io
import os
import smtplib
import ssl
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import requests as _requests

INSTRUMENTS: list[tuple[str, str, str]] = [
    ("Corn", "ZC=F", "USD / bushel"),
    ("Soybeans", "ZS=F", "USD / bushel"),
    ("Wheat", "ZW=F", "USD / bushel"),
    ("Class III Milk", "DC=F", "USD / cwt"),
    ("Live Cattle (Beef)", "LE=F", "USD / lb"),
    ("Lean Hogs", "HE=F", "USD / lb"),
    ("Sheep/Lamb (proxy: Livestock ETF COW)", "COW", "USD / share"),
]


@dataclass
class InstrumentReport:
    name: str
    symbol: str
    unit: str
    last: Optional[float]
    prev_close: Optional[float]
    change_pct_1d: Optional[float]
    change_pct_7d: Optional[float]
    change_pct_30d: Optional[float]
    sma20: Optional[float]
    sma50: Optional[float]
    rsi14: Optional[float]
    macd_line: Optional[float]
    macd_signal: Optional[float]
    macd_histogram: Optional[float]
    bb_upper: Optional[float]
    bb_middle: Optional[float]
    bb_lower: Optional[float]
    trend: str
    signal: str
    confidence: str
    rationale: str
    forecast_1d: Optional[float]
    forecast_7d: Optional[float]
    forecast_1d_pct: Optional[float]
    forecast_7d_pct: Optional[float]
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Yahoo Finance authenticated session (cookie + crumb).
# ---------------------------------------------------------------------------
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

_yahoo_session: Optional[_requests.Session] = None
_yahoo_crumb: Optional[str] = None


def _init_yahoo_session() -> tuple[_requests.Session, str]:
    global _yahoo_session, _yahoo_crumb
    if _yahoo_session is not None and _yahoo_crumb is not None:
        return _yahoo_session, _yahoo_crumb

    session = _requests.Session()
    session.headers.update(_HEADERS)

    for attempt in range(3):
        try:
            session.get("https://fc.yahoo.com", timeout=10, allow_redirects=True)
            crumb_resp = session.get(
                "https://query2.finance.yahoo.com/v1/test/getcrumb",
                timeout=10,
            )
            crumb_resp.raise_for_status()
            crumb = crumb_resp.text.strip()
            if not crumb:
                raise RuntimeError("empty crumb")
            _yahoo_session = session
            _yahoo_crumb = crumb
            return session, crumb
        except Exception as exc:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"Yahoo session init failed after 3 attempts: {exc}") from exc
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------------------
# Data fetching — multiple strategies for resilience.
# ---------------------------------------------------------------------------
def _fetch_yahoo_chart(symbol: str, range_str: str = "6mo", interval: str = "1d") -> pd.Series:
    session, crumb = _init_yahoo_session()
    url = (
        f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?range={range_str}&interval={interval}&includePrePost=false&crumb={crumb}"
    )
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            result = data["chart"]["result"][0]
            timestamps = result["timestamp"]
            closes = result["indicators"]["quote"][0]["close"]
            idx = pd.to_datetime(timestamps, unit="s", utc=True)
            series = pd.Series(closes, index=idx, name="Close", dtype=float).dropna()
            if series.empty:
                raise ValueError("empty close series")
            return series
        except Exception as exc:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"Yahoo chart API failed after 3 attempts: {exc}") from exc
    raise RuntimeError("unreachable")


def _fetch_yfinance(symbol: str) -> pd.Series:
    import yfinance as yf

    ticker = yf.Ticker(symbol)
    hist = ticker.history(period="6mo", interval="1d")
    if hist is None or hist.empty:
        raise RuntimeError("yfinance returned no data")
    if "Close" not in hist.columns:
        raise RuntimeError("yfinance returned no Close column")
    closes = hist["Close"].dropna()
    if closes.empty:
        raise RuntimeError("yfinance Close series is empty")
    return closes


def _fetch_yahoo_csv(symbol: str) -> pd.Series:
    session, crumb = _init_yahoo_session()
    now = int(time.time())
    period1 = now - (180 * 86400)
    url = (
        f"https://query2.finance.yahoo.com/v7/finance/download/{symbol}"
        f"?period1={period1}&period2={now}&interval=1d&events=history&crumb={crumb}"
    )
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=20)
            resp.raise_for_status()
            df = pd.read_csv(io.StringIO(resp.text), parse_dates=["Date"])
            if df.empty or "Close" not in df.columns:
                raise RuntimeError("CSV returned no Close data")
            df = df.set_index("Date").sort_index()
            closes = df["Close"].dropna()
            if closes.empty:
                raise RuntimeError("CSV Close series is empty")
            return closes
        except Exception as exc:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"Yahoo CSV download failed after 3 attempts: {exc}") from exc
    raise RuntimeError("unreachable")


def fetch_closes(symbol: str) -> pd.Series:
    errors = []
    for fetcher_name, fetcher in [
        ("yahoo_chart_api", _fetch_yahoo_chart),
        ("yfinance", _fetch_yfinance),
        ("yahoo_csv", _fetch_yahoo_csv),
    ]:
        try:
            series = fetcher(symbol)
            print(f"[{fetcher_name}]", end=" ", flush=True)
            return series
        except Exception as exc:
            errors.append(f"{fetcher_name}: {exc}")
    raise RuntimeError(" | ".join(errors))


# ---------------------------------------------------------------------------
# Indicators.
# ---------------------------------------------------------------------------
def _rsi(series: pd.Series, period: int = 14) -> Optional[float]:
    if len(series) < period + 1:
        return None
    delta = series.diff().dropna()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    avg_gain = gains.rolling(period).mean().iloc[-1]
    avg_loss = losses.rolling(period).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - (100 / (1 + rs)))


def _macd(
    series: pd.Series, fast: int = 12, slow: int = 26, signal_period: int = 9
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    if len(series) < slow + signal_period:
        return None, None, None
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal_period, adjust=False).mean()
    histogram = macd_line - signal_line
    return float(macd_line.iloc[-1]), float(signal_line.iloc[-1]), float(histogram.iloc[-1])


def _bollinger(
    series: pd.Series, period: int = 20, num_std: float = 2.0
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    if len(series) < period:
        return None, None, None
    rolling_mean = series.rolling(period).mean()
    rolling_std = series.rolling(period).std()
    upper = rolling_mean + num_std * rolling_std
    lower = rolling_mean - num_std * rolling_std
    return float(upper.iloc[-1]), float(rolling_mean.iloc[-1]), float(lower.iloc[-1])


def _pct_change(series: pd.Series, days: int) -> Optional[float]:
    if len(series) <= days:
        return None
    old = float(series.iloc[-1 - days])
    new = float(series.iloc[-1])
    if old == 0 or pd.isna(old):
        return None
    return float((new - old) / old * 100)


def _linear_forecast(series: pd.Series, horizon_days: int) -> Optional[float]:
    window = series.dropna().tail(20)
    if len(window) < 5:
        return None
    x = pd.Series(range(len(window)), index=window.index, dtype=float)
    x_mean = x.mean()
    y_mean = window.mean()
    denom = ((x - x_mean) ** 2).sum()
    if denom == 0:
        return None
    slope = ((x - x_mean) * (window - y_mean)).sum() / denom
    intercept = y_mean - slope * x_mean
    forecast_x = len(window) - 1 + horizon_days
    return float(intercept + slope * forecast_x)


# ---------------------------------------------------------------------------
# Analysis — multi-indicator confluence scoring.
# ---------------------------------------------------------------------------
def _derive_signal(
    last: float,
    sma20: Optional[float],
    sma50: Optional[float],
    rsi: Optional[float],
    macd_line: Optional[float],
    macd_signal: Optional[float],
    macd_histogram: Optional[float],
    bb_upper: Optional[float],
    bb_lower: Optional[float],
) -> tuple[str, str, str, str]:
    """Return (trend, signal, rationale, confidence) using multi-indicator scoring."""
    score = 0
    reasons: list[str] = []

    # SMA trend
    if sma20 is not None and sma50 is not None:
        if last > sma20 > sma50:
            trend = "Uptrend"
            score += 2
            reasons.append("price > SMA20 > SMA50 (uptrend)")
        elif last < sma20 < sma50:
            trend = "Downtrend"
            score -= 2
            reasons.append("price < SMA20 < SMA50 (downtrend)")
        elif last > sma50:
            trend = "Weak uptrend"
            score += 1
            reasons.append("price above SMA50")
        else:
            trend = "Weak downtrend"
            score -= 1
            reasons.append("price below SMA50")
    else:
        trend = "Unknown"

    # RSI
    if rsi is not None:
        if rsi < 30:
            score += 2
            reasons.append(f"RSI {rsi:.0f} (oversold)")
        elif rsi > 70:
            score -= 2
            reasons.append(f"RSI {rsi:.0f} (overbought)")
        elif rsi < 45:
            score += 1
            reasons.append(f"RSI {rsi:.0f} (leaning oversold)")
        elif rsi > 55:
            score -= 1
            reasons.append(f"RSI {rsi:.0f} (leaning overbought)")
        else:
            reasons.append(f"RSI {rsi:.0f} (neutral)")

    # MACD
    if macd_histogram is not None:
        if macd_histogram > 0:
            score += 1
            reasons.append("MACD bullish (histogram > 0)")
        else:
            score -= 1
            reasons.append("MACD bearish (histogram < 0)")

    # Bollinger Bands
    if bb_upper is not None and bb_lower is not None:
        if last <= bb_lower:
            score += 1
            reasons.append("price at/below lower Bollinger Band")
        elif last >= bb_upper:
            score -= 1
            reasons.append("price at/above upper Bollinger Band")

    # Signal mapping
    if score >= 4:
        signal = "STRONG BUY"
    elif score >= 2:
        signal = "BUY"
    elif score <= -4:
        signal = "STRONG SELL"
    elif score <= -2:
        signal = "SELL"
    else:
        signal = "HOLD"

    # Confidence
    abs_score = abs(score)
    if abs_score >= 4:
        confidence = "HIGH"
    elif abs_score >= 2:
        confidence = "MODERATE"
    else:
        confidence = "LOW"

    return trend, signal, "; ".join(reasons) if reasons else "insufficient data", confidence


def analyze(name: str, symbol: str, unit: str) -> InstrumentReport:
    blank = InstrumentReport(
        name=name, symbol=symbol, unit=unit,
        last=None, prev_close=None,
        change_pct_1d=None, change_pct_7d=None, change_pct_30d=None,
        sma20=None, sma50=None, rsi14=None,
        macd_line=None, macd_signal=None, macd_histogram=None,
        bb_upper=None, bb_middle=None, bb_lower=None,
        trend="Unknown", signal="HOLD", confidence="LOW",
        rationale="no data", forecast_1d=None, forecast_7d=None,
        forecast_1d_pct=None, forecast_7d_pct=None,
    )
    try:
        closes = fetch_closes(symbol)
    except Exception as exc:
        blank.error = f"fetch failed: {exc}"
        return blank

    if closes.empty:
        blank.error = "no close prices"
        return blank

    last = float(closes.iloc[-1])
    prev = float(closes.iloc[-2]) if len(closes) > 1 else None
    sma20 = float(closes.tail(20).mean()) if len(closes) >= 20 else None
    sma50 = float(closes.tail(50).mean()) if len(closes) >= 50 else None
    rsi14 = _rsi(closes)
    macd_l, macd_s, macd_h = _macd(closes)
    bb_upper, bb_middle, bb_lower = _bollinger(closes)

    trend, signal, rationale, confidence = _derive_signal(
        last, sma20, sma50, rsi14, macd_l, macd_s, macd_h, bb_upper, bb_lower
    )

    forecast_1d = _linear_forecast(closes, 1)
    forecast_7d = _linear_forecast(closes, 5)
    forecast_1d_pct = ((forecast_1d - last) / last * 100) if forecast_1d and last else None
    forecast_7d_pct = ((forecast_7d - last) / last * 100) if forecast_7d and last else None

    return InstrumentReport(
        name=name, symbol=symbol, unit=unit,
        last=last, prev_close=prev,
        change_pct_1d=_pct_change(closes, 1),
        change_pct_7d=_pct_change(closes, 5),
        change_pct_30d=_pct_change(closes, 21),
        sma20=sma20, sma50=sma50, rsi14=rsi14,
        macd_line=macd_l, macd_signal=macd_s, macd_histogram=macd_h,
        bb_upper=bb_upper, bb_middle=bb_middle, bb_lower=bb_lower,
        trend=trend, signal=signal, confidence=confidence, rationale=rationale,
        forecast_1d=forecast_1d, forecast_7d=forecast_7d,
        forecast_1d_pct=forecast_1d_pct, forecast_7d_pct=forecast_7d_pct,
    )


# ---------------------------------------------------------------------------
# Rendering.
# ---------------------------------------------------------------------------
def _fmt(value: Optional[float], places: int = 2, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    return f"{value:,.{places}f}{suffix}"


def _badge(signal: str) -> str:
    colour = {
        "STRONG BUY": "#0d6a3a",
        "BUY": "#1a7f37",
        "HOLD": "#6c6c6c",
        "SELL": "#b42318",
        "STRONG SELL": "#8b0000",
    }.get(signal, "#6c6c6c")
    return (
        f'<span style="background:{colour};color:#fff;padding:2px 8px;'
        f'border-radius:10px;font-weight:600;font-size:12px;">{signal}</span>'
    )


def _confidence_badge(confidence: str) -> str:
    colour = {"HIGH": "#0d6a3a", "MODERATE": "#b8860b", "LOW": "#6c6c6c"}.get(confidence, "#6c6c6c")
    return (
        f'<span style="background:{colour};color:#fff;padding:1px 6px;'
        f'border-radius:8px;font-size:11px;">{confidence}</span>'
    )


def _signal_emoji(signal: str) -> str:
    return {
        "STRONG BUY": "[STRONG BUY]",
        "BUY": "[BUY]",
        "HOLD": "[HOLD]",
        "SELL": "[SELL]",
        "STRONG SELL": "[STRONG SELL]",
    }.get(signal, "[?]")


def _forecast_html(current: Optional[float], forecast: Optional[float], pct: Optional[float]) -> str:
    if current is None or forecast is None or pct is None:
        return "n/a"
    if forecast > current:
        arrow = "&#9650;"
        color = "#1a7f37"
    elif forecast < current:
        arrow = "&#9660;"
        color = "#b42318"
    else:
        arrow = "&#9654;"
        color = "#6c6c6c"
    return f'<span style="color:{color};font-weight:600;">{arrow} {_fmt(forecast)} ({pct:+.1f}%)</span>'


def _forecast_text(current: Optional[float], forecast: Optional[float], pct: Optional[float]) -> str:
    if current is None or forecast is None or pct is None:
        return "n/a"
    if forecast > current:
        arrow = "^"
    elif forecast < current:
        arrow = "v"
    else:
        arrow = "->"
    return f"{arrow} {_fmt(forecast)} ({pct:+.1f}%)"


def _market_overview(reports: list[InstrumentReport]) -> tuple[str, str]:
    valid = [r for r in reports if not r.error]
    if not valid:
        msg = "All instruments failed to fetch. No market overview available."
        return f'<p style="color:#b42318;">{msg}</p>', msg

    signal_counts: dict[str, int] = {}
    for r in valid:
        signal_counts[r.signal] = signal_counts.get(r.signal, 0) + 1

    score_map = {"STRONG BUY": 2, "BUY": 1, "HOLD": 0, "SELL": -1, "STRONG SELL": -2}
    avg_score = sum(score_map.get(r.signal, 0) for r in valid) / len(valid)

    if avg_score > 0.5:
        sentiment = "Bullish"
        sent_color = "#1a7f37"
    elif avg_score > 0.1:
        sentiment = "Slightly Bullish"
        sent_color = "#2d8a4e"
    elif avg_score >= -0.1:
        sentiment = "Neutral"
        sent_color = "#6c6c6c"
    elif avg_score >= -0.5:
        sentiment = "Slightly Bearish"
        sent_color = "#c94a2e"
    else:
        sentiment = "Bearish"
        sent_color = "#b42318"

    avg_1d = [r.change_pct_1d for r in valid if r.change_pct_1d is not None]
    avg_1d_val = sum(avg_1d) / len(avg_1d) if avg_1d else None

    grains = [r for r in valid if r.symbol in ("ZC=F", "ZS=F", "ZW=F")]
    livestock = [r for r in valid if r.symbol in ("LE=F", "HE=F", "COW")]

    grain_avg = None
    if grains:
        grain_chgs = [r.change_pct_1d for r in grains if r.change_pct_1d is not None]
        grain_avg = sum(grain_chgs) / len(grain_chgs) if grain_chgs else None

    livestock_avg = None
    if livestock:
        live_chgs = [r.change_pct_1d for r in livestock if r.change_pct_1d is not None]
        livestock_avg = sum(live_chgs) / len(live_chgs) if live_chgs else None

    signal_parts = []
    for s in ["STRONG BUY", "BUY", "HOLD", "SELL", "STRONG SELL"]:
        if signal_counts.get(s, 0) > 0:
            signal_parts.append(f"{signal_counts[s]} {s}")

    summary_line = f"Overall market sentiment is {sentiment}."
    signals_line = f"Signals: {', '.join(signal_parts)}."
    sector_parts = []
    if grain_avg is not None:
        sector_parts.append(f"Grains averaged {grain_avg:+.1f}% today")
    if livestock_avg is not None:
        sector_parts.append(f"livestock {livestock_avg:+.1f}%")
    sector_line = "; ".join(sector_parts) + "." if sector_parts else ""

    text = f"{summary_line} {signals_line} {sector_line}"

    html = (
        f'<div style="background:linear-gradient(135deg,#f8f9fa,#e9ecef);border-left:4px solid {sent_color};'
        f'padding:12px 16px;margin:12px 0;border-radius:4px;">'
        f'<div style="font-size:18px;font-weight:700;color:{sent_color};margin-bottom:4px;">'
        f'{sentiment}</div>'
        f'<div style="font-size:13px;color:#444;">{signals_line} {sector_line}</div>'
        f'</div>'
    )

    return html, text


def render_html(reports: list[InstrumentReport], run_ts: datetime, is_weekend: bool = False) -> str:
    overview_html, _ = _market_overview(reports)

    weekend_note = ""
    if is_weekend:
        weekend_note = (
            '<div style="background:#fff3cd;border:1px solid #ffc107;padding:8px 12px;'
            'border-radius:4px;margin:8px 0;font-size:12px;color:#664d03;">'
            'Weekend report -- prices shown are from market close on Friday. '
            'Markets reopen Sunday evening.</div>'
        )

    rows: list[str] = []
    for i, r in enumerate(reports):
        bg = "#f9f9f9" if i % 2 == 0 else "#ffffff"
        border_color = {"STRONG BUY": "#0d6a3a", "BUY": "#1a7f37", "SELL": "#b42318",
                        "STRONG SELL": "#8b0000"}.get(r.signal, "#ddd")
        if r.error:
            rows.append(
                f'<tr style="background:{bg};">'
                f'<td style="border-left:3px solid #b42318;padding:8px;"><b>{r.name}</b>'
                f'<br><small>{r.symbol}</small></td>'
                f"<td colspan='8' style='color:#b42318;padding:8px;'>Data unavailable: {r.error}</td></tr>"
            )
            continue
        chg_color_1d = "#1a7f37" if (r.change_pct_1d or 0) >= 0 else "#b42318"
        chg_color_7d = "#1a7f37" if (r.change_pct_7d or 0) >= 0 else "#b42318"
        rows.append(
            f'<tr style="background:{bg};border-bottom:1px solid #eee;">'
            f'<td style="border-left:3px solid {border_color};padding:8px;">'
            f'<b>{r.name}</b><br><small>{r.symbol} &middot; {r.unit}</small></td>'
            f'<td style="text-align:right;font-weight:600;padding:8px;">{_fmt(r.last)}</td>'
            f'<td style="text-align:right;color:{chg_color_1d};padding:8px;">{_fmt(r.change_pct_1d, 2, "%")}</td>'
            f'<td style="text-align:right;color:{chg_color_7d};padding:8px;">{_fmt(r.change_pct_7d, 2, "%")}</td>'
            f'<td style="text-align:right;padding:8px;">{_fmt(r.rsi14, 0)}</td>'
            f'<td style="padding:8px;">{r.trend}</td>'
            f'<td style="padding:8px;">{_badge(r.signal)}</td>'
            f'<td style="text-align:right;padding:8px;">'
            f'{_forecast_html(r.last, r.forecast_1d, r.forecast_1d_pct)}<br>'
            f'<small>{_forecast_html(r.last, r.forecast_7d, r.forecast_7d_pct)} (7d)</small></td>'
            f'</tr>'
        )

    details: list[str] = []
    for r in reports:
        if r.error:
            continue
        macd_color = "#1a7f37" if (r.macd_histogram or 0) > 0 else "#b42318"
        bb_position = ""
        if r.bb_upper is not None and r.bb_lower is not None and r.last is not None:
            if r.last >= r.bb_upper:
                bb_position = "At/above upper band"
            elif r.last <= r.bb_lower:
                bb_position = "At/below lower band"
            else:
                bb_range = r.bb_upper - r.bb_lower
                if bb_range > 0:
                    pct_pos = (r.last - r.bb_lower) / bb_range * 100
                    bb_position = f"{pct_pos:.0f}% within bands"
                else:
                    bb_position = "Middle of bands"

        details.append(
            f'<div style="border:1px solid #e0e0e0;border-radius:6px;padding:12px;margin:10px 0;">'
            f'<h3 style="margin:0 0 8px 0;">{r.name} &mdash; {_badge(r.signal)} '
            f'{_confidence_badge(r.confidence)}</h3>'
            f'<table cellpadding="4" style="font-size:12px;color:#444;width:100%;">'
            f'<tr><td><b>Trend:</b> {r.trend}</td>'
            f'<td><b>Last:</b> {_fmt(r.last)} {r.unit}</td></tr>'
            f'<tr><td><b>SMA20:</b> {_fmt(r.sma20)}</td>'
            f'<td><b>SMA50:</b> {_fmt(r.sma50)}</td></tr>'
            f'<tr><td><b>RSI(14):</b> {_fmt(r.rsi14, 0)}</td>'
            f'<td><b>MACD Histogram:</b> '
            f'<span style="color:{macd_color};font-weight:600;">{_fmt(r.macd_histogram, 3)}</span></td></tr>'
            f'<tr><td><b>Bollinger Bands:</b> {_fmt(r.bb_lower)} / {_fmt(r.bb_middle)} / {_fmt(r.bb_upper)}</td>'
            f'<td><b>BB Position:</b> {bb_position}</td></tr>'
            f'<tr><td colspan="2" style="padding-top:6px;border-top:1px solid #eee;">'
            f'<b>1-Day Forecast:</b> {_forecast_html(r.last, r.forecast_1d, r.forecast_1d_pct)} &nbsp;&nbsp;'
            f'<b>7-Day Forecast:</b> {_forecast_html(r.last, r.forecast_7d, r.forecast_7d_pct)}</td></tr>'
            f'<tr><td colspan="2"><b>Rationale:</b> {r.rationale}</td></tr>'
            f'</table></div>'
        )

    error_count = sum(1 for r in reports if r.error)
    status_note = ""
    if error_count > 0:
        status_note = (
            f'<p style="color:#b42318;font-weight:600;">'
            f'Warning: {error_count} instrument(s) could not be fetched.</p>'
        )

    return f"""\
<!doctype html>
<html><head>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="font-family:Arial,Helvetica,sans-serif;color:#222;max-width:900px;margin:auto;padding:10px;">
  <h2 style="margin-bottom:0;">Daily Grain &amp; Livestock Report</h2>
  <p style="margin-top:4px;color:#666;">
    Generated {run_ts.strftime('%A, %B %d, %Y at %H:%M UTC')}
  </p>
  {weekend_note}
  {status_note}

  <h3 style="margin-bottom:4px;">Market Overview</h3>
  {overview_html}

  <div style="overflow-x:auto;">
  <table cellpadding="0" cellspacing="0" border="0"
         style="border-collapse:collapse;width:100%;font-size:13px;">
    <thead>
      <tr style="background:#2c3e50;color:#fff;">
        <th align="left" style="padding:10px 8px;">Instrument</th>
        <th align="right" style="padding:10px 8px;">Last</th>
        <th align="right" style="padding:10px 8px;">1d %</th>
        <th align="right" style="padding:10px 8px;">7d %</th>
        <th align="right" style="padding:10px 8px;">RSI</th>
        <th align="left" style="padding:10px 8px;">Trend</th>
        <th align="left" style="padding:10px 8px;">Signal</th>
        <th align="right" style="padding:10px 8px;">Forecast</th>
      </tr>
    </thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  </div>

  <h3 style="margin-top:24px;">Detailed Analysis &amp; Strategy</h3>
  {''.join(details)}

  <hr style="margin-top:24px;border:none;border-top:1px solid #ddd;">
  <p style="color:#888;font-size:11px;">
    Prices are end-of-day from Yahoo Finance and may be delayed.
    Signals are based on multi-indicator confluence: SMA crossover, RSI(14),
    MACD(12,26,9), and Bollinger Bands(20,2). Forecasts are linear
    extrapolations of the last 20 closes. This is an automated
    summary, <b>not financial advice</b>. Do your own research before trading.
    Sheep/Lamb uses the iPath Bloomberg Livestock ETN (COW) as a proxy.
  </p>
</body></html>
"""


def render_text(reports: list[InstrumentReport], run_ts: datetime, is_weekend: bool = False) -> str:
    _, overview_text = _market_overview(reports)

    lines = [
        "=" * 64,
        "   Daily Grain & Livestock Report",
        f"   Generated {run_ts.strftime('%A, %B %d, %Y at %H:%M UTC')}",
        "=" * 64,
    ]

    if is_weekend:
        lines += [
            "",
            "  [WEEKEND] Prices from market close on Friday.",
            "  Markets reopen Sunday evening.",
        ]

    lines += [
        "",
        "  MARKET OVERVIEW",
        f"  {overview_text}",
        "",
        "-" * 64,
    ]

    for r in reports:
        if r.error:
            lines.append(f"  {r.name} ({r.symbol}): DATA UNAVAILABLE - {r.error}")
            lines.append("")
            continue

        macd_str = f"MACD Hist: {_fmt(r.macd_histogram, 3)}"
        bb_str = f"BB: {_fmt(r.bb_lower)} / {_fmt(r.bb_middle)} / {_fmt(r.bb_upper)}"

        lines += [
            f"--- {r.name} ({r.symbol}) {_signal_emoji(r.signal)} [{r.confidence}] ---",
            f"  Last Price: {_fmt(r.last)} {r.unit}",
            f"  Changes:    1d {_fmt(r.change_pct_1d, 2, '%')}  |  "
            f"7d {_fmt(r.change_pct_7d, 2, '%')}  |  "
            f"30d {_fmt(r.change_pct_30d, 2, '%')}",
            f"  SMA20/50:   {_fmt(r.sma20)} / {_fmt(r.sma50)}",
            f"  RSI(14):    {_fmt(r.rsi14, 0)}  |  {macd_str}",
            f"  {bb_str}",
            f"  Trend:      {r.trend}",
            f"  Signal:     {r.signal} ({r.confidence} confidence)",
            f"  Rationale:  {r.rationale}",
            f"  1d Forecast: {_forecast_text(r.last, r.forecast_1d, r.forecast_1d_pct)}",
            f"  7d Forecast: {_forecast_text(r.last, r.forecast_7d, r.forecast_7d_pct)}",
            "",
        ]

    lines += [
        "-" * 64,
        "Prices from Yahoo Finance (end-of-day, may be delayed).",
        "Signals: SMA crossover + RSI(14) + MACD(12,26,9) + Bollinger(20,2).",
        "NOT financial advice. Do your own research.",
        "-" * 64,
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Email.
# ---------------------------------------------------------------------------
def send_email(subject: str, text_body: str, html_body: str) -> None:
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or "587")
    username = os.environ.get("SMTP_USERNAME") or ""
    password = os.environ.get("SMTP_PASSWORD") or ""
    email_from = os.environ.get("EMAIL_FROM") or username
    email_to = os.environ.get("EMAIL_TO") or "wickdav9413@gmail.com"

    if not username or not password:
        raise RuntimeError(
            "SMTP_USERNAME and SMTP_PASSWORD must be set (use a Gmail App Password).\n"
            "Set these as GitHub repository secrets under Settings > Secrets and variables > Actions."
        )

    if not email_from:
        email_from = username

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = email_to
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port) as server:
        server.starttls(context=context)
        server.login(username, password)
        server.send_message(msg)


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------
def main() -> int:
    run_ts = datetime.now(timezone.utc)
    is_weekend = run_ts.weekday() in (5, 6)

    print(f"Fetching data for {len(INSTRUMENTS)} instruments...")
    print(f"Run time: {run_ts.isoformat()}")
    if is_weekend:
        print("Weekend run -- will show last available market close.")
    print()

    try:
        print("Initializing Yahoo Finance session...", end=" ", flush=True)
        _init_yahoo_session()
        print("OK")
    except Exception as exc:
        print(f"WARNING: {exc}")
        print("Will rely on yfinance library fallback.\n")

    reports = []
    for i, (name, symbol, unit) in enumerate(INSTRUMENTS):
        print(f"  [{i+1}/{len(INSTRUMENTS)}] {name} ({symbol})...", end=" ", flush=True)
        report = analyze(name, symbol, unit)
        if report.error:
            print(f"FAILED: {report.error}")
        else:
            print(f"OK - {_fmt(report.last)} {unit}")
        reports.append(report)
        if i < len(INSTRUMENTS) - 1:
            time.sleep(1.5)

    success_count = sum(1 for r in reports if not r.error)
    print(f"\nResults: {success_count}/{len(reports)} instruments fetched successfully.\n")

    text_body = render_text(reports, run_ts, is_weekend)
    html_body = render_html(reports, run_ts, is_weekend)

    weekend_tag = " (Weekend)" if is_weekend else ""
    subject = f"Grain & Livestock Report - {run_ts.strftime('%Y-%m-%d')}{weekend_tag}"

    print(text_body)

    if os.environ.get("DRY_RUN", "").lower() in {"1", "true", "yes"}:
        print("\nDRY_RUN set; skipping email send.")
        return 0

    if success_count == 0:
        print("\nERROR: All instruments failed to fetch. Not sending empty report.")
        return 1

    try:
        send_email(subject, text_body, html_body)
        print("\nEmail sent successfully.")
    except Exception as exc:
        print(f"\nERROR sending email: {exc}")
        print("The report was printed above. Check SMTP_USERNAME and SMTP_PASSWORD secrets.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
