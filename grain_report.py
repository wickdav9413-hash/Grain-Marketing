"""
Daily Grain & Livestock Market Report.

Fetches recent prices for corn, soybeans, wheat, milk, live cattle (beef),
lean hogs, and sheep/lamb, computes simple technical indicators, generates
buy/sell signals and 1-/7-day forecasts, then emails the report.

Data source:  Yahoo Finance (via yfinance) - delayed/end-of-day futures quotes.
Email:        SMTP (Gmail app password expected).

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

import os
import smtplib
import ssl
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Optional

import pandas as pd
import yfinance as yf


# ---------------------------------------------------------------------------
# Instruments to track.
# ---------------------------------------------------------------------------
# Note on sheep: there is no actively-traded sheep/lamb futures contract on
# Yahoo Finance. We use the iShares MSCI New Zealand ETF as a rough proxy for
# the New Zealand lamb-export economy; it is imperfect but the closest public
# free data we can pull without a paid subscription. The report clearly labels
# it as a proxy so the reader is not misled.
INSTRUMENTS: list[tuple[str, str, str]] = [
    ("Corn",           "ZC=F", "USD / bushel"),
    ("Soybeans",       "ZS=F", "USD / bushel"),
    ("Wheat",          "ZW=F", "USD / bushel"),
    ("Class III Milk", "DC=F", "USD / cwt"),
    ("Live Cattle (Beef)", "LE=F", "USD / lb"),
    ("Lean Hogs",      "HE=F", "USD / lb"),
    ("Sheep/Lamb (NZ proxy)", "ENZL", "USD / share"),
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
    old = series.iloc[-1 - days]
    new = series.iloc[-1]
    if old == 0 or pd.isna(old):
        return None
    return float((new - old) / old * 100)


def _linear_forecast(series: pd.Series, horizon_days: int) -> Optional[float]:
    """Linear-regression extrapolation over the last 20 closes."""
    window = series.dropna().tail(20)
    if len(window) < 5:
        return None
    x = pd.Series(range(len(window)), index=window.index, dtype=float)
    # slope via least squares
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

    # Trend
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

    # Signal
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
        hist = yf.Ticker(symbol).history(period="6mo", interval="1d", auto_adjust=False)
    except Exception as exc:  # noqa: BLE001
        blank.error = f"fetch failed: {exc}"
        return blank

    if hist is None or hist.empty or "Close" not in hist.columns:
        blank.error = "no history returned"
        return blank

    closes = hist["Close"].dropna()
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
        change_pct_7d=_pct_change(closes, 5),   # 5 trading days ~ 1 week
        change_pct_30d=_pct_change(closes, 21), # ~1 month of trading days
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


def render_html(reports: list[InstrumentReport], run_ts: datetime) -> str:
    rows: list[str] = []
    for r in reports:
        if r.error:
            rows.append(
                f"<tr><td><b>{r.name}</b><br><small>{r.symbol}</small></td>"
                f"<td colspan=8 style='color:#b42318;'>Data unavailable: {r.error}</td></tr>"
            )
            continue
        rows.append(
            "<tr>"
            f"<td><b>{r.name}</b><br><small>{r.symbol} &middot; {r.unit}</small></td>"
            f"<td style='text-align:right;'>{_fmt(r.last)}</td>"
            f"<td style='text-align:right;'>{_fmt(r.change_pct_1d, 2, '%')}</td>"
            f"<td style='text-align:right;'>{_fmt(r.change_pct_7d, 2, '%')}</td>"
            f"<td style='text-align:right;'>{_fmt(r.change_pct_30d, 2, '%')}</td>"
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
        details.append(
            f"<h3 style='margin-bottom:4px;'>{r.name} &mdash; {_badge(r.signal)}</h3>"
            f"<p style='margin-top:0;color:#444;'>"
            f"<b>Trend:</b> {r.trend}<br>"
            f"<b>Rationale:</b> {r.rationale}<br>"
            f"<b>Last:</b> {_fmt(r.last)} {r.unit} &nbsp;"
            f"<b>SMA20:</b> {_fmt(r.sma20)} &nbsp;"
            f"<b>SMA50:</b> {_fmt(r.sma50)}<br>"
            f"<b>1-day forecast:</b> {_fmt(r.forecast_1d)} &nbsp;"
            f"<b>7-day forecast:</b> {_fmt(r.forecast_7d)}"
            f"</p>"
        )

    return f"""\
<!doctype html>
<html><body style="font-family:Arial,Helvetica,sans-serif;color:#222;">
  <h2 style="margin-bottom:0;">Daily Grain &amp; Livestock Report</h2>
  <p style="margin-top:4px;color:#666;">
    Generated {run_ts.strftime('%Y-%m-%d %H:%M UTC')}
  </p>

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

  <h3 style="margin-top:24px;">Commentary &amp; Strategy</h3>
  {''.join(details)}

  <hr>
  <p style="color:#888;font-size:11px;">
    Prices are end-of-day from Yahoo Finance and may be delayed.
    Signals come from simple moving-average / RSI rules; forecasts are
    linear extrapolations of the last 20 closes. This is an automated
    summary, not financial advice. Do your own research before trading.
    Sheep/Lamb uses the iShares MSCI New Zealand ETF (ENZL) as a proxy
    because no public sheep futures contract is available.
  </p>
</body></html>
"""


def render_text(reports: list[InstrumentReport], run_ts: datetime) -> str:
    lines = [
        "Daily Grain & Livestock Report",
        f"Generated {run_ts.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]
    for r in reports:
        if r.error:
            lines.append(f"{r.name} ({r.symbol}): data unavailable - {r.error}")
            continue
        lines += [
            f"== {r.name} ({r.symbol}) ==",
            f"  Last:       {_fmt(r.last)} {r.unit}",
            f"  Changes:    1d {_fmt(r.change_pct_1d, 2, '%')}  "
            f"7d {_fmt(r.change_pct_7d, 2, '%')}  "
            f"30d {_fmt(r.change_pct_30d, 2, '%')}",
            f"  SMA20/50:   {_fmt(r.sma20)} / {_fmt(r.sma50)}",
            f"  RSI(14):    {_fmt(r.rsi14, 0)}",
            f"  Trend:      {r.trend}",
            f"  Signal:     {r.signal}  ({r.rationale})",
            f"  Forecast:   1d {_fmt(r.forecast_1d)}  7d {_fmt(r.forecast_7d)}",
            "",
        ]
    lines.append(
        "Automated summary from Yahoo Finance end-of-day data. "
        "Not financial advice."
    )
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
    reports = [analyze(name, symbol, unit) for name, symbol, unit in INSTRUMENTS]

    text_body = render_text(reports, run_ts)
    html_body = render_html(reports, run_ts)
    subject = f"Grain & Livestock Report - {run_ts.strftime('%Y-%m-%d')}"

    print(text_body)

    if os.environ.get("DRY_RUN", "").lower() in {"1", "true", "yes"}:
        print("\nDRY_RUN set; skipping email send.")
        return 0

    send_email(subject, text_body, html_body)
    print("\nEmail sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
