# -*- coding: utf-8 -*-
"""
TAC Index / Baltic Exchange Airfreight Rate Index Crawler
GitHub Actions + GitHub Pages static dashboard version

Data source
- Air Cargo News Data Hub
- Infogram embedded chart

Output
- TAC Index/TAC_Index.db
- TAC Index/tac_index_data.json
- TAC Index/index.html

Database schema
- tac_master
- tac_detail
- crawl_audit
"""

import argparse
import hashlib
import html as html_lib
import json
import logging
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except ImportError:
    print("ERROR: 'requests' library is not installed.")
    print("Run: pip install requests")
    sys.exit(1)

# =============================================================================
# 1. Basic configuration
# =============================================================================
BASE_DIR = Path(__file__).resolve().parent

DB_PATH = BASE_DIR / "TAC_Index.db"
JSON_PATH = BASE_DIR / "tac_index_data.json"
HTML_PATH = BASE_DIR / "index.html"
LOG_PATH = BASE_DIR / "tac_index_crawl.log"

INFOGRAM_ID = "SuAWaj6s2iFahJI4zPJw"

PARENT_URL = (
    "https://www.aircargonews.net/data-hub/industry-data/"
    "baltic-exchange-airfreight-rate-index/"
)

EMBED_URL = f"https://e.infogram.com/_/{INFOGRAM_ID}?src=embed"

REQUEST_TIMEOUT_SECONDS = 40

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
    "Referer": PARENT_URL,
}

EXPECTED_ROUTE_KEYWORDS = [
    "frankfurt",
    "hong kong",
    "north america",
    "europe",
]

# =============================================================================
# 2. Logging
# =============================================================================
logger = logging.getLogger("tac_index")
logger.setLevel(logging.INFO)
logger.handlers.clear()

_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s"
)

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(_formatter)
logger.addHandler(_console_handler)

try:
    _file_handler = logging.FileHandler(
        LOG_PATH,
        mode="a",
        encoding="utf-8",
    )
    _file_handler.setFormatter(_formatter)
    logger.addHandler(_file_handler)
except OSError:
    pass

def log_info(message: str) -> None:
    """Write info-level log."""
    logger.info(message)

def log_warning(message: str) -> None:
    """Write warning-level log."""
    logger.warning(message)

# =============================================================================
# 3. Common utility functions
# =============================================================================
def normalize_text(value: Any) -> str:
    """Normalize HTML entities and whitespace."""
    text = html_lib.unescape(str(value or ""))
    return " ".join(text.replace("\xa0", " ").strip().split())

def safe_float(value: Any) -> Optional[float]:
    """Convert currency / percentage text into float."""
    if value is None:
        return None

    raw = normalize_text(value)

    if raw.lower() in {"", "-", "--", "n/a", "na", "null", "none"}:
        return None

    negative = raw.startswith("-") or (
        raw.startswith("(") and raw.endswith(")")
    )

    cleaned = raw.replace(",", "")
    cleaned = cleaned.replace("$", "")
    cleaned = cleaned.replace("£", "")
    cleaned = cleaned.replace("€", "")
    cleaned = cleaned.replace("%", "")
    cleaned = re.sub(r"[^0-9.]", "", cleaned)

    if not cleaned:
        return None

    try:
        numeric_value = float(cleaned)
        return -numeric_value if negative else numeric_value
    except ValueError:
        return None

def get_now_string() -> str:
    """Return local execution timestamp."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def atomic_write_text(path: Path, content: str) -> None:
    """Prevent partial JSON / HTML file generation."""
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)

# =============================================================================
# 4. Database management and migration
# =============================================================================
def cleanup_duplicate_data_dates(conn: sqlite3.Connection) -> int:
    """
    Remove legacy duplicate records.

    Rule
    - One data_date must have one tac_master record
    - Highest snapshot_id retained
    """
    duplicate_rows = conn.execute(
        """
        SELECT data_date
        FROM tac_master
        GROUP BY data_date
        HAVING COUNT(*) > 1
        """
    ).fetchall()

    deleted_count = 0

    for row in duplicate_rows:
        data_date = row["data_date"]

        snapshots = conn.execute(
            """
            SELECT snapshot_id
            FROM tac_master
            WHERE data_date = ?
            ORDER BY snapshot_id DESC
            """,
            (data_date,),
        ).fetchall()

        keep_snapshot_id = int(snapshots[0]["snapshot_id"])

        delete_snapshot_ids = [
            int(snapshot["snapshot_id"])
            for snapshot in snapshots[1:]
        ]

        for snapshot_id in delete_snapshot_ids:
            conn.execute(
                """
                DELETE FROM tac_master
                WHERE snapshot_id = ?
                """,
                (snapshot_id,),
            )
            deleted_count += 1

        log_warning(
            "[DB] Duplicate date cleanup completed: "
            f"date={data_date}, "
            f"kept_snapshot_id={keep_snapshot_id}, "
            f"deleted={len(delete_snapshot_ids)}"
        )

    return deleted_count

def get_db_connection() -> sqlite3.Connection:
    """
    Create and migrate SQLite database.

    Existing file
    - TAC Index/TAC_Index.db

    Preserved tables
    - tac_master
    - tac_detail
    - crawl_audit
    """
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tac_master (
            snapshot_id    INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at    TEXT NOT NULL,
            data_date      TEXT NOT NULL,
            data_hash      TEXT NOT NULL,
            overall_index  REAL,
            weekly_change  REAL,
            route_count    INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tac_detail (
            snapshot_id     INTEGER NOT NULL,
            route_code      TEXT NOT NULL,
            origin          TEXT,
            destination     TEXT,
            index_value     REAL,
            weekly_change   REAL,
            monthly_change  REAL,
            ytd_change      REAL,
            PRIMARY KEY (snapshot_id, route_code),
            FOREIGN KEY (snapshot_id)
                REFERENCES tac_master(snapshot_id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS crawl_audit (
            audit_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at   TEXT NOT NULL,
            finished_at  TEXT NOT NULL,
            status       TEXT NOT NULL,
            message      TEXT,
            snapshot_id  INTEGER,
            route_count  INTEGER,
            data_date    TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_tac_detail_snapshot_id
        ON tac_detail(snapshot_id);

        CREATE INDEX IF NOT EXISTS idx_crawl_audit_finished_at
        ON crawl_audit(finished_at);
        """
    )

    with conn:
        cleanup_duplicate_data_dates(conn)

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_tac_master_data_date
            ON tac_master(data_date)
            """
        )

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_tac_master_data_hash
            ON tac_master(data_hash)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tac_master_data_date
            ON tac_master(data_date)
            """
        )

    return conn

def save_crawl_audit(
    started_at: str,
    finished_at: str,
    status: str,
    message: str,
    snapshot_id: Optional[int] = None,
    route_count: int = 0,
    data_date: Optional[str] = None,
) -> None:
    """Save crawler execution audit."""
    conn = get_db_connection()

    try:
        conn.execute(
            """
            INSERT INTO crawl_audit (
                started_at,
                finished_at,
                status,
                message,
                snapshot_id,
                route_count,
                data_date
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                started_at,
                finished_at,
                status,
                str(message)[:2000],
                snapshot_id,
                int(route_count or 0),
                data_date,
            ),
        )
        conn.commit()
    finally:
        conn.close()

def get_total_snapshot_count() -> int:
    """Count preserved historical snapshots."""
    conn = get_db_connection()

    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM tac_master
            """
        ).fetchone()

        return int(row["cnt"]) if row else 0
    finally:
        conn.close()

def get_last_sync_meta() -> Dict[str, Any]:
    """Return latest crawler audit record."""
    conn = get_db_connection()

    try:
        row = conn.execute(
            """
            SELECT
                started_at,
                finished_at,
                status,
                message,
                snapshot_id,
                route_count,
                data_date
            FROM crawl_audit
            ORDER BY audit_id DESC
            LIMIT 1
            """
        ).fetchone()

        return dict(row) if row else {}
    finally:
        conn.close()

# =============================================================================
# 5. Infogram extraction
# =============================================================================
def fetch_infogram_html() -> str:
    """Request Infogram embed page."""
    log_info(f"[FETCH] Infogram request: {EMBED_URL}")

    response = requests.get(
        EMBED_URL,
        headers=HEADERS,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    response.raise_for_status()

    log_info(
        "[FETCH] Infogram response received: "
        f"HTTP={response.status_code}, "
        f"size={len(response.text):,} bytes"
    )

    return response.text

def extract_balanced_json_object(
    source_text: str,
    start_position: int,
) -> Dict[str, Any]:
    """Extract balanced JSON after infographicData assignment."""
    object_start = source_text.find("{", start_position)

    if object_start < 0:
        raise RuntimeError(
            "infographicData JSON object start was not found."
        )

    depth = 0
    in_string = False
    escaped = False

    for index in range(object_start, len(source_text)):
        char = source_text[index]

        if escaped:
            escaped = False
            continue

        if char == "\\":
            escaped = True
            continue

        if char == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        if char == "{":
            depth += 1

        elif char == "}":
            depth -= 1

            if depth == 0:
                json_text = source_text[object_start:index + 1]

                try:
                    return json.loads(json_text)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"Infogram JSON decoding failed: {exc}"
                    ) from exc

    raise RuntimeError(
        "infographicData JSON object end was not found."
    )

def extract_infographic_data(embed_html: str) -> Dict[str, Any]:
    """Extract Infogram chart JSON."""
    log_info("[PARSE] Extracting infographicData.")

    patterns = [
        r"window\.infographicData\s*=",
        r"infographicData\s*=",
    ]

    for pattern in patterns:
        match = re.search(pattern, embed_html)

        if match:
            data = extract_balanced_json_object(
                embed_html,
                match.end(),
            )

            log_info("[PARSE] infographicData extraction completed.")
            return data

    raise RuntimeError(
        "Infogram infographicData was not found. "
        "The source-page structure may have changed."
    )

def get_cell_value(cell: Any) -> Any:
    """Extract usable chart cell values."""
    if isinstance(cell, dict):
        for key in ("value", "formattedValue", "label", "text"):
            if key in cell:
                return cell[key]

        return ""

    return cell

def normalize_sheet(sheet: Any) -> List[List[Any]]:
    """Normalize Infogram sheet into matrix."""
    if not isinstance(sheet, list):
        return []

    rows: List[List[Any]] = []

    for row in sheet:
        if not isinstance(row, list):
            continue

        rows.append(
            [get_cell_value(cell) for cell in row]
        )

    return rows

def get_chart_sheets(
    entity: Dict[str, Any],
) -> List[List[List[Any]]]:
    """Read chart data from candidate Infogram chart entity."""
    props = entity.get("props", {})
    chart_data = props.get("chartData", {})

    sheets: List[List[List[Any]]] = []

    raw_data = chart_data.get("data", [])

    if isinstance(raw_data, list):
        for candidate in raw_data:
            normalized = normalize_sheet(candidate)

            if normalized:
                sheets.append(normalized)

    if not sheets and isinstance(chart_data.get("sheets"), list):
        for candidate in chart_data["sheets"]:
            normalized = normalize_sheet(candidate)

            if normalized:
                sheets.append(normalized)

    return sheets

def iter_chart_entities(
    data: Dict[str, Any],
) -> List[Tuple[str, Dict[str, Any]]]:
    """Find chart entities within Infogram JSON."""
    entities = (
        data.get("elements", {})
        .get("content", {})
        .get("content", {})
        .get("entities", {})
    )

    if not isinstance(entities, dict):
        return []

    charts: List[Tuple[str, Dict[str, Any]]] = []

    for entity_id, entity in entities.items():
        if (
            isinstance(entity, dict)
            and str(entity.get("type", "")).upper() == "CHART"
        ):
            charts.append((str(entity_id), entity))

    return charts

def score_chart_sheet(rows: List[List[Any]]) -> int:
    """Score potential TAC route chart."""
    if len(rows) < 2:
        return -10000

    header_text = " ".join(
        normalize_text(value).lower()
        for value in rows[0]
    )

    whole_text = " ".join(
        normalize_text(value).lower()
        for row in rows[:10]
        for value in row
    )

    score = 0

    if len(rows[0]) >= 3:
        score += 10

    if len(rows) >= 10:
        score += 5

    for keyword in EXPECTED_ROUTE_KEYWORDS:
        if keyword in header_text:
            score += 50
        elif keyword in whole_text:
            score += 10

    if "baltic" in whole_text:
        score += 10

    if "airfreight" in whole_text or "air freight" in whole_text:
        score += 10

    return score

def extract_best_chart_rows(
    data: Dict[str, Any],
) -> List[List[Any]]:
    """Select highest-confidence TAC chart sheet."""
    log_info("[PARSE] Searching TAC route-level chart.")

    chart_entities = iter_chart_entities(data)

    if not chart_entities:
        raise RuntimeError(
            "No CHART entity was found in infographicData."
        )

    candidates: List[
        Tuple[int, str, int, List[List[Any]]]
    ] = []

    for entity_id, entity in chart_entities:
        sheets = get_chart_sheets(entity)

        for sheet_index, sheet in enumerate(sheets):
            score = score_chart_sheet(sheet)
            candidates.append(
                (score, entity_id, sheet_index, sheet)
            )

    if not candidates:
        raise RuntimeError(
            "No usable chart sheet was found in Infogram data."
        )

    candidates.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    best_score, best_entity_id, best_sheet_index, best_rows = (
        candidates[0]
    )

    if best_score < 20:
        preview = []

        for score, entity_id, sheet_index, rows in candidates[:5]:
            preview.append(
                f"entity={entity_id}, "
                f"sheet={sheet_index}, "
                f"score={score}, "
                f"header={rows[0] if rows else []}"
            )

        raise RuntimeError(
            "Unable to identify TAC route chart. "
            f"Candidates: {' | '.join(preview)}"
        )

    log_info(
        "[PARSE] Chart selected: "
        f"entity={best_entity_id}, "
        f"sheet={best_sheet_index}, "
        f"score={best_score}, "
        f"rows={len(best_rows)}"
    )

    return best_rows

# =============================================================================
# 6. Dataset transformation
# =============================================================================
def normalize_route_name(route_name: str) -> str:
    """Standardize known route labels."""
    cleaned = normalize_text(route_name)
    cleaned = re.sub(r"\s*-\s*", "-", cleaned)

    replacements = {
        "Frankfurt - North America": "Frankfurt-North America",
        "Hong Kong - Europe": "Hong Kong-Europe",
        "Hong Kong - North America": "Hong Kong-North America",
        "Frankfurt to North America": "Frankfurt-North America",
        "Hong Kong to Europe": "Hong Kong-Europe",
        "Hong Kong to North America": "Hong Kong-North America",
    }

    return replacements.get(cleaned, cleaned)

def split_route(route_name: str) -> Tuple[str, str]:
    """Split route label into origin and destination."""
    normalized = normalize_route_name(route_name)

    if "-" not in normalized:
        return normalized, ""

    origin, destination = normalized.split("-", 1)

    return origin.strip(), destination.strip()

def parse_source_date(label: str) -> Optional[str]:
    """Convert source date label to ISO date."""
    raw = normalize_text(label)

    date_formats = [
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%d-%m-%Y",
        "%d %b %Y",
        "%d %B %Y",
        "%b %Y",
        "%B %Y",
        "%b-%y",
        "%b-%Y",
        "%Y-%m",
        "%Y/%m",
    ]

    for fmt in date_formats:
        try:
            parsed = datetime.strptime(raw, fmt)
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            continue

    matched = re.search(
        r"(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})",
        raw,
    )

    if matched:
        return parse_source_date(matched.group(1))

    matched = re.search(
        r"(\d{1,2}-[A-Za-z]{3}-\d{4})",
        raw,
    )

    if matched:
        try:
            parsed = datetime.strptime(
                matched.group(1),
                "%d-%b-%Y",
            )
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            pass

    return None

def build_dataset(rows: List[List[Any]]) -> Dict[str, Any]:
    """Convert Infogram matrix into route time-series dataset."""
    if not rows:
        raise RuntimeError("TAC chart rows are empty.")

    header = rows[0]
    body = rows[1:]

    if len(header) < 2:
        raise RuntimeError(
            f"Unexpected TAC chart header format: {header}"
        )

    labels: List[str] = []
    source_dates: List[Optional[str]] = []

    for row in body:
        if not row:
            continue

        label = normalize_text(row[0])

        if not label:
            continue

        labels.append(label)
        source_dates.append(parse_source_date(label))

    if not labels:
        raise RuntimeError(
            "No date labels were found in TAC chart."
        )

    series: List[Dict[str, Any]] = []

    for column_index in range(1, len(header)):
        route_name = normalize_route_name(
            str(header[column_index])
        )

        values: List[Optional[float]] = []
        valid_value_count = 0

        for row in body:
            if not row or not normalize_text(row[0]):
                continue

            raw_value = (
                row[column_index]
                if column_index < len(row)
                else None
            )

            numeric_value = safe_float(raw_value)

            if numeric_value is not None:
                valid_value_count += 1

            values.append(numeric_value)

        if valid_value_count == 0:
            continue

        series.append(
            {
                "name": route_name,
                "values": values,
            }
        )

    if not series:
        raise RuntimeError(
            "No numeric TAC route series were extracted."
        )

    log_info(
        "[PARSE] Dataset created: "
        f"labels={len(labels)}, "
        f"routes={len(series)}, "
        f"route_names={[item['name'] for item in series]}"
    )

    return {
        "labels": labels,
        "source_dates": source_dates,
        "series": series,
    }

def get_previous_valid_value(
    values: List[Optional[float]],
    from_index: int,
) -> Optional[float]:
    """Get preceding non-empty rate."""
    for index in range(from_index - 1, -1, -1):
        if index < len(values) and values[index] is not None:
            return values[index]

    return None

def build_all_snapshot_bundles(
    dataset: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Build one snapshot per valid source date.

    Persistence logic
    - new data_date: INSERT
    - existing same hash: unchanged
    - existing changed hash: UPDATE
    """
    labels = dataset["labels"]
    source_dates = dataset["source_dates"]
    series = dataset["series"]

    bundles: List[Dict[str, Any]] = []
    seen_dates = set()

    for index, source_label in enumerate(labels):
        data_date = source_dates[index]

        if not data_date:
            log_warning(
                "[PARSE] Date row skipped: "
                f"unparseable_label={source_label}"
            )
            continue

        if data_date in seen_dates:
            log_warning(
                "[PARSE] Date row skipped: "
                f"duplicate_date={data_date}, "
                f"source_label={source_label}"
            )
            continue

        seen_dates.add(data_date)

        details: List[Dict[str, Any]] = []
        route_values: List[float] = []
        route_changes: List[float] = []

        for item in series:
            route_name = item["name"]
            values = item["values"]

            current_value = (
                values[index]
                if index < len(values)
                else None
            )

            if current_value is None:
                continue

            previous_value = get_previous_valid_value(
                values,
                index,
            )

            periodic_change: Optional[float] = None

            if previous_value not in (None, 0):
                periodic_change = (
                    (current_value - previous_value)
                    / previous_value
                    * 100
                )
                route_changes.append(periodic_change)

            origin, destination = split_route(route_name)

            details.append(
                {
                    "route_code": route_name,
                    "origin": origin,
                    "destination": destination,
                    "index_value": current_value,
                    "weekly_change": periodic_change,
                    "monthly_change": None,
                    "ytd_change": None,
                }
            )

            route_values.append(current_value)

        if not details:
            log_warning(
                "[PARSE] Date row skipped: "
                f"no_route_data={data_date}"
            )
            continue

        overall_index = sum(route_values) / len(route_values)

        average_change = (
            sum(route_changes) / len(route_changes)
            if route_changes
            else None
        )

        bundles.append(
            {
                "data_date": data_date,
                "source_label": source_label,
                "overall_index": overall_index,
                "weekly_change": average_change,
                "details": details,
            }
        )

    if not bundles:
        raise RuntimeError(
            "No TAC snapshot bundles were created."
        )

    bundles.sort(key=lambda item: item["data_date"])

    log_info(
        "[PARSE] Historical bundles created: "
        f"count={len(bundles)}, "
        f"first={bundles[0]['data_date']}, "
        f"last={bundles[-1]['data_date']}"
    )

    return bundles

def make_snapshot_hash(
    data_date: str,
    details: List[Dict[str, Any]],
) -> str:
    """Create deterministic source revision hash."""
    normalized_payload = {
        "data_date": data_date,
        "details": sorted(
            details,
            key=lambda row: row["route_code"],
        ),
    }

    raw_payload = json.dumps(
        normalized_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    return hashlib.sha256(
        raw_payload.encode("utf-8")
    ).hexdigest()

# =============================================================================
# 7. Historical snapshot persistence
# =============================================================================
def save_all_snapshots(
    bundles: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Persist full TAC historical series.

    New date
    - INSERT tac_master / tac_detail

    Existing date, same data hash
    - KEEP historical record

    Existing date, changed data hash
    - UPDATE master / REPLACE detail
    """
    if not bundles:
        raise RuntimeError(
            "No TAC snapshot bundles were supplied."
        )

    conn = get_db_connection()
    captured_at = get_now_string()

    new_count = 0
    updated_count = 0
    unchanged_count = 0

    latest_snapshot_id: Optional[int] = None
    latest_bundle: Optional[Dict[str, Any]] = None

    try:
        with conn:
            for bundle in bundles:
                data_date = bundle["data_date"]
                details = bundle["details"]

                data_hash = make_snapshot_hash(
                    data_date,
                    details,
                )

                existing = conn.execute(
                    """
                    SELECT snapshot_id, data_hash
                    FROM tac_master
                    WHERE data_date = ?
                    LIMIT 1
                    """,
                    (data_date,),
                ).fetchone()

                if existing is None:
                    cursor = conn.execute(
                        """
                        INSERT INTO tac_master (
                            captured_at,
                            data_date,
                            data_hash,
                            overall_index,
                            weekly_change,
                            route_count
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            captured_at,
                            data_date,
                            data_hash,
                            bundle["overall_index"],
                            bundle["weekly_change"],
                            len(details),
                        ),
                    )

                    snapshot_id = int(cursor.lastrowid)

                    conn.executemany(
                        """
                        INSERT INTO tac_detail (
                            snapshot_id,
                            route_code,
                            origin,
                            destination,
                            index_value,
                            weekly_change,
                            monthly_change,
                            ytd_change
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                snapshot_id,
                                row["route_code"],
                                row["origin"],
                                row["destination"],
                                row["index_value"],
                                row["weekly_change"],
                                row["monthly_change"],
                                row["ytd_change"],
                            )
                            for row in details
                        ],
                    )

                    new_count += 1

                else:
                    snapshot_id = int(existing["snapshot_id"])

                    if existing["data_hash"] == data_hash:
                        unchanged_count += 1

                    else:
                        conn.execute(
                            """
                            UPDATE tac_master
                            SET
                                captured_at = ?,
                                data_hash = ?,
                                overall_index = ?,
                                weekly_change = ?,
                                route_count = ?
                            WHERE snapshot_id = ?
                            """,
                            (
                                captured_at,
                                data_hash,
                                bundle["overall_index"],
                                bundle["weekly_change"],
                                len(details),
                                snapshot_id,
                            ),
                        )

                        conn.execute(
                            """
                            DELETE FROM tac_detail
                            WHERE snapshot_id = ?
                            """,
                            (snapshot_id,),
                        )

                        conn.executemany(
                            """
                            INSERT INTO tac_detail (
                                snapshot_id,
                                route_code,
                                origin,
                                destination,
                                index_value,
                                weekly_change,
                                monthly_change,
                                ytd_change
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            [
                                (
                                    snapshot_id,
                                    row["route_code"],
                                    row["origin"],
                                    row["destination"],
                                    row["index_value"],
                                    row["weekly_change"],
                                    row["monthly_change"],
                                    row["ytd_change"],
                                )
                                for row in details
                            ],
                        )

                        updated_count += 1

                latest_snapshot_id = snapshot_id
                latest_bundle = bundle

        log_info(
            "[DB] Historical sync completed: "
            f"new={new_count}, "
            f"updated={updated_count}, "
            f"unchanged={unchanged_count}, "
            f"processed={len(bundles)}"
        )

        return {
            "new_count": new_count,
            "updated_count": updated_count,
            "unchanged_count": unchanged_count,
            "latest_snapshot_id": latest_snapshot_id,
            "latest_bundle": latest_bundle,
        }

    finally:
        conn.close()

# =============================================================================
# 8. Static JSON generation
# =============================================================================
def load_history() -> List[Dict[str, Any]]:
    """Load master-level historical records."""
    conn = get_db_connection()

    try:
        rows = conn.execute(
            """
            SELECT
                snapshot_id,
                captured_at,
                data_date,
                overall_index,
                weekly_change,
                route_count
            FROM tac_master
            ORDER BY data_date ASC, snapshot_id ASC
            """
        ).fetchall()

        return [dict(row) for row in rows]

    finally:
        conn.close()

def load_latest_details() -> List[Dict[str, Any]]:
    """Load latest route-level observation."""
    conn = get_db_connection()

    try:
        rows = conn.execute(
            """
            SELECT
                d.snapshot_id,
                d.route_code,
                d.origin,
                d.destination,
                d.index_value,
                d.weekly_change,
                d.monthly_change,
                d.ytd_change,
                m.data_date,
                m.captured_at
            FROM tac_detail d
            INNER JOIN tac_master m
                ON d.snapshot_id = m.snapshot_id
            WHERE d.snapshot_id = (
                SELECT snapshot_id
                FROM tac_master
                ORDER BY data_date DESC, snapshot_id DESC
                LIMIT 1
            )
            ORDER BY d.route_code ASC
            """
        ).fetchall()

        return [dict(row) for row in rows]

    finally:
        conn.close()

def build_raw_dataset_from_db() -> Dict[str, Any]:
    """Build chart dataset from preserved SQLite history."""
    conn = get_db_connection()

    try:
        master_rows = conn.execute(
            """
            SELECT snapshot_id, data_date
            FROM tac_master
            ORDER BY data_date ASC, snapshot_id ASC
            """
        ).fetchall()

        if not master_rows:
            return {
                "labels": [],
                "series": [],
            }

        snapshot_ids = [
            int(row["snapshot_id"])
            for row in master_rows
        ]

        labels = [
            row["data_date"]
            for row in master_rows
        ]

        placeholders = ",".join(
            "?" for _ in snapshot_ids
        )

        detail_rows = conn.execute(
            f"""
            SELECT
                snapshot_id,
                route_code,
                index_value
            FROM tac_detail
            WHERE snapshot_id IN ({placeholders})
            ORDER BY route_code ASC, snapshot_id ASC
            """,
            snapshot_ids,
        ).fetchall()

        route_values: Dict[
            str,
            Dict[int, Optional[float]],
        ] = {}

        for row in detail_rows:
            route_code = row["route_code"]
            snapshot_id = int(row["snapshot_id"])

            route_values.setdefault(route_code, {})
            route_values[route_code][snapshot_id] = (
                row["index_value"]
            )

        series = []

        for route_code in sorted(route_values):
            values = [
                route_values[route_code].get(snapshot_id)
                for snapshot_id in snapshot_ids
            ]

            series.append(
                {
                    "name": route_code,
                    "values": values,
                }
            )

        return {
            "labels": labels,
            "series": series,
        }

    finally:
        conn.close()

def build_static_json() -> Dict[str, Any]:
    """Generate static dashboard JSON."""
    payload = {
        "generated_at": get_now_string(),
        "source": {
            "name": "Baltic Exchange Airfreight Rate Index",
            "publisher": "Air Cargo News",
            "parent_url": PARENT_URL,
            "embed_url": EMBED_URL,
            "infogram_id": INFOGRAM_ID,
        },
        "definition": {
            "overall_index": (
                "Dashboard-derived arithmetic average of "
                "available route-level airfreight rates. "
                "This is not an official TAC composite index."
            ),
            "weekly_change": (
                "Dashboard-derived average percentage change "
                "versus the preceding available period."
            ),
            "unit": "USD/kg",
        },
        "last_sync": get_last_sync_meta(),
        "history": load_history(),
        "latest_detail": load_latest_details(),
        "raw_dataset": build_raw_dataset_from_db(),
    }

    atomic_write_text(
        JSON_PATH,
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ),
    )

    log_info(
        f"[STATIC] JSON generated: {JSON_PATH.name}"
    )

    return payload

# =============================================================================
# 9. Static HTML dashboard
# =============================================================================
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>MI Terminal | TAC Airfreight Index</title>

  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>

  <style>
    :root {
      --bg: #02060b;
      --panel: #07111c;
      --panel2: #0a1724;
      --line: #1c3448;
      --text: #f1f6fa;
      --muted: #8298aa;
      --amber: #f6a623;
      --cyan: #39c7e2;
      --blue: #42a8f5;
      --green: #35ca82;
      --red: #ff6565;
      --purple: #b791ff;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      color: var(--text);
      background: var(--bg);
      font-family: Consolas, "Courier New", monospace;
      font-size: 12px;
    }

    .topbar {
      position: sticky;
      top: 0;
      z-index: 10;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      min-height: 44px;
      padding: 0 16px;
      border-bottom: 1px solid var(--line);
      background: #06101a;
    }

    .brand {
      color: white;
      font-weight: 900;
      letter-spacing: .08em;
      white-space: nowrap;
    }

    .brand-mark {
      display: inline-block;
      margin-right: 10px;
      padding: 4px 7px;
      color: #02060b;
      background: var(--amber);
    }

    .meta {
      color: var(--muted);
      font-size: 10px;
      text-align: right;
    }

    .meta a {
      color: var(--cyan);
      text-decoration: none;
    }

    .terminal {
      width: min(1600px, calc(100% - 24px));
      margin: 12px auto 24px;
    }

    .kpis {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 10px;
    }

    .kpi {
      padding: 12px;
      border: 1px solid var(--line);
      border-top: 2px solid var(--blue);
      background: var(--panel);
    }

    .kpi.amber {
      border-top-color: var(--amber);
    }

    .kpi.green {
      border-top-color: var(--green);
    }

    .kpi.purple {
      border-top-color: var(--purple);
    }

    .kpi-label {
      color: var(--muted);
      font-size: 9px;
      font-weight: 700;
      letter-spacing: .06em;
    }

    .kpi-value {
      margin-top: 8px;
      font-size: 22px;
      font-weight: 900;
      text-align: right;
    }

    .kpi-sub {
      margin-top: 5px;
      color: var(--muted);
      font-size: 9px;
      text-align: right;
    }

    .grid {
      display: grid;
      grid-template-columns: 2fr 1fr;
      gap: 10px;
    }

    .panel {
      overflow: hidden;
      border: 1px solid var(--line);
      background: var(--panel);
    }

    .panel-head {
      display: flex;
      justify-content: space-between;
      padding: 10px;
      border-bottom: 1px solid var(--line);
      background: var(--panel2);
    }

    .panel-title {
      color: var(--amber);
      font-size: 10px;
      font-weight: 900;
      letter-spacing: .06em;
    }

    .panel-meta {
      color: var(--muted);
      font-size: 9px;
    }

    .panel-body {
      padding: 10px;
    }

    .buttons {
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
      margin-bottom: 10px;
    }

    .route-btn {
      padding: 5px 8px;
      border: 1px solid var(--line);
      color: var(--muted);
      background: #040c14;
      cursor: pointer;
      font-family: inherit;
      font-size: 9px;
    }

    .route-btn.active {
      color: #02060b;
      border-color: var(--amber);
      background: var(--amber);
    }

    .chart-wrap {
      position: relative;
      height: 410px;
    }

    .table-wrap {
      max-height: 455px;
      overflow-y: auto;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 10px;
    }

    th {
      position: sticky;
      top: 0;
      padding: 8px;
      color: var(--amber);
      border-bottom: 1px solid var(--line);
      background: var(--panel2);
      font-size: 9px;
      text-align: right;
    }

    td {
      padding: 8px;
      border-bottom: 1px solid #132536;
      text-align: right;
      white-space: nowrap;
    }

    th:first-child,
    td:first-child {
      text-align: left;
    }

    .status {
      margin-top: 10px;
      padding: 9px 12px;
      border: 1px solid var(--line);
      border-left: 3px solid var(--green);
      color: var(--muted);
      background: #02070d;
      font-size: 10px;
    }

    .up {
      color: var(--green);
      font-weight: 800;
    }

    .down {
      color: var(--red);
      font-weight: 800;
    }

    .flat {
      color: var(--muted);
    }

    @media (max-width: 900px) {
      .grid {
        grid-template-columns: 1fr;
      }

      .kpis {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .meta {
        display: none;
      }
    }

    @media (max-width: 520px) {
      .kpis {
        grid-template-columns: 1fr;
      }

      .terminal {
        width: calc(100% - 16px);
      }
    }
  </style>
</head>

<body>
  <header class="topbar">
    <div class="brand">
      <span class="brand-mark">MI&lt;GO&gt;</span>
      AIR TERMINAL // TAC BAI
    </div>

    <div class="meta">
      SRC:
      <a href="__PARENT_URL__" target="_blank" rel="noopener">
        AIR CARGO NEWS
      </a>
      | UPDATED: __GENERATED_AT__
    </div>
  </header>

  <main class="terminal">
    <section class="kpis" id="kpi-area"></section>

    <div class="grid">
      <section class="panel">
        <div class="panel-head">
          <span class="panel-title">
            01. HISTORICAL SPOT RATE TIME-SERIES
          </span>
          <span class="panel-meta">USD / KG</span>
        </div>

        <div class="panel-body">
          <div class="buttons" id="route-buttons"></div>

          <div class="chart-wrap">
            <canvas id="chart"></canvas>
          </div>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <span class="panel-title">
            02. ROUTE WATCHLIST
          </span>
          <span class="panel-meta">LATEST / PREV CHANGE</span>
        </div>

        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>ROUTE</th>
                <th>LATEST</th>
                <th>CHANGE</th>
                <th>DATE</th>
              </tr>
            </thead>
            <tbody id="watchlist-body"></tbody>
          </table>
        </div>
      </section>
    </div>

    <footer class="status" id="status-bar">
      SYSTEM STATUS: INITIALIZING
    </footer>
  </main>

  <script type="application/json" id="tac-data">
__JSON_PAYLOAD__
  </script>

  <script>
    (function () {
      "use strict";

      var DATA = JSON.parse(
        document.getElementById("tac-data").textContent
      );

      var selectedRoutes = {};
      var chart = null;

      var colors = [
        "#f6a623",
        "#42a8f5",
        "#35ca82",
        "#b791ff",
        "#ff6565",
        "#39c7e2"
      ];

      function number(value, digits) {
        if (
          value === null ||
          value === undefined ||
          isNaN(Number(value))
        ) {
          return "--";
        }

        return Number(value).toLocaleString("en-US", {
          minimumFractionDigits: digits,
          maximumFractionDigits: digits
        });
      }

      function percent(value) {
        if (
          value === null ||
          value === undefined ||
          isNaN(Number(value))
        ) {
          return "--";
        }

        var numericValue = Number(value);

        return (
          (numericValue >= 0 ? "+" : "") +
          numericValue.toFixed(2) +
          "%"
        );
      }

      function deltaClass(value) {
        if (value === null || value === undefined) {
          return "flat";
        }

        if (Number(value) > 0) {
          return "up";
        }

        if (Number(value) < 0) {
          return "down";
        }

        return "flat";
      }

      function arrow(value) {
        if (value === null || value === undefined) {
          return "";
        }

        if (Number(value) > 0) {
          return "▲ ";
        }

        if (Number(value) < 0) {
          return "▼ ";
        }

        return "";
      }

      function getSeriesMap() {
        var result = {};

        ((DATA.raw_dataset || {}).series || []).forEach(
          function (item) {
            result[item.name] = item;
          }
        );

        return result;
      }

      function renderKpi() {
        var history = DATA.history || [];
        var latest = history.length
          ? history[history.length - 1]
          : null;

        var detail = DATA.latest_detail || [];

        var cards = [
          {
            label: "LATEST DATE",
            value: latest ? latest.data_date : "--",
            sub: "SOURCE OBSERVATION",
            cls: ""
          },
          {
            label: "ROUTE SPOT AVERAGE",
            value: latest
              ? "$" + number(latest.overall_index, 2)
              : "--",
            sub: "USD / KG",
            cls: "amber"
          },
          {
            label: "PERIODIC DELTA",
            value: latest &&
              latest.weekly_change !== null
              ? '<span class="' +
                deltaClass(latest.weekly_change) +
                '">' +
                arrow(latest.weekly_change) +
                percent(latest.weekly_change) +
                "</span>"
              : "--",
            sub: "AVERAGE ROUTE CHANGE",
            cls: "purple"
          },
          {
            label: "TRACKED CORRIDORS",
            value: (
              latest
                ? latest.route_count
                : detail.length
            ) + " PAIRS",
            sub: "ACTIVE ROUTES",
            cls: "green"
          }
        ];

        document.getElementById("kpi-area").innerHTML =
          cards.map(function (card) {
            return (
              '<div class="kpi ' + card.cls + '">' +
                '<div class="kpi-label">' +
                  card.label +
                "</div>" +
                '<div class="kpi-value">' +
                  card.value +
                "</div>" +
                '<div class="kpi-sub">' +
                  card.sub +
                "</div>" +
              "</div>"
            );
          }).join("");
      }

      function renderButtons() {
        var routeMap = getSeriesMap();
        var routes = Object.keys(routeMap);
        var target = document.getElementById("route-buttons");

        routes.forEach(function (route) {
          if (selectedRoutes[route] === undefined) {
            selectedRoutes[route] = true;
          }
        });

        target.innerHTML = routes.map(function (route) {
          return (
            '<button class="route-btn ' +
            (selectedRoutes[route] ? "active" : "") +
            '" data-route="' +
            route +
            '">' +
            route.toUpperCase() +
            "</button>"
          );
        }).join("");

        target.querySelectorAll(".route-btn").forEach(
          function (button) {
            button.addEventListener("click", function () {
              var route = button.getAttribute("data-route");

              selectedRoutes[route] = !selectedRoutes[route];

              var activeRoutes = Object.keys(
                selectedRoutes
              ).filter(function (key) {
                return selectedRoutes[key];
              });

              if (!activeRoutes.length) {
                selectedRoutes[route] = true;
              }

              renderButtons();
              renderChart();
            });
          }
        );
      }

      function renderChart() {
        if (typeof Chart === "undefined") {
          return;
        }

        var routeMap = getSeriesMap();
        var labels = (
          (DATA.raw_dataset || {}).labels || []
        );

        var activeRoutes = Object.keys(routeMap).filter(
          function (route) {
            return selectedRoutes[route];
          }
        );

        var datasets = activeRoutes.map(
          function (route, index) {
            var color = colors[index % colors.length];

            return {
              label: route.toUpperCase(),
              data: routeMap[route].values,
              borderColor: color,
              backgroundColor: color + "18",
              borderWidth: 2,
              tension: 0.12,
              spanGaps: true,
              pointRadius: 1.5,
              pointHoverRadius: 4,
              pointBackgroundColor: color
            };
          }
        );

        if (chart) {
          chart.data.labels = labels;
          chart.data.datasets = datasets;
          chart.update();
          return;
        }

        chart = new Chart(
          document.getElementById("chart").getContext("2d"),
          {
            type: "line",
            data: {
              labels: labels,
              datasets: datasets
            },
            options: {
              responsive: true,
              maintainAspectRatio: false,
              interaction: {
                mode: "index",
                intersect: false
              },
              plugins: {
                legend: {
                  position: "top",
                  align: "end",
                  labels: {
                    color: "#8298aa",
                    boxWidth: 10,
                    font: {
                      family: "Consolas, monospace",
                      size: 9
                    }
                  }
                },
                tooltip: {
                  backgroundColor: "#07111c",
                  titleColor: "#f6a623",
                  bodyColor: "#f1f6fa",
                  borderColor: "#2b506d",
                  borderWidth: 1,
                  callbacks: {
                    label: function (context) {
                      return (
                        " " +
                        context.dataset.label +
                        ": $" +
                        number(context.parsed.y, 2) +
                        " / kg"
                      );
                    }
                  }
                }
              },
              scales: {
                x: {
                  ticks: {
                    color: "#8298aa",
                    maxTicksLimit: 12,
                    font: {
                      family: "Consolas, monospace",
                      size: 8
                    }
                  },
                  grid: {
                    color: "rgba(28, 52, 72, 0.4)"
                  }
                },
                y: {
                  title: {
                    display: true,
                    text: "USD / KG",
                    color: "#8298aa",
                    font: {
                      family: "Consolas, monospace",
                      size: 8
                    }
                  },
                  ticks: {
                    color: "#8298aa",
                    callback: function (value) {
                      return "$" + Number(value).toFixed(2);
                    }
                  },
                  grid: {
                    color: "rgba(28, 52, 72, 0.4)"
                  }
                }
              }
            }
          }
        );
      }

      function renderWatchlist() {
        var rows = DATA.latest_detail || [];
        var target = document.getElementById("watchlist-body");

        if (!rows.length) {
          target.innerHTML =
            '<tr><td colspan="4" class="flat" ' +
            'style="text-align:center;padding:18px;">' +
            "NO ROUTE RECORDS" +
            "</td></tr>";
          return;
        }

        target.innerHTML = rows.map(function (row) {
          return (
            "<tr>" +
              "<td><strong>" +
                (row.route_code || "--") +
              "</strong></td>" +
              '<td style="font-weight:800;">$' +
                number(row.index_value, 2) +
              "</td>" +
              '<td class="' +
                deltaClass(row.weekly_change) +
                '">' +
                arrow(row.weekly_change) +
                percent(row.weekly_change) +
              "</td>" +
              '<td class="flat">' +
                (row.data_date || "--") +
              "</td>" +
            "</tr>"
          );
        }).join("");
      }

      function renderStatus() {
        var sync = DATA.last_sync || {};
        var status = sync.status || "NO_AUDIT_LOG";
        var detail = sync.message || "Historical data loaded.";

        document.getElementById("status-bar").innerHTML =
          "SYSTEM STATUS: <strong>" +
          status +
          "</strong> | " +
          detail;
      }

      renderKpi();
      renderButtons();
      renderChart();
      renderWatchlist();
      renderStatus();
    })();
  </script>
</body>
</html>
"""

def build_static_html(payload: Dict[str, Any]) -> None:
    """Generate GitHub Pages static dashboard."""
    json_payload_text = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
    ).replace("</", "<\\/")

    page = (
        HTML_TEMPLATE
        .replace("__PARENT_URL__", PARENT_URL)
        .replace("__GENERATED_AT__", get_now_string())
        .replace("__JSON_PAYLOAD__", json_payload_text)
    )

    atomic_write_text(HTML_PATH, page)

    log_info(
        f"[STATIC] HTML generated: {HTML_PATH.name}"
    )

# =============================================================================
# 10. Main crawling process
# =============================================================================
def run_tac_sync() -> Dict[str, Any]:
    """Crawl source and synchronize historical database."""
    started_at = get_now_string()

    try:
        embed_html = fetch_infogram_html()
        infographic_data = extract_infographic_data(embed_html)
        chart_rows = extract_best_chart_rows(infographic_data)
        dataset = build_dataset(chart_rows)

        bundles = build_all_snapshot_bundles(dataset)

        sync_result = save_all_snapshots(bundles)

        latest_bundle = bundles[-1]
        finished_at = get_now_string()

        save_crawl_audit(
            started_at=started_at,
            finished_at=finished_at,
            status="SUCCESS",
            message=(
                "HISTORICAL_SYNC "
                f"new={sync_result['new_count']}, "
                f"updated={sync_result['updated_count']}, "
                f"unchanged={sync_result['unchanged_count']}, "
                f"total={len(bundles)}"
            ),
            snapshot_id=sync_result["latest_snapshot_id"],
            route_count=len(latest_bundle["details"]),
            data_date=latest_bundle["data_date"],
        )

        log_info(
            "[SUCCESS] TAC historical sync completed: "
            f"new={sync_result['new_count']}, "
            f"updated={sync_result['updated_count']}, "
            f"unchanged={sync_result['unchanged_count']}, "
            f"total={len(bundles)}, "
            f"latest_date={latest_bundle['data_date']}"
        )

        return {
            "ok": True,
            "snapshot_id": sync_result["latest_snapshot_id"],
            "is_new": sync_result["new_count"] > 0,
            "new_count": sync_result["new_count"],
            "updated_count": sync_result["updated_count"],
            "unchanged_count": sync_result["unchanged_count"],
            "dataset": dataset,
            "bundle": latest_bundle,
            "bundles": bundles,
        }

    except Exception as exc:
        finished_at = get_now_string()

        logger.exception(
            f"[ERROR] TAC collection failed: {exc}"
        )

        try:
            save_crawl_audit(
                started_at=started_at,
                finished_at=finished_at,
                status="ERROR",
                message=str(exc),
            )
        except Exception as audit_exc:
            logger.error(
                "[ERROR] Crawl audit save failed: "
                f"{audit_exc}"
            )

        return {
            "ok": False,
            "error": str(exc),
        }

# =============================================================================
# 11. Entry point
# =============================================================================
def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "TAC Index / Baltic Exchange "
            "Airfreight Rate Index crawler"
        )
    )

    parser.add_argument(
        "--cron",
        action="store_true",
        help="Run as scheduled GitHub Actions task.",
    )

    parser.add_argument(
        "--allow-empty-success",
        action="store_true",
        help=(
            "Return exit code 0 if crawling fails "
            "without any historical snapshot."
        ),
    )

    return parser.parse_args()

def main() -> int:
    """Run scheduled crawler and static dashboard generation."""
    args = parse_arguments()

    log_info("=" * 72)
    log_info("[CRON] TAC Index dashboard update started.")
    log_info("=" * 72)

    conn = get_db_connection()
    conn.close()

    result = run_tac_sync()

    payload = build_static_json()
    build_static_html(payload)

    if result.get("ok"):
        log_info(
            "[CRON] TAC Index dashboard update completed."
        )
        log_info("=" * 72)
        return 0

    historical_count = get_total_snapshot_count()

    if historical_count > 0:
        log_warning(
            "[WARN] Fresh TAC collection failed. "
            "Historical DB retained. "
            "Static dashboard rebuilt."
        )
        log_info("=" * 72)
        return 0

    if args.allow_empty_success:
        log_warning(
            "[WARN] Fresh TAC collection failed. "
            "Historical DB unavailable. "
            "Empty static dashboard generated."
        )
        log_info("=" * 72)
        return 0

    log_warning(
        "[ERROR] Fresh TAC collection failed. "
        "Historical DB unavailable. "
        "Exit code=1."
    )
    log_info("=" * 72)

    return 1

if __name__ == "__main__":
    sys.exit(main())
