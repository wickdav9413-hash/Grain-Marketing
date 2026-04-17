"""
Daily Grain & Livestock Market Report.

Fetches recent prices for corn, soybeans, wheat, milk, live cattle (beef),
lean hogs, and sheep/lamb, computes simple technical indicators, generates
buy/sell signals and 1-/7-day forecasts, then emails the report.

Data sources (in priority order):
  1. Yahoo Finance chart API via requests (direct HTTP)
  2. yfinance library (fallback)

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

import json
import os
import smtplib
import ssl
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Optional
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pandas as pd

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
    trend: str
    signal: str
    rationale: str
    forecast_1d: Optional[float]
    forecast_7d: Optional[float]
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Data fetching — multiple strategies for resilience.
# ---------------------------------------------------------------------------
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


def _fetch_yahoo_chart(symbol: str, range_str: str = "6mo", interval: str = "1d") -> pd.Series:
    """Fetch closing prices directly from Yahoo Finance chart API."""
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?range={range_str}&interval={interval}&includePrePost=false"
    )
    req = Request(url, headers=_HEADERS)
    for attempt in range(3):
        try:
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            result = data["chart"]["result"][0]
            timestamps = result["timestamp"]
            closes = result["indicators"]["quote"][0]["close"]
            idx = pd.to_datetime(timestamps, unit="s", utc=True)
            series = pd.Series(closes, index=idx, name="Close", dtype=float).dropna()
            if series.empty:
                raise ValueError("empty close series")
            return series
        except (HTTPError, KeyError, TypeError, ValueError, Exception) as exc:
            if attempt < 2:
                time.sleep(1 * (attempt + 1))
                continue
            raise RuntimeError(f"Yahoo chart API failed after 3 attempts: {exc}") from exc
    raise RuntimeError("unreachable")


def _fetch_yfinance(symbol: str) -> pd.Series:
    """Fallback: use the yfinance library."""
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


def fetch_closes(symbol: str) -> pd.Series:
    """Try multiple data sources to get closing prices."""
    errors = []

    for fetcher_name, fetcher in [
        ("yahoo_chart_api", _fetch_yahoo_chart),
        ("yfinance", _fetch_yfinance),
    ]:
        try:
            return fetcher(symbol)
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


def _pct_change(series: pd.Series, days: int) -> Optional[float]:
    if len(series) <= days:
        return None
    old = float(series.iloc[-1 - days])
    new = float(series.iloc[-1])
    if old == 0 or pd.isna(old):
        return None
    return float((new - old) / old * 100)


def _linear_forecast(series: pd.Series, horizon_days: int) -> Optional[float]:
    """Linear-regression extrapolation over the last 20 closes."""
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
) -> tuple[str, str, str]:
    """Return (trend, signal, rationale) from mechanical rules."""
    reasons: list[str] = []

    if sma20 is not None and sma50 is not None:
        if last > sma20 > sma50:
            trend = "Uptrend"
            reasons.append("price > SMA20 > SMA50")
        elif last < sma20 < sma50:
            trend = "Downtrend"
            reasons.append("price < SMA20 < SMA50")
        elif last > sma50:
            trend = "Weak uptrend"
            reasons.append("price above SMA50")
        else:
            trend = "Weak downtrend"
            reasons.append("price below SMA50")
    else:
        trend = "Unknown"

    signal = "HOLD"
    if rsi is not None:
        if rsi < 30:
            signal = "BUY"
            reasons.append(f"RSI {rsi:.0f} (oversold)")
        elif rsi > 70:
            signal = "SELL"
            reasons.append(f"RSI {rsi:.0f} (overbought)")
        else:
            reasons.append(f"RSI {rsi:.0f} (neutral)")

    if signal == "HOLD" and sma20 is not None and sma50 is not None:
        if sma20 > sma50 and last > sma20:
            signal = "BUY"
            reasons.append("bullish SMA crossover with price above SMA20")
        elif sma20 < sma50 and last < sma20:
            signal = "SELL"
            reasons.append("bearish SMA crossover with price below SMA20")

    return trend, signal, "; ".join(reasons) if reasons else "insufficient data"


def analyze(name: str, symbol: str, unit: str) -> InstrumentReport:
    blank = InstrumentReport(
        name=name, symbol=symbol, unit=unit,
        last=None, prev_close=None,
        change_pct_1d=None, change_pct_7d=None, change_pct_30d=None,
        sma20=None, sma50=None, rsi14=None,
        trend="Unknown", signal="HOLD",
        rationale="no data", forecast_1d=None, forecast_7d=None,
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
    trend, signal, rationale = _derive_signal(last, sma20, sma50, rsi14)

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
        trend=trend,
        signal=signal,
        rationale=rationale,
        forecast_1d=_linear_forecast(closes, 1),
        forecast_7d=_linear_forecast(closes, 5),
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


def _signal_emoji(signal: str) -> str:
    return {"BUY": "[BUY]", "SELL": "[SELL]", "HOLD": "[HOLD]"}.get(signal, "[?]")


def render_html(reports: list[InstrumentReport], run_ts: datetime) -> str:
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
            f"<td>{_badge(r.signal)}</td>"
            f"<td style='text-align:right;'>{_fmt(r.forecast_1d)}<br>"
            f"<small>{_fmt(r.forecast_7d)} (7d)</small></td>"
            "</tr>"
        )

    details: list[str] = []
    for r in reports:
        if r.error:
            continue
        fc_1d_direction = ""
        if r.forecast_1d is not None and r.last is not None:
            fc_1d_direction = " (higher)" if r.forecast_1d > r.last else " (lower)"
        fc_7d_direction = ""
        if r.forecast_7d is not None and r.last is not None:
            fc_7d_direction = " (higher)" if r.forecast_7d > r.last else " (lower)"
        details.append(
            f"<h3 style='margin-bottom:4px;'>{r.name} &mdash; {_badge(r.signal)}</h3>"
            f"<p style='margin-top:0;color:#444;'>"
            f"<b>Trend:</b> {r.trend}<br>"
            f"<b>Rationale:</b> {r.rationale}<br>"
            f"<b>Last:</b> {_fmt(r.last)} {r.unit} &nbsp;"
            f"<b>SMA20:</b> {_fmt(r.sma20)} &nbsp;"
            f"<b>SMA50:</b> {_fmt(r.sma50)}<br>"
            f"<b>1-day forecast:</b> {_fmt(r.forecast_1d)}{fc_1d_direction} &nbsp;"
            f"<b>7-day forecast:</b> {_fmt(r.forecast_7d)}{fc_7d_direction}"
            f"</p>"
        )

    error_count = sum(1 for r in reports if r.error)
    status_note = ""
    if error_count > 0:
        status_note = (
            f"<p style='color:#b42318;font-weight:600;'>"
            f"Warning: {error_count} instrument(s) could not be fetched. "
            f"See details below.</p>"
        )

    return f"""\
<!doctype html>
<html><body style="font-family:Arial,Helvetica,sans-serif;color:#222;max-width:900px;margin:auto;">
  <h2 style="margin-bottom:0;">Daily Grain &amp; Livestock Report</h2>
  <p style="margin-top:4px;color:#666;">
    Generated {run_ts.strftime('%A, %B %d, %Y at %H:%M UTC')}
  </p>
  {status_note}

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
        <th align="right">Forecast</th>
      </tr>
    </thead>
    <tbody>{''.join(rows)}</tbody>
  </table>

  <h3 style="margin-top:24px;">Detailed Analysis &amp; Strategy</h3>
  {''.join(details)}

  <hr>
  <p style="color:#888;font-size:11px;">
    Prices are end-of-day from Yahoo Finance and may be delayed.
    Signals are based on simple SMA crossover and RSI(14) rules; forecasts are
    linear extrapolations of the last 20 closes. This is an automated
    summary, <b>not financial advice</b>. Do your own research before trading.
    Sheep/Lamb uses the iPath Bloomberg Livestock ETN (COW) as a proxy
    because no public sheep futures contract is available on Yahoo Finance.
  </p>
</body></html>
"""


def render_text(reports: list[InstrumentReport], run_ts: datetime) -> str:
    lines = [
        "=" * 60,
        "   Daily Grain & Livestock Report",
        f"   Generated {run_ts.strftime('%A, %B %d, %Y at %H:%M UTC')}",
        "=" * 60,
        "",
    ]
    for r in reports:
        if r.error:
            lines.append(f"  {r.name} ({r.symbol}): DATA UNAVAILABLE - {r.error}")
            lines.append("")
            continue
        fc_1d_dir = ""
        if r.forecast_1d is not None and r.last is not None:
            fc_1d_dir = " (higher)" if r.forecast_1d > r.last else " (lower)"
        fc_7d_dir = ""
        if r.forecast_7d is not None and r.last is not None:
            fc_7d_dir = " (higher)" if r.forecast_7d > r.last else " (lower)"
        lines += [
            f"--- {r.name} ({r.symbol}) {_signal_emoji(r.signal)} ---",
            f"  Last Price: {_fmt(r.last)} {r.unit}",
            f"  Changes:    1d {_fmt(r.change_pct_1d, 2, '%')}  |  "
            f"7d {_fmt(r.change_pct_7d, 2, '%')}  |  "
            f"30d {_fmt(r.change_pct_30d, 2, '%')}",
            f"  SMA20/50:   {_fmt(r.sma20)} / {_fmt(r.sma50)}",
            f"  RSI(14):    {_fmt(r.rsi14, 0)}",
            f"  Trend:      {r.trend}",
            f"  Signal:     {r.signal}  ({r.rationale})",
            f"  Forecast:   1d -> {_fmt(r.forecast_1d)}{fc_1d_dir}  |  "
            f"7d -> {_fmt(r.forecast_7d)}{fc_7d_dir}",
            "",
        ]
    lines += [
        "-" * 60,
        "Prices from Yahoo Finance (end-of-day, may be delayed).",
        "Signals use SMA crossover + RSI rules. NOT financial advice.",
        "-" * 60,
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Email.
# ---------------------------------------------------------------------------
def send_email(subject: str, text_body: str, html_body: str) -> None:
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("SMTP_USERNAME")
    password = os.environ.get("SMTP_PASSWORD")
    email_from = os.environ.get("EMAIL_FROM", username)
    email_to = os.environ.get("EMAIL_TO", "wickdav9413@gmail.com")

    if not username or not password:
        raise RuntimeError(
            "SMTP_USERNAME and SMTP_PASSWORD must be set (use a Gmail App Password)."
        )

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

    reports = []
    for name, symbol, unit in INSTRUMENTS:
        print(f"  Fetching {name} ({symbol})...", end=" ", flush=True)
        report = analyze(name, symbol, unit)
        if report.error:
            print(f"FAILED: {report.error}")
        else:
            print(f"OK - {_fmt(report.last)} {unit}")
        reports.append(report)

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

    send_email(subject, text_body, html_body)
    print("\nEmail sent successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
