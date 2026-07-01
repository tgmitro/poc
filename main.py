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
from flask import Flask, render_template, request, session, redirect, url_for

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

DEBUG    = os.getenv("DEBUG", "False").lower() == "true"

REFRESH_INTERVAL = int(os.getenv("REFRESH_INTERVAL", "300"))
DASHBOARD_PIN = os.getenv("DASHBOARD_PIN", "1234")
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key")

UNIT_LABEL = {"metric": "C", "imperial": "F"}.get(WEATHER_UNITS, "C")

# ---------------------------------------------------------------------------
# Global Caches
# ---------------------------------------------------------------------------
_BANK_CACHES = {}
_INTEREST_CACHE = {"rate": "N/A", "timestamp": 0}
_FORECAST_CACHE = {"data": None, "timestamp": 0}

# ---------------------------------------------------------------------------
# Data fetching helpers
# ---------------------------------------------------------------------------

def format_iban(iban):
    """Format IBAN with spaces every 4 characters for better readability/line-breaking."""
    if not iban or iban == "N/A":
        return iban
    # Remove existing spaces and group by 4
    s = str(iban).replace(" ", "")
    return " ".join(s[i:i+4] for i in range(0, len(s), 4))

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

            date_only = date_text.split("+")[0]
            try:
                dt_obj = datetime.strptime(date_only, "%Y-%m-%d")
                weekday = dt_obj.strftime("%A")
            except Exception:
                weekday = ""

            transactions.append(
                {
                    "movement_id": movement_id,
                    "date": date_text,
                    "weekday": weekday,
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
            "iban":     format_iban(info.get("iban", "N/A")),
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


def fetch_forecast():
    """Return a list of forecast items for 3 days or {'error': str}."""
    global _FORECAST_CACHE
    now = time.time()

    # Cache for 30 minutes
    if _FORECAST_CACHE["data"] and (now - _FORECAST_CACHE["timestamp"] < 1800):
        return _FORECAST_CACHE["data"]

    if not WEATHER_API_KEY:
        return {"error": "WEATHER_API_KEY not set in .env"}

    try:
        url = "https://api.openweathermap.org/data/2.5/forecast"
        params = {
            "q": WEATHER_CITY,
            "appid": WEATHER_API_KEY,
            "units": WEATHER_UNITS,
        }
        r = requests.get(url, params=params, timeout=10)
        d = r.json()

        if r.status_code >= 400:
            message = d.get("message") if isinstance(d, dict) else None
            if message:
                return {"error": f"OpenWeather API error: {message}"}
        r.raise_for_status()

        forecast_list = d.get("list", [])
        results = []
        seen_dates = set()

        # Try to pick entries around noon for the next 3 days
        for item in forecast_list:
            dt_txt = item.get("dt_txt", "")
            if not dt_txt:
                continue
            date_part = dt_txt.split(" ")[0]
            time_part = dt_txt.split(" ")[1]

            if date_part not in seen_dates and "12:00:00" in time_part:
                main = item.get("main", {})
                weather = item.get("weather", [{}])[0]
                dt_obj = datetime.strptime(date_part, "%Y-%m-%d")
                weekday = dt_obj.strftime("%A")
                results.append({
                    "date": date_part,
                    "weekday": weekday,
                    "temp": round(float(main.get("temp", 0)), 1),
                    "desc": weather.get("description", "").capitalize(),
                    "humidity": main.get("humidity", 0)
                })
                seen_dates.add(date_part)
                if len(results) >= 3:
                    break

        # Fallback: if we didn't find 12:00 entries, just take first 3 unique days
        if len(results) < 3:
            for item in forecast_list:
                dt_txt = item.get("dt_txt", "")
                date_part = dt_txt.split(" ")[0]
                if date_part not in seen_dates:
                    main = item.get("main", {})
                    weather = item.get("weather", [{}])[0]
                    dt_obj = datetime.strptime(date_part, "%Y-%m-%d")
                    weekday = dt_obj.strftime("%A")
                    results.append({
                        "date": date_part,
                        "weekday": weekday,
                        "temp": round(float(main.get("temp", 0)), 1),
                        "desc": weather.get("description", "").capitalize(),
                        "humidity": main.get("humidity", 0)
                    })
                    seen_dates.add(date_part)
                    if len(results) >= 3:
                        break

        _FORECAST_CACHE["data"] = results
        _FORECAST_CACHE["timestamp"] = now
        return results
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    return {"ok": True}, 200

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("pin") == DASHBOARD_PIN:
            session["authenticated"] = True
            return redirect(url_for("dashboard"))
        else:
            error = "Invalid PIN"
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.pop("authenticated", None)
    return redirect(url_for("login"))

@app.route("/toggle_tx")
def toggle_tx():
    if not session.get("authenticated"):
        return redirect(url_for("login"))
    session["show_tx"] = not session.get("show_tx", True)
    return redirect(url_for("dashboard"))

@app.route("/weather")
def weather():
    if not session.get("authenticated"):
        return redirect(url_for("login"))
    forecast = fetch_forecast()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return render_template(
        "weather.html",
        now=now,
        forecast=forecast,
        weather_city=WEATHER_CITY,
        weather_unit=UNIT_LABEL
    )

@app.route("/")
def dashboard():
    if not session.get("authenticated"):
        return redirect(url_for("login"))
    weather = fetch_weather()
    bank    = fetch_bank(FIO_API_TOKEN, "main")
    savings = fetch_bank(SAVINGS_API_TOKEN, "savings")
    interest_rate = fetch_interest_rate()
    now     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    show_tx = session.get("show_tx", True)

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
        show_tx=show_tx,

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
    app.run(host=HOST, port=PORT, debug=DEBUG)
