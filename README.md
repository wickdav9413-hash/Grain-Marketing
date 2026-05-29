# Grain & Livestock Daily Report

Automated daily email covering grain and livestock commodity prices,
technical trends, buy/sell signals with confidence levels, and short-term
forecasts. Runs on GitHub Actions — no server to maintain.

## What you get

Every morning, `wickdav9413@gmail.com` receives an email containing:

1. **Market Overview** — overall sentiment (Bullish / Bearish / Neutral)
   with sector averages for grains and livestock.
2. **Current prices** for Corn, Soybeans, Wheat, Class III Milk,
   Live Cattle (beef), Lean Hogs, and a Sheep/Lamb proxy.
3. **Trend read** (uptrend / downtrend / weak) based on 20- and
   50-day simple moving averages.
4. **Buy / Sell / Hold signal** with confidence level (HIGH / MODERATE / LOW),
   driven by multi-indicator confluence: RSI(14), SMA crossover,
   MACD(12,26,9), and Bollinger Bands.
5. **1-day and 7-day forecasts** with direction arrows and percentage
   change, from a linear regression of the last 20 closes.

Prices are end-of-day quotes from Yahoo Finance (free, possibly
delayed). The report clearly labels itself as automated and **not
financial advice**.

### Instruments and symbols

| Product              | Symbol  | Notes                                  |
| -------------------- | ------- | -------------------------------------- |
| Corn                 | `ZC=F`  | CBOT corn futures                      |
| Soybeans             | `ZS=F`  | CBOT soybean futures                   |
| Wheat                | `ZW=F`  | CBOT wheat futures                     |
| Class III Milk       | `DC=F`  | CME Class III milk futures             |
| Live Cattle (Beef)   | `LE=F`  | CME live cattle futures                |
| Lean Hogs            | `HE=F`  | CME lean hogs futures                  |
| Sheep / Lamb (proxy) | `COW`   | iPath Bloomberg Livestock ETN - proxy  |

> Yahoo Finance does not offer a public sheep or lamb futures contract,
> so we use the iPath Bloomberg Livestock ETN (COW) as a proxy for
> livestock prices including lamb. If you have a paid data source for
> real lamb prices, swap the symbol in `grain_report.py` under `INSTRUMENTS`.

## One-time setup

The workflow needs a few GitHub secrets before it can send email.

1. **Create a Gmail App Password**
   (<https://myaccount.google.com/apppasswords>). Your regular Gmail
   password will not work if 2-Step Verification is on, and Google
   rejects less-secure-app logins.

2. In the repository, go to **Settings -> Secrets and variables ->
   Actions -> New repository secret** and add:

   | Secret name     | Value                                      |
   | --------------- | ------------------------------------------ |
   | `SMTP_USERNAME` | The Gmail address sending the report       |
   | `SMTP_PASSWORD` | The 16-character Gmail App Password        |
   | `EMAIL_FROM`    | *(optional)* defaults to `SMTP_USERNAME`   |
   | `EMAIL_TO`      | *(optional)* defaults to `wickdav9413@gmail.com` |
   | `SMTP_HOST`     | *(optional)* defaults to `smtp.gmail.com`  |
   | `SMTP_PORT`     | *(optional)* defaults to `587`             |

3. Enable Actions on the repo if it was disabled
   (**Settings -> Actions -> General -> Allow all actions**).

4. Merge this branch into `main` (or whichever branch GitHub Actions
   runs from). Scheduled workflows only run from the default branch.

## Running it

- **Scheduled**: the workflow runs automatically every day at
  `11:30 UTC` (`06:30 US Central`), including weekends. Weekend
  reports note that prices are from Friday's close. Change the cron in
  `.github/workflows/daily-grain-report.yml` if you want a different
  time.

- **Manual**: go to **Actions -> Daily Grain & Livestock Report ->
  Run workflow**. You can tick "dry run" to print the report in the
  log without sending the email, which is useful for testing before
  you add secrets.

- **Locally**:
  ```bash
  pip install -r requirements.txt
  DRY_RUN=1 python grain_report.py            # print only
  SMTP_USERNAME=you@gmail.com \
  SMTP_PASSWORD=app-password \
  EMAIL_TO=wickdav9413@gmail.com \
  python grain_report.py                      # actually send
  ```

## How the signals work

The script uses multi-indicator confluence scoring — no black box.

### Indicators

| Indicator | Parameters | Purpose |
| --------- | ---------- | ------- |
| SMA | 20-day, 50-day | Trend direction |
| RSI | 14-period | Overbought / oversold |
| MACD | 12, 26, 9 | Momentum crossover |
| Bollinger Bands | 20-day, 2 std dev | Volatility & extremes |

### Scoring

Each indicator contributes a score:

| Condition | Score |
| --------- | ----- |
| price > SMA20 > SMA50 (uptrend) | +2 |
| price < SMA20 < SMA50 (downtrend) | -2 |
| RSI < 30 (oversold) | +2 |
| RSI > 70 (overbought) | -2 |
| RSI 30-45 (leaning oversold) | +1 |
| RSI 55-70 (leaning overbought) | -1 |
| MACD histogram > 0 (bullish) | +1 |
| MACD histogram < 0 (bearish) | -1 |
| Price at/below lower Bollinger Band | +1 |
| Price at/above upper Bollinger Band | -1 |

### Signal mapping

| Total Score | Signal | Confidence |
| ----------- | ------ | ---------- |
| >= 4 | **STRONG BUY** | HIGH |
| 2 to 3 | **BUY** | MODERATE |
| -1 to 1 | **HOLD** | LOW |
| -2 to -3 | **SELL** | MODERATE |
| <= -4 | **STRONG SELL** | HIGH |

### Forecasts

Linear least-squares fit of the last 20 closes, projected 1 and 5
trading days ahead. Shown with direction arrows and percentage change.

These rules catch momentum moves and reversals, but they will be wrong
around news events and regime changes. Treat the signal as a starting
point for your own analysis.

## Disclaimer

This is an educational automation. It is **not financial advice**,
not a recommendation to buy or sell, and carries no warranty. Futures
trading involves substantial risk. Always do your own research and
consult a licensed professional before making trading decisions.
