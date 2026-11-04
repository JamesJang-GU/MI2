# -*- coding: utf-8 -*-
"""
Shipping Index Dashboard + Incremental Crawler

필수 환경변수:
    KSG_ID
    KSG_PW

선택 환경변수:
    FLASK_SECRET_KEY
    DASHBOARD_USER
    DASHBOARD_PASSWORD
    CHROMEDRIVER_PATH
    SCFI_WEB_HOST
    SCFI_WEB_PORT

필수 패키지:
    flask
    pandas
    plotly
    selenium
    openpyxl
    webdriver-manager
    kaleido
"""

import os
import re
import sys
import glob
import time
import hmac
import secrets
import sqlite3
import logging
import warnings
import argparse
import webbrowser

from datetime import datetime, date
from functools import wraps
from io import BytesIO
from logging.handlers import RotatingFileHandler
from threading import Lock, Thread, Timer
from urllib.parse import urlencode

warnings.filterwarnings("ignore")

try:
    import pandas as pd
    import plotly.graph_objects as go
    import plotly.io as pio

    from flask import (
        Flask,
        Response,
        jsonify,
        redirect,
        render_template_string,
        request,
        session,
        url_for,
    )
    from markupsafe import escape
    from selenium import webdriver
    from selenium.common.exceptions import NoAlertPresentException, TimeoutException
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

except ImportError as exc:
    raise SystemExit(
        f"필수 패키지가 설치되지 않았습니다: {exc}\n"
        "다음 명령으로 설치하세요.\n"
        "python -m pip install flask pandas plotly selenium openpyxl "
        "webdriver-manager kaleido"
    )

try:
    from webdriver_manager.chrome import ChromeDriverManager

    HAS_WDM = True
except ImportError:
    HAS_WDM = False


# =============================================================================
# 기본 설정
# =============================================================================

BASE_DIR = (
    os.path.dirname(os.path.abspath(__file__))
    if "__file__" in globals()
    else os.getcwd()
)

LOG_FILE = os.path.join(BASE_DIR, "index_crawl.log")
DB_FILE = os.path.join(BASE_DIR, "index_history.db")
BASE_URL = "https://www.ksg.co.kr/"

DEFAULT_INDEX_NAME = "SCFI"
SUPPORTED_INDEXES = ["SCFI", "BDI"]
PALETTE_OPTIONS = ["Auto", "Grey", "Red", "Blue"]

BDI_LABEL_MAP = {
    "BDI": "BDI (Baltic Exchange Dry Index)",
    "BCI": "BCI (Baltic Capesize Index)",
    "BPI": "BPI (Baltic Panamax Index)",
    "BSI": "BSI (Baltic Supramax Index)",
    "BHSI": "BHSI (Baltic Handysize Index)",
    "BDTI": "BDTI (Baltic Dirty Tanker Index)",
    "BCTI": "BCTI (Baltic Clean Tanker Index)",
}

BDI_REGION_PRIORITY = [
    "BDI",
    "BCI",
    "BPI",
    "BSI",
    "BHSI",
    "BDTI",
    "BCTI",
]

INVALID_REGION_NAMES = {
    "",
    "일자",
    "날짜",
    "날 자",
    "DATE",
    "Date",
    "date",
    "YEAR",
    "Year",
    "year",
    "INDEX_NAME",
    "CRAWL_PAGE",
}

MAX_DOWNLOAD_ROWS = 500_000

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)

RUNTIME_CONFIG = None

crawl_lock = Lock()
sync_state_lock = Lock()

SYNC_STATE = {
    "status": "IDLE",
    "index_name": None,
    "started_at": None,
    "finished_at": None,
    "message": "",
    "result": None,
}


# =============================================================================
# 로깅
# =============================================================================

logger = logging.getLogger("shipping_index_web")
logger.setLevel(logging.INFO)
logger.handlers.clear()
logger.propagate = False

formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s"
)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)


def activate_file_logging():
    if any(
        isinstance(handler, RotatingFileHandler)
        for handler in logger.handlers
    ):
        return

    try:
        file_handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except Exception as exc:
        logger.warning("파일 로그 활성화 실패: %s", exc)


def log(message):
    logger.info(message)


# =============================================================================
# 실행 인수
# =============================================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description="Shipping Index Dashboard + Incremental Crawler"
    )

    parser.add_argument("--user", default=os.getenv("KSG_ID"))
    parser.add_argument("--password", default=os.getenv("KSG_PW"))
    parser.add_argument("--output", default=BASE_DIR)

    parser.add_argument(
        "--driver-path",
        default=os.getenv("CHROMEDRIVER_PATH", ""),
    )
    parser.add_argument(
        "--host",
        default=os.getenv("SCFI_WEB_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("SCFI_WEB_PORT", "8082")),
    )

    parser.add_argument(
        "--open-browser",
        dest="open_browser",
        action="store_true",
        default=True,
    )
    parser.add_argument(
        "--no-open-browser",
        dest="open_browser",
        action="store_false",
    )

    parser.add_argument(
        "--headless",
        dest="headless",
        action="store_true",
        default=True,
    )
    parser.add_argument(
        "--no-headless",
        dest="headless",
        action="store_false",
    )

    parser.add_argument(
        "--incremental",
        dest="incremental",
        action="store_true",
        default=True,
    )
    parser.add_argument(
        "--no-incremental",
        dest="incremental",
        action="store_false",
    )

    parser.add_argument("--max-pages", type=int, default=100000)
    parser.add_argument("--pause", type=float, default=0.2)

    parser.add_argument(
        "--init-refresh",
        dest="init_refresh",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--no-init-refresh",
        dest="init_refresh",
        action="store_false",
    )

    return parser.parse_args()


# =============================================================================
# 공통 유틸리티
# =============================================================================

def normalize_index_name(index_name):
    value = str(index_name or DEFAULT_INDEX_NAME).strip().upper()
    return value if value in SUPPORTED_INDEXES else DEFAULT_INDEX_NAME


def normalize_palette_name(name):
    value = str(name or "Auto").strip()
    return value if value in PALETTE_OPTIONS else "Auto"


def normalize_date_arg(value):
    value = str(value or "").strip()

    if not value:
        return ""

    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def is_valid_region_for_index(region, index_name):
    index_name = normalize_index_name(index_name)
    region = str(region or "").strip()

    if not region:
        return False

    invalid_upper = {
        str(item).strip().upper()
        for item in INVALID_REGION_NAMES
    }

    if region.upper() in invalid_upper:
        return False

    if index_name == "BDI":
        return region.upper() in BDI_REGION_PRIORITY

    return True


def display_region_label(region, selected_index):
    region = str(region)
    selected_index = normalize_index_name(selected_index)

    if selected_index == "BDI":
        return BDI_LABEL_MAP.get(region, region)

    return region


def sort_regions_for_display(regions, selected_index):
    selected_index = normalize_index_name(selected_index)

    normalized = [
        str(region).strip()
        for region in regions
        if is_valid_region_for_index(region, selected_index)
    ]
    normalized = sorted(set(normalized))

    if selected_index == "BDI":
        priority = {
            name: index
            for index, name in enumerate(BDI_REGION_PRIORITY)
        }
        return sorted(
            normalized,
            key=lambda item: (priority.get(item, 999), item),
        )

    return normalized


def parse_date(date_text):
    date_text = str(date_text).strip()

    formats = [
        "%Y-%m-%d",
        "%Y.%m.%d",
        "%Y/%m/%d",
        "%y-%m-%d",
        "%y.%m.%d",
        "%y/%m/%d",
    ]

    for date_format in formats:
        try:
            return datetime.strptime(date_text, date_format).date()
        except ValueError:
            continue

    return date_text


def clean_number(text):
    text = str(text).strip().replace(",", "")

    if not text:
        return None

    if re.fullmatch(r"[+-]?\d+(\.\d+)?", text):
        try:
            return float(text) if "." in text else int(text)
        except (TypeError, ValueError):
            return None

    return text


def make_unique_headers(headers, max_length):
    result = []
    used = {}

    for index in range(max_length):
        base = (
            str(headers[index]).strip()
            if index < len(headers) and str(headers[index]).strip()
            else f"COL_{index + 1}"
        )

        count = used.get(base, 0) + 1
        used[base] = count

        result.append(base if count == 1 else f"{base}_{count}")

    return result


# =============================================================================
# 데이터베이스
# =============================================================================

def _ensure_column(conn, table_name, column_name, column_definition):
    columns = {
        row[1]
        for row in conn.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
    }

    if column_name not in columns:
        conn.execute(
            f"ALTER TABLE {table_name} "
            f"ADD COLUMN {column_name} {column_definition}"
        )


def get_db_conn():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=30,
        check_same_thread=False,
    )

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS index_raw (
            dt TEXT NOT NULL,
            region TEXT NOT NULL,
            value REAL,
            index_name TEXT NOT NULL,
            source_file TEXT,
            created_at TEXT,
            updated_at TEXT,
            PRIMARY KEY (dt, region, index_name)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crawl_audit (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT,
            finished_at TEXT,
            status TEXT,
            message TEXT,
            rows_added INTEGER DEFAULT 0,
            rows_updated INTEGER DEFAULT 0,
            total_rows INTEGER DEFAULT 0,
            index_name TEXT
        )
        """
    )

    _ensure_column(conn, "index_raw", "updated_at", "TEXT")
    _ensure_column(
        conn,
        "crawl_audit",
        "rows_updated",
        "INTEGER DEFAULT 0",
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_index_raw_name_dt
        ON index_raw(index_name, dt)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_index_raw_name_region_dt
        ON index_raw(index_name, region, dt)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_crawl_audit_name_id
        ON crawl_audit(index_name, audit_id DESC)
        """
    )

    conn.commit()
    return conn


def cleanup_invalid_regions():
    conn = None

    try:
        conn = get_db_conn()
        cursor = conn.cursor()

        invalid_names = sorted({
            str(name).strip().upper()
            for name in INVALID_REGION_NAMES
        })

        if invalid_names:
            placeholders = ",".join("?" for _ in invalid_names)
            cursor.execute(
                f"""
                DELETE FROM index_raw
                WHERE UPPER(TRIM(region)) IN ({placeholders})
                """,
                invalid_names,
            )

        allowed_bdi = [
            name.upper()
            for name in BDI_REGION_PRIORITY
        ]
        placeholders = ",".join("?" for _ in allowed_bdi)

        cursor.execute(
            f"""
            DELETE FROM index_raw
            WHERE UPPER(TRIM(index_name)) = 'BDI'
              AND UPPER(TRIM(region)) NOT IN ({placeholders})
            """,
            allowed_bdi,
        )

        conn.commit()
        log("[DB] 잘못된 region 데이터 정리 완료")

    except Exception as exc:
        if conn:
            conn.rollback()
        logger.exception(
            "[DB] 잘못된 region 데이터 정리 실패: %s",
            exc,
        )

    finally:
        if conn:
            conn.close()


def get_total_row_count(index_name=None):
    conn = get_db_conn()

    try:
        if index_name:
            index_name = normalize_index_name(index_name)
            row = conn.execute(
                """
                SELECT COUNT(*)
                FROM index_raw
                WHERE index_name = ?
                """,
                [index_name],
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM index_raw"
            ).fetchone()

        return int(row[0] or 0)

    finally:
        conn.close()


def load_long_df_from_db(
    index_name,
    start_date=None,
    end_date=None,
    years=None,
    regions=None,
):
    index_name = normalize_index_name(index_name)

    conditions = ["index_name = ?"]
    params = [index_name]

    if start_date:
        conditions.append("dt >= ?")
        params.append(start_date)

    if end_date:
        conditions.append("dt <= ?")
        params.append(end_date)

    if years:
        valid_years = sorted({
            int(year)
            for year in years
            if str(year).isdigit()
        })

        if valid_years:
            placeholders = ",".join("?" for _ in valid_years)
            conditions.append(
                f"CAST(substr(dt, 1, 4) AS INTEGER) "
                f"IN ({placeholders})"
            )
            params.extend(valid_years)

    if regions:
        valid_regions = [
            str(region).strip()
            for region in regions
            if is_valid_region_for_index(region, index_name)
        ]

        if valid_regions:
            placeholders = ",".join("?" for _ in valid_regions)
            conditions.append(f"region IN ({placeholders})")
            params.extend(valid_regions)

    query = f"""
        SELECT
            dt AS DATE,
            region AS REGION,
            value AS VALUE,
            index_name AS INDEX_NAME
        FROM index_raw
        WHERE {" AND ".join(conditions)}
        ORDER BY dt, region
    """

    conn = get_db_conn()

    try:
        df = pd.read_sql_query(query, conn, params=params)
    finally:
        conn.close()

    if df.empty:
        return pd.DataFrame(
            columns=[
                "DATE",
                "YEAR",
                "REGION",
                "VALUE",
                "INDEX_NAME",
            ]
        )

    df["DATE"] = pd.to_datetime(df["DATE"], errors="coerce")
    df = df.dropna(subset=["DATE"])

    df["REGION"] = df["REGION"].astype(str).str.strip()

    if index_name == "BDI":
        df = df[df["REGION"].isin(BDI_REGION_PRIORITY)]
    else:
        invalid_upper = {
            str(name).strip().upper()
            for name in INVALID_REGION_NAMES
        }
        df = df[
            ~df["REGION"].str.upper().isin(invalid_upper)
        ]

    df["YEAR"] = df["DATE"].dt.year

    return df[
        ["DATE", "YEAR", "REGION", "VALUE", "INDEX_NAME"]
    ].reset_index(drop=True)


def load_wide_df_from_db(index_name):
    long_df = load_long_df_from_db(index_name=index_name)

    if long_df.empty:
        return pd.DataFrame()

    pivot_df = (
        long_df.pivot_table(
            index="DATE",
            columns="REGION",
            values="VALUE",
            aggfunc="mean",
        )
        .sort_index()
        .reset_index()
    )
    pivot_df.columns.name = None

    return pivot_df


def get_last_update_meta(index_name):
    index_name = normalize_index_name(index_name)
    conn = get_db_conn()

    try:
        df = pd.read_sql_query(
            """
            SELECT *
            FROM crawl_audit
            WHERE index_name = ?
            ORDER BY audit_id DESC
            LIMIT 1
            """,
            conn,
            params=[index_name],
        )
    finally:
        conn.close()

    if df.empty:
        return None

    return df.iloc[0].to_dict()


def _normalize_db_value(value):
    value = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(value) else float(value)


def _db_values_equal(left, right):
    if left is None and right is None:
        return True

    if left is None or right is None:
        return False

    try:
        return abs(float(left) - float(right)) < 1e-12
    except (TypeError, ValueError):
        return left == right


def upsert_long_df_to_db(df_long, index_name, source_file=""):
    index_name = normalize_index_name(index_name)

    if df_long is None or df_long.empty:
        return 0, 0, get_total_row_count(index_name)

    normalized_rows = {}

    for row in df_long.itertuples(index=False):
        dt = pd.to_datetime(
            getattr(row, "DATE", None),
            errors="coerce",
        )

        if pd.isna(dt):
            continue

        region = str(
            getattr(row, "REGION", "")
        ).strip()

        if not is_valid_region_for_index(region, index_name):
            continue

        value = _normalize_db_value(
            getattr(row, "VALUE", None)
        )

        key = (dt.strftime("%Y-%m-%d"), region)
        normalized_rows[key] = value

    if not normalized_rows:
        return 0, 0, get_total_row_count(index_name)

    conn = get_db_conn()

    try:
        existing_rows = conn.execute(
            """
            SELECT dt, region, value
            FROM index_raw
            WHERE index_name = ?
            """,
            [index_name],
        ).fetchall()

        existing_map = {
            (str(dt), str(region).strip()): value
            for dt, region, value in existing_rows
        }

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        insert_rows = []
        update_rows = []

        for (dt, region), value in normalized_rows.items():
            key = (dt, region)

            if key not in existing_map:
                insert_rows.append(
                    (
                        dt,
                        region,
                        value,
                        index_name,
                        source_file,
                        now,
                        now,
                    )
                )
            elif not _db_values_equal(existing_map[key], value):
                update_rows.append(
                    (
                        value,
                        source_file,
                        now,
                        dt,
                        region,
                        index_name,
                    )
                )

        if insert_rows:
            conn.executemany(
                """
                INSERT INTO index_raw (
                    dt,
                    region,
                    value,
                    index_name,
                    source_file,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                insert_rows,
            )

        if update_rows:
            conn.executemany(
                """
                UPDATE index_raw
                SET value = ?,
                    source_file = ?,
                    updated_at = ?
                WHERE dt = ?
                  AND region = ?
                  AND index_name = ?
                """,
                update_rows,
            )

        conn.commit()

        total_rows = conn.execute(
            """
            SELECT COUNT(*)
            FROM index_raw
            WHERE index_name = ?
            """,
            [index_name],
        ).fetchone()[0]

        return (
            len(insert_rows),
            len(update_rows),
            int(total_rows),
        )

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


# =============================================================================
# Excel 처리
# =============================================================================

def get_output_search_dir(output_path):
    output_path = os.path.abspath(output_path or BASE_DIR)

    if os.path.isdir(output_path):
        return output_path

    parent = os.path.dirname(output_path)
    return parent or BASE_DIR


def find_latest_existing_file(search_dir, index_name):
    index_name = normalize_index_name(index_name)

    patterns = [
        os.path.join(search_dir, f"{index_name}_Index_*.xlsx"),
        os.path.join(search_dir, f"{index_name}_Index_*.xls"),
    ]

    files = []

    for pattern in patterns:
        files.extend(glob.glob(pattern))

    files = [
        path
        for path in files
        if ".tmp." not in os.path.basename(path)
        and not os.path.basename(path).startswith("~$")
    ]

    if not files:
        return None

    return max(files, key=os.path.getmtime)


def load_existing_raw(file_path, index_name):
    index_name = normalize_index_name(index_name)

    try:
        if not file_path or not os.path.exists(file_path):
            return None

        log(
            f"[{index_name}] 기존 Excel 파일 로드: "
            f"{os.path.basename(file_path)}"
        )

        df = pd.read_excel(
            file_path,
            sheet_name=index_name,
        )

        return None if df.empty else df

    except Exception as exc:
        logger.warning(
            "[%s] 기존 Excel 파일 파싱 실패: %s",
            index_name,
            exc,
        )
        return None


def get_last_date_from_df(df):
    if df is None or df.empty:
        return None

    first_column = df.columns[0]
    dates = pd.to_datetime(
        df[first_column],
        errors="coerce",
    ).dropna()

    if dates.empty:
        return None

    return dates.max().date()


def get_first_date_value(df):
    if df is None or df.empty:
        return None

    first_column = df.columns[0]

    for value in df[first_column].dropna().tolist():
        if isinstance(value, (datetime, date)):
            return pd.to_datetime(value).strftime("%Y-%m-%d")

        text = str(value).strip()
        if text:
            return text

    return None


def merge_with_existing(existing_df, new_df):
    if existing_df is None or existing_df.empty:
        return new_df

    if new_df is None or new_df.empty:
        return existing_df

    combined = pd.concat(
        [new_df, existing_df],
        ignore_index=True,
        sort=False,
    )

    first_column = combined.columns[0]
    combined["__sort_dt"] = pd.to_datetime(
        combined[first_column],
        errors="coerce",
    )

    combined = combined.drop_duplicates(
        subset=[first_column],
        keep="first",
    )
    combined = combined.sort_values(
        "__sort_dt",
        ascending=True,
    )
    combined = combined.drop(columns=["__sort_dt"])

    return combined.reset_index(drop=True)


def _build_output_path(base_output, index_name):
    index_name = normalize_index_name(index_name)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{index_name}_Index_{timestamp}.xlsx"

    base_output = os.path.abspath(base_output or BASE_DIR)

    if os.path.isdir(base_output):
        return os.path.join(base_output, filename)

    if base_output.lower().endswith(".xlsx"):
        return base_output

    os.makedirs(base_output, exist_ok=True)
    return os.path.join(base_output, filename)


def _melt_for_pivot(df, index_name):
    index_name = normalize_index_name(index_name)

    empty_columns = [
        "DATE",
        "YEAR",
        "REGION",
        "VALUE",
        "INDEX_NAME",
    ]

    if df is None or df.empty:
        return pd.DataFrame(columns=empty_columns)

    work = df.copy()
    date_column = work.columns[0]

    work[date_column] = pd.to_datetime(
        work[date_column],
        errors="coerce",
    )
    work = work.dropna(subset=[date_column])

    excluded = {
        date_column,
        "INDEX_NAME",
        "CRAWL_PAGE",
    }

    value_columns = [
        column
        for column in work.columns
        if column not in excluded
        and is_valid_region_for_index(column, index_name)
    ]

    numeric_columns = []

    for column in value_columns:
        converted = pd.to_numeric(
            work[column],
            errors="coerce",
        )

        if converted.notna().any():
            work[column] = converted
            numeric_columns.append(column)

    if not numeric_columns:
        return pd.DataFrame(columns=empty_columns)

    melted = work.melt(
        id_vars=[date_column],
        value_vars=numeric_columns,
        var_name="REGION",
        value_name="VALUE",
    )

    melted = melted.rename(
        columns={date_column: "DATE"}
    )
    melted["DATE"] = pd.to_datetime(
        melted["DATE"]
    ).dt.normalize()
    melted["YEAR"] = melted["DATE"].dt.year
    melted["INDEX_NAME"] = index_name

    melted = melted.dropna(subset=["VALUE"])
    melted["REGION"] = (
        melted["REGION"].astype(str).str.strip()
    )

    if index_name == "BDI":
        melted = melted[
            melted["REGION"].isin(BDI_REGION_PRIORITY)
        ]

    return (
        melted[
            [
                "DATE",
                "YEAR",
                "REGION",
                "VALUE",
                "INDEX_NAME",
            ]
        ]
        .sort_values(["DATE", "REGION"])
        .reset_index(drop=True)
    )


def _write_excel_file(df, output_path, index_name):
    index_name = normalize_index_name(index_name)

    with pd.ExcelWriter(
        output_path,
        engine="openpyxl",
    ) as writer:
        df.to_excel(
            writer,
            index=False,
            sheet_name=index_name,
        )

        long_df = _melt_for_pivot(
            df,
            index_name=index_name,
        )

        if not long_df.empty:
            long_df.to_excel(
                writer,
                index=False,
                sheet_name=f"{index_name}_Long",
            )

            summary = (
                long_df.pivot_table(
                    index="DATE",
                    columns="REGION",
                    values="VALUE",
                    aggfunc="mean",
                )
                .sort_index()
                .reset_index()
            )

            summary.to_excel(
                writer,
                index=False,
                sheet_name="Summary",
            )


def save_to_excel(df, base_output, index_name):
    index_name = normalize_index_name(index_name)

    if df is None or df.empty:
        return None

    output_path = _build_output_path(
        base_output,
        index_name,
    )
    output_directory = os.path.dirname(
        os.path.abspath(output_path)
    )
    os.makedirs(output_directory, exist_ok=True)

    temporary_path = f"{output_path}.tmp.xlsx"

    try:
        _write_excel_file(
            df,
            temporary_path,
            index_name,
        )

        os.replace(temporary_path, output_path)

        log(
            f"[{index_name}] Excel 저장 완료: "
            f"{os.path.basename(output_path)}"
        )
        return output_path

    except Exception as exc:
        if os.path.exists(temporary_path):
            try:
                os.remove(temporary_path)
            except OSError:
                pass

        logger.exception(
            "[%s] Excel 저장 실패: %s",
            index_name,
            exc,
        )
        return None


# =============================================================================
# Selenium 크롤러
# =============================================================================

def get_driver(driver_path="", headless=True):
    options = webdriver.ChromeOptions()
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--lang=ko-KR")
    options.add_argument("--window-size=1600,1000")
    options.page_load_strategy = "normal"

    if headless:
        options.add_argument("--headless=new")

    if driver_path:
        log(f"[Driver] 지정 경로 사용: {driver_path}")
        return webdriver.Chrome(
            service=Service(driver_path),
            options=options,
        )

    if HAS_WDM:
        try:
            log("[Driver] webdriver-manager 사용")
            installed_path = ChromeDriverManager().install()

            executable_name = (
                "chromedriver.exe"
                if os.name == "nt"
                else "chromedriver"
            )

            if os.path.basename(installed_path) != executable_name:
                candidate = os.path.join(
                    os.path.dirname(installed_path),
                    executable_name,
                )
                if os.path.exists(candidate):
                    installed_path = candidate

            return webdriver.Chrome(
                service=Service(installed_path),
                options=options,
            )

        except Exception as exc:
            logger.warning(
                "webdriver-manager 사용 실패: %s",
                exc,
            )

    return webdriver.Chrome(options=options)


def handle_login_alerts(driver, timeout=1.0, max_alerts=3):
    for _ in range(max_alerts):
        try:
            WebDriverWait(driver, timeout).until(
                EC.alert_is_present()
            )
            alert = driver.switch_to.alert
            log(f"로그인 알림: {alert.text}")
            alert.accept()
            time.sleep(0.1)

        except (TimeoutException, NoAlertPresentException):
            return


def login(driver, user_id, password):
    if not user_id or not password:
        raise RuntimeError(
            "KSG 로그인 정보가 없습니다. "
            "KSG_ID와 KSG_PW 환경변수를 설정하세요."
        )

    driver.get(BASE_URL)
    wait = WebDriverWait(driver, 15)

    wait.until(
        EC.element_to_be_clickable(
            (By.LINK_TEXT, "로그인")
        )
    ).click()

    id_input = wait.until(
        EC.presence_of_element_located(
            (By.ID, "loginId")
        )
    )
    password_input = wait.until(
        EC.presence_of_element_located(
            (By.ID, "loginPass")
        )
    )

    id_input.clear()
    id_input.send_keys(user_id)

    password_input.clear()
    password_input.send_keys(password)

    driver.execute_script(
        """
        if (typeof loginSubmitForm !== 'function') {
            throw new Error(
                'loginSubmitForm 함수를 찾을 수 없습니다.'
            );
        }
        loginSubmitForm(document.form1);
        """
    )

    handle_login_alerts(driver)
    time.sleep(0.8)

    login_inputs = driver.find_elements(By.ID, "loginId")
    logout_elements = driver.find_elements(
        By.XPATH,
        "//*[contains(normalize-space(text()), '로그아웃')]",
    )

    if login_inputs and not logout_elements:
        raise RuntimeError(
            "로그인 성공 여부를 확인할 수 없습니다. "
            "아이디, 비밀번호 또는 사이트 구조를 확인하세요."
        )

    log("로그인이 완료되었습니다.")


def go_to_index_page(driver, index_name):
    index_name = normalize_index_name(index_name)
    wait = WebDriverWait(driver, 15)

    wait.until(
        EC.element_to_be_clickable(
            (By.LINK_TEXT, index_name)
        )
    ).click()

    time.sleep(0.8)

    try:
        wait.until(
            EC.presence_of_element_located(
                (
                    By.CSS_SELECTOR,
                    "table, .paginate, .paging, .pagination",
                )
            )
        )
    except TimeoutException:
        pass

    log(f"{index_name} 지수 페이지 진입 완료")


def parse_current_table_fast(driver, index_name):
    index_name = normalize_index_name(index_name)

    script = """
    const indexName = arguments[0];
    const tables = Array.from(document.querySelectorAll('table'));
    let target = null;

    for (const table of tables) {
        const caption =
            (table.querySelector('caption')?.innerText || '')
            .trim().toUpperCase();

        const text =
            (table.innerText || '').trim().toUpperCase();

        if (
            table.querySelector('tbody tr') &&
            (
                caption.includes(indexName) ||
                text.includes(indexName) ||
                text.includes('일자')
            )
        ) {
            target = table;
            break;
        }
    }

    if (!target) {
        let maximumRows = 0;

        for (const table of tables) {
            const rowCount =
                table.querySelectorAll('tbody tr').length;

            if (rowCount > maximumRows) {
                maximumRows = rowCount;
                target = table;
            }
        }
    }

    if (!target) {
        return {headers: [], rows: []};
    }

    const headerRows =
        Array.from(target.querySelectorAll('thead tr'));

    let headers = [];

    if (headerRows.length) {
        headers = Array.from(
            headerRows[headerRows.length - 1]
                .querySelectorAll('th, td')
        ).map(element =>
            (element.innerText || '').trim()
        );
    }

    const rows = Array.from(
        target.querySelectorAll('tbody tr')
    ).map(row =>
        Array.from(
            row.querySelectorAll('th, td')
        ).map(cell =>
            (cell.innerText || '').trim()
        )
    ).filter(row => row.length > 0);

    return {headers, rows};
    """

    data = driver.execute_script(script, index_name)

    if not isinstance(data, dict):
        return pd.DataFrame()

    headers = data.get("headers", [])
    rows = data.get("rows", [])

    if not rows:
        return pd.DataFrame()

    maximum_length = max(len(row) for row in rows)
    headers = make_unique_headers(headers, maximum_length)

    normalized_rows = []

    for row_values in rows:
        row_values = list(row_values) + [""] * (
            maximum_length - len(row_values)
        )

        row = {}

        for index, value in enumerate(row_values):
            column = headers[index]

            row[column] = (
                parse_date(value)
                if index == 0
                else clean_number(value)
            )

        row["INDEX_NAME"] = index_name
        normalized_rows.append(row)

    return pd.DataFrame(normalized_rows)


def get_table_quick_signature(driver, index_name):
    index_name = normalize_index_name(index_name)

    try:
        script = """
        const indexName = arguments[0];
        const tables = Array.from(
            document.querySelectorAll('table')
        );

        let target = null;

        for (const table of tables) {
            const caption =
                (table.querySelector('caption')?.innerText || '')
                .trim().toUpperCase();

            const text =
                (table.innerText || '').trim().toUpperCase();

            if (
                table.querySelector('tbody tr') &&
                (
                    caption.includes(indexName) ||
                    text.includes(indexName) ||
                    text.includes('일자')
                )
            ) {
                target = table;
                break;
            }
        }

        if (!target) {
            let maximumRows = 0;

            for (const table of tables) {
                const count =
                    table.querySelectorAll('tbody tr').length;

                if (count > maximumRows) {
                    maximumRows = count;
                    target = table;
                }
            }
        }

        const selectors = [
            '.paginate strong',
            '.paging strong',
            '.pagination .active a',
            '.pagination strong',
            '.paginate .on'
        ];

        let currentPage = '';

        for (const selector of selectors) {
            const element = document.querySelector(selector);

            if (
                element &&
                /^\\d+$/.test(
                    (element.innerText || '').trim()
                )
            ) {
                currentPage =
                    (element.innerText || '').trim();
                break;
            }
        }

        let rowSignature = '';

        if (target) {
            const row = target.querySelector('tbody tr');
            const cells = row
                ? Array.from(
                    row.querySelectorAll('th, td')
                ).slice(0, 4)
                : [];

            rowSignature = cells.map(cell =>
                (cell.innerText || '').trim()
            ).join('|');
        }

        return currentPage + '||' + rowSignature;
        """

        value = driver.execute_script(script, index_name)
        return str(value or "").strip()

    except Exception:
        return ""


def wait_for_table_change(
    driver,
    previous_signature,
    index_name,
    timeout=8,
):
    started_at = time.time()

    while time.time() - started_at < timeout:
        current_signature = get_table_quick_signature(
            driver,
            index_name,
        )

        if (
            current_signature
            and current_signature != previous_signature
        ):
            return True

        time.sleep(0.2)

    return False


def _try_click(driver, element):
    try:
        if not element.is_displayed():
            return False

        driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});",
            element,
        )
        driver.execute_script(
            "arguments[0].click();",
            element,
        )
        return True

    except Exception:
        return False


def _get_current_page_number(driver):
    selectors = [
        (By.CSS_SELECTOR, ".paginate strong"),
        (By.CSS_SELECTOR, ".paging strong"),
        (By.CSS_SELECTOR, ".pagination .active a"),
        (By.CSS_SELECTOR, ".pagination strong"),
        (By.CSS_SELECTOR, ".paginate .on"),
    ]

    for by, selector in selectors:
        try:
            for element in driver.find_elements(by, selector):
                text = str(element.text or "").strip()

                if text.isdigit():
                    return int(text)

        except Exception:
            continue

    return None


def move_to_next_page(driver):
    current_page = _get_current_page_number(driver)

    if current_page is not None:
        target_page = current_page + 1

        try:
            has_go_page = driver.execute_script(
                "return typeof goPage === 'function';"
            )

            if has_go_page:
                driver.execute_script(
                    "goPage(arguments[0]);",
                    target_page,
                )
                return True

        except Exception:
            pass

        xpaths = [
            f"//a[normalize-space(text())='{target_page}']",
            (
                f"//span[normalize-space(text())="
                f"'{target_page}']/parent::a"
            ),
        ]

        for xpath in xpaths:
            try:
                for element in driver.find_elements(
                    By.XPATH,
                    xpath,
                ):
                    if _try_click(driver, element):
                        return True
            except Exception:
                continue

    candidates = [
        (By.CSS_SELECTOR, "a.next"),
        (By.CSS_SELECTOR, ".paginate a.next"),
        (By.CSS_SELECTOR, ".paging a.next"),
        (By.CSS_SELECTOR, ".pagination a.next"),
        (By.LINK_TEXT, "다음"),
        (By.XPATH, "//a[contains(normalize-space(.), '다음')]"),
    ]

    for by, selector in candidates:
        try:
            for element in driver.find_elements(by, selector):
                if _try_click(driver, element):
                    return True
        except Exception:
            continue

    return False


def crawl_all_pages(
    driver,
    index_name,
    max_pages=100000,
    pause=0.2,
    stop_date=None,
):
    index_name = normalize_index_name(index_name)
    max_pages = min(max(1, int(max_pages)), 100000)
    pause = min(max(0.0, float(pause)), 10.0)

    log(
        f"[{index_name}] 수집 시작 "
        f"(증분 기준일: {stop_date})"
    )

    all_frames = []
    seen_signatures = set()

    for page_number in range(1, max_pages + 1):
        df = pd.DataFrame()

        for _ in range(4):
            try:
                df = parse_current_table_fast(
                    driver,
                    index_name,
                )
                if not df.empty:
                    break
            except Exception:
                pass

            time.sleep(0.4)

        if df.empty:
            log(
                f"[{index_name}] {page_number}페이지: "
                "수집 데이터 없음"
            )
            break

        signature = (
            get_table_quick_signature(driver, index_name)
            or get_first_date_value(df)
            or f"page-{page_number}"
        )

        if signature in seen_signatures:
            log(
                f"[{index_name}] 중복 페이지 감지: "
                f"{page_number}"
            )
            break

        seen_signatures.add(signature)

        should_stop = False

        if stop_date is not None:
            first_column = df.columns[0]
            parsed_dates = pd.to_datetime(
                df[first_column],
                errors="coerce",
            ).dt.date

            has_existing_date = (
                parsed_dates.notna()
                & (parsed_dates <= stop_date)
            ).any()

            if has_existing_date:
                df = df[
                    parsed_dates.notna()
                    & (parsed_dates > stop_date)
                ].copy()
                should_stop = True

        if not df.empty:
            df["CRAWL_PAGE"] = page_number
            all_frames.append(df)

            log(
                f"[{index_name}] {page_number}페이지 완료: "
                f"+{len(df)}행"
            )

        if should_stop:
            log(
                f"[{index_name}] 증분 기준일 발견, 수집 종료"
            )
            break

        if not move_to_next_page(driver):
            break

        if not wait_for_table_change(
            driver,
            signature,
            index_name,
            timeout=8,
        ):
            log(
                f"[{index_name}] 페이지 변경 확인 실패"
            )
            break

        if pause:
            time.sleep(pause)

    if not all_frames:
        return pd.DataFrame()

    return pd.concat(
        all_frames,
        ignore_index=True,
        sort=False,
    )


# =============================================================================
# 동기화
# =============================================================================

def run_incremental_sync(config, index_name, force_full=False):
    index_name = normalize_index_name(index_name)

    if config is None:
        return {
            "ok": False,
            "error": "런타임 설정이 초기화되지 않았습니다.",
        }

    if not config.user or not config.password:
        return {
            "ok": False,
            "error": (
                "KSG_ID와 KSG_PW 환경변수를 설정하세요."
            ),
        }

    with crawl_lock:
        started_at = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        before_total = get_total_row_count(index_name)

        conn = get_db_conn()

        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO crawl_audit (
                    started_at,
                    status,
                    message,
                    rows_added,
                    rows_updated,
                    total_rows,
                    index_name
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    started_at,
                    "RUNNING",
                    "",
                    0,
                    0,
                    before_total,
                    index_name,
                ),
            )
            audit_id = cursor.lastrowid
            conn.commit()
        finally:
            conn.close()

        driver = None

        try:
            existing_df = None
            stop_date = None

            if config.incremental and not force_full:
                wide_df = load_wide_df_from_db(index_name)

                if not wide_df.empty:
                    existing_df = wide_df.rename(
                        columns={"DATE": "일자"}
                    )
                    stop_date = get_last_date_from_df(
                        existing_df
                    )

                    if stop_date:
                        log(
                            f"[{index_name}] DB 기준 최종일: "
                            f"{stop_date}"
                        )

                if existing_df is None or existing_df.empty:
                    search_directory = get_output_search_dir(
                        config.output
                    )
                    latest_file = find_latest_existing_file(
                        search_directory,
                        index_name,
                    )

                    if latest_file:
                        existing_df = load_existing_raw(
                            latest_file,
                            index_name,
                        )
                        stop_date = get_last_date_from_df(
                            existing_df
                        )

                        if stop_date:
                            log(
                                f"[{index_name}] Excel 기준 "
                                f"최종일: {stop_date}"
                            )

            driver = get_driver(
                driver_path=config.driver_path,
                headless=config.headless,
            )

            login(
                driver,
                config.user,
                config.password,
            )
            go_to_index_page(driver, index_name)

            new_df = crawl_all_pages(
                driver,
                index_name=index_name,
                max_pages=config.max_pages,
                pause=config.pause,
                stop_date=stop_date,
            )

            final_df = merge_with_existing(
                existing_df,
                new_df,
            )

            if final_df is None or final_df.empty:
                raise RuntimeError(
                    f"{index_name}에서 저장 가능한 데이터를 "
                    "수집하지 못했습니다."
                )

            output_path = save_to_excel(
                final_df,
                config.output,
                index_name,
            )

            long_df = _melt_for_pivot(
                final_df,
                index_name,
            )

            rows_added, rows_updated, _ = (
                upsert_long_df_to_db(
                    long_df,
                    index_name=index_name,
                    source_file=(
                        os.path.basename(output_path)
                        if output_path
                        else ""
                    ),
                )
            )

            cleanup_invalid_regions()
            total_rows = get_total_row_count(index_name)

            conn = get_db_conn()

            try:
                conn.execute(
                    """
                    UPDATE crawl_audit
                    SET finished_at = ?,
                        status = ?,
                        message = ?,
                        rows_added = ?,
                        rows_updated = ?,
                        total_rows = ?
                    WHERE audit_id = ?
                    """,
                    (
                        datetime.now().strftime(
                            "%Y-%m-%d %H:%M:%S"
                        ),
                        "SUCCESS",
                        "OK",
                        rows_added,
                        rows_updated,
                        total_rows,
                        audit_id,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            return {
                "ok": True,
                "rows_added": rows_added,
                "rows_updated": rows_updated,
                "total_rows": total_rows,
                "output_file": (
                    os.path.basename(output_path)
                    if output_path
                    else ""
                ),
            }

        except Exception as exc:
            logger.exception(
                "[%s] 크롤러 실행 예외: %s",
                index_name,
                exc,
            )

            total_rows = get_total_row_count(index_name)
            conn = get_db_conn()

            try:
                conn.execute(
                    """
                    UPDATE crawl_audit
                    SET finished_at = ?,
                        status = ?,
                        message = ?,
                        rows_added = 0,
                        rows_updated = 0,
                        total_rows = ?
                    WHERE audit_id = ?
                    """,
                    (
                        datetime.now().strftime(
                            "%Y-%m-%d %H:%M:%S"
                        ),
                        "ERROR",
                        str(exc)[:1000],
                        total_rows,
                        audit_id,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            return {
                "ok": False,
                "error": str(exc),
            }

        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass


def _sync_worker(index_name):
    try:
        result = run_incremental_sync(
            RUNTIME_CONFIG,
            index_name,
        )

        if result.get("ok"):
            status = "SUCCESS"
            message = (
                f"{index_name} 동기화 완료: "
                f"신규 {result.get('rows_added', 0):,}건, "
                f"수정 {result.get('rows_updated', 0):,}건, "
                f"전체 {result.get('total_rows', 0):,}건"
            )
        else:
            status = "ERROR"
            message = (
                f"{index_name} 동기화 실패: "
                f"{result.get('error', '')}"
            )

        with sync_state_lock:
            SYNC_STATE.update(
                {
                    "status": status,
                    "finished_at": datetime.now().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    "message": message,
                    "result": result,
                }
            )

    except Exception as exc:
        logger.exception(
            "백그라운드 동기화 실패: %s",
            exc,
        )

        with sync_state_lock:
            SYNC_STATE.update(
                {
                    "status": "ERROR",
                    "finished_at": datetime.now().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    "message": f"동기화 실패: {exc}",
                    "result": {
                        "ok": False,
                        "error": str(exc),
                    },
                }
            )


def start_sync_job(index_name):
    index_name = normalize_index_name(index_name)

    with sync_state_lock:
        if SYNC_STATE["status"] == "RUNNING":
            return (
                False,
                (
                    f"{SYNC_STATE.get('index_name')} 동기화가 "
                    "이미 진행 중입니다."
                ),
            )

        SYNC_STATE.update(
            {
                "status": "RUNNING",
                "index_name": index_name,
                "started_at": datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "finished_at": None,
                "message": f"{index_name} 동기화를 시작했습니다.",
                "result": None,
            }
        )

    thread = Thread(
        target=_sync_worker,
        args=(index_name,),
        daemon=True,
    )
    thread.start()

    return True, f"{index_name} 동기화를 시작했습니다."


# =============================================================================
# 필터, 차트 및 다운로드
# =============================================================================

def filter_long_df(
    df_long,
    start_date=None,
    end_date=None,
    years=None,
    regions=None,
):
    if df_long is None or df_long.empty:
        return df_long

    work = df_long.copy()
    work["DATE"] = pd.to_datetime(
        work["DATE"],
        errors="coerce",
    )
    work = work.dropna(subset=["DATE"])

    if start_date:
        work = work[
            work["DATE"] >= pd.to_datetime(start_date)
        ]

    if end_date:
        work = work[
            work["DATE"] <= pd.to_datetime(end_date)
        ]

    if years:
        valid_years = sorted({
            int(year)
            for year in years
            if str(year).isdigit()
        })

        if valid_years:
            work = work[
                work["DATE"].dt.year.isin(valid_years)
            ]

    if regions:
        valid_regions = [
            str(region).strip()
            for region in regions
            if str(region).strip()
        ]

        if valid_regions:
            work = work[
                work["REGION"].isin(valid_regions)
            ]

    work["YEAR"] = work["DATE"].dt.year

    return work.sort_values(
        ["DATE", "REGION"]
    ).reset_index(drop=True)


def get_palette_colors(name):
    palettes = {
        "Auto": [
            "#e53935",
            "#fb8c00",
            "#fdd835",
            "#43a047",
            "#00acc1",
            "#1e88e5",
            "#3949ab",
            "#8e24aa",
            "#d81b60",
            "#6d4c41",
            "#00897b",
            "#7cb342",
        ],
        "Grey": [
            "#111827",
            "#1f2937",
            "#374151",
            "#4b5563",
            "#6b7280",
            "#9ca3af",
            "#d1d5db",
            "#e5e7eb",
        ],
        "Red": [
            "#7f1d1d",
            "#991b1b",
            "#b91c1c",
            "#dc2626",
            "#ef4444",
            "#f87171",
            "#fb7185",
            "#ec4899",
            "#f97316",
        ],
        "Blue": [
            "#0f172a",
            "#1d4ed8",
            "#2563eb",
            "#3b82f6",
            "#60a5fa",
            "#0ea5e9",
            "#38bdf8",
            "#0284c7",
            "#1e3a8a",
        ],
    }

    return palettes.get(
        normalize_palette_name(name),
        palettes["Auto"],
    )


def build_auto_color_map(regions, palette_name="Auto"):
    colors = get_palette_colors(palette_name)

    return {
        region: colors[index % len(colors)]
        for index, region in enumerate(regions)
    }


def safe_color_key(region):
    return "color_" + re.sub(
        r"[^a-zA-Z0-9가-힣_]+",
        "_",
        str(region),
    )


def extract_user_color_map(regions, req):
    result = {}

    for region in regions:
        value = req.args.get(
            safe_color_key(region),
            "",
        ).strip()

        if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            result[region] = value

    return result


def build_final_color_map(
    regions,
    palette_name,
    user_color_map,
):
    automatic = build_auto_color_map(
        regions,
        palette_name,
    )

    return {
        region: user_color_map.get(region)
        or automatic.get(region)
        for region in regions
    }


def build_peak_trough_html(
    df_long,
    selected_index,
    selected_regions=None,
    sort_by="region",
):
    if df_long is None or df_long.empty:
        return "-"

    regions = (
        selected_regions
        if selected_regions
        else sort_regions_for_display(
            df_long["REGION"]
            .dropna()
            .astype(str)
            .unique()
            .tolist(),
            selected_index,
        )
    )

    records = []

    for region in regions:
        if not is_valid_region_for_index(
            region,
            selected_index,
        ):
            continue

        subset = df_long[
            df_long["REGION"] == region
        ].copy()

        subset["VALUE"] = pd.to_numeric(
            subset["VALUE"],
            errors="coerce",
        )
        subset = subset.dropna(
            subset=["DATE", "VALUE"]
        )

        if subset.empty:
            continue

        peak = subset.loc[subset["VALUE"].idxmax()]
        trough = subset.loc[subset["VALUE"].idxmin()]

        records.append(
            {
                "region": str(region),
                "label": display_region_label(
                    region,
                    selected_index,
                ),
                "peak_date": pd.to_datetime(
                    peak["DATE"]
                ).strftime("%Y-%m-%d"),
                "peak_value": float(peak["VALUE"]),
                "trough_date": pd.to_datetime(
                    trough["DATE"]
                ).strftime("%Y-%m-%d"),
                "trough_value": float(
                    trough["VALUE"]
                ),
                "spread": float(
                    peak["VALUE"] - trough["VALUE"]
                ),
            }
        )

    if not records:
        return "-"

    if sort_by == "peak_desc":
        records.sort(
            key=lambda row: (
                -row["peak_value"],
                row["region"],
            )
        )
    elif sort_by == "trough_asc":
        records.sort(
            key=lambda row: (
                row["trough_value"],
                row["region"],
            )
        )
    elif sort_by == "spread_desc":
        records.sort(
            key=lambda row: (
                -row["spread"],
                row["region"],
            )
        )
    elif normalize_index_name(selected_index) == "BDI":
        priority = {
            name: index
            for index, name in enumerate(
                BDI_REGION_PRIORITY
            )
        }
        records.sort(
            key=lambda row: (
                priority.get(row["region"], 999),
                row["region"],
            )
        )
    else:
        records.sort(key=lambda row: row["region"])

    rows = []

    for row in records:
        label = escape(row["label"])
        peak_date = escape(row["peak_date"])
        trough_date = escape(row["trough_date"])

        rows.append(
            f"""
            <tr>
                <td><span class="route-badge">{label}</span></td>
                <td>
                    <span class="mini-badge peak">최고</span>
                    {peak_date}
                </td>
                <td class="number peak-text">
                    {row["peak_value"]:,.2f}
                </td>
                <td>
                    <span class="mini-badge trough">최저</span>
                    {trough_date}
                </td>
                <td class="number trough-text">
                    {row["trough_value"]:,.2f}
                </td>
                <td class="number spread-text">
                    {row["spread"]:,.2f}
                </td>
            </tr>
            """
        )

    return f"""
    <div class="table-wrap">
        <table class="data-table">
            <thead>
                <tr>
                    <th>항로 / 지수</th>
                    <th>최고 일자</th>
                    <th>최고 값</th>
                    <th>최저 일자</th>
                    <th>최저 값</th>
                    <th>스프레드</th>
                </tr>
            </thead>
            <tbody>
                {''.join(rows)}
            </tbody>
        </table>
    </div>
    """


def make_plot(
    df_long,
    selected_regions,
    line_width=2.5,
    palette="Auto",
    user_color_map=None,
):
    fig = go.Figure()
    palette = normalize_palette_name(palette)

    if df_long is None or df_long.empty:
        fig.update_layout(
            template="plotly_white",
            height=560,
            margin=dict(l=20, r=20, t=20, b=40),
            xaxis_title="Date",
            yaxis_title="Index",
        )
        return fig

    index_name = (
        df_long["INDEX_NAME"].iloc[0]
        if "INDEX_NAME" in df_long.columns
        else DEFAULT_INDEX_NAME
    )

    regions = (
        selected_regions
        if selected_regions
        else sort_regions_for_display(
            df_long["REGION"]
            .dropna()
            .astype(str)
            .unique()
            .tolist(),
            index_name,
        )
    )

    regions = [
        region
        for region in regions
        if is_valid_region_for_index(
            region,
            index_name,
        )
    ]

    color_map = build_final_color_map(
        regions,
        palette,
        user_color_map or {},
    )

    for region in regions:
        subset = df_long[
            df_long["REGION"] == region
        ].sort_values("DATE")

        if subset.empty:
            continue

        fig.add_trace(
            go.Scatter(
                x=subset["DATE"],
                y=subset["VALUE"],
                mode="lines",
                name=display_region_label(
                    region,
                    index_name,
                ),
                customdata=[region] * len(subset),
                line=dict(
                    color=color_map.get(region),
                    width=line_width,
                ),
                hovertemplate=(
                    "날짜=%{x|%Y-%m-%d}<br>"
                    "지수=%{customdata}<br>"
                    "값=%{y:,.2f}"
                    "<extra></extra>"
                ),
            )
        )

    fig.update_layout(
        template="plotly_white",
        height=620,
        margin=dict(l=20, r=20, t=20, b=40),
        xaxis_title="Date",
        yaxis_title="Index",
        hovermode="x unified",
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            x=0,
            font=dict(size=12),
        ),
        xaxis=dict(
            tickfont=dict(size=14),
            title_font=dict(size=16),
        ),
        yaxis=dict(
            tickfont=dict(size=14),
            title_font=dict(size=16),
            tickformat=",",
        ),
    )

    return fig


def make_download_response(
    df,
    filename,
    kind="csv",
    sheet_name="INDEX",
):
    if kind == "excel":
        output = BytesIO()

        with pd.ExcelWriter(
            output,
            engine="openpyxl",
        ) as writer:
            df.to_excel(
                writer,
                index=False,
                sheet_name=sheet_name[:31],
            )

        output.seek(0)

        return Response(
            output.getvalue(),
            mimetype=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{filename}"'
                )
            },
        )

    return Response(
        df.to_csv(
            index=False,
            encoding="utf-8-sig",
        ),
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{filename}"'
            )
        },
    )


# =============================================================================
# 웹 보안
# =============================================================================

def _basic_auth_required():
    return bool(
        os.getenv("DASHBOARD_USER")
        and os.getenv("DASHBOARD_PASSWORD")
    )


def _check_basic_auth():
    if not _basic_auth_required():
        return True

    auth = request.authorization

    if not auth:
        return False

    expected_user = os.getenv("DASHBOARD_USER", "")
    expected_password = os.getenv(
        "DASHBOARD_PASSWORD",
        "",
    )

    return (
        hmac.compare_digest(
            auth.username or "",
            expected_user,
        )
        and hmac.compare_digest(
            auth.password or "",
            expected_password,
        )
    )


@app.before_request
def security_before_request():
    if not _check_basic_auth():
        return Response(
            "Authentication required",
            status=401,
            headers={
                "WWW-Authenticate": (
                    'Basic realm="Shipping Dashboard"'
                )
            },
        )

    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)

    return None


@app.context_processor
def inject_csrf_token():
    return {
        "csrf_token": session.get("csrf_token", "")
    }


def validate_csrf_token():
    expected = session.get("csrf_token", "")
    received = request.form.get("csrf_token", "")

    return (
        bool(expected)
        and bool(received)
        and hmac.compare_digest(expected, received)
    )


# =============================================================================
# 요청 파라미터
# =============================================================================

def current_filter_params():
    selected_index = normalize_index_name(
        request.args.get(
            "index_name",
            DEFAULT_INDEX_NAME,
        )
    )

    start_date = normalize_date_arg(
        request.args.get("start_date", "")
    )
    end_date = normalize_date_arg(
        request.args.get("end_date", "")
    )

    if (
        start_date
        and end_date
        and start_date > end_date
    ):
        start_date, end_date = end_date, start_date

    year_start = request.args.get(
        "year_start",
        "",
    ).strip()
    year_end = request.args.get(
        "year_end",
        "",
    ).strip()

    regions = [
        str(region).strip()
        for region in request.args.getlist("regions")
        if is_valid_region_for_index(
            region,
            selected_index,
        )
    ]

    try:
        line_width_value = float(
            request.args.get(
                "line_width",
                "1.2",
            )
        )
        line_width_value = min(
            10.0,
            max(0.1, line_width_value),
        )
    except (TypeError, ValueError):
        line_width_value = 1.2

    line_width = str(line_width_value)

    palette = normalize_palette_name(
        request.args.get("palette", "Auto")
    )
    previous_palette = normalize_palette_name(
        request.args.get(
            "prev_palette",
            palette,
        )
    )

    peak_sort = request.args.get(
        "peak_sort",
        "region",
    ).strip()

    if peak_sort not in {
        "region",
        "peak_desc",
        "trough_asc",
        "spread_desc",
    }:
        peak_sort = "region"

    years = []

    if year_start.isdigit() and year_end.isdigit():
        start_year = int(year_start)
        end_year = int(year_end)

        if start_year > end_year:
            start_year, end_year = end_year, start_year

        years = list(
            range(start_year, end_year + 1)
        )

    return (
        selected_index,
        start_date,
        end_date,
        year_start,
        year_end,
        regions,
        years,
        line_width,
        palette,
        previous_palette,
        peak_sort,
    )


def build_query_string(
    selected_index,
    start_date,
    end_date,
    year_start,
    year_end,
    regions,
    line_width,
    palette,
    color_map,
    peak_sort="region",
):
    params = [("index_name", selected_index)]

    if start_date:
        params.append(("start_date", start_date))

    if end_date:
        params.append(("end_date", end_date))

    if str(year_start):
        params.append(("year_start", str(year_start)))

    if str(year_end):
        params.append(("year_end", str(year_end)))

    for region in regions:
        if is_valid_region_for_index(
            region,
            selected_index,
        ):
            params.append(("regions", region))

    params.append(("line_width", str(line_width)))
    params.append(("palette", palette))
    params.append(("prev_palette", palette))
    params.append(("peak_sort", peak_sort))

    for region, color in color_map.items():
        if (
            is_valid_region_for_index(
                region,
                selected_index,
            )
            and re.fullmatch(
                r"#[0-9a-fA-F]{6}",
                str(color or ""),
            )
        ):
            params.append(
                (safe_color_key(region), color)
            )

    return urlencode(params, doseq=True)


# =============================================================================
# HTML
# =============================================================================

HTML = r"""
<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Shipping Index Dashboard</title>
<style>
:root {
    --bg:#eef3f8;
    --surface:#fff;
    --surface2:#f8fafc;
    --border:#dbe4ee;
    --text:#1f2937;
    --muted:#64748b;
    --primary:#2563eb;
    --success:#16a34a;
    --danger:#dc2626;
    --shadow:0 8px 28px rgba(15,23,42,.08);
}
* { box-sizing:border-box; }
body {
    margin:0;
    font-family:"Segoe UI",Arial,sans-serif;
    background:linear-gradient(180deg,#f7fafc,var(--bg));
    color:var(--text);
}
.page-wrap {
    max-width:1600px;
    margin:auto;
    padding:24px;
}
.layout {
    display:grid;
    grid-template-columns:340px minmax(0,1fr);
    gap:22px;
    align-items:start;
}
.sidebar {
    position:sticky;
    top:20px;
    padding:22px 20px;
    border:1px solid var(--border);
    border-radius:20px;
    background:var(--surface);
    box-shadow:var(--shadow);
}
.main { min-width:0; }
.card,.main-header {
    margin-bottom:20px;
    padding:20px;
    border:1px solid var(--border);
    border-radius:18px;
    background:var(--surface);
    box-shadow:var(--shadow);
}
.main-header { padding:22px 24px; }
.main-title {
    margin:0 0 10px;
    color:#0f172a;
    font-size:32px;
}
.meta {
    color:var(--muted);
    font-size:13px;
    line-height:1.8;
}
.meta a {
    color:var(--primary);
    font-weight:700;
    text-decoration:none;
}
.sidebar-section {
    margin-bottom:18px;
    padding-bottom:18px;
    border-bottom:1px solid #e5edf5;
}
.sidebar-section:last-child {
    margin:0;
    padding:0;
    border:0;
}
.sidebar-label {
    margin-bottom:10px;
    color:#475569;
    font-size:12px;
    font-weight:800;
    letter-spacing:.5px;
    text-transform:uppercase;
}
.index-tabs {
    display:grid;
    grid-template-columns:1fr 1fr;
    gap:8px;
}
.index-tab,.btn {
    display:inline-flex;
    align-items:center;
    justify-content:center;
    min-height:40px;
    padding:9px 14px;
    border:1px solid #cbd5e1;
    border-radius:10px;
    background:#fff;
    color:#334155;
    font-size:13px;
    font-weight:800;
    text-decoration:none;
    cursor:pointer;
}
.index-tab.active,.btn:hover {
    border-color:var(--primary);
    background:var(--primary);
    color:#fff;
}
.btn.success {
    border-color:var(--success);
    color:var(--success);
}
.btn.success:hover {
    background:var(--success);
    color:#fff;
}
.action-group {
    display:flex;
    flex-wrap:wrap;
    gap:8px;
}
.field-block { margin-bottom:14px; }
.field-label {
    display:block;
    margin-bottom:6px;
    color:#334155;
    font-size:12px;
    font-weight:700;
}
input[type=date],input[type=number],select {
    width:100%;
    min-height:40px;
    padding:10px 12px;
    border:1px solid #cbd5e1;
    border-radius:10px;
    background:#fff;
}
input[type=color] {
    width:52px;
    min-height:34px;
    padding:2px;
    border:1px solid #cbd5e1;
    border-radius:8px;
}
.year-range {
    display:flex;
    flex-direction:column;
    gap:8px;
}
.range-values {
    color:var(--muted);
    font-size:12px;
}
.color-grid {
    display:grid;
    max-height:260px;
    gap:8px;
    overflow:auto;
    padding:10px;
    border:1px solid #e2e8f0;
    border-radius:10px;
    background:var(--surface2);
}
.color-item {
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:8px;
}
.color-item label {
    overflow:hidden;
    color:#334155;
    font-size:12px;
    text-overflow:ellipsis;
    white-space:nowrap;
}
.section-title {
    margin:0 0 14px;
    color:#0f172a;
    font-size:18px;
    font-weight:800;
}
.metrics-grid {
    display:grid;
    grid-template-columns:repeat(auto-fit,minmax(210px,1fr));
    gap:14px;
}
.insight-card {
    min-height:115px;
    padding:16px;
    border:1px solid #e5edf5;
    border-radius:14px;
    background:var(--surface2);
}
.insight-label {
    margin-bottom:8px;
    color:#64748b;
    font-size:12px;
    font-weight:800;
    text-transform:uppercase;
}
.insight-value {
    margin-bottom:6px;
    color:#0f172a;
    font-size:27px;
    font-weight:800;
}
.insight-desc {
    color:#475569;
    font-size:13px;
    line-height:1.5;
}
.chart-toolbar {
    display:flex;
    flex-wrap:wrap;
    align-items:flex-start;
    justify-content:space-between;
    gap:12px;
    margin-bottom:14px;
}
.toolbar-title {
    color:#334155;
    font-size:16px;
    font-weight:800;
}
.toolbar-subtitle {
    margin-top:4px;
    color:var(--muted);
    font-size:12px;
}
.download-group {
    display:flex;
    flex-wrap:wrap;
    gap:8px;
}
.download-btn {
    display:inline-flex;
    align-items:center;
    justify-content:center;
    min-height:38px;
    padding:8px 12px;
    border:1px solid #cbd5e1;
    border-radius:10px;
    color:#334155;
    font-size:12px;
    font-weight:700;
    text-decoration:none;
}
.download-btn:hover {
    border-color:var(--primary);
    background:var(--primary);
    color:#fff;
}
.notice {
    padding:12px 14px;
    border:1px solid #bfdbfe;
    border-radius:12px;
    background:#eff6ff;
    color:#1e3a8a;
    font-size:13px;
    font-weight:700;
}
.notice.error {
    border-color:#fecaca;
    background:#fef2f2;
    color:#991b1b;
}
.notice.success {
    border-color:#bbf7d0;
    background:#f0fdf4;
    color:#166534;
}
.table-wrap {
    width:100%;
    overflow:auto;
}
.data-table {
    width:100%;
    border-collapse:collapse;
    font-size:12px;
}
.data-table th,.data-table td {
    padding:10px 12px;
    border:1px solid #e5e7eb;
    white-space:nowrap;
}
.data-table th {
    background:#f1f5f9;
    color:#334155;
    text-align:left;
}
.number {
    text-align:right;
    font-weight:800;
    font-variant-numeric:tabular-nums;
}
.route-badge,.mini-badge {
    display:inline-flex;
    padding:4px 9px;
    border-radius:999px;
    font-size:11px;
    font-weight:700;
}
.route-badge {
    background:#e0f2fe;
    color:#075985;
}
.mini-badge { margin-right:7px; }
.mini-badge.peak {
    background:#fee2e2;
    color:#b91c1c;
}
.mini-badge.trough {
    background:#dbeafe;
    color:#1d4ed8;
}
.peak-text { color:#b91c1c; }
.trough-text { color:#1d4ed8; }
.spread-text { color:#7c3aed; }
.sync-running::after {
    content:"";
    display:inline-block;
    width:12px;
    height:12px;
    margin-left:8px;
    border:2px solid #93c5fd;
    border-top-color:#1d4ed8;
    border-radius:50%;
    animation:spin .8s linear infinite;
}
@keyframes spin { to { transform:rotate(360deg); } }
@media(max-width:1100px) {
    .layout { grid-template-columns:1fr; }
    .sidebar { position:static; }
}
@media(max-width:768px) {
    .page-wrap { padding:14px; }
    .main-title { font-size:25px; }
}
</style>
</head>
<body>
<div class="page-wrap">
<div class="layout">

<aside class="sidebar">
<form method="get" action="/">
<input type="hidden" name="index_name" value="{{ selected_index }}">
<input type="hidden" name="prev_palette" value="{{ palette }}">
<input type="hidden" name="csrf_token" value="{{ csrf_token }}">

<div class="sidebar-section">
    <div class="sidebar-label">Index</div>
    <div class="index-tabs">
        <a href="/?index_name=SCFI"
           class="index-tab {% if selected_index == 'SCFI' %}active{% endif %}">
           SCFI
        </a>
        <a href="/?index_name=BDI"
           class="index-tab {% if selected_index == 'BDI' %}active{% endif %}">
           BDI
        </a>
    </div>
</div>

<div class="sidebar-section">
    <div class="sidebar-label">Actions</div>
    <div class="action-group">
        <button class="btn" type="submit">Apply</button>
        <a class="btn" href="/?index_name={{ selected_index }}">Reset</a>
        <button class="btn success"
                type="submit"
                formaction="/refresh"
                formmethod="post">
            Refresh
        </button>
    </div>
</div>

<div class="sidebar-section">
    <div class="sidebar-label">Date Range</div>

    <div class="field-block">
        <label class="field-label">시작일</label>
        <input type="date" name="start_date" value="{{ start_date }}">
    </div>

    <div class="field-block">
        <label class="field-label">종료일</label>
        <input type="date" name="end_date" value="{{ end_date }}">
    </div>

    <div class="field-block">
        <label class="field-label">연도 범위</label>
        <div class="year-range">
            <input type="range"
                   id="year_start"
                   min="{{ year_min }}"
                   max="{{ year_max }}"
                   value="{{ year_start }}"
                   oninput="syncYears()">
            <input type="range"
                   id="year_end"
                   min="{{ year_min }}"
                   max="{{ year_max }}"
                   value="{{ year_end }}"
                   oninput="syncYears()">
        </div>

        <div class="range-values">
            선택 연도:
            <span id="yearLabel">{{ year_start }} ~ {{ year_end }}</span>
        </div>

        <input type="hidden"
               id="year_start_hidden"
               name="year_start"
               value="{{ year_start }}">

        <input type="hidden"
               id="year_end_hidden"
               name="year_end"
               value="{{ year_end }}">
    </div>
</div>

<div class="sidebar-section">
    <div class="sidebar-label">Routes / Indexes</div>
    <div class="field-block">
        <label class="field-label">복수 선택</label>
        <select name="regions" multiple size="10" style="min-height:220px">
            {% for region in all_regions %}
            <option value="{{ region }}"
                    {% if region in selected_regions %}selected{% endif %}>
                {{ region_display_map.get(region, region) }}
            </option>
            {% endfor %}
        </select>
    </div>
</div>

<div class="sidebar-section">
    <div class="sidebar-label">Chart Style</div>

    <div class="field-block">
        <label class="field-label">선 두께</label>
        <input type="number"
               name="line_width"
               value="{{ line_width }}"
               step="0.1"
               min="0.1"
               max="10">
    </div>

    <div class="field-block">
        <label class="field-label">색상 테마</label>
        <select name="palette">
            {% for value in palette_options %}
            <option value="{{ value }}"
                    {% if palette == value %}selected{% endif %}>
                {{ value }}
            </option>
            {% endfor %}
        </select>
    </div>

    <div class="field-block">
        <label class="field-label">개별 색상</label>
        <div class="color-grid">
            {% for region in selected_regions_for_color %}
            <div class="color-item">
                <label title="{{ region_display_map.get(region, region) }}">
                    {{ region_display_map.get(region, region) }}
                </label>
                <input type="color"
                       name="{{ color_keys[region] }}"
                       value="{{ color_values.get(region, auto_color_map.get(region, '#636EFA')) }}">
            </div>
            {% endfor %}
        </div>
    </div>
</div>

<div class="sidebar-section">
    <div class="sidebar-label">Highlight Sorting</div>
    <select name="peak_sort">
        <option value="region"
                {% if peak_sort == 'region' %}selected{% endif %}>
            항로명
        </option>
        <option value="peak_desc"
                {% if peak_sort == 'peak_desc' %}selected{% endif %}>
            최고값 내림차순
        </option>
        <option value="trough_asc"
                {% if peak_sort == 'trough_asc' %}selected{% endif %}>
            최저값 오름차순
        </option>
        <option value="spread_desc"
                {% if peak_sort == 'spread_desc' %}selected{% endif %}>
            스프레드 내림차순
        </option>
    </select>
</div>

</form>
</aside>

<main class="main">

<div class="main-header">
    <h1 class="main-title">Shipping Index Dashboard</h1>
    <div class="meta">
        <strong>Source:</strong>
        <a href="https://www.ksg.co.kr/"
           target="_blank"
           rel="noopener noreferrer">
            Korea Shipping Gazette
        </a>
        &nbsp;|&nbsp;
        <strong>Index:</strong> {{ selected_index }}
        &nbsp;|&nbsp;
        <strong>Last sync:</strong> {{ last_sync }}
        &nbsp;|&nbsp;
        <strong>Total points:</strong> {{ total_points }}
    </div>
</div>

{% if refresh_message %}
<div class="card">
    <div id="syncNotice"
         class="notice {% if sync_status == 'RUNNING' %}sync-running{% elif sync_status == 'ERROR' %}error{% elif sync_status == 'SUCCESS' %}success{% endif %}">
        {{ refresh_message }}
    </div>
</div>
{% endif %}

<div class="card">
    <div class="section-title">Current Highlights</div>
    <div class="metrics-grid">
        <div class="insight-card">
            <div class="insight-label">조회 시작일</div>
            <div class="insight-value">{{ shown_start }}</div>
            <div class="insight-desc">필터링된 데이터의 시작일입니다.</div>
        </div>
        <div class="insight-card">
            <div class="insight-label">조회 종료일</div>
            <div class="insight-value">{{ shown_end }}</div>
            <div class="insight-desc">필터링된 데이터의 종료일입니다.</div>
        </div>
        <div class="insight-card">
            <div class="insight-label">선택 항목 수</div>
            <div class="insight-value">{{ region_count }}</div>
            <div class="insight-desc">현재 차트에 표시된 항목 수입니다.</div>
        </div>
        <div class="insight-card">
            <div class="insight-label">표시 데이터 수</div>
            <div class="insight-value">{{ shown_rows }}</div>
            <div class="insight-desc">차트에 반영된 데이터 포인트 수입니다.</div>
        </div>
    </div>
</div>

<div class="card">
    <div class="chart-toolbar">
        <div>
            <div class="toolbar-title">항로별 비교 차트</div>
            <div class="toolbar-subtitle">
                선택 항목을 한 화면에서 비교합니다.
            </div>
        </div>

        <div class="download-group">
            <a class="download-btn" href="{{ download_csv_url }}">CSV</a>
            <a class="download-btn" href="{{ download_xlsx_url }}">XLSX</a>
            <a class="download-btn" href="{{ download_chart_html_url }}">Chart HTML</a>
            <a class="download-btn" href="{{ download_chart_png_url }}">Chart PNG</a>
        </div>
    </div>

    {{ chart_html|safe }}
</div>

<div class="card">
    <div class="chart-toolbar">
        <div>
            <div class="section-title" style="margin:0">
                최고점 / 최저점 / 스프레드
            </div>
            <div class="toolbar-subtitle">
                정렬: {{ peak_sort_label }}
            </div>
        </div>
    </div>

    {{ peak_summary|safe }}
</div>

</main>
</div>
</div>

<script>
function syncYears() {
    const startElement = document.getElementById("year_start");
    const endElement = document.getElementById("year_end");

    if (!startElement || !endElement) return;

    let start = parseInt(startElement.value);
    let end = parseInt(endElement.value);

    if (start > end) {
        const temp = start;
        start = end;
        end = temp;
    }

    document.getElementById("year_start_hidden").value = start;
    document.getElementById("year_end_hidden").value = end;
    document.getElementById("yearLabel").innerText =
        start + " ~ " + end;
}

syncYears();

const initialSyncStatus = {{ sync_status|tojson }};
const selectedIndex = {{ selected_index|tojson }};

if (initialSyncStatus === "RUNNING") {
    const timer = setInterval(async () => {
        try {
            const response = await fetch(
                "/sync-status?index_name=" +
                encodeURIComponent(selectedIndex),
                {cache: "no-store"}
            );

            if (!response.ok) return;

            const data = await response.json();
            const notice = document.getElementById("syncNotice");

            if (notice && data.message) {
                notice.textContent = data.message;
            }

            if (data.status !== "RUNNING") {
                clearInterval(timer);
                window.location.href =
                    "/?index_name=" +
                    encodeURIComponent(selectedIndex) +
                    "&msg=" +
                    encodeURIComponent(data.message || "");
            }
        } catch (error) {
            console.error(error);
        }
    }, 2500);
}
</script>
</body>
</html>
"""


# =============================================================================
# Flask 라우트
# =============================================================================

@app.post("/refresh")
def refresh():
    if not validate_csrf_token():
        return Response(
            "잘못된 CSRF 토큰입니다.",
            status=400,
            mimetype="text/plain; charset=utf-8",
        )

    selected_index = normalize_index_name(
        request.form.get(
            "index_name",
            DEFAULT_INDEX_NAME,
        )
    )

    _, message = start_sync_job(selected_index)

    return redirect(
        url_for(
            "index",
            index_name=selected_index,
            msg=message,
        )
    )


@app.get("/sync-status")
def sync_status():
    selected_index = normalize_index_name(
        request.args.get(
            "index_name",
            DEFAULT_INDEX_NAME,
        )
    )

    with sync_state_lock:
        state = dict(SYNC_STATE)

    if (
        state.get("index_name")
        and state.get("index_name") != selected_index
    ):
        return jsonify(
            {
                "status": "IDLE",
                "index_name": selected_index,
                "message": "",
            }
        )

    return jsonify(state)


@app.get("/download")
def download():
    (
        selected_index,
        start_date,
        end_date,
        year_start,
        year_end,
        regions,
        years,
        line_width,
        palette,
        previous_palette,
        peak_sort,
    ) = current_filter_params()

    file_format = request.args.get(
        "format",
        "csv",
    )
    mode = request.args.get(
        "mode",
        "data",
    )

    long_df = load_long_df_from_db(
        selected_index,
        start_date=start_date or None,
        end_date=end_date or None,
        years=years or None,
        regions=regions or None,
    )

    selected_for_color = (
        regions
        if regions
        else sort_regions_for_display(
            long_df["REGION"]
            .dropna()
            .astype(str)
            .unique()
            .tolist(),
            selected_index,
        )
        if not long_df.empty
        else []
    )

    user_color_map = extract_user_color_map(
        selected_for_color,
        request,
    )

    if palette != previous_palette:
        user_color_map = {}

    try:
        width = min(
            10.0,
            max(0.1, float(line_width)),
        )
    except (TypeError, ValueError):
        width = 1.2

    today = datetime.now().strftime("%Y%m%d")

    if mode == "chart_html":
        fig = make_plot(
            long_df,
            regions,
            line_width=width,
            palette=palette,
            user_color_map=user_color_map,
        )

        html = fig.to_html(
            full_html=True,
            include_plotlyjs="cdn",
        )

        return Response(
            html,
            mimetype="text/html; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="'
                    f'{selected_index}_Chart_{today}.html"'
                )
            },
        )

    if mode == "chart_png":
        fig = make_plot(
            long_df,
            regions,
            line_width=width,
            palette=palette,
            user_color_map=user_color_map,
        )

        try:
            image = pio.to_image(
                fig,
                format="png",
                width=1600,
                height=900,
                scale=2,
            )

            return Response(
                image,
                mimetype="image/png",
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="'
                        f'{selected_index}_Chart_{today}.png"'
                    )
                },
            )

        except Exception as exc:
            return Response(
                f"PNG 내보내기 실패: {exc}",
                status=500,
                mimetype="text/plain; charset=utf-8",
            )

    if len(long_df) > MAX_DOWNLOAD_ROWS:
        return Response(
            (
                f"다운로드 최대 행 수 "
                f"{MAX_DOWNLOAD_ROWS:,}건을 초과했습니다."
            ),
            status=413,
            mimetype="text/plain; charset=utf-8",
        )

    output = long_df.copy()

    if output.empty:
        output = pd.DataFrame(
            columns=[
                "DATE",
                "YEAR",
                "REGION",
                "VALUE",
                "INDEX_NAME",
            ]
        )
    else:
        output["DATE"] = pd.to_datetime(
            output["DATE"],
            errors="coerce",
        ).dt.strftime("%Y-%m-%d")

    if file_format == "excel":
        return make_download_response(
            output,
            f"{selected_index}_Filtered_{today}.xlsx",
            kind="excel",
            sheet_name="Filtered",
        )

    return make_download_response(
        output,
        f"{selected_index}_Filtered_{today}.csv",
        kind="csv",
        sheet_name="Filtered",
    )


@app.get("/")
def index():
    (
        selected_index,
        start_date,
        end_date,
        year_start_raw,
        year_end_raw,
        selected_regions,
        years,
        line_width,
        palette,
        previous_palette,
        peak_sort,
    ) = current_filter_params()

    meta = get_last_update_meta(selected_index)
    full_df = load_long_df_from_db(selected_index)

    refresh_message = request.args.get(
        "msg",
        "",
    ).strip()

    with sync_state_lock:
        current_sync_state = dict(SYNC_STATE)

    sync_status_value = "IDLE"

    if (
        current_sync_state.get("index_name")
        == selected_index
    ):
        sync_status_value = current_sync_state.get(
            "status",
            "IDLE",
        )

        if (
            not refresh_message
            and current_sync_state.get("message")
        ):
            refresh_message = current_sync_state["message"]

    if (
        full_df.empty
        and RUNTIME_CONFIG
        and RUNTIME_CONFIG.init_refresh
        and sync_status_value != "RUNNING"
    ):
        _, initialization_message = start_sync_job(
            selected_index
        )

        if not refresh_message:
            refresh_message = initialization_message

        sync_status_value = "RUNNING"

    current_year = datetime.now().year

    if full_df.empty:
        year_min = current_year
        year_max = current_year
        year_start = current_year
        year_end = current_year

        all_regions = []
        filtered = full_df
        selected_regions = []
        selected_for_color = []
    else:
        all_regions = sort_regions_for_display(
            full_df["REGION"]
            .dropna()
            .astype(str)
            .unique()
            .tolist(),
            selected_index,
        )

        year_min = int(full_df["YEAR"].min())
        year_max = int(full_df["YEAR"].max())

        year_start = (
            int(year_start_raw)
            if str(year_start_raw).isdigit()
            else year_min
        )
        year_end = (
            int(year_end_raw)
            if str(year_end_raw).isdigit()
            else year_max
        )

        year_start = max(
            year_min,
            min(year_start, year_max),
        )
        year_end = max(
            year_min,
            min(year_end, year_max),
        )

        if year_start > year_end:
            year_start, year_end = year_end, year_start

        selected_regions = [
            region
            for region in selected_regions
            if region in all_regions
        ]

        filtered = filter_long_df(
            full_df,
            start_date=start_date or None,
            end_date=end_date or None,
            years=list(
                range(year_start, year_end + 1)
            ),
            regions=selected_regions or None,
        )

        selected_for_color = (
            selected_regions
            if selected_regions
            else all_regions
        )

    user_color_map = extract_user_color_map(
        selected_for_color,
        request,
    )

    if palette != previous_palette:
        user_color_map = {}

    automatic_color_map = build_auto_color_map(
        selected_for_color,
        palette,
    )

    color_keys = {
        region: safe_color_key(region)
        for region in selected_for_color
    }

    region_display_map = {
        region: display_region_label(
            region,
            selected_index,
        )
        for region in all_regions
    }

    try:
        width = min(
            10.0,
            max(0.1, float(line_width)),
        )
    except (TypeError, ValueError):
        width = 1.2

    line_width = str(width)

    figure = make_plot(
        filtered,
        selected_regions,
        line_width=width,
        palette=palette,
        user_color_map=user_color_map,
    )

    shown_start = (
        filtered["DATE"].min().strftime("%Y-%m-%d")
        if filtered is not None and not filtered.empty
        else "-"
    )
    shown_end = (
        filtered["DATE"].max().strftime("%Y-%m-%d")
        if filtered is not None and not filtered.empty
        else "-"
    )

    peak_summary = build_peak_trough_html(
        filtered,
        selected_index,
        selected_regions,
        peak_sort,
    )

    peak_sort_labels = {
        "region": "항로명",
        "peak_desc": "최고값 내림차순",
        "trough_asc": "최저값 오름차순",
        "spread_desc": "스프레드 내림차순",
    }

    complete_color_map = {
        region: (
            user_color_map.get(region)
            or automatic_color_map.get(region)
        )
        for region in selected_for_color
    }

    query_string = build_query_string(
        selected_index,
        start_date,
        end_date,
        year_start,
        year_end,
        selected_regions,
        line_width,
        palette,
        complete_color_map,
        peak_sort,
    )

    query_suffix = (
        f"&{query_string}"
        if query_string
        else ""
    )

    last_sync = (
        meta.get("finished_at")
        if meta
        else "-"
    )

    return render_template_string(
        HTML,
        selected_index=selected_index,
        last_sync=last_sync or "-",
        total_points=f"{len(full_df):,}",
        refresh_message=refresh_message,
        sync_status=sync_status_value,
        start_date=start_date,
        end_date=end_date,
        year_min=year_min,
        year_max=year_max,
        year_start=year_start,
        year_end=year_end,
        all_regions=all_regions,
        region_display_map=region_display_map,
        selected_regions=selected_regions,
        selected_regions_for_color=selected_for_color,
        color_keys=color_keys,
        color_values=user_color_map,
        auto_color_map=automatic_color_map,
        line_width=line_width,
        palette=palette,
        palette_options=PALETTE_OPTIONS,
        peak_sort=peak_sort,
        peak_sort_label=peak_sort_labels.get(
            peak_sort,
            "항로명",
        ),
        shown_start=shown_start,
        shown_end=shown_end,
        region_count=(
            len(selected_regions)
            if selected_regions
            else len(all_regions)
        ),
        shown_rows=(
            f"{len(filtered):,}"
            if filtered is not None
            else "0"
        ),
        peak_summary=peak_summary,
        download_csv_url=(
            f"/download?format=csv{query_suffix}"
        ),
        download_xlsx_url=(
            f"/download?format=excel{query_suffix}"
        ),
        download_chart_html_url=(
            f"/download?mode=chart_html{query_suffix}"
        ),
        download_chart_png_url=(
            f"/download?mode=chart_png{query_suffix}"
        ),
        chart_html=figure.to_html(
            full_html=False,
            include_plotlyjs="cdn",
        ),
    )


# =============================================================================
# 프로그램 실행
# =============================================================================

def open_browser(host, port):
    webbrowser.open_new(
        f"http://{host}:{port}"
    )


if __name__ == "__main__":
    args = parse_args()
    RUNTIME_CONFIG = args

    activate_file_logging()
    cleanup_invalid_regions()

    logger.info("Shipping Index Dashboard 시작")
    logger.info("Database: %s", DB_FILE)
    logger.info("Supported indexes: %s", SUPPORTED_INDEXES)
    logger.info(
        "Server: http://%s:%s",
        args.host,
        args.port,
    )

    if not args.user or not args.password:
        logger.warning(
            "KSG_ID 또는 KSG_PW가 설정되지 않았습니다. "
            "대시보드는 실행되지만 Refresh는 실패합니다."
        )

    if args.host not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        logger.warning(
            "외부 인터페이스에 바인딩됩니다. "
            "DASHBOARD_USER/DASHBOARD_PASSWORD 설정과 "
            "방화벽 또는 리버스 프록시 사용을 권장합니다."
        )

    browser_host = (
        "127.0.0.1"
        if args.host in {"0.0.0.0", "::"}
        else args.host
    )

    if args.open_browser:
        Timer(
            1.5,
            open_browser,
            args=(browser_host, args.port),
        ).start()

    app.run(
        host=args.host,
        port=args.port,
        debug=False,
        use_reloader=False,
        threaded=True,
    )
```

## 적용된 주요 개선 사항

- 소스 코드에서 KSG 계정 기본값을 제거하고 환경변수로만 받도록 변경했습니다.
- 누락 패키지를 실행 중 자동 설치하는 로직을 제거했습니다.
- `rows_added`와 `rows_updated`를 실제 신규·수정 건수로 구분합니다.
- 기존 데이터의 `created_at`이 매번 변경되지 않도록 수정했습니다.
- SQLite WAL, busy timeout 및 조회 인덱스를 적용했습니다.
- 기존 DB에 `updated_at`, `rows_updated`가 없으면 자동 추가합니다.
- 출력 폴더를 기준으로 기존 Excel 파일을 검색합니다.
- Excel은 임시 파일에 작성한 뒤 원자적으로 교체합니다.
- Refresh를 GET이 아닌 CSRF 보호된 POST 요청으로 변경했습니다.
- Selenium 작업을 백그라운드 스레드에서 실행합니다.
- 동기화 상태를 주기적으로 확인하고 완료 후 화면을 갱신합니다.
- 선택적으로 Basic 인증을 적용할 수 있습니다.
- 최고점·최저점 HTML의 외부 문자열을 escape 처리했습니다.
- 날짜, 연도, 선 두께, 색상 등 요청값을 검증합니다.
- DB 조회 시 날짜·연도·항로 조건을 SQL에 적용할 수 있도록 개선했습니다.
- 로그 파일은 5MB 단위로 최대 5개까지 순환 보관합니다.
- 다운로드 최대 행 수를 500,000건으로 제한했습니다.
- BDI 차트 범례에 전체 지수 명칭을 표시합니다.

### 패키지 설치

```bash
python -m pip install flask pandas plotly selenium openpyxl webdriver-manager kaleido
```

### Windows PowerShell 실행 예시

```powershell
$env:KSG_ID="사용자ID"
$env:KSG_PW="비밀번호"
$env:FLASK_SECRET_KEY="충분히-긴-임의의-비밀키"
python app.py
```

외부 접속을 허용한다면 인증 정보도 설정하는 것이 좋습니다.

```powershell
$env:DASHBOARD_USER="admin"
$env:DASHBOARD_PASSWORD="강력한비밀번호"
python app.py --host 0.0.0.0
```

### Linux/macOS 실행 예시

```bash
export KSG_ID="사용자ID"
export KSG_PW="비밀번호"
export FLASK_SECRET_KEY="충분히-긴-임의의-비밀키"
python app.py
```

기존 코드에 포함되어 공유된 비밀번호는 노출된 것으로 간주하고 변경하는 것이 안전합니다.
