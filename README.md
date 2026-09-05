# Grain & Livestock Daily Report

Automated daily email covering grain and livestock commodity prices,
technical trend analysis with **Stochastic Oscillator charts**,
buy/sell signals, short-term forecasts, and a **7-day weather forecast**.
Designed to run on GitHub Actions once per day with no server to maintain.

## What you get

Each weekday morning, `wickdav9413@gmail.com` receives an email containing:

1. **Current prices** for Corn, Soybeans, Wheat, Class III Milk,
   Live Cattle (beef), Lean Hogs, Feeder Cattle, and Sheep/Lamb
   reference links.
2. **Trend read** (uptrend / downtrend / weak) based on 20- and
   50-day simple moving averages, with market predictions and
   reasoning for each instrument.
3. **Buy / Sell / Hold signal** using Stochastic Oscillator + SMA20:
   - **BUY** when price is above the 20-day moving average AND
     Stochastic %K is above 75%
   - **SELL** when price is below the 20-day moving average AND
     Stochastic %K is below 25%
   - **HOLD** otherwise
4. **Embedded charts** for each instrument showing price with SMA20
   overlay and Stochastic Oscillator (%K/%D) with overbought/oversold
   zones highlighted.
5. **1-day and 7-day price forecasts** from linear regression of the
   last 20 closes.
6. **7-day weather forecast** for the US Corn Belt (configurable
   location) from Open-Meteo, including temperature, precipitation,
   wind, and conditions.

All prices are end-of-day quotes from Yahoo Finance (free, possibly
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
| Feeder Cattle        | `GF=F`  | CME feeder cattle futures              |

> Sheep/Lamb has no publicly traded futures contract. The report
> includes links to the USDA National Direct Sheep Report for current
> lamb prices.

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

### Weather location (optional)

By default the weather forecast covers Des Moines, IA (central Corn
Belt). To change it, add these environment variables in the workflow
or as secrets:

| Variable         | Default                      |
| ---------------- | ---------------------------- |
| `WEATHER_LAT`    | `41.59` (Des Moines, IA)     |
| `WEATHER_LON`    | `-93.62`                     |
| `WEATHER_LABEL`  | `Des Moines, IA (Corn Belt)` |

## Running it

- **Scheduled**: the workflow runs automatically every weekday at
  `11:30 UTC` (`06:30 US Central`). Change the cron in
  `.github/workflows/daily-grain-report.yml` if you want a different
  time. Cron in GitHub Actions is best-effort; expect a few minutes
  of drift.

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

The script uses transparent, mechanical rules:

- **Stochastic Oscillator** (14-period %K, 3-period %D):
  Measures where the closing price sits relative to its high-low
  range over the last 14 periods. Above 75% = strong upward
  momentum; below 25% = strong downward momentum.

- **Signal**:
  - Price above SMA20 AND Stoch %K > 75% -> **BUY** (uptrend +
    strong momentum confirmation)
  - Price below SMA20 AND Stoch %K < 25% -> **SELL** (downtrend +
    weak momentum confirmation)
  - Otherwise -> **HOLD**

- **Trend**: compares last price to SMA20 and SMA50.
  - `price > SMA20 > SMA50` -> uptrend
  - `price < SMA20 < SMA50` -> downtrend
  - otherwise -> weak up/down based on SMA50

- **RSI(14)**: shown as a supplementary indicator (oversold < 30,
  overbought > 70).

- **Forecasts**: linear least-squares fit of the last 20 closes,
  projected 1 and 5 trading days ahead.

- **Charts**: each instrument gets an embedded chart showing the
  last 60 trading days with price/SMA20 overlay and Stochastic
  subplot with 75/25 threshold zones.

These rules catch momentum moves and obvious reversals, but they
will be wrong around news events and regime changes. Treat the
signal as a starting point for your own analysis.

## Disclaimer

This is an educational automation. It is **not financial advice**,
not a recommendation to buy or sell, and carries no warranty. Futures
trading involves substantial risk. Always do your own research and
consult a licensed professional before making trading decisions.
