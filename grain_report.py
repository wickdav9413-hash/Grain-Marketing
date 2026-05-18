"""
Daily Grain & Livestock Market Report.

Fetches recent prices for corn, soybeans, wheat, milk, live cattle (beef),
lean hogs, and sheep/lamb, computes technical indicators (SMA, RSI, MACD),
generates buy/sell signals and 1-/7-day forecasts, then emails the report.

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
simple moving-average / momentum rules. Always do your own research.
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
) -> tuple[str, str, str, str, str]:
    """Return (trend, signal, strength, rationale, strategy)."""
    reasons: list[str] = []
    bullish_points = 0
    bearish_points = 0

    if sma20 is not None and sma50 is not None:
        if last > sma20 > sma50:
            trend = "Uptrend"
            reasons.append("price > SMA20 > SMA50")
            bullish_points += 2
        elif last < sma20 < sma50:
            trend = "Downtrend"
            reasons.append("price < SMA20 < SMA50")
            bearish_points += 2
        elif last > sma50:
            trend = "Weak uptrend"
            reasons.append("price above SMA50")
            bullish_points += 1
        else:
            trend = "Weak downtrend"
            reasons.append("price below SMA50")
            bearish_points += 1
    else:
        trend = "Unknown"

    if rsi is not None:
        if rsi < 30:
            bullish_points += 2
            reasons.append(f"RSI {rsi:.0f} (oversold)")
        elif rsi < 40:
            bullish_points += 1
            reasons.append(f"RSI {rsi:.0f} (approaching oversold)")
        elif rsi > 70:
            bearish_points += 2
            reasons.append(f"RSI {rsi:.0f} (overbought)")
        elif rsi > 60:
            bearish_points += 1
            reasons.append(f"RSI {rsi:.0f} (approaching overbought)")
        else:
            reasons.append(f"RSI {rsi:.0f} (neutral)")

    if macd_hist is not None:
        if macd_hist > 0:
            bullish_points += 1
            reasons.append("MACD histogram positive (bullish momentum)")
        else:
            bearish_points += 1
            reasons.append("MACD histogram negative (bearish momentum)")

    score = bullish_points - bearish_points
    if score >= 3:
        signal = "BUY"
        strength = "Strong"
    elif score >= 1:
        signal = "BUY"
        strength = "Moderate"
    elif score <= -3:
        signal = "SELL"
        strength = "Strong"
    elif score <= -1:
        signal = "SELL"
        strength = "Moderate"
    else:
        signal = "HOLD"
        strength = "Neutral"

    strategy = _build_strategy(signal, strength, trend, rsi, macd_hist, last, sma20, sma50)

    return trend, signal, strength, "; ".join(reasons) if reasons else "insufficient data", strategy


def _build_strategy(
    signal: str,
    strength: str,
    trend: str,
    rsi: Optional[float],
    macd_hist: Optional[float],
    last: float,
    sma20: Optional[float],
    sma50: Optional[float],
) -> str:
    parts: list[str] = []

    if signal == "BUY":
        if strength == "Strong":
            parts.append(
                "STRONG BUY signal. Multiple indicators align bullish. "
                "Consider entering long positions or adding to existing ones."
            )
        else:
            parts.append(
                "Moderate BUY signal. Conditions are leaning bullish but not unanimous. "
                "Consider scaling into a position rather than going all-in."
            )
        if sma20 is not None and last < sma20:
            parts.append(f"Price is below the 20-day SMA ({sma20:.2f}), which may act as near-term resistance.")
        if rsi is not None and rsi < 35:
            parts.append("RSI is in oversold territory, suggesting a potential bounce is due.")

    elif signal == "SELL":
        if strength == "Strong":
            parts.append(
                "STRONG SELL signal. Multiple indicators align bearish. "
                "Consider reducing exposure or hedging with puts/shorts."
            )
        else:
            parts.append(
                "Moderate SELL signal. Conditions are leaning bearish. "
                "Consider tightening stops or scaling out of long positions."
            )
        if sma50 is not None and last > sma50:
            parts.append(f"Price is still above the 50-day SMA ({sma50:.2f}), so the longer-term trend hasn't fully broken.")
        if rsi is not None and rsi > 65:
            parts.append("RSI is elevated, suggesting upside momentum is fading.")

    else:
        parts.append(
            "HOLD / wait for clearer direction. Indicators are mixed. "
            "Avoid new entries until a stronger signal emerges."
        )
        if sma20 is not None and sma50 is not None:
            gap_pct = abs(sma20 - sma50) / sma50 * 100
            if gap_pct < 1.5:
                parts.append(
                    "SMA20 and SMA50 are very close together, suggesting a potential breakout or breakdown is near."
                )

    if macd_hist is not None:
        if abs(macd_hist) < 0.5:
            parts.append("MACD is near the zero line; watch for a crossover to confirm the next move.")

    return " ".join(parts)


def analyze(name: str, symbol: str, unit: str) -> InstrumentReport:
    blank = InstrumentReport(
        name=name, symbol=symbol, unit=unit,
        last=None, prev_close=None,
        change_pct_1d=None, change_pct_7d=None, change_pct_30d=None,
        sma20=None, sma50=None, rsi14=None,
        macd_line=None, macd_signal=None, macd_histogram=None,
        trend="Unknown", signal="HOLD", signal_strength="Neutral",
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
    trend, signal, strength, rationale = _derive_signal(last, sma20, sma50, rsi14, macd_h)[:4]
    trend, signal, strength, rationale, strategy = _derive_signal(last, sma20, sma50, rsi14, macd_h)

    fc_1d = _linear_forecast(closes, 1)
    fc_7d = _linear_forecast(closes, 5)
    fc_1d_pct = ((fc_1d - last) / last * 100) if fc_1d is not None and last else None
    fc_7d_pct = ((fc_7d - last) / last * 100) if fc_7d is not None and last else None

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


def _badge(signal: str) -> str:
    colour = {"BUY": "#1a7f37", "SELL": "#b42318", "HOLD": "#6c6c6c"}.get(signal, "#6c6c6c")
    return (
        f'<span style="background:{colour};color:#fff;padding:2px 8px;'
        f'border-radius:10px;font-weight:600;font-size:12px;">{signal}</span>'
    )


def _strength_color(strength: str) -> str:
    return {"Strong": "#d4380d", "Moderate": "#d48806", "Neutral": "#6c6c6c"}.get(strength, "#6c6c6c")


def _signal_emoji(signal: str) -> str:
    return {"BUY": "[BUY]", "SELL": "[SELL]", "HOLD": "[HOLD]"}.get(signal, "[?]")


def _arrow(pct: Optional[float]) -> str:
    if pct is None:
        return ""
    return "&#9650;" if pct >= 0 else "&#9660;"


def render_html(reports: list[InstrumentReport], run_ts: datetime) -> str:
    buy_count = sum(1 for r in reports if r.signal == "BUY" and not r.error)
    sell_count = sum(1 for r in reports if r.signal == "SELL" and not r.error)
    hold_count = sum(1 for r in reports if r.signal == "HOLD" and not r.error)
    error_count = sum(1 for r in reports if r.error)

    market_mood = "Mixed"
    mood_color = "#6c6c6c"
    if buy_count > sell_count + hold_count:
        market_mood = "Bullish"
        mood_color = "#1a7f37"
    elif sell_count > buy_count + hold_count:
        market_mood = "Bearish"
        mood_color = "#b42318"
    elif buy_count > sell_count:
        market_mood = "Leaning Bullish"
        mood_color = "#389e0d"
    elif sell_count > buy_count:
        market_mood = "Leaning Bearish"
        mood_color = "#cf1322"

    summary_html = f"""
    <div style="background:#f8f9fa;border-radius:8px;padding:16px;margin-bottom:20px;border-left:4px solid {mood_color};">
      <h3 style="margin:0 0 8px 0;color:{mood_color};">Market Overview: {market_mood}</h3>
      <p style="margin:0;color:#444;font-size:14px;">
        <b>{buy_count}</b> BUY &nbsp;|&nbsp; <b>{hold_count}</b> HOLD &nbsp;|&nbsp; <b>{sell_count}</b> SELL
        {f' &nbsp;|&nbsp; <span style="color:#b42318;">{error_count} unavailable</span>' if error_count else ''}
      </p>
    </div>
    """

    rows: list[str] = []
    for r in reports:
        if r.error:
            rows.append(
                f"<tr><td><b>{r.name}</b><br><small>{r.symbol}</small></td>"
                f"<td colspan='9' style='color:#b42318;'>Data unavailable: {r.error}</td></tr>"
            )
            continue
        chg_color_1d = "#1a7f37" if (r.change_pct_1d or 0) >= 0 else "#b42318"
        chg_color_7d = "#1a7f37" if (r.change_pct_7d or 0) >= 0 else "#b42318"
        fc_1d_color = "#1a7f37" if (r.forecast_1d_pct or 0) >= 0 else "#b42318"
        fc_7d_color = "#1a7f37" if (r.forecast_7d_pct or 0) >= 0 else "#b42318"
        rows.append(
            "<tr>"
            f"<td><b>{r.name}</b><br><small>{r.symbol} &middot; {r.unit}</small></td>"
            f"<td style='text-align:right;font-weight:600;'>{_fmt(r.last)}</td>"
            f"<td style='text-align:right;color:{chg_color_1d};'>{_fmt(r.change_pct_1d, 2, '%')}</td>"
            f"<td style='text-align:right;color:{chg_color_7d};'>{_fmt(r.change_pct_7d, 2, '%')}</td>"
            f"<td style='text-align:right;'>{_fmt(r.rsi14, 0)}</td>"
            f"<td>{r.trend}</td>"
            f"<td>{_badge(r.signal)}<br>"
            f"<small style='color:{_strength_color(r.signal_strength)};'>{r.signal_strength}</small></td>"
            f"<td style='text-align:right;color:{fc_1d_color};'>"
            f"{_arrow(r.forecast_1d_pct)} {_fmt(r.forecast_1d)}<br>"
            f"<small>({_fmt(r.forecast_1d_pct, 2, '%')})</small></td>"
            f"<td style='text-align:right;color:{fc_7d_color};'>"
            f"{_arrow(r.forecast_7d_pct)} {_fmt(r.forecast_7d)}<br>"
            f"<small>({_fmt(r.forecast_7d_pct, 2, '%')})</small></td>"
            "</tr>"
        )

    details: list[str] = []
    for r in reports:
        if r.error:
            continue

        signal_bg = {"BUY": "#f6ffed", "SELL": "#fff2f0", "HOLD": "#fafafa"}.get(r.signal, "#fafafa")
        signal_border = {"BUY": "#b7eb8f", "SELL": "#ffa39e", "HOLD": "#d9d9d9"}.get(r.signal, "#d9d9d9")

        fc_1d_dir = ""
        if r.forecast_1d_pct is not None:
            fc_1d_dir = f" ({'+' if r.forecast_1d_pct >= 0 else ''}{r.forecast_1d_pct:.2f}%)"
        fc_7d_dir = ""
        if r.forecast_7d_pct is not None:
            fc_7d_dir = f" ({'+' if r.forecast_7d_pct >= 0 else ''}{r.forecast_7d_pct:.2f}%)"

        details.append(
            f"<div style='background:{signal_bg};border:1px solid {signal_border};border-radius:8px;"
            f"padding:12px 16px;margin-bottom:12px;'>"
            f"<h3 style='margin:0 0 8px 0;'>{r.name} &mdash; {_badge(r.signal)} "
            f"<small style='color:{_strength_color(r.signal_strength)};'>{r.signal_strength}</small></h3>"
            f"<table cellpadding='4' cellspacing='0' style='font-size:13px;'>"
            f"<tr><td><b>Current Price:</b></td><td>{_fmt(r.last)} {r.unit}</td>"
            f"<td width='20'></td>"
            f"<td><b>SMA20:</b></td><td>{_fmt(r.sma20)}</td>"
            f"<td width='20'></td>"
            f"<td><b>SMA50:</b></td><td>{_fmt(r.sma50)}</td></tr>"
            f"<tr><td><b>RSI(14):</b></td><td>{_fmt(r.rsi14, 0)}</td>"
            f"<td></td>"
            f"<td><b>MACD:</b></td><td>{_fmt(r.macd_line, 4)}</td>"
            f"<td></td>"
            f"<td><b>MACD Signal:</b></td><td>{_fmt(r.macd_signal, 4)}</td></tr>"
            f"</table>"
            f"<p style='margin:8px 0 4px 0;'><b>Trend:</b> {r.trend}</p>"
            f"<p style='margin:4px 0;'><b>Rationale:</b> {r.rationale}</p>"
            f"<div style='background:#fff;border-radius:4px;padding:8px 12px;margin:8px 0;'>"
            f"<b>1-Day Forecast:</b> {_fmt(r.forecast_1d)}{fc_1d_dir}<br>"
            f"<b>7-Day Forecast:</b> {_fmt(r.forecast_7d)}{fc_7d_dir}"
            f"</div>"
            f"<p style='margin:4px 0 0 0;color:#333;'><b>Strategy:</b> {r.strategy}</p>"
            f"</div>"
        )

    status_note = ""
    if error_count > 0:
        status_note = (
            f"<p style='color:#b42318;font-weight:600;'>"
            f"Warning: {error_count} instrument(s) could not be fetched. "
            f"See details below.</p>"
        )

    return f"""\
<!doctype html>
<html><body style="font-family:Arial,Helvetica,sans-serif;color:#222;max-width:900px;margin:auto;padding:16px;">
  <h2 style="margin-bottom:0;">Daily Grain &amp; Livestock Market Report</h2>
  <p style="margin-top:4px;color:#666;">
    Generated {run_ts.strftime('%A, %B %d, %Y at %H:%M UTC')}
  </p>
  {status_note}
  {summary_html}

  <h3>Price Summary &amp; Signals</h3>
  <table cellpadding="6" cellspacing="0" border="0"
         style="border-collapse:collapse;width:100%;font-size:13px;">
    <thead>
      <tr style="background:#f2f2f2;">
        <th align="left">Instrument</th>
        <th align="right">Last</th>
        <th align="right">1d %</th>
        <th align="right">7d %</th>
        <th align="right">RSI</th>
        <th align="left">Trend</th>
        <th align="left">Signal</th>
        <th align="right">1d Forecast</th>
        <th align="right">7d Forecast</th>
      </tr>
    </thead>
    <tbody>{''.join(rows)}</tbody>
  </table>

  <h3 style="margin-top:24px;">Detailed Analysis, Forecasts &amp; Strategy</h3>
  {''.join(details)}

  <hr>
  <p style="color:#888;font-size:11px;">
    Prices are end-of-day from Yahoo Finance and may be delayed.
    Signals are based on SMA crossover, RSI(14), and MACD rules; forecasts are
    linear extrapolations of the last 20 closes. This is an automated
    summary, <b>not financial advice</b>. Do your own research before trading.
    Sheep/Lamb uses the iPath Bloomberg Livestock ETN (COW) as a proxy
    because no public sheep futures contract is available on Yahoo Finance.
  </p>
</body></html>
"""


def render_text(reports: list[InstrumentReport], run_ts: datetime) -> str:
    buy_count = sum(1 for r in reports if r.signal == "BUY" and not r.error)
    sell_count = sum(1 for r in reports if r.signal == "SELL" and not r.error)
    hold_count = sum(1 for r in reports if r.signal == "HOLD" and not r.error)

    lines = [
        "=" * 65,
        "   Daily Grain & Livestock Market Report",
        f"   Generated {run_ts.strftime('%A, %B %d, %Y at %H:%M UTC')}",
        "=" * 65,
        "",
        f"   MARKET OVERVIEW: BUY={buy_count}  HOLD={hold_count}  SELL={sell_count}",
        "",
    ]
    for r in reports:
        if r.error:
            lines.append(f"  {r.name} ({r.symbol}): DATA UNAVAILABLE - {r.error}")
            lines.append("")
            continue

        fc_1d_dir = ""
        if r.forecast_1d_pct is not None:
            fc_1d_dir = f" ({'+' if r.forecast_1d_pct >= 0 else ''}{r.forecast_1d_pct:.2f}%)"
        fc_7d_dir = ""
        if r.forecast_7d_pct is not None:
            fc_7d_dir = f" ({'+' if r.forecast_7d_pct >= 0 else ''}{r.forecast_7d_pct:.2f}%)"

        lines += [
            f"--- {r.name} ({r.symbol}) {_signal_emoji(r.signal)} {r.signal_strength} ---",
            f"  Last Price:   {_fmt(r.last)} {r.unit}",
            f"  Changes:      1d {_fmt(r.change_pct_1d, 2, '%')}  |  "
            f"7d {_fmt(r.change_pct_7d, 2, '%')}  |  "
            f"30d {_fmt(r.change_pct_30d, 2, '%')}",
            f"  SMA20/50:     {_fmt(r.sma20)} / {_fmt(r.sma50)}",
            f"  RSI(14):      {_fmt(r.rsi14, 0)}",
            f"  MACD:         {_fmt(r.macd_line, 4)}  Signal: {_fmt(r.macd_signal, 4)}  Hist: {_fmt(r.macd_histogram, 4)}",
            f"  Trend:        {r.trend}",
            f"  Signal:       {r.signal} ({r.signal_strength})  -- {r.rationale}",
            f"  1-Day Fcst:   {_fmt(r.forecast_1d)}{fc_1d_dir}",
            f"  7-Day Fcst:   {_fmt(r.forecast_7d)}{fc_7d_dir}",
            f"  Strategy:     {r.strategy}",
            "",
        ]
    lines += [
        "-" * 65,
        "Prices from Yahoo Finance (end-of-day, may be delayed).",
        "Signals use SMA crossover + RSI + MACD rules. NOT financial advice.",
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
