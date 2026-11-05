#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MI2 통합 크롤러 러너 + 대시보드 데이터 빌더
============================================
- 7개 지표 모듈(air/alphaliner/forwarders/linerlytica/macro/oil/sea)을
  헤드리스(정적 생성) 모드로 순차 실행한다.
- 실행 결과를 status.json 으로 기록한다.
- 각 모듈의 SQLite DB에서 포털 대시보드(index.html, Bloomberg 터미널 스타일)용
  요약 데이터 summary.json 을 생성한다.
  - metric: 최신 크롤링 값 + 직전 스냅샷 대비 증감(change/pct)
  - series: 최근 1년 그래프용 포인트

사용법
    python run_all.py                # 전체 모듈 크롤링 + 요약 생성
    python run_all.py sea oil        # 지정 모듈만 크롤링 + 요약 생성
    python run_all.py --summary-only # 크롤링 없이 요약만 재생성
"""

import json
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# -----------------------------------------------------------------------------
# 모듈 정의: id / 포털 제목 / 실행 명령 / 타임아웃(초) / 상세 페이지
# -----------------------------------------------------------------------------
MODULES = [
    {
        "id": "sea",
        "title": "SCFI / BDI",
        "cmd": ["sea/sea.py", "--cron"],
        "timeout": 1800,
        "page": None,  # Flask 기반이라 정적 상세 페이지 없음
    },
    {
        "id": "linerlytica",
        "title": "컨테이너 선대",
        "cmd": ["linerlytica/linerytica.py"],
        "timeout": 1800,
        "page": "linerlytica/index.html",
    },
    {
        "id": "alphaliner",
        "title": "Top100 선사",
        "cmd": ["alphaliner/alphaliner.py", "--cron"],
        "timeout": 900,
        "page": "alphaliner/index.html",
    },
    {
        "id": "forwarders",
        "title": "포워더 순위",
        "cmd": ["forwarders/forwarders.py", "--refresh"],
        "timeout": 600,
        "page": "forwarders/index.html",
    },
    {
        "id": "air",
        "title": "TAC Index (항공)",
        "cmd": ["air/air.py", "--cron"],
        "timeout": 600,
        "page": "air/index.html",
    },
    {
        "id": "oil",
        "title": "유가 / 벙커링",
        "cmd": ["oil/oil.py"],
        "timeout": 900,
        "page": "oil/index.html",
    },
    {
        "id": "macro",
        "title": "거시경제",
        "cmd": ["macro/macro.py", "--cron"],
        "timeout": 600,
        "page": "macro/index.html",
    },
]


def log(message: str) -> None:
    print(f"[run_all] {message}", flush=True)


def r2(value):
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def connect(module_id: str):
    db_path = ROOT / module_id / f"{module_id}.db"
    if not db_path.exists():
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def metric(label, value, prev, asof, unit=""):
    """최신값 + 직전 스냅샷 대비 증감(숫자, %)."""
    change = None
    pct = None
    if value is not None and prev is not None:
        change = r2(float(value) - float(prev))
        if float(prev) != 0:
            pct = round((float(value) - float(prev)) / abs(float(prev)) * 100, 1)
    return {
        "label": label,
        "value": r2(value) if value is not None else None,
        "prev": r2(prev) if prev is not None else None,
        "change": change,
        "pct": pct,
        "unit": unit,
        "asof": asof,
    }


def year_filter(points):
    """그래프용 최근 1년 필터 (월 단위 키도 지원)."""
    cutoff = (date.today() - timedelta(days=365)).replace(day=1).isoformat()
    out = []
    for key, value in points:
        norm = key if len(str(key)) >= 10 else f"{key}-01"
        if norm >= cutoff:
            out.append([key, value])
    return out


# -----------------------------------------------------------------------------
# 모듈별 요약 추출기
# -----------------------------------------------------------------------------
def summarize_sea(conn):
    out = {"metrics": [], "series": []}
    # SCFI는 region='종합', BDI는 region='BDI'가 종합 지수
    for index_name, region, label in (
        ("SCFI", "종합", "SCFI 종합"),
        ("BDI", "BDI", "BDI"),
    ):
        rows = conn.execute(
            "SELECT dt, value FROM index_raw "
            "WHERE index_name = ? AND region = ? "
            "ORDER BY dt DESC LIMIT 400",
            (index_name, region),
        ).fetchall()
        if not rows:
            continue
        latest, prev = rows[0], (rows[1] if len(rows) > 1 else None)
        out["metrics"].append(
            metric(label, latest["value"], prev["value"] if prev else None, latest["dt"])
        )
        out["series"].append(
            {
                "label": label,
                "points": year_filter(
                    [[r["dt"], r2(r["value"])] for r in reversed(rows)]
                ),
            }
        )
    out["updated"] = conn.execute("SELECT MAX(dt) FROM index_raw").fetchone()[0]
    return out


def summarize_air(conn):
    out = {"metrics": [], "series": []}
    master = conn.execute(
        "SELECT data_date, overall_index FROM tac_master "
        "ORDER BY data_date DESC LIMIT 400"
    ).fetchall()
    if master:
        latest, prev = master[0], (master[1] if len(master) > 1 else None)
        out["metrics"].append(
            metric(
                "TAC 종합지수",
                latest["overall_index"],
                prev["overall_index"] if prev else None,
                latest["data_date"],
            )
        )
        out["series"].append(
            {
                "label": "TAC 종합지수",
                "points": year_filter(
                    [[r["data_date"], r2(r["overall_index"])] for r in reversed(master)]
                ),
            }
        )
        out["updated"] = latest["data_date"]
    return out


def summarize_alphaliner(conn):
    out = {"metrics": [], "series": []}
    master = conn.execute(
        "SELECT snapshot_id, data_date, total_teu FROM alphaliner_master "
        "ORDER BY data_date DESC LIMIT 400"
    ).fetchall()
    if master:
        latest, prev = master[0], (master[1] if len(master) > 1 else None)
        top1 = conn.execute(
            "SELECT operator_name, total_teu FROM alphaliner_detail "
            "WHERE snapshot_id = ? ORDER BY rank LIMIT 1",
            (latest["snapshot_id"],),
        ).fetchone()
        prev_top1 = None
        if prev:
            row = conn.execute(
                "SELECT total_teu FROM alphaliner_detail "
                "WHERE snapshot_id = ? ORDER BY rank LIMIT 1",
                (prev["snapshot_id"],),
            ).fetchone()
            prev_top1 = row["total_teu"] if row else None
        if top1:
            out["metrics"].append(
                metric(
                    f"1위 {top1['operator_name']} (TEU)",
                    top1["total_teu"],
                    prev_top1,
                    latest["data_date"],
                )
            )
        out["metrics"].append(
            metric(
                "Top100 합계 (TEU)",
                latest["total_teu"],
                prev["total_teu"] if prev else None,
                latest["data_date"],
            )
        )
        out["series"].append(
            {
                "label": "Top100 합계 (TEU)",
                "points": year_filter(
                    [[r["data_date"], r["total_teu"]] for r in reversed(master)]
                ),
            }
        )
        out["updated"] = latest["data_date"]
    return out


def summarize_forwarders(conn):
    out = {"metrics": [], "series": []}
    dates = conn.execute(
        "SELECT DISTINCT date FROM forwarders ORDER BY date DESC LIMIT 2"
    ).fetchall()
    if not dates:
        return out
    latest_date = dates[0]["date"]
    prev_date = dates[1]["date"] if len(dates) > 1 else None

    # 원본 rank 컬럼 기준 1위 (동률이면 매출 상위)
    top = conn.execute(
        "SELECT provider, revenue, teu FROM forwarders "
        "WHERE date = ? AND rank = (SELECT MIN(rank) FROM forwarders WHERE date = ?) "
        "ORDER BY revenue DESC LIMIT 1",
        (latest_date, latest_date),
    ).fetchone()
    if top:
        prev_rev = prev_teu = None
        if prev_date:
            row = conn.execute(
                "SELECT revenue FROM forwarders "
                "WHERE date = ? AND rank = (SELECT MIN(rank) FROM forwarders WHERE date = ?) "
                "ORDER BY revenue DESC LIMIT 1",
                (prev_date, prev_date),
            ).fetchone()
            prev_rev = row["revenue"] if row else None
            row2 = conn.execute(
                "SELECT teu FROM forwarders WHERE date = ? AND provider = ?",
                (prev_date, top["provider"]),
            ).fetchone()
            prev_teu = row2["teu"] if row2 else None
        out["metrics"].append(
            metric(f"1위 {top['provider']} 매출 ($M)", top["revenue"], prev_rev, latest_date)
        )
        out["metrics"].append(
            metric(f"{top['provider']} TEU", top["teu"], prev_teu, latest_date)
        )
    # 1위 매출 추이 (일자별 rank=1 중 최대 매출)
    series_rows = conn.execute(
        "SELECT date, MAX(revenue) AS revenue FROM forwarders "
        "WHERE rank = (SELECT MIN(rank) FROM forwarders f2 WHERE f2.date = forwarders.date) "
        "GROUP BY date ORDER BY date"
    ).fetchall()
    if series_rows:
        out["series"].append(
            {
                "label": "1위 매출 ($M) 추이",
                "points": year_filter([[r["date"], r2(r["revenue"])] for r in series_rows]),
            }
        )
    out["updated"] = latest_date
    return out


def summarize_linerlytica(conn):
    out = {"metrics": [], "series": []}
    snaps = conn.execute(
        "SELECT DISTINCT snapshot_date FROM fleet_snapshot "
        "ORDER BY snapshot_date DESC LIMIT 2"
    ).fetchall()
    if not snaps:
        return out
    latest_date = snaps[0]["snapshot_date"]
    prev_date = snaps[1]["snapshot_date"] if len(snaps) > 1 else None

    def active(d):
        return conn.execute(
            "SELECT ships, teu FROM fleet_snapshot "
            "WHERE snapshot_date = ? AND ship_status = 'Active Ships'",
            (d,),
        ).fetchone()

    cur = active(latest_date)
    prv = active(prev_date) if prev_date else None
    if cur:
        out["metrics"].append(
            metric(
                "운항 선대 (TEU)",
                cur["teu"],
                prv["teu"] if prv else None,
                latest_date,
            )
        )
        out["metrics"].append(
            metric("운항 선박 수", cur["ships"], prv["ships"] if prv else None, latest_date)
        )
    series_rows = conn.execute(
        "SELECT snapshot_date, teu FROM fleet_snapshot "
        "WHERE ship_status = 'Active Ships' ORDER BY snapshot_date"
    ).fetchall()
    if series_rows:
        out["series"].append(
            {
                "label": "운항 선대 (TEU)",
                "points": year_filter(
                    [[r["snapshot_date"], r["teu"]] for r in series_rows]
                ),
            }
        )
    out["updated"] = latest_date
    return out


def summarize_macro(conn):
    out = {"metrics": [], "series": []}
    gscpi = conn.execute(
        "SELECT date, value FROM gscpi_series ORDER BY date DESC LIMIT 400"
    ).fetchall()
    if gscpi:
        latest, prev = gscpi[0], (gscpi[1] if len(gscpi) > 1 else None)
        out["metrics"].append(
            metric(
                "GSCPI (공급망 압력)",
                latest["value"],
                prev["value"] if prev else None,
                latest["date"],
            )
        )
        out["series"].append(
            {
                "label": "GSCPI",
                "points": year_filter(
                    [[r["date"], r2(r["value"])] for r in reversed(gscpi)]
                ),
            }
        )
        out["updated"] = latest["date"]
    for indicator, label in (
        ("GDP_GROWTH", "GDP 성장률 (월드)"),
        ("CPI_INFLATION", "물가상승률 (월드)"),
    ):
        row = conn.execute(
            "SELECT year, value FROM macro_series "
            "WHERE indicator = ? AND country_code = 'WLD' "
            "ORDER BY year DESC LIMIT 1",
            (indicator,),
        ).fetchone()
        if row:
            out["metrics"].append(
                metric(f"{label} {row['year']} (%)", row["value"], None, str(row["year"]))
            )
    return out


def summarize_oil(conn):
    out = {"metrics": [], "series": []}
    prices = conn.execute(
        "SELECT DATE, Dubai, Brent, WTI FROM oil_prices "
        "ORDER BY DATE DESC LIMIT 400"
    ).fetchall()
    if prices:
        latest, prev = prices[0], (prices[1] if len(prices) > 1 else None)
        for column in ("Dubai", "Brent", "WTI"):
            out["metrics"].append(
                metric(
                    f"{column} ($)",
                    latest[column],
                    prev[column] if prev else None,
                    latest["DATE"],
                )
            )
        out["series"].append(
            {
                "label": "Dubai ($)",
                "points": year_filter(
                    [[r["DATE"], r2(r["Dubai"])] for r in reversed(prices)]
                ),
            }
        )
        out["updated"] = latest["DATE"]
    return out


SUMMARIZERS = {
    "sea": summarize_sea,
    "air": summarize_air,
    "alphaliner": summarize_alphaliner,
    "forwarders": summarize_forwarders,
    "linerlytica": summarize_linerlytica,
    "macro": summarize_macro,
    "oil": summarize_oil,
}


def build_summary() -> dict:
    modules = []
    for module in MODULES:
        entry = {
            "id": module["id"],
            "title": module["title"],
            "page": module["page"],
            "metrics": [],
            "series": [],
            "updated": None,
            "error": None,
        }
        conn = connect(module["id"])
        if conn is None:
            entry["error"] = "DB 파일 없음"
            modules.append(entry)
            continue
        try:
            entry.update(SUMMARIZERS[module["id"]](conn))
        except Exception as exc:  # noqa: BLE001
            entry["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            conn.close()
        modules.append(entry)
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "modules": modules,
    }


def write_json(name: str, payload) -> None:
    path = ROOT / name
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    temp.replace(path)
    log(f"{name} 작성 완료 ({path.stat().st_size:,} bytes)")


def run_module(module: dict) -> dict:
    command = [sys.executable, str(ROOT / module["cmd"][0]), *module["cmd"][1:]]
    log(f"▶ {module['id']} 실행: {' '.join(module['cmd'])}")
    started = time.time()
    try:
        proc = subprocess.run(
            command,
            cwd=str(ROOT),
            timeout=module["timeout"],
            check=False,
        )
        ok = proc.returncode == 0
        error = None if ok else f"exit code {proc.returncode}"
    except subprocess.TimeoutExpired:
        ok, error = False, f"timeout ({module['timeout']}s)"
    except Exception as exc:  # noqa: BLE001
        ok, error = False, f"{type(exc).__name__}: {exc}"
    elapsed = round(time.time() - started, 1)
    log(f"{'✅' if ok else '❌'} {module['id']} ({elapsed}s) {error or ''}")
    return {
        "module": module["id"],
        "ok": ok,
        "seconds": elapsed,
        "error": error,
        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def main() -> int:
    args = sys.argv[1:]
    summary_only = "--summary-only" in args
    targets = [a for a in args if not a.startswith("-")]

    if not summary_only:
        selected = [m for m in MODULES if not targets or m["id"] in targets]
        runs = [run_module(module) for module in selected]
        write_json(
            "status.json",
            {
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "runs": runs,
            },
        )
        failed = [r["module"] for r in runs if not r["ok"]]
        if failed:
            log(f"크롤링 실패 모듈: {', '.join(failed)} (나머지 정상)")
    else:
        log("크롤링 생략(--summary-only), 요약만 재생성합니다.")

    write_json("summary.json", build_summary())
    return 0  # CI가 커밋 단계까지 진행하도록 항상 0


if __name__ == "__main__":
    sys.exit(main())
