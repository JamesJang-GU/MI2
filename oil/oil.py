# -*- coding: utf-8 -*-
"""
Energy Market Dashboard
- VLSFO / Bunker Price: Ship & Bunker
- Crude Oil: Opinet (Dubai / Brent / WTI)
- Static GitHub Pages Build Supported

Generated files:
  Bunker Price/
  ├── index.html
  ├── shipandbunker_vlsfo.csv
  ├── bunker_forecast.csv
  ├── bunker_price_data.db
  └── OilPrice/
      ├── index.html
      └── opinet_oil_prices.csv
"""

import argparse
import datetime as dt
import json
import os
import re
import socket
import sqlite3
import sys
import threading
import time
import urllib.parse
import warnings
import webbrowser
from datetime import datetime
from io import StringIO
from pathlib import Path

warnings.filterwarnings("ignore")

import pandas as pd
from bs4 import BeautifulSoup
from flask import Flask, Response, render_template_string, request
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

app = Flask(__name__)

# =============================================================================
# 1. Configuration
# =============================================================================

BASE_DIR = Path(__file__).resolve().parent

DB_FILENAME = "bunker_price_data.db"

PRODUCT_TABLE = "product_vlsfo"
FORECAST_TABLE = "bunker_forecast"
OIL_TABLE = "oil_prices"

PRODUCT_CSV_PATH = BASE_DIR / "shipandbunker_vlsfo.csv"
FORECAST_CSV_PATH = BASE_DIR / "bunker_forecast.csv"

OIL_DIR = BASE_DIR / "OilPrice"
OIL_HTML_PATH = OIL_DIR / "index.html"
OIL_CSV_PATH = OIL_DIR / "opinet_oil_prices.csv"

# Ship & Bunker
BASE_SOURCE_URL = "https://shipandbunker.com/prices"

ROTTERDAM_URL = (
    "https://shipandbunker.com/prices/emea/nwe/nl-rtm-rotterdam#_VLSFO"
)
FUJAIRAH_URL = (
    "https://shipandbunker.com/prices/emea/me/ae-fjr-fujairah#_VLSFO"
)
SINGAPORE_URL = (
    "https://shipandbunker.com/prices/apac/sea/sg-sin-singapore#_VLSFO"
)

URLS = [
    ROTTERDAM_URL,
    FUJAIRAH_URL,
    SINGAPORE_URL,
]

ALL_PORTS = [
    "Rotterdam",
    "Fujairah",
    "Singapore",
]

FORECAST_URLS = {
    "Rotterdam": ROTTERDAM_URL,
    "Fujairah": FUJAIRAH_URL,
    "Singapore": SINGAPORE_URL,
}

FORECAST_PRODUCT_COLUMNS = [
    "VLSFO",
    "MGO",
    "LSMGO",
    "HSFO",
]

# Opinet
OIL_SOURCE_URL = "https://www.opinet.co.kr/glopcoilSelect.do"

OIL_ALL_TYPES = [
    "Dubai",
    "Brent",
    "WTI",
]

OIL_DEFAULT_START = "2008-01-02"

# Terminal Colors
TERM_BG = "#02060b"
TERM_PANEL = "#07111c"
TERM_PANEL_2 = "#0b1825"
TERM_BORDER = "#23394d"
TERM_TEXT = "#dce9f5"
TERM_MUTED = "#8aa0b5"
TERM_ORANGE = "#ff8a00"
TERM_BLUE = "#39a8ff"
TERM_GREEN = "#2bd682"
TERM_RED = "#ff5c5c"
TERM_PURPLE = "#be7bff"

DATA_CACHE = {
    "df": None,
    "fetched_at": None,
}

FORECAST_CACHE = {
    "df": None,
    "fetched_at": None,
    "message": "",
}

OIL_CACHE = {
    "df": None,
    "fetched_at": None,
}

def log(message):
    print(message, flush=True)

# =============================================================================
# 2. SQLite Utilities
# =============================================================================

def get_db_path():
    return BASE_DIR / DB_FILENAME

def get_db_conn():
    conn = sqlite3.connect(get_db_path())

    conn.execute("""
        CREATE TABLE IF NOT EXISTS data_meta (
            dataset TEXT PRIMARY KEY,
            fetched_at TEXT,
            updated_at TEXT
        )
    """)

    conn.commit()
    return conn

def db_table_exists(table_name):
    conn = get_db_conn()

    try:
        row = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type='table' AND name=?
            """,
            (table_name,),
        ).fetchone()

        return row is not None

    finally:
        conn.close()

def set_dataset_meta(dataset, fetched_at=None):
    fetched_at = fetched_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db_conn()

    try:
        conn.execute("""
            INSERT INTO data_meta(dataset, fetched_at, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(dataset) DO UPDATE SET
                fetched_at=excluded.fetched_at,
                updated_at=excluded.updated_at
        """, (dataset, fetched_at, updated_at))

        conn.commit()

    finally:
        conn.close()

def get_dataset_meta(dataset):
    conn = get_db_conn()

    try:
        row = conn.execute(
            "SELECT fetched_at FROM data_meta WHERE dataset=?",
            (dataset,),
        ).fetchone()

        return row[0] if row and row[0] else "-"

    finally:
        conn.close()

def save_dataframe_to_db(df, table_name, dataset_name):
    if df is None or df.empty:
        return "-"

    out = df.copy()

    for column in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[column]):
            out[column] = pd.to_datetime(
                out[column],
                errors="coerce",
            ).dt.strftime("%Y-%m-%d %H:%M:%S")

    fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db_conn()

    try:
        out.to_sql(
            table_name,
            conn,
            if_exists="replace",
            index=False,
        )

        conn.commit()

    finally:
        conn.close()

    set_dataset_meta(dataset_name, fetched_at)

    log(
        f"[DB] Saved / table={table_name} / rows={len(out):,}"
    )

    return fetched_at

def load_dataframe_from_db(table_name):
    if not db_table_exists(table_name):
        return pd.DataFrame()

    conn = get_db_conn()

    try:
        return pd.read_sql_query(
            f"SELECT * FROM {table_name}",
            conn,
        )

    finally:
        conn.close()

def atomic_write_text(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)

    temp_path = path.with_suffix(path.suffix + ".tmp")

    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)

# =============================================================================
# 3. Selenium Driver
# =============================================================================

def make_driver(headless=True):
    options = Options()

    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1600,1000")
    else:
        options.add_argument("--start-maximized")

    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-notifications")

    return webdriver.Chrome(options=options)

# =============================================================================
# 4. Ship & Bunker / VLSFO Crawler
# =============================================================================

def try_click_all_for_vlsfo(driver):
    return driver.execute_script("""
        if (typeof Highcharts === 'undefined' || !Highcharts.charts) {
            return {ok:false};
        }

        for (let i = 0; i < Highcharts.charts.length; i++) {
            const chart = Highcharts.charts[i];

            if (!chart || !chart.series || chart.series.length === 0) {
                continue;
            }

            const hasVLSFO = chart.series.some(
                series => series && series.name && series.name.includes('VLSFO')
            );

            if (!hasVLSFO) continue;

            if (
                !chart.rangeSelector ||
                !chart.rangeSelector.buttons ||
                chart.rangeSelector.buttons.length === 0
            ) {
                return {ok:false};
            }

            try {
                const lastIndex = chart.rangeSelector.buttons.length - 1;
                chart.rangeSelector.clickButton(lastIndex, true);

                return {
                    ok: true,
                    chartIndex: i
                };

            } catch(e) {
                return {ok:false};
            }
        }

        return {ok:false};
    """)

def extract_vlsfo_full_data(driver):
    return driver.execute_script("""
        if (typeof Highcharts === 'undefined' || !Highcharts.charts) {
            return [];
        }

        function normalizePoint(pt) {
            if (Array.isArray(pt) && pt.length >= 2) {
                return { x: pt[0], y: pt[1] };
            }

            if (
                pt &&
                typeof pt === 'object' &&
                pt.x !== undefined &&
                pt.y !== undefined
            ) {
                return { x: pt.x, y: pt.y };
            }

            return null;
        }

        let results = [];

        Highcharts.charts.forEach((chart, chartIndex) => {
            if (!chart || !chart.series || chart.series.length === 0) {
                return;
            }

            chart.series.forEach((series, seriesIndex) => {
                if (
                    !series ||
                    !series.name ||
                    !series.name.includes('VLSFO')
                ) {
                    return;
                }

                let rawPoints = [];
                let sourceType = "none";

                if (
                    series.options &&
                    Array.isArray(series.options.data) &&
                    series.options.data.length > 0
                ) {
                    rawPoints = series.options.data;
                    sourceType = "series.options.data";

                } else if (
                    series.userOptions &&
                    Array.isArray(series.userOptions.data) &&
                    series.userOptions.data.length > 0
                ) {
                    rawPoints = series.userOptions.data;
                    sourceType = "series.userOptions.data";

                } else if (
                    Array.isArray(series.data) &&
                    series.data.length > 0
                ) {
                    rawPoints = series.data;
                    sourceType = "series.data";
                }

                const points = rawPoints
                    .map(normalizePoint)
                    .filter(
                        point =>
                            point &&
                            point.x !== undefined &&
                            point.y !== undefined &&
                            point.y !== null
                    );

                results.push({
                    chartIndex: chartIndex,
                    seriesIndex: seriesIndex,
                    seriesName: series.name,
                    sourceType: sourceType,
                    pointCount: points.length,
                    data: points
                });
            });
        });

        return results;
    """)

def collect_all_data():
    log("[VLSFO] Ship & Bunker crawl started")

    driver = make_driver()
    all_rows = []

    try:
        for url in URLS:
            log(f"[VLSFO] Accessing: {url}")

            driver.get(url)
            time.sleep(5)

            try_click_all_for_vlsfo(driver)
            time.sleep(2)

            extracted = extract_vlsfo_full_data(driver)

            for item in extracted:
                port = item["seriesName"].replace(
                    " VLSFO",
                    "",
                ).strip()

                for point in item["data"]:
                    x = point.get("x")
                    y = point.get("y")

                    if x is None or y is None:
                        continue

                    all_rows.append({
                        "port": port,
                        "series_name": item["seriesName"],
                        "source_type": item["sourceType"],
                        "timestamp_ms": x,
                        "price": y,
                        "source_url": url,
                    })

    finally:
        try:
            driver.quit()
        except Exception:
            pass

    df = pd.DataFrame(all_rows)

    if df.empty:
        raise RuntimeError("No VLSFO data collected.")

    df["timestamp_ms"] = pd.to_numeric(
        df["timestamp_ms"],
        errors="coerce",
    )

    df["price"] = pd.to_numeric(
        df["price"],
        errors="coerce",
    )

    df = df.dropna(
        subset=["timestamp_ms", "price"],
    )

    df["date"] = pd.to_datetime(
        df["timestamp_ms"],
        unit="ms",
        errors="coerce",
    )

    df = df.dropna(subset=["date"])

    df["date_str"] = df["date"].dt.strftime("%Y-%m-%d")

    df = (
        df.drop_duplicates(
            subset=["port", "date_str", "price"],
        )
        .sort_values(["port", "date"])
        .reset_index(drop=True)
    )

    save_dataframe_to_db(
        df,
        PRODUCT_TABLE,
        PRODUCT_TABLE,
    )

    return df

def load_or_collect_data(force_refresh=False):
    if not force_refresh and DATA_CACHE["df"] is not None:
        return (
            DATA_CACHE["df"].copy(),
            DATA_CACHE["fetched_at"],
        )

    db_df = pd.DataFrame()
    db_fetched_at = "-"

    try:
        db_df = load_dataframe_from_db(PRODUCT_TABLE)

        if not db_df.empty:
            db_df["date"] = pd.to_datetime(
                db_df["date"],
                errors="coerce",
            )

            db_df["price"] = pd.to_numeric(
                db_df["price"],
                errors="coerce",
            )

            db_df = db_df.dropna(subset=["date", "price"])

            db_fetched_at = get_dataset_meta(PRODUCT_TABLE)

    except Exception as error:
        log(f"[VLSFO] DB Load Warning: {error}")

    if not force_refresh and not db_df.empty:
        DATA_CACHE["df"] = db_df.copy()
        DATA_CACHE["fetched_at"] = db_fetched_at

        return db_df, db_fetched_at

    try:
        fresh_df = collect_all_data()

        fetched_at = get_dataset_meta(PRODUCT_TABLE)

        DATA_CACHE["df"] = fresh_df.copy()
        DATA_CACHE["fetched_at"] = fetched_at

        return fresh_df, fetched_at

    except Exception as error:
        if not db_df.empty:
            log(
                f"[VLSFO] Crawl failed. Using cached DB data: {error}"
            )

            DATA_CACHE["df"] = db_df.copy()
            DATA_CACHE["fetched_at"] = db_fetched_at

            return db_df, db_fetched_at

        raise error

# =============================================================================
# 5. Bunker Forecast Crawler
# =============================================================================

def clean_forecast_number(value):
    text = str(value).strip()

    if not text or text.lower() == "nan" or text == "-":
        return None

    text = text.replace(",", "")
    text = re.sub(r"[^0-9.+-]", "", text)

    if not text or text in ["+", "-", "."]:
        return None

    try:
        return float(text)
    except Exception:
        return None

def forecast_period_to_date(period):
    text = str(period or "").strip()

    match = re.search(
        r"(20\d{2})\s*Q([1-4])",
        text,
        flags=re.I,
    )

    if not match:
        return pd.NaT

    year = int(match.group(1))
    quarter = int(match.group(2))

    month_day = {
        1: (3, 31),
        2: (6, 30),
        3: (9, 30),
        4: (12, 31),
    }[quarter]

    return pd.Timestamp(
        year=year,
        month=month_day[0],
        day=month_day[1],
    )

def normalize_forecast_table(raw_df):
    if raw_df is None or raw_df.empty:
        return pd.DataFrame()

    df = raw_df.copy()

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [
            " ".join(
                [
                    str(item)
                    for item in column
                    if str(item) != "nan"
                ]
            ).strip()
            for column in df.columns
        ]
    else:
        df.columns = [
            str(column).strip()
            for column in df.columns
        ]

    df = df.dropna(how="all").dropna(axis=1, how="all")

    if df.empty:
        return pd.DataFrame()

    column_text = " ".join(
        map(str, df.columns)
    ).upper()

    first_row_text = (
        " ".join(map(str, df.iloc[0].tolist())).upper()
        if len(df)
        else ""
    )

    if (
        not any(
            product in column_text
            for product in FORECAST_PRODUCT_COLUMNS
        )
        and any(
            product in first_row_text
            for product in FORECAST_PRODUCT_COLUMNS
        )
    ):
        df.columns = [
            str(value).strip()
            for value in df.iloc[0].tolist()
        ]

        df = df.iloc[1:].reset_index(drop=True)

    seen = {}
    unique_columns = []

    for column in df.columns:
        base = str(column).strip() or "COL"
        count = seen.get(base, 0)

        unique_columns.append(
            base if count == 0 else f"{base}_{count}"
        )

        seen[base] = count + 1

    df.columns = unique_columns

    product_positions = {}

    for index, column in enumerate(df.columns):
        upper = str(column).strip().upper()

        for product in FORECAST_PRODUCT_COLUMNS:
            if upper == product or product in upper.split():
                product_positions[product] = index

    if not product_positions:
        return pd.DataFrame()

    first_product_position = min(
        product_positions.values()
    )

    period_position = max(
        0,
        first_product_position - 1,
    )

    for column_index in range(df.shape[1]):
        if column_index in product_positions.values():
            continue

        sample = df.iloc[:, column_index].astype(str)

        if sample.str.contains(
            r"20\d{2}|Q[1-4]|Full Year",
            case=False,
            regex=True,
            na=False,
        ).any():
            period_position = column_index
            break

    out = pd.DataFrame()

    out["Period"] = (
        df.iloc[:, period_position]
        .astype(str)
        .str.strip()
    )

    for product in FORECAST_PRODUCT_COLUMNS:
        position = product_positions.get(product)

        if position is None:
            out[product] = None
        else:
            out[product] = df.iloc[:, position].apply(
                clean_forecast_number
            )

    out = out[
        out["Period"].str.contains(
            r"20\d{2}|Q[1-4]|Full Year",
            case=False,
            regex=True,
            na=False,
        )
    ]

    out = out.dropna(
        subset=FORECAST_PRODUCT_COLUMNS,
        how="all",
    )

    if out.empty:
        return pd.DataFrame()

    out["graph_date"] = out["Period"].apply(
        forecast_period_to_date
    )

    out["source_url"] = ""

    return out[
        ["Period"]
        + FORECAST_PRODUCT_COLUMNS
        + ["graph_date", "source_url"]
    ].reset_index(drop=True)

def parse_bunker_forecast_from_html(html, port_name):
    try:
        tables = pd.read_html(StringIO(html))

        best_df = pd.DataFrame()
        best_score = -1

        for table in tables:
            normalized = normalize_forecast_table(table)

            if normalized.empty:
                continue

            text = (
                " ".join(map(str, table.columns))
                + " "
                + " ".join(
                    map(
                        str,
                        table.head(3).values.ravel(),
                    )
                )
            ).upper()

            score = sum(
                5
                for product in FORECAST_PRODUCT_COLUMNS
                if product in text
            )

            if normalized["graph_date"].notna().any():
                score += 3

            if score > best_score:
                best_score = score
                best_df = normalized

        if not best_df.empty:
            best_df["port"] = port_name
            return best_df

    except Exception as error:
        log(
            f"[FORECAST] pandas parse warning "
            f"/ {port_name} / {error}"
        )

    soup = BeautifulSoup(html, "html.parser")

    table = soup.find("table")

    if table is None:
        raise RuntimeError(
            f"{port_name}: Bunker forecast table not found."
        )

    rows = []

    for tr in table.find_all("tr"):
        cells = [
            cell.get_text(" ", strip=True)
            for cell in tr.find_all(["th", "td"])
        ]

        if cells:
            rows.append(cells)

    if not rows:
        raise RuntimeError(
            f"{port_name}: Bunker forecast rows not found."
        )

    max_length = max(len(row) for row in rows)

    rows = [
        row + [""] * (max_length - len(row))
        for row in rows
    ]

    raw_df = pd.DataFrame(rows)

    normalized = normalize_forecast_table(raw_df)

    if normalized.empty:
        raise RuntimeError(
            f"{port_name}: Unable to normalize forecast table."
        )

    normalized["port"] = port_name

    return normalized

def collect_all_forecast_data():
    log("[FORECAST] Ship & Bunker forecast crawl started")

    driver = make_driver()
    frames = []
    errors = []

    try:
        for port_name, url in FORECAST_URLS.items():
            try:
                log(f"[FORECAST] Accessing: {port_name}")

                driver.get(url)
                time.sleep(5)

                forecast_df = parse_bunker_forecast_from_html(
                    driver.page_source,
                    port_name,
                )

                forecast_df["port"] = port_name
                forecast_df["source_url"] = url

                frames.append(forecast_df)

            except Exception as error:
                errors.append(
                    f"{port_name}: {error}"
                )

    finally:
        try:
            driver.quit()
        except Exception:
            pass

    if not frames:
        raise RuntimeError(
            "All bunker forecast crawling failed: "
            + " | ".join(errors)
        )

    out = pd.concat(
        frames,
        ignore_index=True,
        sort=False,
    )

    save_dataframe_to_db(
        out,
        FORECAST_TABLE,
        FORECAST_TABLE,
    )

    return out, " | ".join(errors)

def load_or_collect_forecast_data(force_refresh=False):
    if (
        not force_refresh
        and FORECAST_CACHE["df"] is not None
    ):
        return (
            FORECAST_CACHE["df"].copy(),
            FORECAST_CACHE["fetched_at"],
            FORECAST_CACHE["message"],
        )

    db_df = pd.DataFrame()
    db_fetched_at = "-"

    try:
        db_df = load_dataframe_from_db(FORECAST_TABLE)

        if not db_df.empty:
            for column in FORECAST_PRODUCT_COLUMNS:
                if column in db_df.columns:
                    db_df[column] = pd.to_numeric(
                        db_df[column],
                        errors="coerce",
                    )

            db_df["graph_date"] = pd.to_datetime(
                db_df["graph_date"],
                errors="coerce",
            )

            db_fetched_at = get_dataset_meta(
                FORECAST_TABLE
            )

    except Exception as error:
        log(f"[FORECAST] DB Load Warning: {error}")

    if not force_refresh and not db_df.empty:
        FORECAST_CACHE["df"] = db_df.copy()
        FORECAST_CACHE["fetched_at"] = db_fetched_at
        FORECAST_CACHE["message"] = ""

        return db_df, db_fetched_at, ""

    try:
        fresh_df, message = collect_all_forecast_data()

        fetched_at = get_dataset_meta(FORECAST_TABLE)

        FORECAST_CACHE["df"] = fresh_df.copy()
        FORECAST_CACHE["fetched_at"] = fetched_at
        FORECAST_CACHE["message"] = message

        return fresh_df, fetched_at, message

    except Exception as error:
        if not db_df.empty:
            warning = f"Using previous forecast data: {error}"

            FORECAST_CACHE["df"] = db_df.copy()
            FORECAST_CACHE["fetched_at"] = db_fetched_at
            FORECAST_CACHE["message"] = warning

            return db_df, db_fetched_at, warning

        empty = pd.DataFrame()

        return empty, "-", str(error)

# =============================================================================
# 6. Opinet / Crude Oil Crawler
# =============================================================================

def oil_normalize_date(value):
    text = str(value).strip()

    if not text or text.lower() == "nan":
        return None

    match = re.search(
        r"(\d{2,4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일",
        text,
    )

    if match:
        year, month, day = match.groups()

        year = int(year)

        if year < 100:
            year += 2000 if year < 70 else 1900

        try:
            return datetime(
                year,
                int(month),
                int(day),
            ).strftime("%Y-%m-%d")
        except Exception:
            return None

    cleaned = re.sub(r"[^0-9./-]", "", text)

    for fmt in [
        "%Y-%m-%d",
        "%Y.%m.%d",
        "%Y/%m/%d",
        "%y-%m-%d",
        "%y.%m.%d",
        "%y/%m/%d",
        "%Y%m%d",
        "%y%m%d",
    ]:
        try:
            return datetime.strptime(
                cleaned,
                fmt,
            ).strftime("%Y-%m-%d")
        except Exception:
            pass

    parsed = pd.to_datetime(
        cleaned,
        errors="coerce",
    )

    if pd.isna(parsed):
        return None

    return parsed.strftime("%Y-%m-%d")

def oil_clean_num(value):
    text = str(value).strip()

    if not text or text.lower() == "nan":
        return None

    text = text.replace(",", "")
    text = re.sub(r"[^0-9.+-]", "", text)

    if not text or text in ["+", "-", "."]:
        return None

    try:
        return float(text)
    except Exception:
        return None

def oil_set_select(driver, name, value):
    element = WebDriverWait(
        driver,
        20,
    ).until(
        EC.presence_of_element_located(
            (By.NAME, name)
        )
    )

    Select(element).select_by_value(str(value))

def oil_set_checkbox(driver, value):
    xpaths = [
        f"//input[@type='checkbox' and @value='{value}']",
        f"//input[@value='{value}']",
    ]

    for xpath in xpaths:
        elements = driver.find_elements(
            By.XPATH,
            xpath,
        )

        for element in elements:
            try:
                if not element.is_selected():
                    driver.execute_script(
                        "arguments[0].click();",
                        element,
                    )

                return True

            except Exception:
                pass

    raise RuntimeError(
        f"Oil checkbox value {value} not found."
    )

def oil_parse_html(html):
    soup = BeautifulSoup(html, "html.parser")

    tbody = (
        soup.find("tbody", {"id": "tbody2"})
        or soup.find("tbody", {"id": "tbody1"})
    )

    if tbody is None:
        raise RuntimeError(
            "Opinet oil result tbody was not found."
        )

    rows = []

    for tr in tbody.find_all("tr"):
        cells = [
            cell.get_text(" ", strip=True)
            for cell in tr.find_all(["th", "td"])
        ]

        if len(cells) >= 4:
            rows.append(cells[:4])

    if not rows:
        raise RuntimeError(
            "Opinet oil result rows were not found."
        )

    df = pd.DataFrame(
        rows,
        columns=[
            "DATE",
            "Dubai",
            "Brent",
            "WTI",
        ],
    )

    df["DATE"] = df["DATE"].apply(
        oil_normalize_date
    )

    for oil_type in OIL_ALL_TYPES:
        df[oil_type] = df[oil_type].apply(
            oil_clean_num
        )

    df = df.dropna(subset=["DATE"])

    df = df.dropna(
        subset=OIL_ALL_TYPES,
        how="all",
    )

    df = (
        df.drop_duplicates(subset=["DATE"])
        .sort_values("DATE")
        .reset_index(drop=True)
    )

    if df.empty:
        raise RuntimeError(
            "Opinet oil parsing result is empty."
        )

    return df

def oil_collect_data(start_date=None, end_date=None):
    start_date = pd.to_datetime(
        start_date or OIL_DEFAULT_START
    )

    end_date = pd.to_datetime(
        end_date
        or datetime.today().strftime("%Y-%m-%d")
    )

    if start_date > end_date:
        raise ValueError(
            "Oil start date cannot be after end date."
        )

    log("[OIL] Opinet crawl started")

    driver = make_driver()

    try:
        driver.get(OIL_SOURCE_URL)

        WebDriverWait(driver, 30).until(
            lambda current_driver:
                current_driver.execute_script(
                    "return document.readyState"
                ) == "complete"
        )

        WebDriverWait(driver, 20).until(
            lambda current_driver:
                len(
                    current_driver.find_elements(
                        By.NAME,
                        "TERM",
                    )
                ) > 0
        )

        radios = driver.find_elements(
            By.NAME,
            "TERM",
        )

        daily_found = False

        for radio in radios:
            if radio.get_attribute("value") == "D":
                driver.execute_script(
                    "arguments[0].click();",
                    radio,
                )

                daily_found = True
                break

        if not daily_found:
            raise RuntimeError(
                "Daily oil price option was not found."
            )

        oil_set_select(
            driver,
            "STA_Y",
            start_date.strftime("%Y"),
        )

        oil_set_select(
            driver,
            "STA_M",
            start_date.strftime("%m"),
        )

        oil_set_select(
            driver,
            "STA_D",
            start_date.strftime("%d"),
        )

        oil_set_select(
            driver,
            "END_Y",
            end_date.strftime("%Y"),
        )

        oil_set_select(
            driver,
            "END_M",
            end_date.strftime("%m"),
        )

        oil_set_select(
            driver,
            "END_D",
            end_date.strftime("%d"),
        )

        # Dubai / Brent / WTI
        for value in ["001", "002", "003"]:
            oil_set_checkbox(driver, value)

        search_button = WebDriverWait(
            driver,
            20,
        ).until(
            EC.element_to_be_clickable(
                (By.ID, "glopcoilSelect")
            )
        )

        driver.execute_script(
            "arguments[0].click();",
            search_button,
        )

        WebDriverWait(driver, 30).until(
            lambda current_driver:
                "Dubai" in current_driver.page_source
                or "두바이" in current_driver.page_source
        )

        time.sleep(1)

        df = oil_parse_html(driver.page_source)

    finally:
        try:
            driver.quit()
        except Exception:
            pass

    save_dataframe_to_db(
        df,
        OIL_TABLE,
        OIL_TABLE,
    )

    return df

def oil_load_or_collect_data(force_refresh=False):
    if not force_refresh and OIL_CACHE["df"] is not None:
        return (
            OIL_CACHE["df"].copy(),
            OIL_CACHE["fetched_at"],
        )

    db_df = pd.DataFrame()
    db_fetched_at = "-"

    try:
        db_df = load_dataframe_from_db(OIL_TABLE)

        if not db_df.empty:
            db_df["DATE"] = pd.to_datetime(
                db_df["DATE"],
                errors="coerce",
            )

            for oil_type in OIL_ALL_TYPES:
                db_df[oil_type] = pd.to_numeric(
                    db_df[oil_type],
                    errors="coerce",
                )

            db_df = db_df.dropna(subset=["DATE"])

            db_fetched_at = get_dataset_meta(OIL_TABLE)

    except Exception as error:
        log(f"[OIL] DB Load Warning: {error}")

    if not force_refresh and not db_df.empty:
        OIL_CACHE["df"] = db_df.copy()
        OIL_CACHE["fetched_at"] = db_fetched_at

        return db_df, db_fetched_at

    try:
        fresh_df = oil_collect_data()

        fetched_at = get_dataset_meta(OIL_TABLE)

        OIL_CACHE["df"] = fresh_df.copy()
        OIL_CACHE["fetched_at"] = fetched_at

        return fresh_df, fetched_at

    except Exception as error:
        if not db_df.empty:
            log(
                f"[OIL] Crawl failed. Using cached data: {error}"
            )

            OIL_CACHE["df"] = db_df.copy()
            OIL_CACHE["fetched_at"] = db_fetched_at

            return db_df, db_fetched_at

        raise error

def oil_to_long(df):
    if df is None or df.empty:
        return pd.DataFrame(
            columns=[
                "date",
                "oil_type",
                "price",
            ]
        )

    work = df.copy()

    work["DATE"] = pd.to_datetime(
        work["DATE"],
        errors="coerce",
    )

    work = work.dropna(subset=["DATE"])

    long_df = work.melt(
        id_vars=["DATE"],
        value_vars=OIL_ALL_TYPES,
        var_name="oil_type",
        value_name="price",
    )

    long_df["price"] = pd.to_numeric(
        long_df["price"],
        errors="coerce",
    )

    long_df = long_df.dropna(subset=["price"])

    long_df = long_df.rename(
        columns={"DATE": "date"}
    )

    return (
        long_df.sort_values(["oil_type", "date"])
        .reset_index(drop=True)
    )

# =============================================================================
# 7. Data Export Helpers
# =============================================================================

def export_static_csv_files(product_df, forecast_df):
    if product_df is not None and not product_df.empty:
        out = product_df.copy()

        out["date"] = pd.to_datetime(
            out["date"],
            errors="coerce",
        ).dt.strftime("%Y-%m-%d")

        out.to_csv(
            PRODUCT_CSV_PATH,
            index=False,
            encoding="utf-8-sig",
        )

        log(f"[EXPORT] {PRODUCT_CSV_PATH.name}")

    if forecast_df is not None and not forecast_df.empty:
        out = forecast_df.copy()

        if "graph_date" in out.columns:
            out["graph_date"] = pd.to_datetime(
                out["graph_date"],
                errors="coerce",
            ).dt.strftime("%Y-%m-%d")

        out.to_csv(
            FORECAST_CSV_PATH,
            index=False,
            encoding="utf-8-sig",
        )

        log(f"[EXPORT] {FORECAST_CSV_PATH.name}")

def export_oil_csv(oil_long_df):
    if oil_long_df is None or oil_long_df.empty:
        return

    OIL_DIR.mkdir(parents=True, exist_ok=True)

    out = oil_long_df.copy()

    out["date"] = pd.to_datetime(
        out["date"],
        errors="coerce",
    ).dt.strftime("%Y-%m-%d")

    out.to_csv(
        OIL_CSV_PATH,
        index=False,
        encoding="utf-8-sig",
    )

    log(f"[EXPORT] OilPrice/{OIL_CSV_PATH.name}")

def dataframe_to_records(df, date_column, rename_map=None):
    if df is None or df.empty:
        return []

    out = df.copy()

    out[date_column] = pd.to_datetime(
        out[date_column],
        errors="coerce",
    ).dt.strftime("%Y-%m-%d")

    out = out.where(pd.notna(out), None)

    if rename_map:
        out = out.rename(columns=rename_map)

    records = []

    for row in out.to_dict(orient="records"):
        clean_row = {}

        for key, value in row.items():
            if isinstance(value, (pd.Timestamp, dt.datetime)):
                clean_row[key] = value.strftime("%Y-%m-%d")
            elif isinstance(value, float):
                clean_row[key] = round(value, 6)
            else:
                clean_row[key] = value

        records.append(clean_row)

    return records

# =============================================================================
# 8. Bloomberg Terminal Static HTML
# =============================================================================

TERMINAL_CSS = """
:root {
  --bg: #02060b;
  --panel: #07111c;
  --panel2: #0b1825;
  --panel3: #0e2131;
  --line: #23394d;
  --text: #dce9f5;
  --muted: #8aa0b5;
  --orange: #ff8a00;
  --blue: #39a8ff;
  --green: #2bd682;
  --red: #ff5c5c;
  --purple: #be7bff;
}
* { box-sizing: border-box; }
html, body { min-height: 100%; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: "Roboto Mono", Consolas, "Noto Sans KR", monospace;
  font-size: 12px;
}
button, input { font: inherit; }
a { color: inherit; text-decoration: none; }

.topbar {
  min-height: 54px;
  padding: 0 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 15px;
  border-bottom: 1px solid var(--line);
  background: #050a11;
}
.brand {
  display: flex;
  align-items: center;
  gap: 12px;
}
.brand-mark {
  color: var(--orange);
  font-size: 18px;
  font-weight: 900;
}
.brand-title {
  font-size: 13px;
  color: white;
  letter-spacing: .8px;
  font-weight: 700;
}
.nav-link {
  padding: 7px 11px;
  border: 1px solid #34526e;
  color: var(--blue);
  font-size: 11px;
}
.nav-link:hover {
  color: #fff;
  background: #12344f;
}

.terminal {
  max-width: 1720px;
  margin: 0 auto;
  padding: 18px 20px 32px;
}
.status {
  padding: 9px 12px;
  display: flex;
  justify-content: space-between;
  gap: 12px;
  color: var(--muted);
  border: 1px solid var(--line);
  background: var(--panel);
  font-size: 10px;
}
.workspace {
  margin-top: 14px;
  display: grid;
  grid-template-columns: 245px minmax(0, 1fr);
  gap: 14px;
}
.panel {
  border: 1px solid var(--line);
  background: var(--panel);
}
.panel-header {
  min-height: 39px;
  padding: 0 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 9px;
  background: var(--panel2);
  border-bottom: 1px solid var(--line);
}
.panel-title {
  color: var(--orange);
  font-size: 10px;
  letter-spacing: .8px;
  font-weight: 700;
}
.filter-section {
  padding: 13px;
  border-bottom: 1px solid #172a3a;
}
.label {
  display: block;
  color: var(--muted);
  font-size: 10px;
  letter-spacing: .65px;
  margin-bottom: 8px;
  font-weight: 700;
}
.button-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 5px;
}
.all-button { grid-column: span 2; }
.btn {
  min-height: 31px;
  padding: 6px 8px;
  border: 1px solid var(--line);
  background: #07101a;
  color: var(--muted);
  cursor: pointer;
  font-size: 10px;
  font-weight: 700;
}
.btn:hover {
  border-color: var(--orange);
  color: var(--orange);
}
.btn.active {
  border-color: var(--orange);
  background: var(--orange);
  color: #0a0a0a;
}
.series-list {
  display: grid;
  gap: 6px;
}
.series-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px;
  border: 1px solid #1d3041;
  background: #06101a;
  color: #d6e2ec;
  cursor: pointer;
}
.series-item input { accent-color: var(--orange); }

.heading {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 12px;
}
h1 {
  color: #fff;
  margin: 0;
  font-size: 20px;
  letter-spacing: .2px;
}
.subtitle {
  color: var(--muted);
  font-size: 10px;
  margin-top: 6px;
}
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 9px;
  margin-bottom: 12px;
}
.kpi {
  min-height: 94px;
  padding: 12px;
  border: 1px solid var(--line);
  background: var(--panel);
}
.kpi-label {
  display: block;
  min-height: 25px;
  color: var(--muted);
  font-size: 9px;
  line-height: 1.3;
}
.kpi-value {
  display: block;
  margin-top: 10px;
  color: #fff;
  font-size: 18px;
  font-weight: 700;
}
.kpi-sub {
  display: block;
  margin-top: 5px;
  color: var(--muted);
  font-size: 9px;
}
.positive { color: var(--green); }
.negative { color: var(--red); }
#chart { width: 100%; min-height: 510px; }
.empty {
  padding: 45px 15px;
  color: var(--muted);
  text-align: center;
}
.table-wrap {
  overflow-x: auto;
  padding: 12px;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 11px;
}
th, td {
  padding: 9px 10px;
  text-align: right;
  border-bottom: 1px solid #1c3041;
}
th {
  color: var(--muted);
  background: #0a1723;
  font-size: 10px;
}
td:first-child, th:first-child { text-align: left; }
@media (max-width: 1100px) {
  .workspace { grid-template-columns: 1fr; }
  .kpi-grid { grid-template-columns: repeat(3, 1fr); }
}
@media (max-width: 650px) {
  .terminal { padding: 12px; }
  .kpi-grid { grid-template-columns: repeat(2, 1fr); }
  .status { display: block; line-height: 1.8; }
}
"""

def generate_product_static_html(product_df, forecast_df, fetched_at):
    product_records = dataframe_to_records(
        product_df,
        "date",
        rename_map={
            "date": "DATE",
            "port": "PORT",
            "price": "PRICE",
        },
    )

    forecast_records = dataframe_to_records(
        forecast_df,
        "graph_date",
        rename_map={
            "graph_date": "GRAPH_DATE",
            "port": "PORT",
        },
    )

    product_json = json.dumps(
        product_records,
        ensure_ascii=False,
    )

    forecast_json = json.dumps(
        forecast_records,
        ensure_ascii=False,
    )

    html = f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MI Terminal | Bunker Price</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>{TERMINAL_CSS}</style>
</head>

<body>
<header class="topbar">
  <div class="brand">
    <span class="brand-mark">MI&lt;GO&gt;</span>
    <span class="brand-title">BUNKER PRICE MONITOR</span>
  </div>
  <a class="nav-link" href="./OilPrice/index.html">CRUDE OIL →</a>
</header>

<main class="terminal">
  <section class="status">
    <span>SYSTEM / SHIP &amp; BUNKER DATA ONLINE</span>
    <span>LAST REFRESH: {fetched_at}</span>
  </section>

  <div class="workspace">
    <aside class="panel">
      <div class="panel-header">
        <span class="panel-title">FUNCTIONS / FILTERS</span>
      </div>

      <section class="filter-section">
        <span class="label">TIME RANGE</span>
        <div class="button-grid">
          <button class="btn" data-period="1Y" onclick="setPreset('1Y')">1Y</button>
          <button class="btn" data-period="3Y" onclick="setPreset('3Y')">3Y</button>
          <button class="btn" data-period="5Y" onclick="setPreset('5Y')">5Y</button>
          <button class="btn" data-period="YTD" onclick="setPreset('YTD')">YTD</button>
          <button class="btn all-button active" data-period="ALL" onclick="setPreset('ALL')">ALL HISTORY</button>
        </div>
      </section>

      <section class="filter-section">
        <span class="label">SELECT PORTS</span>
        <div class="series-list">
          <label class="series-item"><input type="checkbox" value="Rotterdam" checked onchange="render()"> Rotterdam</label>
          <label class="series-item"><input type="checkbox" value="Fujairah" checked onchange="render()"> Fujairah</label>
          <label class="series-item"><input type="checkbox" value="Singapore" checked onchange="render()"> Singapore</label>
        </div>
      </section>

      <section class="filter-section">
        <span class="label">MOVING AVERAGE</span>
        <div class="button-grid">
          <button class="btn active" data-ma="OFF" onclick="setMA('OFF')">OFF</button>
          <button class="btn" data-ma="90" onclick="setMA('90')">90D</button>
          <button class="btn" data-ma="180" onclick="setMA('180')">180D</button>
          <button class="btn" data-ma="365" onclick="setMA('365')">365D</button>
        </div>
      </section>

      <section class="filter-section">
        <button class="btn" style="width:100%;border-color:#34526e;color:#39a8ff;" onclick="downloadCSV()">EXPORT FILTERED CSV</button>
      </section>
    </aside>

    <section>
      <div class="heading">
        <div>
          <h1>VLSFO BUNKER PRICE</h1>
          <div class="subtitle">Rotterdam · Fujairah · Singapore / USD per Metric Ton</div>
        </div>
      </div>

      <section class="kpi-grid">
        <div class="kpi">
          <span class="kpi-label">LATEST<br>SELECTED AVG</span>
          <span class="kpi-value" id="latestValue">-</span>
          <span class="kpi-sub" id="latestDate">-</span>
        </div>
        <div class="kpi">
          <span class="kpi-label">PERIOD<br>AVERAGE</span>
          <span class="kpi-value" id="averageValue">-</span>
          <span class="kpi-sub">USD / MT</span>
        </div>
        <div class="kpi">
          <span class="kpi-label">PERIOD<br>HIGH / LOW</span>
          <span class="kpi-value" id="highLow">-</span>
          <span class="kpi-sub">HIGH / LOW</span>
        </div>
        <div class="kpi">
          <span class="kpi-label">CHANGE<br>FIRST → LAST</span>
          <span class="kpi-value" id="changeValue">-</span>
          <span class="kpi-sub" id="changeRate">-</span>
        </div>
        <div class="kpi">
          <span class="kpi-label">OBSERVATIONS<br>/ PORTS</span>
          <span class="kpi-value" id="recordCount">-</span>
          <span class="kpi-sub" id="portCount">-</span>
        </div>
      </section>

      <section class="panel">
        <div class="panel-header">
          <span class="panel-title">VLSFO PRICE HISTORY / RANGE SLIDER ENABLED</span>
          <span class="panel-title">FORECAST = DOTTED LINE</span>
        </div>
        <div id="chart"></div>
      </section>

      <section class="panel" style="margin-top:14px;">
        <div class="panel-header">
          <span class="panel-title">BUNKER PRICE FORECAST / USD PER MT</span>
        </div>
        <div class="table-wrap" id="forecastTable"></div>
      </section>
    </section>
  </div>
</main>

<script>
const PRODUCT_DATA = {product_json};
const FORECAST_DATA = {forecast_json};

const COLORS = {{
  Rotterdam: "#39a8ff",
  Fujairah: "#2bd682",
  Singapore: "#be7bff"
}};

let currentRange = null;
let currentPreset = "ALL";
let currentMA = "OFF";

function selectedPorts() {{
  return Array.from(document.querySelectorAll(".series-list input:checked"))
    .map(x => x.value);
}}

function formatNumber(value, decimals=1) {{
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return Number(value).toLocaleString("en-US", {{
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals
  }});
}}

function bounds() {{
  const data = [...PRODUCT_DATA].sort((a,b) => a.DATE.localeCompare(b.DATE));
  return {{
    min: data[0]?.DATE || null,
    max: data[data.length - 1]?.DATE || null
  }};
}}

function setPreset(preset) {{
  const b = bounds();
  if (!b.max) return;

  currentPreset = preset;

  if (preset === "ALL") {{
    currentRange = null;
  }} else if (preset === "YTD") {{
    currentRange = {{ start: b.max.slice(0,4) + "-01-01", end: b.max }};
  }} else {{
    const date = new Date(b.max + "T00:00:00");
    date.setFullYear(date.getFullYear() - Number(preset.replace("Y","")));
    currentRange = {{ start: date.toISOString().slice(0,10), end: b.max }};
  }}

  document.querySelectorAll("[data-period]").forEach(btn => {{
    btn.classList.toggle("active", btn.dataset.period === preset);
  }});

  render();
}}

function setMA(mode) {{
  currentMA = mode;

  document.querySelectorAll("[data-ma]").forEach(btn => {{
    btn.classList.toggle("active", btn.dataset.ma === mode);
  }});

  render();
}}

function filteredData() {{
  const ports = selectedPorts();

  return PRODUCT_DATA.filter(row => {{
    if (!ports.includes(row.PORT)) return false;
    if (!currentRange) return true;
    return row.DATE >= currentRange.start && row.DATE <= currentRange.end;
  }});
}}

function dailyAverage(rows) {{
  const grouped = {{}};

  rows.forEach(row => {{
    if (!grouped[row.DATE]) grouped[row.DATE] = [];
    grouped[row.DATE].push(Number(row.PRICE));
  }});

  return Object.keys(grouped).sort().map(date => ({{
    date,
    value: grouped[date].reduce((a,b) => a+b, 0) / grouped[date].length
  }}));
}}

function updateKpis(rows) {{
  document.getElementById("recordCount").textContent = rows.length.toLocaleString("en-US");
  document.getElementById("portCount").textContent = selectedPorts().length + " PORTS SELECTED";

  if (!rows.length) {{
    ["latestValue","averageValue","highLow","changeValue"].forEach(id => {{
      document.getElementById(id).textContent = "-";
    }});
    document.getElementById("latestDate").textContent = "NO DATA";
    document.getElementById("changeRate").textContent = "-";
    return;
  }}

  const avgByDay = dailyAverage(rows);
  const first = avgByDay[0];
  const latest = avgByDay[avgByDay.length - 1];
  const values = rows.map(row => Number(row.PRICE));

  const avg = values.reduce((a,b) => a+b, 0) / values.length;
  const high = Math.max(...values);
  const low = Math.min(...values);
  const change = latest.value - first.value;
  const rate = first.value ? change / first.value * 100 : 0;

  document.getElementById("latestValue").textContent = formatNumber(latest.value);
  document.getElementById("latestDate").textContent = latest.date + " AVG";
  document.getElementById("averageValue").textContent = formatNumber(avg);
  document.getElementById("highLow").textContent =
    formatNumber(high,0) + " / " + formatNumber(low,0);

  const el = document.getElementById("changeValue");
  el.textContent = (change > 0 ? "+" : "") + formatNumber(change);
  el.className = "kpi-value " + (change > 0 ? "positive" : change < 0 ? "negative" : "");

  document.getElementById("changeRate").textContent =
    (rate > 0 ? "+" : "") + formatNumber(rate,2) + "% / " +
    first.date + " → " + latest.date;
}}

function movingAverage(values, windowSize) {{
  return values.map((item, index) => {{
    const start = Math.max(0, index - windowSize + 1);
    const range = values.slice(start, index + 1);
    return range.reduce((sum, row) => sum + row.y, 0) / range.length;
  }});
}}

function renderChart(rows) {{
  const chart = document.getElementById("chart");

  if (!rows.length) {{
    Plotly.purge(chart);
    chart.innerHTML = '<div class="empty">NO DATA FOR SELECTED CONDITIONS</div>';
    return;
  }}

  const groups = {{}};

  rows.forEach(row => {{
    if (!groups[row.PORT]) groups[row.PORT] = [];
    groups[row.PORT].push(row);
  }});

  const traces = [];

  Object.entries(groups).forEach(([port, data]) => {{
    data.sort((a,b) => a.DATE.localeCompare(b.DATE));

    const series = data.map(row => ({{
      x: row.DATE,
      y: Number(row.PRICE)
    }}));

    traces.push({{
      x: series.map(x => x.x),
      y: series.map(x => x.y),
      type: "scatter",
      mode: "lines",
      name: port,
      line: {{ color: COLORS[port], width: 2.2 }},
      hovertemplate: "<b>" + port + "</b><br>DATE: %{{x}}<br>PRICE: $%{{y:,.1f}}/mt<extra></extra>"
    }});

    if (currentMA !== "OFF") {{
      const ma = movingAverage(series, Number(currentMA));

      traces.push({{
        x: series.map(x => x.x),
        y: ma,
        type: "scatter",
        mode: "lines",
        name: port + " " + currentMA + "D MA",
        line: {{ color: COLORS[port], width: 1.1, dash: "dot" }},
        hoverinfo: "skip"
      }});
    }}
  }});

  const b = bounds();
  const chartRange = currentRange
    ? [currentRange.start, currentRange.end]
    : [b.min, b.max];

  const layout = {{
    margin: {{ l:72, r:26, t:22, b:68 }},
    paper_bgcolor: "#07111c",
    plot_bgcolor: "#07111c",
    font: {{ family: '"Roboto Mono", Consolas, monospace', color:"#c9d6e2", size:11 }},
    hovermode: "x unified",
    legend: {{ orientation:"h", x:0, y:1.12 }},
    xaxis: {{
      type: "date",
      range: chartRange,
      gridcolor:"#1f3040",
      linecolor:"#405568",
      rangeslider: {{ visible:true, bgcolor:"#050c14", bordercolor:"#405568", thickness:.09 }}
    }},
    yaxis: {{
      title:"USD / MT",
      gridcolor:"#1f3040",
      linecolor:"#405568"
    }}
  }};

  Plotly.newPlot(chart, traces, layout, {{
    responsive:true,
    displaylogo:false,
    modeBarButtonsToRemove:["select2d","lasso2d"]
  }});

  chart.on("plotly_relayout", event => {{
    const start = event["xaxis.range[0]"];
    const end = event["xaxis.range[1]"];

    if (start && end) {{
      currentRange = {{
        start: String(start).slice(0,10),
        end: String(end).slice(0,10)
      }};
      currentPreset = "CUSTOM";

      document.querySelectorAll("[data-period]").forEach(btn => {{
        btn.classList.remove("active");
      }});

      render();
    }}
  }});
}}

function renderForecast() {{
  const selected = selectedPorts();

  const rows = FORECAST_DATA.filter(row => selected.includes(row.PORT));

  if (!rows.length) {{
    document.getElementById("forecastTable").innerHTML =
      '<div class="empty">NO FORECAST DATA AVAILABLE</div>';
    return;
  }}

  let html = "<table><thead><tr><th>PORT / PERIOD</th><th>VLSFO</th><th>MGO</th><th>LSMGO</th><th>HSFO</th></tr></thead><tbody>";

  rows.forEach(row => {{
    html += "<tr>" +
      "<td>" + row.PORT + " / " + row.Period + "</td>" +
      "<td>" + formatNumber(row.VLSFO,0) + "</td>" +
      "<td>" + formatNumber(row.MGO,0) + "</td>" +
      "<td>" + formatNumber(row.LSMGO,0) + "</td>" +
      "<td>" + formatNumber(row.HSFO,0) + "</td>" +
      "</tr>";
  }});

  html += "</tbody></table>";
  document.getElementById("forecastTable").innerHTML = html;
}}

function render() {{
  const rows = filteredData();
  updateKpis(rows);
  renderChart(rows);
  renderForecast();
}}

function downloadCSV() {{
  const rows = filteredData();

  if (!rows.length) {{
    alert("다운로드할 데이터가 없습니다.");
    return;
  }}

  const content = [
    "DATE,PORT,PRICE",
    ...rows.map(row => row.DATE + "," + row.PORT + "," + row.PRICE)
  ].join("\\n");

  const blob = new Blob(["\\uFEFF" + content], {{
    type: "text/csv;charset=utf-8;"
  }});

  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");

  anchor.href = url;
  anchor.download = "shipandbunker_vlsfo_filtered.csv";

  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();

  URL.revokeObjectURL(url);
}}

document.addEventListener("DOMContentLoaded", render);
</script>
</body>
</html>
"""

    atomic_write_text(
        BASE_DIR / "index.html",
        html,
    )

    log("[HTML] Product dashboard generated: index.html")

def generate_oil_static_html(oil_long_df, fetched_at):
    """
    Generate OilPrice/index.html.

    일반 문자열 템플릿을 사용하여 CSS / JavaScript 중괄호가
    Python f-string 문법으로 해석되는 문제를 방지한다.
    """
    oil_records = dataframe_to_records(
        oil_long_df,
        "date",
        rename_map={
            "date": "DATE",
            "oil_type": "OIL_TYPE",
            "price": "PRICE",
        },
    )

    oil_json = json.dumps(
        oil_records,
        ensure_ascii=False,
    )

    html_template = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MI Terminal | Crude Oil Price</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>__TERMINAL_CSS__</style>
</head>

<body>
<header class="topbar">
  <div class="brand">
    <span class="brand-mark">MI&lt;GO&gt;</span>
    <span class="brand-title">CRUDE OIL MONITOR</span>
  </div>
  <a class="nav-link" href="../index.html">← BUNKER PRICE</a>
</header>

<main class="terminal">
  <section class="status">
    <span>SYSTEM / OPINET DATA ONLINE</span>
    <span>LAST REFRESH: __FETCHED_AT__</span>
  </section>

  <div class="workspace">
    <aside class="panel">
      <div class="panel-header">
        <span class="panel-title">FUNCTIONS / FILTERS</span>
      </div>

      <section class="filter-section">
        <span class="label">TIME RANGE</span>
        <div class="button-grid">
          <button class="btn" data-period="1Y" onclick="setPreset('1Y')">1Y</button>
          <button class="btn" data-period="3Y" onclick="setPreset('3Y')">3Y</button>
          <button class="btn" data-period="5Y" onclick="setPreset('5Y')">5Y</button>
          <button class="btn" data-period="YTD" onclick="setPreset('YTD')">YTD</button>
          <button class="btn all-button active" data-period="ALL" onclick="setPreset('ALL')">ALL HISTORY</button>
        </div>
      </section>

      <section class="filter-section">
        <span class="label">SELECT SERIES</span>
        <div class="series-list">
          <label class="series-item">
            <input type="checkbox" value="Dubai" checked onchange="render()"> Dubai
          </label>
          <label class="series-item">
            <input type="checkbox" value="Brent" checked onchange="render()"> Brent
          </label>
          <label class="series-item">
            <input type="checkbox" value="WTI" checked onchange="render()"> WTI
          </label>
        </div>
      </section>

      <section class="filter-section">
        <button
          class="btn"
          style="width:100%;border-color:#34526e;color:#39a8ff;"
          onclick="downloadCSV()"
        >
          EXPORT FILTERED CSV
        </button>
      </section>
    </aside>

    <section>
      <div class="heading">
        <div>
          <h1>INTERNATIONAL CRUDE OIL PRICE</h1>
          <div class="subtitle">
            Dubai · Brent · WTI / USD per Barrel
          </div>
        </div>
      </div>

      <section class="kpi-grid">
        <div class="kpi">
          <span class="kpi-label">LATEST<br>SELECTED AVG</span>
          <span class="kpi-value" id="latestValue">-</span>
          <span class="kpi-sub" id="latestDate">-</span>
        </div>

        <div class="kpi">
          <span class="kpi-label">PERIOD<br>AVERAGE</span>
          <span class="kpi-value" id="averageValue">-</span>
          <span class="kpi-sub">USD / BBL</span>
        </div>

        <div class="kpi">
          <span class="kpi-label">PERIOD<br>HIGH / LOW</span>
          <span class="kpi-value" id="highLow">-</span>
          <span class="kpi-sub">HIGH / LOW</span>
        </div>

        <div class="kpi">
          <span class="kpi-label">CHANGE<br>FIRST → LAST</span>
          <span class="kpi-value" id="changeValue">-</span>
          <span class="kpi-sub" id="changeRate">-</span>
        </div>

        <div class="kpi">
          <span class="kpi-label">OBSERVATIONS<br>/ SERIES</span>
          <span class="kpi-value" id="recordCount">-</span>
          <span class="kpi-sub" id="seriesCount">-</span>
        </div>
      </section>

      <section class="panel">
        <div class="panel-header">
          <span class="panel-title">
            CRUDE OIL PRICE HISTORY / RANGE SLIDER ENABLED
          </span>
          <span class="panel-title">USD PER BARREL</span>
        </div>
        <div id="chart"></div>
      </section>
    </section>
  </div>
</main>

<script>
const OIL_DATA = __OIL_JSON__;

const COLORS = {
  Dubai: "#ff8a00",
  Brent: "#39a8ff",
  WTI: "#2bd682"
};

let currentRange = null;
let currentPreset = "ALL";

function selectedTypes() {
  return Array.from(
    document.querySelectorAll(".series-list input:checked")
  ).map(element => element.value);
}

function formatNumber(value, decimals = 2) {
  if (
    value === null ||
    value === undefined ||
    Number.isNaN(Number(value))
  ) {
    return "-";
  }

  return Number(value).toLocaleString(
    "en-US",
    {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals
    }
  );
}

function bounds() {
  const data = [...OIL_DATA].sort(
    (a, b) => a.DATE.localeCompare(b.DATE)
  );

  return {
    min: data[0]?.DATE || null,
    max: data[data.length - 1]?.DATE || null
  };
}

function setPreset(preset) {
  const rangeBounds = bounds();

  if (!rangeBounds.max) {
    return;
  }

  currentPreset = preset;

  if (preset === "ALL") {
    currentRange = null;
  } else if (preset === "YTD") {
    currentRange = {
      start: rangeBounds.max.slice(0, 4) + "-01-01",
      end: rangeBounds.max
    };
  } else {
    const date = new Date(rangeBounds.max + "T00:00:00");

    date.setFullYear(
      date.getFullYear() - Number(preset.replace("Y", ""))
    );

    currentRange = {
      start: date.toISOString().slice(0, 10),
      end: rangeBounds.max
    };
  }

  document.querySelectorAll("[data-period]").forEach(button => {
    button.classList.toggle(
      "active",
      button.dataset.period === preset
    );
  });

  render();
}

function filteredData() {
  const selected = selectedTypes();

  return OIL_DATA.filter(row => {
    if (!selected.includes(row.OIL_TYPE)) {
      return false;
    }

    if (!currentRange) {
      return true;
    }

    return (
      row.DATE >= currentRange.start &&
      row.DATE <= currentRange.end
    );
  });
}

function dailyAverage(rows) {
  const grouped = {};

  rows.forEach(row => {
    if (!grouped[row.DATE]) {
      grouped[row.DATE] = [];
    }

    grouped[row.DATE].push(Number(row.PRICE));
  });

  return Object.keys(grouped)
    .sort()
    .map(date => ({
      date: date,
      value: grouped[date].reduce(
        (sum, value) => sum + value,
        0
      ) / grouped[date].length
    }));
}

function updateKpis(rows) {
  document.getElementById("recordCount").textContent =
    rows.length.toLocaleString("en-US");

  document.getElementById("seriesCount").textContent =
    selectedTypes().length + " SERIES SELECTED";

  if (!rows.length) {
    [
      "latestValue",
      "averageValue",
      "highLow",
      "changeValue"
    ].forEach(id => {
      document.getElementById(id).textContent = "-";
    });

    document.getElementById("latestDate").textContent = "NO DATA";
    document.getElementById("changeRate").textContent = "-";

    return;
  }

  const averages = dailyAverage(rows);
  const first = averages[0];
  const latest = averages[averages.length - 1];

  const values = rows.map(row => Number(row.PRICE));
  const average = values.reduce(
    (sum, value) => sum + value,
    0
  ) / values.length;

  const high = Math.max(...values);
  const low = Math.min(...values);

  const change = latest.value - first.value;
  const rate = first.value
    ? (change / first.value) * 100
    : 0;

  document.getElementById("latestValue").textContent =
    formatNumber(latest.value);

  document.getElementById("latestDate").textContent =
    latest.date + " AVG";

  document.getElementById("averageValue").textContent =
    formatNumber(average);

  document.getElementById("highLow").textContent =
    formatNumber(high, 0) + " / " + formatNumber(low, 0);

  const changeElement = document.getElementById("changeValue");

  changeElement.textContent =
    (change > 0 ? "+" : "") + formatNumber(change);

  changeElement.className =
    "kpi-value " +
    (
      change > 0
        ? "positive"
        : change < 0
          ? "negative"
          : ""
    );

  document.getElementById("changeRate").textContent =
    (rate > 0 ? "+" : "") +
    formatNumber(rate, 2) +
    "% / " +
    first.date +
    " → " +
    latest.date;
}

function renderChart(rows) {
  const chart = document.getElementById("chart");

  if (!rows.length) {
    Plotly.purge(chart);
    chart.innerHTML =
      '<div class="empty">NO DATA FOR SELECTED CONDITIONS</div>';
    return;
  }

  const groups = {};

  rows.forEach(row => {
    if (!groups[row.OIL_TYPE]) {
      groups[row.OIL_TYPE] = [];
    }

    groups[row.OIL_TYPE].push(row);
  });

  const traces = [];

  Object.entries(groups).forEach(([oilType, data]) => {
    data.sort((a, b) => a.DATE.localeCompare(b.DATE));

    traces.push({
      x: data.map(row => row.DATE),
      y: data.map(row => Number(row.PRICE)),
      type: "scatter",
      mode: "lines",
      name: oilType,
      line: {
        color: COLORS[oilType] || "#dce9f5",
        width: 2.2
      },
      hovertemplate:
        "<b>" + oilType + "</b>" +
        "<br>DATE: %{x}" +
        "<br>PRICE: $%{y:,.2f}/bbl" +
        "<extra></extra>"
    });
  });

  const rangeBounds = bounds();

  const chartRange = currentRange
    ? [currentRange.start, currentRange.end]
    : [rangeBounds.min, rangeBounds.max];

  const layout = {
    margin: {
      l: 72,
      r: 26,
      t: 22,
      b: 68
    },
    paper_bgcolor: "#07111c",
    plot_bgcolor: "#07111c",
    font: {
      family: '"Roboto Mono", Consolas, monospace',
      color: "#c9d6e2",
      size: 11
    },
    hovermode: "x unified",
    legend: {
      orientation: "h",
      x: 0,
      y: 1.12
    },
    xaxis: {
      type: "date",
      range: chartRange,
      gridcolor: "#1f3040",
      linecolor: "#405568",
      rangeslider: {
        visible: true,
        bgcolor: "#050c14",
        bordercolor: "#405568",
        thickness: 0.09
      }
    },
    yaxis: {
      title: "USD / BBL",
      gridcolor: "#1f3040",
      linecolor: "#405568"
    }
  };

  Plotly.newPlot(
    chart,
    traces,
    layout,
    {
      responsive: true,
      displaylogo: false,
      modeBarButtonsToRemove: [
        "select2d",
        "lasso2d"
      ]
    }
  );

  chart.on("plotly_relayout", event => {
    const start = event["xaxis.range[0]"];
    const end = event["xaxis.range[1]"];

    if (!start || !end) {
      return;
    }

    currentRange = {
      start: String(start).slice(0, 10),
      end: String(end).slice(0, 10)
    };

    currentPreset = "CUSTOM";

    document.querySelectorAll("[data-period]").forEach(button => {
      button.classList.remove("active");
    });

    render();
  });
}

function render() {
  const rows = filteredData();

  updateKpis(rows);
  renderChart(rows);
}

function downloadCSV() {
  const rows = filteredData();

  if (!rows.length) {
    alert("다운로드할 데이터가 없습니다.");
    return;
  }

  const content = [
    "DATE,OIL_TYPE,PRICE",
    ...rows.map(
      row =>
        row.DATE +
        "," +
        row.OIL_TYPE +
        "," +
        row.PRICE
    )
  ].join("\\n");

  const blob = new Blob(
    ["\\uFEFF" + content],
    {
      type: "text/csv;charset=utf-8;"
    }
  );

  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");

  anchor.href = url;
  anchor.download = "opinet_oil_prices_filtered.csv";

  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();

  URL.revokeObjectURL(url);
}

document.addEventListener("DOMContentLoaded", render);
</script>
</body>
</html>
"""

    html = (
        html_template
        .replace("__TERMINAL_CSS__", TERMINAL_CSS)
        .replace("__FETCHED_AT__", str(fetched_at))
        .replace("__OIL_JSON__", oil_json)
    )

    atomic_write_text(
        OIL_HTML_PATH,
        html,
    )
# =============================================================================
# 8. Bloomberg Terminal Static HTML
# =============================================================================

TERMINAL_CSS = """
:root {
  --bg: #02060b;
  --bg2: #040a11;
  --panel: #07111c;
  --panel2: #0a1724;
  --panel3: #0d1d2b;
  --line: #1b3043;
  --line2: #29445c;
  --text: #f3f7fb;
  --muted: #879caf;
  --dim: #536c80;
  --orange: #f6a623;
  --amber: #ffb000;
  --blue: #45a9f5;
  --green: #36ca82;
  --red: #ff6262;
  --purple: #bb86fc;
  --shadow: 0 0 18px rgba(34, 115, 173, .08);
}

* {
  box-sizing: border-box;
}

html,
body {
  min-height: 100%;
}

body {
  margin: 0;
  background:
    radial-gradient(circle at 50% -20%, rgba(34, 106, 154, .12), transparent 42%),
    linear-gradient(180deg, #03070c 0%, #02060b 100%);
  color: var(--text);
  font-family: "Roboto Mono", "SFMono-Regular", Consolas, "Noto Sans KR", monospace;
  font-size: 11px;
  letter-spacing: -.05px;
}

button,
input {
  font: inherit;
}

button {
  outline: none;
}

a {
  color: inherit;
  text-decoration: none;
}

/* ==========================================================================
   1. TOP TICKER BAR
   ========================================================================== */

.topbar {
  height: 43px;
  padding: 0 16px;
  display: flex;
  align-items: center;
  gap: 18px;
  border-bottom: 1px solid var(--line2);
  background:
    linear-gradient(180deg, rgba(12, 27, 41, .98) 0%, rgba(3, 9, 15, .98) 100%);
  box-shadow: 0 3px 14px rgba(0, 0, 0, .42);
}

.brand {
  min-width: 230px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding-right: 17px;
  border-right: 1px solid var(--line);
}

.brand-mark {
  color: #05070a;
  background: var(--orange);
  padding: 3px 5px;
  font-size: 11px;
  line-height: 1;
  font-weight: 900;
  letter-spacing: -.7px;
}

.brand-title {
  color: #fff;
  font-size: 11px;
  letter-spacing: .9px;
  font-weight: 800;
  white-space: nowrap;
}

.ticker-stream {
  display: flex;
  flex: 1;
  align-items: center;
  gap: 0;
  min-width: 0;
  overflow: hidden;
}

.ticker-item {
  min-width: 164px;
  height: 27px;
  padding: 0 15px;
  display: grid;
  grid-template-columns: 1fr auto;
  grid-template-rows: 12px 12px;
  column-gap: 9px;
  border-right: 1px solid var(--line);
}

.ticker-symbol {
  grid-column: 1;
  color: var(--muted);
  font-size: 9px;
  font-weight: 800;
  letter-spacing: .7px;
}

.ticker-value {
  grid-column: 2;
  grid-row: 1 / 3;
  align-self: center;
  color: #fff;
  font-size: 12px;
  font-weight: 700;
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.ticker-change {
  color: var(--green);
  font-size: 9px;
  font-weight: 700;
}

.ticker-change.negative {
  color: var(--red);
}

.nav-link {
  min-width: 142px;
  padding: 6px 9px;
  border: 1px solid #2d506e;
  color: var(--blue);
  background: rgba(25, 71, 105, .13);
  text-align: center;
  font-size: 9px;
  font-weight: 800;
  letter-spacing: .65px;
  transition: .15s ease;
}

.nav-link:hover {
  color: #fff;
  border-color: var(--blue);
  background: rgba(57, 168, 255, .17);
}

/* ==========================================================================
   MAIN 12-COLUMN TERMINAL GRID
   ========================================================================== */

.terminal {
  max-width: 1920px;
  margin: 0 auto;
  padding: 10px 14px 12px;
}

.status {
  min-height: 29px;
  padding: 0 10px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  border: 1px solid var(--line);
  border-left: 2px solid var(--green);
  background: linear-gradient(90deg, #07131d 0%, #05101a 100%);
  color: var(--muted);
  font-size: 9px;
  letter-spacing: .4px;
}

.status strong {
  color: var(--text);
  font-weight: 700;
}

.workspace {
  margin-top: 9px;
  display: grid;
  grid-template-columns: repeat(12, minmax(0, 1fr));
  gap: 9px;
}

/* ==========================================================================
   2. KPI ROW
   ========================================================================== */

.kpi-grid {
  grid-column: 1 / -1;
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 9px;
}

.kpi {
  min-height: 84px;
  padding: 9px 10px;
  border: 1px solid var(--line);
  border-top-color: #2c4b64;
  background:
    linear-gradient(145deg, rgba(14, 34, 50, .95), rgba(5, 14, 23, .97));
  box-shadow: var(--shadow);
}

.kpi-label {
  display: block;
  min-height: 24px;
  color: var(--muted);
  font-size: 8.5px;
  font-weight: 800;
  line-height: 1.35;
  letter-spacing: .65px;
  text-transform: uppercase;
}

.kpi-value {
  display: block;
  margin-top: 7px;
  color: #fff;
  font-size: 17px;
  font-weight: 800;
  line-height: 1;
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.kpi-sub {
  display: block;
  margin-top: 7px;
  padding-top: 4px;
  border-top: 1px dotted #254157;
  color: var(--muted);
  font-size: 8.5px;
  text-align: right;
}

.positive {
  color: var(--green) !important;
}

.negative {
  color: var(--red) !important;
}

/* ==========================================================================
   PANEL SYSTEM
   ========================================================================== */

.panel {
  border: 1px solid var(--line);
  background: var(--panel);
  box-shadow: var(--shadow);
  overflow: hidden;
}

.panel-header {
  height: 31px;
  padding: 0 10px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  border-bottom: 1px solid var(--line);
  background:
    linear-gradient(180deg, #0c1c2a 0%, #07111c 100%);
}

.panel-title {
  color: var(--orange);
  font-size: 9px;
  font-weight: 800;
  letter-spacing: .75px;
  text-transform: uppercase;
}

.panel-meta {
  color: var(--muted);
  font-size: 8.5px;
  text-align: right;
}

/* ==========================================================================
   FILTER PANEL
   ========================================================================== */

.filter-panel {
  grid-column: span 2;
  align-self: start;
}

.filter-section {
  padding: 10px;
  border-bottom: 1px solid #15293a;
}

.filter-section:last-child {
  border-bottom: none;
}

.label {
  display: block;
  margin-bottom: 7px;
  color: var(--muted);
  font-size: 8.5px;
  font-weight: 800;
  letter-spacing: .7px;
}

.button-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 4px;
}

.all-button {
  grid-column: span 2;
}

.btn {
  min-height: 27px;
  padding: 5px 6px;
  border: 1px solid #263e52;
  background: #06101a;
  color: var(--muted);
  cursor: pointer;
  font-size: 9px;
  font-weight: 800;
  letter-spacing: .3px;
  transition: .15s ease;
}

.btn:hover {
  border-color: var(--orange);
  color: var(--orange);
}

.btn.active {
  border-color: var(--orange);
  background: var(--orange);
  color: #080a0d;
}

.series-list {
  display: grid;
  gap: 4px;
}

.series-item {
  min-height: 29px;
  padding: 0 7px;
  display: flex;
  align-items: center;
  gap: 7px;
  border: 1px solid #203749;
  background: #06101a;
  color: #dbe8f4;
  cursor: pointer;
  font-size: 9px;
}

.series-item:hover {
  border-color: #3b637f;
  background: #091827;
}

.series-item input {
  accent-color: var(--orange);
}

/* ==========================================================================
   3. MAIN CHART PANEL
   ========================================================================== */

.main-chart-panel {
  grid-column: span 7;
  min-width: 0;
}

.chart-heading {
  padding: 9px 10px 8px;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  border-bottom: 1px solid #15293a;
  background: linear-gradient(90deg, rgba(12, 33, 48, .48), transparent);
}

.chart-heading h1 {
  margin: 0;
  color: #fff;
  font-size: 15px;
  font-weight: 800;
  letter-spacing: .15px;
}

.subtitle {
  margin-top: 4px;
  color: var(--muted);
  font-size: 8.5px;
  letter-spacing: .3px;
}

.chart-stat {
  color: var(--blue);
  font-size: 9px;
  font-weight: 700;
  text-align: right;
}

#chart {
  width: 100%;
  min-height: 407px;
}

/* ==========================================================================
   4. WATCHLIST PANEL
   ========================================================================== */

.watchlist-panel {
  grid-column: span 3;
  min-width: 0;
}

.watchlist-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 9px;
}

.watchlist-table th,
.watchlist-table td {
  padding: 7px 8px;
  border-bottom: 1px solid #142939;
  font-variant-numeric: tabular-nums;
}

.watchlist-table th {
  color: var(--muted);
  background: #07131e;
  font-size: 8px;
  font-weight: 800;
  letter-spacing: .4px;
  text-align: right;
}

.watchlist-table td {
  color: #dfeaf3;
  text-align: right;
}

.watchlist-table th:first-child,
.watchlist-table td:first-child {
  text-align: left;
}

.watchlist-table tr:hover td {
  background: #0b1b2a;
}

.watch-symbol {
  color: #fff !important;
  font-weight: 700;
}

.watch-sub {
  display: block;
  margin-top: 2px;
  color: var(--dim);
  font-size: 7.5px;
}

.watch-positive {
  color: var(--green) !important;
}

.watch-negative {
  color: var(--red) !important;
}

/* ==========================================================================
   5. BOTTOM-LEFT HEATMAP
   ========================================================================== */

.heatmap-panel {
  grid-column: span 4;
}

.heatmap-grid {
  padding: 8px;
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 5px;
}

.heat-cell {
  min-height: 62px;
  padding: 7px;
  border: 1px solid #1c3547;
  background: #081520;
}

.heat-cell-name {
  color: var(--muted);
  font-size: 8px;
  font-weight: 800;
  letter-spacing: .35px;
}

.heat-cell-value {
  margin-top: 9px;
  color: #fff;
  font-size: 12px;
  font-weight: 800;
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.heat-cell-change {
  margin-top: 4px;
  font-size: 8px;
  font-weight: 700;
  text-align: right;
}

.heat-up {
  border-left: 2px solid var(--green);
  background: linear-gradient(135deg, rgba(31, 169, 100, .18), rgba(5, 20, 28, .92));
}

.heat-down {
  border-left: 2px solid var(--red);
  background: linear-gradient(135deg, rgba(180, 53, 53, .18), rgba(5, 20, 28, .92));
}

.heat-neutral {
  border-left: 2px solid var(--blue);
}

/* ==========================================================================
   6. NEWS / MOVERS / RISK
   ========================================================================== */

.news-panel {
  grid-column: span 4;
}

.news-list {
  padding: 5px 9px;
}

.news-item {
  padding: 7px 0;
  display: grid;
  grid-template-columns: 38px 1fr;
  gap: 7px;
  border-bottom: 1px dotted #243c4d;
}

.news-item:last-child {
  border-bottom: none;
}

.news-time {
  color: var(--orange);
  font-size: 8px;
  font-weight: 700;
}

.news-text {
  color: #d7e2eb;
  font-size: 9px;
  line-height: 1.45;
}

.news-text strong {
  color: #fff;
}

.risk-panel {
  grid-column: span 4;
}

.risk-content {
  padding: 9px;
}

.risk-row {
  margin-bottom: 11px;
}

.risk-row:last-child {
  margin-bottom: 0;
}

.risk-label-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 5px;
  color: var(--muted);
  font-size: 8.5px;
  font-weight: 700;
}

.risk-label-row strong {
  color: #fff;
  font-size: 9px;
}

.risk-track {
  height: 6px;
  overflow: hidden;
  border: 1px solid #213a4d;
  background: #040b12;
}

.risk-fill {
  height: 100%;
  background: linear-gradient(90deg, var(--blue), #66c5ff);
}

.risk-fill.warning {
  background: linear-gradient(90deg, #c58114, var(--amber));
}

.risk-fill.alert {
  background: linear-gradient(90deg, #bd4242, var(--red));
}

/* ==========================================================================
   7. FOOTER STATUS BAR
   ========================================================================== */

.footer-status {
  grid-column: 1 / -1;
  min-height: 30px;
  padding: 0 10px;
  display: grid;
  grid-template-columns: 1.2fr 1fr 1fr 1.2fr;
  align-items: center;
  border: 1px solid var(--line);
  background: #050d15;
  color: var(--muted);
  font-size: 8.5px;
}

.footer-item {
  padding: 0 9px;
  border-right: 1px solid var(--line);
}

.footer-item:last-child {
  border-right: none;
  text-align: right;
}

.footer-item strong {
  color: #fff;
  font-weight: 700;
}

.footer-green {
  color: var(--green) !important;
}

.footer-amber {
  color: var(--amber) !important;
}

/* ==========================================================================
   RESPONSIVE
   ========================================================================== */

@media (max-width: 1250px) {
  .filter-panel { grid-column: span 3; }
  .main-chart-panel { grid-column: span 6; }
  .watchlist-panel { grid-column: span 3; }
  .kpi-grid { grid-template-columns: repeat(3, 1fr); }
}

@media (max-width: 960px) {
  .topbar {
    height: auto;
    min-height: 43px;
    flex-wrap: wrap;
    padding: 7px 10px;
  }

  .brand {
    min-width: auto;
    border-right: none;
  }

  .ticker-stream {
    order: 3;
    flex-basis: 100%;
  }

  .workspace {
    grid-template-columns: repeat(1, minmax(0, 1fr));
  }

  .filter-panel,
  .main-chart-panel,
  .watchlist-panel,
  .heatmap-panel,
  .news-panel,
  .risk-panel {
    grid-column: 1 / -1;
  }
}

@media (max-width: 620px) {
  .terminal {
    padding: 8px;
  }

  .kpi-grid {
    grid-template-columns: repeat(2, 1fr);
  }

  .footer-status {
    grid-template-columns: 1fr;
    gap: 5px;
    padding: 7px;
  }

  .footer-item {
    padding: 2px 0;
    border-right: none;
    border-bottom: 1px solid var(--line);
  }

  .footer-item:last-child {
    border-bottom: none;
    text-align: left;
  }

  .heatmap-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}
"""

def generate_product_static_html(product_df, forecast_df, fetched_at):
    product_records = dataframe_to_records(
        product_df,
        "date",
        rename_map={
            "date": "DATE",
            "port": "PORT",
            "price": "PRICE",
        },
    )

    forecast_records = dataframe_to_records(
        forecast_df,
        "graph_date",
        rename_map={
            "graph_date": "GRAPH_DATE",
            "port": "PORT",
        },
    )

    product_json = json.dumps(product_records, ensure_ascii=False)
    forecast_json = json.dumps(forecast_records, ensure_ascii=False)

    html_template = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MI Terminal | Bunker Price</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>__TERMINAL_CSS__</style>
</head>

<body>
<header class="topbar">
  <div class="brand">
    <span class="brand-mark">MI&lt;GO&gt;</span>
    <span class="brand-title">ENERGY MARKET TERMINAL</span>
  </div>

  <div class="ticker-stream" id="tickerStream"></div>

  <a class="nav-link" href="./OilPrice/index.html">CRUDE OIL MONITOR →</a>
</header>

<main class="terminal">
  <section class="status">
    <span>SYSTEM STATUS / <strong>SHIP &amp; BUNKER MARKET DATA ONLINE</strong></span>
    <span>LAST REFRESH / <strong>__FETCHED_AT__</strong></span>
  </section>

  <section class="workspace">

    <section class="kpi-grid">
      <div class="kpi">
        <span class="kpi-label">SELECTED MARKET<br>LATEST AVG</span>
        <span class="kpi-value" id="latestValue">-</span>
        <span class="kpi-sub" id="latestDate">-</span>
      </div>

      <div class="kpi">
        <span class="kpi-label">PERIOD<br>AVERAGE</span>
        <span class="kpi-value" id="averageValue">-</span>
        <span class="kpi-sub">USD / MT</span>
      </div>

      <div class="kpi">
        <span class="kpi-label">PERIOD<br>HIGH / LOW</span>
        <span class="kpi-value" id="highLow">-</span>
        <span class="kpi-sub">RANGE ANALYSIS</span>
      </div>

      <div class="kpi">
        <span class="kpi-label">PERIOD<br>CHANGE</span>
        <span class="kpi-value" id="changeValue">-</span>
        <span class="kpi-sub" id="changeRate">-</span>
      </div>

      <div class="kpi">
        <span class="kpi-label">OBSERVATIONS<br>/ MARKETS</span>
        <span class="kpi-value" id="recordCount">-</span>
        <span class="kpi-sub" id="portCount">-</span>
      </div>

      <div class="kpi">
        <span class="kpi-label">MARKET<br>VOLATILITY</span>
        <span class="kpi-value" id="volatilityValue">-</span>
        <span class="kpi-sub">STD DEV / USD MT</span>
      </div>
    </section>

    <aside class="panel filter-panel">
      <div class="panel-header">
        <span class="panel-title">FUNCTIONS / FILTERS</span>
      </div>

      <section class="filter-section">
        <span class="label">TIME RANGE</span>
        <div class="button-grid">
          <button class="btn" data-period="1Y" onclick="setPreset('1Y')">1Y</button>
          <button class="btn" data-period="3Y" onclick="setPreset('3Y')">3Y</button>
          <button class="btn" data-period="5Y" onclick="setPreset('5Y')">5Y</button>
          <button class="btn" data-period="YTD" onclick="setPreset('YTD')">YTD</button>
          <button class="btn all-button active" data-period="ALL" onclick="setPreset('ALL')">ALL HISTORY</button>
        </div>
      </section>

      <section class="filter-section">
        <span class="label">SELECT BUNKER MARKETS</span>
        <div class="series-list">
          <label class="series-item"><input type="checkbox" value="Rotterdam" checked onchange="render()"> ROTTERDAM</label>
          <label class="series-item"><input type="checkbox" value="Fujairah" checked onchange="render()"> FUJAIRAH</label>
          <label class="series-item"><input type="checkbox" value="Singapore" checked onchange="render()"> SINGAPORE</label>
        </div>
      </section>

      <section class="filter-section">
        <span class="label">MOVING AVERAGE</span>
        <div class="button-grid">
          <button class="btn active" data-ma="OFF" onclick="setMA('OFF')">OFF</button>
          <button class="btn" data-ma="90" onclick="setMA('90')">90D</button>
          <button class="btn" data-ma="180" onclick="setMA('180')">180D</button>
          <button class="btn" data-ma="365" onclick="setMA('365')">365D</button>
        </div>
      </section>

      <section class="filter-section">
        <button class="btn" style="width:100%;border-color:#2f6287;color:#45a9f5;" onclick="downloadCSV()">
          EXPORT FILTERED CSV
        </button>
      </section>
    </aside>

    <section class="panel main-chart-panel">
      <div class="chart-heading">
        <div>
          <h1>VLSFO BUNKER PRICE HISTORY</h1>
          <div class="subtitle">ROTTERDAM · FUJAIRAH · SINGAPORE / USD PER METRIC TON</div>
        </div>
        <div class="chart-stat">LINE CHART / RANGE SLIDER ENABLED</div>
      </div>
      <div id="chart"></div>
    </section>

    <section class="panel watchlist-panel">
      <div class="panel-header">
        <span class="panel-title">ENERGY WATCHLIST</span>
        <span class="panel-meta">LAST / Δ / VOL</span>
      </div>
      <div id="watchlist"></div>
    </section>

    <section class="panel heatmap-panel">
      <div class="panel-header">
        <span class="panel-title">MARKET HEATMAP</span>
        <span class="panel-meta">LATEST DAILY MOVE</span>
      </div>
      <div class="heatmap-grid" id="heatmap"></div>
    </section>

    <section class="panel news-panel">
      <div class="panel-header">
        <span class="panel-title">MARKET INTELLIGENCE / MOVERS</span>
        <span class="panel-meta">DERIVED SIGNALS</span>
      </div>
      <div class="news-list" id="newsFeed"></div>
    </section>

    <section class="panel risk-panel">
      <div class="panel-header">
        <span class="panel-title">RISK INDICATORS</span>
        <span class="panel-meta">MARKET CONDITIONS</span>
      </div>
      <div class="risk-content" id="riskPanel"></div>
    </section>

    <footer class="footer-status">
      <div class="footer-item">MARKET HEALTH / <strong class="footer-green">NORMAL</strong></div>
      <div class="footer-item">SENTIMENT / <strong id="footerSentiment">NEUTRAL</strong></div>
      <div class="footer-item">ALERTS / <strong class="footer-amber" id="footerAlert">MONITORING ACTIVE</strong></div>
      <div class="footer-item">SOURCE / <strong>SHIP &amp; BUNKER</strong></div>
    </footer>
  </section>
</main>

<script>
const PRODUCT_DATA = __PRODUCT_JSON__;
const FORECAST_DATA = __FORECAST_JSON__;

const COLORS = {
  Rotterdam: "#45a9f5",
  Fujairah: "#36ca82",
  Singapore: "#bb86fc"
};

let currentRange = null;
let currentPreset = "ALL";
let currentMA = "OFF";

function formatNumber(value, decimals = 1) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "-";
  }

  return Number(value).toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals
  });
}

function signed(value, decimals = 1) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "-";
  }

  return (Number(value) > 0 ? "+" : "") + formatNumber(value, decimals);
}

function selectedPorts() {
  return Array.from(document.querySelectorAll(".series-list input:checked"))
    .map(element => element.value);
}

function bounds() {
  const sorted = [...PRODUCT_DATA].sort((a, b) => a.DATE.localeCompare(b.DATE));

  return {
    min: sorted[0]?.DATE || null,
    max: sorted[sorted.length - 1]?.DATE || null
  };
}

function setPreset(preset) {
  const marketBounds = bounds();

  if (!marketBounds.max) {
    return;
  }

  currentPreset = preset;

  if (preset === "ALL") {
    currentRange = null;
  } else if (preset === "YTD") {
    currentRange = {
      start: marketBounds.max.slice(0, 4) + "-01-01",
      end: marketBounds.max
    };
  } else {
    const date = new Date(marketBounds.max + "T00:00:00");
    date.setFullYear(date.getFullYear() - Number(preset.replace("Y", "")));

    currentRange = {
      start: date.toISOString().slice(0, 10),
      end: marketBounds.max
    };
  }

  document.querySelectorAll("[data-period]").forEach(button => {
    button.classList.toggle("active", button.dataset.period === preset);
  });

  render();
}

function setMA(mode) {
  currentMA = mode;

  document.querySelectorAll("[data-ma]").forEach(button => {
    button.classList.toggle("active", button.dataset.ma === mode);
  });

  render();
}

function filteredData() {
  const ports = selectedPorts();

  return PRODUCT_DATA.filter(row => {
    if (!ports.includes(row.PORT)) {
      return false;
    }

    if (!currentRange) {
      return true;
    }

    return row.DATE >= currentRange.start && row.DATE <= currentRange.end;
  });
}

function getPortStats(port, rows = PRODUCT_DATA) {
  const portRows = rows
    .filter(row => row.PORT === port)
    .sort((a, b) => a.DATE.localeCompare(b.DATE));

  if (!portRows.length) {
    return null;
  }

  const latest = portRows[portRows.length - 1];
  const previous = portRows.length > 1 ? portRows[portRows.length - 2] : latest;
  const values = portRows.map(row => Number(row.PRICE));
  const change = Number(latest.PRICE) - Number(previous.PRICE);
  const changePct = previous && Number(previous.PRICE)
    ? change / Number(previous.PRICE) * 100
    : 0;

  return {
    port,
    latest: Number(latest.PRICE),
    previous: Number(previous.PRICE),
    date: latest.DATE,
    change,
    changePct,
    high: Math.max(...values),
    low: Math.min(...values),
    observations: portRows.length
  };
}

function dailyAverage(rows) {
  const grouped = {};

  rows.forEach(row => {
    if (!grouped[row.DATE]) {
      grouped[row.DATE] = [];
    }
    grouped[row.DATE].push(Number(row.PRICE));
  });

  return Object.keys(grouped).sort().map(date => ({
    date,
    value: grouped[date].reduce((sum, value) => sum + value, 0) / grouped[date].length
  }));
}

function standardDeviation(values) {
  if (!values.length) {
    return 0;
  }

  const mean = values.reduce((sum, value) => sum + value, 0) / values.length;
  const variance = values.reduce(
    (sum, value) => sum + Math.pow(value - mean, 2),
    0
  ) / values.length;

  return Math.sqrt(variance);
}

function updateKpis(rows) {
  document.getElementById("recordCount").textContent = rows.length.toLocaleString("en-US");
  document.getElementById("portCount").textContent = selectedPorts().length + " MARKETS SELECTED";

  if (!rows.length) {
    ["latestValue", "averageValue", "highLow", "changeValue", "volatilityValue"].forEach(id => {
      document.getElementById(id).textContent = "-";
    });

    document.getElementById("latestDate").textContent = "NO DATA";
    document.getElementById("changeRate").textContent = "-";
    return;
  }

  const averages = dailyAverage(rows);
  const first = averages[0];
  const latest = averages[averages.length - 1];
  const values = rows.map(row => Number(row.PRICE));
  const average = values.reduce((sum, value) => sum + value, 0) / values.length;
  const high = Math.max(...values);
  const low = Math.min(...values);
  const change = latest.value - first.value;
  const rate = first.value ? change / first.value * 100 : 0;
  const volatility = standardDeviation(values);

  document.getElementById("latestValue").textContent = formatNumber(latest.value);
  document.getElementById("latestDate").textContent = latest.date + " AVG";
  document.getElementById("averageValue").textContent = formatNumber(average);
  document.getElementById("highLow").textContent =
    formatNumber(high, 0) + " / " + formatNumber(low, 0);
  document.getElementById("volatilityValue").textContent = formatNumber(volatility, 1);

  const changeElement = document.getElementById("changeValue");
  changeElement.textContent = signed(change);
  changeElement.className =
    "kpi-value " + (change > 0 ? "positive" : change < 0 ? "negative" : "");

  document.getElementById("changeRate").textContent =
    signed(rate, 2) + "% / " + first.date + " → " + latest.date;

  document.getElementById("footerSentiment").textContent =
    change > 0 ? "BULLISH" : change < 0 ? "BEARISH" : "NEUTRAL";

  document.getElementById("footerSentiment").className =
    change > 0 ? "footer-green" : change < 0 ? "negative" : "";
}

function movingAverage(values, windowSize) {
  return values.map((item, index) => {
    const start = Math.max(0, index - windowSize + 1);
    const range = values.slice(start, index + 1);

    return range.reduce((sum, row) => sum + row.y, 0) / range.length;
  });
}

function renderChart(rows) {
  const chart = document.getElementById("chart");

  if (!rows.length) {
    Plotly.purge(chart);
    chart.innerHTML = '<div class="empty">NO DATA FOR SELECTED CONDITIONS</div>';
    return;
  }

  const groups = {};

  rows.forEach(row => {
    if (!groups[row.PORT]) {
      groups[row.PORT] = [];
    }
    groups[row.PORT].push(row);
  });

  const traces = [];

  Object.entries(groups).forEach(([port, data]) => {
    data.sort((a, b) => a.DATE.localeCompare(b.DATE));

    const series = data.map(row => ({
      x: row.DATE,
      y: Number(row.PRICE)
    }));

    traces.push({
      x: series.map(item => item.x),
      y: series.map(item => item.y),
      type: "scatter",
      mode: "lines",
      name: port.toUpperCase(),
      line: {
        color: COLORS[port],
        width: 2
      },
      hovertemplate:
        "<b>" + port.toUpperCase() + "</b>" +
        "<br>DATE: %{x}" +
        "<br>PRICE: $%{y:,.1f}/MT" +
        "<extra></extra>"
    });

    if (currentMA !== "OFF") {
      const ma = movingAverage(series, Number(currentMA));

      traces.push({
        x: series.map(item => item.x),
        y: ma,
        type: "scatter",
        mode: "lines",
        name: port.toUpperCase() + " " + currentMA + "D MA",
        line: {
          color: COLORS[port],
          width: 1,
          dash: "dot"
        },
        hoverinfo: "skip"
      });
    }
  });

  const marketBounds = bounds();
  const chartRange = currentRange
    ? [currentRange.start, currentRange.end]
    : [marketBounds.min, marketBounds.max];

  const layout = {
    margin: {
      l: 62,
      r: 19,
      t: 18,
      b: 57
    },
    paper_bgcolor: "#07111c",
    plot_bgcolor: "#07111c",
    font: {
      family: '"Roboto Mono", Consolas, monospace',
      color: "#cdd9e4",
      size: 10
    },
    hovermode: "x unified",
    hoverlabel: {
      bgcolor: "#07111c",
      bordercolor: "#385976",
      font: {
        color: "#ffffff",
        family: '"Roboto Mono", Consolas, monospace'
      }
    },
    legend: {
      orientation: "h",
      x: 0,
      y: 1.11,
      font: {
        size: 9,
        color: "#a9bdce"
      }
    },
    xaxis: {
      type: "date",
      range: chartRange,
      gridcolor: "#142939",
      linecolor: "#365268",
      zerolinecolor: "#142939",
      tickfont: {
        size: 9,
        color: "#8aa0b5"
      },
      rangeslider: {
        visible: true,
        bgcolor: "#040b12",
        bordercolor: "#29465d",
        thickness: .085
      }
    },
    yaxis: {
      title: {
        text: "USD / MT",
        font: {
          size: 9,
          color: "#8aa0b5"
        }
      },
      gridcolor: "#142939",
      linecolor: "#365268",
      tickfont: {
        size: 9,
        color: "#8aa0b5"
      }
    }
  };

  Plotly.newPlot(chart, traces, layout, {
    responsive: true,
    displaylogo: false,
    modeBarButtonsToRemove: ["select2d", "lasso2d"]
  });

  chart.on("plotly_relayout", event => {
    const start = event["xaxis.range[0]"];
    const end = event["xaxis.range[1]"];

    if (!start || !end) {
      return;
    }

    currentRange = {
      start: String(start).slice(0, 10),
      end: String(end).slice(0, 10)
    };

    currentPreset = "CUSTOM";

    document.querySelectorAll("[data-period]").forEach(button => {
      button.classList.remove("active");
    });

    render();
  });
}

function renderTicker() {
  const stats = ["Rotterdam", "Fujairah", "Singapore"]
    .map(port => getPortStats(port))
    .filter(Boolean);

  const html = stats.map(item => {
    const directionClass = item.change >= 0 ? "" : "negative";
    const arrow = item.change >= 0 ? "▲" : "▼";

    return `
      <div class="ticker-item">
        <span class="ticker-symbol">${item.port.toUpperCase()} VLSFO</span>
        <span class="ticker-value">${formatNumber(item.latest, 1)}</span>
        <span class="ticker-change ${directionClass}">
          ${arrow} ${signed(item.change, 1)} / ${signed(item.changePct, 2)}%
        </span>
      </div>
    `;
  }).join("");

  document.getElementById("tickerStream").innerHTML = html;
}

function renderWatchlist() {
  const stats = ["Rotterdam", "Fujairah", "Singapore"]
    .map(port => getPortStats(port))
    .filter(Boolean);

  let html = `
    <table class="watchlist-table">
      <thead>
        <tr>
          <th>MARKET</th>
          <th>LAST</th>
          <th>CHANGE</th>
          <th>OBS</th>
        </tr>
      </thead>
      <tbody>
  `;

  stats.forEach(item => {
    const className = item.change >= 0 ? "watch-positive" : "watch-negative";

    html += `
      <tr>
        <td class="watch-symbol">
          ${item.port.toUpperCase()}
          <span class="watch-sub">VLSFO / ${item.date}</span>
        </td>
        <td>${formatNumber(item.latest, 1)}</td>
        <td class="${className}">
          ${signed(item.change, 1)}
          <span class="watch-sub ${className}">${signed(item.changePct, 2)}%</span>
        </td>
        <td>${item.observations.toLocaleString("en-US")}</td>
      </tr>
    `;
  });

  html += "</tbody></table>";
  document.getElementById("watchlist").innerHTML = html;
}

function renderHeatmap() {
  const stats = ["Rotterdam", "Fujairah", "Singapore"]
    .map(port => getPortStats(port))
    .filter(Boolean);

  const heatmap = stats.map(item => {
    const state = item.change > 0 ? "heat-up" : item.change < 0 ? "heat-down" : "heat-neutral";
    const className = item.change >= 0 ? "positive" : "negative";

    return `
      <div class="heat-cell ${state}">
        <div class="heat-cell-name">${item.port.toUpperCase()}</div>
        <div class="heat-cell-value">${formatNumber(item.latest, 1)}</div>
        <div class="heat-cell-change ${className}">
          ${item.change >= 0 ? "▲" : "▼"} ${signed(item.change, 1)} / ${signed(item.changePct, 2)}%
        </div>
      </div>
    `;
  }).join("");

  document.getElementById("heatmap").innerHTML = heatmap;
}

function renderNewsAndRisk(rows) {
  const stats = ["Rotterdam", "Fujairah", "Singapore"]
    .map(port => getPortStats(port))
    .filter(Boolean)
    .sort((a, b) => Math.abs(b.changePct) - Math.abs(a.changePct));

  const leader = stats[0];
  const positiveCount = stats.filter(item => item.change > 0).length;
  const negativeCount = stats.filter(item => item.change < 0).length;
  const values = rows.map(row => Number(row.PRICE));
  const vol = standardDeviation(values);
  const range = values.length ? Math.max(...values) - Math.min(...values) : 0;

  const newsItems = [
    {
      time: "LIVE",
      text: leader
        ? `<strong>${leader.port.toUpperCase()}</strong> is the largest daily mover at ${signed(leader.changePct, 2)}%.`
        : "No market movement signal is currently available."
    },
    {
      time: "FLOW",
      text: `<strong>${positiveCount} ADVANCERS</strong> and <strong>${negativeCount} DECLINERS</strong> across monitored VLSFO hubs.`
    },
    {
      time: "RANGE",
      text: `Selected market price range is <strong>${formatNumber(range, 1)} USD/MT</strong> across the active period.`
    }
  ];

  document.getElementById("newsFeed").innerHTML = newsItems.map(item => `
    <div class="news-item">
      <div class="news-time">${item.time}</div>
      <div class="news-text">${item.text}</div>
    </div>
  `).join("");

  const directionScore = stats.length
    ? positiveCount / stats.length * 100
    : 0;

  const volScore = Math.min(vol / 2, 100);
  const rangeScore = Math.min(range / 8, 100);

  document.getElementById("riskPanel").innerHTML = `
    <div class="risk-row">
      <div class="risk-label-row">
        <span>BREADTH / ADVANCERS</span>
        <strong>${formatNumber(directionScore, 0)}%</strong>
      </div>
      <div class="risk-track">
        <div class="risk-fill" style="width:${directionScore}%;"></div>
      </div>
    </div>

    <div class="risk-row">
      <div class="risk-label-row">
        <span>PRICE VOLATILITY</span>
        <strong>${formatNumber(vol, 1)} USD/MT</strong>
      </div>
      <div class="risk-track">
        <div class="risk-fill warning" style="width:${volScore}%;"></div>
      </div>
    </div>

    <div class="risk-row">
      <div class="risk-label-row">
        <span>PERIOD RANGE EXPOSURE</span>
        <strong>${formatNumber(range, 1)} USD/MT</strong>
      </div>
      <div class="risk-track">
        <div class="risk-fill alert" style="width:${rangeScore}%;"></div>
      </div>
    </div>
  `;

  document.getElementById("footerAlert").textContent =
    range > 100 ? "ELEVATED RANGE WATCH" : "MONITORING ACTIVE";
}

function downloadCSV() {
  const rows = filteredData();

  if (!rows.length) {
    alert("다운로드할 데이터가 없습니다.");
    return;
  }

  const content = [
    "DATE,PORT,PRICE",
    ...rows.map(row => row.DATE + "," + row.PORT + "," + row.PRICE)
  ].join("\\n");

  const blob = new Blob(["\\uFEFF" + content], {
    type: "text/csv;charset=utf-8;"
  });

  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");

  anchor.href = url;
  anchor.download = "shipandbunker_vlsfo_filtered.csv";

  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();

  URL.revokeObjectURL(url);
}

function render() {
  const rows = filteredData();

  updateKpis(rows);
  renderChart(rows);
  renderWatchlist();
  renderHeatmap();
  renderNewsAndRisk(rows);
}

document.addEventListener("DOMContentLoaded", () => {
  renderTicker();
  render();
});
</script>
</body>
</html>
"""

    html = (
        html_template
        .replace("__TERMINAL_CSS__", TERMINAL_CSS)
        .replace("__FETCHED_AT__", str(fetched_at))
        .replace("__PRODUCT_JSON__", product_json)
        .replace("__FORECAST_JSON__", forecast_json)
    )

    atomic_write_text(BASE_DIR / "index.html", html)

    log("[HTML] Product dashboard generated: index.html")

def generate_oil_static_html(oil_long_df, fetched_at):
    oil_records = dataframe_to_records(
        oil_long_df,
        "date",
        rename_map={
            "date": "DATE",
            "oil_type": "OIL_TYPE",
            "price": "PRICE",
        },
    )

    oil_json = json.dumps(oil_records, ensure_ascii=False)

    html_template = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MI Terminal | Crude Oil Price</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>__TERMINAL_CSS__</style>
</head>

<body>
<header class="topbar">
  <div class="brand">
    <span class="brand-mark">MI&lt;GO&gt;</span>
    <span class="brand-title">ENERGY MARKET TERMINAL</span>
  </div>

  <div class="ticker-stream" id="tickerStream"></div>

  <a class="nav-link" href="../index.html">← BUNKER PRICE MONITOR</a>
</header>

<main class="terminal">
  <section class="status">
    <span>SYSTEM STATUS / <strong>OPINET CRUDE OIL DATA ONLINE</strong></span>
    <span>LAST REFRESH / <strong>__FETCHED_AT__</strong></span>
  </section>

  <section class="workspace">

    <section class="kpi-grid">
      <div class="kpi">
        <span class="kpi-label">SELECTED MARKET<br>LATEST AVG</span>
        <span class="kpi-value" id="latestValue">-</span>
        <span class="kpi-sub" id="latestDate">-</span>
      </div>

      <div class="kpi">
        <span class="kpi-label">PERIOD<br>AVERAGE</span>
        <span class="kpi-value" id="averageValue">-</span>
        <span class="kpi-sub">USD / BBL</span>
      </div>

      <div class="kpi">
        <span class="kpi-label">PERIOD<br>HIGH / LOW</span>
        <span class="kpi-value" id="highLow">-</span>
        <span class="kpi-sub">RANGE ANALYSIS</span>
      </div>

      <div class="kpi">
        <span class="kpi-label">PERIOD<br>CHANGE</span>
        <span class="kpi-value" id="changeValue">-</span>
        <span class="kpi-sub" id="changeRate">-</span>
      </div>

      <div class="kpi">
        <span class="kpi-label">OBSERVATIONS<br>/ SERIES</span>
        <span class="kpi-value" id="recordCount">-</span>
        <span class="kpi-sub" id="seriesCount">-</span>
      </div>

      <div class="kpi">
        <span class="kpi-label">MARKET<br>VOLATILITY</span>
        <span class="kpi-value" id="volatilityValue">-</span>
        <span class="kpi-sub">STD DEV / USD BBL</span>
      </div>
    </section>

    <aside class="panel filter-panel">
      <div class="panel-header">
        <span class="panel-title">FUNCTIONS / FILTERS</span>
      </div>

      <section class="filter-section">
        <span class="label">TIME RANGE</span>
        <div class="button-grid">
          <button class="btn" data-period="1Y" onclick="setPreset('1Y')">1Y</button>
          <button class="btn" data-period="3Y" onclick="setPreset('3Y')">3Y</button>
          <button class="btn" data-period="5Y" onclick="setPreset('5Y')">5Y</button>
          <button class="btn" data-period="YTD" onclick="setPreset('YTD')">YTD</button>
          <button class="btn all-button active" data-period="ALL" onclick="setPreset('ALL')">ALL HISTORY</button>
        </div>
      </section>

      <section class="filter-section">
        <span class="label">SELECT BENCHMARKS</span>
        <div class="series-list">
          <label class="series-item"><input type="checkbox" value="Dubai" checked onchange="render()"> DUBAI</label>
          <label class="series-item"><input type="checkbox" value="Brent" checked onchange="render()"> BRENT</label>
          <label class="series-item"><input type="checkbox" value="WTI" checked onchange="render()"> WTI</label>
        </div>
      </section>

      <section class="filter-section">
        <button class="btn" style="width:100%;border-color:#2f6287;color:#45a9f5;" onclick="downloadCSV()">
          EXPORT FILTERED CSV
        </button>
      </section>
    </aside>

    <section class="panel main-chart-panel">
      <div class="chart-heading">
        <div>
          <h1>INTERNATIONAL CRUDE OIL PRICE</h1>
          <div class="subtitle">DUBAI · BRENT · WTI / USD PER BARREL</div>
        </div>
        <div class="chart-stat">LINE CHART / RANGE SLIDER ENABLED</div>
      </div>
      <div id="chart"></div>
    </section>

    <section class="panel watchlist-panel">
      <div class="panel-header">
        <span class="panel-title">CRUDE OIL WATCHLIST</span>
        <span class="panel-meta">LAST / Δ / VOL</span>
      </div>
      <div id="watchlist"></div>
    </section>

    <section class="panel heatmap-panel">
      <div class="panel-header">
        <span class="panel-title">MARKET HEATMAP</span>
        <span class="panel-meta">LATEST DAILY MOVE</span>
      </div>
      <div class="heatmap-grid" id="heatmap"></div>
    </section>

    <section class="panel news-panel">
      <div class="panel-header">
        <span class="panel-title">MARKET INTELLIGENCE / MOVERS</span>
        <span class="panel-meta">DERIVED SIGNALS</span>
      </div>
      <div class="news-list" id="newsFeed"></div>
    </section>

    <section class="panel risk-panel">
      <div class="panel-header">
        <span class="panel-title">RISK INDICATORS</span>
        <span class="panel-meta">MARKET CONDITIONS</span>
      </div>
      <div class="risk-content" id="riskPanel"></div>
    </section>

    <footer class="footer-status">
      <div class="footer-item">MARKET HEALTH / <strong class="footer-green">NORMAL</strong></div>
      <div class="footer-item">SENTIMENT / <strong id="footerSentiment">NEUTRAL</strong></div>
      <div class="footer-item">ALERTS / <strong class="footer-amber" id="footerAlert">MONITORING ACTIVE</strong></div>
      <div class="footer-item">SOURCE / <strong>OPINET</strong></div>
    </footer>
  </section>
</main>

<script>
const OIL_DATA = __OIL_JSON__;

const COLORS = {
  Dubai: "#f6a623",
  Brent: "#45a9f5",
  WTI: "#36ca82"
};

let currentRange = null;
let currentPreset = "ALL";

function formatNumber(value, decimals = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "-";
  }

  return Number(value).toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals
  });
}

function signed(value, decimals = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "-";
  }

  return (Number(value) > 0 ? "+" : "") + formatNumber(value, decimals);
}

function selectedTypes() {
  return Array.from(document.querySelectorAll(".series-list input:checked"))
    .map(element => element.value);
}

function bounds() {
  const sorted = [...OIL_DATA].sort((a, b) => a.DATE.localeCompare(b.DATE));

  return {
    min: sorted[0]?.DATE || null,
    max: sorted[sorted.length - 1]?.DATE || null
  };
}

function setPreset(preset) {
  const marketBounds = bounds();

  if (!marketBounds.max) {
    return;
  }

  currentPreset = preset;

  if (preset === "ALL") {
    currentRange = null;
  } else if (preset === "YTD") {
    currentRange = {
      start: marketBounds.max.slice(0, 4) + "-01-01",
      end: marketBounds.max
    };
  } else {
    const date = new Date(marketBounds.max + "T00:00:00");
    date.setFullYear(date.getFullYear() - Number(preset.replace("Y", "")));

    currentRange = {
      start: date.toISOString().slice(0, 10),
      end: marketBounds.max
    };
  }

  document.querySelectorAll("[data-period]").forEach(button => {
    button.classList.toggle("active", button.dataset.period === preset);
  });

  render();
}

function filteredData() {
  const selected = selectedTypes();

  return OIL_DATA.filter(row => {
    if (!selected.includes(row.OIL_TYPE)) {
      return false;
    }

    if (!currentRange) {
      return true;
    }

    return row.DATE >= currentRange.start && row.DATE <= currentRange.end;
  });
}

function getOilStats(oilType, rows = OIL_DATA) {
  const oilRows = rows
    .filter(row => row.OIL_TYPE === oilType)
    .sort((a, b) => a.DATE.localeCompare(b.DATE));

  if (!oilRows.length) {
    return null;
  }

  const latest = oilRows[oilRows.length - 1];
  const previous = oilRows.length > 1 ? oilRows[oilRows.length - 2] : latest;
  const values = oilRows.map(row => Number(row.PRICE));
  const change = Number(latest.PRICE) - Number(previous.PRICE);
  const changePct = previous && Number(previous.PRICE)
    ? change / Number(previous.PRICE) * 100
    : 0;

  return {
    oilType,
    latest: Number(latest.PRICE),
    previous: Number(previous.PRICE),
    date: latest.DATE,
    change,
    changePct,
    high: Math.max(...values),
    low: Math.min(...values),
    observations: oilRows.length
  };
}

function dailyAverage(rows) {
  const grouped = {};

  rows.forEach(row => {
    if (!grouped[row.DATE]) {
      grouped[row.DATE] = [];
    }
    grouped[row.DATE].push(Number(row.PRICE));
  });

  return Object.keys(grouped).sort().map(date => ({
    date,
    value: grouped[date].reduce((sum, value) => sum + value, 0) / grouped[date].length
  }));
}

function standardDeviation(values) {
  if (!values.length) {
    return 0;
  }

  const mean = values.reduce((sum, value) => sum + value, 0) / values.length;
  const variance = values.reduce(
    (sum, value) => sum + Math.pow(value - mean, 2),
    0
  ) / values.length;

  return Math.sqrt(variance);
}

function updateKpis(rows) {
  document.getElementById("recordCount").textContent = rows.length.toLocaleString("en-US");
  document.getElementById("seriesCount").textContent = selectedTypes().length + " SERIES SELECTED";

  if (!rows.length) {
    ["latestValue", "averageValue", "highLow", "changeValue", "volatilityValue"].forEach(id => {
      document.getElementById(id).textContent = "-";
    });

    document.getElementById("latestDate").textContent = "NO DATA";
    document.getElementById("changeRate").textContent = "-";
    return;
  }

  const averages = dailyAverage(rows);
  const first = averages[0];
  const latest = averages[averages.length - 1];
  const values = rows.map(row => Number(row.PRICE));
  const average = values.reduce((sum, value) => sum + value, 0) / values.length;
  const high = Math.max(...values);
  const low = Math.min(...values);
  const change = latest.value - first.value;
  const rate = first.value ? change / first.value * 100 : 0;
  const volatility = standardDeviation(values);

  document.getElementById("latestValue").textContent = formatNumber(latest.value);
  document.getElementById("latestDate").textContent = latest.date + " AVG";
  document.getElementById("averageValue").textContent = formatNumber(average);
  document.getElementById("highLow").textContent =
    formatNumber(high, 0) + " / " + formatNumber(low, 0);
  document.getElementById("volatilityValue").textContent = formatNumber(volatility, 2);

  const changeElement = document.getElementById("changeValue");
  changeElement.textContent = signed(change);
  changeElement.className =
    "kpi-value " + (change > 0 ? "positive" : change < 0 ? "negative" : "");

  document.getElementById("changeRate").textContent =
    signed(rate, 2) + "% / " + first.date + " → " + latest.date;

  document.getElementById("footerSentiment").textContent =
    change > 0 ? "BULLISH" : change < 0 ? "BEARISH" : "NEUTRAL";

  document.getElementById("footerSentiment").className =
    change > 0 ? "footer-green" : change < 0 ? "negative" : "";
}

function renderChart(rows) {
  const chart = document.getElementById("chart");

  if (!rows.length) {
    Plotly.purge(chart);
    chart.innerHTML = '<div class="empty">NO DATA FOR SELECTED CONDITIONS</div>';
    return;
  }

  const groups = {};

  rows.forEach(row => {
    if (!groups[row.OIL_TYPE]) {
      groups[row.OIL_TYPE] = [];
    }
    groups[row.OIL_TYPE].push(row);
  });

  const traces = Object.entries(groups).map(([oilType, data]) => {
    data.sort((a, b) => a.DATE.localeCompare(b.DATE));

    return {
      x: data.map(row => row.DATE),
      y: data.map(row => Number(row.PRICE)),
      type: "scatter",
      mode: "lines",
      name: oilType.toUpperCase(),
      line: {
        color: COLORS[oilType] || "#dce9f5",
        width: 2
      },
      hovertemplate:
        "<b>" + oilType.toUpperCase() + "</b>" +
        "<br>DATE: %{x}" +
        "<br>PRICE: $%{y:,.2f}/BBL" +
        "<extra></extra>"
    };
  });

  const marketBounds = bounds();
  const chartRange = currentRange
    ? [currentRange.start, currentRange.end]
    : [marketBounds.min, marketBounds.max];

  const layout = {
    margin: {
      l: 62,
      r: 19,
      t: 18,
      b: 57
    },
    paper_bgcolor: "#07111c",
    plot_bgcolor: "#07111c",
    font: {
      family: '"Roboto Mono", Consolas, monospace',
      color: "#cdd9e4",
      size: 10
    },
    hovermode: "x unified",
    hoverlabel: {
      bgcolor: "#07111c",
      bordercolor: "#385976",
      font: {
        color: "#ffffff",
        family: '"Roboto Mono", Consolas, monospace'
      }
    },
    legend: {
      orientation: "h",
      x: 0,
      y: 1.11,
      font: {
        size: 9,
        color: "#a9bdce"
      }
    },
    xaxis: {
      type: "date",
      range: chartRange,
      gridcolor: "#142939",
      linecolor: "#365268",
      tickfont: {
        size: 9,
        color: "#8aa0b5"
      },
      rangeslider: {
        visible: true,
        bgcolor: "#040b12",
        bordercolor: "#29465d",
        thickness: .085
      }
    },
    yaxis: {
      title: {
        text: "USD / BBL",
        font: {
          size: 9,
          color: "#8aa0b5"
        }
      },
      gridcolor: "#142939",
      linecolor: "#365268",
      tickfont: {
        size: 9,
        color: "#8aa0b5"
      }
    }
  };

  Plotly.newPlot(chart, traces, layout, {
    responsive: true,
    displaylogo: false,
    modeBarButtonsToRemove: ["select2d", "lasso2d"]
  });

  chart.on("plotly_relayout", event => {
    const start = event["xaxis.range[0]"];
    const end = event["xaxis.range[1]"];

    if (!start || !end) {
      return;
    }

    currentRange = {
      start: String(start).slice(0, 10),
      end: String(end).slice(0, 10)
    };

    currentPreset = "CUSTOM";

    document.querySelectorAll("[data-period]").forEach(button => {
      button.classList.remove("active");
    });

    render();
  });
}

function renderTicker() {
  const stats = ["Dubai", "Brent", "WTI"]
    .map(oilType => getOilStats(oilType))
    .filter(Boolean);

  document.getElementById("tickerStream").innerHTML = stats.map(item => {
    const directionClass = item.change >= 0 ? "" : "negative";
    const arrow = item.change >= 0 ? "▲" : "▼";

    return `
      <div class="ticker-item">
        <span class="ticker-symbol">${item.oilType.toUpperCase()} CRUDE</span>
        <span class="ticker-value">${formatNumber(item.latest, 2)}</span>
        <span class="ticker-change ${directionClass}">
          ${arrow} ${signed(item.change, 2)} / ${signed(item.changePct, 2)}%
        </span>
      </div>
    `;
  }).join("");
}

function renderWatchlist() {
  const stats = ["Dubai", "Brent", "WTI"]
    .map(oilType => getOilStats(oilType))
    .filter(Boolean);

  let html = `
    <table class="watchlist-table">
      <thead>
        <tr>
          <th>BENCHMARK</th>
          <th>LAST</th>
          <th>CHANGE</th>
          <th>OBS</th>
        </tr>
      </thead>
      <tbody>
  `;

  stats.forEach(item => {
    const className = item.change >= 0 ? "watch-positive" : "watch-negative";

    html += `
      <tr>
        <td class="watch-symbol">
          ${item.oilType.toUpperCase()}
          <span class="watch-sub">CRUDE / ${item.date}</span>
        </td>
        <td>${formatNumber(item.latest, 2)}</td>
        <td class="${className}">
          ${signed(item.change, 2)}
          <span class="watch-sub ${className}">${signed(item.changePct, 2)}%</span>
        </td>
        <td>${item.observations.toLocaleString("en-US")}</td>
      </tr>
    `;
  });

  html += "</tbody></table>";
  document.getElementById("watchlist").innerHTML = html;
}

function renderHeatmap() {
  const stats = ["Dubai", "Brent", "WTI"]
    .map(oilType => getOilStats(oilType))
    .filter(Boolean);

  document.getElementById("heatmap").innerHTML = stats.map(item => {
    const state = item.change > 0 ? "heat-up" : item.change < 0 ? "heat-down" : "heat-neutral";
    const className = item.change >= 0 ? "positive" : "negative";

    return `
      <div class="heat-cell ${state}">
        <div class="heat-cell-name">${item.oilType.toUpperCase()}</div>
        <div class="heat-cell-value">${formatNumber(item.latest, 2)}</div>
        <div class="heat-cell-change ${className}">
          ${item.change >= 0 ? "▲" : "▼"} ${signed(item.change, 2)} / ${signed(item.changePct, 2)}%
        </div>
      </div>
    `;
  }).join("");
}

function renderNewsAndRisk(rows) {
  const stats = ["Dubai", "Brent", "WTI"]
    .map(oilType => getOilStats(oilType))
    .filter(Boolean)
    .sort((a, b) => Math.abs(b.changePct) - Math.abs(a.changePct));

  const leader = stats[0];
  const positiveCount = stats.filter(item => item.change > 0).length;
  const negativeCount = stats.filter(item => item.change < 0).length;
  const values = rows.map(row => Number(row.PRICE));
  const volatility = standardDeviation(values);
  const range = values.length ? Math.max(...values) - Math.min(...values) : 0;

  const newsItems = [
    {
      time: "LIVE",
      text: leader
        ? `<strong>${leader.oilType.toUpperCase()}</strong> is the largest daily mover at ${signed(leader.changePct, 2)}%.`
        : "No benchmark movement signal is currently available."
    },
    {
      time: "FLOW",
      text: `<strong>${positiveCount} ADVANCERS</strong> and <strong>${negativeCount} DECLINERS</strong> across monitored crude benchmarks.`
    },
    {
      time: "RANGE",
      text: `Selected price range is <strong>${formatNumber(range, 2)} USD/BBL</strong> across the active period.`
    }
  ];

  document.getElementById("newsFeed").innerHTML = newsItems.map(item => `
    <div class="news-item">
      <div class="news-time">${item.time}</div>
      <div class="news-text">${item.text}</div>
    </div>
  `).join("");

  const directionScore = stats.length ? positiveCount / stats.length * 100 : 0;
  const volatilityScore = Math.min(volatility * 10, 100);
  const rangeScore = Math.min(range * 2.5, 100);

  document.getElementById("riskPanel").innerHTML = `
    <div class="risk-row">
      <div class="risk-label-row">
        <span>BREADTH / ADVANCERS</span>
        <strong>${formatNumber(directionScore, 0)}%</strong>
      </div>
      <div class="risk-track">
        <div class="risk-fill" style="width:${directionScore}%;"></div>
      </div>
    </div>

    <div class="risk-row">
      <div class="risk-label-row">
        <span>PRICE VOLATILITY</span>
        <strong>${formatNumber(volatility, 2)} USD/BBL</strong>
      </div>
      <div class="risk-track">
        <div class="risk-fill warning" style="width:${volatilityScore}%;"></div>
      </div>
    </div>

    <div class="risk-row">
      <div class="risk-label-row">
        <span>PERIOD RANGE EXPOSURE</span>
        <strong>${formatNumber(range, 2)} USD/BBL</strong>
      </div>
      <div class="risk-track">
        <div class="risk-fill alert" style="width:${rangeScore}%;"></div>
      </div>
    </div>
  `;

  document.getElementById("footerAlert").textContent =
    range > 25 ? "ELEVATED RANGE WATCH" : "MONITORING ACTIVE";
}

function downloadCSV() {
  const rows = filteredData();

  if (!rows.length) {
    alert("다운로드할 데이터가 없습니다.");
    return;
  }

  const content = [
    "DATE,OIL_TYPE,PRICE",
    ...rows.map(row => row.DATE + "," + row.OIL_TYPE + "," + row.PRICE)
  ].join("\\n");

  const blob = new Blob(["\\uFEFF" + content], {
    type: "text/csv;charset=utf-8;"
  });

  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");

  anchor.href = url;
  anchor.download = "opinet_oil_prices_filtered.csv";

  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();

  URL.revokeObjectURL(url);
}

function render() {
  const rows = filteredData();

  updateKpis(rows);
  renderChart(rows);
  renderWatchlist();
  renderHeatmap();
  renderNewsAndRisk(rows);
}

document.addEventListener("DOMContentLoaded", () => {
  renderTicker();
  render();
});
</script>
</body>
</html>
"""

    html = (
        html_template
        .replace("__TERMINAL_CSS__", TERMINAL_CSS)
        .replace("__FETCHED_AT__", str(fetched_at))
        .replace("__OIL_JSON__", oil_json)
    )

    atomic_write_text(OIL_HTML_PATH, html)

    log("[HTML] Oil dashboard generated: OilPrice/index.html")


# =============================================================================
# 9. Static Build Process
# =============================================================================

def build_static_dashboard(force_refresh=False):
    """
    Collect or load all datasets, export CSV files, and generate
    GitHub Pages-compatible static HTML dashboards.
    """
    log("=" * 70)
    log("[BUILD] Energy Market Dashboard static build started")
    log(f"[BUILD] Force refresh: {force_refresh}")
    log("=" * 70)

    product_df, product_fetched_at = load_or_collect_data(
        force_refresh=force_refresh,
    )

    forecast_df, forecast_fetched_at, forecast_message = (
        load_or_collect_forecast_data(
            force_refresh=force_refresh,
        )
    )

    if forecast_message:
        log(f"[FORECAST] {forecast_message}")

    oil_df, oil_fetched_at = oil_load_or_collect_data(
        force_refresh=force_refresh,
    )

    oil_long_df = oil_to_long(oil_df)

    export_static_csv_files(
        product_df,
        forecast_df,
    )

    export_oil_csv(oil_long_df)

    generate_product_static_html(
        product_df,
        forecast_df,
        product_fetched_at,
    )

    generate_oil_static_html(
        oil_long_df,
        oil_fetched_at,
    )

    log("=" * 70)
    log("[BUILD] Static dashboard build completed")
    log(f"[BUILD] Bunker HTML: {BASE_DIR / 'index.html'}")
    log(f"[BUILD] Oil HTML:    {OIL_HTML_PATH}")
    log("=" * 70)

# =============================================================================
# 10. Flask Local Preview
# =============================================================================

@app.route("/")
def product_dashboard():
    html_path = BASE_DIR / "index.html"

    if not html_path.exists():
        return Response(
            "Dashboard file not found. Run with --build-static first.",
            status=404,
            mimetype="text/plain",
        )

    return Response(
        html_path.read_text(encoding="utf-8"),
        mimetype="text/html",
    )

@app.route("/OilPrice/")
@app.route("/OilPrice/index.html")
def oil_dashboard():
    if not OIL_HTML_PATH.exists():
        return Response(
            "Oil dashboard file not found. Run with --build-static first.",
            status=404,
            mimetype="text/plain",
        )

    return Response(
        OIL_HTML_PATH.read_text(encoding="utf-8"),
        mimetype="text/html",
    )

@app.route("/shipandbunker_vlsfo.csv")
def product_csv_file():
    if not PRODUCT_CSV_PATH.exists():
        return Response("CSV file not found.", status=404)

    return Response(
        PRODUCT_CSV_PATH.read_text(encoding="utf-8-sig"),
        mimetype="text/csv",
    )

@app.route("/bunker_forecast.csv")
def forecast_csv_file():
    if not FORECAST_CSV_PATH.exists():
        return Response("Forecast CSV file not found.", status=404)

    return Response(
        FORECAST_CSV_PATH.read_text(encoding="utf-8-sig"),
        mimetype="text/csv",
    )

@app.route("/OilPrice/opinet_oil_prices.csv")
def oil_csv_file():
    if not OIL_CSV_PATH.exists():
        return Response("Oil CSV file not found.", status=404)

    return Response(
        OIL_CSV_PATH.read_text(encoding="utf-8-sig"),
        mimetype="text/csv",
    )

# =============================================================================
# 11. CLI Entry Point
# =============================================================================

def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Energy Market Dashboard Builder",
    )

    parser.add_argument(
        "--build-static",
        action="store_true",
        help="Create CSV and HTML files for GitHub Pages.",
    )

    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Force Selenium crawling instead of using SQLite cache.",
    )

    parser.add_argument(
        "--serve",
        action="store_true",
        help="Start a local Flask preview server.",
    )

    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Flask preview host. Default: 127.0.0.1",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=5000,
        help="Flask preview port. Default: 5000",
    )

    return parser.parse_args()

def main():
    args = parse_arguments()

    # 옵션이 없을 때도 GitHub Actions/CLI에서 정적 생성이 되도록 설정
    if args.build_static or not args.serve:
        build_static_dashboard(
            force_refresh=args.refresh,
        )

    if args.serve:
        log(
            f"[SERVER] Local preview: "
            f"http://{args.host}:{args.port}"
        )

        app.run(
            host=args.host,
            port=args.port,
            debug=False,
        )

if __name__ == "__main__":
    main()
