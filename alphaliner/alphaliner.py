# -*- coding: utf-8 -*-
"""
Alphaliner TOP 100 Liner Operators Static Dashboard Crawler (GitHub Optimized)
- Direct ExtJS Store extraction (Top100description)
- Official Global Figures + TOP 100 Capacity Separation
- Historical snapshot comparison & Full Time-travel Delta analysis
- Authentic Alphaliner 2-tier Grouped Table & UTF-8 BOM CSV Export
- Bloomberg MI Terminal Look & Feel (Aligned with Shipping Index / BunkerPrice)
- Dynamic SQLite Schema Migration & Strict Pre-persist Quality Gate

Output files:
- alphaliner_history.db
- alphaliner_data.json
- index.html
"""

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from threading import Lock

from flask import Flask, send_from_directory
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait

try:
    from webdriver_manager.chrome import ChromeDriverManager
    HAS_WEBDRIVER_MANAGER = True
except Exception:
    HAS_WEBDRIVER_MANAGER = False

# =========================================================
# 1. Configuration & Constants
# =========================================================
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "alphaliner_history.db"
JSON_PATH = BASE_DIR / "alphaliner_data.json"
HTML_PATH = BASE_DIR / "index.html"

TARGET_URL = "https://alphaliner.axsmarine.com/PublicTop100/"

# ---------------------------------------------------------
# Official Alphaliner Global Figures
# ---------------------------------------------------------
# 중요:
# TOP 100 operator table의 합계는 global fleet 전체가 아닙니다.
# 따라서 Total Ships KPI에는 아래 공식 Global Active Ships 값을 사용합니다.
OFFICIAL_GLOBAL_SHIPS = 7672

# Alphaliner Official Global Figure 기준 Top 10 carrier concentration
OFFICIAL_TOP10_SHARE = 85.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("Alphaliner")

app = Flask(__name__, static_folder=str(BASE_DIR))
db_lock = Lock()

# =========================================================
# 2. Database Layer & Auto-Migration
# =========================================================
def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_table_columns(conn, table_name: str) -> set:
    try:
        cur = conn.cursor()
        cur.execute(f"PRAGMA table_info({table_name})")
        return {row["name"] for row in cur.fetchall()}
    except Exception:
        return set()

def init_db():
    with db_lock:
        with get_db_connection() as conn:
            cur = conn.cursor()

            cur.execute("""
                CREATE TABLE IF NOT EXISTS snapshot_meta (
                    snapshot_date TEXT PRIMARY KEY,
                    global_teu INTEGER,
                    global_ships INTEGER,
                    top100_teu INTEGER,
                    top100_ships INTEGER,
                    top10_share REAL,
                    orderbook_teu INTEGER,
                    extracted_at TEXT,
                    source_url TEXT,
                    raw_hash TEXT,
                    notes TEXT,
                    updated_at TEXT
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS operator_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_date TEXT,
                    rank INTEGER,
                    operator_name TEXT,
                    total_teu INTEGER,
                    total_ships INTEGER,
                    market_share REAL,
                    owned_teu INTEGER,
                    owned_ships INTEGER,
                    owned_share REAL,
                    chartered_teu INTEGER,
                    chartered_ships INTEGER,
                    chartered_share REAL,
                    orderbook_teu INTEGER,
                    orderbook_ships INTEGER,
                    orderbook_share REAL,
                    country TEXT,
                    created_at TEXT,
                    UNIQUE(snapshot_date, rank)
                )
            """)

            op_cols = get_table_columns(conn, "operator_snapshots")

            if op_cols:
                if "operator_name" not in op_cols and "operator" in op_cols:
                    try:
                        cur.execute(
                            "ALTER TABLE operator_snapshots ADD COLUMN operator_name TEXT"
                        )
                        cur.execute("""
                            UPDATE operator_snapshots
                            SET operator_name = operator
                            WHERE operator_name IS NULL
                        """)
                        logger.info(
                            "Migrated schema: Added operator_name column."
                        )
                    except Exception as e:
                        logger.warning(
                            f"Schema migration warning (operator_name): {e}"
                        )

                if "operator" not in op_cols and "operator_name" in op_cols:
                    try:
                        cur.execute(
                            "ALTER TABLE operator_snapshots ADD COLUMN operator TEXT"
                        )
                        cur.execute("""
                            UPDATE operator_snapshots
                            SET operator = operator_name
                            WHERE operator IS NULL
                        """)
                        logger.info("Migrated schema: Added operator column.")
                    except Exception as e:
                        logger.warning(
                            f"Schema migration warning (operator): {e}"
                        )

            conn.commit()

# =========================================================
# 3. Selenium Extractor
# =========================================================
def build_chrome_options():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(
        "--user-agent=Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    chrome_bin = (
        os.environ.get("CHROME_BINARY")
        or os.environ.get("GOOGLE_CHROME_BIN")
    )

    if chrome_bin and os.path.exists(chrome_bin):
        options.binary_location = chrome_bin

    return options

def get_driver():
    options = build_chrome_options()
    chromedriver_bin = os.environ.get("CHROMEDRIVER_PATH")

    if chromedriver_bin and os.path.exists(chromedriver_bin):
        service = Service(executable_path=chromedriver_bin)
        return webdriver.Chrome(service=service, options=options)

    if HAS_WEBDRIVER_MANAGER:
        try:
            return webdriver.Chrome(
                service=Service(ChromeDriverManager().install()),
                options=options
            )
        except Exception:
            pass

    return webdriver.Chrome(options=options)

def fetch_alphaliner_data():
    logger.info(f"Connecting to target: {TARGET_URL}")
    driver = None

    try:
        driver = get_driver()
        driver.set_page_load_timeout(60)
        driver.get(TARGET_URL)

        WebDriverWait(driver, 45).until(
            lambda d: d.execute_script("""
                return typeof Ext !== 'undefined' &&
                       Ext.data &&
                       Ext.data.StoreManager &&
                       Ext.data.StoreManager.lookup('Top100description') &&
                       Ext.data.StoreManager
                           .lookup('Top100description')
                           .getCount() > 0;
            """)
        )

        time.sleep(2)

        records = driver.execute_script("""
            var store = Ext.data.StoreManager.lookup('Top100description');
            if (!store) return [];

            var data = [];

            store.each(function(rec) {
                data.push(rec.data);
            });

            return data;
        """)

        logger.info(
            f"Extracted {len(records)} raw records from ExtJS Store."
        )

        return records

    except Exception as e:
        logger.error(f"Failed to fetch data from Alphaliner: {e}")
        raise

    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

# =========================================================
# 4. Parsing, Cleaning & Quality Gate
# =========================================================
def clean_int(val, default=0):
    if val is None:
        return default

    try:
        if isinstance(val, (int, float)):
            return int(val)

        s = str(val).replace(",", "").strip()

        if not s or s == "-":
            return default

        return int(float(s))

    except Exception:
        return default

def clean_float(val, default=0.0):
    if val is None:
        return default

    try:
        if isinstance(val, (int, float)):
            return float(val)

        s = str(val).replace(",", "").replace("%", "").strip()

        if not s or s == "-":
            return default

        return float(s)

    except Exception:
        return default

def parse_records(raw_records):
    parsed = []

    for rank_idx, r in enumerate(raw_records, start=1):
        operator = (
            r.get("operator")
            or r.get("comp_name")
            or r.get("comp_cle_oper")
            or f"Operator {rank_idx}"
        )

        owned_teu = clean_int(
            r.get("ownedTeu") or r.get("owned_teu")
        )
        owned_ships = clean_int(
            r.get("ownedShips") or r.get("owned_ships")
        )

        chartered_teu = clean_int(
            r.get("chartTeu") or r.get("chartered_teu")
        )
        chartered_ships = clean_int(
            r.get("chartShips") or r.get("chartered_ships")
        )

        total_teu = clean_int(
            r.get("totalTeu") or r.get("total_teu")
        )

        if total_teu == 0 and (owned_teu > 0 or chartered_teu > 0):
            total_teu = owned_teu + chartered_teu

        total_ships = clean_int(
            r.get("totalShips") or r.get("total_ships")
        )

        if total_ships == 0 and (owned_ships > 0 or chartered_ships > 0):
            total_ships = owned_ships + chartered_ships

        market_share = clean_float(
            r.get("percent") or r.get("market_share")
        )

        owned_share = clean_float(
            r.get("ownedProcents") or r.get("owned_share")
        )

        if owned_share == 0.0 and total_teu > 0:
            owned_share = round(
                (owned_teu / total_teu) * 100,
                2
            )

        chartered_share = clean_float(
            r.get("chartProcent") or r.get("chartered_share")
        )

        if chartered_share == 0.0 and total_teu > 0:
            chartered_share = round(
                (chartered_teu / total_teu) * 100,
                2
            )

        order_teu = clean_int(
            r.get("orderTeu") or r.get("orderbook_teu")
        )
        order_ships = clean_int(
            r.get("orderShips") or r.get("orderbook_ships")
        )
        order_share = clean_float(
            r.get("orderProcents") or r.get("orderbook_share")
        )

        country = r.get("carrier_flag", "")

        if country and "/" in country:
            country = country.split("/")[-1].replace(".png", "").upper()

        parsed.append({
            "rank": rank_idx,
            "operator_name": str(operator).strip(),
            "operator": str(operator).strip(),
            "total_teu": total_teu,
            "total_ships": total_ships,
            "market_share": market_share,
            "owned_teu": owned_teu,
            "owned_ships": owned_ships,
            "owned_share": owned_share,
            "chartered_teu": chartered_teu,
            "chartered_ships": chartered_ships,
            "chartered_share": chartered_share,
            "orderbook_teu": order_teu,
            "orderbook_ships": order_ships,
            "orderbook_share": order_share,
            "country": country
        })

    return parsed

def validate_candidate_metrics(parsed_ops):
    if len(parsed_ops) < 90:
        return (
            False,
            f"Operator count too low: {len(parsed_ops)} "
            "(minimum 90 required)"
        )

    total_teu = sum(o["total_teu"] for o in parsed_ops)
    total_ships = sum(o["total_ships"] for o in parsed_ops)

    if total_teu < 20_000_000:
        return (
            False,
            f"Total TEU too low: {total_teu:,} "
            "(minimum 20,000,000 required)"
        )

    if total_ships < 3_000:
        return (
            False,
            f"Total Ships too low: {total_ships:,} "
            "(minimum 3,000 required)"
        )

    return True, "Quality validation passed."

# =========================================================
# 5. Database Persistence
# =========================================================
def persist_snapshot(snapshot_date, parsed_ops, raw_records):
    raw_json_str = json.dumps(raw_records, sort_keys=True)
    raw_hash = hashlib.sha256(
        raw_json_str.encode("utf-8")
    ).hexdigest()

    now_str = datetime.now().isoformat()

    top100_teu = sum(o["total_teu"] for o in parsed_ops)
    top100_ships = sum(o["total_ships"] for o in parsed_ops)

    top10_teu = sum(
        o["total_teu"]
        for o in parsed_ops[:10]
    )

    top10_share = round(
        (top10_teu / top100_teu * 100),
        2
    ) if top100_teu > 0 else 0.0

    orderbook_teu = sum(
        o["orderbook_teu"]
        for o in parsed_ops
    )

    with db_lock:
        with get_db_connection() as conn:
            cur = conn.cursor()

            cur.execute("""
                INSERT INTO snapshot_meta (
                    snapshot_date,
                    global_teu,
                    global_ships,
                    top100_teu,
                    top100_ships,
                    top10_share,
                    orderbook_teu,
                    extracted_at,
                    source_url,
                    raw_hash,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

                ON CONFLICT(snapshot_date) DO UPDATE SET
                    global_teu = excluded.global_teu,
                    global_ships = excluded.global_ships,
                    top100_teu = excluded.top100_teu,
                    top100_ships = excluded.top100_ships,
                    top10_share = excluded.top10_share,
                    orderbook_teu = excluded.orderbook_teu,
                    extracted_at = excluded.extracted_at,
                    source_url = excluded.source_url,
                    raw_hash = excluded.raw_hash,
                    updated_at = excluded.updated_at
            """, (
                snapshot_date,

                # global_teu는 별도 공식 global figure가 수집되지 않는 한
                # 기존 TOP100 기준 값을 유지
                top100_teu,

                # 핵심 수정:
                # TOP100 ships 합계가 아니라 공식 Global Active Ships 사용
                OFFICIAL_GLOBAL_SHIPS,

                top100_teu,
                top100_ships,
                top10_share,
                orderbook_teu,
                now_str,
                TARGET_URL,
                raw_hash,
                now_str
            ))

            cur.execute("""
                DELETE FROM operator_snapshots
                WHERE snapshot_date = ?
            """, (snapshot_date,))

            op_cols = get_table_columns(
                conn,
                "operator_snapshots"
            )

            cols = [
                "snapshot_date",
                "rank",
                "total_teu",
                "total_ships",
                "market_share",
                "owned_teu",
                "owned_ships",
                "owned_share",
                "chartered_teu",
                "chartered_ships",
                "chartered_share",
                "orderbook_teu",
                "orderbook_ships",
                "orderbook_share",
                "country",
                "created_at"
            ]

            if "operator_name" in op_cols:
                cols.append("operator_name")

            if "operator" in op_cols:
                cols.append("operator")

            placeholders = ", ".join(["?"] * len(cols))
            col_names = ", ".join(cols)

            insert_sql = f"""
                INSERT INTO operator_snapshots ({col_names})
                VALUES ({placeholders})
            """

            rows_to_insert = []

            for op in parsed_ops:
                row = []

                for c in cols:
                    if c == "snapshot_date":
                        row.append(snapshot_date)

                    elif c == "created_at":
                        row.append(now_str)

                    elif c in ("operator_name", "operator"):
                        row.append(op["operator_name"])

                    else:
                        row.append(op.get(c))

                rows_to_insert.append(tuple(row))

            cur.executemany(insert_sql, rows_to_insert)
            conn.commit()

            logger.info(
                f"Successfully persisted snapshot {snapshot_date} "
                f"({len(parsed_ops)} operators)."
            )

def process_and_persist():
    raw_records = fetch_alphaliner_data()
    parsed_ops = parse_records(raw_records)

    top100_teu = sum(o["total_teu"] for o in parsed_ops)
    top100_ships = sum(o["total_ships"] for o in parsed_ops)

    top10_teu = sum(
        o["total_teu"]
        for o in parsed_ops[:10]
    )

    top10_share = round(
        (top10_teu / top100_teu * 100),
        2
    ) if top100_teu > 0 else 0.0

    orderbook_teu = sum(
        o["orderbook_teu"]
        for o in parsed_ops
    )

    logger.info(
        f"Parsed candidate snapshot | "
        f"Count: {len(parsed_ops)} | "
        f"TOP100 TEU: {top100_teu:,} | "
        f"TOP100 Ships: {top100_ships:,} | "
        f"Official Global Ships: {OFFICIAL_GLOBAL_SHIPS:,} | "
        f"Top10 Recalculated Share: {top10_share}% | "
        f"Official Top10 Share: {OFFICIAL_TOP10_SHARE:.1f}% | "
        f"Orderbook TEU: {orderbook_teu:,}"
    )

    is_valid, message = validate_candidate_metrics(parsed_ops)

    if not is_valid:
        logger.error(
            f"Quality gate rejected data: {message}. "
            "Snapshot aborted."
        )
        return False

    today_str = datetime.now().strftime("%Y-%m-%d")

    persist_snapshot(
        today_str,
        parsed_ops,
        raw_records
    )

    return True

# =========================================================
# 6. JSON Export & Static Dashboard Generator
# =========================================================
def export_json_and_html():
    with get_db_connection() as conn:
        cur = conn.cursor()

        meta_rows = cur.execute("""
            SELECT *
            FROM snapshot_meta
            WHERE top100_teu >= 20000000
              AND top100_ships >= 3000
            ORDER BY snapshot_date DESC
        """).fetchall()

        if not meta_rows:
            logger.warning(
                "No valid snapshots available in DB for JSON export."
            )
            return

        dates = [r["snapshot_date"] for r in meta_rows]
        snapshots = {}

        op_cols = get_table_columns(
            conn,
            "operator_snapshots"
        )

        name_field = (
            "operator_name"
            if "operator_name" in op_cols
            else "operator"
        )

        for m in meta_rows:
            d = m["snapshot_date"]

            cur.execute(f"""
                SELECT
                    rank,
                    {name_field} AS operator_name,
                    total_teu,
                    total_ships,
                    market_share,
                    owned_teu,
                    owned_ships,
                    owned_share,
                    chartered_teu,
                    chartered_ships,
                    chartered_share,
                    orderbook_teu,
                    orderbook_ships,
                    orderbook_share,
                    country
                FROM operator_snapshots
                WHERE snapshot_date = ?
                ORDER BY rank ASC
            """, (d,))

            ops = [dict(row) for row in cur.fetchall()]

            snapshots[d] = {
                "meta": {
                    "snapshot_date": d,

                    # 중요:
                    # 기존 DB의 global_ships가 TOP100 합계로 잘못 저장되어 있더라도
                    # JSON/dashboard에는 공식 global active ships 7,672를 강제 적용
                    "global_ships": OFFICIAL_GLOBAL_SHIPS,

                    "global_teu": (
                        m["global_teu"]
                        or m["top100_teu"]
                    ),

                    "top100_teu": m["top100_teu"],
                    "top100_ships": m["top100_ships"],

                    # TOP100 상위 10개 TEU / TOP100 전체 TEU 재계산 값
                    "top10_share": m["top10_share"],

                    # Alphaliner official global concentration figure
                    "official_top10_share": OFFICIAL_TOP10_SHARE,

                    "orderbook_teu": m["orderbook_teu"],
                    "extracted_at": m["extracted_at"]
                },
                "operators": ops
            }

    data = {
        "generated_at": datetime.now().isoformat(),
        "dates": dates,
        "latest_date": dates[0] if dates else None,
        "snapshots": snapshots
    }

    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            indent=2,
            ensure_ascii=False
        )

    logger.info(f"Serialized JSON data to {JSON_PATH}")

    generate_static_html()

def generate_static_html():
    html_content = """<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Alphaliner TOP 100 Liner Operators | MI Terminal</title>

  <style>
    :root {
      --bg-base: #0a0e14;
      --bg-panel: #111822;
      --bg-card: #151d28;
      --bg-hover: #1c2636;
      --border-subtle: #212e3f;
      --border-accent: #33445c;
      --text-main: #e2e8f0;
      --text-muted: #8292a2;
      --text-dim: #556577;
      --accent-blue: #3b82f6;
      --accent-cyan: #06b6d4;
      --accent-green: #10b981;
      --accent-red: #ef4444;
      --accent-amber: #f59e0b;
      --mono-font: "SF Pro Text", -apple-system, BlinkMacSystemFont,
                   "Segoe UI", Roboto, "Helvetica Neue", monospace;
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }

    body {
      background-color: var(--bg-base);
      color: var(--text-main);
      font-family: var(--mono-font);
      font-size: 12px;
      line-height: 1.4;
      padding: 16px;
      -webkit-font-smoothing: antialiased;
    }

    .terminal-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: var(--bg-panel);
      border: 1px solid var(--border-subtle);
      border-radius: 4px;
      padding: 12px 18px;
      margin-bottom: 14px;
    }

    .terminal-title {
      font-size: 15px;
      font-weight: 700;
      letter-spacing: 0.8px;
      color: #ffffff;
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .terminal-badge {
      font-size: 10px;
      background: rgba(59, 130, 246, 0.15);
      color: var(--accent-blue);
      border: 1px solid rgba(59, 130, 246, 0.3);
      padding: 2px 6px;
      border-radius: 3px;
      font-weight: 600;
      text-transform: uppercase;
    }

    .terminal-meta {
      font-size: 11px;
      color: var(--text-muted);
      font-family: monospace;
    }

    .kpi-grid {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 12px;
      margin-bottom: 14px;
    }

    .kpi-card {
      background: var(--bg-panel);
      border: 1px solid var(--border-subtle);
      border-radius: 4px;
      padding: 12px 16px;
      position: relative;
      overflow: hidden;
      min-height: 104px;
    }

    .kpi-card::before {
      content: "";
      position: absolute;
      top: 0;
      left: 0;
      width: 3px;
      height: 100%;
      background: var(--border-accent);
    }

    .kpi-card.kpi-blue::before {
      background: var(--accent-blue);
    }

    .kpi-card.kpi-cyan::before {
      background: var(--accent-cyan);
    }

    .kpi-card.kpi-amber::before {
      background: var(--accent-amber);
    }

    .kpi-card.kpi-green::before {
      background: var(--accent-green);
    }

    .kpi-label {
      font-size: 10.5px;
      font-weight: 600;
      text-transform: uppercase;
      color: var(--text-muted);
      margin-bottom: 6px;
      letter-spacing: 0.5px;
    }

    .kpi-value {
      font-size: 22px;
      font-weight: 700;
      color: #ffffff;
      font-family: monospace;
      letter-spacing: -0.5px;
    }

    /* Top 10 카드 내부의 Official / Recalculation 2-line layout */
    .kpi-value-dual {
      display: flex;
      flex-direction: column;
      gap: 4px;
      min-height: 47px;
      justify-content: center;
    }

    .top10-metric-line {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 8px;
      white-space: nowrap;
    }

    .top10-metric-label {
      font-size: 8.5px;
      line-height: 1.15;
      color: var(--text-muted);
      font-weight: 700;
      letter-spacing: 0.25px;
    }

    .top10-metric-value {
      font-size: 17px;
      line-height: 1;
      color: #ffffff;
      font-weight: 700;
      font-family: monospace;
      letter-spacing: -0.4px;
    }

    .top10-metric-value.official {
      color: var(--accent-amber);
    }

    .top10-metric-value.recalc {
      color: var(--accent-cyan);
    }

    .kpi-sub {
      display: flex;
      align-items: center;
      gap: 6px;
      margin-top: 4px;
      font-size: 11px;
      font-weight: 600;
      font-family: monospace;
    }

    .delta-up {
      color: var(--accent-green);
    }

    .delta-down {
      color: var(--accent-red);
    }

    .delta-flat {
      color: var(--text-dim);
    }

    .controls-panel {
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: var(--bg-panel);
      border: 1px solid var(--border-subtle);
      border-radius: 4px;
      padding: 10px 14px;
      margin-bottom: 12px;
      gap: 12px;
    }

    .controls-group {
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .control-label {
      font-size: 11px;
      color: var(--text-muted);
      text-transform: uppercase;
      font-weight: 600;
    }

    select,
    input,
    .btn {
      background: var(--bg-card);
      border: 1px solid var(--border-accent);
      color: var(--text-main);
      padding: 6px 10px;
      border-radius: 3px;
      font-family: var(--mono-font);
      font-size: 11.5px;
      outline: none;
      transition: border-color 0.2s;
    }

    select:focus,
    input:focus {
      border-color: var(--accent-blue);
    }

    .btn {
      cursor: pointer;
      font-weight: 600;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: #1e293b;
    }

    .btn:hover {
      background: #334155;
      border-color: var(--text-muted);
      color: #ffffff;
    }

    .table-viewport {
      background: var(--bg-panel);
      border: 1px solid var(--border-subtle);
      border-radius: 4px;
      overflow: auto;
      max-height: calc(100vh - 290px);
    }

    table {
      width: 100%;
      border-collapse: collapse;
      text-align: right;
      font-size: 11.5px;
      white-space: nowrap;
    }

    thead th {
      background: #131c28;
      color: var(--text-muted);
      font-weight: 600;
      text-transform: uppercase;
      font-size: 10px;
      letter-spacing: 0.5px;
      padding: 6px 10px;
      border-bottom: 1px solid var(--border-subtle);
      border-right: 1px solid var(--border-subtle);
      position: sticky;
      top: 0;
      z-index: 10;
      user-select: none;
      cursor: pointer;
    }

    thead tr:first-child th {
      text-align: center;
      background: #172232;
      color: #cbd5e1;
      font-size: 10.5px;
      font-weight: 700;
      border-bottom: 1px solid #2d3f58;
    }

    thead tr:nth-child(2) th {
      top: 27px;
    }

    th:hover {
      color: #ffffff;
      background: #1e2c40;
    }

    th.sort-active {
      color: var(--accent-blue);
      border-bottom: 2px solid var(--accent-blue);
    }

    tbody tr {
      border-bottom: 1px solid rgba(33, 46, 63, 0.6);
      transition: background 0.15s;
    }

    tbody tr:hover {
      background: var(--bg-hover) !important;
    }

    tbody tr:nth-child(even) {
      background: rgba(21, 29, 40, 0.4);
    }

    tbody td {
      padding: 6px 10px;
      border-right: 1px solid rgba(33, 46, 63, 0.4);
      font-family: monospace;
      color: #cbd5e1;
    }

    .col-center {
      text-align: center;
    }

    .col-left {
      text-align: left;
    }

    .rank-cell {
      font-weight: 700;
      color: var(--text-muted);
      width: 44px;
    }

    .op-cell {
      font-family: var(--mono-font);
      font-weight: 600;
      color: #ffffff;
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .flag-icon {
      width: 16px;
      height: 11px;
      border-radius: 1px;
      object-fit: cover;
      display: inline-block;
      vertical-align: middle;
      background: #2b384c;
    }

    .flag-fallback {
      font-size: 9px;
      font-weight: 700;
      color: var(--text-dim);
      background: #1c2636;
      padding: 1px 3px;
      border-radius: 2px;
      border: 1px solid #2d3b4e;
    }

    .total-col {
      font-weight: 700;
      color: #f8fafc;
    }

    .share-col {
      color: var(--accent-cyan);
      font-weight: 600;
    }

    @media (max-width: 1100px) {
      .kpi-grid {
        grid-template-columns: repeat(2, 1fr);
      }

      .controls-panel {
        flex-direction: column;
        align-items: flex-start;
      }
    }
  </style>
</head>

<body>
  <div class="terminal-header">
    <div class="terminal-title">
      <span>ALPHALINER TOP 100 LINER OPERATORS</span>
      <span class="terminal-badge">GLOBAL FLEET & ORDERBOOK</span>
    </div>
    <div class="terminal-meta" id="header-meta">INITIALIZING FEED...</div>
  </div>

  <div class="kpi-grid">
    <div class="kpi-card kpi-blue">
      <div class="kpi-label">1. Total Ships</div>
      <div class="kpi-value" id="kpi-ships">-</div>
      <div class="kpi-sub" id="delta-ships">
        <span class="delta-flat">-</span>
      </div>
    </div>

    <div class="kpi-card kpi-cyan">
      <div class="kpi-label">2. Total Fleet TEU</div>
      <div class="kpi-value" id="kpi-teu">-</div>
      <div class="kpi-sub" id="delta-teu">
        <span class="delta-flat">-</span>
      </div>
    </div>

    <div class="kpi-card kpi-amber">
      <div class="kpi-label">3. Top 10 Concentration</div>

      <div class="kpi-value-dual" id="kpi-top10">
        <div class="top10-metric-line">
          <span class="top10-metric-label">OFFICIAL GLOBAL FIGURE</span>
          <span class="top10-metric-value official">-</span>
        </div>
        <div class="top10-metric-line">
          <span class="top10-metric-label">TOP 100 DATA RECALCULATION</span>
          <span class="top10-metric-value recalc">-</span>
        </div>
      </div>

      <div class="kpi-sub" id="delta-top10">
        <span class="delta-flat">-</span>
      </div>
    </div>

    <div class="kpi-card kpi-green">
      <div class="kpi-label">4. Orderbook TEU</div>
      <div class="kpi-value" id="kpi-orderbook">-</div>
      <div class="kpi-sub" id="delta-orderbook">
        <span class="delta-flat">-</span>
      </div>
    </div>
  </div>

  <div class="controls-panel">
    <div class="controls-group">
      <span class="control-label">Snapshot:</span>
      <select id="sel-date"></select>

      <span class="control-label" style="margin-left: 10px;">
        Compare Vs:
      </span>
      <select id="sel-compare"></select>
    </div>

    <div class="controls-group">
      <input
        type="text"
        id="filter-input"
        placeholder="Filter operator name / country..."
        style="width: 220px;"
      >

      <button class="btn" id="btn-export">
        <svg width="12" height="12" fill="currentColor" viewBox="0 0 16 16">
          <path d="M.5 9.9a.5.5 0 0 1 .5.5v2.5a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-2.5a.5.5 0 0 1 1 0v2.5a2 2 0 0 1-2 2H2a2 2 0 0 1-2-2v-2.5a.5.5 0 0 1 .5-.5z"/>
          <path d="M7.646 11.854a.5.5 0 0 0 .708 0l3-3a.5.5 0 0 0-.708-.708L8.5 10.293V1.5a.5.5 0 0 0-1 0v8.793L5.354 8.146a.5.5 0 1 0-.708.708l3 3z"/>
        </svg>
        EXPORT CSV (BOM)
      </button>
    </div>
  </div>

  <div class="table-viewport">
    <table id="main-table">
      <thead>
        <tr>
          <th colspan="2">OPERATOR PROFILE</th>
          <th colspan="3">TOTAL CAPACITY</th>
          <th colspan="3">OWNED FLEET</th>
          <th colspan="3">CHARTERED FLEET</th>
          <th colspan="3">ORDERBOOK</th>
        </tr>

        <tr>
          <th class="col-center" data-col="rank" style="width: 44px;">#</th>
          <th class="col-left" data-col="operator_name">OPERATOR</th>
          <th data-col="total_teu">TEU</th>
          <th data-col="total_ships">SHIPS</th>
          <th data-col="market_share">SHARE</th>
          <th data-col="owned_teu">TEU</th>
          <th data-col="owned_ships">SHIPS</th>
          <th data-col="owned_share">%</th>
          <th data-col="chartered_teu">TEU</th>
          <th data-col="chartered_ships">SHIPS</th>
          <th data-col="chartered_share">%</th>
          <th data-col="orderbook_teu">TEU</th>
          <th data-col="orderbook_ships">SHIPS</th>
          <th data-col="orderbook_share">%</th>
        </tr>
      </thead>

      <tbody id="table-body"></tbody>
    </table>
  </div>

  <script>
    let rawPayload = null;

    let currentSort = {
      col: "rank",
      asc: true
    };

    async function initTerminal() {
      try {
        const res = await fetch(
          "alphaliner_data.json?t=" + Date.now()
        );

        rawPayload = await res.json();

        populateDateDropdowns();
        bindEvents();
        refreshDashboard();

      } catch (err) {
        console.error("Fatal: failed to load feed:", err);

        document.getElementById("header-meta").innerText =
          "FEED ERROR: Check alphaliner_data.json";
      }
    }

    function populateDateDropdowns() {
      const selDate = document.getElementById("sel-date");
      const selCompare = document.getElementById("sel-compare");

      selDate.innerHTML = "";
      selCompare.innerHTML =
        '<option value="">None (Baseline)</option>';

      if (!rawPayload.dates || rawPayload.dates.length === 0) {
        return;
      }

      rawPayload.dates.forEach((d, idx) => {
        selDate.innerHTML +=
          `<option value="${d}">${d}</option>`;

        selCompare.innerHTML +=
          `<option value="${d}" ${idx === 1 ? "selected" : ""}>${d}</option>`;
      });
    }

    function bindEvents() {
      document
        .getElementById("sel-date")
        .addEventListener("change", refreshDashboard);

      document
        .getElementById("sel-compare")
        .addEventListener("change", updateKpisOnly);

      document
        .getElementById("filter-input")
        .addEventListener("input", renderTableRows);

      document
        .getElementById("btn-export")
        .addEventListener("click", exportToCsv);

      document
        .querySelectorAll("thead tr:nth-child(2) th")
        .forEach(th => {
          th.addEventListener("click", () => {
            const col = th.getAttribute("data-col");

            if (!col) {
              return;
            }

            if (currentSort.col === col) {
              currentSort.asc = !currentSort.asc;
            } else {
              currentSort.col = col;
              currentSort.asc =
                (col === "operator_name" || col === "rank");
            }

            updateSortHeaderUI();
            renderTableRows();
          });
        });
    }

    function updateSortHeaderUI() {
      document
        .querySelectorAll("thead tr:nth-child(2) th")
        .forEach(th => {
          th.classList.remove("sort-active");

          if (th.getAttribute("data-col") === currentSort.col) {
            th.classList.add("sort-active");
          }
        });
    }

    function formatNumber(num) {
      if (num === null || num === undefined) {
        return "-";
      }

      return Number(num).toLocaleString("en-US");
    }

    function calculateDeltaBadge(curr, prev, isPercent = false) {
      if (
        prev === null ||
        prev === undefined ||
        curr === null ||
        curr === undefined
      ) {
        return '<span class="delta-flat">-</span>';
      }

      const diff = curr - prev;

      if (Math.abs(diff) < 0.0001) {
        return '<span class="delta-flat">0.0 (0.0%)</span>';
      }

      const isUp = diff > 0;
      const sign = isUp ? "+" : "";
      const cls = isUp ? "delta-up" : "delta-down";

      const pct = prev > 0
        ? ((diff / prev) * 100).toFixed(2)
        : "0.00";

      const valStr = isPercent
        ? `${sign}${diff.toFixed(2)}%`
        : `${sign}${formatNumber(diff)}`;

      return `<span class="${cls}">${valStr} (${sign}${pct}%)</span>`;
    }

    function updateKpisOnly() {
      const curDate =
        document.getElementById("sel-date").value;

      const compDate =
        document.getElementById("sel-compare").value;

      const curSnap =
        rawPayload.snapshots[curDate];

      const compSnap =
        compDate
          ? rawPayload.snapshots[compDate]
          : null;

      if (!curSnap) {
        return;
      }

      const m = curSnap.meta;
      const cm = compSnap ? compSnap.meta : null;

      // ---------------------------------------------------
      // 1. TOTAL SHIPS
      // 공식 Global Active Ships 값 (7,672)을 표시
      // TOP 100 합산 ships 값은 사용하지 않음
      // ---------------------------------------------------
      document.getElementById("kpi-ships").innerText =
        formatNumber(m.global_ships);

      document.getElementById("delta-ships").innerHTML =
        calculateDeltaBadge(
          m.global_ships,
          cm ? cm.global_ships : null
        );

      // ---------------------------------------------------
      // 2. TOTAL FLEET TEU
      // 현재 기존 dashboard 기준을 유지: TOP 100 합산 TEU
      // ---------------------------------------------------
      document.getElementById("kpi-teu").innerText =
        formatNumber(m.top100_teu);

      document.getElementById("delta-teu").innerHTML =
        calculateDeltaBadge(
          m.top100_teu,
          cm ? cm.top100_teu : null
        );

      // ---------------------------------------------------
      // 3. TOP 10 CONCENTRATION
      // Official Global Figure: 85.0%
      // TOP 100 Data Recalculation: e.g. 86.41%
      // ---------------------------------------------------
      const officialShare =
        Number(m.official_top10_share || 85.0);

      const recalculatedShare =
        Number(m.top10_share || 0);

      document.getElementById("kpi-top10").innerHTML = `
        <div class="top10-metric-line">
          <span class="top10-metric-label">
            OFFICIAL GLOBAL FIGURE
          </span>
          <span class="top10-metric-value official">
            ${officialShare.toFixed(1)}%
          </span>
        </div>

        <div class="top10-metric-line">
          <span class="top10-metric-label">
            TOP 100 DATA RECALCULATION
          </span>
          <span class="top10-metric-value recalc">
            ${recalculatedShare.toFixed(2)}%
          </span>
        </div>
      `;

      // Official figure는 고정값이므로,
      // 비교 변화율은 TOP100 recalculation 기준으로 표시
      document.getElementById("delta-top10").innerHTML =
        calculateDeltaBadge(
          recalculatedShare,
          cm ? cm.top10_share : null,
          true
        );

      // ---------------------------------------------------
      // 4. ORDERBOOK
      // ---------------------------------------------------
      document.getElementById("kpi-orderbook").innerText =
        formatNumber(m.orderbook_teu);

      document.getElementById("delta-orderbook").innerHTML =
        calculateDeltaBadge(
          m.orderbook_teu,
          cm ? cm.orderbook_teu : null
        );
    }

    function refreshDashboard() {
      const curDate =
        document.getElementById("sel-date").value;

      if (
        !rawPayload ||
        !rawPayload.snapshots[curDate]
      ) {
        return;
      }

      const genTime = rawPayload.generated_at
        ? rawPayload.generated_at
            .replace("T", " ")
            .substring(0, 19) + " UTC"
        : "";

      document.getElementById("header-meta").innerText =
        `FEED UPDATED: ${genTime}`;

      updateKpisOnly();
      updateSortHeaderUI();
      renderTableRows();
    }

    function renderFlag(countryCode) {
      if (
        !countryCode ||
        countryCode === "" ||
        countryCode === "UNKNOWN"
      ) {
        return '<span class="flag-fallback">N/A</span>';
      }

      const cc = countryCode.toLowerCase();

      return `
        <img
          class="flag-icon"
          src="https://flagcdn.com/20x15/${cc}.png"
          onerror="this.outerHTML='<span class=\\\\'flag-fallback\\\\'>${countryCode}</span>'"
          alt="${countryCode}"
        >
      `;
    }

    function renderTableRows() {
      const curDate =
        document.getElementById("sel-date").value;

      const curSnap =
        rawPayload.snapshots[curDate];

      if (!curSnap || !curSnap.operators) {
        return;
      }

      const query =
        document.getElementById("filter-input")
          .value
          .toLowerCase()
          .trim();

      let list = curSnap.operators.filter(op => {
        const name =
          (op.operator_name || op.operator || "")
            .toLowerCase();

        const cntry =
          (op.country || "")
            .toLowerCase();

        return name.includes(query) || cntry.includes(query);
      });

      list.sort((a, b) => {
        let va = a[currentSort.col];
        let vb = b[currentSort.col];

        if (currentSort.col === "operator_name") {
          va =
            (a.operator_name || a.operator || "")
              .toLowerCase();

          vb =
            (b.operator_name || b.operator || "")
              .toLowerCase();
        }

        if (va === null || va === undefined) {
          va = (typeof vb === "string") ? "" : -1;
        }

        if (vb === null || vb === undefined) {
          vb = (typeof va === "string") ? "" : -1;
        }

        if (va < vb) {
          return currentSort.asc ? -1 : 1;
        }

        if (va > vb) {
          return currentSort.asc ? 1 : -1;
        }

        return 0;
      });

      const tbody =
        document.getElementById("table-body");

      tbody.innerHTML = "";

      const rowsHtml = list.map(op => {
        const opName =
          op.operator_name || op.operator || "Unknown";

        const flagHtml =
          renderFlag(op.country);

        return `
          <tr>
            <td class="col-center rank-cell">${op.rank}</td>

            <td class="col-left">
              <div class="op-cell">
                ${flagHtml}
                <span>${opName}</span>
              </div>
            </td>

            <td class="total-col">${formatNumber(op.total_teu)}</td>
            <td>${formatNumber(op.total_ships)}</td>
            <td class="share-col">${(op.market_share || 0).toFixed(1)}%</td>

            <td>${formatNumber(op.owned_teu)}</td>
            <td>${formatNumber(op.owned_ships)}</td>
            <td style="color: var(--text-muted);">
              ${(op.owned_share || 0).toFixed(1)}%
            </td>

            <td>${formatNumber(op.chartered_teu)}</td>
            <td>${formatNumber(op.chartered_ships)}</td>
            <td style="color: var(--text-muted);">
              ${(op.chartered_share || 0).toFixed(1)}%
            </td>

            <td style="color: var(--accent-amber);">
              ${formatNumber(op.orderbook_teu)}
            </td>
            <td>${formatNumber(op.orderbook_ships)}</td>
            <td style="color: var(--text-muted);">
              ${(op.orderbook_share || 0).toFixed(1)}%
            </td>
          </tr>
        `;
      }).join("");

      tbody.innerHTML = rowsHtml;
    }

    function exportToCsv() {
      const curDate =
        document.getElementById("sel-date").value;

      const curSnap =
        rawPayload.snapshots[curDate];

      if (!curSnap) {
        return;
      }

      const query =
        document.getElementById("filter-input")
          .value
          .toLowerCase()
          .trim();

      let list = curSnap.operators.filter(op => {
        const name =
          (op.operator_name || op.operator || "")
            .toLowerCase();

        const cntry =
          (op.country || "")
            .toLowerCase();

        return name.includes(query) || cntry.includes(query);
      });

      let csv =
        "\\uFEFFRank,Operator,Country,Total TEU,Total Ships," +
        "Market Share (%),Owned TEU,Owned Ships,Owned Share (%)," +
        "Chartered TEU,Chartered Ships,Chartered Share (%)," +
        "Orderbook TEU,Orderbook Ships,Orderbook Share (%)\\r\\n";

      list.forEach(op => {
        const name =
          `"${(op.operator_name || op.operator || "")
            .replace(/"/g, '""')}"`;

        const c = op.country || "";

        csv +=
          `${op.rank},${name},${c},` +
          `${op.total_teu || 0},` +
          `${op.total_ships || 0},` +
          `${op.market_share || 0},` +
          `${op.owned_teu || 0},` +
          `${op.owned_ships || 0},` +
          `${op.owned_share || 0},` +
          `${op.chartered_teu || 0},` +
          `${op.chartered_ships || 0},` +
          `${op.chartered_share || 0},` +
          `${op.orderbook_teu || 0},` +
          `${op.orderbook_ships || 0},` +
          `${op.orderbook_share || 0}\\r\\n`;
      });

      const blob = new Blob(
        [csv],
        { type: "text/csv;charset=utf-8;" }
      );

      const link = document.createElement("a");
      const url = URL.createObjectURL(blob);

      link.setAttribute("href", url);
      link.setAttribute(
        "download",
        `Alphaliner_TOP100_${curDate}.csv`
      );

      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
    }

    window.onload = initTerminal;
  </script>
</body>
</html>
"""

    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html_content)

    logger.info(
        f"Generated Bloomberg-style HTML viewer: {HTML_PATH}"
    )

# =========================================================
# 7. Flask Server Routing
# =========================================================
@app.route("/")
def serve_index():
    return send_from_directory(str(BASE_DIR), "index.html")

@app.route("/alphaliner_data.json")
def serve_json():
    return send_from_directory(
        str(BASE_DIR),
        "alphaliner_data.json"
    )

# =========================================================
# 8. Main Entry Point
# =========================================================
def main():
    parser = argparse.ArgumentParser(
        description="Alphaliner Static Terminal Crawler"
    )

    parser.add_argument(
        "--cron",
        action="store_true",
        help="Run in GitHub Actions cron batch mode"
    )

    parser.add_argument(
        "--port",
        type=int,
        default=5000
    )

    parser.add_argument(
        "--host",
        default="0.0.0.0"
    )

    args = parser.parse_args()

    init_db()

    if args.cron:
        logger.info(
            "Starting Alphaliner batch cron execution..."
        )

        success = process_and_persist()
        export_json_and_html()

        if not success:
            logger.warning(
                "Cron executed: No new valid snapshot persisted."
            )

        sys.exit(0)

    try:
        with get_db_connection() as conn:
            cnt = conn.cursor().execute(
                "SELECT count(*) FROM snapshot_meta"
            ).fetchone()[0]

            if cnt == 0:
                logger.info(
                    "Database empty on boot. "
                    "Executing initial scrape..."
                )

                process_and_persist()

    except Exception as e:
        logger.warning(
            f"Initial boot scrape skipped: {e}"
        )

    export_json_and_html()

    logger.info(
        f"Starting Alphaliner Web Terminal at "
        f"http://{args.host}:{args.port}"
    )

    app.run(
        host=args.host,
        port=args.port,
        debug=False
    )

if __name__ == "__main__":
    main()
