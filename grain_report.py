"""
Daily Grain & Livestock Market Report.

Fetches recent prices for corn, soybeans, wheat, milk, live cattle (beef),
lean hogs, and sheep/lamb, computes technical indicators (SMA, RSI, MACD,
Bollinger Bands), generates buy/sell signals with confidence scores,
produces 1- and 7-day forecasts, and emails the report.

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
technical-analysis rules. Always do your own research.
"""
from __future__ import annotations

import io
import math
import os
import smtplib
import ssl
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Optional

import pandas as pd
import requests as _requests

INSTRUMENTS: list[tuple[str, str, str]] = [
    ("Corn", "ZC=F", "USD/bushel"),
    ("Soybeans", "ZS=F", "USD/bushel"),
    ("Wheat", "ZW=F", "USD/bushel"),
    ("Class III Milk", "DC=F", "USD/cwt"),
    ("Live Cattle (Beef)", "LE=F", "USD/lb"),
    ("Lean Hogs", "HE=F", "USD/lb"),
    ("Sheep/Lamb (proxy: COW ETN)", "COW", "USD/share"),
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
    bb_lower: Optional[float]
    trend: str
    signal: str
    confidence: str
    rationale: str
    strategy: str
    forecast_1d: Optional[float]
    forecast_7d: Optional[float]
    support: Optional[float] = None
    resistance: Optional[float] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Yahoo Finance authenticated session (cookie + crumb).
# ---------------------------------------------------------------------------
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
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
# Technical indicators.
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


def _macd(series: pd.Series) -> tuple[Optional[float], Optional[float], Optional[float]]:
    if len(series) < 35:
        return None, None, None
    ema12 = series.ewm(span=12, adjust=False).mean()
    ema26 = series.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    histogram = macd_line - signal_line
    return float(macd_line.iloc[-1]), float(signal_line.iloc[-1]), float(histogram.iloc[-1])


def _bollinger_bands(series: pd.Series, period: int = 20) -> tuple[Optional[float], Optional[float]]:
    if len(series) < period:
        return None, None
    window = series.tail(period)
    sma = window.mean()
    std = window.std()
    return float(sma + 2 * std), float(sma - 2 * std)


def _support_resistance(series: pd.Series) -> tuple[Optional[float], Optional[float]]:
    if len(series) < 20:
        return None, None
    recent = series.tail(20)
    return float(recent.min()), float(recent.max())


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
# Signal analysis.
# ---------------------------------------------------------------------------
def _derive_signal(
    last: float,
    sma20: Optional[float],
    sma50: Optional[float],
    rsi: Optional[float],
    macd_hist: Optional[float],
    bb_upper: Optional[float],
    bb_lower: Optional[float],
    forecast_1d: Optional[float],
    forecast_7d: Optional[float],
) -> tuple[str, str, str, str, str]:
    """Return (trend, signal, confidence, rationale, strategy)."""
    bullish_votes = 0
    bearish_votes = 0
    reasons: list[str] = []

    # SMA trend
    if sma20 is not None and sma50 is not None:
        if last > sma20 > sma50:
            trend = "Uptrend"
            bullish_votes += 2
            reasons.append("price > SMA20 > SMA50 (strong uptrend)")
        elif last < sma20 < sma50:
            trend = "Downtrend"
            bearish_votes += 2
            reasons.append("price < SMA20 < SMA50 (strong downtrend)")
        elif last > sma50:
            trend = "Weak uptrend"
            bullish_votes += 1
            reasons.append("price above SMA50")
        else:
            trend = "Weak downtrend"
            bearish_votes += 1
            reasons.append("price below SMA50")
    else:
        trend = "Unknown"

    # RSI
    if rsi is not None:
        if rsi < 30:
            bullish_votes += 2
            reasons.append(f"RSI {rsi:.0f} — oversold")
        elif rsi > 70:
            bearish_votes += 2
            reasons.append(f"RSI {rsi:.0f} — overbought")
        elif rsi < 40:
            bullish_votes += 1
            reasons.append(f"RSI {rsi:.0f} — approaching oversold")
        elif rsi > 60:
            bearish_votes += 1
            reasons.append(f"RSI {rsi:.0f} — approaching overbought")
        else:
            reasons.append(f"RSI {rsi:.0f} — neutral")

    # MACD
    if macd_hist is not None:
        if macd_hist > 0:
            bullish_votes += 1
            reasons.append("MACD histogram positive (bullish momentum)")
        else:
            bearish_votes += 1
            reasons.append("MACD histogram negative (bearish momentum)")

    # Bollinger Bands
    if bb_upper is not None and bb_lower is not None:
        if last >= bb_upper:
            bearish_votes += 1
            reasons.append("price at upper Bollinger Band (potentially stretched)")
        elif last <= bb_lower:
            bullish_votes += 1
            reasons.append("price at lower Bollinger Band (potential bounce)")

    # Forecast direction
    if forecast_7d is not None and last:
        if forecast_7d > last * 1.01:
            bullish_votes += 1
        elif forecast_7d < last * 0.99:
            bearish_votes += 1

    # Determine signal from votes
    net = bullish_votes - bearish_votes
    if net >= 3:
        signal = "STRONG BUY"
    elif net >= 1:
        signal = "BUY"
    elif net <= -3:
        signal = "STRONG SELL"
    elif net <= -1:
        signal = "SELL"
    else:
        signal = "HOLD"

    # Confidence
    total_votes = bullish_votes + bearish_votes
    if total_votes == 0:
        confidence = "Low"
    elif abs(net) >= 4:
        confidence = "High"
    elif abs(net) >= 2:
        confidence = "Medium"
    else:
        confidence = "Low"

    # Strategy text
    strategy = _build_strategy(signal, last, sma20, sma50, bb_upper, bb_lower, forecast_1d, forecast_7d)

    return trend, signal, confidence, "; ".join(reasons), strategy


def _build_strategy(
    signal: str,
    last: float,
    sma20: Optional[float],
    sma50: Optional[float],
    bb_upper: Optional[float],
    bb_lower: Optional[float],
    forecast_1d: Optional[float],
    forecast_7d: Optional[float],
) -> str:
    parts: list[str] = []

    if "BUY" in signal:
        parts.append(f"Consider buying near current price ({last:,.2f}).")
        if bb_lower is not None:
            parts.append(f"Ideal entry near lower Bollinger Band at {bb_lower:,.2f}.")
        if sma50 is not None:
            parts.append(f"Set stop-loss below SMA50 at {sma50:,.2f}.")
        if bb_upper is not None:
            parts.append(f"Take-profit target near {bb_upper:,.2f}.")
    elif "SELL" in signal:
        parts.append(f"Consider selling or reducing position at current price ({last:,.2f}).")
        if bb_upper is not None:
            parts.append(f"If short, target lower Bollinger Band at {bb_lower:,.2f}." if bb_lower else "")
        if sma20 is not None:
            parts.append(f"Watch SMA20 at {sma20:,.2f} for potential support.")
    else:
        parts.append("Hold current positions. No strong directional signal.")
        if sma20 is not None and sma50 is not None:
            parts.append(f"Watch for SMA20 ({sma20:,.2f}) crossing SMA50 ({sma50:,.2f}) for next signal.")

    if forecast_1d is not None and forecast_7d is not None:
        direction_1d = "up" if forecast_1d > last else "down"
        direction_7d = "up" if forecast_7d > last else "down"
        parts.append(f"Model projects {direction_1d} tomorrow, {direction_7d} over the week.")

    return " ".join(p for p in parts if p)


def analyze(name: str, symbol: str, unit: str) -> InstrumentReport:
    blank = InstrumentReport(
        name=name, symbol=symbol, unit=unit,
        last=None, prev_close=None,
        change_pct_1d=None, change_pct_7d=None, change_pct_30d=None,
        sma20=None, sma50=None, rsi14=None,
        macd_line=None, macd_signal=None, macd_histogram=None,
        bb_upper=None, bb_lower=None,
        trend="Unknown", signal="HOLD", confidence="Low",
        rationale="no data", strategy="No data available for analysis.",
        forecast_1d=None, forecast_7d=None,
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
    bb_upper, bb_lower = _bollinger_bands(closes)
    support, resistance = _support_resistance(closes)
    fc_1d = _linear_forecast(closes, 1)
    fc_7d = _linear_forecast(closes, 5)

    trend, signal, confidence, rationale, strategy = _derive_signal(
        last, sma20, sma50, rsi14, macd_h, bb_upper, bb_lower, fc_1d, fc_7d
    )

    return InstrumentReport(
        name=name, symbol=symbol, unit=unit,
        last=last, prev_close=prev,
        change_pct_1d=_pct_change(closes, 1),
        change_pct_7d=_pct_change(closes, 5),
        change_pct_30d=_pct_change(closes, 21),
        sma20=sma20, sma50=sma50, rsi14=rsi14,
        macd_line=macd_l, macd_signal=macd_s, macd_histogram=macd_h,
        bb_upper=bb_upper, bb_lower=bb_lower,
        trend=trend, signal=signal, confidence=confidence,
        rationale=rationale, strategy=strategy,
        forecast_1d=fc_1d, forecast_7d=fc_7d,
        support=support, resistance=resistance,
    )


# ---------------------------------------------------------------------------
# Rendering.
# ---------------------------------------------------------------------------
def _fmt(value: Optional[float], places: int = 2, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    return f"{value:,.{places}f}{suffix}"


def _signal_color(signal: str) -> str:
    if "BUY" in signal:
        return "#0d6e3f"
    if "SELL" in signal:
        return "#b42318"
    return "#6c6c6c"


def _badge(signal: str) -> str:
    colour = _signal_color(signal)
    return (
        f'<span style="background:{colour};color:#fff;padding:3px 10px;'
        f'border-radius:12px;font-weight:700;font-size:12px;letter-spacing:0.5px;">'
        f'{signal}</span>'
    )


def _confidence_dots(confidence: str) -> str:
    filled = {"High": 3, "Medium": 2, "Low": 1}.get(confidence, 0)
    dots = "●" * filled + "○" * (3 - filled)
    return f'<span style="color:#888;font-size:14px;" title="{confidence} confidence">{dots}</span>'


def _arrow(pct: Optional[float]) -> str:
    if pct is None:
        return ""
    if pct > 0:
        return '<span style="color:#0d6e3f;">&#9650;</span>'
    if pct < 0:
        return '<span style="color:#b42318;">&#9660;</span>'
    return '<span style="color:#888;">&#9654;</span>'


def _market_overview(reports: list[InstrumentReport]) -> str:
    valid = [r for r in reports if not r.error]
    if not valid:
        return "<p>Unable to generate market overview — no data available.</p>"

    buy_count = sum(1 for r in valid if "BUY" in r.signal)
    sell_count = sum(1 for r in valid if "SELL" in r.signal)
    hold_count = sum(1 for r in valid if r.signal == "HOLD")
    up_count = sum(1 for r in valid if (r.change_pct_1d or 0) > 0)
    down_count = sum(1 for r in valid if (r.change_pct_1d or 0) < 0)

    if buy_count > sell_count and buy_count > hold_count:
        tone = "Bullish"
        tone_color = "#0d6e3f"
        summary = "The majority of tracked commodities are showing buying signals. Market momentum is positive across the grain and livestock complex."
    elif sell_count > buy_count and sell_count > hold_count:
        tone = "Bearish"
        tone_color = "#b42318"
        summary = "The majority of tracked commodities are showing selling signals. Consider tightening stops and reducing exposure."
    else:
        tone = "Mixed / Neutral"
        tone_color = "#996600"
        summary = "Market signals are mixed. Some commodities are bullish while others are bearish. Focus on individual instrument signals for opportunities."

    return f"""
    <div style="background:linear-gradient(135deg,#f8f9fa,#e9ecef);border-radius:12px;padding:20px;margin:16px 0;">
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;">
        <span style="font-size:18px;font-weight:700;">Market Overview</span>
        <span style="background:{tone_color};color:#fff;padding:4px 12px;border-radius:8px;
               font-weight:600;font-size:13px;">{tone}</span>
      </div>
      <p style="margin:0 0 12px 0;color:#444;font-size:14px;">{summary}</p>
      <div style="display:flex;gap:24px;flex-wrap:wrap;">
        <div><span style="font-size:22px;font-weight:700;color:#0d6e3f;">{buy_count}</span>
             <span style="color:#666;font-size:12px;"> Buy</span></div>
        <div><span style="font-size:22px;font-weight:700;color:#b42318;">{sell_count}</span>
             <span style="color:#666;font-size:12px;"> Sell</span></div>
        <div><span style="font-size:22px;font-weight:700;color:#6c6c6c;">{hold_count}</span>
             <span style="color:#666;font-size:12px;"> Hold</span></div>
        <div style="border-left:1px solid #ccc;padding-left:24px;">
             <span style="font-size:22px;font-weight:700;color:#0d6e3f;">{up_count}</span>
             <span style="color:#666;font-size:12px;"> Up today</span></div>
        <div><span style="font-size:22px;font-weight:700;color:#b42318;">{down_count}</span>
             <span style="color:#666;font-size:12px;"> Down today</span></div>
      </div>
    </div>
    """


def render_html(reports: list[InstrumentReport], run_ts: datetime) -> str:
    rows: list[str] = []
    for r in reports:
        if r.error:
            rows.append(
                f"<tr><td style='padding:10px;'><b>{r.name}</b><br>"
                f"<small style='color:#888;'>{r.symbol}</small></td>"
                f"<td colspan='7' style='color:#b42318;padding:10px;'>"
                f"Data unavailable: {r.error}</td></tr>"
            )
            continue

        chg_1d = r.change_pct_1d or 0
        chg_7d = r.change_pct_7d or 0
        chg_30d = r.change_pct_30d or 0

        rows.append(
            f"<tr style='border-bottom:1px solid #eee;'>"
            f"<td style='padding:10px;'><b>{r.name}</b><br>"
            f"<small style='color:#888;'>{r.symbol} &middot; {r.unit}</small></td>"
            f"<td style='text-align:right;padding:10px;font-weight:700;font-size:15px;'>"
            f"{_fmt(r.last)}</td>"
            f"<td style='text-align:right;padding:10px;color:{_signal_color('BUY' if chg_1d >= 0 else 'SELL')};'>"
            f"{_arrow(r.change_pct_1d)} {_fmt(r.change_pct_1d, 2, '%')}</td>"
            f"<td style='text-align:right;padding:10px;color:{_signal_color('BUY' if chg_7d >= 0 else 'SELL')};'>"
            f"{_fmt(r.change_pct_7d, 2, '%')}</td>"
            f"<td style='text-align:right;padding:10px;'>{_fmt(r.rsi14, 0)}</td>"
            f"<td style='text-align:center;padding:10px;'>{_badge(r.signal)}<br>"
            f"<small>{_confidence_dots(r.confidence)} {r.confidence}</small></td>"
            f"<td style='text-align:right;padding:10px;font-size:13px;'>"
            f"1d: {_fmt(r.forecast_1d)}<br>"
            f"<small style='color:#666;'>7d: {_fmt(r.forecast_7d)}</small></td>"
            f"</tr>"
        )

    details: list[str] = []
    for r in reports:
        if r.error:
            continue

        fc_1d_pct = ""
        if r.forecast_1d is not None and r.last:
            pct = (r.forecast_1d - r.last) / r.last * 100
            fc_1d_pct = f" ({pct:+.2f}%)"
        fc_7d_pct = ""
        if r.forecast_7d is not None and r.last:
            pct = (r.forecast_7d - r.last) / r.last * 100
            fc_7d_pct = f" ({pct:+.2f}%)"

        border_color = _signal_color(r.signal)
        details.append(f"""
        <div style="border-left:4px solid {border_color};background:#fafafa;
                    border-radius:0 8px 8px 0;padding:16px;margin:12px 0;">
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;">
            <span style="font-size:16px;font-weight:700;">{r.name}</span>
            {_badge(r.signal)}
            {_confidence_dots(r.confidence)}
          </div>

          <table style="font-size:13px;color:#444;margin-bottom:12px;" cellpadding="4">
            <tr><td style="color:#888;width:100px;">Trend</td><td><b>{r.trend}</b></td>
                <td style="color:#888;width:100px;">Last Price</td><td><b>{_fmt(r.last)} {r.unit}</b></td></tr>
            <tr><td style="color:#888;">SMA 20</td><td>{_fmt(r.sma20)}</td>
                <td style="color:#888;">SMA 50</td><td>{_fmt(r.sma50)}</td></tr>
            <tr><td style="color:#888;">RSI (14)</td><td>{_fmt(r.rsi14, 0)}</td>
                <td style="color:#888;">MACD Hist</td><td>{_fmt(r.macd_histogram, 4)}</td></tr>
            <tr><td style="color:#888;">Bollinger Hi</td><td>{_fmt(r.bb_upper)}</td>
                <td style="color:#888;">Bollinger Lo</td><td>{_fmt(r.bb_lower)}</td></tr>
            <tr><td style="color:#888;">20d Support</td><td>{_fmt(r.support)}</td>
                <td style="color:#888;">20d Resistance</td><td>{_fmt(r.resistance)}</td></tr>
          </table>

          <div style="margin-bottom:8px;">
            <span style="font-weight:600;color:#333;">Forecast:</span>
            <span style="margin-left:8px;">1-day: <b>{_fmt(r.forecast_1d)}{fc_1d_pct}</b></span>
            <span style="margin-left:16px;">7-day: <b>{_fmt(r.forecast_7d)}{fc_7d_pct}</b></span>
          </div>

          <div style="background:#fff;border:1px solid #e0e0e0;border-radius:6px;padding:10px;">
            <span style="font-weight:600;color:#333;">Strategy: </span>
            <span style="color:#444;">{r.strategy}</span>
          </div>

          <div style="margin-top:6px;font-size:12px;color:#888;">
            Analysis: {r.rationale}
          </div>
        </div>
        """)

    error_count = sum(1 for r in reports if r.error)
    status_note = ""
    if error_count > 0:
        status_note = (
            f"<div style='background:#fef3f2;border:1px solid #fecdca;border-radius:8px;"
            f"padding:12px;margin:12px 0;color:#b42318;font-weight:600;'>"
            f"Warning: {error_count} instrument(s) could not be fetched. See details below.</div>"
        )

    overview = _market_overview(reports)

    return f"""\
<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;
             color:#222;max-width:900px;margin:auto;padding:20px;background:#fff;">

  <div style="border-bottom:3px solid #1a5632;padding-bottom:12px;margin-bottom:16px;">
    <h1 style="margin:0;font-size:24px;color:#1a5632;">Daily Grain &amp; Livestock Report</h1>
    <p style="margin:4px 0 0 0;color:#888;font-size:13px;">
      {run_ts.strftime('%A, %B %d, %Y at %H:%M UTC')} &nbsp;&middot;&nbsp;
      Prices from Yahoo Finance (end-of-day, may be delayed)
    </p>
  </div>

  {status_note}
  {overview}

  <h2 style="font-size:18px;margin:24px 0 12px 0;color:#333;">Price Summary</h2>
  <table cellpadding="0" cellspacing="0" border="0"
         style="border-collapse:collapse;width:100%;font-size:13px;">
    <thead>
      <tr style="background:#f2f4f5;border-bottom:2px solid #ddd;">
        <th align="left" style="padding:10px;">Instrument</th>
        <th align="right" style="padding:10px;">Last</th>
        <th align="right" style="padding:10px;">1d Change</th>
        <th align="right" style="padding:10px;">7d Change</th>
        <th align="right" style="padding:10px;">RSI</th>
        <th align="center" style="padding:10px;">Signal</th>
        <th align="right" style="padding:10px;">Forecast</th>
      </tr>
    </thead>
    <tbody>{''.join(rows)}</tbody>
  </table>

  <h2 style="font-size:18px;margin:28px 0 12px 0;color:#333;">
    Detailed Analysis &amp; Strategy
  </h2>
  {''.join(details)}

  <div style="margin-top:28px;padding-top:16px;border-top:1px solid #ddd;">
    <p style="color:#999;font-size:11px;line-height:1.5;">
      <b>Disclaimer:</b> This is an automated report. Signals are generated
      mechanically using SMA crossover, RSI(14), MACD(12,26,9), and
      Bollinger Band(20,2) indicators. Forecasts use linear regression on
      the last 20 trading sessions. This is <b>not financial advice</b>.
      Always do your own research before making trading decisions.<br>
      Sheep/Lamb uses the iPath Bloomberg Livestock ETN (COW) as a proxy
      since no public sheep futures contract is available on Yahoo Finance.<br>
      7-day forecast = 5 trading days.
    </p>
  </div>

</body></html>
"""


def render_text(reports: list[InstrumentReport], run_ts: datetime) -> str:
    lines = [
        "=" * 65,
        "   DAILY GRAIN & LIVESTOCK MARKET REPORT",
        f"   {run_ts.strftime('%A, %B %d, %Y at %H:%M UTC')}",
        "=" * 65,
        "",
    ]

    # Market overview
    valid = [r for r in reports if not r.error]
    buy_count = sum(1 for r in valid if "BUY" in r.signal)
    sell_count = sum(1 for r in valid if "SELL" in r.signal)
    hold_count = sum(1 for r in valid if r.signal == "HOLD")
    lines += [
        "MARKET OVERVIEW",
        f"  Buy signals: {buy_count}  |  Sell signals: {sell_count}  |  Hold: {hold_count}",
        "",
        "-" * 65,
        "",
    ]

    for r in reports:
        if r.error:
            lines.append(f"  {r.name} ({r.symbol}): DATA UNAVAILABLE - {r.error}")
            lines.append("")
            continue
        fc_1d_dir = ""
        if r.forecast_1d is not None and r.last is not None:
            pct = (r.forecast_1d - r.last) / r.last * 100
            fc_1d_dir = f" ({pct:+.2f}%)"
        fc_7d_dir = ""
        if r.forecast_7d is not None and r.last is not None:
            pct = (r.forecast_7d - r.last) / r.last * 100
            fc_7d_dir = f" ({pct:+.2f}%)"

        sig_label = f"[{r.signal}]"
        lines += [
            f"--- {r.name} ({r.symbol}) {sig_label} ---",
            f"  Last Price:   {_fmt(r.last)} {r.unit}",
            f"  Changes:      1d {_fmt(r.change_pct_1d, 2, '%')}  |  "
            f"7d {_fmt(r.change_pct_7d, 2, '%')}  |  "
            f"30d {_fmt(r.change_pct_30d, 2, '%')}",
            f"  SMA 20/50:    {_fmt(r.sma20)} / {_fmt(r.sma50)}",
            f"  RSI(14):      {_fmt(r.rsi14, 0)}",
            f"  MACD Hist:    {_fmt(r.macd_histogram, 4)}",
            f"  Bollinger:    {_fmt(r.bb_lower)} — {_fmt(r.bb_upper)}",
            f"  Trend:        {r.trend}",
            f"  Signal:       {r.signal}  (Confidence: {r.confidence})",
            f"  Forecast:     1d -> {_fmt(r.forecast_1d)}{fc_1d_dir}  |  "
            f"7d -> {_fmt(r.forecast_7d)}{fc_7d_dir}",
            f"  Strategy:     {r.strategy}",
            f"  Analysis:     {r.rationale}",
            "",
        ]
    lines += [
        "-" * 65,
        "Prices from Yahoo Finance (end-of-day, may be delayed).",
        "Signals: SMA crossover + RSI + MACD + Bollinger Bands.",
        "7-day forecast = 5 trading days. NOT financial advice.",
        "-" * 65,
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
    print(f"Fetching data for {len(INSTRUMENTS)} instruments...")
    print(f"Run time: {run_ts.isoformat()}")
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

    text_body = render_text(reports, run_ts)
    html_body = render_html(reports, run_ts)
    subject = f"Grain & Livestock Report - {run_ts.strftime('%Y-%m-%d')}"

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
