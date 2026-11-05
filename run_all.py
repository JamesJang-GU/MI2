#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MI2 통합 크롤러 러너 + 대시보드 데이터 빌더
============================================
- 7개 지표 모듈을 헤드리스 모드로 순차 실행 → status.json
- 각 모듈 SQLite DB에서 포털(index.html, Bloomberg 터미널 스타일)용
  summary.json 생성
  - metric: 최신값 + 단위(unit) + 직전 스냅샷 대비 증감(change/pct)
  - series: 최근 1년 그래프용 포인트
  - top3/shares/groups: 박스별 특수 레이아웃용 데이터

사용법
    python run_all.py                # 전체 크롤링 + 요약
    python run_all.py sea oil        # 일부 모듈만
    python run_all.py --summary-only # 요약만 재생성
"""

import json
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent

MODULES = [
    {
        "id": "sea",
        "title": "SCFI / BDI",
        "cmd": ["sea/sea.py", "--cron"],
        "timeout": 1800,
        "page": None,  # 정적 상세 페이지 없음(로컬 Flask 전용)
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
        # --refresh: DB 캐시 대신 Selenium 실 크롤링(VLSFO+Opinet) 매일 수행
        "cmd": ["oil/oil.py", "--refresh"],
        "timeout": 1200,
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
    change = pct = None
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
    """최근 1년 필터 (월/연 단위 키도 지원)."""
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
            {"label": label,
             "points": year_filter([[r["dt"], r2(r["value"])] for r in reversed(rows)])}
        )
    out["updated"] = conn.execute("SELECT MAX(dt) FROM index_raw").fetchone()[0]
    return out


def summarize_linerlytica(conn):
    """선박수 + TEU 를 상단에, 각각 그래프."""
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
            metric("운항 선박 수", cur["ships"], prv["ships"] if prv else None,
                   latest_date, unit="척")
        )
        out["metrics"].append(
            metric("운항 선대", cur["teu"], prv["teu"] if prv else None,
                   latest_date, unit="TEU")
        )
    for column, label, unit in (("ships", "운항 선박 수", "척"),
                                ("teu", "운항 선대", "TEU")):
        rows = conn.execute(
            f"SELECT snapshot_date, {column} AS v FROM fleet_snapshot "
            "WHERE ship_status = 'Active Ships' ORDER BY snapshot_date"
        ).fetchall()
        if rows:
            out["series"].append(
                {"label": label,
                 "points": year_filter([[r["snapshot_date"], r["v"]] for r in rows])}
            )
    out["updated"] = latest_date
    return out


def summarize_alphaliner(conn):
    """Top3 리스트 + Top10 점유율 도넛."""
    out = {"metrics": [], "series": [], "top3": [], "shares": []}
    master = conn.execute(
        "SELECT snapshot_id, data_date, total_teu FROM alphaliner_master "
        "ORDER BY data_date DESC LIMIT 2"
    ).fetchall()
    if not master:
        return out
    latest, prev = master[0], (master[1] if len(master) > 1 else None)
    out["metrics"].append(
        metric("Top100 합계", latest["total_teu"],
               prev["total_teu"] if prev else None, latest["data_date"], unit="TEU")
    )
    top = conn.execute(
        "SELECT rank, operator_name, total_teu, market_share FROM alphaliner_detail "
        "WHERE snapshot_id = ? ORDER BY rank LIMIT 10",
        (latest["snapshot_id"],),
    ).fetchall()
    for i, row in enumerate(top[:3]):
        out["top3"].append(
            {"rank": i + 1, "name": row["operator_name"],
             "value": row["total_teu"], "unit": "TEU"}
        )
    covered = 0.0
    for row in top:
        share = r2(row["market_share"])
        if share is None and latest["total_teu"]:
            share = round(row["total_teu"] / latest["total_teu"] * 100, 1)
        if share is None:
            continue
        covered += share
        out["shares"].append({"name": row["operator_name"], "pct": share})
    if out["shares"] and covered < 100:
        out["shares"].append({"name": "기타", "pct": round(100 - covered, 1)})
    out["updated"] = latest["data_date"]
    return out


def summarize_forwarders(conn):
    """Top3 포워더 세로 리스트 (그래프 없음)."""
    out = {"metrics": [], "series": [], "top3": []}
    dates = conn.execute(
        "SELECT DISTINCT date FROM forwarders ORDER BY date DESC LIMIT 2"
    ).fetchall()
    if not dates:
        return out
    latest_date = dates[0]["date"]
    prev_date = dates[1]["date"] if len(dates) > 1 else None

    rows = conn.execute(
        "SELECT provider, revenue, teu FROM forwarders "
        "WHERE date = ? ORDER BY rank ASC, revenue DESC",
        (latest_date,),
    ).fetchall()
    seen = []
    seen_names = set()
    for row in rows:
        if row["provider"] in seen_names:
            continue
        seen_names.add(row["provider"])
        seen.append(row)
        if len(seen) == 3:
            break
    for i, row in enumerate(seen):
        prev_rev = None
        if prev_date:
            p = conn.execute(
                "SELECT revenue FROM forwarders WHERE date = ? AND provider = ?",
                (prev_date, row["provider"]),
            ).fetchone()
            prev_rev = p["revenue"] if p else None
        m = metric(row["provider"], row["revenue"], prev_rev, latest_date, unit="$M")
        out["top3"].append(
            {"rank": i + 1, "name": row["provider"], "value": row["revenue"],
             "unit": "$M", "change": m["change"], "pct": m["pct"],
             "teu": row["teu"]}
        )
    out["updated"] = latest_date
    return out


def summarize_air(conn):
    """항로 3개 지수 상단 + 3개 라인 그래프."""
    out = {"metrics": [], "series": []}
    snaps = conn.execute(
        "SELECT snapshot_id, data_date, overall_index FROM tac_master "
        "ORDER BY data_date DESC LIMIT 2"
    ).fetchall()
    if not snaps:
        return out
    latest, prev = snaps[0], (snaps[1] if len(snaps) > 1 else None)

    def detail(sid):
        return {
            r["route_code"]: r
            for r in conn.execute(
                "SELECT route_code, index_value FROM tac_detail WHERE snapshot_id = ?",
                (sid,),
            ).fetchall()
        }

    cur_map = detail(latest["snapshot_id"])
    prev_map = detail(prev["snapshot_id"]) if prev else {}
    for route in ("Frankfurt-North America", "Hong Kong-Europe",
                  "Hong Kong-North America"):
        cur = cur_map.get(route)
        if not cur:
            continue
        prv = prev_map.get(route)
        out["metrics"].append(
            metric(route, cur["index_value"],
                   prv["index_value"] if prv else None, latest["data_date"])
        )
    out["metrics"].append(
        metric("TAC 종합", latest["overall_index"],
               prev["overall_index"] if prev else None, latest["data_date"])
    )
    for route in ("Frankfurt-North America", "Hong Kong-Europe",
                  "Hong Kong-North America"):
        rows = conn.execute(
            "SELECT m.data_date, d.index_value FROM tac_detail d "
            "JOIN tac_master m ON m.snapshot_id = d.snapshot_id "
            "WHERE d.route_code = ? ORDER BY m.data_date",
            (route,),
        ).fetchall()
        if rows:
            out["series"].append(
                {"label": route,
                 "points": year_filter([[r["data_date"], r2(r["index_value"])]
                                        for r in rows])}
            )
    out["updated"] = latest["data_date"]
    return out


def summarize_oil(conn):
    """좌: Crude 3숫자+3그래프 / 우: VLSFO 3숫자+3그래프."""
    out = {"groups": []}
    prices = conn.execute(
        "SELECT DATE, Dubai, Brent, WTI FROM oil_prices "
        "ORDER BY DATE DESC LIMIT 400"
    ).fetchall()
    crude = {"title": "CRUDE OIL", "metrics": [], "series": []}
    if prices:
        latest, prev = prices[0], (prices[1] if len(prices) > 1 else None)
        for column in ("Dubai", "Brent", "WTI"):
            crude["metrics"].append(
                metric(column, latest[column], prev[column] if prev else None,
                       latest["DATE"], unit="$")
            )
            crude["series"].append(
                {"label": column,
                 "points": year_filter([[r["DATE"], r2(r[column])]
                                        for r in reversed(prices)])}
            )
        out["updated"] = latest["DATE"]
    vlsfo = {"title": "VLSFO", "metrics": [], "series": []}
    for port in ("Rotterdam", "Singapore", "Fujairah"):
        rows = conn.execute(
            "SELECT date_str, price FROM product_vlsfo "
            "WHERE port = ? ORDER BY date_str DESC LIMIT 400",
            (port,),
        ).fetchall()
        if not rows:
            continue
        latest, prev = rows[0], (rows[1] if len(rows) > 1 else None)
        vlsfo["metrics"].append(
            metric(port, latest["price"], prev["price"] if prev else None,
                   latest["date_str"], unit="$/ton")
        )
        vlsfo["series"].append(
            {"label": port,
             "points": year_filter([[r["date_str"], r2(r["price"])]
                                    for r in reversed(rows)])}
        )
        out.setdefault("updated", latest["date_str"])
    out["groups"] = [crude, vlsfo]
    return out


def summarize_macro(conn):
    """지표별 숫자 상단 + 그래프 하단 (3단 스택)."""
    out = {"groups": []}
    gscpi = conn.execute(
        "SELECT date, value FROM gscpi_series ORDER BY date DESC LIMIT 400"
    ).fetchall()
    if gscpi:
        latest, prev = gscpi[0], (gscpi[1] if len(gscpi) > 1 else None)
        out["groups"].append(
            {
                "title": "GSCPI (공급망 압력)",
                "metrics": [metric("GSCPI", latest["value"],
                                   prev["value"] if prev else None, latest["date"])],
                "series": [{"label": "GSCPI",
                            "points": year_filter([[r["date"], r2(r["value"])]
                                                   for r in reversed(gscpi)])}],
            }
        )
        out["updated"] = latest["date"]
    for indicator, label in (("GDP_GROWTH", "GDP 성장률 (월드)"),
                             ("CPI_INFLATION", "물가상승률 (월드)")):
        rows = conn.execute(
            "SELECT year, value FROM macro_series "
            "WHERE indicator = ? AND country_code = 'WLD' ORDER BY year",
            (indicator,),
        ).fetchall()
        if not rows:
            continue
        latest = rows[-1]
        out["groups"].append(
            {
                "title": f"{label} {latest['year']}",
                "metrics": [metric(label, latest["value"], None,
                                   str(latest["year"]), unit="%")],
                "series": [{"label": label,
                            "points": [[str(r["year"]), r2(r["value"])]
                                       for r in rows[-20:]]}],
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
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    temp.replace(path)
    log(f"{name} 작성 완료 ({path.stat().st_size:,} bytes)")


def run_module(module: dict) -> dict:
    command = [sys.executable, str(ROOT / module["cmd"][0]), *module["cmd"][1:]]
    log(f"▶ {module['id']} 실행: {' '.join(module['cmd'])}")
    started = time.time()
    try:
        proc = subprocess.run(command, cwd=str(ROOT),
                              timeout=module["timeout"], check=False)
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
        write_json("status.json", {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "runs": runs,
        })
        failed = [r["module"] for r in runs if not r["ok"]]
        if failed:
            log(f"크롤링 실패 모듈: {', '.join(failed)} (나머지 정상)")
    else:
        log("크롤링 생략(--summary-only), 요약만 재생성합니다.")

    write_json("summary.json", build_summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
