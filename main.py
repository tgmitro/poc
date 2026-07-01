"""
Dashboard – Weather + Fio Banka SK account balance.
Serves HTTPS, compatible with Opera Mini Native v4.4.
"""

import os
import time
from datetime import datetime

import re
import requests
from dotenv import load_dotenv
from flask import Flask, render_template

import logging
logger = logging.getLogger(__name__)
FORMAT = '%(asctime)s %(levelname)-8s %(name)s %(message)s'
logging.basicConfig(level=logging.INFO, format=FORMAT)
load_dotenv()

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config from .env
# ---------------------------------------------------------------------------
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY", "")
WEATHER_CITY    = os.getenv("WEATHER_CITY", "Bratislava")
WEATHER_UNITS   = os.getenv("WEATHER_UNITS", "metric")  # metric | imperial
WEATHER_API_URL = os.getenv(
    "WEATHER_API_URL", "https://api.openweathermap.org/data/2.5/weather"
)

FIO_API_TOKEN   = os.getenv("FIO_API_TOKEN", "")
SAVINGS_API_TOKEN = os.getenv("SAVINGS_API_TOKEN", "")
FIO_INTEREST_RATE_URL = "https://www.fio.sk/bankove-sluzby/sporenie/sporiace-ucty"

HOST     = os.getenv("HOST", "0.0.0.0")
PORT     = int(os.getenv("PORT", "4443"))
SSL_CERT = os.getenv("SSL_CERT", "")
SSL_KEY  = os.getenv("SSL_KEY", "")

REFRESH_INTERVAL = int(os.getenv("REFRESH_INTERVAL", "300"))

UNIT_LABEL = {"metric": "C", "imperial": "F"}.get(WEATHER_UNITS, "C")

# ---------------------------------------------------------------------------
# Global Caches
# ---------------------------------------------------------------------------
_BANK_CACHES = {}
_INTEREST_CACHE = {"rate": "N/A", "timestamp": 0}

# ---------------------------------------------------------------------------
# Data fetching helpers
# ---------------------------------------------------------------------------

def fetch_weather():
    """Return a dict with weather data or {'error': str}."""
    if not WEATHER_API_KEY:
        return {"error": "WEATHER_API_KEY not set in .env"}
    try:
        params = {
            "q": WEATHER_CITY,
            "appid": WEATHER_API_KEY,
            "units": WEATHER_UNITS,
        }
        r = requests.get(WEATHER_API_URL, params=params, timeout=10)
        d = r.json()

        # OpenWeather returns details in JSON for non-2xx responses.
        if r.status_code >= 400:
            message = d.get("message") if isinstance(d, dict) else None
            if message:
                return {"error": f"OpenWeather API error: {message}"}
        r.raise_for_status()

        weather_items = d.get("weather") if isinstance(d, dict) else None
        weather_desc = ""
        if isinstance(weather_items, list) and weather_items:
            weather_desc = weather_items[0].get("description", "")

        main = d.get("main", {}) if isinstance(d, dict) else {}
        wind = d.get("wind", {}) if isinstance(d, dict) else {}

        return {
            "city":    d.get("name", WEATHER_CITY),
            "desc":    weather_desc.capitalize(),
            "temp":    round(float(main.get("temp", 0)), 1),
            "feels":   round(float(main.get("feels_like", 0)), 1),
            "humidity":int(main.get("humidity", 0)),
            "wind":    round(float(wind.get("speed", 0)), 1),
        }
    except requests.RequestException as exc:
        return {"error": str(exc)}
    except (KeyError, ValueError) as exc:
        return {"error": f"Unexpected response: {exc}"}


def fetch_bank(token, cache_key):
    """Return account balance and today's latest transactions or {'error': str}."""
    global _BANK_CACHES
    now = time.time()

    if cache_key not in _BANK_CACHES:
        _BANK_CACHES[cache_key] = {"data": None, "timestamp": 0}

    cache = _BANK_CACHES[cache_key]

    # Return cached data if it's less than 30 seconds old
    if cache["data"] and (now - cache["timestamp"] < 30):
        return cache["data"]

    if not token:
        return {"error": f"Token for {cache_key} not set in .env"}
    try:
        today = datetime.now().date()
        start_of_month = today.replace(day=1)
        url = (
            f"https://fioapi.fio.cz/v1/rest/periods/{token}"
            f"/{start_of_month:%Y-%m-%d}/{today:%Y-%m-%d}"
            "/transactions.json"
        )
        r = requests.get(url, timeout=15)
        logger.info(url)

        # If rate limited (409), return last known data if available
        if r.status_code == 409 and cache["data"]:
            return cache["data"]

        r.raise_for_status()

        data = r.json()
        statement = data.get("accountStatement", {})
        info = statement.get("info", {})

        if not info:
            return {"error": "JSON response missing accountStatement info"}

        def get_val(tx, column, default=None):
            col = tx.get(column)
            if col is None:
                return default
            return col.get("value", default)

        transactions = []
        tx_list = statement.get("transactionList", {}).get("transaction", [])
        if not tx_list:
            tx_list = []

        for tx in tx_list:
            if tx is None:
                continue
            date_text = get_val(tx, "column0")
            if not date_text:
                continue

            movement_id = get_val(tx, "column22", 0)
            amount = get_val(tx, "column1", 0.0)
            tx_type = get_val(tx, "column8", "")
            counterparty = get_val(tx, "column10", "")

            # Note can be in column 16 or 25
            note16 = get_val(tx, "column16", "")
            note25 = get_val(tx, "column25", "")
            note = (note16 or note25 or "").strip()

            tx_currency = get_val(tx, "column14", "")

            transactions.append(
                {
                    "movement_id": movement_id,
                    "date": date_text,
                    "amount": amount,
                    "currency": tx_currency,
                    "type": tx_type,
                    "counterparty": counterparty,
                    "note": note,
                }
            )

        transactions.sort(key=lambda x: x["movement_id"], reverse=True)
        latest_transactions = transactions[:3]

        result = {
            "account":  info.get("accountId", ""),
            "iban":     info.get("iban", "N/A"),
            "balance":  round(float(info.get("closingBalance", 0)), 2),
            "currency": info.get("currency", ""),
            "transactions": latest_transactions,
        }

        # Update cache
        cache["data"] = result
        cache["timestamp"] = now

        return result
    except requests.RequestException as exc:
        return {"error": str(exc)}
    except (ValueError, TypeError, KeyError) as exc:
        return {"error": f"Unexpected response: {exc}"}


def fetch_interest_rate():
    """Scrape the current interest rate from Fio Banka website."""
    global _INTEREST_CACHE
    now = time.time()

    # Cache for 1 hour (3600 seconds)
    if _INTEREST_CACHE["rate"] != "N/A" and (now - _INTEREST_CACHE["timestamp"] < 3600):
        return _INTEREST_CACHE["rate"]

    try:
        r = requests.get(FIO_INTEREST_RATE_URL, timeout=10)
        r.raise_for_status()
        html = r.text
        match = re.search(r'Aktuálna úroková sadzba:\s*<big>(.*?)</big>', html)
        if match:
            rate = match.group(1).strip()
            _INTEREST_CACHE["rate"] = rate
            _INTEREST_CACHE["timestamp"] = now
            return rate
    except Exception as exc:
        logger.error(f"Failed to fetch interest rate: {exc}")

    return _INTEREST_CACHE["rate"]


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    return {"ok": True}, 200

@app.route("/")
def dashboard():
    weather = fetch_weather()
    bank    = fetch_bank(FIO_API_TOKEN, "main")
    savings = fetch_bank(SAVINGS_API_TOKEN, "savings")
    interest_rate = fetch_interest_rate()
    now     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    refresh_interval = REFRESH_INTERVAL
    if refresh_interval % 60 == 0:
        m = refresh_interval // 60
        refresh_text = f"{m} minute{'s' if m != 1 else ''}"
    else:
        refresh_text = f"{refresh_interval} second{'s' if refresh_interval != 1 else ''}"

    return render_template(
        "dashboard.html",
        now=now,
        refresh_interval=refresh_interval,
        refresh_text=refresh_text,

        # weather
        weather_city    = weather.get("city", WEATHER_CITY),
        weather_error   = weather.get("error"),
        weather_desc    = weather.get("desc", ""),
        weather_temp    = weather.get("temp", ""),
        weather_feels   = weather.get("feels", ""),
        weather_humidity= weather.get("humidity", ""),
        weather_wind    = weather.get("wind", ""),
        weather_unit    = UNIT_LABEL,

        # bank
        bank_error    = bank.get("error"),
        bank_account  = bank.get("account", ""),
        bank_iban     = bank.get("iban", ""),
        bank_balance  = bank.get("balance", ""),
        bank_currency = bank.get("currency", ""),
        bank_transactions = bank.get("transactions", []),

        # savings bank
        savings_error    = savings.get("error"),
        savings_account  = savings.get("account", ""),
        savings_iban     = savings.get("iban", ""),
        savings_balance  = savings.get("balance", ""),
        savings_currency = savings.get("currency", ""),
        savings_transactions = savings.get("transactions", []),
        savings_interest_rate = interest_rate,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"Starting dashboard on http://{HOST}:{PORT}/")
    app.run(host=HOST, port=PORT, debug=True)
