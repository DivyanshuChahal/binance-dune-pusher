#!/usr/bin/env python3
"""
Push Binance spot 1-second OHLC klines for a rolling 7-day window to Dune.

What it does (per run):
  * Computes the last N complete UTC days, IGNORING the current day.
    e.g. run on 2026-09-29 -> window 2026-09-22 .. 2026-09-28 (7 days).
  * For each symbol, fetches every day's 1s klines from Binance's public
    daily dumps (data.binance.vision), verifying the SHA-256 checksum.
    If a day's dump is not published yet, it falls back to Binance's public
    REST endpoint (data-api.binance.vision) for that day.
  * Uploads the whole window to a Dune table via the CSV upload API.
    Uploading to an existing table REPLACES its contents, so each daily run
    is exactly "drop the oldest day, add the newest" -- an atomic rolling
    window with no dedup needed.

Symbols that don't exist on Binance are SKIPPED (logged, not failed), so you
can list pairs that aren't listed yet; they auto-activate once Binance lists
them. Each symbol goes to its own table: binance_<symbol>_1s_ohlc_7d.

Only required configuration:  DUNE_API_KEY  (env var / GitHub secret)

Optional env vars:
  SYMBOLS         comma-separated Binance symbols (default: the list below)
  DAYS            window length in days             (default "7")
  TABLE_PREFIX    Dune table name prefix           (default "binance_")
  TABLE_SUFFIX    Dune table name suffix           (default "_1s_ohlc_7d")
  DUNE_IS_PRIVATE "true"/"false"                    (default "false")

Stdlib only -- no third-party packages.
"""

import os
import sys
import io
import csv
import json
import time
import zipfile
import hashlib
import datetime as dt
import urllib.request
import urllib.error

# ----------------------------- configuration --------------------------------
# Binance SPOT symbols (base+quote, no separator). Note: Binance lists the
# USDC/USD1 pair reversed as USD1USDC (price = USDC per USD1).
DEFAULT_SYMBOLS = [
    "SOLUSDC",   # SOL-USDC
    "USDCUSDT",  # USDC-USDT
    "SOLUSDT",   # SOL-USDT
    "USD1USDC",  # USDC-USD1  (listed reversed: price is USDC per USD1)
    "SOLUSD1",   # SOL-USD1
    "PUMPUSDC",  # PUMP-USDC
    "ZECUSDC",   # ZEC-USDC
    "HYPEUSDC",  # HYPE-USDC
    "BONKUSDC",  # BONK-USDC
    "PENGUUSDC", # PENGU-USDC
    "TRUMPUSDC", # TRUMP-USDC
    # Not on Binance as of writing -- left in so they auto-activate if listed:
    # "CASHUSDC", "CBBTCUSDC", "USELESSUSDC", "FARTCOINUSDC",
]

DUNE_API_KEY = os.environ.get("DUNE_API_KEY")
_env_symbols = os.environ.get("SYMBOLS", "")
SYMBOLS      = ([s.strip().upper() for s in _env_symbols.split(",") if s.strip()]
                if _env_symbols.strip() else DEFAULT_SYMBOLS)
WINDOW_DAYS  = int(os.environ.get("DAYS", "7"))
TABLE_PREFIX = os.environ.get("TABLE_PREFIX", "binance_")
TABLE_SUFFIX = os.environ.get("TABLE_SUFFIX", "_1s_ohlc_7d")
IS_PRIVATE   = os.environ.get("DUNE_IS_PRIVATE", "false").lower() == "true"

DUMP_BASE       = "https://data.binance.vision/data/spot/daily/klines"
REST_BASE       = "https://data-api.binance.vision/api/v3/klines"
DUNE_UPLOAD_URL = "https://api.dune.com/api/v1/table/upload/csv"

# Output columns (one row = one 1-second kline).
HEADER = ["open_time_ms", "datetime_utc", "open", "high", "low",
          "close", "volume", "quote_volume", "trades"]

SECONDS_PER_DAY = 86_400
UA = {"User-Agent": "binance-dune-pusher/1.0"}


# ------------------------------- http helpers -------------------------------
def _urlopen(url, data=None, headers=None, timeout=120):
    req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})},
                                 method="POST" if data is not None else "GET")
    return urllib.request.urlopen(req, timeout=timeout)


def get_bytes(url, retries=4, timeout=180):
    """GET raw bytes. Returns None on HTTP 404; retries transient errors."""
    last = None
    for i in range(retries):
        try:
            with _urlopen(url, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last = e
        except Exception as e:  # noqa: BLE001 - network flakiness
            last = e
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url}: {last}")


def get_json(url, retries=5, timeout=60):
    last = None
    for i in range(retries):
        try:
            with _urlopen(url, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            last = e
            time.sleep(2 * (i + 1) * (3 if e.code == 429 else 1))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET json failed after {retries} tries: {url}: {last}")


def binance_symbol_exists(symbol, retries=3):
    """True if the symbol trades on Binance spot. False on 'invalid symbol'.
    On transient failures, assume True and let the main flow surface errors."""
    url = f"{REST_BASE}?symbol={symbol}&interval=1s&limit=1"
    for i in range(retries):
        try:
            with _urlopen(url, timeout=30) as r:
                data = json.loads(r.read())
                return isinstance(data, list) and len(data) > 0
        except urllib.error.HTTPError as e:
            if e.code == 400:      # -1121 Invalid symbol
                return False
            time.sleep(1.5 * (i + 1))
        except Exception:          # noqa: BLE001
            time.sleep(1.5 * (i + 1))
    return True


# ------------------------------- parsing utils ------------------------------
def to_ms(ts):
    """Normalize a Binance timestamp to milliseconds (dumps use microseconds,
    REST uses milliseconds; also tolerates nanoseconds)."""
    ts = int(ts)
    while ts > 10 ** 13:
        ts //= 1000
    return ts


def fmt_utc(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _row_from_kline(cols):
    """Binance kline layout:
       0 open_time, 1 open, 2 high, 3 low, 4 close, 5 volume, 6 close_time,
       7 quote_volume, 8 num_trades, 9 taker_base, 10 taker_quote, 11 ignore
    """
    ms = to_ms(cols[0])
    return (ms, fmt_utc(ms), cols[1], cols[2], cols[3], cols[4], cols[5], cols[7], cols[8])


# ------------------------------- data sources -------------------------------
def rows_from_dump(symbol, day):
    """Return list of rows from the daily dump, or None if unavailable/corrupt."""
    ds = day.strftime("%Y-%m-%d")
    url = f"{DUMP_BASE}/{symbol}/1s/{symbol}-1s-{ds}.zip"
    blob = get_bytes(url)
    if blob is None:
        return None

    checksum = get_bytes(url + ".CHECKSUM")
    if checksum:
        want = checksum.decode().split()[0].strip().lower()
        if hashlib.sha256(blob).hexdigest() != want:
            print(f"    ! checksum mismatch for {symbol} {ds}; re-downloading once")
            blob = get_bytes(url)
            if blob is None or hashlib.sha256(blob).hexdigest() != want:
                return None  # let caller fall back to REST

    rows = []
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        with z.open(z.namelist()[0]) as fh:
            for line in io.TextIOWrapper(fh, "utf-8"):
                p = line.rstrip("\n").split(",")
                if not p or not p[0].lstrip("-").isdigit():
                    continue  # skip any header row
                rows.append(_row_from_kline(p))
    return rows


def rows_from_rest(symbol, day):
    """Fetch one full UTC day of 1s klines from the public REST endpoint.
    Returns [] for days before the pair was listed."""
    start = int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc).timestamp() * 1000)
    end = start + SECONDS_PER_DAY * 1000  # next-day 00:00:00 (exclusive)
    rows, cur = [], start
    while cur < end:
        url = (f"{REST_BASE}?symbol={symbol}&interval=1s"
               f"&startTime={cur}&endTime={end - 1}&limit=1000")
        batch = get_json(url)
        if not batch:
            break
        for k in batch:
            ms = to_ms(k[0])
            if ms >= end:
                break
            rows.append(_row_from_kline(k))
        cur = to_ms(batch[-1][0]) + 1000
        time.sleep(0.05)  # be polite to the endpoint
    return rows


def fetch_day(symbol, day):
    rows = rows_from_dump(symbol, day)
    if rows is None:
        print(f"    dump not available for {symbol} {day}; using REST fallback")
        rows = rows_from_rest(symbol, day)
        src = "REST"
    else:
        src = "dump"
    return rows, src


# --------------------------------- build + push -----------------------------
def build_csv(symbol, days):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HEADER)
    total = 0
    for day in days:
        rows, src = fetch_day(symbol, day)
        w.writerows(rows)
        total += len(rows)
        print(f"    {symbol} {day} [{src}]: {len(rows):,} rows")
    return buf.getvalue(), total


def upload_to_dune(table, csv_str, description):
    payload = json.dumps({
        "table_name": table,
        "data": csv_str,
        "description": description,
        "is_private": IS_PRIVATE,
    }).encode("utf-8")
    print(f"    uploading {len(csv_str) / 1e6:.1f} MB CSV -> table '{table}' ...")
    headers = {"X-Dune-Api-Key": DUNE_API_KEY, "Content-Type": "application/json"}
    last = None
    for i in range(4):
        try:
            with _urlopen(DUNE_UPLOAD_URL, data=payload, headers=headers, timeout=600) as r:
                body = r.read().decode()
                print(f"    Dune OK [{r.status}]: {body[:200]}")
                return
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:600]
            # Client errors (bad request / auth) won't fix themselves -> stop.
            if 400 <= e.code < 500 and e.code != 429:
                raise RuntimeError(f"Dune upload rejected [{e.code}]: {msg}")
            last = f"[{e.code}] {msg}"
        except Exception as e:  # noqa: BLE001
            last = str(e)
        print(f"    upload attempt {i + 1} failed: {last}; retrying")
        time.sleep(3 * (i + 1))
    raise RuntimeError(f"Dune upload failed after retries: {last}")


def main():
    if not DUNE_API_KEY:
        sys.exit("ERROR: DUNE_API_KEY environment variable is required.")

    today = dt.datetime.now(dt.timezone.utc).date()
    # Last WINDOW_DAYS complete days, current day excluded.
    days = sorted(today - dt.timedelta(days=i) for i in range(1, WINDOW_DAYS + 1))
    expected = WINDOW_DAYS * SECONDS_PER_DAY
    print(f"UTC today = {today}")
    print(f"Window    = {days[0]} .. {days[-1]}  ({WINDOW_DAYS} days; current day ignored)")
    print(f"Symbols   = {', '.join(SYMBOLS)}")

    pushed, skipped, failures = [], [], []
    for symbol in SYMBOLS:
        print(f"== {symbol} ==")
        if not binance_symbol_exists(symbol):
            print(f"    SKIP: '{symbol}' is not a valid Binance spot symbol")
            skipped.append(symbol)
            continue
        try:
            csv_str, n = build_csv(symbol, days)
            if n < expected * 0.95:
                print(f"    NOTE: {n:,} rows (< ~{expected:,}); pair may be newly listed")
            table = f"{TABLE_PREFIX}{symbol.lower()}{TABLE_SUFFIX}"
            desc = (f"Binance {symbol} spot 1s OHLC, rolling {WINDOW_DAYS}-day window "
                    f"{days[0]}..{days[-1]} (UTC). Auto-updated daily.")
            upload_to_dune(table, csv_str, desc)
            print(f"    DONE: {n:,} rows -> dune.<your_handle>.{table}")
            pushed.append(symbol)
        except Exception as e:  # noqa: BLE001 - keep going with other symbols
            print(f"    ERROR for {symbol}: {e}")
            failures.append(symbol)

    print("\nSummary")
    print(f"  pushed : {', '.join(pushed) or '-'}")
    print(f"  skipped: {', '.join(skipped) or '-'}  (not on Binance)")
    print(f"  failed : {', '.join(failures) or '-'}")

    if failures:
        sys.exit(f"Completed with failures: {', '.join(failures)}")
    print("Done.")


if __name__ == "__main__":
    main()
