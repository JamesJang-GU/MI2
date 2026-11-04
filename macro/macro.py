# -*- coding: utf-8 -*-
"""
GDP, Inflation & GSCPI Bloomberg/MI Terminal Dashboard
------------------------------------------------------
로컬 개발 모드:
    python GDP_Inflation_GSCPI_DB.py --host 0.0.0.0 --port 8000

GitHub Actions / 배포 정적 생성 모드:
    python GDP_Inflation_GSCPI_DB.py --cron

생성 및 관리 자산:
    - index.html (Bloomberg Dark Terminal UI)
    - macro_data.json
    - gscpi_data.json
    - dashboard_meta.json
    - gdp_inflation_gscpi_cache.db
"""

import argparse
import datetime
import io
import json
import logging
import os
import re
import socket
import sqlite3
import sys
import threading
import time
import webbrowser
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

# =========================================================
# Common Logging Helper
# =========================================================
def log(message, level="INFO"):
    """
    파이프라인 공통 콘솔 로그 출력 (logger 및 직접 호출 호환).
    """
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(message, str) and message.startswith("["):
        print(f"{timestamp} {message}", flush=True)
    else:
        print(f"{timestamp} [{level}] {message}", flush=True)

# =========================================================
# 1. Global Configurations & Path Definitions
# =========================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("MI_MacroEconomies")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "gdp_inflation_gscpi_cache.db"
MACRO_JSON_PATH = BASE_DIR / "macro_data.json"
GSCPI_JSON_PATH = BASE_DIR / "gscpi_data.json"
META_JSON_PATH = BASE_DIR / "dashboard_meta.json"
HTML_PATH = BASE_DIR / "index.html"

# 대상 국가군 및 그룹 분류
COUNTRIES = [
    {"code": "WLD", "name": "World", "group": "Global", "color": "#00e5ff"},
    {"code": "USA", "name": "United States", "group": "Advanced", "color": "#2979ff"},
    {"code": "CHN", "name": "China", "group": "Emerging", "color": "#ff5252"},
    {"code": "KOR", "name": "South Korea", "group": "Asia", "color": "#00e676"},
    {"code": "JPN", "name": "Japan", "group": "Advanced", "color": "#ff9100"},
    {"code": "DEU", "name": "Germany", "group": "Europe", "color": "#ffd600"},
    {"code": "GBR", "name": "United Kingdom", "group": "Europe", "color": "#e040fb"},
    {"code": "FRA", "name": "France", "group": "Europe", "color": "#00b0ff"},
    {"code": "IND", "name": "India", "group": "Emerging", "color": "#ff6d00"},
    {"code": "VNM", "name": "Vietnam", "group": "Asia", "color": "#76ff03"},
    {"code": "SGP", "name": "Singapore", "group": "Asia", "color": "#1de9b6"},
    {"code": "BRA", "name": "Brazil", "group": "Emerging", "color": "#c6ff00"},
    {"code": "MEX", "name": "Mexico", "group": "Emerging", "color": "#ff3d00"},
    {"code": "EUU", "name": "European Union", "group": "Europe", "color": "#651fff"}
]

WB_INDICATORS = {
    "GDP_GROWTH": {
        "id": "NY.GDP.MKTP.KD.ZG",
        "name": "GDP Growth (Annual %)",
        "unit": "%"
    },
    "CPI_INFLATION": {
        "id": "FP.CPI.TOTL.ZG",
        "name": "Inflation, Consumer Prices (Annual %)",
        "unit": "%"
    }
}

# =========================================================
# 2. Database Schema Initialization & Auto-Migration
# =========================================================
def init_db(conn: sqlite3.Connection):
    """
    DB 스키마 점검 및 기존 구버전 테이블 자동 마이그레이션
    """
    cursor = conn.cursor()

    # 1. macro_series 스키마 검증
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS macro_series (
            indicator TEXT NOT NULL,
            country_code TEXT NOT NULL,
            country_name TEXT NOT NULL,
            year INTEGER NOT NULL,
            value REAL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (indicator, country_code, year)
        )
    """)

    cursor.execute("PRAGMA table_info(macro_series)")
    columns = [col[1] for col in cursor.fetchall()]

    # country_code 컬럼이 없거나 구조가 다른 경우 안전 마이그레이션
    if "country_code" not in columns:
        logger.warning("[DB] macro_series 테이블 스키마 불일치 감지 -> 최신 규격으로 자동 마이그레이션 실행")
        cursor.execute("ALTER TABLE macro_series RENAME TO macro_series_old")
        cursor.execute("""
            CREATE TABLE macro_series (
                indicator TEXT NOT NULL,
                country_code TEXT NOT NULL,
                country_name TEXT NOT NULL,
                year INTEGER NOT NULL,
                value REAL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (indicator, country_code, year)
            )
        """)
        cursor.execute("DROP TABLE IF EXISTS macro_series_old")

    # 2. gscpi_series 스키마 생성 및 구버전 자동 마이그레이션
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS gscpi_series (
            date TEXT PRIMARY KEY,
            value REAL NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("PRAGMA table_info(gscpi_series)")
    gscpi_columns = [col[1] for col in cursor.fetchall()]

    # 구버전 스키마: period / gscpi_value / updated_at
    if "date" not in gscpi_columns or "value" not in gscpi_columns:
        logger.warning(
            "[DB] gscpi_series 구버전 스키마 감지 -> "
            "date/value 표준 구조로 자동 마이그레이션 실행"
        )

        cursor.execute("ALTER TABLE gscpi_series RENAME TO gscpi_series_old")

        cursor.execute("""
            CREATE TABLE gscpi_series (
                date TEXT PRIMARY KEY,
                value REAL NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        old_columns = [
            col[1]
            for col in cursor.execute(
                "PRAGMA table_info(gscpi_series_old)"
            ).fetchall()
        ]

        # 기존 DB에 실제 데이터가 있을 때만 안전 이관
        if "period" in old_columns and "gscpi_value" in old_columns:
            cursor.execute("""
                INSERT OR REPLACE INTO gscpi_series (date, value, updated_at)
                SELECT
                    period,
                    gscpi_value,
                    COALESCE(updated_at, CURRENT_TIMESTAMP)
                FROM gscpi_series_old
                WHERE period IS NOT NULL
                  AND gscpi_value IS NOT NULL
            """)

        cursor.execute("DROP TABLE IF EXISTS gscpi_series_old")

    # 3. meta 정보 테이블
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_meta (
            key TEXT PRIMARY KEY,
            val TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    logger.info("[DB] SQLite 스키마 검증 및 준비 완료.")

# =========================================================
# 3. Data Collection Pipeline (World Bank & NY Fed GSCPI)
# =========================================================
def fetch_world_bank_data(conn: sqlite3.Connection):
    """
    World Bank Open API 배치 수집
    """
    logger.info("[COLLECT] World Bank 거시경제 지표 수집 시작...")
    cursor = conn.cursor()
    current_year = datetime.datetime.now().year
    start_year = current_year - 30

    session = requests.Session()
    session.headers.update({"User-Agent": "MI-Platform-Terminal/2.0"})

    for ind_key, ind_meta in WB_INDICATORS.items():
        ind_code = ind_meta["id"]
        total_ingested = 0

        for country in COUNTRIES:
            c_code = country["code"]
            c_name = country["name"]
            url = f"https://api.worldbank.org/v2/country/{c_code}/indicator/{ind_code}?date={start_year}:{current_year}&format=json&per_page=100"

            try:
                res = session.get(url, timeout=12)
                if res.status_code == 200:
                    payload = res.json()
                    if len(payload) >= 2 and isinstance(payload[1], list):
                        for item in payload[1]:
                            year_str = item.get("date")
                            val = item.get("value")
                            if year_str and val is not None:
                                try:
                                    y_int = int(year_str)
                                    v_float = float(val)
                                    cursor.execute("""
                                        INSERT OR REPLACE INTO macro_series
                                        (indicator, country_code, country_name, year, value, updated_at)
                                        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                                    """, (ind_key, c_code, c_name, y_int, v_float))
                                    total_ingested += 1
                                except ValueError:
                                    continue
            except Exception as e:
                logger.warning(f"[WORLD_BANK WARN] {c_code} ({ind_key}) 통신 장애: {e}")

        conn.commit()
        logger.info(f"[COLLECT] {ind_key} 수집 완료 (적재 건수: {total_ingested}건)")

def fetch_gscpi_from_nyfed_csv():
    """
    NY Fed GSCPI 공식 CSV의 최신 vintage 열을 선택해
    date(YYYY-MM), value 구조의 시계열 레코드로 반환한다.
    """
    csv_url = (
        "https://www.newyorkfed.org/medialibrary/research/"
        "interactives/data/gscpi/gscpi_interactive_data.csv"
    )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "text/csv,text/plain,*/*",
    }

    try:
        log("[GSCPI] NY Fed 공식 CSV 수집 시작...")

        response = requests.get(
            csv_url,
            headers=headers,
            timeout=60
        )
        response.raise_for_status()

        df = pd.read_csv(StringIO(response.text))

        if "Date" not in df.columns:
            raise ValueError(
                f"GSCPI CSV에 Date 컬럼이 없습니다: {df.columns.tolist()}"
            )

        # Date 이외의 열은 Jan-22, Feb-22 ... 형식의 vintage 열
        vintage_candidates = []

        for column in df.columns:
            if column == "Date":
                continue

            vintage_date = pd.to_datetime(
                str(column).strip(),
                format="%b-%y",
                errors="coerce"
            )

            if pd.isna(vintage_date):
                continue

            valid_count = pd.to_numeric(
                df[column],
                errors="coerce"
            ).notna().sum()

            if valid_count > 0:
                vintage_candidates.append({
                    "column": column,
                    "vintage_date": vintage_date,
                    "valid_count": int(valid_count),
                })

        if not vintage_candidates:
            raise ValueError("유효한 GSCPI vintage 열을 찾지 못했습니다.")

        # 가장 최근 발표·개정본 열 선택: 예, Oct-26
        latest_vintage = max(
            vintage_candidates,
            key=lambda item: item["vintage_date"]
        )

        value_column = latest_vintage["column"]

        log(
            f"[GSCPI] 최신 vintage 선택: {value_column} "
            f"(유효 관측치: {latest_vintage['valid_count']}건)"
        )

        parsed = pd.DataFrame({
            "date": pd.to_datetime(
                df["Date"],
                format="%d-%b-%Y",
                errors="coerce"
            ),
            "value": pd.to_numeric(
                df[value_column],
                errors="coerce"
            ),
        })

        # 하단 빈 행 및 아직 발표되지 않은 미래 월 제거
        parsed = parsed.dropna(subset=["date", "value"]).copy()

        parsed["date"] = parsed["date"].dt.strftime("%Y-%m")

        parsed = (
            parsed
            .drop_duplicates(subset=["date"], keep="last")
            .sort_values("date")
            .reset_index(drop=True)
        )

        records = [
            {
                "date": row["date"],
                "value": round(float(row["value"]), 4),
            }
            for _, row in parsed.iterrows()
        ]

        if not records:
            raise ValueError("GSCPI 유효 시계열이 없습니다.")

        log(
            f"[GSCPI] 수집 성공: {len(records)}건 | "
            f"{records[0]['date']} ~ {records[-1]['date']} | "
            f"최신값: {records[-1]['value']}"
        )

        return records

    except Exception as exc:
        log(f"[WARNING] [GSCPI] 공식 CSV 수집 실패: {exc}")
        return []

def fetch_gscpi_data(conn):
    """
    NY Fed GSCPI 최신 vintage 데이터를 수집하고
    SQLite gscpi_series 테이블에 저장한다.

    기존 main()의 fetch_gscpi_data(conn) 호출 구조를 유지하기 위한
    호환 함수이다.
    """
    gscpi_records = fetch_gscpi_from_nyfed_csv()

    if not gscpi_records:
        log("[WARNING] [GSCPI] 신규 수집 데이터가 없습니다. 기존 DB 캐시를 유지합니다.")
        return 0

    cursor = conn.cursor()

    # 현재 실제 테이블의 컬럼 구조를 확인
    table_columns = {
        row[1]
        for row in cursor.execute("PRAGMA table_info(gscpi_series)").fetchall()
    }

    if "date" not in table_columns or "value" not in table_columns:
        raise RuntimeError(
            "[GSCPI] gscpi_series 테이블에 필수 컬럼(date, value)이 없습니다. "
            f"현재 컬럼: {sorted(table_columns)}"
        )

    # updated_at 컬럼 유무에 따라 안전하게 저장
    if "updated_at" in table_columns:
        cursor.executemany("""
            INSERT OR REPLACE INTO gscpi_series (
                date,
                value,
                updated_at
            )
            VALUES (?, ?, CURRENT_TIMESTAMP)
        """, [
            (item["date"], item["value"])
            for item in gscpi_records
        ])
    else:
        cursor.executemany("""
            INSERT OR REPLACE INTO gscpi_series (
                date,
                value
            )
            VALUES (?, ?)
        """, [
            (item["date"], item["value"])
            for item in gscpi_records
        ])

    conn.commit()

    log(f"[GSCPI] SQLite 저장 완료: {len(gscpi_records)}건")
    return len(gscpi_records)

# =========================================================
# 4. JSON Export & Bloomberg Terminal HTML Generation
# =========================================================
def export_assets(conn: sqlite3.Connection):
    """
    대시보드 구동용 정적 JSON 및 Bloomberg UI HTML 렌더링
    """
    cursor = conn.cursor()

    # 1. Macro JSON
    cursor.execute("""
        SELECT indicator, country_code, country_name, year, value
        FROM macro_series
        ORDER BY indicator, country_code, year ASC
    """)
    macro_rows = cursor.fetchall()
    macro_dict = {"GDP_GROWTH": {}, "CPI_INFLATION": {}}

    for ind, code, name, year, val in macro_rows:
        if ind not in macro_dict:
            macro_dict[ind] = {}
        if code not in macro_dict[ind]:
            c_meta = next((c for c in COUNTRIES if c["code"] == code), {"group": "Other", "color": "#8b8e9b"})
            macro_dict[ind][code] = {
                "name": name,
                "group": c_meta.get("group", "Other"),
                "color": c_meta.get("color", "#8b8e9b"),
                "series": []
            }
        macro_dict[ind][code]["series"].append({"year": year, "value": val})

    with open(MACRO_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(macro_dict, f, ensure_ascii=False, indent=2)

    # 2. GSCPI JSON
    cursor.execute("""
        SELECT date, value
        FROM gscpi_series
        ORDER BY date ASC
    """)

    gscpi_rows = cursor.fetchall()
    gscpi_list = [{"date": r[0], "value": r[1]} for r in gscpi_rows]

    with open(GSCPI_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(gscpi_list, f, ensure_ascii=False, indent=2)

    # 3. Meta JSON
    latest_gscpi = gscpi_list[-1] if gscpi_list else {"date": "N/A", "value": 0.0}
    meta_data = {
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "latest_gscpi": latest_gscpi,
        "total_countries": len(COUNTRIES),
        "source": "World Bank, Federal Reserve Bank of New York"
    }
    with open(META_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(meta_data, f, ensure_ascii=False, indent=2)

    # 4. Generate Bloomberg Terminal HTML
    html_content = build_bloomberg_html(meta_data)
    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html_content)

    logger.info("[EXPORT] 정적 JSON 자산 및 index.html 생성 완료.")

def build_bloomberg_html(meta: dict) -> str:
    """Bloomberg / MI Terminal 고도화 UI HTML 템플릿"""
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>MI Terminal - Macro Economies & GSCPI</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Noto+Sans+KR:wght@400;500;700&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root {{
      --bg-base: #0a0a0c;
      --bg-panel: #121318;
      --bg-header: #181a20;
      --bg-hover: #1f222e;
      --border: #282a36;
      --amber: #ff9800;
      --cyan: #00e5ff;
      --green: #00e676;
      --red: #ff5252;
      --text: #e0e2ec;
      --text-muted: #8b8e9b;
      --font-mono: 'JetBrains Mono', monospace;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background-color: var(--bg-base);
      color: var(--text);
      font-family: 'Noto Sans KR', sans-serif;
      padding: 16px;
      font-size: 13px;
    }}

    /* Top Standard Terminal Header */
    .terminal-header {{
      background-color: var(--bg-header);
      border: 1px solid var(--border);
      border-radius: 4px;
      padding: 12px 16px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 14px;
    }}
    .terminal-left {{
      display: flex;
      align-items: center;
      gap: 12px;
    }}
    .terminal-title {{
      font-family: var(--font-mono);
      font-size: 16px;
      font-weight: 700;
      color: var(--amber);
      letter-spacing: 0.5px;
    }}
    .badge {{
      background: #232733;
      color: var(--cyan);
      font-family: var(--font-mono);
      font-size: 11px;
      padding: 3px 8px;
      border-radius: 3px;
      border: 1px solid rgba(0, 229, 255, 0.3);
    }}
    .terminal-right {{
      display: flex;
      align-items: center;
      gap: 16px;
      font-family: var(--font-mono);
      font-size: 12px;
    }}
    .nav-btn {{
      background: #1e222d;
      color: var(--amber);
      border: 1px solid var(--amber);
      padding: 4px 12px;
      border-radius: 3px;
      text-decoration: none;
      font-weight: 600;
      transition: all 0.2s ease;
    }}
    .nav-btn:hover {{
      background: var(--amber);
      color: #000;
    }}

    /* KPI Summary Cards */
    .kpi-row {{
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 12px;
      margin-bottom: 14px;
    }}
    @media (max-width: 1024px) {{
      .kpi-row {{ grid-template-columns: repeat(2, 1fr); }}
    }}
    .kpi-card {{
      background: var(--bg-panel);
      border: 1px solid var(--border);
      border-left: 3px solid var(--amber);
      border-radius: 4px;
      padding: 12px 14px;
    }}
    .kpi-label {{
      color: var(--text-muted);
      font-size: 11px;
      margin-bottom: 4px;
      font-family: var(--font-mono);
    }}
    .kpi-val {{
      font-family: var(--font-mono);
      font-size: 20px;
      font-weight: 700;
      color: var(--cyan);
    }}
    .kpi-sub {{
      font-family: var(--font-mono);
      font-size: 11px;
      color: var(--text-muted);
      margin-top: 2px;
    }}

    /* Main Grid Layout */
    .grid-container {{
      display: grid;
      grid-template-columns: 2fr 1fr;
      gap: 14px;
    }}
    @media (max-width: 1024px) {{
      .grid-container {{ grid-template-columns: 1fr; }}
    }}

    .panel {{
      background: var(--bg-panel);
      border: 1px solid var(--border);
      border-radius: 4px;
      padding: 14px;
      margin-bottom: 14px;
    }}
    .panel-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid var(--border);
      padding-bottom: 8px;
      margin-bottom: 12px;
    }}
    .panel-title {{
      font-family: var(--font-mono);
      font-size: 13px;
      font-weight: 700;
      color: var(--amber);
    }}
    .chart-container {{
      position: relative;
      height: 300px;
      width: 100%;
    }}

    /* Tab Controls */
    .tab-bar {{
      display: flex;
      gap: 8px;
      margin-bottom: 12px;
    }}
    .tab-btn {{
      background: #181a20;
      border: 1px solid var(--border);
      color: var(--text-muted);
      padding: 4px 10px;
      font-family: var(--font-mono);
      font-size: 11px;
      cursor: pointer;
      border-radius: 3px;
    }}
    .tab-btn.active {{
      background: #232733;
      border-color: var(--cyan);
      color: var(--cyan);
      font-weight: 600;
    }}

    /* Data Table */
    .table-container {{
      max-height: 680px;
      overflow-y: auto;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-family: var(--font-mono);
      font-size: 12px;
    }}
    th, td {{
      padding: 8px 10px;
      text-align: right;
      border-bottom: 1px solid #1c1e26;
    }}
    th {{
      color: var(--text-muted);
      font-weight: 600;
      background: #15171e;
      position: sticky;
      top: 0;
    }}
    th:first-child, td:first-child {{ text-align: left; }}
    tr:hover td {{ background: var(--bg-hover); }}
    .pos {{ color: var(--green); }}
    .neg {{ color: var(--red); }}
  </style>
</head>
<body>

  <!-- Bloomberg MI Header -->
  <div class="terminal-header">
    <div class="terminal-left">
      <div class="terminal-title">MI TERMINAL // 03. MACRO ECONOMIES &amp; GSCPI</div>
      <span class="badge">PROD LIVE</span>
    </div>
    <div class="terminal-right">
      <span style="color: var(--text-muted)">SRC: World Bank / NY Fed</span>
      <span style="color: var(--cyan)" id="liveClock">--:--:-- KST</span>
      <a href="../index.html" class="nav-btn">PORTAL &lt;GO&gt;</a>
    </div>
  </div>

  <!-- KPI Ribbon -->
  <div class="kpi-row">
    <div class="kpi-card">
      <div class="kpi-label">GLOBAL SUPPLY CHAIN PRESSURE (GSCPI)</div>
      <div class="kpi-val" id="kpiGscpi">--</div>
      <div class="kpi-sub" id="kpiGscpiDate">Standard Deviations (0 = Average)</div>
    </div>
    <div class="kpi-card" style="border-left-color: var(--cyan)">
      <div class="kpi-label">US GDP GROWTH (ANNUAL %)</div>
      <div class="kpi-val" id="kpiUsGdp">--%</div>
      <div class="kpi-sub" id="kpiUsGdpYear">World Bank Benchmark</div>
    </div>
    <div class="kpi-card" style="border-left-color: var(--green)">
      <div class="kpi-label">US CPI INFLATION (ANNUAL %)</div>
      <div class="kpi-val" id="kpiUsCpi">--%</div>
      <div class="kpi-sub" id="kpiUsCpiYear">World Bank Benchmark</div>
    </div>
    <div class="kpi-card" style="border-left-color: #ffd600">
      <div class="kpi-label">CHINA GDP GROWTH (ANNUAL %)</div>
      <div class="kpi-val" id="kpiChnGdp">--%</div>
      <div class="kpi-sub" id="kpiChnGdpYear">World Bank Benchmark</div>
    </div>
  </div>

  <!-- Main Content Layout -->
  <div class="grid-container">
    <div>
      <!-- GSCPI Chart Panel -->
      <div class="panel">
        <div class="panel-header">
          <span class="panel-title">01. GLOBAL SUPPLY CHAIN PRESSURE INDEX (GSCPI)</span>
          <span style="font-family:var(--font-mono); font-size:11px; color:var(--text-muted);">Monthly Historical Trend</span>
        </div>
        <div class="chart-container">
          <canvas id="gscpiChart"></canvas>
        </div>
      </div>

      <!-- Macro Chart Panel with Tabs -->
      <div class="panel">
        <div class="panel-header">
          <span class="panel-title">02. MACRO INDICATORS BY MAJOR ECONOMIES</span>
          <div class="tab-bar" style="margin-bottom:0;">
            <button class="tab-btn active" onclick="switchMacroTab('GDP_GROWTH')">GDP GROWTH (%)</button>
            <button class="tab-btn" onclick="switchMacroTab('CPI_INFLATION')">CPI INFLATION (%)</button>
          </div>
        </div>
        <div class="chart-container">
          <canvas id="macroChart"></canvas>
        </div>
      </div>
    </div>

    <!-- Country Snapshot Table Panel -->
    <div>
      <div class="panel">
        <div class="panel-header">
          <span class="panel-title">03. CROSS-COUNTRY SNAPSHOT</span>
          <span style="font-family:var(--font-mono); font-size:11px; color:var(--text-muted);">Latest WB Data</span>
        </div>
        <div class="table-container">
          <table>
            <thead>
              <tr>
                <th>Country</th>
                <th>Group</th>
                <th>GDP (%)</th>
                <th>CPI (%)</th>
              </tr>
            </thead>
            <tbody id="macroTableBody">
              <tr><td colspan="4" style="text-align:center;">Loading Data...</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

  <script>
    // Live KST Clock Generator
    function updateKstClock() {{
      const now = new Date();
      const utc = now.getTime() + (now.getTimezoneOffset() * 60000);
      const kst = new Date(utc + (3600000 * 9));
      document.getElementById('liveClock').textContent =
        kst.toISOString().replace('T', ' ').substring(0, 19) + ' KST';
    }}
    setInterval(updateKstClock, 1000);
    updateKstClock();

    let globalMacroData = {{}};
    let currentIndicator = 'GDP_GROWTH';
    let macroChartInstance = null;

    async function initDashboard() {{
      try {{
        const [mRes, gRes] = await Promise.all([
          fetch('macro_data.json'),
          fetch('gscpi_data.json')
        ]);
        globalMacroData = await mRes.json();
        const gscpiData = await gRes.json();

        // 1. Render GSCPI
        if (gscpiData && gscpiData.length > 0) {{
          const latest = gscpiData[gscpiData.length - 1];
          const el = document.getElementById('kpiGscpi');
          el.textContent = `${{latest.value >= 0 ? '+' : ''}}${{latest.value.toFixed(2)}}σ`;
          el.className = 'kpi-val ' + (latest.value > 0 ? 'neg' : 'pos');
          document.getElementById('kpiGscpiDate').textContent = `Latest: ${{latest.date}} (New York Fed)`;

          renderGscpiChart(gscpiData);
        }}

        // 2. Render Macro KPIs
        const gdp = globalMacroData.GDP_GROWTH || {{}};
        const cpi = globalMacroData.CPI_INFLATION || {{}};

        if (gdp.USA?.series.length) {{
          const usLastGdp = gdp.USA.series[gdp.USA.series.length - 1];
          document.getElementById('kpiUsGdp').textContent = `${{usLastGdp.value.toFixed(2)}}%`;
          document.getElementById('kpiUsGdpYear').textContent = `Period: ${{usLastGdp.year}} Annual`;
        }}
        if (cpi.USA?.series.length) {{
          const usLastCpi = cpi.USA.series[cpi.USA.series.length - 1];
          document.getElementById('kpiUsCpi').textContent = `${{usLastCpi.value.toFixed(2)}}%`;
          document.getElementById('kpiUsCpiYear').textContent = `Period: ${{usLastCpi.year}} Annual`;
        }}
        if (gdp.CHN?.series.length) {{
          const chnLastGdp = gdp.CHN.series[gdp.CHN.series.length - 1];
          document.getElementById('kpiChnGdp').textContent = `${{chnLastGdp.value.toFixed(2)}}%`;
          document.getElementById('kpiChnGdpYear').textContent = `Period: ${{chnLastGdp.year}} Annual`;
        }}

        // 3. Render Table & Macro Chart
        renderMacroTable();
        renderMacroChart(currentIndicator);

      }} catch (err) {{
        console.error('Data Loading Error:', err);
      }}
    }}

    function renderGscpiChart(data) {{
      const ctx = document.getElementById('gscpiChart').getContext('2d');
      const recent = data.slice(-72); // Last 6 years
      new Chart(ctx, {{
        type: 'line',
        data: {{
          labels: recent.map(d => d.date),
          datasets: [{{
            label: 'GSCPI (Standard Deviations)',
            data: recent.map(d => d.value),
            borderColor: '#00e5ff',
            backgroundColor: 'rgba(0, 229, 255, 0.08)',
            fill: true,
            borderWidth: 1.5,
            pointRadius: 0
          }}]
        }},
        options: {{
          responsive: true,
          maintainAspectRatio: false,
          scales: {{
            x: {{ grid: {{ color: '#1c1e26' }}, ticks: {{ color: '#8b8e9b', font: {{ family: 'JetBrains Mono', size: 10 }} }} }},
            y: {{ grid: {{ color: '#1c1e26' }}, ticks: {{ color: '#8b8e9b', font: {{ family: 'JetBrains Mono', size: 10 }} }} }}
          }},
          plugins: {{ legend: {{ display: false }} }}
        }}
      }});
    }}

    function switchMacroTab(indicator) {{
      currentIndicator = indicator;
      document.querySelectorAll('.tab-btn').forEach(btn => {{
        btn.classList.toggle('active', btn.textContent.includes(indicator === 'GDP_GROWTH' ? 'GDP' : 'CPI'));
      }});
      renderMacroChart(indicator);
    }}

    function renderMacroChart(indicator) {{
      const ctx = document.getElementById('macroChart').getContext('2d');
      if (macroChartInstance) macroChartInstance.destroy();

      const obj = globalMacroData[indicator] || {{}};
      const targetCodes = ['WLD', 'USA', 'CHN', 'KOR', 'DEU', 'VNM'];
      const years = obj.USA?.series.slice(-12).map(s => s.year) || [];

      const datasets = targetCodes.map(code => {{
        const item = obj[code];
        if (!item) return null;
        const series = item.series.slice(-12);
        return {{
          label: item.name,
          data: series.map(s => s.value),
          borderColor: item.color || '#2979ff',
          borderWidth: 1.5,
          fill: false,
          pointRadius: 2
        }};
      }}).filter(Boolean);

      macroChartInstance = new Chart(ctx, {{
        type: 'line',
        data: {{ labels: years, datasets: datasets }},
        options: {{
          responsive: true,
          maintainAspectRatio: false,
          scales: {{
            x: {{ grid: {{ color: '#1c1e26' }}, ticks: {{ color: '#8b8e9b', font: {{ family: 'JetBrains Mono', size: 10 }} }} }},
            y: {{ grid: {{ color: '#1c1e26' }}, ticks: {{ color: '#8b8e9b', font: {{ family: 'JetBrains Mono', size: 10 }} }} }}
          }},
          plugins: {{
            legend: {{
              labels: {{ color: '#e0e2ec', font: {{ family: 'JetBrains Mono', size: 11 }} }}
            }}
          }}
        }}
      }});
    }}

    function renderMacroTable() {{
      const tbody = document.getElementById('macroTableBody');
      tbody.innerHTML = '';
      const gdpObj = globalMacroData.GDP_GROWTH || {{}};
      const cpiObj = globalMacroData.CPI_INFLATION || {{}};

      Object.keys(gdpObj).forEach(code => {{
        const gItem = gdpObj[code];
        const cItem = cpiObj[code];
        const lastG = gItem?.series.length ? gItem.series[gItem.series.length - 1].value : null;
        const lastC = cItem?.series.length ? cItem.series[cItem.series.length - 1].value : null;

        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td style="color:#e0e2ec; font-weight:600;">${{gItem.name}} <span style="color:var(--text-muted); font-size:10px;">(${{code}})</span></td>
          <td style="color:var(--text-muted); text-align:right;">${{gItem.group}}</td>
          <td class="${{lastG >= 0 ? 'pos' : 'neg'}}">${{lastG !== null ? lastG.toFixed(1) + '%' : '-'}}</td>
          <td style="color:var(--amber);">${{lastC !== null ? lastC.toFixed(1) + '%' : '-'}}</td>
        `;
        tbody.appendChild(tr);
      }});
    }}

    window.addEventListener('DOMContentLoaded', initDashboard);
  </script>
</body>
</html>"""

# =========================================================
# 5. Local Development Server Support (Flask Mode)
# =========================================================
def run_local_server(host: str, port: int, no_open: bool):
    """
    Codespaces 및 로컬 환경에서 대시보드를 즉시 검증할 수 있는 경량 HTTP 서버 구동
    """
    try:
        from flask import Flask, send_from_directory
    except ImportError:
        logger.warning("[SERVER] Flask 미설치 상태입니다. 내장 http.server 로 대체 가동합니다.")
        import http.server
        import socketserver
        os.chdir(BASE_DIR)
        handler = http.server.SimpleHTTPRequestHandler
        with socketserver.TCPServer((host, port), handler) as httpd:
            logger.info(f"[SERVER] Local Server Ready at http://{host}:{port}/")
            httpd.serve_forever()
        return

    app = Flask(__name__, static_folder=str(BASE_DIR))

    @app.route("/")
    def index():
        return send_from_directory(BASE_DIR, "index.html")

    @app.route("/<path:path>")
    def static_files(path):
        return send_from_directory(BASE_DIR, path)

    if not no_open:
        def open_browser():
            time.sleep(1)
            webbrowser.open(f"http://localhost:{port}/")
        threading.Thread(target=open_browser, daemon=True).start()

    logger.info(f"[SERVER] Flask Server Serving at http://{host}:{port}/")
    app.run(host=host, port=port, debug=False, use_reloader=False)

# =========================================================
# 6. Main Execution Entrypoint
# =========================================================
def main():
    parser = argparse.ArgumentParser(description="MacroEconomies Bloomberg Terminal Engine")
    parser.add_argument("--cron", action="store_true", help="GitHub Actions Static Generator Mode")
    parser.add_argument("--host", default="0.0.0.0", help="Local Server Host IP")
    parser.add_argument("--port", type=int, default=8000, help="Local Server Port")
    parser.add_argument("--no-open", action="store_true", help="Do not open web browser automatically")
    args = parser.parse_args()

    logger.info("========================================================================")
    logger.info("[START] MacroEconomies 파이프라인 가동")
    logger.info(f"Target DB: {DB_PATH}")
    logger.info("========================================================================")

    conn = sqlite3.connect(DB_PATH)
    try:
        init_db(conn)
        fetch_world_bank_data(conn)
        fetch_gscpi_data(conn)
        export_assets(conn)
        logger.info("[SUCCESS] 거시경제 데이터 수집 및 Bloomberg Terminal 빌드 완료.")
    except Exception as e:
        logger.error(f"[FATAL] 파이프라인 처리 중 치명적 예외: {e}", exc_info=True)
        if not HTML_PATH.exists():
            sys.exit(1)
    finally:
        conn.close()

    # Cron 모드가 아닐 경우 로컬 서버 실행
    if not args.cron:
        run_local_server(args.host, args.port, args.no_open)

if __name__ == "__main__":
    main()
