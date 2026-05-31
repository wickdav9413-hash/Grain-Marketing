"""
Daily Grain & Livestock Market Report.

Fetches recent prices for corn, soybeans, wheat, milk, live cattle (beef),
lean hogs, and sheep/lamb, computes technical indicators (SMA, RSI, MACD),
generates buy/sell signals with confidence levels, produces 1-/7-day
forecasts, and emails the report.

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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
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
    trend: str
    signal: str
    signal_strength: str
    rationale: str
    strategy: str
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


def _macd(series: pd.Series) -> tuple[Optional[float], Optional[float], Optional[float]]:
    if len(series) < 35:
        return None, None, None
    ema12 = series.ewm(span=12, adjust=False).mean()
    ema26 = series.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    histogram = macd_line - signal_line
    return float(macd_line.iloc[-1]), float(signal_line.iloc[-1]), float(histogram.iloc[-1])


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
# Analysis.
# ---------------------------------------------------------------------------
def _derive_signal(
    last: float,
    sma20: Optional[float],
    sma50: Optional[float],
    rsi: Optional[float],
    macd_hist: Optional[float],
    macd_line: Optional[float],
    macd_signal: Optional[float],
) -> tuple[str, str, str, str, str]:
    """Return (trend, signal, strength, rationale, strategy)."""
    reasons: list[str] = []
    bullish_count = 0
    bearish_count = 0
    total_indicators = 0

    # SMA trend
    if sma20 is not None and sma50 is not None:
        total_indicators += 1
        if last > sma20 > sma50:
            trend = "Uptrend"
            reasons.append("price > SMA20 > SMA50 (bullish alignment)")
            bullish_count += 1
        elif last < sma20 < sma50:
            trend = "Downtrend"
            reasons.append("price < SMA20 < SMA50 (bearish alignment)")
            bearish_count += 1
        elif last > sma50:
            trend = "Weak uptrend"
            reasons.append("price above SMA50 but below SMA20")
            bullish_count += 0.5
        else:
            trend = "Weak downtrend"
            reasons.append("price below SMA50")
            bearish_count += 0.5
    else:
        trend = "Unknown"

    # RSI
    if rsi is not None:
        total_indicators += 1
        if rsi < 30:
            bullish_count += 1
            reasons.append(f"RSI {rsi:.0f} (oversold - reversal likely)")
        elif rsi > 70:
            bearish_count += 1
            reasons.append(f"RSI {rsi:.0f} (overbought - pullback likely)")
        elif rsi < 45:
            bearish_count += 0.3
            reasons.append(f"RSI {rsi:.0f} (leaning bearish)")
        elif rsi > 55:
            bullish_count += 0.3
            reasons.append(f"RSI {rsi:.0f} (leaning bullish)")
        else:
            reasons.append(f"RSI {rsi:.0f} (neutral)")

    # MACD
    if macd_hist is not None and macd_line is not None and macd_signal is not None:
        total_indicators += 1
        if macd_line > macd_signal and macd_hist > 0:
            bullish_count += 1
            reasons.append("MACD bullish (line above signal, positive histogram)")
        elif macd_line < macd_signal and macd_hist < 0:
            bearish_count += 1
            reasons.append("MACD bearish (line below signal, negative histogram)")
        elif macd_hist > 0:
            bullish_count += 0.5
            reasons.append("MACD histogram positive (momentum building)")
        else:
            bearish_count += 0.5
            reasons.append("MACD histogram negative (momentum fading)")

    # Determine signal and strength
    if total_indicators == 0:
        signal = "HOLD"
        strength = "Low"
        strategy = "Insufficient data for analysis. Monitor for more price action."
    else:
        net_score = bullish_count - bearish_count

        if net_score >= 2:
            signal = "BUY"
            strength = "Strong"
        elif net_score >= 1:
            signal = "BUY"
            strength = "Moderate"
        elif net_score <= -2:
            signal = "SELL"
            strength = "Strong"
        elif net_score <= -1:
            signal = "SELL"
            strength = "Moderate"
        else:
            signal = "HOLD"
            strength = "Neutral"

        strategy = _build_strategy(signal, strength, trend, rsi, macd_hist, sma20, sma50, last)

    return trend, signal, strength, "; ".join(reasons) if reasons else "insufficient data", strategy


def _build_strategy(
    signal: str,
    strength: str,
    trend: str,
    rsi: Optional[float],
    macd_hist: Optional[float],
    sma20: Optional[float],
    sma50: Optional[float],
    last: float,
) -> str:
    parts: list[str] = []

    if signal == "BUY" and strength == "Strong":
        parts.append("STRONG BUY - Multiple indicators confirm bullish momentum.")
        parts.append("Consider entering long positions or adding to existing longs.")
        if sma20 is not None:
            parts.append(f"Watch SMA20 ({sma20:,.2f}) as near-term support.")
    elif signal == "BUY" and strength == "Moderate":
        parts.append("MODERATE BUY - Bullish signals emerging but not fully confirmed.")
        parts.append("Consider scaling in with partial positions.")
        if sma50 is not None:
            parts.append(f"Set stops below SMA50 ({sma50:,.2f}).")
    elif signal == "SELL" and strength == "Strong":
        parts.append("STRONG SELL - Multiple indicators confirm bearish pressure.")
        parts.append("Consider reducing long exposure or initiating hedges.")
        if sma20 is not None:
            parts.append(f"SMA20 ({sma20:,.2f}) is now acting as resistance.")
    elif signal == "SELL" and strength == "Moderate":
        parts.append("MODERATE SELL - Bearish signals building.")
        parts.append("Consider tightening stops on existing longs.")
        if sma50 is not None:
            parts.append(f"A break below SMA50 ({sma50:,.2f}) would confirm further downside.")
    else:
        parts.append("HOLD - No clear directional signal.")
        parts.append("Wait for trend confirmation before entering new positions.")

    if rsi is not None:
        if rsi < 25:
            parts.append("Extreme oversold condition may present a short-term bounce opportunity.")
        elif rsi > 75:
            parts.append("Extreme overbought condition may lead to a near-term correction.")

    return " ".join(parts)


def analyze(name: str, symbol: str, unit: str) -> InstrumentReport:
    blank = InstrumentReport(
        name=name, symbol=symbol, unit=unit,
        last=None, prev_close=None,
        change_pct_1d=None, change_pct_7d=None, change_pct_30d=None,
        sma20=None, sma50=None, rsi14=None,
        macd_line=None, macd_signal=None, macd_histogram=None,
        trend="Unknown", signal="HOLD", signal_strength="Low",
        rationale="no data", strategy="No data available.",
        forecast_1d=None, forecast_7d=None,
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

    trend, signal, strength, rationale, strategy = _derive_signal(
        last, sma20, sma50, rsi14, macd_h, macd_l, macd_s
    )

    fc_1d = _linear_forecast(closes, 1)
    fc_7d = _linear_forecast(closes, 5)

    fc_1d_pct = None
    if fc_1d is not None and last != 0:
        fc_1d_pct = (fc_1d - last) / last * 100

    fc_7d_pct = None
    if fc_7d is not None and last != 0:
        fc_7d_pct = (fc_7d - last) / last * 100

    return InstrumentReport(
        name=name,
        symbol=symbol,
        unit=unit,
        last=last,
        prev_close=prev,
        change_pct_1d=_pct_change(closes, 1),
        change_pct_7d=_pct_change(closes, 5),
        change_pct_30d=_pct_change(closes, 21),
        sma20=sma20,
        sma50=sma50,
        rsi14=rsi14,
        macd_line=macd_l,
        macd_signal=macd_s,
        macd_histogram=macd_h,
        trend=trend,
        signal=signal,
        signal_strength=strength,
        rationale=rationale,
        strategy=strategy,
        forecast_1d=fc_1d,
        forecast_7d=fc_7d,
        forecast_1d_pct=fc_1d_pct,
        forecast_7d_pct=fc_7d_pct,
    )


# ---------------------------------------------------------------------------
# Rendering.
# ---------------------------------------------------------------------------
def _fmt(value: Optional[float], places: int = 2, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    return f"{value:,.{places}f}{suffix}"


def _badge(signal: str, strength: str = "") -> str:
    colour = {"BUY": "#1a7f37", "SELL": "#b42318", "HOLD": "#6c6c6c"}.get(signal, "#6c6c6c")
    label = signal
    if strength and strength != "Neutral" and strength != "Low":
        label = f"{strength} {signal}"
    return (
        f'<span style="background:{colour};color:#fff;padding:2px 8px;'
        f'border-radius:10px;font-weight:600;font-size:12px;">{label}</span>'
    )


def _signal_emoji(signal: str) -> str:
    return {"BUY": "[BUY]", "SELL": "[SELL]", "HOLD": "[HOLD]"}.get(signal, "[?]")


def _arrow(pct: Optional[float]) -> str:
    if pct is None:
        return ""
    if pct > 0.1:
        return " &#x25B2;"
    elif pct < -0.1:
        return " &#x25BC;"
    return " &#x25BA;"


def render_html(reports: list[InstrumentReport], run_ts: datetime) -> str:
    is_weekend = run_ts.weekday() >= 5
    weekend_note = ""
    if is_weekend:
        weekend_note = (
            "<p style='color:#b86e00;font-style:italic;'>"
            "Note: Markets are closed on weekends. Prices shown are from the most recent trading day. "
            "Forecasts project from Friday's close.</p>"
        )

    # --- Summary table ---
    rows: list[str] = []
    for r in reports:
        if r.error:
            rows.append(
                f"<tr><td><b>{r.name}</b><br><small>{r.symbol}</small></td>"
                f"<td colspan='8' style='color:#b42318;'>Data unavailable: {r.error}</td></tr>"
            )
            continue
        chg_color_1d = "#1a7f37" if (r.change_pct_1d or 0) >= 0 else "#b42318"
        chg_color_7d = "#1a7f37" if (r.change_pct_7d or 0) >= 0 else "#b42318"
        chg_color_30d = "#1a7f37" if (r.change_pct_30d or 0) >= 0 else "#b42318"
        rows.append(
            "<tr>"
            f"<td><b>{r.name}</b><br><small>{r.symbol} &middot; {r.unit}</small></td>"
            f"<td style='text-align:right;font-weight:600;'>{_fmt(r.last)}</td>"
            f"<td style='text-align:right;color:{chg_color_1d};'>{_fmt(r.change_pct_1d, 2, '%')}</td>"
            f"<td style='text-align:right;color:{chg_color_7d};'>{_fmt(r.change_pct_7d, 2, '%')}</td>"
            f"<td style='text-align:right;color:{chg_color_30d};'>{_fmt(r.change_pct_30d, 2, '%')}</td>"
            f"<td style='text-align:right;'>{_fmt(r.rsi14, 0)}</td>"
            f"<td>{r.trend}</td>"
            f"<td>{_badge(r.signal, r.signal_strength)}</td>"
            "</tr>"
        )

    # --- Forecast section ---
    forecast_rows: list[str] = []
    for r in reports:
        if r.error:
            continue
        fc1_color = "#1a7f37" if (r.forecast_1d_pct or 0) >= 0 else "#b42318"
        fc7_color = "#1a7f37" if (r.forecast_7d_pct or 0) >= 0 else "#b42318"
        forecast_rows.append(
            "<tr>"
            f"<td><b>{r.name}</b></td>"
            f"<td style='text-align:right;font-weight:600;'>{_fmt(r.last)}</td>"
            f"<td style='text-align:right;'>{_fmt(r.forecast_1d)}</td>"
            f"<td style='text-align:right;color:{fc1_color};'>"
            f"{_fmt(r.forecast_1d_pct, 2, '%')}{_arrow(r.forecast_1d_pct)}</td>"
            f"<td style='text-align:right;'>{_fmt(r.forecast_7d)}</td>"
            f"<td style='text-align:right;color:{fc7_color};'>"
            f"{_fmt(r.forecast_7d_pct, 2, '%')}{_arrow(r.forecast_7d_pct)}</td>"
            "</tr>"
        )

    # --- Detailed strategy per instrument ---
    details: list[str] = []
    for r in reports:
        if r.error:
            continue
        macd_info = ""
        if r.macd_line is not None:
            macd_info = (
                f"<b>MACD:</b> Line {r.macd_line:+.3f} / Signal {r.macd_signal:+.3f} / "
                f"Histogram {r.macd_histogram:+.3f}<br>"
            )
        details.append(
            f"<div style='background:#f9f9f9;border-left:4px solid "
            f"{'#1a7f37' if r.signal == 'BUY' else '#b42318' if r.signal == 'SELL' else '#6c6c6c'};"
            f"padding:12px;margin:12px 0;border-radius:4px;'>"
            f"<h3 style='margin:0 0 8px 0;'>{r.name} &mdash; {_badge(r.signal, r.signal_strength)}</h3>"
            f"<table style='font-size:13px;color:#444;border:none;' cellpadding='2'>"
            f"<tr><td style='width:120px;'><b>Current Price:</b></td>"
            f"<td>{_fmt(r.last)} {r.unit}</td></tr>"
            f"<tr><td><b>Trend:</b></td><td>{r.trend}</td></tr>"
            f"<tr><td><b>SMA20 / SMA50:</b></td>"
            f"<td>{_fmt(r.sma20)} / {_fmt(r.sma50)}</td></tr>"
            f"<tr><td><b>RSI(14):</b></td><td>{_fmt(r.rsi14, 0)}</td></tr>"
            f"</table>"
            f"<p style='margin:8px 0 4px 0;color:#444;font-size:13px;'>"
            f"{macd_info}"
            f"<b>Indicators:</b> {r.rationale}</p>"
            f"<p style='margin:4px 0;padding:8px;background:#fff;border-radius:4px;"
            f"font-size:13px;border:1px solid #e0e0e0;'>"
            f"<b>Strategy:</b> {r.strategy}</p>"
            f"<p style='margin:4px 0 0 0;color:#666;font-size:12px;'>"
            f"<b>1-Day Forecast:</b> {_fmt(r.forecast_1d)} "
            f"({_fmt(r.forecast_1d_pct, 2, '%')}) &nbsp;|&nbsp; "
            f"<b>7-Day Forecast:</b> {_fmt(r.forecast_7d)} "
            f"({_fmt(r.forecast_7d_pct, 2, '%')})</p>"
            f"</div>"
        )

    error_count = sum(1 for r in reports if r.error)
    status_note = ""
    if error_count > 0:
        status_note = (
            f"<p style='color:#b42318;font-weight:600;'>"
            f"Warning: {error_count} instrument(s) could not be fetched. "
            f"See details below.</p>"
        )

    buy_count = sum(1 for r in reports if not r.error and r.signal == "BUY")
    sell_count = sum(1 for r in reports if not r.error and r.signal == "SELL")
    hold_count = sum(1 for r in reports if not r.error and r.signal == "HOLD")
    market_sentiment = "Mixed"
    if buy_count > sell_count + hold_count:
        market_sentiment = "Bullish"
    elif sell_count > buy_count + hold_count:
        market_sentiment = "Bearish"
    elif buy_count == 0 and sell_count == 0:
        market_sentiment = "Neutral"

    return f"""\
<!doctype html>
<html><body style="font-family:Arial,Helvetica,sans-serif;color:#222;max-width:900px;margin:auto;padding:20px;">
  <h2 style="margin-bottom:0;color:#1a1a2e;">Daily Grain &amp; Livestock Report</h2>
  <p style="margin-top:4px;color:#666;">
    Generated {run_ts.strftime('%A, %B %d, %Y at %H:%M UTC')}
  </p>
  {weekend_note}
  {status_note}

  <div style="background:#f0f4ff;padding:12px;border-radius:8px;margin:16px 0;">
    <b>Market Overview:</b> {market_sentiment} &mdash;
    {buy_count} BUY | {hold_count} HOLD | {sell_count} SELL signals across all instruments
  </div>

  <h3 style="color:#1a1a2e;">Current Prices &amp; Signals</h3>
  <table cellpadding="6" cellspacing="0" border="0"
         style="border-collapse:collapse;width:100%;font-size:13px;">
    <thead>
      <tr style="background:#f2f2f2;">
        <th align="left">Instrument</th>
        <th align="right">Last</th>
        <th align="right">1d %</th>
        <th align="right">7d %</th>
        <th align="right">30d %</th>
        <th align="right">RSI</th>
        <th align="left">Trend</th>
        <th align="left">Signal</th>
      </tr>
    </thead>
    <tbody>{''.join(rows)}</tbody>
  </table>

  <h3 style="margin-top:24px;color:#1a1a2e;">Price Forecasts (1-Day &amp; 7-Day)</h3>
  <table cellpadding="6" cellspacing="0" border="0"
         style="border-collapse:collapse;width:100%;font-size:13px;">
    <thead>
      <tr style="background:#f2f2f2;">
        <th align="left">Instrument</th>
        <th align="right">Current</th>
        <th align="right">1-Day Target</th>
        <th align="right">1-Day Change</th>
        <th align="right">7-Day Target</th>
        <th align="right">7-Day Change</th>
      </tr>
    </thead>
    <tbody>{''.join(forecast_rows)}</tbody>
  </table>

  <h3 style="margin-top:24px;color:#1a1a2e;">Detailed Analysis &amp; Strategy</h3>
  {''.join(details)}

  <hr style="margin-top:24px;">
  <p style="color:#888;font-size:11px;">
    Prices are end-of-day from Yahoo Finance and may be delayed.
    Signals combine SMA(20/50) crossover, RSI(14), and MACD(12,26,9) indicators.
    Forecasts use linear regression on the last 20 trading days.
    This is an automated summary, <b>not financial advice</b>. Do your own research before trading.
    Sheep/Lamb uses the iPath Bloomberg Livestock ETN (COW) as a proxy
    because no public sheep futures contract is available on Yahoo Finance.
  </p>
</body></html>
"""


def render_text(reports: list[InstrumentReport], run_ts: datetime) -> str:
    is_weekend = run_ts.weekday() >= 5
    lines = [
        "=" * 70,
        "   DAILY GRAIN & LIVESTOCK MARKET REPORT",
        f"   Generated {run_ts.strftime('%A, %B %d, %Y at %H:%M UTC')}",
        "=" * 70,
        "",
    ]
    if is_weekend:
        lines.append("  ** Markets closed on weekends. Showing most recent trading day data. **")
        lines.append("")

    buy_count = sum(1 for r in reports if not r.error and r.signal == "BUY")
    sell_count = sum(1 for r in reports if not r.error and r.signal == "SELL")
    hold_count = sum(1 for r in reports if not r.error and r.signal == "HOLD")
    lines.append(f"  MARKET OVERVIEW: {buy_count} BUY | {hold_count} HOLD | {sell_count} SELL")
    lines.append("")

    lines.append("-" * 70)
    lines.append("  SECTION 1: CURRENT PRICES")
    lines.append("-" * 70)
    for r in reports:
        if r.error:
            lines.append(f"  {r.name} ({r.symbol}): DATA UNAVAILABLE - {r.error}")
            lines.append("")
            continue
        strength_label = f" ({r.signal_strength})" if r.signal_strength not in ("Neutral", "Low") else ""
        lines += [
            f"  {r.name} ({r.symbol})  [{r.signal}{strength_label}]",
            f"    Last Price: {_fmt(r.last)} {r.unit}",
            f"    Changes:    1d {_fmt(r.change_pct_1d, 2, '%')}  |  "
            f"7d {_fmt(r.change_pct_7d, 2, '%')}  |  "
            f"30d {_fmt(r.change_pct_30d, 2, '%')}",
            "",
        ]

    lines.append("-" * 70)
    lines.append("  SECTION 2: MARKET TRENDS & PREDICTIONS")
    lines.append("-" * 70)
    for r in reports:
        if r.error:
            continue
        macd_info = ""
        if r.macd_line is not None:
            macd_info = (
                f"    MACD:       Line {r.macd_line:+.3f} / Signal {r.macd_signal:+.3f} / "
                f"Hist {r.macd_histogram:+.3f}\n"
            )
        lines += [
            f"  {r.name}:",
            f"    Trend:      {r.trend}",
            f"    SMA20/50:   {_fmt(r.sma20)} / {_fmt(r.sma50)}",
            f"    RSI(14):    {_fmt(r.rsi14, 0)}",
        ]
        if macd_info:
            lines.append(macd_info.rstrip())
        lines += [
            f"    Indicators: {r.rationale}",
            "",
        ]

    lines.append("-" * 70)
    lines.append("  SECTION 3: BUY/SELL STRATEGY")
    lines.append("-" * 70)
    for r in reports:
        if r.error:
            continue
        strength_label = f" ({r.signal_strength})" if r.signal_strength not in ("Neutral", "Low") else ""
        lines += [
            f"  {r.name}  [{r.signal}{strength_label}]",
            f"    {r.strategy}",
            "",
        ]

    lines.append("-" * 70)
    lines.append("  SECTION 4: PRICE FORECASTS")
    lines.append("-" * 70)
    for r in reports:
        if r.error:
            continue
        fc1_dir = ""
        if r.forecast_1d_pct is not None:
            fc1_dir = f" ({r.forecast_1d_pct:+.2f}%)"
        fc7_dir = ""
        if r.forecast_7d_pct is not None:
            fc7_dir = f" ({r.forecast_7d_pct:+.2f}%)"
        lines += [
            f"  {r.name} (current: {_fmt(r.last)})",
            f"    1-Day Forecast: {_fmt(r.forecast_1d)}{fc1_dir}",
            f"    7-Day Forecast: {_fmt(r.forecast_7d)}{fc7_dir}",
            "",
        ]

    lines += [
        "=" * 70,
        "Prices from Yahoo Finance (end-of-day, may be delayed).",
        "Signals use SMA crossover + RSI + MACD rules. NOT financial advice.",
        "=" * 70,
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
