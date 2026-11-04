#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Linerlytica Global Containership Fleet Snapshot Crawler & Dashboard Generator

기능
----------------------------------------------------------------
1. Linerlytica Global Containership Fleet Snapshot을 수집한다.
2. 매월 17일자 데이터만 SQLite에 누적 저장한다.
3. 누락 또는 불완전한 월은 Backfill 방식으로 재수집한다.
4. 필수 상태 5개가 모두 확보된 경우에만 DB에 저장한다.
5. SQLite 이력을 기반으로 Linerlytica/index.html을 생성한다.

필수 패키지
----------------------------------------------------------------
pip install beautifulsoup4 playwright
python -m playwright install chromium

환경변수
----------------------------------------------------------------
LINERLYTICA_URL
    기본값: https://www.linerlytica.com/

LINERLYTICA_START_DATE
    백필 시작일.
    기본값: 2024-01-17

LINERLYTICA_DATE_INPUT_SELECTOR
    날짜 입력 selector.
    기본값: input.dx-texteditor-input

LINERLYTICA_DATE_INPUT_FORMAT
    날짜 입력 표시 형식.
    기본값: %m/%d/%Y

LINERLYTICA_STORAGE_STATE
    로그인 세션이 필요할 때 Playwright storage state JSON 파일 경로.

LINERLYTICA_HEADLESS
    true / false
    기본값: true
"""

from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from bs4 import BeautifulSoup

try:
    from playwright.sync_api import (
        Browser,
        Error as PlaywrightError,
        Page,
        TimeoutError as PlaywrightTimeoutError,
        sync_playwright,
    )
except ImportError:
    print(
        "[FATAL] Playwright가 설치되어 있지 않습니다.\n"
        "다음 명령을 실행하세요:\n"
        "  pip install playwright\n"
        "  python -m playwright install chromium"
    )
    sys.exit(1)

# =============================================================================
# 1. 기본 설정
# =============================================================================

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "Linerlytica.db"
HTML_PATH = BASE_DIR / "index.html"

TARGET_URL = os.getenv("LINERLYTICA_URL", "https://www.linerlytica.com/")
START_DATE_STR = os.getenv("LINERLYTICA_START_DATE", "2024-01-17")

DATE_INPUT_SELECTOR = os.getenv(
    "LINERLYTICA_DATE_INPUT_SELECTOR",
    "input.dx-texteditor-input",
).strip()

DATE_INPUT_FORMAT = os.getenv(
    "LINERLYTICA_DATE_INPUT_FORMAT",
    "%m/%d/%Y",
).strip()

STORAGE_STATE_PATH = os.getenv("LINERLYTICA_STORAGE_STATE", "").strip()

HEADLESS = os.getenv("LINERLYTICA_HEADLESS", "true").strip().lower() not in {
    "false",
    "0",
    "no",
}

PAGE_TIMEOUT_MS = 60_000
DATE_CHANGE_WAIT_MS = 3_000
DATA_RENDER_WAIT_MS = 2_000

# =============================================================================
# 2. 상태 정의
# =============================================================================

REQUIRED_STATUSES: Tuple[str, ...] = (
    "Ships at port",
    "Active Ships",
    "Inactive Ships",
    "Ships at anchorage",
    "Ships in shipyard",
)

STATUS_ALIASES = {
    "ships at port": "Ships at port",
    "ships at ports": "Ships at port",
    "ships in port": "Ships at port",
    "ships in ports": "Ships at port",
    "active ships": "Active Ships",
    "inactive ships": "Inactive Ships",
    "ships at anchorage": "Ships at anchorage",
    "ships at anchor": "Ships at anchorage",
    "ships in shipyard": "Ships in shipyard",
    "ships in shipyards": "Ships in shipyard",
    "ships at shipyard": "Ships in shipyard",
}

# =============================================================================
# 3. 예외 및 유틸리티
# =============================================================================

class DateSelectionError(Exception):
    """웹페이지에서 목표 날짜 선택에 실패한 경우."""

class IncompleteSnapshotError(Exception):
    """필수 Fleet Snapshot 상태가 모두 확보되지 않은 경우."""

def utc_now_string() -> str:
    """UTC 기준 현재 시간을 문자열로 반환한다."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def normalize_space(value: str) -> str:
    """연속 공백, 줄바꿈, 탭을 단일 공백으로 표준화한다."""
    return re.sub(r"\s+", " ", value or "").strip()

def normalize_status(value: str) -> Optional[str]:
    """페이지 원문 상태명을 표준 상태명으로 변환한다."""
    return STATUS_ALIASES.get(normalize_space(value).lower())

def parse_integer(value: str) -> Optional[int]:
    """콤마 포함 숫자 문자열을 int로 변환한다."""
    if value is None:
        return None

    cleaned = re.sub(r"[^\d]", "", str(value))

    if not cleaned:
        return None

    try:
        return int(cleaned)
    except ValueError:
        return None

def format_number(value: Optional[int]) -> str:
    """숫자를 천 단위 구분자 형식으로 표시한다."""
    return "-" if value is None else f"{value:,}"

def safe_percent_change(
    current: Optional[int],
    previous: Optional[int],
) -> Optional[float]:
    """직전 값 대비 증감률을 계산한다."""
    if current is None or previous in (None, 0):
        return None

    return ((current - previous) / previous) * 100

def format_delta(
    current: Optional[int],
    previous: Optional[int],
) -> str:
    """현재값과 이전값의 증감 HTML을 생성한다."""
    if current is None or previous is None:
        return '<span class="neutral">-</span>'

    delta = current - previous
    pct = safe_percent_change(current, previous)
    pct_text = f"{pct:+.1f}%" if pct is not None else "-"

    if delta > 0:
        return f'<span class="up">↗ +{delta:,} ({pct_text})</span>'

    if delta < 0:
        return f'<span class="down">↘ {delta:,} ({pct_text})</span>'

    return '<span class="neutral">→ 0 (0.0%)</span>'

def month_17th_dates(start_date: date, end_date: date) -> List[str]:
    """시작일부터 종료일까지 매월 17일 목록을 반환한다."""
    results: List[str] = []
    year = start_date.year
    month = start_date.month

    while True:
        candidate = date(year, month, 17)

        if start_date <= candidate <= end_date:
            results.append(candidate.isoformat())

        if year == end_date.year and month == end_date.month:
            break

        month += 1

        if month == 13:
            year += 1
            month = 1

    return results

# =============================================================================
# 4. SQLite 관리
# =============================================================================

def init_db() -> sqlite3.Connection:
    """SQLite 데이터베이스 및 테이블을 초기화한다."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fleet_snapshot (
            snapshot_date TEXT NOT NULL,
            ship_status TEXT NOT NULL,
            ships INTEGER NOT NULL,
            teu INTEGER NOT NULL,
            source_url TEXT,
            collected_at TEXT NOT NULL,
            PRIMARY KEY (snapshot_date, ship_status)
        )
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_fleet_snapshot_date
        ON fleet_snapshot(snapshot_date)
        """
    )

    conn.commit()

    return conn

def get_completed_dates(conn: sqlite3.Connection) -> Set[str]:
    """필수 5개 상태가 모두 존재하는 완료 스냅샷 날짜를 조회한다."""
    rows = conn.execute(
        """
        SELECT snapshot_date, ship_status
        FROM fleet_snapshot
        """
    ).fetchall()

    status_by_date: Dict[str, Set[str]] = {}

    for row in rows:
        status_by_date.setdefault(row["snapshot_date"], set()).add(
            row["ship_status"]
        )

    required = set(REQUIRED_STATUSES)

    return {
        snapshot_date
        for snapshot_date, statuses in status_by_date.items()
        if required.issubset(statuses)
    }

def save_snapshot(
    conn: sqlite3.Connection,
    snapshot_date: str,
    data_rows: List[Dict[str, int]],
    source_url: str,
) -> None:
    """검증된 월별 Fleet Snapshot을 저장한다."""
    statuses = {row["ship_status"] for row in data_rows}
    missing = set(REQUIRED_STATUSES) - statuses

    if missing:
        raise IncompleteSnapshotError(
            f"{snapshot_date} DB 저장 불가. "
            f"누락 상태: {', '.join(sorted(missing))}"
        )

    collected_at = utc_now_string()

    with conn:
        conn.execute(
            """
            DELETE FROM fleet_snapshot
            WHERE snapshot_date = ?
            """,
            (snapshot_date,),
        )

        for row in data_rows:
            conn.execute(
                """
                INSERT INTO fleet_snapshot (
                    snapshot_date,
                    ship_status,
                    ships,
                    teu,
                    source_url,
                    collected_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_date,
                    row["ship_status"],
                    int(row["ships"]),
                    int(row["teu"]),
                    source_url,
                    collected_at,
                ),
            )

    print(
        f"  [DB Saved] {snapshot_date} | "
        f"{len(data_rows)} statuses | collected_at={collected_at}"
    )

# =============================================================================
# 5. 데이터 파싱
# =============================================================================

def parse_snapshot_table_html(html_text: str) -> List[Dict[str, int]]:
    """일반 HTML table 구조에서 Fleet Snapshot 데이터를 파싱한다."""
    soup = BeautifulSoup(html_text, "html.parser")
    parsed: Dict[str, Dict[str, int]] = {}

    for table in soup.find_all("table"):
        table_text = normalize_space(table.get_text(" ", strip=True)).lower()

        if "ship status" not in table_text:
            continue

        if "ships" not in table_text or "teu" not in table_text:
            continue

        for tr in table.find_all("tr"):
            cells = [
                normalize_space(cell.get_text(" ", strip=True))
                for cell in tr.find_all(["td", "th"])
            ]

            if len(cells) < 3:
                continue

            status = normalize_status(cells[0])
            ships = parse_integer(cells[1])
            teu = parse_integer(cells[2])

            if status and ships is not None and teu is not None:
                parsed[status] = {
                    "ship_status": status,
                    "ships": ships,
                    "teu": teu,
                }

    return [
        parsed[status]
        for status in REQUIRED_STATUSES
        if status in parsed
    ]

def extract_snapshot_section(page_text: str) -> str:
    """
    화면 전체 텍스트에서 Global Containership Fleet Snapshot 구간만 추출한다.

    Port Congestion 표에도 Ships / TEU 숫자가 반복되므로, Fleet Snapshot과
    다음 섹션인 Global Containership Port Congestion 사이만 사용한다.
    """
    text = normalize_space(page_text)

    start_match = re.search(
        r"Global\s+Containership\s+Fleet\s+Snapshot\s+as\s+of",
        text,
        flags=re.IGNORECASE,
    )

    if not start_match:
        return text

    section = text[start_match.start():]

    end_match = re.search(
        r"Global\s+Containership\s+Port\s+Congestion\s+as\s+of",
        section,
        flags=re.IGNORECASE,
    )

    if end_match:
        section = section[:end_match.start()]

    return normalize_space(section)

def parse_snapshot_from_text(page_text: str) -> List[Dict[str, int]]:
    """
    DevExtreme Grid 화면 텍스트에서 Fleet Snapshot 데이터를 파싱한다.

    핵심 수정:
    ----------------------------------------------------------------
    기존 r"active\\s+ships" 패턴은 "Inactive Ships"의 일부 문자열인
    "active ships"를 매칭할 수 있다.

    따라서 Active Ships는 (?<!in)active와 word boundary를 사용하여
    Inactive Ships와 명확히 분리한다.

    실제 대상 텍스트 예:
        Ship Status Ships TEU Total TEU
        Ships at port 1,129 5,639,764
        Active Ships 5,751 24,488,025
        Inactive Ships 74 177,760
        Ships at anchorage 968 4,182,132
        Ships in shipyard 235 832,537
    """
    snapshot_text = extract_snapshot_section(page_text)
    parsed: Dict[str, Dict[str, int]] = {}

    status_patterns = {
        "Ships at port": r"\bships\s+at\s+port(?:s)?\b",
        # (?<!in)은 "Inactive Ships" 내부의 active 매칭 방지
        "Active Ships": r"(?<!in)\bactive\s+ships\b",
        "Inactive Ships": r"\binactive\s+ships\b",
        "Ships at anchorage": r"\bships\s+at\s+anchorage\b",
        "Ships in shipyard": r"\bships\s+in\s+shipyard(?:s)?\b",
    }

    status_matches: List[Tuple[int, int, str]] = []

    for status, pattern in status_patterns.items():
        match = re.search(pattern, snapshot_text, flags=re.IGNORECASE)

        if match:
            status_matches.append((match.start(), match.end(), status))

    status_matches.sort(key=lambda item: item[0])

    for index, (_, match_end, status) in enumerate(status_matches):
        next_start = (
            status_matches[index + 1][0]
            if index + 1 < len(status_matches)
            else len(snapshot_text)
        )

        # 현재 상태명 종료 직후부터 다음 상태명 직전까지가 숫자 영역이다.
        value_segment = snapshot_text[match_end:next_start]

        number_tokens = re.findall(
            r"(?<![\d,])\d{1,3}(?:,\d{3})+(?![\d,])"
            r"|(?<![\d,])\d+(?![\d,])",
            value_segment,
        )

        values: List[int] = []

        for token in number_tokens:
            number = parse_integer(token)

            if number is not None:
                values.append(number)

        if len(values) < 2:
            continue

        ships = values[0]
        teu = values[1]

        if ships <= 0 or teu <= 0:
            continue

        parsed[status] = {
            "ship_status": status,
            "ships": ships,
            "teu": teu,
        }

    return [
        parsed[status]
        for status in REQUIRED_STATUSES
        if status in parsed
    ]

def parse_snapshot_data(
    html_text: str,
    page_text: str,
) -> List[Dict[str, int]]:
    """HTML table 우선, 화면 텍스트 fallback 순으로 데이터를 파싱한다."""
    html_rows = parse_snapshot_table_html(html_text)
    html_statuses = {row["ship_status"] for row in html_rows}

    if set(REQUIRED_STATUSES).issubset(html_statuses):
        return html_rows

    text_rows = parse_snapshot_from_text(page_text)

    merged: Dict[str, Dict[str, int]] = {
        row["ship_status"]: row
        for row in html_rows
    }

    for row in text_rows:
        merged[row["ship_status"]] = row

    return [
        merged[status]
        for status in REQUIRED_STATUSES
        if status in merged
    ]

# =============================================================================
# 6. Playwright 제어
# =============================================================================

def dismiss_cookie_banner(page: Page) -> None:
    """Cookie Consent 배너를 닫는다."""
    try:
        button = page.locator("#rcc-confirm-button")

        if button.count() > 0 and button.is_visible():
            button.click(timeout=5_000)
            page.wait_for_timeout(500)
            print("  [Cookie] Consent banner accepted.")

    except Exception as exc:
        print(f"  [Cookie] Banner handling skipped: {exc}")

def wait_for_loading_complete(page: Page) -> None:
    """Loading... 문구가 사라질 때까지 대기한다."""
    try:
        page.wait_for_function(
            """
            () => !document.body.innerText.includes('Loading...')
            """,
            timeout=20_000,
        )
    except PlaywrightTimeoutError:
        print("  [Warning] Loading... 문구가 남아 있어 계속 진행합니다.")

def candidate_date_selectors() -> List[str]:
    """날짜 입력 후보 selector 목록을 반환한다."""
    candidates = [
        DATE_INPUT_SELECTOR,
        "input.dx-texteditor-input",
        "input[type='date']",
        "input[name='date']",
        "input[name='snapshot_date']",
        "input[name='data_date']",
        "input[id*='date' i]",
        "input[class*='date' i]",
    ]

    seen = set()
    result = []

    for selector in candidates:
        if selector and selector not in seen:
            seen.add(selector)
            result.append(selector)

    return result

def get_hidden_values(page: Page) -> List[str]:
    """모든 hidden input의 value 목록을 반환한다."""
    values: List[str] = []
    locator = page.locator("input[type='hidden']")

    for index in range(locator.count()):
        try:
            value = locator.nth(index).get_attribute("value")

            if value:
                values.append(value.strip())
        except PlaywrightError:
            continue

    return values

def select_target_date(page: Page, target_date: str) -> str:
    """DevExtreme DateBox에 목표 날짜를 입력하고 변경 여부를 검증한다."""
    target_dt = datetime.strptime(target_date, "%Y-%m-%d")
    displayed_date = target_dt.strftime(DATE_INPUT_FORMAT)

    errors: List[str] = []

    for selector in candidate_date_selectors():
        try:
            locator = page.locator(selector).first

            if locator.count() == 0:
                continue

            locator.wait_for(state="visible", timeout=15_000)
            locator.scroll_into_view_if_needed(timeout=5_000)
            locator.click(timeout=8_000)
            locator.press("Control+A", timeout=5_000)

            try:
                locator.fill(displayed_date, timeout=8_000)
            except PlaywrightError:
                locator.evaluate(
                    """
                    (element, value) => {
                        const setter = Object.getOwnPropertyDescriptor(
                            HTMLInputElement.prototype,
                            "value"
                        ).set;

                        setter.call(element, value);

                        element.dispatchEvent(
                            new Event("input", { bubbles: true })
                        );
                        element.dispatchEvent(
                            new Event("change", { bubbles: true })
                        );
                        element.dispatchEvent(
                            new Event("blur", { bubbles: true })
                        );
                    }
                    """,
                    displayed_date,
                )

            try:
                locator.press("Enter", timeout=5_000)
            except PlaywrightError:
                pass

            try:
                locator.press("Tab", timeout=5_000)
            except PlaywrightError:
                pass

            page.wait_for_timeout(DATE_CHANGE_WAIT_MS)
            wait_for_loading_complete(page)
            page.wait_for_timeout(DATA_RENDER_WAIT_MS)

            visible_value = ""

            try:
                visible_value = locator.input_value(timeout=5_000).strip()
            except PlaywrightError:
                pass

            hidden_values = get_hidden_values(page)

            allowed_visible_values = {
                target_date,
                displayed_date,
                target_dt.strftime("%Y-%m-%d"),
            }

            if (
                visible_value not in allowed_visible_values
                and target_date not in hidden_values
            ):
                raise DateSelectionError(
                    f"날짜 변경 검증 실패: "
                    f"visible={visible_value!r}, "
                    f"hidden={hidden_values}"
                )

            return selector

        except Exception as exc:
            errors.append(f"{selector}: {exc}")

    raise DateSelectionError(
        f"날짜 선택 실패: {target_date}\n"
        f"시도 selector: {candidate_date_selectors()}\n"
        f"상세: {' | '.join(errors)}"
    )

def crawl_one_snapshot(page: Page, target_date: str) -> List[Dict[str, int]]:
    """특정 날짜의 Fleet Snapshot을 수집한다."""
    page.goto(
        TARGET_URL,
        wait_until="domcontentloaded",
        timeout=PAGE_TIMEOUT_MS,
    )

    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except PlaywrightTimeoutError:
        pass

    dismiss_cookie_banner(page)
    wait_for_loading_complete(page)

    selector = select_target_date(page, target_date)

    print(f"  [Date Selected] {target_date} | selector={selector}")

    html_text = page.content()
    page_text = page.locator("body").inner_text(timeout=15_000)

    data_rows = parse_snapshot_data(
        html_text=html_text,
        page_text=page_text,
    )

    found = {row["ship_status"] for row in data_rows}
    missing = set(REQUIRED_STATUSES) - found

    if missing:
        snapshot_preview = extract_snapshot_section(page_text)[:900]

        raise IncompleteSnapshotError(
            f"{target_date} 표 데이터 불완전. "
            f"수집 상태={sorted(found)} | "
            f"누락 상태={sorted(missing)} | "
            f"Snapshot 미리보기={snapshot_preview!r}"
        )

    return data_rows

def crawl_and_backfill(conn: sqlite3.Connection) -> None:
    """DB에서 누락되거나 불완전한 월별 17일자 데이터를 수집한다."""
    try:
        start_date = datetime.strptime(
            START_DATE_STR,
            "%Y-%m-%d",
        ).date()
    except ValueError as exc:
        raise ValueError(
            "LINERLYTICA_START_DATE는 YYYY-MM-DD 형식이어야 합니다. "
            f"현재 값: {START_DATE_STR}"
        ) from exc

    today = datetime.now(timezone.utc).date()

    if today.day < 17:
        if today.month == 1:
            end_date = date(today.year - 1, 12, 17)
        else:
            end_date = date(today.year, today.month - 1, 17)
    else:
        end_date = today

    target_dates = month_17th_dates(start_date, end_date)
    completed_dates = get_completed_dates(conn)

    missing_dates = [
        snapshot_date
        for snapshot_date in target_dates
        if snapshot_date not in completed_dates
    ]

    if not missing_dates:
        print("[Crawler] 모든 월별 17일자 스냅샷이 최신 상태입니다.")
        return

    print(f"[Crawler] Missing / incomplete dates: {len(missing_dates)}")
    print(f"[Crawler] Targets: {missing_dates}")

    storage_state = None

    if STORAGE_STATE_PATH:
        state_path = Path(STORAGE_STATE_PATH)

        if not state_path.is_absolute():
            state_path = BASE_DIR.parent / state_path

        if state_path.exists():
            storage_state = str(state_path)
            print(f"[Crawler] Using storage state: {storage_state}")
        else:
            print(f"[Warning] Storage state file not found: {state_path}")

    with sync_playwright() as playwright:
        try:
            browser: Browser = playwright.chromium.launch(headless=HEADLESS)
        except PlaywrightError as exc:
            raise RuntimeError(
                "Chromium을 실행할 수 없습니다.\n"
                "다음 명령을 실행하세요:\n"
                "  python -m playwright install chromium\n"
                f"원본 오류: {exc}"
            ) from exc

        context_kwargs = {
            "user_agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
            "viewport": {"width": 1600, "height": 1200},
            "locale": "en-US",
            "timezone_id": "UTC",
        }

        if storage_state:
            context_kwargs["storage_state"] = storage_state

        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT_MS)

        try:
            for target_date in missing_dates:
                print(f"[Crawler] Processing: {target_date}")

                try:
                    data_rows = crawl_one_snapshot(page, target_date)

                    save_snapshot(
                        conn=conn,
                        snapshot_date=target_date,
                        data_rows=data_rows,
                        source_url=page.url or TARGET_URL,
                    )

                except DateSelectionError as exc:
                    print(f"  [Skip: Date Selection Failed] {exc}")

                except IncompleteSnapshotError as exc:
                    print(f"  [Skip: Incomplete Data] {exc}")

                except PlaywrightTimeoutError as exc:
                    print(f"  [Skip: Timeout] {target_date} | {exc}")

                except Exception as exc:
                    print(
                        f"  [Skip: Unexpected Error] {target_date} | "
                        f"{type(exc).__name__}: {exc}"
                    )

        finally:
            context.close()
            browser.close()

# =============================================================================
# 7. Dashboard 생성
# =============================================================================

def get_snapshot_data(
    conn: sqlite3.Connection,
    snapshot_date: str,
) -> Dict[str, Dict[str, int]]:
    """특정 스냅샷 날짜의 상태별 데이터를 조회한다."""
    rows = conn.execute(
        """
        SELECT ship_status, ships, teu
        FROM fleet_snapshot
        WHERE snapshot_date = ?
        """,
        (snapshot_date,),
    ).fetchall()

    return {
        row["ship_status"]: {
            "ships": row["ships"],
            "teu": row["teu"],
        }
        for row in rows
    }

def generate_dashboard_html(conn: sqlite3.Connection) -> None:
    """
    SQLite 데이터를 기반으로 MI Terminal 표준 정적 HTML 대시보드를 생성한다.

    공통 CSS:
    - ../assets/mi_terminal.css

    데이터 원칙:
    - fleet_snapshot의 완전한 5개 상태가 존재하는 날짜만 사용
    - 최신 스냅샷 기준 Ships 내림차순으로 Watchlist 정렬
    """
    completed_dates = sorted(get_completed_dates(conn))

    if not completed_dates:
        print("[Dashboard] 완전한 스냅샷 데이터가 없어 HTML 생성을 건너뜁니다.")
        return

    latest_date = completed_dates[-1]
    previous_date = completed_dates[-2] if len(completed_dates) >= 2 else None

    latest_data = get_snapshot_data(conn, latest_date)
    previous_data = (
        get_snapshot_data(conn, previous_date)
        if previous_date
        else {}
    )

    history_rows = conn.execute(
        """
        SELECT snapshot_date, ship_status, ships, teu
        FROM fleet_snapshot
        ORDER BY snapshot_date ASC, ship_status ASC
        """
    ).fetchall()

    history_map = {
        (row["snapshot_date"], row["ship_status"]): {
            "ships": row["ships"],
            "teu": row["teu"],
        }
        for row in history_rows
    }

    teu_series: Dict[str, List[Optional[int]]] = {
        status: [] for status in REQUIRED_STATUSES
    }

    ships_series: Dict[str, List[Optional[int]]] = {
        status: [] for status in REQUIRED_STATUSES
    }

    for snapshot_date in completed_dates:
        for status in REQUIRED_STATUSES:
            record = history_map.get((snapshot_date, status))

            teu_series[status].append(
                record["teu"] if record else None
            )
            ships_series[status].append(
                record["ships"] if record else None
            )

    sorted_statuses = sorted(
        REQUIRED_STATUSES,
        key=lambda status: latest_data.get(status, {}).get("ships", 0),
        reverse=True,
    )

    active_latest = latest_data.get("Active Ships", {})
    active_previous = previous_data.get("Active Ships", {})

    inactive_latest = latest_data.get("Inactive Ships", {})
    inactive_previous = previous_data.get("Inactive Ships", {})

    port_latest = latest_data.get("Ships at port", {})
    port_previous = previous_data.get("Ships at port", {})

    anchorage_latest = latest_data.get("Ships at anchorage", {})
    shipyard_latest = latest_data.get("Ships in shipyard", {})

    active_teu = active_latest.get("teu", 0)
    inactive_teu = inactive_latest.get("teu", 0)
    port_teu = port_latest.get("teu", 0)
    anchorage_teu = anchorage_latest.get("teu", 0)
    shipyard_teu = shipyard_latest.get("teu", 0)

    active_teu_change = (
        active_latest.get("teu", 0) - active_previous.get("teu", 0)
        if active_previous.get("teu") is not None
        else None
    )

    inactive_teu_change = (
        inactive_latest.get("teu", 0) - inactive_previous.get("teu", 0)
        if inactive_previous.get("teu") is not None
        else None
    )

    port_teu_change = (
        port_latest.get("teu", 0) - port_previous.get("teu", 0)
        if port_previous.get("teu") is not None
        else None
    )

    if active_teu_change is None:
        active_signal = "STABLE"
    elif active_teu_change > 0:
        active_signal = "EXPANDING"
    elif active_teu_change < 0:
        active_signal = "CONTRACTING"
    else:
        active_signal = "STABLE"

    if inactive_teu >= 1_000_000:
        risk_assessment = "ELEVATED"
        risk_class = "down"
    elif inactive_teu >= 500_000:
        risk_assessment = "MONITORED"
        risk_class = "neutral"
    else:
        risk_assessment = "NORMAL"
        risk_class = "up"

    ticker_items = ""

    for status in REQUIRED_STATUSES:
        current_ships = latest_data.get(status, {}).get("ships")
        previous_ships = previous_data.get(status, {}).get("ships")

        if current_ships is None or previous_ships is None:
            delta_class = "neutral"
            delta_text = "N/A"
        else:
            delta = current_ships - previous_ships

            if delta > 0:
                delta_class = "up"
                delta_text = f"↗ +{delta:,}"
            elif delta < 0:
                delta_class = "down"
                delta_text = f"↘ {delta:,}"
            else:
                delta_class = "neutral"
                delta_text = "→ 0"

        ticker_items += f"""
        <div class="mi-ticker fleet-ticker">
            <span class="fleet-ticker-label">{html.escape(status)}</span>
            <strong class="fleet-ticker-value">{format_number(current_ships)}</strong>
            <span class="{delta_class} fleet-ticker-delta">{delta_text}</span>
        </div>
        """

    table_rows = ""

    for status in sorted_statuses:
        latest = latest_data.get(status, {})
        previous = previous_data.get(status, {})

        table_rows += f"""
        <tr>
            <td class="fleet-status-name">{html.escape(status)}</td>
            <td>{format_number(latest.get("ships"))}</td>
            <td>{format_number(latest.get("teu"))}</td>
            <td>{format_delta(latest.get("ships"), previous.get("ships"))}</td>
            <td>{format_delta(latest.get("teu"), previous.get("teu"))}</td>
        </tr>
        """

    max_teu_val = max(
        active_teu,
        port_teu,
        anchorage_teu,
        inactive_teu,
        shipyard_teu,
        1,
    )

    allocation_data = [
        ("Active Ships", active_teu, (active_teu / max_teu_val) * 100, "#31d0a0"),
        ("Ships at port", port_teu, (port_teu / max_teu_val) * 100, "#f5a623"),
        ("Ships at anchorage", anchorage_teu, (anchorage_teu / max_teu_val) * 100, "#4fa3ff"),
        ("Inactive Ships", inactive_teu, (inactive_teu / max_teu_val) * 100, "#ff5b68"),
        ("Ships in shipyard", shipyard_teu, (shipyard_teu / max_teu_val) * 100, "#27c7e8"),
    ]

    allocation_rows = ""

    for status, teu_value, width_pct, color in allocation_data:
        allocation_rows += f"""
        <div class="fleet-allocation-row">
            <div class="fleet-allocation-header">
                <span>{html.escape(status)}</span>
                <strong>{format_number(teu_value)} TEU</strong>
            </div>
            <div class="fleet-progress-track">
                <span
                    style="width:{min(width_pct, 100):.2f}%; background:{color};"
                ></span>
            </div>
            <div class="fleet-allocation-footer">
                <span>CAPACITY LEVEL (RELATIVE SCALE)</span>
            </div>
        </div>
        """

    generated_at = utc_now_string()

    dashboard_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">

<title>Linerlytica | MI Terminal</title>

<link rel="stylesheet" href="../assets/mi_terminal.css">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.6/dist/chart.umd.min.js"></script>

<style>
/* =========================================================
   Linerlytica Module Extension
   공통 assets/mi_terminal.css 위에서만 동작하는 모듈 전용 보완 스타일
   ========================================================= */

body {{
    background: var(--mi-bg);
}}

.mi-page {{
    width: min(1920px, calc(100% - 20px));
    margin: 0 auto;
    padding: 10px 0 18px;
}}

.mi-terminal-header {{
    display: flex;
    align-items: stretch;
    justify-content: space-between;
    min-height: 56px;
    border: 1px solid var(--mi-border);
    background: var(--mi-header);
}}

.mi-terminal-brand {{
    display: flex;
    align-items: center;
    justify-content: center;
    width: 62px;
    flex: 0 0 62px;
    color: var(--mi-bg-deep);
    background: var(--mi-amber);
    font-size: 14px;
    font-weight: 900;
    letter-spacing: .12em;
}}

.mi-terminal-title {{
    flex: 1;
    min-width: 0;
    padding: 10px 14px;
}}

.mi-terminal-title h1 {{
    margin: 0;
    color: var(--mi-white);
    font-size: 14px;
    font-weight: 900;
    letter-spacing: .08em;
    text-transform: uppercase;
}}

.mi-terminal-title p {{
    margin: 5px 0 0;
    color: var(--mi-text-muted);
    font-size: 9px;
    font-weight: 700;
    letter-spacing: .05em;
    text-transform: uppercase;
}}

.mi-terminal-meta {{
    min-width: 350px;
    padding: 9px 14px;
    border-left: 1px solid var(--mi-border);
    color: var(--mi-text-muted);
    font-size: 9px;
    font-weight: 700;
    letter-spacing: .04em;
    line-height: 1.7;
    text-align: right;
    text-transform: uppercase;
}}

.mi-terminal-meta strong {{
    color: var(--mi-white);
}}

.fleet-ticker-wrap {{
    display: flex;
    margin-top: 6px;
    overflow-x: auto;
    border: 1px solid var(--mi-border);
    background: var(--mi-bg-deep);
}}

.fleet-ticker-heading {{
    display: flex;
    align-items: center;
    flex: 0 0 118px;
    padding: 0 12px;
    color: var(--mi-bg-deep);
    background: var(--mi-amber);
    font-size: 9px;
    font-weight: 900;
    letter-spacing: .08em;
    text-transform: uppercase;
}}

.fleet-ticker {{
    min-width: 215px;
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 3px 10px;
    padding: 8px 12px;
    border-right: 1px solid var(--mi-border);
}}

.fleet-ticker-label {{
    overflow: hidden;
    color: var(--mi-text-muted);
    font-size: 9px;
    font-weight: 700;
    letter-spacing: .04em;
    text-overflow: ellipsis;
    text-transform: uppercase;
    white-space: nowrap;
}}

.fleet-ticker-value {{
    color: var(--mi-white);
    font-size: 12px;
    font-variant-numeric: tabular-nums;
    text-align: right;
}}

.fleet-ticker-delta {{
    grid-column: 1 / -1;
    font-size: 9px;
    font-weight: 800;
    text-align: right;
}}

.fleet-grid {{
    display: grid;
    grid-template-columns: repeat(12, minmax(0, 1fr));
    gap: 8px;
    margin-top: 8px;
}}

.fleet-panel {{
    min-width: 0;
    overflow: hidden;
    border: 1px solid var(--mi-border);
    background: var(--mi-panel);
}}

.fleet-panel-title {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    min-height: 33px;
    gap: 10px;
    margin: 0;
    padding: 0 11px;
    color: var(--mi-amber);
    border-bottom: 1px solid var(--mi-border);
    background: var(--mi-header-2);
    font-size: 10px;
    font-weight: 900;
    letter-spacing: .07em;
    text-transform: uppercase;
}}

.fleet-panel-title small {{
    color: var(--mi-text-muted);
    font-size: 8px;
    font-weight: 700;
}}

.fleet-kpi-grid {{
    grid-column: span 12;
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 8px;
}}

.fleet-kpi {{
    min-height: 104px;
    padding: 11px;
    border: 1px solid var(--mi-border);
    border-top: 3px solid var(--mi-cyan);
    background: var(--mi-panel);
}}

.fleet-kpi:nth-child(2) {{
    border-top-color: var(--mi-amber);
}}

.fleet-kpi:nth-child(3) {{
    border-top-color: var(--mi-red);
}}

.fleet-kpi:nth-child(4) {{
    border-top-color: var(--mi-blue);
}}

.fleet-kpi-label {{
    color: var(--mi-text-muted);
    font-size: 9px;
    font-weight: 800;
    letter-spacing: .07em;
    text-transform: uppercase;
}}

.fleet-kpi-value {{
    margin-top: 13px;
    color: var(--mi-white);
    font-size: clamp(20px, 2vw, 29px);
    font-weight: 900;
    font-variant-numeric: tabular-nums;
    letter-spacing: -.05em;
    text-align: right;
}}

.fleet-kpi-delta {{
    min-height: 16px;
    margin-top: 8px;
    font-size: 9px;
    font-weight: 800;
    text-align: right;
}}

.fleet-chart-panel {{
    grid-column: span 8;
}}

.fleet-watchlist-panel {{
    grid-column: span 4;
}}

.fleet-chart-wrap {{
    position: relative;
    height: 392px;
    padding: 12px;
}}

.fleet-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 10px;
}}

.fleet-table th {{
    padding: 9px 10px;
    color: var(--mi-text-muted);
    border-bottom: 1px solid var(--mi-border);
    background: var(--mi-bg-deep);
    font-size: 8px;
    letter-spacing: .05em;
    text-align: right;
    text-transform: uppercase;
}}

.fleet-table td {{
    padding: 11px 10px;
    color: var(--mi-text);
    border-bottom: 1px solid var(--mi-border-soft);
    font-variant-numeric: tabular-nums;
    text-align: right;
}}

.fleet-table th:first-child,
.fleet-table td:first-child {{
    text-align: left;
}}

.fleet-status-name {{
    color: var(--mi-white) !important;
    font-weight: 800;
}}

.fleet-note {{
    padding: 9px 10px;
    color: var(--mi-text-muted);
    background: var(--mi-header-2);
    font-size: 8px;
    font-weight: 700;
    letter-spacing: .04em;
    line-height: 1.6;
    text-transform: uppercase;
}}

.fleet-allocation-panel {{
    grid-column: span 4;
}}

.fleet-insight-panel {{
    grid-column: span 4;
}}

.fleet-risk-panel {{
    grid-column: span 4;
}}

.fleet-allocation-content,
.fleet-risk-content {{
    padding: 10px;
}}

.fleet-allocation-row {{
    padding: 8px 0;
    border-bottom: 1px solid var(--mi-border-soft);
}}

.fleet-allocation-row:last-child {{
    border-bottom: 0;
}}

.fleet-allocation-header,
.fleet-allocation-footer {{
    display: flex;
    justify-content: space-between;
    gap: 8px;
}}

.fleet-allocation-header {{
    color: var(--mi-text);
    font-size: 9px;
    font-weight: 800;
}}

.fleet-allocation-footer {{
    margin-top: 5px;
    color: var(--mi-text-muted);
    font-size: 8px;
}}

.fleet-progress-track {{
    height: 7px;
    margin-top: 6px;
    overflow: hidden;
    border: 1px solid var(--mi-border);
    background: var(--mi-bg-deep);
}}

.fleet-progress-track span {{
    display: block;
    height: 100%;
}}

.fleet-insight-list {{
    min-height: 278px;
    margin: 0;
    padding: 5px 15px 8px 30px;
}}

.fleet-insight-list li {{
    padding: 9px 0;
    color: var(--mi-text);
    border-bottom: 1px solid var(--mi-border-soft);
    font-size: 9px;
    font-weight: 700;
    line-height: 1.55;
}}

.fleet-insight-list li::marker {{
    color: var(--mi-amber);
}}

.fleet-risk-row {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 10px 0;
    border-bottom: 1px solid var(--mi-border-soft);
}}

.fleet-risk-row:last-child {{
    border-bottom: 0;
}}

.fleet-risk-label {{
    color: var(--mi-text-muted);
    font-size: 8px;
    font-weight: 800;
    letter-spacing: .05em;
    text-transform: uppercase;
}}

.fleet-risk-value {{
    margin-top: 3px;
    color: var(--mi-white);
    font-size: 10px;
    font-weight: 900;
    text-align: right;
}}

.fleet-status-footer {{
    grid-column: span 12;
    display: flex;
    justify-content: space-between;
    gap: 12px;
    padding: 9px 11px;
    color: var(--mi-text-muted);
    border: 1px solid var(--mi-border);
    border-left: 3px solid var(--mi-green);
    background: var(--mi-header-2);
    font-size: 8px;
    font-weight: 700;
    letter-spacing: .05em;
    text-transform: uppercase;
}}

.up {{
    color: var(--mi-green) !important;
}}

.down {{
    color: var(--mi-red) !important;
}}

.neutral {{
    color: var(--mi-text-muted) !important;
}}

@media (max-width: 1200px) {{
    .mi-terminal-header {{
        flex-wrap: wrap;
    }}

    .mi-terminal-meta {{
        width: 100%;
        min-width: 100%;
        border-top: 1px solid var(--mi-border);
        border-left: 0;
        text-align: left;
    }}

    .fleet-chart-panel,
    .fleet-watchlist-panel,
    .fleet-allocation-panel,
    .fleet-insight-panel,
    .fleet-risk-panel {{
        grid-column: span 12;
    }}
}}

@media (max-width: 760px) {{
    .fleet-kpi-grid {{
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }}

    .fleet-status-footer {{
        flex-direction: column;
    }}

    .fleet-watchlist-panel {{
        overflow-x: auto;
    }}

    .fleet-table {{
        min-width: 700px;
    }}
}}

@media (max-width: 480px) {{
    .fleet-kpi-grid {{
        grid-template-columns: 1fr;
    }}

    .mi-terminal-title h1 {{
        font-size: 11px;
    }}
}}
</style>
</head>

<body>
<div class="mi-page">

    <header class="mi-terminal-header">
        <div class="mi-terminal-brand">MI</div>

        <div class="mi-terminal-title">
            <h1>Linerlytica | Global Containership Fleet</h1>
            <p>
                Fleet Supply Intelligence · Monthly 17th Snapshot History ·
                Ships &amp; Capacity Monitoring
            </p>
        </div>

        <div class="mi-terminal-meta">
            <div>
                DATA SNAPSHOT:
                <strong>{html.escape(latest_date)}</strong>
            </div>
            <div>
                PREVIOUS:
                <strong>{html.escape(previous_date or "N/A")}</strong>
            </div>
            <div>
                SOURCE:
                <strong>LINERLYTICA</strong>
            </div>
        </div>
    </header>

    <section class="fleet-ticker-wrap">
        <div class="fleet-ticker-heading">Fleet Ticker</div>
        {ticker_items}
    </section>

    <main class="fleet-grid">

        <section class="fleet-kpi-grid">
            <article class="fleet-kpi">
                <div class="fleet-kpi-label">Active Fleet Capacity</div>
                <div class="fleet-kpi-value">
                    {format_number(active_latest.get("teu"))}
                </div>
                <div class="fleet-kpi-delta">
                    {format_delta(
                        active_latest.get("teu"),
                        active_previous.get("teu")
                    )}
                </div>
            </article>

            <article class="fleet-kpi">
                <div class="fleet-kpi-label">Ships at Port</div>
                <div class="fleet-kpi-value">
                    {format_number(port_latest.get("ships"))}
                </div>
                <div class="fleet-kpi-delta">
                    {format_delta(
                        port_latest.get("ships"),
                        port_previous.get("ships")
                    )}
                </div>
            </article>

            <article class="fleet-kpi">
                <div class="fleet-kpi-label">Inactive Fleet Capacity</div>
                <div class="fleet-kpi-value">
                    {format_number(inactive_latest.get("teu"))}
                </div>
                <div class="fleet-kpi-delta">
                    {format_delta(
                        inactive_latest.get("teu"),
                        inactive_previous.get("teu")
                    )}
                </div>
            </article>

            <article class="fleet-kpi">
                <div class="fleet-kpi-label">Historical Observations</div>
                <div class="fleet-kpi-value">
                    {len(completed_dates)} MOS
                </div>
                <div class="fleet-kpi-delta">
                    <span class="neutral">17TH OF EACH MONTH</span>
                </div>
            </article>
        </section>

        <section class="fleet-panel fleet-chart-panel">
            <h2 class="fleet-panel-title">
                <span>01. Fleet Capacity Trend | TEU</span>
                <small>
                    {html.escape(completed_dates[0])} — {html.escape(latest_date)}
                </small>
            </h2>

            <div class="fleet-chart-wrap">
                <canvas id="teuChart"></canvas>
            </div>
        </section>

        <section class="fleet-panel fleet-watchlist-panel">
            <h2 class="fleet-panel-title">
                <span>02. Fleet Status Watchlist</span>
                <small>Latest Snapshot</small>
            </h2>

            <table class="fleet-table">
                <thead>
                    <tr>
                        <th>Fleet Segment</th>
                        <th>Ships</th>
                        <th>Capacity</th>
                        <th>Ships Δ</th>
                        <th>TEU Δ</th>
                    </tr>
                </thead>
                <tbody>
                    {table_rows}
                </tbody>
            </table>

            <div class="fleet-note">
                DELTA VALUES COMPARE THE LATEST SNAPSHOT WITH THE IMMEDIATELY
                PRECEDING COMPLETE MONTHLY SNAPSHOT.
            </div>
        </section>

        <section class="fleet-panel fleet-allocation-panel">
            <h2 class="fleet-panel-title">
                <span>03. Capacity Allocation</span>
                <small>TEU Distribution</small>
            </h2>

            <div class="fleet-allocation-content">
                {allocation_rows}
            </div>
        </section>

        <section class="fleet-panel fleet-insight-panel">
            <h2 class="fleet-panel-title">
                <span>04. Market Intelligence</span>
                <small>Supply Narrative</small>
            </h2>

            <ul class="fleet-insight-list">
                <li>
                    ACTIVE FLEET CAPACITY STANDS AT
                    {format_number(active_teu)} TEU.
                </li>
                <li>
                    ACTIVE CAPACITY SIGNAL:
                    <strong>{active_signal}</strong>.
                </li>
                <li>
                    PORT-BASED CAPACITY STANDS AT
                    {format_number(port_teu)} TEU.
                </li>
                <li>
                    ANCHORAGE CAPACITY STANDS AT
                    {format_number(anchorage_teu)} TEU.
                </li>
                <li>
                    SHIPYARD CAPACITY STANDS AT
                    {format_number(shipyard_teu)} TEU.
                </li>
            </ul>
        </section>

        <section class="fleet-panel fleet-risk-panel">
            <h2 class="fleet-panel-title">
                <span>05. Risk Indicators</span>
                <small>Fleet Utilization Signals</small>
            </h2>

            <div class="fleet-risk-content">
                <div class="fleet-risk-row">
                    <div>
                        <div class="fleet-risk-label">Active Capacity</div>
                        <div class="fleet-risk-value">{format_number(active_teu)} TEU</div>
                    </div>
                </div>

                <div class="fleet-risk-row">
                    <div>
                        <div class="fleet-risk-label">Port Capacity</div>
                        <div class="fleet-risk-value">{format_number(port_teu)} TEU</div>
                    </div>
                </div>

                <div class="fleet-risk-row">
                    <div>
                        <div class="fleet-risk-label">Anchorage Capacity</div>
                        <div class="fleet-risk-value">{format_number(anchorage_teu)} TEU</div>
                    </div>
                </div>

                <div class="fleet-risk-row">
                    <div>
                        <div class="fleet-risk-label">Inactive Fleet Signal</div>
                        <div class="fleet-risk-value">
                            {format_number(inactive_teu)} TEU
                        </div>
                    </div>
                </div>

                <div class="fleet-risk-row">
                    <div>
                        <div class="fleet-risk-label">Risk Assessment</div>
                        <div class="fleet-risk-value {risk_class}">
                            {risk_assessment}
                        </div>
                    </div>
                </div>
            </div>

            <div class="fleet-note">
                INACTIVE CAPACITY MONTH-ON-MONTH CHANGE:
                {format_delta(
                    inactive_latest.get("teu"),
                    inactive_previous.get("teu")
                )}
            </div>
        </section>

        <footer class="fleet-status-footer">
            <div>
                SYSTEM STATUS:
                <strong>NORMAL</strong>
                | DATASET:
                <strong>COMPLETE MONTHLY SNAPSHOTS ONLY</strong>
            </div>

            <div>
                GENERATED:
                <strong>{html.escape(generated_at)}</strong>
                | SOURCE:
                <strong>LINERLYTICA.DB</strong>
            </div>
        </footer>

    </main>
</div>

<script>
const fleetLabels = {json.dumps(completed_dates, ensure_ascii=False)};

new Chart(document.getElementById("teuChart"), {{
    type: "line",
    data: {{
        labels: fleetLabels,
        datasets: [
            {{
                label: "ACTIVE SHIPS",
                data: {json.dumps(teu_series["Active Ships"])},
                borderColor: "#31d0a0",
                backgroundColor: "rgba(49, 208, 160, .08)",
                borderWidth: 2.4,
                pointRadius: 2,
                pointHoverRadius: 4,
                fill: true,
                tension: .20
            }},
            {{
                label: "SHIPS AT PORT",
                data: {json.dumps(teu_series["Ships at port"])},
                borderColor: "#f5a623",
                backgroundColor: "transparent",
                borderWidth: 1.8,
                pointRadius: 1.5,
                pointHoverRadius: 3,
                fill: false,
                tension: .20
            }},
            {{
                label: "SHIPS AT ANCHORAGE",
                data: {json.dumps(teu_series["Ships at anchorage"])},
                borderColor: "#4fa3ff",
                backgroundColor: "transparent",
                borderWidth: 1.7,
                pointRadius: 1.5,
                pointHoverRadius: 3,
                fill: false,
                tension: .20
            }},
            {{
                label: "INACTIVE SHIPS",
                data: {json.dumps(teu_series["Inactive Ships"])},
                borderColor: "#ff5b68",
                backgroundColor: "transparent",
                borderWidth: 1.7,
                pointRadius: 1.5,
                pointHoverRadius: 3,
                fill: false,
                tension: .20
            }},
            {{
                label: "SHIPS IN SHIPYARD",
                data: {json.dumps(teu_series["Ships in shipyard"])},
                borderColor: "#27c7e8",
                backgroundColor: "transparent",
                borderWidth: 1.7,
                pointRadius: 1.5,
                pointHoverRadius: 3,
                fill: false,
                tension: .20
            }}
        ]
    }},
    options: {{
        responsive: true,
        maintainAspectRatio: false,
        interaction: {{
            mode: "index",
            intersect: false
        }},
        plugins: {{
            legend: {{
                position: "bottom",
                labels: {{
                    color: "#b6c7d7",
                    boxWidth: 12,
                    boxHeight: 2,
                    padding: 14,
                    font: {{
                        family: "Consolas, monospace",
                        size: 9,
                        weight: "700"
                    }}
                }}
            }},
            tooltip: {{
                backgroundColor: "rgba(3, 11, 19, .97)",
                titleColor: "#f2f6fa",
                bodyColor: "#b6c7d7",
                borderColor: "#16334b",
                borderWidth: 1,
                padding: 10,
                callbacks: {{
                    label: function(context) {{
                        if (
                            context.raw === null ||
                            context.raw === undefined
                        ) {{
                            return context.dataset.label + ": N/A";
                        }}

                        return context.dataset.label + ": " +
                            Number(context.raw).toLocaleString() + " TEU";
                    }}
                }}
            }}
        }},
        scales: {{
            x: {{
                grid: {{
                    color: "rgba(22, 51, 75, .42)",
                    drawBorder: false
                }},
                ticks: {{
                    color: "#8298aa",
                    maxRotation: 0,
                    maxTicksLimit: 9,
                    font: {{
                        family: "Consolas, monospace",
                        size: 9
                    }}
                }}
            }},
            y: {{
                grid: {{
                    color: "rgba(22, 51, 75, .42)",
                    drawBorder: false
                }},
                ticks: {{
                    color: "#8298aa",
                    font: {{
                        family: "Consolas, monospace",
                        size: 9
                    }},
                    callback: function(value) {{
                        return (value / 1000000).toFixed(1) + "M";
                    }}
                }}
            }}
        }}
    }}
}});
</script>
</body>
</html>
"""

    HTML_PATH.write_text(dashboard_html, encoding="utf-8")

    print(
        f"[Dashboard] Generated: {HTML_PATH} | "
        f"latest={latest_date} | "
        f"history={len(completed_dates)} snapshots"
    )

# =============================================================================
# 8. Main
# =============================================================================

def main() -> None:
    """모듈 실행 진입점."""
    print("=" * 72)
    print("[Linerlytica Module Execution Started]")
    print(f"Working Directory: {BASE_DIR}")
    print(f"Database Path: {DB_PATH}")
    print(f"Dashboard Path: {HTML_PATH}")
    print(f"Target URL: {TARGET_URL}")
    print(f"Backfill Start Date: {START_DATE_STR}")
    print(f"Date Input Selector: {DATE_INPUT_SELECTOR}")
    print(f"Date Input Format: {DATE_INPUT_FORMAT}")
    print("=" * 72)

    conn: Optional[sqlite3.Connection] = None

    try:
        conn = init_db()
        crawl_and_backfill(conn)
        generate_dashboard_html(conn)

    except KeyboardInterrupt:
        print("\n[Interrupted] 사용자에 의해 실행이 중단되었습니다.")

    except Exception as exc:
        print(f"\n[FATAL] {type(exc).__name__}: {exc}")
        raise

    finally:
        if conn is not None:
            conn.close()

        print("=" * 72)
        print("[Linerlytica Module Execution Completed]")
        print("=" * 72)

if __name__ == "__main__":
    main()
