"""
Daily Grain & Livestock Market Report with Charts & Weather.

Fetches prices for corn, soybeans, wheat, milk, live cattle (beef),
lean hogs, and feeder cattle. Computes 20-day SMA and Stochastic
Oscillator, generates buy/sell signals with embedded charts, adds a
7-day agricultural weather forecast, and emails the report.

Signal rules (user-specified):
  BUY:  price > 20-day SMA AND Stochastic %K > 75%
  SELL: price < 20-day SMA AND Stochastic %K < 25%
  HOLD: otherwise

Data: Yahoo Finance (free, end-of-day, possibly delayed).
Weather: Open-Meteo API (free, no key required).
Email: SMTP (Gmail App Password expected).

Environment variables:
    SMTP_HOST        default smtp.gmail.com
    SMTP_PORT        default 587
    SMTP_USERNAME    Gmail address (required)
    SMTP_PASSWORD    Gmail App Password (required)
    EMAIL_FROM       default SMTP_USERNAME
    EMAIL_TO         default wickdav9413@gmail.com
    WEATHER_LAT      default 41.59 (Des Moines, IA)
    WEATHER_LON      default -93.62
    WEATHER_LABEL    default "Des Moines, IA (Corn Belt)"

NOT financial advice. Signals are mechanical. Do your own research.
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
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd
import requests as _requests

# ── Instruments ──────────────────────────────────────────────────────────────

INSTRUMENTS: list[tuple[str, str, str]] = [
    ("Corn",               "ZC=F",  "USD / bushel"),
    ("Soybeans",           "ZS=F",  "USD / bushel"),
    ("Wheat",              "ZW=F",  "USD / bushel"),
    ("Class III Milk",     "DC=F",  "USD / cwt"),
    ("Live Cattle (Beef)", "LE=F",  "USD / lb"),
    ("Lean Hogs",          "HE=F",  "USD / lb"),
    ("Feeder Cattle",      "GF=F",  "USD / lb"),
]

SHEEP_NOTE = (
    "Sheep/Lamb: No publicly traded futures contract on major exchanges. "
    "For current lamb prices see the USDA National Direct Sheep Report at "
    "https://mymarketnews.ams.usda.gov/viewReport/2907 and weekly summary at "
    "https://www.ams.usda.gov/mnreports/lswlamb.pdf"
)

# ── Weather config ───────────────────────────────────────────────────────────

WEATHER_LAT = float(os.environ.get("WEATHER_LAT", "41.59"))
WEATHER_LON = float(os.environ.get("WEATHER_LON", "-93.62"))
WEATHER_LABEL = os.environ.get("WEATHER_LABEL", "Des Moines, IA (Corn Belt)")

WMO_WEATHER_CODES = {
    0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Dense drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Rain showers", 81: "Mod. rain showers", 82: "Heavy showers",
    85: "Light snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "T-storm w/ hail", 99: "T-storm w/ heavy hail",
}

# ── Data model ───────────────────────────────────────────────────────────────


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
    stoch_k: Optional[float]
    stoch_d: Optional[float]
    trend: str
    signal: str
    rationale: str
    forecast_1d: Optional[float]
    forecast_7d: Optional[float]
    chart_png: Optional[bytes] = None
    error: Optional[str] = None


# ── Yahoo Finance session ────────────────────────────────────────────────────

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
                "https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=10,
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
            raise RuntimeError(f"Yahoo session init failed: {exc}") from exc
    raise RuntimeError("unreachable")


# ── Data fetching (returns DataFrame with High, Low, Close) ──────────────────


def _fetch_yahoo_chart(symbol: str, range_str: str = "6mo", interval: str = "1d") -> pd.DataFrame:
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
            quote = result["indicators"]["quote"][0]
            idx = pd.to_datetime(timestamps, unit="s", utc=True)
            df = pd.DataFrame(
                {"High": quote.get("high"), "Low": quote.get("low"), "Close": quote.get("close")},
                index=idx,
            ).dropna()
            if df.empty:
                raise ValueError("empty OHLC data")
            return df
        except Exception as exc:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"Yahoo chart API failed: {exc}") from exc
    raise RuntimeError("unreachable")


def _fetch_yfinance(symbol: str) -> pd.DataFrame:
    import yfinance as yf
    ticker = yf.Ticker(symbol)
    hist = ticker.history(period="6mo", interval="1d")
    if hist is None or hist.empty:
        raise RuntimeError("yfinance returned no data")
    for col in ("High", "Low", "Close"):
        if col not in hist.columns:
            raise RuntimeError(f"yfinance missing {col} column")
    return hist[["High", "Low", "Close"]].dropna()


def _fetch_yahoo_csv(symbol: str) -> pd.DataFrame:
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
            df = df.set_index("Date").sort_index()
            for col in ("High", "Low", "Close"):
                if col not in df.columns:
                    raise RuntimeError(f"CSV missing {col}")
            return df[["High", "Low", "Close"]].dropna()
        except Exception as exc:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"Yahoo CSV failed: {exc}") from exc
    raise RuntimeError("unreachable")


def fetch_ohlc(symbol: str) -> pd.DataFrame:
    errors = []
    for fetcher_name, fetcher in [
        ("yahoo_chart_api", _fetch_yahoo_chart),
        ("yfinance", _fetch_yfinance),
        ("yahoo_csv", _fetch_yahoo_csv),
    ]:
        try:
            df = fetcher(symbol)
            print(f"[{fetcher_name}]", end=" ", flush=True)
            return df
        except Exception as exc:
            errors.append(f"{fetcher_name}: {exc}")
    raise RuntimeError(" | ".join(errors))


# ── Indicators ───────────────────────────────────────────────────────────────


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


def _stochastic(
    df: pd.DataFrame, k_period: int = 14, d_period: int = 3,
) -> tuple[Optional[float], Optional[float], pd.Series, pd.Series]:
    """Stochastic Oscillator %K and %D from High/Low/Close data."""
    if len(df) < k_period:
        return None, None, pd.Series(dtype=float), pd.Series(dtype=float)
    low_min = df["Low"].rolling(k_period).min()
    high_max = df["High"].rolling(k_period).max()
    denom = high_max - low_min
    denom = denom.replace(0, float("nan"))
    k = 100 * (df["Close"] - low_min) / denom
    d = k.rolling(d_period).mean()
    last_k = float(k.iloc[-1]) if pd.notna(k.iloc[-1]) else None
    last_d = float(d.iloc[-1]) if pd.notna(d.iloc[-1]) else None
    return last_k, last_d, k, d


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
    return float(intercept + slope * (len(window) - 1 + horizon_days))


# ── Signal logic ─────────────────────────────────────────────────────────────


def _derive_signal(
    last: float,
    sma20: Optional[float],
    sma50: Optional[float],
    rsi: Optional[float],
    stoch_k: Optional[float],
) -> tuple[str, str, str]:
    """BUY if price > SMA20 and Stoch %K > 75; SELL if price < SMA20 and Stoch %K < 25."""
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
    elif sma20 is not None:
        trend = "Above SMA20" if last > sma20 else "Below SMA20"
        reasons.append(f"price {'above' if last > sma20 else 'below'} SMA20")
    else:
        trend = "Unknown"

    above_sma20 = sma20 is not None and last > sma20
    below_sma20 = sma20 is not None and last < sma20
    signal = "HOLD"

    if above_sma20 and stoch_k is not None and stoch_k > 75:
        signal = "BUY"
        reasons.append(f"price above SMA20 & Stoch %K={stoch_k:.0f}% > 75%")
    elif below_sma20 and stoch_k is not None and stoch_k < 25:
        signal = "SELL"
        reasons.append(f"price below SMA20 & Stoch %K={stoch_k:.0f}% < 25%")
    else:
        if stoch_k is not None:
            reasons.append(f"Stoch %K={stoch_k:.0f}%")
        if above_sma20:
            reasons.append("above SMA20 but stoch not > 75%")
        elif below_sma20:
            reasons.append("below SMA20 but stoch not < 25%")

    if rsi is not None:
        label = " (oversold)" if rsi < 30 else " (overbought)" if rsi > 70 else ""
        reasons.append(f"RSI {rsi:.0f}{label}")

    return trend, signal, "; ".join(reasons) if reasons else "insufficient data"


# ── Chart generation ─────────────────────────────────────────────────────────


def _generate_chart(
    df: pd.DataFrame,
    name: str,
    symbol: str,
    signal: str,
    sma20_series: pd.Series,
    stoch_k_series: pd.Series,
    stoch_d_series: pd.Series,
) -> bytes:
    plot_df = df.tail(60).copy()
    plot_sma = sma20_series.reindex(plot_df.index)
    plot_k = stoch_k_series.reindex(plot_df.index)
    plot_d = stoch_d_series.reindex(plot_df.index)

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(8, 5), sharex=True, constrained_layout=True,
        gridspec_kw={"height_ratios": [3, 1], "hspace": 0.08},
    )

    sig_color = {"BUY": "#1a7f37", "SELL": "#b42318", "HOLD": "#555555"}.get(signal, "#555")

    # Price + SMA20
    ax1.plot(plot_df.index, plot_df["Close"], color="#1f77b4", linewidth=1.5, label="Close")
    ax1.plot(plot_df.index, plot_sma, color="#ff7f0e", linewidth=1.2, linestyle="--", label="SMA20")
    ax1.set_title(f"{name} ({symbol}) — {signal}", fontsize=13, fontweight="bold", color=sig_color, loc="left")
    ax1.set_ylabel("Price", fontsize=10)
    ax1.legend(loc="upper left", fontsize=9, framealpha=0.8)
    ax1.grid(True, alpha=0.3)
    ax1.tick_params(axis="y", labelsize=9)

    last_price = plot_df["Close"].iloc[-1]
    ax1.annotate(
        f"{last_price:,.2f}", xy=(plot_df.index[-1], last_price),
        fontsize=9, fontweight="bold", color="#1f77b4",
        xytext=(5, 0), textcoords="offset points", va="center",
    )

    # Stochastic Oscillator
    ax2.plot(plot_df.index, plot_k, color="#1f77b4", linewidth=1.2, label="%K")
    ax2.plot(plot_df.index, plot_d, color="#e377c2", linewidth=1.0, linestyle="--", label="%D")
    ax2.axhline(75, color="#1a7f37", linewidth=0.8, linestyle=":", alpha=0.7)
    ax2.axhline(25, color="#b42318", linewidth=0.8, linestyle=":", alpha=0.7)
    ax2.fill_between(plot_df.index, 75, 100, alpha=0.08, color="#1a7f37")
    ax2.fill_between(plot_df.index, 0, 25, alpha=0.08, color="#b42318")
    ax2.set_ylabel("Stoch %", fontsize=10)
    ax2.set_ylim(-5, 105)
    ax2.legend(loc="upper left", fontsize=8, framealpha=0.8)
    ax2.grid(True, alpha=0.3)
    ax2.tick_params(axis="both", labelsize=9)

    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax2.xaxis.set_major_locator(mdates.WeekdayLocator(interval=2))
    plt.setp(ax2.xaxis.get_majorticklabels(), rotation=30, ha="right", fontsize=8)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ── Weather ──────────────────────────────────────────────────────────────────


def _fetch_weather() -> tuple[str, str]:
    """Fetch 7-day forecast from Open-Meteo. Returns (html, text)."""
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={WEATHER_LAT}&longitude={WEATHER_LON}"
            f"&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,"
            f"precipitation_probability_max,wind_speed_10m_max,weather_code"
            f"&temperature_unit=fahrenheit&wind_speed_unit=mph"
            f"&precipitation_unit=inch&timezone=America/Chicago"
        )
        resp = _requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        daily = data["daily"]

        dates = daily["time"]
        t_max = daily["temperature_2m_max"]
        t_min = daily["temperature_2m_min"]
        precip = daily["precipitation_sum"]
        precip_prob = daily.get("precipitation_probability_max", [None] * len(dates))
        wind = daily.get("wind_speed_10m_max", [None] * len(dates))
        codes = daily.get("weather_code", [None] * len(dates))

        rows_html = []
        rows_text = []
        for i, date_str in enumerate(dates):
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            day_name = dt.strftime("%a %b %d")
            desc = WMO_WEATHER_CODES.get(codes[i], "N/A") if codes[i] is not None else "N/A"
            hi = f"{t_max[i]:.0f}" if t_max[i] is not None else "?"
            lo = f"{t_min[i]:.0f}" if t_min[i] is not None else "?"
            rain = f'{precip[i]:.2f}"' if precip[i] is not None else "?"
            prob = f"{precip_prob[i]:.0f}%" if precip_prob[i] is not None else "?"
            w = f"{wind[i]:.0f} mph" if wind[i] is not None else "?"

            label = " (Today)" if i == 0 else ""
            bg = "#f8f8f8" if i % 2 == 0 else "#ffffff"
            rows_html.append(
                f'<tr style="background:{bg};">'
                f"<td><b>{day_name}{label}</b></td>"
                f"<td>{desc}</td>"
                f"<td style='text-align:center;'>{hi}°F</td>"
                f"<td style='text-align:center;'>{lo}°F</td>"
                f"<td style='text-align:center;'>{rain}</td>"
                f"<td style='text-align:center;'>{prob}</td>"
                f"<td style='text-align:center;'>{w}</td>"
                f"</tr>"
            )
            rows_text.append(
                f"  {day_name}{label}: {desc}, Hi {hi}°F / Lo {lo}°F, "
                f"Precip {rain} ({prob}), Wind {w}"
            )

        html = (
            f"<h3 style='margin-top:24px;'>7-Day Weather Forecast — {WEATHER_LABEL}</h3>"
            f"<table cellpadding='5' cellspacing='0' border='0' "
            f"style='border-collapse:collapse;width:100%;font-size:13px;'>"
            f"<thead><tr style='background:#f2f2f2;'>"
            f"<th align='left'>Day</th><th align='left'>Conditions</th>"
            f"<th>High</th><th>Low</th><th>Precip</th><th>Prob</th><th>Wind</th>"
            f"</tr></thead><tbody>{''.join(rows_html)}</tbody></table>"
            f"<p style='color:#888;font-size:11px;'>Source: Open-Meteo.com | "
            f"Location: {WEATHER_LABEL} ({WEATHER_LAT}°N, {abs(WEATHER_LON)}°W). "
            f"Set WEATHER_LAT, WEATHER_LON, WEATHER_LABEL env vars to change location.</p>"
        )

        text = (
            f"\n7-DAY WEATHER FORECAST — {WEATHER_LABEL}\n"
            + "\n".join(rows_text)
            + f"\n  Source: Open-Meteo.com\n"
        )
        return html, text

    except Exception as exc:
        err_html = (
            f"<h3 style='margin-top:24px;'>Weather Forecast</h3>"
            f"<p style='color:#b42318;'>Weather data unavailable: {exc}</p>"
        )
        err_text = f"\nWEATHER FORECAST\n  Unavailable: {exc}\n"
        return err_html, err_text


# ── Analysis ─────────────────────────────────────────────────────────────────


def analyze(name: str, symbol: str, unit: str) -> InstrumentReport:
    blank = InstrumentReport(
        name=name, symbol=symbol, unit=unit,
        last=None, prev_close=None,
        change_pct_1d=None, change_pct_7d=None, change_pct_30d=None,
        sma20=None, sma50=None, rsi14=None,
        stoch_k=None, stoch_d=None,
        trend="Unknown", signal="HOLD",
        rationale="no data", forecast_1d=None, forecast_7d=None,
    )
    try:
        df = fetch_ohlc(symbol)
    except Exception as exc:
        blank.error = f"fetch failed: {exc}"
        return blank

    if df.empty:
        blank.error = "no price data"
        return blank

    closes = df["Close"]
    last = float(closes.iloc[-1])
    prev = float(closes.iloc[-2]) if len(closes) > 1 else None
    sma20_val = float(closes.tail(20).mean()) if len(closes) >= 20 else None
    sma50_val = float(closes.tail(50).mean()) if len(closes) >= 50 else None
    rsi14 = _rsi(closes)
    stoch_k, stoch_d, stoch_k_series, stoch_d_series = _stochastic(df)

    trend, signal, rationale = _derive_signal(last, sma20_val, sma50_val, rsi14, stoch_k)

    chart_png = None
    try:
        sma20_series = closes.rolling(20).mean()
        chart_png = _generate_chart(
            df, name, symbol, signal, sma20_series, stoch_k_series, stoch_d_series,
        )
    except Exception as exc:
        print(f"  Chart failed: {exc}")

    return InstrumentReport(
        name=name, symbol=symbol, unit=unit,
        last=last, prev_close=prev,
        change_pct_1d=_pct_change(closes, 1),
        change_pct_7d=_pct_change(closes, 5),
        change_pct_30d=_pct_change(closes, 21),
        sma20=sma20_val, sma50=sma50_val, rsi14=rsi14,
        stoch_k=stoch_k, stoch_d=stoch_d,
        trend=trend, signal=signal, rationale=rationale,
        forecast_1d=_linear_forecast(closes, 1),
        forecast_7d=_linear_forecast(closes, 5),
        chart_png=chart_png,
    )


# ── Rendering ────────────────────────────────────────────────────────────────


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


def render_html(reports: list[InstrumentReport], run_ts: datetime, weather_html: str) -> str:
    rows: list[str] = []
    for r in reports:
        if r.error:
            rows.append(
                f"<tr><td><b>{r.name}</b></td>"
                f"<td colspan='9' style='color:#b42318;'>Data unavailable: {r.error}</td></tr>"
            )
            continue
        chg1 = "#1a7f37" if (r.change_pct_1d or 0) >= 0 else "#b42318"
        chg7 = "#1a7f37" if (r.change_pct_7d or 0) >= 0 else "#b42318"
        chg30 = "#1a7f37" if (r.change_pct_30d or 0) >= 0 else "#b42318"
        rows.append(
            "<tr>"
            f"<td><b>{r.name}</b><br><small>{r.symbol} · {r.unit}</small></td>"
            f"<td style='text-align:right;font-weight:600;'>{_fmt(r.last)}</td>"
            f"<td style='text-align:right;color:{chg1};'>{_fmt(r.change_pct_1d, 2, '%')}</td>"
            f"<td style='text-align:right;color:{chg7};'>{_fmt(r.change_pct_7d, 2, '%')}</td>"
            f"<td style='text-align:right;color:{chg30};'>{_fmt(r.change_pct_30d, 2, '%')}</td>"
            f"<td style='text-align:right;'>{_fmt(r.stoch_k, 0)}%</td>"
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
        fc_1d_dir = ""
        if r.forecast_1d is not None and r.last is not None:
            fc_1d_dir = " (higher)" if r.forecast_1d > r.last else " (lower)"
        fc_7d_dir = ""
        if r.forecast_7d is not None and r.last is not None:
            fc_7d_dir = " (higher)" if r.forecast_7d > r.last else " (lower)"

        chart_img = ""
        if r.chart_png:
            cid = f"chart_{r.symbol.replace('=', '_')}"
            chart_img = (
                f'<div style="margin:8px 0;">'
                f'<img src="cid:{cid}" alt="{r.name} chart" '
                f'style="max-width:100%;border:1px solid #ddd;border-radius:4px;">'
                f'</div>'
            )

        details.append(
            f"<h3 style='margin-bottom:4px;'>{r.name} — {_badge(r.signal)}</h3>"
            f"<p style='margin-top:0;color:#444;'>"
            f"<b>Trend:</b> {r.trend}<br>"
            f"<b>Rationale:</b> {r.rationale}<br>"
            f"<b>Last:</b> {_fmt(r.last)} {r.unit} · "
            f"<b>SMA20:</b> {_fmt(r.sma20)} · "
            f"<b>SMA50:</b> {_fmt(r.sma50)}<br>"
            f"<b>Stochastic:</b> %K={_fmt(r.stoch_k, 1)}% · %D={_fmt(r.stoch_d, 1)}%<br>"
            f"<b>RSI(14):</b> {_fmt(r.rsi14, 0)}<br>"
            f"<b>1-day forecast:</b> {_fmt(r.forecast_1d)}{fc_1d_dir} · "
            f"<b>7-day forecast:</b> {_fmt(r.forecast_7d)}{fc_7d_dir}"
            f"</p>"
            f"{chart_img}"
        )

    error_count = sum(1 for r in reports if r.error)
    status_note = ""
    if error_count > 0:
        status_note = (
            f"<p style='color:#b42318;font-weight:600;'>"
            f"Warning: {error_count} instrument(s) could not be fetched.</p>"
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
        <th align="right">Stoch</th>
        <th align="right">RSI</th>
        <th align="left">Trend</th>
        <th align="left">Signal</th>
        <th align="right">Forecast</th>
      </tr>
    </thead>
    <tbody>{''.join(rows)}</tbody>
  </table>

  <h3 style="margin-top:24px;">Signal Rules</h3>
  <p style="color:#444;font-size:13px;">
    <b style="color:#1a7f37;">BUY:</b> Price above 20-day SMA AND Stochastic %K &gt; 75%<br>
    <b style="color:#b42318;">SELL:</b> Price below 20-day SMA AND Stochastic %K &lt; 25%<br>
    <b style="color:#6c6c6c;">HOLD:</b> Otherwise
  </p>

  <h3 style="margin-top:24px;">Detailed Analysis &amp; Charts</h3>
  {''.join(details)}

  <h3 style="margin-top:24px;">Sheep / Lamb Prices</h3>
  <p style="color:#444;font-size:13px;">
    {SHEEP_NOTE}<br>
    <a href="https://mymarketnews.ams.usda.gov/viewReport/2907">USDA National Direct Sheep Report</a> ·
    <a href="https://www.ams.usda.gov/mnreports/lswlamb.pdf">Weekly Lamb Market Summary (PDF)</a>
  </p>

  {weather_html}

  <hr>
  <p style="color:#888;font-size:11px;">
    Prices are end-of-day from Yahoo Finance and may be delayed.
    Signals use 20-day SMA + Stochastic(14,3) rules as described above.
    Forecasts are linear extrapolations of the last 20 closes.
    This is an automated summary, <b>not financial advice</b>. Do your own research.
  </p>
</body></html>
"""


def render_text(reports: list[InstrumentReport], run_ts: datetime, weather_text: str) -> str:
    lines = [
        "=" * 60,
        "   Daily Grain & Livestock Report",
        f"   Generated {run_ts.strftime('%A, %B %d, %Y at %H:%M UTC')}",
        "=" * 60,
        "",
        "Signal Rules:",
        "  BUY:  Price > SMA20 AND Stochastic %K > 75%",
        "  SELL: Price < SMA20 AND Stochastic %K < 25%",
        "  HOLD: Otherwise",
        "",
    ]
    for r in reports:
        if r.error:
            lines.append(f"  {r.name} ({r.symbol}): DATA UNAVAILABLE - {r.error}")
            lines.append("")
            continue
        sig_tag = {"BUY": "[BUY]", "SELL": "[SELL]", "HOLD": "[HOLD]"}.get(r.signal, "[?]")
        fc_1d_dir = ""
        if r.forecast_1d is not None and r.last is not None:
            fc_1d_dir = " (higher)" if r.forecast_1d > r.last else " (lower)"
        fc_7d_dir = ""
        if r.forecast_7d is not None and r.last is not None:
            fc_7d_dir = " (higher)" if r.forecast_7d > r.last else " (lower)"
        lines += [
            f"--- {r.name} ({r.symbol}) {sig_tag} ---",
            f"  Last Price:  {_fmt(r.last)} {r.unit}",
            f"  Changes:     1d {_fmt(r.change_pct_1d, 2, '%')}  |  "
            f"7d {_fmt(r.change_pct_7d, 2, '%')}  |  "
            f"30d {_fmt(r.change_pct_30d, 2, '%')}",
            f"  SMA20/50:    {_fmt(r.sma20)} / {_fmt(r.sma50)}",
            f"  Stochastic:  %K={_fmt(r.stoch_k, 1)}%  %D={_fmt(r.stoch_d, 1)}%",
            f"  RSI(14):     {_fmt(r.rsi14, 0)}",
            f"  Trend:       {r.trend}",
            f"  Signal:      {r.signal}  ({r.rationale})",
            f"  Forecast:    1d -> {_fmt(r.forecast_1d)}{fc_1d_dir}  |  "
            f"7d -> {_fmt(r.forecast_7d)}{fc_7d_dir}",
            "",
        ]
    lines += [
        "",
        "SHEEP / LAMB PRICES",
        SHEEP_NOTE,
        "",
        weather_text,
        "",
        "-" * 60,
        "Prices from Yahoo Finance (end-of-day, may be delayed).",
        "Signals use SMA20 + Stochastic(14,3) rules. NOT financial advice.",
        "-" * 60,
    ]
    return "\n".join(lines)


# ── Email ────────────────────────────────────────────────────────────────────


def send_email(
    subject: str, text_body: str, html_body: str, reports: list[InstrumentReport],
) -> None:
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or "587")
    username = os.environ.get("SMTP_USERNAME") or ""
    password = os.environ.get("SMTP_PASSWORD") or ""
    email_from = os.environ.get("EMAIL_FROM") or username
    email_to = os.environ.get("EMAIL_TO") or "wickdav9413@gmail.com"

    if not username or not password:
        raise RuntimeError(
            "SMTP_USERNAME and SMTP_PASSWORD must be set (use a Gmail App Password).\n"
            "Set these as GitHub repository secrets."
        )
    if not email_from:
        email_from = username

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = email_to

    msg.attach(MIMEText(text_body, "plain"))

    html_related = MIMEMultipart("related")
    html_related.attach(MIMEText(html_body, "html"))

    for r in reports:
        if r.chart_png:
            cid = f"chart_{r.symbol.replace('=', '_')}"
            img = MIMEImage(r.chart_png, _subtype="png")
            img.add_header("Content-ID", f"<{cid}>")
            img.add_header("Content-Disposition", "inline", filename=f"{cid}.png")
            html_related.attach(img)

    msg.attach(html_related)

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port) as server:
        server.starttls(context=context)
        server.login(username, password)
        server.send_message(msg)


# ── Main ─────────────────────────────────────────────────────────────────────


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
            chart_ok = "with chart" if report.chart_png else "no chart"
            print(f"OK - {_fmt(report.last)} {unit} ({chart_ok})")
        reports.append(report)
        if i < len(INSTRUMENTS) - 1:
            time.sleep(1.5)

    success_count = sum(1 for r in reports if not r.error)
    print(f"\nResults: {success_count}/{len(reports)} instruments fetched.\n")

    print("Fetching weather forecast...", end=" ", flush=True)
    weather_html, weather_text = _fetch_weather()
    print("OK\n")

    text_body = render_text(reports, run_ts, weather_text)
    html_body = render_html(reports, run_ts, weather_html)
    subject = f"Grain & Livestock Report - {run_ts.strftime('%Y-%m-%d')}"

    print(text_body)

    if os.environ.get("DRY_RUN", "").lower() in {"1", "true", "yes"}:
        print("\nDRY_RUN set; skipping email send.")
        return 0

    if success_count == 0:
        print("\nERROR: All instruments failed. Not sending empty report.")
        return 1

    try:
        send_email(subject, text_body, html_body, reports)
        print("\nEmail sent successfully.")
    except Exception as exc:
        print(f"\nERROR sending email: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
