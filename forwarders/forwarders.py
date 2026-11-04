import argparse
import datetime as dt
import html
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import traceback
from io import StringIO
from pathlib import Path

# [IDENTIFIER] A&A Top 25 Dashboard - Required for GitHub Actions search

# =====================================================
# 1. Environment Setup
# =====================================================
def setup_environment():
    required = {
        "requests": "requests",
        "pandas": "pandas",
        "bs4": "beautifulsoup4",
        "lxml": "lxml",
    }

    for import_name, pip_name in required.items():
        if importlib.util.find_spec(import_name) is not None:
            continue

        print(f"[SETUP] {pip_name} 패키지가 없어 설치를 시도합니다...", flush=True)

        try:
            subprocess.check_call([
                sys.executable,
                "-m",
                "pip",
                "install",
                pip_name
            ])
        except Exception:
            subprocess.check_call([
                sys.executable,
                "-m",
                "pip",
                "install",
                pip_name,
                "--break-system-packages"
            ])

setup_environment()

import pandas as pd
import requests
from bs4 import BeautifulSoup

# =====================================================
# 2. Configuration
# =====================================================
SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_URL = (
    "https://www.3plogistics.com/3pl-market-info-resources/"
    "3pl-market-information/aas-top-25-global-freight-forwarders-list/"
)

DB_PATH = SCRIPT_DIR / "freight_forwarders_data.db"
STATIC_HTML_PATH = SCRIPT_DIR / "index.html"
STATIC_CSV_PATH = SCRIPT_DIR / "freight_forwarders_history.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

EXPECTED_COLUMNS = [
    "rank",
    "provider",
    "hq",
    "revenue",
    "teu",
    "air_tons"
]

# =====================================================
# 3. Utilities
# =====================================================
def log(message):
    print(f"[LOG] {message}", flush=True)

def clean_number(value):
    text = str(value).strip()

    if not text or text.lower() in {"nan", "none", "-", "n/a", "na"}:
        return 0.0

    is_negative = (
        text.startswith("-")
        or (text.startswith("(") and text.endswith(")"))
    )

    text = (
        text.replace(",", "")
        .replace("(", "")
        .replace(")", "")
        .replace("$", "")
        .replace("€", "")
        .replace("£", "")
    )

    text = "".join(
        char for char in text
        if char.isdigit() or char == "."
    )

    if not text:
        return 0.0

    try:
        number = float(text)
        return -number if is_negative else number
    except ValueError:
        return 0.0

def normalize_provider_name(value):
    return " ".join(str(value).strip().split())

def format_compact_number(value, suffix=""):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "--"

    if number == 0:
        return "--"

    if abs(number) >= 1_000_000_000:
        return f"{number / 1_000_000_000:,.2f}B{suffix}"

    if abs(number) >= 1_000_000:
        return f"{number / 1_000_000:,.2f}M{suffix}"

    if abs(number) >= 1_000:
        return f"{number / 1_000:,.1f}K{suffix}"

    return f"{number:,.0f}{suffix}"

def calculate_percentage_change(current, previous):
    try:
        if previous is None:
            return None

        current_value = float(current)
        previous_value = float(previous)

        if previous_value == 0:
            return None

        return ((current_value - previous_value) / abs(previous_value)) * 100

    except Exception:
        return None

def get_delta_text(value):
    if value is None:
        return "N/A"

    prefix = "+" if value > 0 else ""
    return f"{prefix}{value:.1f}%"

def safe_float(value):
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0

# =====================================================
# 4. Web Crawling
# =====================================================
def select_best_table(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    tables = soup.find_all("table")

    if not tables:
        raise RuntimeError("페이지에서 <table> 태그를 찾지 못했습니다.")

    candidates = []

    for index, table in enumerate(tables):
        try:
            df = pd.read_html(StringIO(str(table)))[0]

            if df.empty:
                continue

            columns_text = str(df.columns).lower()

            score = sum(
                5 for keyword in ["rank", "provider", "revenue", "gross"]
                if keyword in columns_text
            )

            candidates.append((score, index, df))

        except Exception:
            continue

    if not candidates:
        raise RuntimeError("적합한 데이터 테이블을 파싱하지 못했습니다.")

    candidates.sort(key=lambda item: item[0], reverse=True)

    return candidates[0][2]

def fetch_top_25_data(url):
    log(f"Fetching URL: {url}")

    response = requests.get(
        url,
        headers=HEADERS,
        timeout=30
    )

    response.raise_for_status()

    df = select_best_table(response.text)

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [
            " ".join(
                str(column)
                for column in multi_column
                if str(column).lower() != "nan"
            ).strip()
            for multi_column in df.columns
        ]
    else:
        df.columns = [str(column).strip() for column in df.columns]

    df = df.iloc[:, :6].copy()
    df.columns = EXPECTED_COLUMNS

    for column in ["rank", "revenue", "teu", "air_tons"]:
        df[column] = df[column].apply(clean_number)

    df["rank"] = df["rank"].fillna(0).astype(int)
    df["provider"] = df["provider"].apply(normalize_provider_name)
    df["hq"] = df["hq"].astype(str).str.strip()

    df["date"] = dt.date.today().isoformat()
    df["source_url"] = url

    return df.sort_values("rank").reset_index(drop=True)

# =====================================================
# 5. Database Management
# =====================================================
def get_connection():
    conn = sqlite3.connect(DB_PATH)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS forwarders (
            date TEXT,
            rank INTEGER,
            provider TEXT,
            hq TEXT,
            revenue REAL,
            teu REAL,
            air_tons REAL,
            source_url TEXT,
            created_at TEXT,
            PRIMARY KEY (date, provider)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS run_log (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT,
            status TEXT,
            message TEXT,
            row_count INTEGER
        )
    """)

    conn.commit()

    return conn

def save_today_data(df):
    conn = get_connection()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        df["created_at"] = now

        conn.execute(
            "DELETE FROM forwarders WHERE date = ?",
            (df["date"].iloc[0],)
        )

        df.to_sql(
            "forwarders",
            conn,
            if_exists="append",
            index=False
        )

        conn.execute("""
            INSERT INTO run_log (
                run_at,
                status,
                message,
                row_count
            )
            VALUES (?, 'SUCCESS', 'OK', ?)
        """, (now, len(df)))

        conn.commit()

        history_df = pd.read_sql_query("""
            SELECT *
            FROM forwarders
            ORDER BY date DESC, rank ASC
        """, conn)

        history_df.to_csv(
            STATIC_CSV_PATH,
            index=False,
            encoding="utf-8-sig"
        )

        log(f"DB 저장 및 CSV 생성 완료: {STATIC_CSV_PATH}")

    except Exception as error:
        conn.execute("""
            INSERT INTO run_log (
                run_at,
                status,
                message,
                row_count
            )
            VALUES (?, 'ERROR', ?, 0)
        """, (now, str(error)))

        conn.commit()
        raise error

    finally:
        conn.close()

def get_dashboard_payload():
    """
    모든 저장 스냅샷을 반환한다.
    화면에서 최신 기준 데이터와 비교 기준일을 선택할 수 있도록
    snapshot_data 구조로 변환한다.
    """
    conn = get_connection()

    try:
        df = pd.read_sql_query("""
            SELECT
                date,
                rank,
                provider,
                hq,
                revenue,
                teu,
                air_tons,
                source_url,
                created_at
            FROM forwarders
            ORDER BY date DESC, rank ASC
        """, conn)

        if df.empty:
            return {}, [], None, None, None

        available_dates = sorted(
            df["date"].dropna().unique().tolist(),
            reverse=True
        )

        latest_date = available_dates[0]
        previous_date = (
            available_dates[1]
            if len(available_dates) > 1
            else None
        )

        source_url = ""
        latest_source = df.loc[
            df["date"] == latest_date,
            "source_url"
        ]

        if not latest_source.empty:
            source_url = str(latest_source.iloc[0])

        snapshots = {}

        for snapshot_date in available_dates:
            snapshot_df = df[df["date"] == snapshot_date].copy()
            snapshot_df = snapshot_df.sort_values("rank")

            records = []

            for _, row in snapshot_df.iterrows():
                records.append({
                    "rank": int(row["rank"]),
                    "provider": str(row["provider"]),
                    "hq": str(row["hq"]),
                    "revenue": safe_float(row["revenue"]),
                    "teu": safe_float(row["teu"]),
                    "air_tons": safe_float(row["air_tons"])
                })

            snapshots[snapshot_date] = records

        return (
            snapshots,
            available_dates,
            latest_date,
            previous_date,
            source_url
        )

    finally:
        conn.close()

# =====================================================
# 6. Static Dashboard Generation
# =====================================================
def build_static_dashboard():
    (
        snapshots,
        available_dates,
        latest_date,
        previous_date,
        source_url
    ) = get_dashboard_payload()

    if not snapshots or not latest_date:
        raise RuntimeError("대시보드를 생성할 데이터가 DB에 없습니다.")

    latest_data = snapshots.get(latest_date, [])

    total_revenue = sum(item["revenue"] for item in latest_data)
    total_teu = sum(item["teu"] for item in latest_data)
    total_air_tons = sum(item["air_tons"] for item in latest_data)

    generated_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    snapshot_json = json.dumps(
        snapshots,
        ensure_ascii=False
    ).replace("</", "<\\/")

    dates_json = json.dumps(
        available_dates,
        ensure_ascii=False
    )

    raw_html = """<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta
        name="viewport"
        content="width=device-width, initial-scale=1.0"
    >
    <title>FORWARDER MONITOR | MI Terminal</title>

    <!-- FORWARDER_BLOOMBERG_TEMPLATE_V2 -->

    <style>
        :root {
            --bg-core: #01050a;
            --bg-base: #030811;
            --bg-panel: #07111c;
            --bg-panel-2: #0a1724;
            --bg-panel-3: #0d1d2c;
            --border: #142a3e;
            --border-strong: #24435e;
            --text: #f0f5f9;
            --text-soft: #a0b2c2;
            --muted: #60788d;
            --dim: #385166;
            --amber: #f5a623;
            --amber-soft: #ffc35c;
            --blue: #4ba9f5;
            --green: #36ca82;
            --red: #ff6464;
            --shadow: 0 0 20px rgba(25, 99, 151, 0.10);
        }

        * {
            box-sizing: border-box;
        }

        html,
        body {
            min-height: 100%;
            background: var(--bg-core);
        }

        body {
            margin: 0;
            min-width: 320px;
            background:
                radial-gradient(
                    circle at 50% -20%,
                    rgba(35, 111, 165, 0.13),
                    transparent 42%
                ),
                linear-gradient(
                    180deg,
                    rgba(245, 166, 35, 0.035),
                    transparent 300px
                ),
                var(--bg-core);
            color: var(--text);
            font-family:
                "IBM Plex Mono",
                "Roboto Mono",
                "SFMono-Regular",
                Consolas,
                "Liberation Mono",
                monospace;
            font-size: 12px;
            line-height: 1.4;
            letter-spacing: -0.01em;
            -webkit-font-smoothing: antialiased;
        }

        button,
        select {
            font: inherit;
        }

        .terminal {
            width: min(1920px, calc(100% - 24px));
            margin: 10px auto 18px;
        }

        /* =================================================
           1. TOP TICKER BAR
           ================================================= */
        .topbar {
            min-height: 40px;
            display: flex;
            align-items: stretch;
            background:
                linear-gradient(
                    180deg,
                    #0a1826 0%,
                    #030912 100%
                );
            border: 1px solid var(--border-strong);
            box-shadow: 0 2px 14px rgba(0, 0, 0, 0.55);
        }

        .brand {
            min-width: 245px;
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 0 13px;
            border-right: 1px solid var(--border);
        }

        .brand-mark {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-height: 18px;
            padding: 2px 6px;
            background: var(--amber);
            color: #030303;
            font-size: 10px;
            font-weight: 900;
            letter-spacing: 0.05em;
        }

        .brand-name {
            color: #ffffff;
            font-size: 10px;
            font-weight: 800;
            letter-spacing: 0.09em;
            white-space: nowrap;
        }

        .ticker-stream {
            flex: 1;
            display: flex;
            overflow: hidden;
        }

        .ticker-item {
            min-width: 175px;
            display: grid;
            grid-template-columns: 1fr auto;
            grid-template-rows: 16px 15px;
            column-gap: 10px;
            padding: 4px 12px;
            border-right: 1px solid var(--border);
        }

        .ticker-label {
            grid-column: 1;
            color: var(--text-soft);
            font-size: 8px;
            font-weight: 800;
            letter-spacing: 0.07em;
            white-space: nowrap;
        }

        .ticker-value {
            grid-column: 2;
            grid-row: 1 / span 2;
            align-self: center;
            color: #ffffff;
            font-size: 13px;
            font-weight: 800;
            font-variant-numeric: tabular-nums;
            text-align: right;
        }

        .ticker-change {
            grid-column: 1;
            color: var(--muted);
            font-size: 8px;
            font-weight: 700;
            white-space: nowrap;
        }

        .ticker-change.up {
            color: var(--green);
        }

        .ticker-change.down {
            color: var(--red);
        }

        .topbar-status {
            min-width: 150px;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 7px;
            padding: 0 12px;
            color: var(--text-soft);
            font-size: 8px;
            font-weight: 800;
            letter-spacing: 0.06em;
            white-space: nowrap;
        }

        .live-dot {
            width: 6px;
            height: 6px;
            background: var(--green);
            border-radius: 50%;
            box-shadow: 0 0 8px rgba(54, 202, 130, 0.8);
        }

        /* =================================================
           TERMINAL STATUS STRIP / 12-COLUMN GRID
           ================================================= */
        .status-strip {
            min-height: 25px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            margin-top: 8px;
            padding: 0 10px;
            background: #02070d;
            border: 1px solid var(--border);
            border-left: 3px solid var(--green);
            color: var(--muted);
            font-size: 8px;
            letter-spacing: 0.06em;
        }

        .status-strip strong {
            color: var(--text-soft);
        }

        .grid-12 {
            display: grid;
            grid-template-columns: repeat(12, minmax(0, 1fr));
            gap: 8px;
            margin-top: 8px;
        }

        .panel {
            display: flex;
            flex-direction: column;
            min-width: 0;
            background:
                linear-gradient(
                    145deg,
                    rgba(12, 30, 46, 0.72),
                    rgba(4, 11, 18, 0.98)
                );
            border: 1px solid var(--border);
            box-shadow: var(--shadow);
        }

        .panel-header {
            min-height: 29px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
            padding: 0 10px;
            background:
                linear-gradient(
                    180deg,
                    #0b1a29,
                    #07111c
                );
            border-bottom: 1px solid var(--border);
        }

        .panel-title {
            color: var(--amber);
            font-size: 8px;
            font-weight: 900;
            letter-spacing: 0.09em;
            text-transform: uppercase;
        }

        .panel-meta {
            color: var(--muted);
            font-size: 8px;
            letter-spacing: 0.04em;
            text-align: right;
            text-transform: uppercase;
            white-space: nowrap;
        }

        /* =================================================
           2. KPI ROW
           ================================================= */
        .kpi-row {
            grid-column: span 12;
            display: grid;
            grid-template-columns: repeat(6, minmax(0, 1fr));
            gap: 8px;
        }

        .kpi-card {
            min-height: 88px;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            padding: 9px 10px;
            background:
                linear-gradient(
                    135deg,
                    rgba(26, 77, 111, 0.24),
                    transparent 62%
                ),
                var(--bg-panel);
            border: 1px solid var(--border);
            border-top: 2px solid #245277;
        }

        .kpi-label {
            color: var(--muted);
            font-size: 8px;
            font-weight: 800;
            letter-spacing: 0.08em;
            text-transform: uppercase;
        }

        .kpi-value {
            margin: 6px 0 3px;
            color: #ffffff;
            font-size: clamp(17px, 1.65vw, 24px);
            font-weight: 800;
            font-variant-numeric: tabular-nums;
            letter-spacing: -0.06em;
            text-align: right;
        }

        .kpi-sub {
            padding-top: 4px;
            border-top: 1px dotted #1a344b;
            color: var(--text-soft);
            font-size: 8px;
            text-align: right;
            text-transform: uppercase;
        }

        /* =================================================
           3. LARGE MAIN CHART PANEL
           ================================================= */
        .chart-panel {
            grid-column: span 8;
            min-height: 445px;
        }

        .chart-heading {
            padding: 10px 11px 8px;
            border-bottom: 1px solid #12273a;
            background:
                linear-gradient(
                    90deg,
                    rgba(27, 79, 114, 0.22),
                    transparent 65%
                );
        }

        .chart-heading h1 {
            margin: 0;
            color: #ffffff;
            font-size: 15px;
            font-weight: 800;
            letter-spacing: 0.015em;
        }

        .chart-heading p {
            margin: 3px 0 0;
            color: var(--muted);
            font-size: 8px;
            letter-spacing: 0.05em;
        }

        .control-row {
            display: grid;
            grid-template-columns: minmax(150px, 1fr) minmax(150px, 1fr) auto;
            gap: 7px;
            padding: 8px 10px;
            border-bottom: 1px solid #12273a;
            background: rgba(3, 9, 15, 0.55);
        }

        .control-field {
            min-width: 0;
        }

        .control-label {
            display: block;
            margin-bottom: 4px;
            color: var(--muted);
            font-size: 7px;
            font-weight: 800;
            letter-spacing: 0.08em;
            text-transform: uppercase;
        }

        select {
            width: 100%;
            height: 25px;
            padding: 0 7px;
            appearance: none;
            background: #030a12;
            border: 1px solid #1d3c56;
            color: var(--amber-soft);
            cursor: pointer;
            font-size: 9px;
            font-weight: 700;
            outline: none;
        }

        select:focus {
            border-color: var(--amber);
        }

        .control-note {
            align-self: end;
            padding: 0 0 5px;
            color: var(--muted);
            font-size: 7px;
            letter-spacing: 0.04em;
            text-align: right;
            white-space: nowrap;
        }

        .chart-wrap {
            flex: 1;
            min-height: 275px;
            padding: 8px 10px 5px;
        }

        #trendChart {
            display: block;
            width: 100%;
            height: 100%;
            min-height: 272px;
            overflow: visible;
        }

        .chart-grid-line {
            stroke: #10263a;
            stroke-width: 1;
        }

        .chart-axis-label {
            fill: #61778a;
            font-size: 9px;
            font-family: inherit;
        }

        .chart-line {
            fill: none;
            stroke: #4ba9f5;
            stroke-width: 2.2;
            filter: drop-shadow(0 0 4px rgba(75, 169, 245, 0.32));
        }

        .chart-area {
            fill: url(#areaGradient);
            opacity: 0.8;
        }

        .chart-point {
            fill: var(--amber);
            stroke: #fff2d3;
            stroke-width: 1;
        }

        .chart-empty {
            fill: #71889a;
            font-size: 11px;
            font-family: inherit;
            text-anchor: middle;
        }

        /* =================================================
           4. RIGHT-SIDE WATCHLIST PANEL
           ================================================= */
        .watchlist-panel {
            grid-column: span 4;
            min-height: 445px;
        }

        .watch-table-wrap {
            overflow: auto;
            flex: 1;
        }

        .watch-table,
        .ranking-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 9px;
        }

        .watch-table th,
        .ranking-table th {
            padding: 7px 8px;
            background: #040c15;
            border-bottom: 1px solid var(--border);
            color: var(--muted);
            font-size: 7px;
            font-weight: 900;
            letter-spacing: 0.07em;
            text-align: right;
            text-transform: uppercase;
            white-space: nowrap;
        }

        .watch-table th:first-child,
        .ranking-table th:first-child,
        .watch-table td:first-child,
        .ranking-table td:first-child {
            text-align: left;
        }

        .watch-table td {
            padding: 8px;
            border-bottom: 1px solid #0c1d2c;
            color: var(--text);
            font-variant-numeric: tabular-nums;
            text-align: right;
            white-space: nowrap;
        }

        .watch-table tr:hover td,
        .ranking-table tbody tr:hover td {
            background: rgba(42, 113, 160, 0.10);
        }

        .watch-name {
            color: #ffffff;
            font-weight: 800;
        }

        .watch-sub {
            display: block;
            margin-top: 2px;
            color: var(--muted);
            font-size: 7px;
        }

        /* =================================================
           5. BOTTOM-LEFT MACRO / HEATMAP PANEL
           ================================================= */
        .macro-panel {
            grid-column: span 4;
        }

        .heatmap-grid {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 5px;
            padding: 8px;
        }

        .heat-cell {
            min-height: 71px;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            padding: 7px;
            background: #040c15;
            border: 1px solid #17344b;
            border-left: 3px solid var(--blue);
        }

        .heat-cell.positive {
            border-left-color: var(--green);
            background:
                linear-gradient(
                    135deg,
                    rgba(54, 202, 130, 0.14),
                    #040c15 78%
                );
        }

        .heat-cell.negative {
            border-left-color: var(--red);
            background:
                linear-gradient(
                    135deg,
                    rgba(255, 100, 100, 0.13),
                    #040c15 78%
                );
        }

        .heat-label {
            color: var(--muted);
            font-size: 7px;
            font-weight: 800;
            letter-spacing: 0.06em;
            text-transform: uppercase;
        }

        .heat-value {
            color: #ffffff;
            font-size: 13px;
            font-weight: 800;
            font-variant-numeric: tabular-nums;
            text-align: right;
        }

        .heat-delta {
            font-size: 8px;
            font-weight: 800;
            text-align: right;
        }

        /* =================================================
           6. NEWS / MOVERS / RISK PANELS
           ================================================= */
        .news-panel {
            grid-column: span 4;
        }

        .risk-panel {
            grid-column: span 4;
        }

        .news-feed {
            padding: 7px 9px;
        }

        .news-item {
            display: grid;
            grid-template-columns: 43px 1fr;
            gap: 7px;
            padding: 7px 0;
            border-bottom: 1px dotted #163047;
        }

        .news-item:last-child {
            border-bottom: 0;
        }

        .news-tag {
            color: var(--amber);
            font-size: 7px;
            font-weight: 900;
            letter-spacing: 0.05em;
        }

        .news-text {
            color: var(--text-soft);
            font-size: 8px;
            line-height: 1.45;
        }

        .news-text strong {
            color: #ffffff;
        }

        .risk-wrap {
            display: grid;
            gap: 10px;
            padding: 10px;
        }

        .risk-row {
            display: grid;
            gap: 4px;
        }

        .risk-head {
            display: flex;
            justify-content: space-between;
            gap: 12px;
            color: var(--muted);
            font-size: 7px;
            font-weight: 800;
            letter-spacing: 0.05em;
            text-transform: uppercase;
        }

        .risk-head strong {
            color: #ffffff;
            font-size: 8px;
        }

        .risk-bar {
            height: 6px;
            overflow: hidden;
            background: #02070d;
            border: 1px solid #153249;
        }

        .risk-fill {
            height: 100%;
            background: linear-gradient(90deg, #2378b5, #62bfff);
        }

        .risk-fill.warn {
            background: linear-gradient(90deg, #a87015, var(--amber));
        }

        .risk-fill.alert {
            background: linear-gradient(90deg, #a23a3a, var(--red));
        }

        /* =================================================
           RANKING TABLE
           ================================================= */
        .ranking-panel {
            grid-column: span 12;
        }

        .ranking-table-wrap {
            overflow-x: auto;
        }

        .ranking-table {
            min-width: 1000px;
        }

        .ranking-table td {
            padding: 9px 10px;
            border-bottom: 1px solid #0d2132;
            color: var(--text);
            vertical-align: middle;
        }

        .ranking-table td.number {
            font-variant-numeric: tabular-nums;
            text-align: right;
            white-space: nowrap;
        }

        .rank-cell {
            color: var(--amber);
            font-size: 12px;
            font-weight: 900;
            font-variant-numeric: tabular-nums;
        }

        .provider-name {
            color: #ffffff;
            font-size: 10px;
            font-weight: 800;
        }

        .provider-hq {
            margin-top: 2px;
            color: var(--muted);
            font-size: 8px;
        }

        .metric-value {
            color: #ffffff;
            font-weight: 800;
        }

        .metric-delta {
            margin-top: 2px;
            color: var(--muted);
            font-size: 8px;
        }

        .positive {
            color: var(--green) !important;
        }

        .negative {
            color: var(--red) !important;
        }

        .neutral {
            color: var(--muted) !important;
        }

        .empty-state {
            padding: 35px !important;
            color: var(--muted) !important;
            text-align: center;
        }

        /* =================================================
           7. FOOTER STATUS BAR
           ================================================= */
        .footer-bar {
            grid-column: span 12;
            min-height: 27px;
            display: grid;
            grid-template-columns: 1.2fr 1fr 1.4fr auto;
            align-items: center;
            padding: 0 9px;
            background: #02070c;
            border: 1px solid var(--border);
            color: var(--muted);
            font-size: 7px;
            letter-spacing: 0.055em;
            text-transform: uppercase;
        }

        .footer-section {
            padding: 0 9px;
            border-right: 1px solid #112536;
        }

        .footer-section:last-child {
            border-right: 0;
            text-align: right;
        }

        .footer-section strong {
            color: var(--text-soft);
        }

        .footer-section a {
            color: var(--amber-soft);
            text-decoration: none;
        }

        .footer-section a:hover {
            color: #ffffff;
        }

        /* =================================================
           RESPONSIVE
           ================================================= */
        @media (max-width: 1250px) {
            .kpi-row {
                grid-template-columns: repeat(3, minmax(0, 1fr));
            }

            .chart-panel {
                grid-column: span 8;
            }

            .watchlist-panel {
                grid-column: span 4;
            }
        }

        @media (max-width: 920px) {
            .terminal {
                width: min(100% - 14px, 1920px);
                margin-top: 7px;
            }

            .topbar {
                flex-wrap: wrap;
            }

            .brand {
                min-width: auto;
                border-right: 0;
            }

            .ticker-stream {
                order: 3;
                width: 100%;
                flex-basis: 100%;
                border-top: 1px solid var(--border);
            }

            .topbar-status {
                margin-left: auto;
            }

            .chart-panel,
            .watchlist-panel,
            .macro-panel,
            .news-panel,
            .risk-panel {
                grid-column: span 12;
            }

            .chart-panel,
            .watchlist-panel {
                min-height: auto;
            }

            .footer-bar {
                grid-template-columns: 1fr 1fr;
                gap: 4px 0;
                padding: 6px 0;
            }

            .footer-section:nth-child(2) {
                border-right: 0;
            }
        }

        @media (max-width: 580px) {
            .kpi-row {
                grid-template-columns: repeat(2, minmax(0, 1fr));
            }

            .control-row {
                grid-template-columns: 1fr;
            }

            .control-note {
                padding: 0;
                text-align: left;
                white-space: normal;
            }

            .footer-bar {
                grid-template-columns: 1fr;
            }

            .footer-section {
                border-right: 0;
            }

            .footer-section:last-child {
                text-align: left;
            }

            .ticker-item {
                min-width: 155px;
            }
        }
    </style>
</head>

<body>
    <main class="terminal">

        <!-- 1. TOP TICKER BAR -->
        <header class="topbar">
            <div class="brand">
                <span class="brand-mark">MI&lt;GO&gt;</span>
                <span class="brand-name">GLOBAL LOGISTICS INTELLIGENCE</span>
            </div>

            <div class="ticker-stream" id="tickerStream"></div>

            <div class="topbar-status">
                <span class="live-dot"></span>
                <span>STATIC MARKET SNAPSHOT</span>
            </div>
        </header>

        <div class="status-strip">
            <span>
                DATASET / <strong>A&amp;A TOP 25 GLOBAL FREIGHT FORWARDERS</strong>
            </span>
            <span>
                GENERATED / <strong>__GENERATED_AT__</strong>
            </span>
        </div>

        <div class="grid-12">

            <!-- 2. KPI ROW -->
            <section class="kpi-row">
                <div class="kpi-card">
                    <span class="kpi-label">Total Revenue</span>
                    <span class="kpi-value" id="totalRevenue">--</span>
                    <span class="kpi-sub">Available Top 25 Records</span>
                </div>

                <div class="kpi-card">
                    <span class="kpi-label">Ocean Volume</span>
                    <span class="kpi-value" id="totalTeu">--</span>
                    <span class="kpi-sub">Ocean Freight / TEU</span>
                </div>

                <div class="kpi-card">
                    <span class="kpi-label">Air Volume</span>
                    <span class="kpi-value" id="totalAir">--</span>
                    <span class="kpi-sub">Air Freight / Tons</span>
                </div>

                <div class="kpi-card">
                    <span class="kpi-label">Tracked Providers</span>
                    <span class="kpi-value" id="providerCount">--</span>
                    <span class="kpi-sub">Ranking Constituents</span>
                </div>

                <div class="kpi-card">
                    <span class="kpi-label">Market Leader</span>
                    <span class="kpi-value" id="leaderName">--</span>
                    <span class="kpi-sub" id="leaderMetric">Revenue Leadership</span>
                </div>

                <div class="kpi-card">
                    <span class="kpi-label">Snapshot Delta</span>
                    <span class="kpi-value" id="snapshotDelta">--</span>
                    <span class="kpi-sub" id="snapshotDeltaSub">Comparison Pending</span>
                </div>
            </section>

            <!-- 3. MAIN CHART PANEL -->
            <section class="panel chart-panel">
                <div class="panel-header">
                    <span class="panel-title">Global Forwarder Revenue Trend</span>
                    <span class="panel-meta">Historical Snapshot Series</span>
                </div>

                <div class="chart-heading">
                    <h1>FORWARDER MARKET CAPITALIZATION PROXY</h1>
                    <p>
                        AGGREGATE REPORTED REVENUE OF AVAILABLE RANKING
                        CONSTITUENTS BY SNAPSHOT DATE
                    </p>
                </div>

                <div class="control-row">
                    <div class="control-field">
                        <label class="control-label" for="currentDateSelect">
                            Current Snapshot
                        </label>
                        <select id="currentDateSelect"></select>
                    </div>

                    <div class="control-field">
                        <label class="control-label" for="compareDateSelect">
                            Compare Against
                        </label>
                        <select id="compareDateSelect"></select>
                    </div>

                    <div class="control-note">
                        DELTA = CURRENT VS SELECTED COMPARISON SNAPSHOT
                    </div>
                </div>

                <div class="chart-wrap">
                    <svg
                        id="trendChart"
                        viewBox="0 0 800 290"
                        preserveAspectRatio="none"
                        aria-label="Historical total revenue trend chart"
                    ></svg>
                </div>
            </section>

            <!-- 4. RIGHT-SIDE WATCHLIST PANEL -->
            <section class="panel watchlist-panel">
                <div class="panel-header">
                    <span class="panel-title">Top Provider Watchlist</span>
                    <span class="panel-meta">Revenue / Rank / TEU</span>
                </div>

                <div class="watch-table-wrap">
                    <table class="watch-table">
                        <thead>
                            <tr>
                                <th>Provider</th>
                                <th>Revenue</th>
                                <th>Rank</th>
                                <th>Ocean TEU</th>
                            </tr>
                        </thead>
                        <tbody id="watchlistBody"></tbody>
                    </table>
                </div>
            </section>

            <!-- 5. BOTTOM-LEFT MACRO / MARKET HEATMAP -->
            <section class="panel macro-panel">
                <div class="panel-header">
                    <span class="panel-title">Market Heatmap</span>
                    <span class="panel-meta">Current vs Comparison</span>
                </div>
                <div class="heatmap-grid" id="heatmapGrid"></div>
            </section>

            <!-- 6. BOTTOM-CENTER NEWS / MOVERS -->
            <section class="panel news-panel">
                <div class="panel-header">
                    <span class="panel-title">Market Intelligence &amp; Movers</span>
                    <span class="panel-meta">Derived Signals</span>
                </div>
                <div class="news-feed" id="newsFeed"></div>
            </section>

            <!-- 6. BOTTOM-RIGHT RISK INDICATORS -->
            <section class="panel risk-panel">
                <div class="panel-header">
                    <span class="panel-title">Risk &amp; Concentration Profile</span>
                    <span class="panel-meta">Index Health</span>
                </div>
                <div class="risk-wrap" id="riskPanel"></div>
            </section>

            <!-- DETAILED RANKING TABLE -->
            <section class="panel ranking-panel">
                <div class="panel-header">
                    <span class="panel-title">Global Freight Forwarder Ranking</span>
                    <span class="panel-meta" id="tableMeta">
                        Loading Snapshot Data...
                    </span>
                </div>

                <div class="ranking-table-wrap">
                    <table class="ranking-table">
                        <thead>
                            <tr>
                                <th>Rank</th>
                                <th>Provider / Headquarters</th>
                                <th style="text-align:right;">Revenue</th>
                                <th style="text-align:right;">Ocean TEU</th>
                                <th style="text-align:right;">Air Tons</th>
                                <th style="text-align:right;">Rank Move</th>
                            </tr>
                        </thead>
                        <tbody id="rankingTableBody"></tbody>
                    </table>
                </div>
            </section>

            <!-- 7. FOOTER STATUS BAR -->
            <footer class="footer-bar">
                <div class="footer-section">
                    MARKET HEALTH /
                    <strong id="footerHealth" class="positive">NORMAL</strong>
                </div>
                <div class="footer-section">
                    SENTIMENT /
                    <strong id="footerSentiment">NEUTRAL</strong>
                </div>
                <div class="footer-section">
                    ALERT /
                    <strong id="footerAlert">MONITORING ACTIVE</strong>
                </div>
                <div class="footer-section">
                    SOURCE /
                    <a
                        href="__SOURCE_URL__"
                        target="_blank"
                        rel="noopener noreferrer"
                    >A&amp;A / 3PL LOGISTICS</a>
                </div>
            </footer>
        </div>
    </main>

    <script>
        const SNAPSHOTS = __SNAPSHOT_JSON__;
        const AVAILABLE_DATES = __DATES_JSON__;
        const DEFAULT_CURRENT_DATE = "__LATEST_DATE__";
        const DEFAULT_COMPARE_DATE = "__PREVIOUS_DATE__";

        const currentDateSelect = document.getElementById("currentDateSelect");
        const compareDateSelect = document.getElementById("compareDateSelect");
        const rankingTableBody = document.getElementById("rankingTableBody");

        function escapeHtml(value) {
            return String(value ?? "")
                .replace(/&/g, "&amp;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;")
                .replace(/"/g, "&quot;")
                .replace(/'/g, "&#039;");
        }

        function compactNumber(value) {
            const number = Number(value);

            if (!Number.isFinite(number) || number === 0) {
                return "--";
            }

            const absolute = Math.abs(number);

            if (absolute >= 1000000000) {
                return `${(number / 1000000000).toFixed(2)}B`;
            }

            if (absolute >= 1000000) {
                return `${(number / 1000000).toFixed(2)}M`;
            }

            if (absolute >= 1000) {
                return `${(number / 1000).toFixed(1)}K`;
            }

            return Math.round(number).toLocaleString("en-US");
        }

        function percentageChange(current, previous) {
            const currentNumber = Number(current);
            const previousNumber = Number(previous);

            if (
                !Number.isFinite(currentNumber)
                || !Number.isFinite(previousNumber)
                || previousNumber === 0
            ) {
                return null;
            }

            return ((currentNumber - previousNumber) / Math.abs(previousNumber)) * 100;
        }

        function deltaClass(value) {
            if (value === null || value === undefined || !Number.isFinite(value)) {
                return "neutral";
            }

            if (value > 0) {
                return "positive";
            }

            if (value < 0) {
                return "negative";
            }

            return "neutral";
        }

        function deltaText(value) {
            if (value === null || value === undefined || !Number.isFinite(value)) {
                return "N/A";
            }

            if (value > 0) {
                return `+${value.toFixed(1)}%`;
            }

            return `${value.toFixed(1)}%`;
        }

        function totalMetric(data, field) {
            return data.reduce(
                (sum, item) => sum + Number(item[field] || 0),
                0
            );
        }

        function metricCell(value, previousValue) {
            const delta = percentageChange(value, previousValue);

            return `
                <td class="number">
                    <div class="metric-value">${compactNumber(value)}</div>
                    <div class="metric-delta ${deltaClass(delta)}">
                        ${deltaText(delta)}
                    </div>
                </td>
            `;
        }

        function rankMoveCell(currentRank, previousRecord) {
            if (!previousRecord) {
                return `
                    <td class="number">
                        <div class="metric-delta neutral">N/A</div>
                    </td>
                `;
            }

            const movement = Number(previousRecord.rank) - Number(currentRank);

            if (movement > 0) {
                return `
                    <td class="number">
                        <div class="positive">▲ +${movement}</div>
                    </td>
                `;
            }

            if (movement < 0) {
                return `
                    <td class="number">
                        <div class="negative">▼ ${movement}</div>
                    </td>
                `;
            }

            return `
                <td class="number">
                    <div class="neutral">— 0</div>
                </td>
            `;
        }

        function populateDateSelectors() {
            currentDateSelect.innerHTML = "";
            compareDateSelect.innerHTML = "";

            AVAILABLE_DATES.forEach((date) => {
                const currentOption = document.createElement("option");
                currentOption.value = date;
                currentOption.textContent = date;
                currentDateSelect.appendChild(currentOption);

                const compareOption = document.createElement("option");
                compareOption.value = date;
                compareOption.textContent = date;
                compareDateSelect.appendChild(compareOption);
            });

            const noComparisonOption = document.createElement("option");
            noComparisonOption.value = "";
            noComparisonOption.textContent = "NO COMPARISON";
            compareDateSelect.insertBefore(
                noComparisonOption,
                compareDateSelect.firstChild
            );

            currentDateSelect.value = DEFAULT_CURRENT_DATE;

            if (DEFAULT_COMPARE_DATE) {
                compareDateSelect.value = DEFAULT_COMPARE_DATE;
            } else {
                compareDateSelect.value = "";
            }
        }

        function buildSnapshotSeries() {
            return AVAILABLE_DATES
                .slice()
                .reverse()
                .map((date) => {
                    const records = SNAPSHOTS[date] || [];

                    return {
                        date,
                        revenue: totalMetric(records, "revenue"),
                        teu: totalMetric(records, "teu"),
                        air: totalMetric(records, "air_tons"),
                        count: records.length
                    };
                });
        }

        function renderTrendChart() {
            const svg = document.getElementById("trendChart");
            const series = buildSnapshotSeries();

            if (!series.length) {
                svg.innerHTML = `
                    <text
                        x="400"
                        y="145"
                        class="chart-empty"
                    >NO HISTORICAL SNAPSHOT DATA AVAILABLE</text>
                `;
                return;
            }

            const width = 800;
            const height = 290;
            const left = 58;
            const right = 20;
            const top = 20;
            const bottom = 38;
            const plotWidth = width - left - right;
            const plotHeight = height - top - bottom;

            const values = series.map((item) => item.revenue);
            const minValue = Math.min(...values);
            const maxValue = Math.max(...values);
            const range = maxValue - minValue || 1;
            const paddedMin = Math.max(0, minValue - range * 0.15);
            const paddedMax = maxValue + range * 0.15;
            const paddedRange = paddedMax - paddedMin || 1;

            const x = (index) => {
                if (series.length === 1) {
                    return left + plotWidth / 2;
                }

                return left + (index / (series.length - 1)) * plotWidth;
            };

            const y = (value) => {
                return top + ((paddedMax - value) / paddedRange) * plotHeight;
            };

            const gridLines = [];
            const labels = [];
            const gridCount = 4;

            for (let index = 0; index <= gridCount; index++) {
                const ratio = index / gridCount;
                const lineY = top + ratio * plotHeight;
                const value = paddedMax - ratio * paddedRange;

                gridLines.push(`
                    <line
                        x1="${left}"
                        y1="${lineY}"
                        x2="${width - right}"
                        y2="${lineY}"
                        class="chart-grid-line"
                    />
                `);

                labels.push(`
                    <text
                        x="${left - 8}"
                        y="${lineY + 3}"
                        class="chart-axis-label"
                        text-anchor="end"
                    >${compactNumber(value)}</text>
                `);
            }

            const points = series.map((item, index) => {
                return `${x(index).toFixed(2)},${y(item.revenue).toFixed(2)}`;
            });

            const areaPoints = [
                `${x(0).toFixed(2)},${top + plotHeight}`,
                ...points,
                `${x(series.length - 1).toFixed(2)},${top + plotHeight}`
            ].join(" ");

            const pointNodes = series.map((item, index) => `
                <circle
                    cx="${x(index).toFixed(2)}"
                    cy="${y(item.revenue).toFixed(2)}"
                    r="3.2"
                    class="chart-point"
                >
                    <title>
                        ${item.date}: ${compactNumber(item.revenue)}
                    </title>
                </circle>
            `).join("");

            const xLabels = series.map((item, index) => {
                const isFirst = index === 0;
                const isLast = index === series.length - 1;
                const isMiddle = series.length <= 5 || index % Math.ceil(series.length / 5) === 0;

                if (!isFirst && !isLast && !isMiddle) {
                    return "";
                }

                return `
                    <text
                        x="${x(index).toFixed(2)}"
                        y="${height - 12}"
                        class="chart-axis-label"
                        text-anchor="middle"
                    >${item.date}</text>
                `;
            }).join("");

            svg.innerHTML = `
                <defs>
                    <linearGradient
                        id="areaGradient"
                        x1="0"
                        y1="0"
                        x2="0"
                        y2="1"
                    >
                        <stop
                            offset="0%"
                            stop-color="#4ba9f5"
                            stop-opacity="0.34"
                        />
                        <stop
                            offset="100%"
                            stop-color="#4ba9f5"
                            stop-opacity="0.01"
                        />
                    </linearGradient>
                </defs>

                ${gridLines.join("")}
                ${labels.join("")}

                <polygon
                    points="${areaPoints}"
                    class="chart-area"
                />

                <polyline
                    points="${points.join(" ")}"
                    class="chart-line"
                />

                ${pointNodes}
                ${xLabels}
            `;
        }

        function renderTicker(currentData, compareData) {
            const currentRevenue = totalMetric(currentData, "revenue");
            const previousRevenue = totalMetric(compareData, "revenue");
            const currentTeu = totalMetric(currentData, "teu");
            const previousTeu = totalMetric(compareData, "teu");
            const currentAir = totalMetric(currentData, "air_tons");
            const previousAir = totalMetric(compareData, "air_tons");

            const tickerItems = [
                {
                    label: "FORWARDER REVENUE",
                    value: compactNumber(currentRevenue),
                    delta: percentageChange(currentRevenue, previousRevenue)
                },
                {
                    label: "OCEAN FREIGHT TEU",
                    value: compactNumber(currentTeu),
                    delta: percentageChange(currentTeu, previousTeu)
                },
                {
                    label: "AIR FREIGHT TONS",
                    value: compactNumber(currentAir),
                    delta: percentageChange(currentAir, previousAir)
                },
                {
                    label: "TRACKED PROVIDERS",
                    value: String(currentData.length).padStart(2, "0"),
                    delta: compareData.length
                        ? percentageChange(currentData.length, compareData.length)
                        : null
                }
            ];

            document.getElementById("tickerStream").innerHTML = tickerItems.map((item) => {
                const state = item.delta > 0
                    ? "up"
                    : item.delta < 0
                        ? "down"
                        : "";

                const direction = item.delta > 0
                    ? "▲"
                    : item.delta < 0
                        ? "▼"
                        : "—";

                return `
                    <div class="ticker-item">
                        <span class="ticker-label">${item.label}</span>
                        <span class="ticker-value">${item.value}</span>
                        <span class="ticker-change ${state}">
                            ${direction} ${deltaText(item.delta)}
                        </span>
                    </div>
                `;
            }).join("");
        }

        function renderWatchlist(currentData, compareMap) {
            const leaders = currentData
                .slice()
                .sort((a, b) => Number(b.revenue) - Number(a.revenue))
                .slice(0, 8);

            const html = leaders.map((item) => {
                const previous = compareMap.get(item.provider);
                const revenueDelta = percentageChange(
                    item.revenue,
                    previous ? previous.revenue : null
                );

                return `
                    <tr>
                        <td>
                            <span class="watch-name">
                                ${escapeHtml(item.provider)}
                            </span>
                            <span class="watch-sub">
                                ${escapeHtml(item.hq)}
                            </span>
                        </td>
                        <td>
                            ${compactNumber(item.revenue)}
                            <span class="watch-sub ${deltaClass(revenueDelta)}">
                                ${deltaText(revenueDelta)}
                            </span>
                        </td>
                        <td>#${String(item.rank).padStart(2, "0")}</td>
                        <td>${compactNumber(item.teu)}</td>
                    </tr>
                `;
            }).join("");

            document.getElementById("watchlistBody").innerHTML = html || `
                <tr>
                    <td colspan="4" class="empty-state">
                        NO WATCHLIST DATA AVAILABLE.
                    </td>
                </tr>
            `;
        }

        function renderHeatmap(currentData, compareData) {
            const metrics = [
                {
                    label: "Revenue",
                    current: totalMetric(currentData, "revenue"),
                    previous: totalMetric(compareData, "revenue")
                },
                {
                    label: "Ocean TEU",
                    current: totalMetric(currentData, "teu"),
                    previous: totalMetric(compareData, "teu")
                },
                {
                    label: "Air Tons",
                    current: totalMetric(currentData, "air_tons"),
                    previous: totalMetric(compareData, "air_tons")
                },
                {
                    label: "Provider Count",
                    current: currentData.length,
                    previous: compareData.length
                }
            ];

            document.getElementById("heatmapGrid").innerHTML = metrics.map((item) => {
                const delta = percentageChange(item.current, item.previous);
                const state = delta > 0
                    ? "positive"
                    : delta < 0
                        ? "negative"
                        : "";

                const arrow = delta > 0
                    ? "▲"
                    : delta < 0
                        ? "▼"
                        : "—";

                return `
                    <div class="heat-cell ${state}">
                        <span class="heat-label">${item.label}</span>
                        <span class="heat-value">${compactNumber(item.current)}</span>
                        <span class="heat-delta ${deltaClass(delta)}">
                            ${arrow} ${deltaText(delta)}
                        </span>
                    </div>
                `;
            }).join("");
        }

        function renderNewsAndRisk(currentData, compareData, compareMap) {
            const sortedByRevenue = currentData
                .slice()
                .sort((a, b) => Number(b.revenue) - Number(a.revenue));

            const revenueLeader = sortedByRevenue[0];
            const rankMovers = currentData
                .map((item) => {
                    const previous = compareMap.get(item.provider);

                    return {
                        ...item,
                        movement: previous
                            ? Number(previous.rank) - Number(item.rank)
                            : 0
                    };
                })
                .sort((a, b) => Math.abs(b.movement) - Math.abs(a.movement));

            const largestMover = rankMovers[0];
            const totalRevenue = totalMetric(currentData, "revenue");
            const previousRevenue = totalMetric(compareData, "revenue");
            const revenueDelta = percentageChange(totalRevenue, previousRevenue);

            const revenueConcentration = revenueLeader && totalRevenue
                ? (Number(revenueLeader.revenue) / totalRevenue) * 100
                : 0;

            const topFiveRevenue = sortedByRevenue
                .slice(0, 5)
                .reduce((sum, item) => sum + Number(item.revenue || 0), 0);

            const topFiveConcentration = totalRevenue
                ? (topFiveRevenue / totalRevenue) * 100
                : 0;

            const upRankCount = rankMovers.filter(
                (item) => item.movement > 0
            ).length;

            const downRankCount = rankMovers.filter(
                (item) => item.movement < 0
            ).length;

            const newsItems = [
                {
                    tag: "LEADER",
                    text: revenueLeader
                        ? `<strong>${escapeHtml(revenueLeader.provider)}</strong> remains the largest ranked provider by reported revenue at <strong>${compactNumber(revenueLeader.revenue)}</strong>.`
                        : "No provider leadership data available."
                },
                {
                    tag: "MOVER",
                    text: largestMover && largestMover.movement !== 0
                        ? `<strong>${escapeHtml(largestMover.provider)}</strong> recorded the largest rank movement at <strong>${largestMover.movement > 0 ? "+" : ""}${largestMover.movement}</strong> positions.`
                        : "No rank movement can be calculated without a comparison snapshot."
                },
                {
                    tag: "BREADTH",
                    text: `<strong>${upRankCount} ADVANCING</strong> and <strong>${downRankCount} DECLINING</strong> providers across the available comparison universe.`
                }
            ];

            document.getElementById("newsFeed").innerHTML = newsItems.map((item) => `
                <div class="news-item">
                    <span class="news-tag">${item.tag}</span>
                    <span class="news-text">${item.text}</span>
                </div>
            `).join("");

            const revenueRisk = Math.min(revenueConcentration * 2.5, 100);
            const concentrationRisk = Math.min(topFiveConcentration, 100);
            const movementRisk = currentData.length
                ? Math.min(
                    ((upRankCount + downRankCount) / currentData.length) * 100,
                    100
                )
                : 0;

            document.getElementById("riskPanel").innerHTML = `
                <div class="risk-row">
                    <div class="risk-head">
                        <span>Market Leader Concentration</span>
                        <strong>${revenueConcentration.toFixed(1)}%</strong>
                    </div>
                    <div class="risk-bar">
                        <div
                            class="risk-fill warn"
                            style="width:${revenueRisk}%;"
                        ></div>
                    </div>
                </div>

                <div class="risk-row">
                    <div class="risk-head">
                        <span>Top Five Revenue Concentration</span>
                        <strong>${topFiveConcentration.toFixed(1)}%</strong>
                    </div>
                    <div class="risk-bar">
                        <div
                            class="risk-fill"
                            style="width:${concentrationRisk}%;"
                        ></div>
                    </div>
                </div>

                <div class="risk-row">
                    <div class="risk-head">
                        <span>Ranking Turnover</span>
                        <strong>${movementRisk.toFixed(1)}%</strong>
                    </div>
                    <div class="risk-bar">
                        <div
                            class="risk-fill alert"
                            style="width:${movementRisk}%;"
                        ></div>
                    </div>
                </div>
            `;

            const sentiment = revenueDelta > 0
                ? "BULLISH"
                : revenueDelta < 0
                    ? "BEARISH"
                    : "NEUTRAL";

            const sentimentClass = revenueDelta > 0
                ? "positive"
                : revenueDelta < 0
                    ? "negative"
                    : "neutral";

            const alertText = movementRisk > 50
                ? "ELEVATED RANKING TURNOVER"
                : "MONITORING ACTIVE";

            document.getElementById("footerSentiment").textContent = sentiment;
            document.getElementById("footerSentiment").className = sentimentClass;

            document.getElementById("footerAlert").textContent = alertText;
            document.getElementById("footerAlert").className =
                movementRisk > 50 ? "negative" : "neutral";

            document.getElementById("footerHealth").textContent =
                currentData.length ? "NORMAL" : "DATA LIMITED";

            document.getElementById("footerHealth").className =
                currentData.length ? "positive" : "negative";
        }

        function renderKpis(currentData, compareData) {
            const totalRevenue = totalMetric(currentData, "revenue");
            const totalTeu = totalMetric(currentData, "teu");
            const totalAir = totalMetric(currentData, "air_tons");
            const previousRevenue = totalMetric(compareData, "revenue");

            const leader = currentData
                .slice()
                .sort((a, b) => Number(b.revenue) - Number(a.revenue))[0];

            const delta = percentageChange(totalRevenue, previousRevenue);

            document.getElementById("totalRevenue").textContent =
                compactNumber(totalRevenue);

            document.getElementById("totalTeu").textContent =
                compactNumber(totalTeu);

            document.getElementById("totalAir").textContent =
                compactNumber(totalAir);

            document.getElementById("providerCount").textContent =
                String(currentData.length).padStart(2, "0");

            document.getElementById("leaderName").textContent = leader
                ? `#${String(leader.rank).padStart(2, "0")}`
                : "--";

            document.getElementById("leaderMetric").textContent = leader
                ? `${leader.provider.toUpperCase()} / ${compactNumber(leader.revenue)}`
                : "NO LEADER DATA";

            const snapshotDelta = document.getElementById("snapshotDelta");
            snapshotDelta.textContent = deltaText(delta);
            snapshotDelta.className = `kpi-value ${deltaClass(delta)}`;

            document.getElementById("snapshotDeltaSub").textContent =
                compareData.length
                    ? "Aggregate Revenue Change"
                    : "No Comparison Snapshot";
        }

        function renderRankingTable(currentData, compareMap) {
            if (!currentData.length) {
                rankingTableBody.innerHTML = `
                    <tr>
                        <td colspan="6" class="empty-state">
                            NO DATA AVAILABLE FOR THE SELECTED SNAPSHOT.
                        </td>
                    </tr>
                `;
                return;
            }

            rankingTableBody.innerHTML = currentData.map((item) => {
                const previousRecord = compareMap.get(item.provider);

                return `
                    <tr>
                        <td class="rank-cell">
                            ${String(item.rank).padStart(2, "0")}
                        </td>

                        <td>
                            <div class="provider-name">
                                ${escapeHtml(item.provider)}
                            </div>
                            <div class="provider-hq">
                                ${escapeHtml(item.hq)}
                            </div>
                        </td>

                        ${metricCell(
                            item.revenue,
                            previousRecord ? previousRecord.revenue : null
                        )}

                        ${metricCell(
                            item.teu,
                            previousRecord ? previousRecord.teu : null
                        )}

                        ${metricCell(
                            item.air_tons,
                            previousRecord ? previousRecord.air_tons : null
                        )}

                        ${rankMoveCell(item.rank, previousRecord)}
                    </tr>
                `;
            }).join("");
        }

        function updateDashboard() {
            const currentDate = currentDateSelect.value;
            const compareDate = compareDateSelect.value;

            const currentData = SNAPSHOTS[currentDate] || [];
            const compareData = compareDate
                ? (SNAPSHOTS[compareDate] || [])
                : [];

            const compareMap = new Map(
                compareData.map((item) => [item.provider, item])
            );

            document.getElementById("tableMeta").textContent = compareDate
                ? `CURRENT: ${currentDate} | VS: ${compareDate}`
                : `CURRENT: ${currentDate} | NO PRIOR SNAPSHOT SELECTED`;

            renderTicker(currentData, compareData);
            renderKpis(currentData, compareData);
            renderTrendChart();
            renderWatchlist(currentData, compareMap);
            renderHeatmap(currentData, compareData);
            renderNewsAndRisk(currentData, compareData, compareMap);
            renderRankingTable(currentData, compareMap);
        }

        currentDateSelect.addEventListener("change", () => {
            const selectedCurrentDate = currentDateSelect.value;

            if (compareDateSelect.value === selectedCurrentDate) {
                const alternativeDate = AVAILABLE_DATES.find(
                    (date) => date !== selectedCurrentDate
                );

                compareDateSelect.value = alternativeDate || "";
            }

            updateDashboard();
        });

        compareDateSelect.addEventListener("change", updateDashboard);

        populateDateSelectors();
        updateDashboard();
    </script>
</body>
</html>
"""

    final_html = (
        raw_html
        .replace("__SOURCE_URL__", html.escape(source_url, quote=True))
        .replace("__GENERATED_AT__", generated_at)
        .replace("__SNAPSHOT_JSON__", snapshot_json)
        .replace("__DATES_JSON__", dates_json)
        .replace("__LATEST_DATE__", latest_date or "")
        .replace("__PREVIOUS_DATE__", previous_date or "")
    )

    with open(STATIC_HTML_PATH, "w", encoding="utf-8") as file:
        file.write(final_html)

    log(f"Bloomberg/MI Terminal 대시보드 생성 완료: {STATIC_HTML_PATH}")

# =====================================================
# 7. Main Execution
# =====================================================
def main():
    parser = argparse.ArgumentParser(
        description="A&A Top 25 Forwarder crawler and MI Terminal dashboard generator"
    )

    parser.add_argument(
        "--build-static",
        action="store_true",
        help="기존 DB 데이터를 바탕으로 index.html만 재생성합니다."
    )

    parser.add_argument(
        "--refresh",
        action="store_true",
        help="데이터 크롤링 후 DB, CSV, 정적 HTML을 갱신합니다."
    )

    args = parser.parse_args()

    try:
        if args.build_static and not args.refresh:
            log("기존 DB 기준으로 Forwarder 정적 대시보드를 재생성합니다.")
            build_static_dashboard()
            log("정적 대시보드 재생성이 완료되었습니다.")
            return

        log("Top25 Forwarder 크롤링 및 Bloomberg/MI Terminal 대시보드 빌드를 시작합니다.")

        df = fetch_top_25_data(DEFAULT_URL)

        if df.empty:
            raise RuntimeError("크롤링 결과가 비어 있습니다.")

        save_today_data(df)
        build_static_dashboard()

        log("모든 작업이 정상적으로 완료되었습니다.")

    except Exception as error:
        print("\\n" + "=" * 60, file=sys.stderr)
        print("!!! 실행 중 치명적 오류 발생 !!!", file=sys.stderr)
        print(f"Error Type: {type(error).__name__}", file=sys.stderr)
        print(f"Error Message: {str(error)}", file=sys.stderr)
        print("=" * 60, file=sys.stderr)

        traceback.print_exc()

        print("=" * 60 + "\\n", file=sys.stderr)

        sys.exit(1)

if __name__ == "__main__":
    main()
