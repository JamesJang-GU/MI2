#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MI2 통합 크롤러 러너 + 대시보드 데이터 빌더
============================================
- 7개 지표 모듈(air/alphaliner/forwarders/linerlytica/macro/oil/sea)을
  헤드리스(정적 생성) 모드로 순차 실행한다.
- 실행 결과를 status.json 으로 기록한다.
- 각 모듈의 SQLite DB에서 포털 대시보드(index.html)용
  요약 데이터 summary.json 을 생성한다.

사용법
    python run_all.py                # 전체 모듈 크롤링 + 요약 생성
    python run_all.py sea oil        # 지정 모듈만 크롤링 + 요약 생성
    python run_all.py --summary-only # 크롤링 없이 요약만 재생성

GitHub Actions(매일 KST 00:00)에서는 이 파일 하나만 호출하면 된다.
"""

import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# -----------------------------------------------------------------------------
# 모듈 정의: id / 포털 제목 / 실행 명령 / 타임아웃(초) / 상세 페이지
# -----------------------------------------------------------------------------
MODULES = [
    {
        "id": "air",
        "title": "항공운임 · TAC/Baltic ARI",
        "cmd": ["air/air.py", "--cron"],
        "timeout": 600,
        "page": "air/index.html",
    },
    {
        "id": "alphaliner",
        "title": "선사 선대 · Alphaliner Top100",
        "cmd": ["alphaliner/alphaliner.py", "--cron"],
        "timeout": 900,
        "page": "alphaliner/index.html",
    },
    {
        "id": "forwarders",
        "title": "포워더 순위 · A&A Top25",
        "cmd": ["forwarders/forwarders.py", "--refresh"],
        "timeout": 600,
        "page": "forwarders/index.html",
    },
    {
        "id": "linerlytica",
        "title": "컨테이너 선대 · Linerlytica",
        "cmd": ["linerlytica/linerytica.py"],
        "timeout": 1800,
        "page": "linerlytica/index.html",
    },
    {
        "id": "macro",
        "title": "거시경제 · GDP/물가/GSCPI",
        "cmd": ["macro/macro.py", "--cron"],
        "timeout": 600,
        "page": "macro/index.html",
    },
    {
        "id": "oil",
        "title": "유가/벙커링 · VLSFO/원유",
        "cmd": ["oil/oil.py"],
        "timeout": 900,
        "page": "oil/index.html",
    },
    {
        "id": "sea",
        "title": "해운운임 · SCFI/BDI",
        "cmd": ["sea/sea.py", "--cron"],
        "timeout": 1800,
        "page": None,  # Flask 기반이라 정적 페이지 없음(포털 카드에서 확인)
    },
]


def log(message: str) -> None:
    print(f"[run_all] {message}", flush=True)


def r2(value):
    """대시보드 표시용 반올림."""
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


def pct_change(current, previous):
    if current is None or previous in (None, 0):
        return None
    return r2((float(current) - float(previous)) / abs(float(previous)) * 100)


# -----------------------------------------------------------------------------
# 모듈별 요약 추출기 (DB 스키마 차이 absorber)
# -----------------------------------------------------------------------------
def summarize_sea(conn):
    out = {"metrics": [], "series": [], "tables": []}
    # SCFI는 region='종합', BDI는 region='BDI'가 종합 지수
    for index_name, region, label in (
        ("SCFI", "종합", "SCFI 종합"),
        ("BDI", "BDI", "BDI"),
    ):
        rows = conn.execute(
            "SELECT dt, value FROM index_raw "
            "WHERE index_name = ? AND region = ? "
            "ORDER BY dt DESC LIMIT 160",
            (index_name, region),
        ).fetchall()
        if not rows:
            continue
        latest, previous = rows[0], (rows[1] if len(rows) > 1 else None)
        out["metrics"].append(
            {
                "label": label,
                "value": r2(latest["value"]),
                "change": pct_change(latest["value"], previous["value"] if previous else None),
                "change_unit": "%",
                "asof": latest["dt"],
            }
        )
        points = [[r["dt"], r2(r["value"])] for r in reversed(rows)]
        out["series"].append({"label": label, "points": points})
    baltic = conn.execute(
        "SELECT region, value, dt FROM index_raw "
        "WHERE index_name = 'BDI' AND region IN ('BDI','BCI','BPI','BSI') "
        "AND dt = (SELECT MAX(dt) FROM index_raw WHERE index_name = 'BDI') "
        "ORDER BY region"
    ).fetchall()
    if baltic:
        out["tables"].append(
            {
                "label": f"Baltic 지수 ({baltic[0]['dt']})",
                "columns": ["지수", "값"],
                "rows": [[r["region"], r2(r["value"])] for r in baltic],
            }
        )
    out["updated"] = conn.execute("SELECT MAX(dt) FROM index_raw").fetchone()[0]
    return out


def summarize_air(conn):
    out = {"metrics": [], "series": [], "tables": []}
    master = conn.execute(
        "SELECT data_date, overall_index, weekly_change FROM tac_master "
        "ORDER BY data_date DESC LIMIT 160"
    ).fetchall()
    if master:
        out["metrics"].append(
            {
                "label": "TAC 종합지수",
                "value": r2(master[0]["overall_index"]),
                "change": r2(master[0]["weekly_change"]),
                "change_unit": "%",
                "asof": master[0]["data_date"],
            }
        )
        out["series"].append(
            {
                "label": "TAC 종합지수",
                "points": [
                    [r["data_date"], r2(r["overall_index"])] for r in reversed(master)
                ],
            }
        )
        out["updated"] = master[0]["data_date"]
    detail = conn.execute(
        "SELECT d.route_code, d.index_value, d.weekly_change "
        "FROM tac_detail d JOIN tac_master m ON m.snapshot_id = d.snapshot_id "
        "WHERE m.data_date = (SELECT MAX(data_date) FROM tac_master) "
        "ORDER BY d.route_code"
    ).fetchall()
    if detail:
        out["tables"].append(
            {
                "label": "항로별 지수",
                "columns": ["항로", "지수", "주간 %"],
                "rows": [
                    [r["route_code"], r2(r["index_value"]), r2(r["weekly_change"])]
                    for r in detail
                ],
            }
        )
    return out


def summarize_alphaliner(conn):
    out = {"metrics": [], "series": [], "tables": []}
    master = conn.execute(
        "SELECT data_date, total_teu, total_ships, operator_count "
        "FROM alphaliner_master ORDER BY data_date DESC LIMIT 160"
    ).fetchall()
    if master:
        latest = master[0]
        out["metrics"].extend(
            [
                {"label": "Top100 선대(TEU)", "value": latest["total_teu"], "asof": latest["data_date"]},
                {"label": "Top100 척수", "value": latest["total_ships"], "asof": latest["data_date"]},
                {"label": "선사 수", "value": latest["operator_count"], "asof": latest["data_date"]},
            ]
        )
        out["series"].append(
            {
                "label": "Top100 선대(TEU)",
                "points": [[r["data_date"], r["total_teu"]] for r in reversed(master)],
            }
        )
        out["updated"] = latest["data_date"]
    top = conn.execute(
        "SELECT d.rank, d.operator_name, d.total_teu, d.market_share "
        "FROM alphaliner_detail d JOIN alphaliner_master m "
        "ON m.snapshot_id = d.snapshot_id "
        "WHERE m.data_date = (SELECT MAX(data_date) FROM alphaliner_master) "
        "ORDER BY d.rank LIMIT 10"
    ).fetchall()
    if top:
        out["tables"].append(
            {
                "label": "Top 10 선사",
                "columns": ["#", "선사", "TEU", "점유율 %"],
                "rows": [
                    [r["rank"], r["operator_name"], r["total_teu"], r2(r["market_share"])]
                    for r in top
                ],
            }
        )
    return out


def summarize_forwarders(conn):
    out = {"metrics": [], "series": [], "tables": []}
    latest_date = conn.execute("SELECT MAX(date) FROM forwarders").fetchone()[0]
    if not latest_date:
        return out
    rows = conn.execute(
        "SELECT rank, provider, revenue, teu FROM forwarders "
        "WHERE date = ? ORDER BY rank LIMIT 10",
        (latest_date,),
    ).fetchall()
    if rows:
        out["metrics"].append(
            {
                "label": f"1위 TEU ({rows[0]['provider']})",
                "value": rows[0]["teu"],
                "asof": latest_date,
            }
        )
        out["tables"].append(
            {
                "label": "Top 10 포워더",
                "columns": ["#", "포워더", "매출($M)", "TEU"],
                "rows": [
                    [r["rank"], r["provider"], r2(r["revenue"]), r["teu"]] for r in rows
                ],
            }
        )
    out["updated"] = latest_date
    return out


def summarize_linerlytica(conn):
    out = {"metrics": [], "series": [], "tables": []}
    latest_date = conn.execute(
        "SELECT MAX(snapshot_date) FROM fleet_snapshot"
    ).fetchone()[0]
    if not latest_date:
        return out
    rows = conn.execute(
        "SELECT ship_status, ships, teu FROM fleet_snapshot "
        "WHERE snapshot_date = ? ORDER BY teu DESC",
        (latest_date,),
    ).fetchall()
    status_map = {r["ship_status"]: r for r in rows}
    active = status_map.get("Active Ships")
    if active:
        out["metrics"].extend(
            [
                {"label": "운항 선박 수", "value": active["ships"], "asof": latest_date},
                {"label": "운항 선대(TEU)", "value": active["teu"], "asof": latest_date},
            ]
        )
    out["tables"].append(
        {
            "label": "선박 상태별 현황",
            "columns": ["상태", "척수", "TEU"],
            "rows": [[r["ship_status"], r["ships"], r["teu"]] for r in rows],
        }
    )
    out["updated"] = latest_date
    return out


def summarize_macro(conn):
    out = {"metrics": [], "series": [], "tables": []}
    gscpi = conn.execute(
        "SELECT date, value FROM gscpi_series ORDER BY date DESC LIMIT 160"
    ).fetchall()
    if gscpi:
        latest, previous = gscpi[0], (gscpi[1] if len(gscpi) > 1 else None)
        out["metrics"].append(
            {
                "label": "GSCPI (글로벌 공급망 압력)",
                "value": r2(latest["value"]),
                "change": r2(
                    (latest["value"] - previous["value"]) if previous else None
                ),
                "change_unit": "p",
                "asof": latest["date"],
            }
        )
        out["series"].append(
            {"label": "GSCPI", "points": [[r["date"], r2(r["value"])] for r in reversed(gscpi)]}
        )
        out["updated"] = latest["date"]
    for indicator, label in (
        ("GDP_GROWTH", "GDP 성장률(월드)"),
        ("CPI_INFLATION", "물가상승률(월드)"),
    ):
        row = conn.execute(
            "SELECT year, value FROM macro_series "
            "WHERE indicator = ? AND country_code = 'WLD' "
            "ORDER BY year DESC LIMIT 1",
            (indicator,),
        ).fetchone()
        if row:
            out["metrics"].append(
                {
                    "label": f"{label} {row['year']}",
                    "value": r2(row["value"]),
                    "change_unit": "%",
                    "asof": str(row["year"]),
                }
            )
    return out


def summarize_oil(conn):
    out = {"metrics": [], "series": [], "tables": []}
    prices = conn.execute(
        "SELECT DATE, Dubai, Brent, WTI FROM oil_prices "
        "ORDER BY DATE DESC LIMIT 160"
    ).fetchall()
    if prices:
        latest, previous = prices[0], (prices[1] if len(prices) > 1 else None)
        for column in ("Dubai", "Brent", "WTI"):
            out["metrics"].append(
                {
                    "label": f"{column} 유가($)",
                    "value": r2(latest[column]),
                    "change": pct_change(
                        latest[column], previous[column] if previous else None
                    ),
                    "change_unit": "%",
                    "asof": latest["DATE"],
                }
            )
        out["series"].append(
            {
                "label": "Dubai 유가($)",
                "points": [[r["DATE"], r2(r["Dubai"])] for r in reversed(prices)],
            }
        )
        out["updated"] = latest["DATE"]
    vlsfo = conn.execute(
        "SELECT port, price, date_str FROM product_vlsfo p1 "
        "WHERE date_str = (SELECT MAX(date_str) FROM product_vlsfo p2 "
        "                 WHERE p2.port = p1.port) "
        "ORDER BY port"
    ).fetchall()
    if vlsfo:
        out["tables"].append(
            {
                "label": "항만별 VLSFO($/ton)",
                "columns": ["항만", "가격", "기준일"],
                "rows": [[r["port"], r2(r["price"]), r["date_str"]] for r in vlsfo],
            }
        )
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
            "tables": [],
            "updated": None,
            "error": None,
        }
        conn = connect(module["id"])
        if conn is None:
            entry["error"] = "DB 파일 없음"
            modules.append(entry)
            continue
        try:
            data = SUMMARIZERS[module["id"]](conn)
            entry.update(data)
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
        write_json("status.json", {"generated_at": datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"), "runs": runs})
        failed = [r["module"] for r in runs if not r["ok"]]
        if failed:
            log(f"크롤링 실패 모듈: {', '.join(failed)} (나머지 정상)")
    else:
        log("크롤링 생략(--summary-only), 요약만 재생성합니다.")

    write_json("summary.json", build_summary())
    return 0  # CI가 커밋 단계까지 진행하도록 항상 0


if __name__ == "__main__":
    sys.exit(main())
