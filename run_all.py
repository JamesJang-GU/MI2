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
        "page": "sea/index.html",
    },
    {
        "id": "linerlytica",
        "title": "컨테이너 선대",
        "cmd": ["linerlytica/linerytica.py"],
        "timeout": 1800,
        "page": "linerlytica/detail.html",
    },
    {
        "id": "alphaliner",
        "title": "Top100 선사",
        "cmd": ["alphaliner/alphaliner.py", "--cron"],
        "timeout": 900,
        "page": "alphaliner/detail.html",
    },
    {
        "id": "forwarders",
        "title": "포워더 순위",
        "cmd": ["forwarders/forwarders.py", "--refresh"],
        "timeout": 600,
        "page": "forwarders/detail.html",
    },
    {
        "id": "air",
        "title": "TAC Index (항공)",
        "cmd": ["air/air.py", "--cron"],
        "timeout": 600,
        "page": "air/detail.html",
    },
    {
        "id": "oil",
        "title": "유가 / 벙커링",
        # --refresh: DB 캐시 대신 Selenium 실 크롤링(VLSFO+Opinet) 매일 수행
        "cmd": ["oil/oil.py", "--refresh"],
        "timeout": 1200,
        "page": "oil/detail.html",
    },
    {
        "id": "macro",
        "title": "거시경제",
        "cmd": ["macro/macro.py", "--cron"],
        "timeout": 600,
        "page": "macro/detail.html",
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


def metric(label, value, prev, asof, unit="", prev_asof=None):
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
        "prev_asof": prev_asof,
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
    """Active Ships + TEU 2단 + 각각 그래프 (순위/점유율 미표기).

    현재 크롤러는 snapshot_meta/operator_snapshots 테이블에 적재하므로
    해당 테이블을 읽고, 비어 있을 때만 레거시 alphaliner_master 로 폴백.
    """
    out = {"metrics": [], "series": []}
    # 레거시(alphaliner_master) 이력 + 신규(snapshot_meta) 스냅샷을 날짜별로 병합
    # Active Ships = 공식 글로벌 선대(global_ships), TEU = Top100 합계
    hist = {}
    for r in conn.execute(
        "SELECT data_date AS d, global_ships AS g, total_ships AS s, "
        "       total_teu AS teu FROM alphaliner_master ORDER BY data_date"
    ):
        hist[r["d"]] = (_clean(r["g"]) or _clean(r["s"]), _clean(r["teu"]))
    for r in conn.execute(
        "SELECT snapshot_date AS d, global_ships AS g, top100_teu AS teu "
        "FROM snapshot_meta ORDER BY snapshot_date"
    ):
        hist[r["d"]] = (_clean(r["g"]), _clean(r["teu"]))
    if not hist:
        return out
    dates = sorted(hist)
    latest_d = dates[-1]
    ship_dates = [d for d in dates if hist[d][0] is not None]
    prev_ship_d = ship_dates[-2] if len(ship_dates) > 1 else None
    teu_dates = [d for d in dates if hist[d][1] is not None]
    prev_teu_d = teu_dates[-2] if len(teu_dates) > 1 else None
    ships = hist[latest_d][0]
    teu = hist[latest_d][1]
    out["metrics"].append(
        metric("Active Ships", ships,
               hist[prev_ship_d][0] if prev_ship_d else None, latest_d,
               unit="척", prev_asof=prev_ship_d)
    )
    out["metrics"].append(
        metric("Top100 선대", teu,
               hist[prev_teu_d][1] if prev_teu_d else None, latest_d,
               unit="TEU", prev_asof=prev_teu_d)
    )
    out["series"].append(
        {"label": "Active Ships",
         "points": year_filter([[d, hist[d][0]] for d in ship_dates])}
    )
    out["series"].append(
        {"label": "Top100 선대 (TEU)",
         "points": year_filter([[d, hist[d][1]] for d in teu_dates])}
    )
    out["updated"] = latest_d
    return out


def summarize_forwarders(conn):
    """Top 1위만: 국적/회사명 + REV/TEU/TON."""
    out = {"metrics": [], "series": [], "top1": None}
    dates = conn.execute(
        "SELECT DISTINCT date FROM forwarders ORDER BY date DESC LIMIT 2"
    ).fetchall()
    if not dates:
        return out
    latest_date = dates[0]["date"]
    prev_date = dates[1]["date"] if len(dates) > 1 else None

    top1 = conn.execute(
        "SELECT provider, hq, revenue, teu, air_tons FROM forwarders "
        "WHERE date = ? ORDER BY rank ASC, revenue DESC LIMIT 1",
        (latest_date,),
    ).fetchone()
    if not top1:
        return out
    out["top1"] = {"name": top1["provider"], "hq": top1["hq"]}

    prev_row = None
    if prev_date:
        prev_row = conn.execute(
            "SELECT revenue, teu, air_tons FROM forwarders "
            "WHERE date = ? AND provider = ?",
            (prev_date, top1["provider"]),
        ).fetchone()
    for label, column, unit in (("REV", "revenue", "$M"),
                                ("TEU", "teu", ""),
                                ("TON", "air_tons", "ton")):
        out["metrics"].append(
            metric(label, top1[column],
                   prev_row[column] if prev_row else None, latest_date, unit=unit,
                   prev_asof=prev_date if prev_row else None)
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
    diag = conn.execute(
        "SELECT fetched_at FROM data_meta WHERE dataset = 'vlsfo_diag'"
    ).fetchone()
    if diag and diag["fetched_at"] and not str(diag["fetched_at"]).startswith("ok"):
        out["note"] = str(diag["fetched_at"])
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
        prev = rows[-2] if len(rows) > 1 else None
        out["groups"].append(
            {
                "title": f"{label} {latest['year']} · YoY",
                "metrics": [metric(label, latest["value"],
                                   prev["value"] if prev else None,
                                   str(latest["year"]), unit="%",
                                   prev_asof=str(prev["year"]) if prev else None)],
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


SEA_SHELL = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SEA DETAIL — SCFI / BDI</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/hammerjs@2.0.8/hammer.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js"></script>
<style>
  :root{--bg:#000;--panel:#0b0b0b;--panel2:#101010;--line:#2b2b2b;--amber:#ffa500;
        --txt:#e6e6e6;--dim:#8f8f8f;--up:#ff4d4d;--down:#58a6ff;--flat:#9a9a9a;
        --mono:Consolas,"Liberation Mono",Menlo,"Malgun Gothic",monospace}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);font-family:var(--mono)}
  .topbar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:8px 12px;
          border-bottom:2px solid var(--amber);
          background:linear-gradient(180deg,#151000,#000)}
  .code{color:#000;background:var(--amber);font-weight:700;font-size:11px;
        padding:1px 6px;letter-spacing:.08em}
  .ttl{color:var(--amber);font-size:13px;letter-spacing:.08em}
  .ref{color:var(--dim);font-size:10px}
  .btn{color:var(--amber);border:1px solid var(--amber);background:none;
       font-family:var(--mono);font-size:10px;padding:2px 9px;cursor:pointer;
       letter-spacing:.06em}
  .btn:hover{background:var(--amber);color:#000}
  .back{margin-left:auto;color:var(--amber);border:1px solid var(--amber);
        font-size:10px;padding:2px 8px;text-decoration:none;letter-spacing:.06em}
  .back:hover{background:var(--amber);color:#000}
  .wrap{padding:8px;display:flex;flex-direction:column;gap:8px}
  /* 좌우 패널 7행(헤더/칩/차트/스트립/가이드/필터/테이블) subgrid 정렬 */
  .charts{display:grid;grid-template-columns:1fr 1fr;
          grid-template-rows:repeat(7,auto);gap:8px}
  .panel{background:var(--panel);border:1px solid var(--line);padding:8px;
         display:grid;grid-template-rows:subgrid;grid-row:span 7;gap:6px;min-width:0}
  @media(max-width:980px){
    .charts{grid-template-columns:1fr;grid-template-rows:none}
    .panel{display:flex;flex-direction:column;grid-row:auto}
  }
  .phead{display:flex;flex-wrap:wrap;align-items:center;gap:6px}
  .ptitle{color:var(--amber);font-size:12px;letter-spacing:.08em}
  .chips{display:flex;flex-wrap:wrap;gap:4px;align-content:flex-start}
  .chip{border:1px solid var(--line);color:var(--dim);font-size:10px;
        padding:1px 7px;cursor:pointer;user-select:none}
  .chip.on{color:#000;background:var(--amber);border-color:var(--amber);font-weight:700}
  .spacer{margin-left:auto}
  .chartbox{height:300px;position:relative}
  .navbox{height:46px;position:relative;border:1px solid var(--line);
          background:#0d0d0d;cursor:crosshair}
  .navbox canvas{width:100%;height:100%;display:block}
  .hint{color:#555;font-size:9px;letter-spacing:.05em}
  .tbar{display:flex;flex-wrap:wrap;align-items:center;gap:6px}
  .tbar input{background:var(--panel2);border:1px solid var(--line);color:var(--txt);
              font-family:var(--mono);font-size:10px;padding:2px 6px;width:150px}
  .note{color:var(--dim);font-size:10px}
  .tblbox{height:300px;overflow:auto;border:1px solid var(--line);background:var(--panel2)}
  table{width:100%;border-collapse:collapse;font-size:11px;
        font-variant-numeric:tabular-nums}
  th{color:var(--dim);text-align:right;font-weight:400;border-bottom:1px solid var(--line);
     padding:3px 6px;position:sticky;top:0;background:var(--panel2);cursor:pointer;
     white-space:nowrap}
  th:first-child,th:nth-child(2){text-align:left}
  th:hover{color:var(--amber)}
  th.sorted{color:var(--amber)}
  td{border-bottom:1px dotted #1c1c1c;padding:3px 6px;text-align:right;white-space:nowrap}
  td:first-child,td:nth-child(2){text-align:left}
  td:first-child{color:var(--dim)}
  .up{color:var(--up)} .down{color:var(--down)} .flat{color:var(--flat)}
  footer{color:var(--dim);font-size:10px;letter-spacing:.06em;padding:8px 12px 20px;
         display:flex;flex-wrap:wrap;gap:12px}
  footer b{color:var(--amber);font-weight:400}
</style>
</head>
<body>
<div class="topbar">
  <span class="code">SEA</span>
  <span class="ttl">SCFI / BDI — SHIPPING INDEX TERMINAL</span>
  <span class="ref" id="ref"></span>
  <a class="back" href="../index.html">← TERMINAL</a>
</div>

<div class="wrap">
  <div class="charts">
    <div class="panel">
      <div class="phead"><span class="ptitle">SCFI — 권역 비교</span>
        <span class="spacer"></span>
        <button class="btn" data-reset="ds">FULL</button>
        <button class="btn" data-dl="ds-csv">CSV ↓</button>
        <button class="btn" data-dl="ds-xlsx">XLSX ↓</button></div>
      <div class="chips" id="chips-scfi"></div>
      <div class="chartbox"><canvas id="c-ds"></canvas></div>
      <div class="navbox"><canvas id="n-ds-nav"></canvas></div>
      <div class="hint">하단 스트립 드래그=기간 선택 · 차트 WHEEL=ZOOM · ALT+DRAG=PAN · 더블클릭=전체</div>
      <div class="tbar">
        <input id="f-ds" placeholder="filter: date / region">
        <span class="note" id="n-ds"></span></div>
      <div class="tblbox"><table id="t-ds">
        <thead><tr>
          <th data-key="date">DATE</th><th data-key="region">REGION</th>
          <th data-key="value">INDEX VALUE</th><th data-key="change">CHANGE</th>
          <th data-key="pct">CHANGE RATE (%)</th>
        </tr></thead><tbody></tbody>
      </table></div>
    </div>
    <div class="panel">
      <div class="phead"><span class="ptitle">BDI — 지수 비교</span>
        <span class="spacer"></span>
        <button class="btn" data-reset="db">FULL</button>
        <button class="btn" data-dl="db-csv">CSV ↓</button>
        <button class="btn" data-dl="db-xlsx">XLSX ↓</button></div>
      <div class="chips" id="chips-bdi"></div>
      <div class="chartbox"><canvas id="c-db"></canvas></div>
      <div class="navbox"><canvas id="n-db-nav"></canvas></div>
      <div class="hint">하단 스트립 드래그=기간 선택 · 차트 WHEEL=ZOOM · ALT+DRAG=PAN · 더블클릭=전체</div>
      <div class="tbar">
        <input id="f-db" placeholder="filter: date / region">
        <span class="note" id="n-db"></span></div>
      <div class="tblbox"><table id="t-db">
        <thead><tr>
          <th data-key="date">DATE</th><th data-key="region">REGION</th>
          <th data-key="value">INDEX VALUE</th><th data-key="change">CHANGE</th>
          <th data-key="pct">CHANGE RATE (%)</th>
        </tr></thead><tbody></tbody>
      </table></div>
    </div>
  </div>
</div>

<footer>
  <span><b>SOURCE</b> SCFI=Shanghai Shipping Exchange · BDI/BCI/BPI/BSI=Baltic Exchange (KSG 경유)</span>
  <span><b>AUTO-CRAWL</b> DAILY 00:00 KST</span>
  <span id="gen"></span>
</footer>

<script>
const PALETTE = ["#ffa500", "#79c0ff", "#d2a8ff", "#7ee787", "#ff7b72", "#ffa657",
                 "#a5d6ff", "#f778ba", "#d4a72c", "#8ddb8c", "#ff9bce", "#9ecbff"];
let DATA = null;
const PANELS = {};

function fmt(v){ return v === null || v === undefined ? "-" :
  Number(v).toLocaleString("ko-KR", {maximumFractionDigits: 2}); }
function sign(v){ if (v === null || v === undefined) return "-";
  return (v > 0 ? "+" : "") + fmt(v); }
function cls(v){ return v === null || v === undefined ? "flat" :
  v > 0 ? "up" : v < 0 ? "down" : "flat"; }
function ms(d){ return Date.parse(d + "T00:00:00Z"); }
function dstr(t){ return new Date(t).toISOString().slice(0, 10); }
function regionsOf(index){ return Object.keys(DATA[index].series); }
function colorOf(index, region){
  return PALETTE[regionsOf(index).indexOf(region) % PALETTE.length]; }
function pointsOf(index, region){
  const blk = DATA[index];
  if (!blk._ms) blk._ms = blk.dates.map(ms);
  const vals = blk.series[region], out = [];
  for (let i = 0; i < blk._ms.length; i++){
    const v = vals[i];
    if (v !== null && v !== undefined) out.push([blk._ms[i], v]);
  }
  return out;
}

class Panel {
  constructor(id, index, opts){
    this.id = id; this.index = index; this.opts = opts || {};
    this.sel = (opts.initial || [regionsOf(index)[0]]).slice();
    this.navRegion = this.sel[0];
    this.win = null; this.sort = {key: "date", dir: -1}; this.filter = "";
    this.full = null;
    this.nav = opts.nav;
    this.navDrag = null;
    this.chart = new Chart(opts.canvas, {
      type: "line",
      data: {datasets: this.datasets()},
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        interaction: {mode: "nearest", intersect: false},
        onHover: (e, els, chart) => { e.native.target.style.cursor = "crosshair"; },
        plugins: {
          legend: {display: true, position: "top",
                   labels: {color: "#8f8f8f", boxWidth: 8, font: {size: 9}}},
          tooltip: {displayColors: false, backgroundColor: "#111",
            borderColor: "#3a3a3a", borderWidth: 1, titleColor: "#ffa500",
            bodyColor: "#e6e6e6",
            callbacks: {title: it => dstr(it[0].parsed.x),
                        label: it => ` ${it.dataset.label} ${fmt(it.parsed.y)}`}},
          zoom: {
            zoom: {wheel: {enabled: true}, pinch: {enabled: true},
              drag: {enabled: true, backgroundColor: "rgba(255,165,0,.12)",
                     borderColor: "#ffa500", borderWidth: 1},
              mode: "x",
              onZoomComplete: c => this.onWindow(c.chart)},
            pan: {enabled: true, mode: "x", modifierKey: "Alt",
                  onPanComplete: c => this.onWindow(c.chart)},
            limits: {x: {min: "original", max: "original"}},
          },
        },
        scales: {
          x: {type: "linear", ticks: {color: "#777", maxTicksLimit: 8,
              callback: v => dstr(v), font: {size: 9, family: "Consolas"}},
              grid: {color: "#161616"}},
          y: {ticks: {color: "#777", font: {size: 9, family: "Consolas"}},
              grid: {color: "#161616"}},
        },
      },
    });
    this.full = [this.chart.scales.x.min, this.chart.scales.x.max];
    this.bindNav();
    this.drawNav();
    opts.canvas.ondblclick = () => this.reset();
  }
  datasets(){
    return this.sel.map(r => ({
      label: r,
      data: pointsOf(this.index, r).map(p => ({x: p[0], y: p[1]})),
      borderColor: colorOf(this.index, r), backgroundColor: "transparent",
      borderWidth: 1.4, pointRadius: 0, pointHoverRadius: 3, tension: .15,
    }));
  }
  onWindow(chart){
    const x = chart.scales.x;
    const DAY = 86400000;
    if (this.full && x.min <= this.full[0] + DAY && x.max >= this.full[1] - DAY)
      this.win = null;
    else
      this.win = [x.min, x.max];
    this.drawNav();
    this.renderTable();
  }
  applyWindow(t0, t1){
    this.chart.zoomScale("x", {min: t0, max: t1}, "none");
    this.win = [t0, t1];
    this.drawNav();
    this.renderTable();
  }
  reset(){
    this.chart.resetZoom();
    this.win = null;
    this.drawNav();
    this.renderTable();
  }
  refresh(){ this.chart.data.datasets = this.datasets();
    this.chart.update("none"); this.renderTable(); }
  /* ---- 하단 기간 스트립 (브러시) ---- */
  bindNav(){
    const cv = this.nav;
    cv.addEventListener("pointerdown", e => {
      cv.setPointerCapture(e.pointerId);
      this.navDrag = {x0: e.offsetX, x1: e.offsetX};
    });
    cv.addEventListener("pointermove", e => {
      if (!this.navDrag) return;
      this.navDrag.x1 = e.offsetX;
      this.drawNav();
    });
    cv.addEventListener("pointerup", e => {
      if (!this.navDrag) return;
      const {x0, x1} = this.navDrag;
      this.navDrag = null;
      if (Math.abs(x1 - x0) < 5){ this.drawNav(); return; }
      let t0 = this.navX2T(Math.min(x0, x1));
      let t1 = this.navX2T(Math.max(x0, x1));
      this.applyWindow(t0, t1);
    });
  }
  navX2T(x){
    const w = this.nav.clientWidth || 1;
    return this.full[0] + (x / w) * (this.full[1] - this.full[0]);
  }
  navT2X(t){
    const w = this.nav.clientWidth || 1;
    return ((t - this.full[0]) / (this.full[1] - this.full[0])) * w;
  }
  drawNav(){
    const cv = this.nav;
    const ctx = cv.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth, h = cv.clientHeight;
    if (!w || !h) return;
    cv.width = w * dpr; cv.height = h * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "#0d0d0d"; ctx.fillRect(0, 0, w, h);
    const pts = pointsOf(this.index, this.navRegion);
    if (pts.length){
      let vmin = Infinity, vmax = -Infinity;
      for (const p of pts){ if (p[1] < vmin) vmin = p[1]; if (p[1] > vmax) vmax = p[1]; }
      const span = (vmax - vmin) || 1;
      ctx.strokeStyle = "rgba(255,165,0,.75)"; ctx.lineWidth = 1;
      ctx.beginPath();
      for (let i = 0; i < pts.length; i++){
        const x = this.navT2X(pts[i][0]);
        const y = h - 4 - ((pts[i][1] - vmin) / span) * (h - 8);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }
    const sel = this.navDrag
      ? [Math.min(this.navDrag.x0, this.navDrag.x1),
         Math.max(this.navDrag.x0, this.navDrag.x1)]
      : (this.win ? [this.navT2X(this.win[0]), this.navT2X(this.win[1])] : null);
    if (sel){
      ctx.fillStyle = "rgba(0,0,0,.62)";
      ctx.fillRect(0, 0, sel[0], h);
      ctx.fillRect(sel[1], 0, w - sel[1], h);
      ctx.strokeStyle = "#ffa500"; ctx.lineWidth = 1;
      ctx.strokeRect(sel[0] + .5, .5, Math.max(sel[1] - sel[0] - 1, 1), h - 1);
    }
    ctx.fillStyle = "#555"; ctx.font = "9px Consolas,monospace";
    ctx.fillText(dstr(this.full[0]), 3, h - 3);
    const right = dstr(this.full[1]);
    ctx.fillText(right, w - ctx.measureText(right).width - 3, h - 3);
  }
  /* ---- 테이블 ---- */
  windowRows(){
    const rows = [];
    for (const r of this.sel){
      const pts = pointsOf(this.index, r);
      for (let i = 0; i < pts.length; i++){
        const t = pts[i][0];
        if (this.win && (t < this.win[0] || t > this.win[1])) continue;
        const prev = i > 0 ? pts[i - 1][1] : null;
        const cur = pts[i][1];
        rows.push({
          index: this.index.toUpperCase(), date: dstr(t), t, region: r, value: cur,
          change: prev === null ? null : +((cur - prev).toFixed(2)),
          pct: (prev === null || prev === 0) ? null
               : +(((cur - prev) / Math.abs(prev)) * 100).toFixed(2),
        });
      }
    }
    return rows;
  }
  filtered(){
    let rows = this.windowRows();
    const f = this.filter.trim().toLowerCase();
    if (f) rows = rows.filter(r =>
      r.region.toLowerCase().includes(f) || r.date.includes(f) ||
      r.index.toLowerCase().includes(f));
    const {key, dir} = this.sort;
    rows.sort((a, b) => {
      const va = a[key], vb = b[key];
      return (va < vb ? -1 : va > vb ? 1 : 0) * dir;
    });
    return rows;
  }
  renderTable(){
    if (!this.opts.table) return;
    const rows = this.filtered();
    this.lastRows = rows;
    const show = rows.slice(0, 500);
    this.opts.table.querySelector("tbody").innerHTML = show.map(r => `
      <tr><td>${r.date}</td><td>${r.region}</td><td>${fmt(r.value)}</td>
      <td class="${cls(r.change)}">${sign(r.change)}</td>
      <td class="${cls(r.pct)}">${sign(r.pct)}</td></tr>`).join("");
    this.opts.note.textContent =
      `${rows.length.toLocaleString()} rows` +
      (rows.length > 500 ? " · 최신 500행 표시" : "") +
      (this.win ? ` · ${dstr(this.win[0])} ~ ${dstr(this.win[1])}` : " · 전체 기간");
    this.opts.table.querySelectorAll("th").forEach(th => {
      th.classList.toggle("sorted", th.dataset.key === this.sort.key);
      th.textContent = th.textContent.replace(/ [▲▼]$/, "") +
        (th.dataset.key === this.sort.key ? (this.sort.dir > 0 ? " ▲" : " ▼") : "");
    });
  }
  download(kind){
    const rows = this.filtered();
    const head = ["Date", "Index", "Region", "Index Value", "Change",
                  "Change Rate(%)"];
    const aoa = [head, ...rows.map(r =>
      [r.date, r.index, r.region, r.value, r.change, r.pct])];
    const tag = `${this.index}_${this.sel.join("+")}`.replace(/[^A-Za-z가-힣+_]/g, "_");
    if (kind === "csv"){
      const csv = "\\uFEFF" + aoa.map(line => line.map(v =>
        v === null || v === undefined ? "" : String(v)).join(",")).join("\\r\\n");
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([csv], {type: "text/csv"}));
      a.download = `${tag}.csv`; a.click(); URL.revokeObjectURL(a.href);
    } else {
      const ws = XLSX.utils.aoa_to_sheet(aoa);
      const wb = XLSX.utils.book_new();
      XLSX.utils.book_append_sheet(wb, ws, this.index.toUpperCase());
      XLSX.writeFile(wb, `${tag}.xlsx`);
    }
  }
}

function buildChips(panel, el){
  el.innerHTML = regionsOf(panel.index).map(r =>
    `<span class="chip ${panel.sel.includes(r) ? "on" : ""}"
          data-r="${r}" style="${panel.sel.includes(r) ?
          "" : `border-color:${colorOf(panel.index, r)};color:${colorOf(panel.index, r)}`}">
      ${r}</span>`).join("");
  el.querySelectorAll(".chip").forEach(chip => chip.onclick = () => {
    const r = chip.dataset.r;
    if (panel.sel.includes(r)){
      if (panel.sel.length > 1) panel.sel = panel.sel.filter(x => x !== r);
    } else panel.sel.push(r);
    buildChips(panel, el);
    panel.refresh();
  });
}

fetch("sea_data.json?t=" + Date.now()).then(r => r.json()).then(data => {
  DATA = data;
  document.getElementById("ref").textContent = "REF " + (data.updated || "-");
  document.getElementById("gen").innerHTML =
    "<b>GENERATED</b> " + (data.generated || "-");
  if (window.ChartZoom) Chart.register(window.ChartZoom);

  PANELS.ds = new Panel("ds", "scfi", {initial: ["종합"],
    canvas: document.getElementById("c-ds"), nav: document.getElementById("n-ds-nav"),
    table: document.getElementById("t-ds"), note: document.getElementById("n-ds")});
  PANELS.db = new Panel("db", "bdi", {initial: ["BDI"],
    canvas: document.getElementById("c-db"), nav: document.getElementById("n-db-nav"),
    table: document.getElementById("t-db"), note: document.getElementById("n-db")});
  buildChips(PANELS.ds, document.getElementById("chips-scfi"));
  buildChips(PANELS.db, document.getElementById("chips-bdi"));
  PANELS.ds.renderTable(); PANELS.db.renderTable();

  document.querySelectorAll("[data-reset]").forEach(b =>
    b.onclick = () => PANELS[b.dataset.reset].reset());
  document.querySelectorAll("[data-dl]").forEach(b => b.onclick = () => {
    const [pid, kind] = b.dataset.dl.split("-");
    PANELS[pid].download(kind);
  });
  document.getElementById("f-ds").oninput = e => {
    PANELS.ds.filter = e.target.value; PANELS.ds.renderTable(); };
  document.getElementById("f-db").oninput = e => {
    PANELS.db.filter = e.target.value; PANELS.db.renderTable(); };
  [["#t-ds", PANELS.ds], ["#t-db", PANELS.db]].forEach(([sel, panel]) =>
    document.querySelectorAll(sel + " th").forEach(th => th.onclick = () => {
      const k = th.dataset.key;
      panel.sort = {key: k, dir: panel.sort.key === k ? -panel.sort.dir : -1};
      panel.renderTable(); }));
  window.addEventListener("resize", () => {
    PANELS.ds.drawNav(); PANELS.db.drawNav(); });
}).catch(e => {
  document.getElementById("ref").textContent = "sea_data.json 로드 실패: " + e;
});
</script>
</body>
</html>
"""


def build_sea_page(conn) -> None:
    """sea/ 정적 상세 애플리케이션 생성 (index.html + sea_data.json)."""
    updated = conn.execute("SELECT MAX(dt) FROM index_raw").fetchone()[0] or "-"
    payload = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "updated": updated,
    }
    for key, index_name in (("scfi", "SCFI"), ("bdi", "BDI")):
        rows = conn.execute(
            "SELECT region, dt, value FROM index_raw "
            "WHERE index_name = ? ORDER BY region, dt",
            (index_name,),
        ).fetchall()
        dates = sorted({r["dt"] for r in rows})
        position = {d: i for i, d in enumerate(dates)}
        series = {}
        for r in rows:
            series.setdefault(r["region"], [None] * len(dates))[position[r["dt"]]] = r2(r["value"])
        payload[key] = {"dates": dates, "series": series}

    data_path = ROOT / "sea" / "sea_data.json"
    temp = data_path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8")
    temp.replace(data_path)
    log(f"sea/sea_data.json 작성 완료 ({data_path.stat().st_size:,} bytes)")

    page_path = ROOT / "sea" / "index.html"
    temp = page_path.with_suffix(".html.tmp")
    temp.write_text(SEA_SHELL, encoding="utf-8")
    temp.replace(page_path)
    log(f"sea/index.html 작성 완료 ({page_path.stat().st_size:,} bytes)")


DETAIL_SHELL = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__PAGE_TITLE__</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/hammerjs@2.0.8/hammer.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js"></script>
<style>
  :root{--bg:#000;--panel:#0b0b0b;--panel2:#101010;--line:#2b2b2b;--amber:#ffa500;
        --txt:#e6e6e6;--dim:#8f8f8f;--up:#ff4d4d;--down:#58a6ff;--flat:#9a9a9a;
        --mono:Consolas,"Liberation Mono",Menlo,"Malgun Gothic",monospace}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);font-family:var(--mono)}
  .topbar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:8px 12px;
          border-bottom:2px solid var(--amber);
          background:linear-gradient(180deg,#151000,#000)}
  .code{color:#000;background:var(--amber);font-weight:700;font-size:11px;
        padding:1px 6px;letter-spacing:.08em}
  .ttl{color:var(--amber);font-size:13px;letter-spacing:.08em}
  .ref{color:var(--dim);font-size:10px}
  .btn{color:var(--amber);border:1px solid var(--amber);background:none;
       font-family:var(--mono);font-size:10px;padding:2px 9px;cursor:pointer;
       letter-spacing:.06em}
  .btn:hover{background:var(--amber);color:#000}
  .back{margin-left:auto;color:var(--amber);border:1px solid var(--amber);
        font-size:10px;padding:2px 8px;text-decoration:none;letter-spacing:.06em}
  .back:hover{background:var(--amber);color:#000}
  .wrap{padding:8px;display:flex;flex-direction:column;gap:8px}
  .charts{display:grid;grid-template-columns:1fr 1fr;gap:8px}
  .panel{background:var(--panel);border:1px solid var(--line);padding:8px;
         display:flex;flex-direction:column;gap:6px;min-width:0}
  .panel.wide{grid-column:1/-1}
  @media(max-width:980px){.charts{grid-template-columns:1fr}.panel.wide{grid-column:auto}}
  .phead{display:flex;flex-wrap:wrap;align-items:center;gap:6px}
  .ptitle{color:var(--amber);font-size:12px;letter-spacing:.08em}
  .chips{display:flex;flex-wrap:wrap;gap:4px;align-content:flex-start}
  .chip{border:1px solid var(--line);color:var(--dim);font-size:10px;
        padding:1px 7px;cursor:pointer;user-select:none}
  .chip.on{color:#000;background:var(--amber);border-color:var(--amber);font-weight:700}
  .fixedlbl{color:var(--dim);font-size:10px;border:1px dashed var(--line);
            padding:1px 7px}
  .spacer{margin-left:auto}
  .chartbox{height:300px;position:relative}
  .navbox{height:46px;position:relative;border:1px solid var(--line);
          background:#0d0d0d;cursor:crosshair}
  .navbox canvas{width:100%;height:100%;display:block}
  .hint{color:#555;font-size:9px;letter-spacing:.05em}
  .tbar{display:flex;flex-wrap:wrap;align-items:center;gap:6px}
  .tbar input,.tbar select{background:var(--panel2);border:1px solid var(--line);
      color:var(--txt);font-family:var(--mono);font-size:10px;padding:2px 6px}
  .tbar input{width:150px}
  .note{color:var(--dim);font-size:10px}
  .tblbox{height:300px;overflow:auto;border:1px solid var(--line);background:var(--panel2)}
  .tblbox.auto{height:auto;max-height:420px}
  table{width:100%;border-collapse:collapse;font-size:11px;
        font-variant-numeric:tabular-nums}
  th{color:var(--dim);text-align:right;font-weight:400;border-bottom:1px solid var(--line);
     padding:3px 6px;position:sticky;top:0;background:var(--panel2);cursor:pointer;
     white-space:nowrap}
  th.txt,td.txt{text-align:left}
  th:hover{color:var(--amber)}
  th.sorted{color:var(--amber)}
  td{border-bottom:1px dotted #1c1c1c;padding:3px 6px;text-align:right;white-space:nowrap}
  td:first-child{color:var(--dim)}
  .up{color:var(--up)} .down{color:var(--down)} .flat{color:var(--flat)}
  .section{background:var(--panel);border:1px solid var(--line);padding:8px;
           display:flex;flex-direction:column;gap:6px}
  footer{color:var(--dim);font-size:10px;letter-spacing:.06em;padding:8px 12px 20px;
         display:flex;flex-wrap:wrap;gap:12px}
  footer b{color:var(--amber);font-weight:400}
</style>
</head>
<body>
<div class="topbar">
  <span class="code" id="code"></span>
  <span class="ttl" id="title"></span>
  <span class="ref" id="ref"></span>
  <a class="back" href="../index.html">← TERMINAL</a>
</div>
<div class="wrap">
  <div class="charts" id="panels"></div>
  <div id="sections"></div>
</div>
<footer>
  <span><b>AUTO-CRAWL</b> DAILY 00:00 KST · GITHUB ACTIONS</span>
  <span id="gen"></span>
</footer>
<script>
const PALETTE = ["#ffa500", "#79c0ff", "#d2a8ff", "#7ee787", "#ff7b72", "#ffa657",
                 "#a5d6ff", "#f778ba", "#d4a72c", "#8ddb8c", "#ff9bce", "#9ecbff"];
const CONFIG = __CONFIG__;
const PANELS = {};

function fmt(v){ return v === null || v === undefined ? "-" :
  Number(v).toLocaleString("ko-KR", {maximumFractionDigits: 2}); }
function sign(v){ if (v === null || v === undefined) return "-";
  return (v > 0 ? "+" : "") + fmt(v); }
function cls(v){ return v === null || v === undefined ? "flat" :
  v > 0 ? "up" : v < 0 ? "down" : "flat"; }
function ms(d){ return Date.parse(d + "T00:00:00Z"); }
function dstr(t){ return new Date(t).toISOString().slice(0, 10); }
function ds(key){
  const d = CONFIG.datasets[key];
  if (!d._ms) d._ms = d.points.map(p => ms(p[0]));
  return d;
}
function pointsOf(key){
  const d = ds(key);
  return d._ms.map((t, i) => [t, d.points[i][1]]);
}
function colorOf(panel, key){
  return PALETTE[panel.keys.indexOf(key) % PALETTE.length];
}
function flagImg(cc){
  const c = String(cc || "").toUpperCase().slice(0, 2);
  if (!/^[A-Z]{2}$/.test(c)) return "";
  const cps = [...c].map(ch => (0x1F1E6 + ch.charCodeAt(0) - 65).toString(16)).join("-");
  return `<img src="https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/svg/${cps}.svg"
    alt="${c}" style="width:16px;height:12px;vertical-align:-2px;margin-right:6px;
    border:1px solid #333;background:#111">`;
}

class Panel {
  constructor(cfg){
    this.cfg = cfg; this.keys = cfg.keys;
    this.sel = (cfg.initial || cfg.keys.slice(0, 1)).slice();
    this.navKey = cfg.navKey || this.sel[0];
    this.win = null; this.sort = {key: "date", dir: -1}; this.filter = "";
    this.full = null; this.navDrag = null;

    const root = document.createElement("div");
    root.className = "panel" + (cfg.span === 2 ? " wide" : "");
    root.innerHTML = `
      <div class="phead"><span class="ptitle">${cfg.title}</span>
        <span class="spacer"></span>
        <button class="btn" data-a="reset">FULL</button>
        <button class="btn" data-a="csv">CSV ↓</button>
        <button class="btn" data-a="xlsx">XLSX ↓</button></div>
      <div class="chips"></div>
      <div class="chartbox"><canvas></canvas></div>
      <div class="navbox"><canvas></canvas></div>
      <div class="hint">하단 스트립 드래그=기간 선택 · 차트 WHEEL=ZOOM · ALT+DRAG=PAN · 더블클릭=전체</div>
      <div class="tbar"><input placeholder="filter: date / series">
        <span class="note"></span></div>
      <div class="tblbox"><table>
        <thead><tr>
          <th class="txt" data-key="date">DATE</th>
          <th class="txt" data-key="series">SERIES</th>
          <th data-key="value">VALUE</th>
          <th data-key="change">CHANGE</th>
          <th data-key="pct">CHANGE RATE (%)</th>
        </tr></thead><tbody></tbody>
      </table></div>`;
    document.getElementById("panels").appendChild(root);
    this.root = root;
    this.chipsEl = root.querySelector(".chips");
    this.table = root.querySelector("table");
    this.note = root.querySelector(".note");
    this.nav = root.querySelectorAll("canvas")[1];
    root.querySelector("[data-a=reset]").onclick = () => this.reset();
    root.querySelector("[data-a=csv]").onclick = () => this.download("csv");
    root.querySelector("[data-a=xlsx]").onclick = () => this.download("xlsx");
    root.querySelector("input").oninput = e => {
      this.filter = e.target.value; this.renderTable(); };
    this.table.querySelectorAll("th").forEach(th => th.onclick = () => {
      const k = th.dataset.key;
      this.sort = {key: k, dir: this.sort.key === k ? -this.sort.dir : -1};
      this.renderTable(); });

    if (cfg.fixed){
      this.chipsEl.innerHTML =
        `<span class="fixedlbl">${CONFIG.datasets[this.sel[0]].label}</span>`;
    } else this.buildChips();

    this.chart = new Chart(root.querySelectorAll("canvas")[0], {
      type: "line",
      data: {datasets: this.datasets()},
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        interaction: {mode: "nearest", intersect: false},
        plugins: {
          legend: {display: !cfg.fixed, position: "top",
                   labels: {color: "#8f8f8f", boxWidth: 8, font: {size: 9}}},
          tooltip: {displayColors: false, backgroundColor: "#111",
            borderColor: "#3a3a3a", borderWidth: 1, titleColor: "#ffa500",
            bodyColor: "#e6e6e6",
            callbacks: {title: it => dstr(it[0].parsed.x),
                        label: it => ` ${it.dataset.label} ${fmt(it.parsed.y)}`}},
          zoom: {
            zoom: {wheel: {enabled: true}, pinch: {enabled: true},
              drag: {enabled: true, backgroundColor: "rgba(255,165,0,.12)",
                     borderColor: "#ffa500", borderWidth: 1},
              mode: "x", onZoomComplete: c => this.onWindow(c.chart)},
            pan: {enabled: true, mode: "x", modifierKey: "Alt",
                  onPanComplete: c => this.onWindow(c.chart)},
            limits: {x: {min: "original", max: "original"}},
          },
        },
        scales: {
          x: {type: "linear", ticks: {color: "#777", maxTicksLimit: 8,
              callback: v => dstr(v), font: {size: 9, family: "Consolas"}},
              grid: {color: "#161616"}},
          y: {ticks: {color: "#777", font: {size: 9, family: "Consolas"}},
              grid: {color: "#161616"}},
        },
      },
    });
    this.full = [this.chart.scales.x.min, this.chart.scales.x.max];
    this.bindNav();
    this.drawNav();
    this.renderTable();
    root.querySelectorAll("canvas")[0].ondblclick = () => this.reset();
  }
  datasets(){
    return this.sel.map(k => ({
      label: CONFIG.datasets[k].label,
      data: pointsOf(k).map(p => ({x: p[0], y: p[1]})),
      borderColor: colorOf(this, k), backgroundColor: "transparent",
      borderWidth: 1.4, pointRadius: 0, pointHoverRadius: 3, tension: .15,
    }));
  }
  buildChips(){
    this.chipsEl.innerHTML = this.keys.map(k =>
      `<span class="chip ${this.sel.includes(k) ? "on" : ""}" data-k="${k}"
        style="${this.sel.includes(k) ? "" :
          `border-color:${colorOf(this, k)};color:${colorOf(this, k)}`}">
        ${CONFIG.datasets[k].label}</span>`).join("");
    this.chipsEl.querySelectorAll(".chip").forEach(chip => chip.onclick = () => {
      const k = chip.dataset.k;
      if (this.sel.includes(k)){
        if (this.sel.length > 1) this.sel = this.sel.filter(x => x !== k);
      } else this.sel.push(k);
      this.buildChips(); this.refresh(); });
  }
  onWindow(chart){
    const x = chart.scales.x, DAY = 86400000;
    if (this.full && x.min <= this.full[0] + DAY && x.max >= this.full[1] - DAY)
      this.win = null;
    else this.win = [x.min, x.max];
    this.drawNav(); this.renderTable();
  }
  applyWindow(t0, t1){
    this.chart.zoomScale("x", {min: t0, max: t1}, "none");
    this.win = [t0, t1]; this.drawNav(); this.renderTable();
  }
  reset(){ this.chart.resetZoom(); this.win = null;
    this.drawNav(); this.renderTable(); }
  refresh(){ this.chart.data.datasets = this.datasets();
    this.chart.update("none"); this.renderTable(); }
  bindNav(){
    const cv = this.nav;
    cv.addEventListener("pointerdown", e => {
      cv.setPointerCapture(e.pointerId);
      this.navDrag = {x0: e.offsetX, x1: e.offsetX}; });
    cv.addEventListener("pointermove", e => {
      if (this.navDrag){ this.navDrag.x1 = e.offsetX; this.drawNav(); } });
    cv.addEventListener("pointerup", e => {
      if (!this.navDrag) return;
      const {x0, x1} = this.navDrag; this.navDrag = null;
      if (Math.abs(x1 - x0) < 5){ this.drawNav(); return; }
      this.applyWindow(this.navX2T(Math.min(x0, x1)), this.navX2T(Math.max(x0, x1)));
    });
  }
  navX2T(x){ const w = this.nav.clientWidth || 1;
    return this.full[0] + (x / w) * (this.full[1] - this.full[0]); }
  navT2X(t){ const w = this.nav.clientWidth || 1;
    return ((t - this.full[0]) / (this.full[1] - this.full[0])) * w; }
  drawNav(){
    const cv = this.nav, ctx = cv.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth, h = cv.clientHeight;
    if (!w || !h) return;
    cv.width = w * dpr; cv.height = h * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "#0d0d0d"; ctx.fillRect(0, 0, w, h);
    const pts = pointsOf(this.navKey);
    if (pts.length){
      let vmin = Infinity, vmax = -Infinity;
      for (const p of pts){ if (p[1] < vmin) vmin = p[1]; if (p[1] > vmax) vmax = p[1]; }
      const span = (vmax - vmin) || 1;
      ctx.strokeStyle = "rgba(255,165,0,.75)"; ctx.lineWidth = 1;
      ctx.beginPath();
      for (let i = 0; i < pts.length; i++){
        const x = this.navT2X(pts[i][0]);
        const y = h - 4 - ((pts[i][1] - vmin) / span) * (h - 8);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }
    const sel = this.navDrag
      ? [Math.min(this.navDrag.x0, this.navDrag.x1),
         Math.max(this.navDrag.x0, this.navDrag.x1)]
      : (this.win ? [this.navT2X(this.win[0]), this.navT2X(this.win[1])] : null);
    if (sel){
      ctx.fillStyle = "rgba(0,0,0,.62)";
      ctx.fillRect(0, 0, sel[0], h); ctx.fillRect(sel[1], 0, w - sel[1], h);
      ctx.strokeStyle = "#ffa500"; ctx.lineWidth = 1;
      ctx.strokeRect(sel[0] + .5, .5, Math.max(sel[1] - sel[0] - 1, 1), h - 1);
    }
    ctx.fillStyle = "#555"; ctx.font = "9px Consolas,monospace";
    ctx.fillText(dstr(this.full[0]), 3, h - 3);
    const right = dstr(this.full[1]);
    ctx.fillText(right, w - ctx.measureText(right).width - 3, h - 3);
  }
  windowRows(){
    const rows = [];
    for (const k of this.sel){
      const d = CONFIG.datasets[k];
      const pts = pointsOf(k);
      for (let i = 0; i < pts.length; i++){
        const t = pts[i][0];
        if (this.win && (t < this.win[0] || t > this.win[1])) continue;
        const prev = i > 0 ? pts[i - 1][1] : null;
        const cur = pts[i][1];
        rows.push({date: dstr(t), t, series: d.label, value: cur,
          change: prev === null ? null : +((cur - prev).toFixed(2)),
          pct: (prev === null || prev === 0) ? null
               : +(((cur - prev) / Math.abs(prev)) * 100).toFixed(2)});
      }
    }
    return rows;
  }
  filtered(){
    let rows = this.windowRows();
    const f = this.filter.trim().toLowerCase();
    if (f) rows = rows.filter(r =>
      r.series.toLowerCase().includes(f) || r.date.includes(f));
    const {key, dir} = this.sort;
    rows.sort((a, b) => (a[key] < b[key] ? -1 : a[key] > b[key] ? 1 : 0) * dir);
    return rows;
  }
  renderTable(){
    const rows = this.filtered();
    this.lastRows = rows;
    const show = rows.slice(0, 500);
    this.table.querySelector("tbody").innerHTML = show.map(r => `
      <tr><td>${r.date}</td><td class="txt">${r.series}</td><td>${fmt(r.value)}</td>
      <td class="${cls(r.change)}">${sign(r.change)}</td>
      <td class="${cls(r.pct)}">${sign(r.pct)}</td></tr>`).join("");
    this.note.textContent =
      `${rows.length.toLocaleString()} rows` +
      (rows.length > 500 ? " · 최신 500행 표시" : "") +
      (this.win ? ` · ${dstr(this.win[0])} ~ ${dstr(this.win[1])}` : " · 전체 기간");
    this.table.querySelectorAll("th").forEach(th => {
      th.classList.toggle("sorted", th.dataset.key === this.sort.key);
      th.textContent = th.textContent.replace(/ [▲▼]$/, "") +
        (th.dataset.key === this.sort.key ? (this.sort.dir > 0 ? " ▲" : " ▼") : "");
    });
  }
  download(kind){
    const rows = this.filtered();
    const head = ["Date", "Series", "Value", "Change", "Change Rate(%)"];
    const aoa = [head, ...rows.map(r =>
      [r.date, r.series, r.value, r.change, r.pct])];
    const tag = `${CONFIG.id}_${this.sel.join("+")}`.replace(/[^A-Za-z0-9가-힣+_]/g, "_");
    exportRows(kind, aoa, tag, "DETAIL");
  }
}

function exportRows(kind, aoa, tag, sheet){
  if (kind === "csv"){
    const csv = "\\uFEFF" + aoa.map(line => line.map(v =>
      v === null || v === undefined ? "" : String(v)).join(",")).join("\\r\\n");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([csv], {type: "text/csv"}));
    a.download = `${tag}.csv`; a.click(); URL.revokeObjectURL(a.href);
  } else {
    const ws = XLSX.utils.aoa_to_sheet(aoa);
    const wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, ws, sheet);
    XLSX.writeFile(wb, `${tag}.xlsx`);
  }
}

function buildSection(sec){
  const box = document.createElement("div");
  box.className = "section";
  if (sec.type === "static"){
    box.innerHTML = `
      <div class="tbar"><span class="ptitle">${sec.title}</span>
        <span class="note">${sec.rows.length} rows</span>
        <span class="spacer"></span>
        <button class="btn" data-a="csv">CSV ↓</button>
        <button class="btn" data-a="xlsx">XLSX ↓</button></div>
      <div class="tblbox auto"><table><thead><tr>${sec.cols.map((c, i) =>
        `<th class="${i === 0 ? "txt" : ""}">${c}</th>`).join("")}</tr></thead>
        <tbody>${sec.rows.map(r => `<tr>${r.map((v, i) =>
          `<td class="${i === 0 ? "txt" : ""}">${v === null ? "-" : fmt(v)}</td>`).join("")}</tr>`).join("")}
        </tbody></table></div>`;
    box.querySelector("[data-a=csv]").onclick = () =>
      exportRows("csv", [sec.cols, ...sec.rows], sec.tag, "DATA");
    box.querySelector("[data-a=xlsx]").onclick = () =>
      exportRows("xlsx", [sec.cols, ...sec.rows], sec.tag, "DATA");
  }
  if (sec.type === "rank"){
    const dates = sec.dates;
    box.innerHTML = `
      <div class="tbar"><span class="ptitle">${sec.title}</span>
        <select>${dates.map(d => `<option>${d}</option>`).join("")}</select>
        <input placeholder="filter: operator">
        <span class="note"></span>
        <span class="spacer"></span>
        <button class="btn" data-a="csv">CSV ↓</button>
        <button class="btn" data-a="xlsx">XLSX ↓</button></div>
      <div class="tblbox"><table><thead><tr>${sec.cols.map((c, i) =>
        `<th class="${i <= 1 ? "txt" : ""}" data-i="${i}">${c}</th>`).join("")}</tr></thead>
        <tbody></tbody></table></div>`;
    const sel = box.querySelector("select");
    const inp = box.querySelector("input");
    const note = box.querySelector(".note");
    const tbody = box.querySelector("tbody");
    let sortI = 0, sortDir = 1, filter = "";
    const render = () => {
      let rows = (sec.rows[sel.value] || []).slice();
      if (filter) rows = rows.filter(r =>
        String(r[1]).toLowerCase().includes(filter));
      rows.sort((a, b) => (a[sortI] < b[sortI] ? -1 : a[sortI] > b[sortI] ? 1 : 0) * sortDir);
      tbody.innerHTML = rows.map(r => `<tr>${sec.cols.map((c, i) => {
        const v = r[i];
        const flag = (i === (sec.nameCol === undefined ? 1 : sec.nameCol) &&
          sec.flagFrom !== undefined && r[sec.flagFrom])
          ? flagImg(r[sec.flagFrom]) : "";
        return `<td class="${i <= 1 ? "txt" : ""}">${flag}${
          v === null || v === undefined ? "-" : fmt(v)}</td>`;
      }).join("")}</tr>`).join("");
      note.textContent = `${rows.length} rows · ${sel.value}`;
    };
    sel.onchange = render;
    inp.oninput = e => { filter = e.target.value.toLowerCase(); render(); };
    box.querySelectorAll("th").forEach(th => th.onclick = () => {
      const i = Number(th.dataset.i);
      if (sortI === i) sortDir = -sortDir; else { sortI = i; sortDir = i <= 1 ? 1 : -1; }
      render(); });
    const aoa = () => [sec.cols, ...(sec.rows[sel.value] || [])
      .map(r => r.slice(0, sec.cols.length))];
    box.querySelector("[data-a=csv]").onclick = () =>
      exportRows("csv", aoa(), sec.tag + "_" + sel.value, "RANK");
    box.querySelector("[data-a=xlsx]").onclick = () =>
      exportRows("xlsx", aoa(), sec.tag + "_" + sel.value, "RANK");
    render();
  }
  document.getElementById("sections").appendChild(box);
}

document.getElementById("code").textContent = CONFIG.id.toUpperCase();
document.getElementById("title").textContent = CONFIG.title;
document.getElementById("ref").textContent = "REF " + (CONFIG.updated || "-");
document.getElementById("gen").innerHTML =
  "<b>GENERATED</b> " + (CONFIG.generated || "-");
if (window.ChartZoom) Chart.register(window.ChartZoom);
CONFIG.panels.forEach(cfg => { PANELS[cfg.id] = new Panel(cfg); });
(CONFIG.sections || []).forEach(buildSection);
window.addEventListener("resize", () =>
  Object.values(PANELS).forEach(p => p.drawNav()));
</script>
</body>
</html>
"""


def _detail_write(module_id: str, config: dict) -> None:
    config["generated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    shell = (DETAIL_SHELL
             .replace("__CONFIG__", json.dumps(config, ensure_ascii=False,
                                               separators=(",", ":")))
             .replace("__PAGE_TITLE__", f"{module_id.upper()} DETAIL"))
    page_path = ROOT / module_id / "detail.html"
    temp = page_path.with_suffix(".html.tmp")
    temp.write_text(shell, encoding="utf-8")
    temp.replace(page_path)
    log(f"{module_id}/detail.html 작성 완료 ({page_path.stat().st_size:,} bytes)")


def _clean(v):
    """NaN/NULL/빈문자열 → None 정규화 (sqlite REAL NaN 및 pandas NaN 대비)."""
    if v is None:
        return None
    if isinstance(v, float) and v != v:  # NaN
        return None
    if isinstance(v, str) and v.strip().lower() in ("", "nan", "none", "null"):
        return None
    return v


def _pairs(rows, date_key, val_key, dates=None):
    out = []
    for i, r in enumerate(rows):
        v = r[val_key]
        if v is None:
            continue
        d = dates[i] if dates is not None else r[date_key]
        out.append([d, r2(v)])
    return out


def _detail_updated(datasets: dict) -> str:
    dates = [p[0] for d in datasets.values() for p in d["points"] if p[0]]
    return max(dates) if dates else "-"


def build_detail_linerlytica(conn) -> None:
    rows = conn.execute(
        "SELECT snapshot_date, ship_status, ships, teu FROM fleet_snapshot "
        "ORDER BY snapshot_date"
    ).fetchall()
    if not rows:
        return
    by = {}
    for r in rows:
        by.setdefault(r["ship_status"], []).append(r)
    statuses = sorted(by)
    total_status = next((s for s in statuses if "total" in s.lower()), None)
    datasets = {}
    for st in statuses:
        datasets[f"teu|{st}"] = {"label": f"{st} (TEU)",
                                 "points": _pairs(by[st], "snapshot_date", "teu")}
        datasets[f"ships|{st}"] = {"label": f"{st} (척)",
                                   "points": _pairs(by[st], "snapshot_date", "ships")}
    if total_status is None:
        sums = {}
        for r in rows:
            e = sums.setdefault(r["snapshot_date"], [0, 0])
            e[0] += r["ships"] or 0
            e[1] += r["teu"] or 0
        datasets["teu|TOTAL"] = {"label": "선대 합계 (TEU)",
                                 "points": [[d, sums[d][1]] for d in sorted(sums)]}
        datasets["ships|TOTAL"] = {"label": "선대 합계 (척)",
                                   "points": [[d, sums[d][0]] for d in sorted(sums)]}
        total_status = "TOTAL"
    others = [s for s in statuses if s != total_status]
    teu_keys = [f"teu|{total_status}"] + [f"teu|{s}" for s in others]
    ship_keys = [f"ships|{total_status}"] + [f"ships|{s}" for s in others]
    _detail_write("linerlytica", {
        "id": "linerlytica", "title": "컨테이너 선대 — Linerlytica (월간 17일)",
        "updated": _detail_updated(datasets), "datasets": datasets,
        "panels": [
            {"id": "teu", "title": "선대 TEU — 상태별 중첩", "keys": teu_keys,
             "initial": [f"teu|{total_status}"], "navKey": f"teu|{total_status}"},
            {"id": "ships", "title": "선대 선박수 — 상태별 중첩", "keys": ship_keys,
             "initial": [f"ships|{total_status}"], "navKey": f"ships|{total_status}"},
        ],
    })


ALPHALINER_SHELL = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ALPHALINER TOP 100 — DETAIL</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/hammerjs@2.0.8/hammer.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js"></script>
<style>
  :root{--bg:#000;--panel:#0b0b0b;--panel2:#101010;--line:#2b2b2b;--amber:#ffa500;
        --txt:#e6e6e6;--dim:#8f8f8f;--up:#ff4d4d;--down:#58a6ff;--flat:#9a9a9a;
        --mono:Consolas,"Liberation Mono",Menlo,"Malgun Gothic",monospace}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);font-family:var(--mono)}
  .topbar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:8px 12px;
          border-bottom:2px solid var(--amber);
          background:linear-gradient(180deg,#151000,#000)}
  .code{color:#000;background:var(--amber);font-weight:700;font-size:11px;
        padding:1px 6px;letter-spacing:.08em}
  .ttl{color:var(--amber);font-size:13px;letter-spacing:.08em}
  .ref{color:var(--dim);font-size:10px}
  .btn{color:var(--amber);border:1px solid var(--amber);background:none;
       font-family:var(--mono);font-size:10px;padding:2px 9px;cursor:pointer;
       letter-spacing:.06em}
  .btn:hover{background:var(--amber);color:#000}
  .back{margin-left:auto;color:var(--amber);border:1px solid var(--amber);
        font-size:10px;padding:2px 8px;text-decoration:none;letter-spacing:.06em}
  .back:hover{background:var(--amber);color:#000}
  .wrap{padding:8px;display:flex;flex-direction:column;gap:8px}
  /* 공식 페이지 Global figures 밴드 */
  .band{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
  @media(max-width:980px){.band{grid-template-columns:repeat(2,1fr)}}
  .bcard{background:var(--panel);border:1px solid var(--line);padding:8px 10px;
         display:flex;flex-direction:column;gap:3px}
  .blabel{color:var(--dim);font-size:10px;letter-spacing:.1em}
  .bval{color:#fff;font-size:22px;font-weight:700;font-variant-numeric:tabular-nums}
  .bval em{font-style:normal;font-size:10px;color:var(--dim);margin-left:5px}
  .chg{font-size:11px} .chg.up{color:var(--up)} .chg.down{color:var(--down)}
  .chg.flat{color:var(--flat)}
  .charts{display:grid;grid-template-columns:1fr 1fr;gap:8px}
  @media(max-width:980px){.charts{grid-template-columns:1fr}}
  .panel{background:var(--panel);border:1px solid var(--line);padding:8px;
         display:flex;flex-direction:column;gap:6px;min-width:0}
  .panel.wide{grid-column:1/-1}
  @media(max-width:980px){.panel.wide{grid-column:auto}}
  .phead{display:flex;flex-wrap:wrap;align-items:center;gap:6px}
  .ptitle{color:var(--amber);font-size:12px;letter-spacing:.08em}
  .chips{display:flex;flex-wrap:wrap;gap:4px;align-content:flex-start}
  .chip{border:1px solid var(--line);color:var(--dim);font-size:10px;
        padding:1px 7px;cursor:pointer;user-select:none}
  .chip.on{color:#000;background:var(--amber);border-color:var(--amber);font-weight:700}
  .spacer{margin-left:auto}
  .chartbox{height:240px;position:relative}
  .navbox{height:40px;position:relative;border:1px solid var(--line);
          background:#0d0d0d;cursor:crosshair}
  .navbox canvas{width:100%;height:100%;display:block}
  .hint{color:#555;font-size:9px;letter-spacing:.05em}
  .tbar{display:flex;flex-wrap:wrap;align-items:center;gap:6px}
  .tbar input,.tbar select{background:var(--panel2);border:1px solid var(--line);
      color:var(--txt);font-family:var(--mono);font-size:10px;padding:2px 6px}
  .tbar input{width:150px}
  .note{color:var(--dim);font-size:10px}
  .tblbox{height:430px;overflow:auto;border:1px solid var(--line);background:var(--panel2)}
  table{width:100%;border-collapse:collapse;font-size:11px;
        font-variant-numeric:tabular-nums}
  th{color:var(--dim);text-align:right;font-weight:400;border-bottom:1px solid var(--line);
     padding:3px 6px;position:sticky;top:0;background:var(--panel2);cursor:pointer;
     white-space:nowrap;z-index:2}
  th.txt,td.txt{text-align:left}
  th:hover{color:var(--amber)} th.sorted{color:var(--amber)}
  td{border-bottom:1px dotted #1c1c1c;padding:3px 6px;text-align:right;white-space:nowrap}
  td:first-child{color:var(--dim)}
  .up{color:var(--up)} .down{color:var(--down)} .flat{color:var(--flat)}
  .barcell{width:170px}
  .barwrap{display:flex;height:10px;background:#141414;border:1px solid #222}
  .bar-ex{background:#58a6ff} .bar-ob{background:#9ecbff}
  .legend{display:flex;gap:12px;align-items:center;color:var(--dim);font-size:10px}
  .sw{display:inline-block;width:10px;height:10px;margin-right:4px;vertical-align:-1px}
  footer{color:var(--dim);font-size:10px;letter-spacing:.06em;padding:8px 12px 20px;
         display:flex;flex-wrap:wrap;gap:12px}
  footer b{color:var(--amber);font-weight:400}
</style>
</head>
<body>
<div class="topbar">
  <span class="code">ALPHALINER</span>
  <span class="ttl">TOP 100 — FLEET &amp; ORDERBOOK TERMINAL</span>
  <span class="ref" id="ref"></span>
  <a class="back" href="../index.html">← TERMINAL</a>
</div>
<div class="wrap">
  <div class="band" id="band"></div>
  <div class="charts" id="charts"></div>
  <div class="panel" id="rankpanel">
    <div class="tbar">
      <span class="ptitle">TOP 100 OPERATORS — Existing / Owned / Chartered / Orderbook</span>
      <select id="snapdate"></select>
      <input id="rkfilter" placeholder="filter: operator">
      <span class="note" id="rknote"></span>
      <span class="legend"><span><span class="sw" style="background:#58a6ff"></span>Existing fleet</span>
        <span><span class="sw" style="background:#9ecbff"></span>Orderbook</span></span>
      <span class="spacer"></span>
      <button class="btn" id="dlcsv">CSV ↓</button>
      <button class="btn" id="dlxlsx">XLSX ↓</button>
    </div>
    <div class="tblbox"><table id="rktable">
      <thead><tr id="rkhead"></tr></thead><tbody id="rkbody"></tbody>
    </table></div>
  </div>
</div>
<footer>
  <span><b>SOURCE</b> Alphaliner Public Top100 (axsmarine) · 매일 00:00 KST 크롤 누적</span>
  <span id="gen"></span>
</footer>
<script>
const PALETTE = ["#ffa500", "#79c0ff", "#d2a8ff", "#7ee787", "#ff7b72", "#ffa657",
                 "#a5d6ff", "#f778ba", "#d4a72c", "#8ddb8c", "#ff9bce", "#9ecbff"];
const CFG = __CONFIG__;
let WIN = null, SYNC = false;
const CHARTS = [];

function fmt(v){ return v === null || v === undefined ? "-" :
  Number(v).toLocaleString("ko-KR", {maximumFractionDigits: 2}); }
function sign(v){ if (v === null || v === undefined) return "-";
  return (v > 0 ? "+" : "") + fmt(v); }
function cls(v){ return v === null || v === undefined ? "flat" :
  v > 0 ? "up" : v < 0 ? "down" : "flat"; }
function ms(d){ return Date.parse(d + "T00:00:00Z"); }
function dstr(t){ return new Date(t).toISOString().slice(0, 10); }
function flagImg(cc){
  const c = String(cc || "").toUpperCase().slice(0, 2);
  if (!/^[A-Z]{2}$/.test(c)) return "";
  const cps = [...c].map(ch => (0x1F1E6 + ch.charCodeAt(0) - 65).toString(16)).join("-");
  return `<img src="https://cdn.jsdelivr.net/gh/twitter/twemoji@14.0.2/assets/svg/${cps}.svg"
    alt="${c}" style="width:16px;height:12px;vertical-align:-2px;margin-right:6px;
    border:1px solid #333;background:#111">`;
}
function pts(series){ return series.map(p => [ms(p[0]), p[1]]); }

/* ---------- Global figures 밴드 ---------- */
function bandCard(label, series, unit){
  const p = pts(series);
  const last = p[p.length - 1], prev = p[p.length - 2];
  const ch = prev ? last[1] - prev[1] : null;
  const pc = prev && prev[1] ? ch / Math.abs(prev[1]) * 100 : null;
  const arrow = ch === null ? "" : ch > 0 ? "↗" : ch < 0 ? "↘" : "─";
  return `<div class="bcard"><span class="blabel">${label}</span>
    <span class="bval">${fmt(last[1])}<em>${unit} · ${dstr(last[0])}</em></span>
    <span class="chg ${cls(ch)}">${arrow} (${sign(ch)}, ${
      pc === null ? "-" : (pc > 0 ? "+" : "") + pc.toFixed(1)}%) vs ${
      prev ? dstr(prev[0]) : "-"}</span></div>`;
}
document.getElementById("band").innerHTML =
  bandCard("ACTIVE SHIPS (GLOBAL)", CFG.series.ships, "척") +
  bandCard("TOP100 FLEET", CFG.series.teu, "TEU") +
  bandCard("ORDERBOOK", CFG.series.ob, "TEU") +
  bandCard("TOP10 SHARE", CFG.series.share, "%");
document.getElementById("ref").textContent = "REF " + (CFG.updated || "-");
document.getElementById("gen").innerHTML =
  "<b>GENERATED</b> " + (CFG.generated || "-");
if (window.ChartZoom) Chart.register(window.ChartZoom);

/* ---------- 차트 + 기간 브러시 (윈도우 공유) ---------- */
function makeChart(cfg){
  const wrap = document.createElement("div");
  wrap.className = "panel" + (cfg.wide ? " wide" : "");
  wrap.innerHTML = `
    <div class="phead"><span class="ptitle">${cfg.title}</span>
      <span class="chips"></span><span class="spacer"></span>
      <button class="btn">FULL</button></div>
    <div class="chartbox"><canvas></canvas></div>
    <div class="navbox"><canvas></canvas></div>
    <div class="hint">하단 스트립 드래그=기간 선택(전 차트 동기화) · WHEEL=ZOOM · ALT+DRAG=PAN · 더블클릭=전체</div>`;
  document.getElementById("charts").appendChild(wrap);
  const api = {cfg, sel: (cfg.initial || cfg.keys || []).slice(), wrap,
    chipsEl: wrap.querySelector(".chips"),
    canvas: wrap.querySelectorAll("canvas")[0],
    nav: wrap.querySelectorAll("canvas")[1], navDrag: null, full: null};
  if (cfg.keys){
    const build = () => {
      api.chipsEl.innerHTML = cfg.keys.map((k, i) =>
        `<span class="chip ${api.sel.includes(k) ? "on" : ""}" data-k="${k}"
          style="${api.sel.includes(k) ? "" :
            `border-color:${PALETTE[i % PALETTE.length]};color:${PALETTE[i % PALETTE.length]}`}">
          ${k}</span>`).join("");
      api.chipsEl.querySelectorAll(".chip").forEach(ch => ch.onclick = () => {
        const k = ch.dataset.k;
        if (api.sel.includes(k)){
          if (api.sel.length > 1) api.sel = api.sel.filter(x => x !== k);
        } else api.sel.push(k);
        build(); api.chart.data.datasets = datasetsOf(api);
        api.chart.update("none"); });
    };
    build();
  }
  function datasetsOf(a){
    if (a.cfg.fixed){
      return a.cfg.fixed.map(f => ({
        label: f.label, yAxisID: f.axis || "y",
        data: pts(CFG.series[f.key]).map(p => ({x: p[0], y: p[1]})),
        borderColor: f.color, backgroundColor: "transparent", borderWidth: 1.5,
        pointRadius: 0, pointHoverRadius: 3, tension: .15 }));
    }
    const src = a.cfg.source;
    return a.sel.map((k, i) => ({
      label: k, data: pts(CFG.operators[src][k] || []).map(p => ({x: p[0], y: p[1]})),
      borderColor: PALETTE[CFG.operators.names.indexOf(k) % PALETTE.length],
      backgroundColor: "transparent", borderWidth: 1.4, pointRadius: 0,
      pointHoverRadius: 3, tension: .15 }));
  }
  api.datasetsOf = datasetsOf;
  api.chart = new Chart(api.canvas, {
    type: "line", data: {datasets: datasetsOf(api)},
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: {mode: "nearest", intersect: false},
      plugins: {
        legend: {display: !!cfg.keys, position: "top",
                 labels: {color: "#8f8f8f", boxWidth: 8, font: {size: 9}}},
        tooltip: {displayColors: false, backgroundColor: "#111",
          borderColor: "#3a3a3a", borderWidth: 1, titleColor: "#ffa500",
          bodyColor: "#e6e6e6",
          callbacks: {title: it => dstr(it[0].parsed.x),
                      label: it => ` ${it.dataset.label} ${fmt(it.parsed.y)}`}},
        zoom: {zoom: {wheel: {enabled: true}, pinch: {enabled: true},
            drag: {enabled: true, backgroundColor: "rgba(255,165,0,.12)",
                   borderColor: "#ffa500", borderWidth: 1}, mode: "x",
            onZoomComplete: c => onWin(c.chart)},
          pan: {enabled: true, mode: "x", modifierKey: "Alt",
                onPanComplete: c => onWin(c.chart)},
          limits: {x: {min: "original", max: "original"}}},
      },
      scales: {
        x: {type: "linear", ticks: {color: "#777", maxTicksLimit: 7,
            callback: v => dstr(v), font: {size: 9, family: "Consolas"}},
            grid: {color: "#161616"}},
        y: {position: "left", ticks: {color: "#777",
            font: {size: 9, family: "Consolas"}}, grid: {color: "#161616"}},
        y1: {position: "right", display: !!cfg.dual,
             ticks: {color: "#79c0ff", font: {size: 9, family: "Consolas"}},
             grid: {drawOnChartArea: false}},
      },
    },
  });
  api.full = [api.chart.scales.x.min, api.chart.scales.x.max];
  wrap.querySelector("button").onclick = () => resetWin();
  api.canvas.ondblclick = () => resetWin();
  api.nav.addEventListener("pointerdown", e => {
    api.nav.setPointerCapture(e.pointerId);
    api.navDrag = {x0: e.offsetX, x1: e.offsetX}; });
  api.nav.addEventListener("pointermove", e => {
    if (api.navDrag){ api.navDrag.x1 = e.offsetX; drawNav(api); } });
  api.nav.addEventListener("pointerup", e => {
    if (!api.navDrag) return;
    const {x0, x1} = api.navDrag; api.navDrag = null;
    if (Math.abs(x1 - x0) < 5){ drawNav(api); return; }
    applyWin(api.navX2T(Math.min(x0, x1)), api.navX2T(Math.max(x0, x1))); });
  api.navX2T = x => api.full[0] + (x / (api.nav.clientWidth || 1)) *
    (api.full[1] - api.full[0]);
  api.navT2X = t => ((t - api.full[0]) / (api.full[1] - api.full[0])) *
    (api.nav.clientWidth || 1);
  CHARTS.push(api);
  return api;
}
function navSeries(api){
  if (api.cfg.navFixed) return pts(CFG.series[api.cfg.navFixed]);
  const k = api.sel[0];
  return pts(CFG.operators[api.cfg.source][k] || []);
}
function drawNav(api){
  const cv = api.nav, ctx = cv.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  if (!w || !h) return;
  cv.width = w * dpr; cv.height = h * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "#0d0d0d"; ctx.fillRect(0, 0, w, h);
  const p = navSeries(api);
  if (p.length){
    let vmin = Infinity, vmax = -Infinity;
    for (const q of p){ if (q[1] < vmin) vmin = q[1]; if (q[1] > vmax) vmax = q[1]; }
    const span = (vmax - vmin) || 1;
    ctx.strokeStyle = "rgba(255,165,0,.75)"; ctx.lineWidth = 1;
    ctx.beginPath();
    for (let i = 0; i < p.length; i++){
      const x = api.navT2X(p[i][0]);
      const y = h - 4 - ((p[i][1] - vmin) / span) * (h - 8);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
  }
  const sel = api.navDrag ? [Math.min(api.navDrag.x0, api.navDrag.x1),
      Math.max(api.navDrag.x0, api.navDrag.x1)]
    : (WIN ? [api.navT2X(WIN[0]), api.navT2X(WIN[1])] : null);
  if (sel){
    ctx.fillStyle = "rgba(0,0,0,.62)";
    ctx.fillRect(0, 0, sel[0], h); ctx.fillRect(sel[1], 0, w - sel[1], h);
    ctx.strokeStyle = "#ffa500"; ctx.lineWidth = 1;
    ctx.strokeRect(sel[0] + .5, .5, Math.max(sel[1] - sel[0] - 1, 1), h - 1);
  }
  ctx.fillStyle = "#555"; ctx.font = "9px Consolas,monospace";
  ctx.fillText(dstr(api.full[0]), 3, h - 3);
  const right = dstr(api.full[1]);
  ctx.fillText(right, w - ctx.measureText(right).width - 3, h - 3);
}
function onWin(chart){
  if (SYNC) return;
  const x = chart.scales.x, DAY = 86400000;
  const base = CHARTS[0].full;
  WIN = (x.min <= base[0] + DAY && x.max >= base[1] - DAY) ? null : [x.min, x.max];
  SYNC = true;
  CHARTS.forEach(a => {
    if (a.chart !== chart && WIN)
      a.chart.zoomScale("x", {min: WIN[0], max: WIN[1]}, "none");
    if (a.chart !== chart && !WIN) a.chart.resetZoom();
    drawNav(a); });
  SYNC = false;
}
function applyWin(t0, t1){
  WIN = [t0, t1];
  SYNC = true;
  CHARTS.forEach(a => { a.chart.zoomScale("x", {min: t0, max: t1}, "none");
    drawNav(a); });
  SYNC = false;
}
function resetWin(){
  WIN = null;
  SYNC = true;
  CHARTS.forEach(a => { a.chart.resetZoom(); drawNav(a); });
  SYNC = false;
}

makeChart({title: "GLOBAL FLEET — Active Ships & Top100 TEU", dual: true,
  navFixed: "teu",
  fixed: [{key: "ships", label: "Active Ships (global)", color: "#ffa500"},
          {key: "teu", label: "Top100 Fleet (TEU)", color: "#79c0ff", axis: "y1"}]});
makeChart({title: "ORDERBOOK & TOP10 SHARE", dual: true, navFixed: "ob",
  fixed: [{key: "ob", label: "Orderbook (TEU)", color: "#d2a8ff"},
          {key: "share", label: "Top10 Share (%)", color: "#7ee787", axis: "y1"}]});
makeChart({title: "선사별 선대 (TEU) 추이 — 선대 확보 누적", wide: true,
  source: "teu", keys: CFG.operators.names,
  initial: CFG.operators.names.slice(0, 4)});
makeChart({title: "선사별 Orderbook (TEU) 추이 — 발주 누적", wide: true,
  source: "ob", keys: CFG.operators.names,
  initial: CFG.operators.names.slice(0, 4)});
CHARTS.forEach(drawNav);

/* ---------- 공식 스타일 순위표 ---------- */
const SNAP = CFG.snap;
const sel = document.getElementById("snapdate");
sel.innerHTML = SNAP.dates.map(d => `<option>${d}</option>`).join("");
const head = document.getElementById("rkhead");
head.innerHTML = SNAP.cols.map((c, i) =>
  `<th class="${c.txt ? "txt" : ""}" data-i="${i}">${c.t}</th>`).join("");
let sortI = 0, sortDir = 1, rkFilter = "";
function barHtml(r){
  const ex = (r[SNAP.bar.owned] || 0) + (r[SNAP.bar.chartered] || 0);
  const ob = r[SNAP.bar.orderbook] || 0;
  const max = SNAP.barMax || 1;
  return `<div class="barwrap" title="Existing ${fmt(ex)} + Orderbook ${fmt(ob)}">
    <span class="bar-ex" style="width:${(ex / max * 100).toFixed(1)}%"></span>
    <span class="bar-ob" style="width:${(ob / max * 100).toFixed(1)}%"></span></div>`;
}
function renderRank(){
  let rows = (SNAP.rows[sel.value] || []).slice();
  if (rkFilter) rows = rows.filter(r =>
    String(r[1] || "").toLowerCase().includes(rkFilter));
  const ci = SNAP.cols[sortI].i;
  rows.sort((a, b) => {
    const va = ci < 0 ? 0 : a[ci], vb = ci < 0 ? 0 : b[ci];
    return (va < vb ? -1 : va > vb ? 1 : 0) * sortDir; });
  document.getElementById("rkbody").innerHTML = rows.map(r =>
    `<tr>${SNAP.cols.map(c => c.i < 0 ? `<td class="barcell">${barHtml(r)}</td>` :
      `<td class="${c.txt ? "txt" : ""}">${c.flag ? flagImg(r[c.flag]) : ""}${
        r[c.i] === null || r[c.i] === undefined ? "-" :
        (c.pct ? fmt(r[c.i]) + "%" : fmt(r[c.i]))}</td>`).join("")}</tr>`).join("");
  document.getElementById("rknote").textContent =
    `${rows.length} rows · ${sel.value}`;
  head.querySelectorAll("th").forEach((th, i) => {
    th.classList.toggle("sorted", i === sortI);
    th.textContent = th.textContent.replace(/ [▲▼]$/, "") +
      (i === sortI ? (sortDir > 0 ? " ▲" : " ▼") : ""); });
}
sel.onchange = renderRank;
document.getElementById("rkfilter").oninput = e => {
  rkFilter = e.target.value.toLowerCase(); renderRank(); };
head.querySelectorAll("th").forEach((th, i) => th.onclick = () => {
  if (sortI === i) sortDir = -sortDir; else { sortI = i; sortDir = i <= 1 ? 1 : -1; }
  renderRank(); });
function rankAoa(){
  const rows = SNAP.rows[sel.value] || [];
  return [SNAP.cols.filter(c => c.i >= 0).map(c => c.t),
    ...rows.map(r => SNAP.cols.filter(c => c.i >= 0).map(c => r[c.i]))];
}
document.getElementById("dlcsv").onclick = () => {
  const aoa = rankAoa();
  const csv = "\\uFEFF" + aoa.map(l => l.map(v =>
    v === null || v === undefined ? "" : String(v)).join(",")).join("\\r\\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], {type: "text/csv"}));
  a.download = `alphaliner_top100_${sel.value}.csv`; a.click();
  URL.revokeObjectURL(a.href); };
document.getElementById("dlxlsx").onclick = () => {
  const ws = XLSX.utils.aoa_to_sheet(rankAoa());
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, ws, "TOP100");
  XLSX.writeFile(wb, `alphaliner_top100_${sel.value}.xlsx`); };
renderRank();
window.addEventListener("resize", () => CHARTS.forEach(drawNav));
</script>
</body>
</html>
"""


def build_detail_alphaliner(conn) -> None:
    """공식 PublicTop100 스타일: 글로벌 밴드 + 시계열 + Existing/Orderbook 바 순위표."""
    glob = {}
    for r in conn.execute(
        "SELECT data_date AS d, global_ships AS g, total_ships AS s, "
        "       total_teu AS teu FROM alphaliner_master"
    ):
        glob.setdefault(r["d"], {}).update(
            ships=_clean(r["g"]) or _clean(r["s"]), teu=_clean(r["teu"]))
    for r in conn.execute(
        "SELECT snapshot_date AS d, global_ships AS g, top100_teu AS teu, "
        "       top10_share AS sh, orderbook_teu AS ob FROM snapshot_meta"
    ):
        glob.setdefault(r["d"], {}).update(
            ships=_clean(r["g"]), teu=_clean(r["teu"]),
            share=_clean(r["sh"]), ob=_clean(r["ob"]))
    if not glob:
        return
    dates = sorted(glob)
    series = {k: [[d, glob[d][k]] for d in dates if glob[d].get(k) is not None]
              for k in ("ships", "teu", "share", "ob")}

    ops = {}

    def _add(d, name, teu, ob, ow, ch):
        name = _clean(name)
        if not name:
            return
        e = ops.setdefault(name, {"teu": {}, "ob": {}, "ow": {}, "ch": {}})
        for key, val in (("teu", teu), ("ob", ob), ("ow", ow), ("ch", ch)):
            v = _clean(val)
            if v is not None:
                e[key][d] = v

    for r in conn.execute(
        "SELECT m.data_date AS d, d.operator_name AS nm, d.total_teu AS teu, "
        "       d.orderbook_teu AS ob, d.existing_owned_teu AS ow, "
        "       d.existing_chartered_teu AS ch FROM alphaliner_detail d "
        "JOIN alphaliner_master m ON m.snapshot_id = d.snapshot_id"
    ):
        _add(r["d"], r["nm"], r["teu"], r["ob"], r["ow"], r["ch"])
    for r in conn.execute(
        "SELECT snapshot_date AS d, COALESCE(operator_name, operator) AS nm, "
        "       total_teu AS teu, orderbook_teu AS ob, owned_teu AS ow, "
        "       chartered_teu AS ch FROM operator_snapshots"
    ):
        _add(r["d"], r["nm"], r["teu"], r["ob"], r["ow"], r["ch"])
    latest = dates[-1]
    names = [n for n, _ in sorted(
        ops.items(), key=lambda kv: (kv[1]["teu"].get(latest) or 0), reverse=True)[:12]]
    operators = {m: {n: [[d, ops[n][m][d]] for d in sorted(ops[n][m])]
                     for n in names} for m in ("teu", "ob")}

    rowsby = {}
    bar_max = 0

    def _row(d, rank, name, cc, teu, ships, ow_t, ow_s, ch_t, ch_s, ob_t, ob_s):
        teu, ow_t, ch_t, ob_t = (_clean(teu), _clean(ow_t), _clean(ch_t),
                                 _clean(ob_t))
        ch_pct = round(ch_t / teu * 100, 1) if teu and ch_t is not None else None
        existing = (ow_t or 0) + (ch_t or 0)
        ob_pct = round(ob_t / existing * 100, 1) if existing and ob_t else None
        rowsby.setdefault(d, []).append([
            _clean(rank), _clean(name), teu, _clean(ships), ow_t, _clean(ow_s),
            ch_t, _clean(ch_s), ch_pct, ob_t, _clean(ob_s), ob_pct, _clean(cc)])
        nonlocal bar_max
        bar_max = max(bar_max, existing + (ob_t or 0))

    for r in conn.execute(
        "SELECT m.data_date AS d, d.rank AS rk, d.operator_name AS nm, NULL AS cc, "
        "       d.total_teu AS teu, d.total_ships AS sh, d.existing_owned_teu AS owt, "
        "       d.existing_owned_ships AS ows, d.existing_chartered_teu AS cht, "
        "       d.existing_chartered_ships AS chs, d.orderbook_teu AS obt, "
        "       d.orderbook_ships AS obs FROM alphaliner_detail d "
        "JOIN alphaliner_master m ON m.snapshot_id = d.snapshot_id"
    ):
        _row(r["d"], r["rk"], r["nm"], r["cc"], r["teu"], r["sh"], r["owt"],
             r["ows"], r["cht"], r["chs"], r["obt"], r["obs"])
    for r in conn.execute(
        "SELECT snapshot_date AS d, rank AS rk, "
        "       COALESCE(operator_name, operator) AS nm, country AS cc, "
        "       total_teu AS teu, total_ships AS sh, owned_teu AS owt, "
        "       owned_ships AS ows, chartered_teu AS cht, chartered_ships AS chs, "
        "       orderbook_teu AS obt, orderbook_ships AS obs "
        "FROM operator_snapshots"
    ):
        _row(r["d"], r["rk"], r["nm"], r["cc"], r["teu"], r["sh"], r["owt"],
             r["ows"], r["cht"], r["chs"], r["obt"], r["obs"])
    for d in rowsby:
        rowsby[d].sort(key=lambda x: (x[0] is None, x[0]))

    shell = (ALPHALINER_SHELL.replace("__CONFIG__", json.dumps({
        "id": "alphaliner", "updated": latest,
        "series": series, "operators": {"names": names, **operators},
        "snap": {
            "dates": sorted(rowsby, reverse=True),
            "cols": [
                {"t": "RANK", "i": 0, "txt": True},
                {"t": "OPERATOR", "i": 1, "txt": True, "flag": 12},
                {"t": "FLEET BAR", "i": -1},
                {"t": "TOTAL TEU", "i": 2}, {"t": "SHIPS", "i": 3},
                {"t": "OWNED TEU", "i": 4}, {"t": "OWNED SHIPS", "i": 5},
                {"t": "CHARTERED TEU", "i": 6}, {"t": "CHARTERED SHIPS", "i": 7},
                {"t": "% CHART", "i": 8, "pct": True},
                {"t": "ORDERBOOK TEU", "i": 9}, {"t": "ORDERBOOK SHIPS", "i": 10},
                {"t": "% EXISTING", "i": 11, "pct": True},
            ],
            "bar": {"owned": 4, "chartered": 6, "orderbook": 9},
            "barMax": bar_max,
            "rows": rowsby,
        },
    }, ensure_ascii=False, separators=(",", ":"))))
    page_path = ROOT / "alphaliner" / "detail.html"
    temp = page_path.with_suffix(".html.tmp")
    temp.write_text(shell, encoding="utf-8")
    temp.replace(page_path)
    log(f"alphaliner/detail.html 작성 완료 ({page_path.stat().st_size:,} bytes)")


def build_detail_forwarders(conn) -> None:
    latest = conn.execute("SELECT MAX(date) FROM forwarders").fetchone()[0]
    if not latest:
        return
    providers = [r[0] for r in conn.execute(
        "SELECT provider FROM forwarders WHERE date = ? ORDER BY rank LIMIT 12",
        (latest,))]
    per = {}
    for r in conn.execute(
        "SELECT date, provider, revenue, teu, air_tons FROM forwarders ORDER BY date"
    ):
        per.setdefault(r["provider"], {})[r["date"]] = r
    datasets = {}
    for p in providers:
        pts = sorted(per.get(p, {}).items())
        datasets[f"rev|{p}"] = {"label": p, "points": _pairs(
            [v for _, v in pts], None, "revenue", dates=[d for d, _ in pts])}
        datasets[f"teu|{p}"] = {"label": p, "points": _pairs(
            [v for _, v in pts], None, "teu", dates=[d for d, _ in pts])}
        datasets[f"ton|{p}"] = {"label": p, "points": _pairs(
            [v for _, v in pts], None, "air_tons", dates=[d for d, _ in pts])}
    mk = lambda m, i: [f"{m}|{p}" for p in providers[:i]]
    _detail_write("forwarders", {
        "id": "forwarders", "title": "포워더 순위 — Armstrong & Associates",
        "updated": latest, "datasets": datasets,
        "panels": [
            {"id": "rev", "title": "매출 ($M) — 회사별 중첩", "keys": mk("rev", 12),
             "initial": mk("rev", 3), "navKey": f"rev|{providers[0]}"},
            {"id": "teu", "title": "해양 물동량 (TEU) — 회사별 중첩",
             "keys": mk("teu", 12), "initial": mk("teu", 3),
             "navKey": f"teu|{providers[0]}"},
            {"id": "ton", "title": "항공 물동량 (TON) — 회사별 중첩", "span": 2,
             "keys": mk("ton", 12), "initial": mk("ton", 3),
             "navKey": f"ton|{providers[0]}"},
        ],
    })


def build_detail_air(conn) -> None:
    master = conn.execute(
        "SELECT snapshot_id, data_date, overall_index FROM tac_master "
        "ORDER BY data_date"
    ).fetchall()
    if not master:
        return
    sid2date = {m["snapshot_id"]: m["data_date"] for m in master}
    datasets = {"overall": {"label": "TAC 종합", "points": _pairs(
        master, "data_date", "overall_index")}}
    routes = {}
    for r in conn.execute(
        "SELECT snapshot_id, origin, destination, index_value FROM tac_detail"
    ):
        name = f"{r['origin']}-{r['destination']}"
        d = sid2date.get(r["snapshot_id"])
        if d and r["index_value"] is not None:
            routes.setdefault(name, {})[d] = r["index_value"]
    for name, m in routes.items():
        datasets[name] = {"label": name,
                          "points": [[d, r2(m[d])] for d in sorted(m)]}
    route_keys = [k for k in datasets if k != "overall"]
    _detail_write("air", {
        "id": "air", "title": "항공운임 — TAC Index (항로별 중첩)",
        "updated": master[-1]["data_date"], "datasets": datasets,
        "panels": [{
            "id": "routes", "title": "TAC 항로 지수 — 중첩 비교", "span": 2,
            "keys": route_keys + ["overall"], "initial": route_keys,
            "navKey": route_keys[0] if route_keys else "overall",
        }],
    })


def build_detail_oil(conn) -> None:
    prices = conn.execute(
        "SELECT DATE, Dubai, Brent, WTI FROM oil_prices ORDER BY DATE"
    ).fetchall()
    datasets = {}
    for column in ("Dubai", "Brent", "WTI"):
        datasets[column] = {"label": column, "points": _pairs(prices, "DATE", column)}
    for port in ("Rotterdam", "Singapore", "Fujairah"):
        rows = conn.execute(
            "SELECT date_str, price FROM product_vlsfo WHERE port = ? "
            "ORDER BY date_str", (port,)).fetchall()
        datasets[port] = {"label": port, "points": _pairs(rows, "date_str", "price")}
    sections = []
    fc = conn.execute(
        "SELECT Period, VLSFO, MGO, LSMGO, HSFO FROM bunker_forecast "
        "WHERE graph_date = (SELECT MAX(graph_date) FROM bunker_forecast) "
        "ORDER BY Period"
    ).fetchall()
    if fc:
        sections.append({
            "type": "static", "title": "벙커유 전망 — Opinet ($/ton)",
            "cols": ["PERIOD", "VLSFO", "MGO", "LSMGO", "HSFO"],
            "rows": [[r["Period"], r2(r["VLSFO"]), r2(r["MGO"]), r2(r["LSMGO"]),
                      r2(r["HSFO"])] for r in fc],
            "tag": "oil_forecast",
        })
    _detail_write("oil", {
        "id": "oil", "title": "유가 / 벙커링 — Crude & VLSFO",
        "updated": _detail_updated(datasets), "datasets": datasets,
        "panels": [
            {"id": "crude", "title": "CRUDE OIL ($/배럴) — 3지수 중첩",
             "keys": ["Dubai", "Brent", "WTI"],
             "initial": ["Dubai", "Brent", "WTI"], "navKey": "Dubai"},
            {"id": "vlsfo", "title": "VLSFO ($/ton) — 항만별 중첩",
             "keys": ["Rotterdam", "Singapore", "Fujairah"],
             "initial": ["Rotterdam", "Singapore", "Fujairah"],
             "navKey": "Rotterdam"},
        ],
        "sections": sections,
    })


def build_detail_macro(conn) -> None:
    gscpi = conn.execute(
        "SELECT date, value FROM gscpi_series ORDER BY date"
    ).fetchall()
    datasets = {"gscpi": {"label": "GSCPI", "points": _pairs(gscpi, "date", "value")}}
    for ind, lab in (("GDP_GROWTH", "GDP 성장률 (월드)"),
                     ("CPI_INFLATION", "물가상승률 (월드)")):
        rows = conn.execute(
            "SELECT year, value FROM macro_series "
            "WHERE indicator = ? AND country_code = 'WLD' ORDER BY year",
            (ind,)).fetchall()
        datasets[ind] = {"label": lab,
                         "points": [[str(r["year"]), r2(r["value"])] for r in rows]}
    _detail_write("macro", {
        "id": "macro", "title": "거시경제 — GSCPI / GDP / CPI",
        "updated": _detail_updated(datasets), "datasets": datasets,
        "panels": [
            {"id": "gscpi", "title": "GSCPI — 공급망 압력 (월간)",
             "keys": ["gscpi"], "fixed": True, "initial": ["gscpi"]},
            {"id": "world", "title": "월드 거시 — GDP / CPI (%)",
             "keys": ["GDP_GROWTH", "CPI_INFLATION"],
             "initial": ["GDP_GROWTH", "CPI_INFLATION"], "navKey": "GDP_GROWTH"},
        ],
    })


DETAIL_BUILDERS = {
    "linerlytica": build_detail_linerlytica,
    "alphaliner": build_detail_alphaliner,
    "forwarders": build_detail_forwarders,
    "air": build_detail_air,
    "oil": build_detail_oil,
    "macro": build_detail_macro,
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
            if module["id"] == "sea":
                build_sea_page(conn)
            if module["id"] in DETAIL_BUILDERS:
                try:
                    DETAIL_BUILDERS[module["id"]](conn)
                except Exception as detail_exc:  # noqa: BLE001
                    log(f"[detail] {module['id']} 페이지 생성 실패: {detail_exc}")
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
