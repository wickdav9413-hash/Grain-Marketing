# Grain & Livestock Daily Report

Automated daily email covering grain and livestock commodity prices,
technical analysis, buy/sell signals with confidence scores, and
short-term price forecasts. Runs on GitHub Actions — no server needed.

## What You Get

Every weekday morning at **6:30 AM Central**, you receive an email with:

1. **Current Prices** — Corn, Soybeans, Wheat, Class III Milk,
   Live Cattle (beef), Lean Hogs, and a Sheep/Lamb proxy
2. **Market Trend Predictions** — trend direction for each commodity
   based on multi-indicator technical analysis
3. **Buy / Sell / Hold Signals** — with confidence level (High/Medium/Low)
   and actionable strategy (entry points, stop-losses, take-profit targets)
4. **1-Day and 7-Day Forecasts** — price projections with percentage
   change from current price

The report includes a **Market Overview** showing the overall market
sentiment across all tracked commodities.

### Instruments

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
> so we use the iPath Bloomberg Livestock ETN (COW) as a proxy.

## Setup (One-Time)

### Step 1: Create a Gmail App Password

1. Go to <https://myaccount.google.com/apppasswords>
2. Select **Mail** and your device, then click **Generate**
3. Copy the 16-character password (you'll need it in Step 2)

> Your regular Gmail password won't work. You must use an App Password.

### Step 2: Add GitHub Secrets

In your repository, go to **Settings > Secrets and variables > Actions**
and add these secrets:

| Secret name     | Value                                            |
| --------------- | ------------------------------------------------ |
| `SMTP_USERNAME` | **Required** — your Gmail address                |
| `SMTP_PASSWORD` | **Required** — the 16-char App Password          |
| `EMAIL_TO`      | Optional — defaults to `wickdav9413@gmail.com`   |
| `EMAIL_FROM`    | Optional — defaults to `SMTP_USERNAME`           |
| `SMTP_HOST`     | Optional — defaults to `smtp.gmail.com`          |
| `SMTP_PORT`     | Optional — defaults to `587`                     |

### Step 3: Enable Actions & Merge

1. Enable Actions: **Settings > Actions > General > Allow all actions**
2. Merge this branch into `main` — scheduled workflows only run from
   the default branch

## Running

- **Automatic**: runs Mon–Fri at 11:30 UTC (6:30 AM Central)
- **Manual**: Actions tab > "Daily Grain & Livestock Report" > Run workflow
  (tick "dry run" to test without sending email)
- **Local**:
  ```bash
  pip install -r requirements.txt
  DRY_RUN=1 python grain_report.py            # print only
  SMTP_USERNAME=you@gmail.com \
  SMTP_PASSWORD=your-app-password \
  python grain_report.py                      # send email
  ```

## How Signals Work

Signals combine four technical indicators with a voting system:

| Indicator              | Buy signal                    | Sell signal                  |
| ---------------------- | ----------------------------- | ---------------------------- |
| **SMA Crossover**      | Price > SMA20 > SMA50         | Price < SMA20 < SMA50       |
| **RSI (14)**           | Below 30 (oversold)           | Above 70 (overbought)       |
| **MACD (12,26,9)**     | Histogram positive            | Histogram negative           |
| **Bollinger Bands**    | Price at lower band           | Price at upper band          |

Each indicator casts bullish or bearish votes. The net vote count
determines the signal:

- **STRONG BUY**: net +3 or more bullish votes
- **BUY**: net +1 to +2
- **HOLD**: tied / neutral
- **SELL**: net -1 to -2
- **STRONG SELL**: net -3 or more bearish votes

**Confidence** is based on how many indicators agree:
High (4+ votes aligned), Medium (2-3), Low (0-1).

**Forecasts** use linear regression on the last 20 trading sessions,
projected 1 and 5 trading days (= ~1 week) ahead.

## Disclaimer

This is an educational automation. It is **not financial advice**,
not a recommendation to buy or sell, and carries no warranty. Futures
trading involves substantial risk. Always do your own research and
consult a licensed professional before making trading decisions.
