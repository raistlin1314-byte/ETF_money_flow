import json
import math
import re
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd


ROOT = Path(r"D:\ETF_MONEY_FLOW")
WORKBOOK = ROOT / "ETF市场概况.xlsx"
DAILY_DIR = ROOT / "ETF_Data" / "data" / "parquet" / "alternative_v2" / "etf"
OUTPUT = ROOT / "etf_money_flow_dashboard.html"

CATEGORY_SHEETS = {
    "规模指数ETF": "股票型ETF-规模指数ETF",
    "行业指数ETF": "股票型ETF-行业指数ETF",
    "策略指数ETF": "股票型ETF-策略指数ETF",
    "风格指数ETF": "股票型ETF-风格指数ETF",
    "主题指数ETF": "股票ETF-主题指数ETF",
}


def clean_number(value):
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return round(float(value), 6)


def parse_date(value):
    if pd.isna(value):
        return None
    return pd.to_datetime(value).strftime("%Y-%m-%d")


def read_categories():
    categories = {}
    metadata = {}
    pattern = re.compile(r"^\d{6}\.(SZ|SH)$")
    for category, sheet in CATEGORY_SHEETS.items():
        df = pd.read_excel(WORKBOOK, sheet_name=sheet, header=1)
        df = df[df["代码"].astype(str).str.match(pattern, na=False)].copy()
        df["代码"] = df["代码"].astype(str)
        categories[category] = set(df["代码"])
        for _, row in df.iterrows():
            code = row["代码"]
            metadata[code] = {
                "code": code,
                "name": str(row.get("基金简称", "") or ""),
                "company": str(row.get("基金公司简称", "") or ""),
                "benchmark": str(row.get("比较基准", "") or ""),
                "latest_share": clean_number(row.get("最新份额(亿份)")),
                "latest_assets": clean_number(row.get("最新资产净值(亿元)")),
            }
    return categories, metadata


def read_total_history():
    df = pd.read_excel(WORKBOOK, sheet_name="股票型ETF总体变化", header=1)
    df = df[pd.to_datetime(df["截止日期"], errors="coerce").notna()].copy()
    df["date"] = pd.to_datetime(df["截止日期"]).dt.strftime("%Y-%m-%d")
    df = df[df["date"] >= "2024-01-01"].sort_values("date")
    rows = []
    prev = None
    for _, row in df.iterrows():
        share = clean_number(row["最新份额(亿份)"])
        size = clean_number(row["最新资产净值(亿元)"])
        rows.append({
            "d": row["date"],
            "n": int(row["基金总数"]) if not pd.isna(row["基金总数"]) else None,
            "s": share,
            "a": size,
            "c": round(share - prev, 6) if prev is not None and share is not None else None,
        })
        prev = share
    return rows


def daily_files():
    files = []
    for file in DAILY_DIR.glob("etf_share_size_*.parquet"):
        match = re.search(r"(\d{8})", file.name)
        if not match:
            continue
        date = datetime.strptime(match.group(1), "%Y%m%d").strftime("%Y-%m-%d")
        if date >= "2024-01-01":
            files.append((date, file))
    return sorted(files)


def build_data():
    categories, metadata = read_categories()
    total_history = read_total_history()
    final_total_date = total_history[-1]["d"] if total_history else None
    code_to_category = {}
    duplicates = defaultdict(list)
    for category, codes in categories.items():
        for code in codes:
            if code in code_to_category:
                duplicates[code].append(category)
            else:
                code_to_category[code] = category
    union_codes = set(code_to_category)

    category_series = {category: [] for category in categories}
    previous_by_category = {category: None for category in categories}
    state_by_code = {}
    movers = {}
    mover_periods = {}
    snapshot_dates = []
    snapshots = {}

    for date, file in daily_files():
        previous_state = state_by_code.copy()
        df = pd.read_parquet(file, columns=["ts_code", "etf_name", "total_share", "total_size"])
        df = df[df["ts_code"].isin(union_codes)].copy()
        df["category"] = df["ts_code"].map(code_to_category)
        df["total_share"] = pd.to_numeric(df["total_share"], errors="coerce") / 10000.0
        df["total_size"] = pd.to_numeric(df["total_size"], errors="coerce") / 10000.0
        df["previous_share"] = df["ts_code"].map(lambda code: state_by_code.get(code, {}).get("share"))
        df["change"] = df["total_share"] - df["previous_share"]

        for _, row in df.iterrows():
            state_by_code[row["ts_code"]] = {
                "code": row["ts_code"],
                "name": row.get("etf_name"),
                "share": row["total_share"],
                "assets": row["total_size"],
                "category": row["category"],
            }

        adjustment_rows = []
        if date == final_total_date:
            for code in union_codes:
                meta = metadata.get(code, {})
                latest_share = meta.get("latest_share")
                if latest_share is None:
                    continue
                latest_assets = meta.get("latest_assets")
                previous = state_by_code.get(code, {})
                previous_share = previous.get("share")
                if previous_share is None or abs(latest_share - previous_share) > 0.000001:
                    category = code_to_category[code]
                    state_by_code[code] = {
                        "code": code,
                        "name": meta.get("name"),
                        "share": latest_share,
                        "assets": latest_assets if latest_assets is not None else previous.get("assets", 0),
                        "category": category,
                    }
                    adjustment_rows.append({
                        "ts_code": code,
                        "etf_name": meta.get("name"),
                        "total_share": latest_share,
                        "total_size": latest_assets if latest_assets is not None else previous.get("assets", 0),
                        "category": category,
                        "previous_share": previous_share,
                        "change": latest_share - previous_share if previous_share is not None else None,
                    })
        if adjustment_rows:
            df = pd.concat([df, pd.DataFrame(adjustment_rows)], ignore_index=True)

        current = pd.DataFrame(state_by_code.values())
        if current.empty:
            continue

        grouped = current.groupby("category", dropna=False).agg(
            count=("code", "nunique"),
            share=("share", "sum"),
            assets=("assets", "sum"),
        )

        for category in categories:
            if category in grouped.index:
                share = clean_number(grouped.loc[category, "share"])
                assets = clean_number(grouped.loc[category, "assets"])
                count = int(grouped.loc[category, "count"])
            else:
                share = 0.0
                assets = 0.0
                count = 0
            prev = previous_by_category[category]
            category_series[category].append({
                "d": date,
                "n": count,
                "s": share,
                "a": assets,
                "c": round(share - prev, 6) if prev is not None else None,
            })
            previous_by_category[category] = share

        five_day_date = snapshot_dates[-5] if len(snapshot_dates) >= 5 else None
        thirty_day_cutoff = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
        thirty_day_index = bisect_right(snapshot_dates, thirty_day_cutoff) - 1
        thirty_day_date = snapshot_dates[thirty_day_index] if thirty_day_index >= 0 else None

        movers[date] = {
            category: {
                "daily": ranked_movers(state_by_code, previous_state, category, metadata),
                "fiveTradingDays": ranked_movers(
                    state_by_code,
                    snapshots[five_day_date] if five_day_date else None,
                    category,
                    metadata,
                ),
                "thirtyCalendarDays": ranked_movers(
                    state_by_code,
                    snapshots[thirty_day_date] if thirty_day_date else None,
                    category,
                    metadata,
                ),
            }
            for category in categories
        }
        mover_periods[date] = {
            "daily": {"start": snapshot_dates[-1] if snapshot_dates else None, "end": date},
            "fiveTradingDays": {"start": five_day_date, "end": date},
            "thirtyCalendarDays": {"start": thirty_day_date, "end": date},
        }
        snapshot_dates.append(date)
        snapshots[date] = state_by_code.copy()

    return {
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sourceDateRange": {
            "start": total_history[0]["d"] if total_history else None,
            "end": total_history[-1]["d"] if total_history else None,
        },
        "categories": list(CATEGORY_SHEETS.keys()),
        "categoryCounts": {category: len(codes) for category, codes in categories.items()},
        "duplicateCodes": {code: values for code, values in duplicates.items()},
        "total": total_history,
        "series": category_series,
        "movers": movers,
        "moverPeriods": mover_periods,
    }


def ranked_movers(current_state, baseline_state, category, metadata):
    if not baseline_state:
        return {"up": [], "down": [], "totals": {"up": 0.0, "down": 0.0, "upCount": 0, "downCount": 0}}

    rows = []
    for code, current in current_state.items():
        if current["category"] != category:
            continue
        baseline = baseline_state.get(code)
        if not baseline:
            continue
        current_share = current.get("share")
        baseline_share = baseline.get("share")
        if current_share is None or baseline_share is None:
            continue
        change = current_share - baseline_share
        # 资产净值变动 (亿元, 拆分免疫, 含市场涨跌)
        current_assets = current.get("assets")
        baseline_assets = baseline.get("assets")
        size_change = None
        if current_assets is not None and baseline_assets is not None:
            size_change = clean_number(current_assets - baseline_assets)
        if abs(change) < 0.000001:
            continue
        meta = metadata.get(code, {})
        rows.append({
            "code": code,
            "name": meta.get("name") or current.get("name", ""),
            "change": clean_number(change),
            "size_change": size_change,
            "share": clean_number(current_share),
            "assets": clean_number(current.get("assets")),
            "company": meta.get("company", ""),
        })

    up_rows = sorted((row for row in rows if row["change"] > 0), key=lambda row: row["change"], reverse=True)
    down_rows = sorted((row for row in rows if row["change"] < 0), key=lambda row: row["change"])
    return {
        "up": up_rows[:5],
        "down": down_rows[:5],
        "totals": {
            "up": clean_number(sum(row["change"] for row in up_rows)) or 0.0,
            "down": clean_number(-sum(row["change"] for row in down_rows)) or 0.0,
            "upCount": len(up_rows),
            "downCount": len(down_rows),
        },
    }


def render_html(data):
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="data:,">
  <title>股票型ETF份额流向观察</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #20242a;
      --muted: #667085;
      --line: #d9dee7;
      --accent: #1f6feb;
      --accent-2: #1a7f64;
      --accent-3: #9a5b00;
      --warn: #b42318;
      --soft: #eef4ff;
      --shadow: 0 8px 22px rgba(15, 23, 42, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Microsoft YaHei", "PingFang SC", Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    main {{ max-width: 1280px; margin: 0 auto; padding: 24px; }}
    header {{ display: flex; justify-content: space-between; gap: 16px; align-items: flex-end; margin-bottom: 18px; }}
    h1 {{ margin: 0 0 6px; font-size: 24px; font-weight: 600; letter-spacing: 0; }}
    p {{ margin: 0; color: var(--muted); font-size: 13px; }}
    .toolbar, .cards, .grid {{ display: grid; gap: 12px; }}
    .toolbar {{ grid-template-columns: 1fr auto; align-items: end; margin-bottom: 14px; }}
    .category-buttons {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    button, select {{
      border: 1px solid var(--line);
      background: var(--panel);
      color: var(--text);
      border-radius: 8px;
      padding: 8px 11px;
      font-size: 14px;
      cursor: pointer;
    }}
    button.active {{ background: var(--accent); border-color: var(--accent); color: white; }}
    label {{ display: grid; gap: 6px; font-size: 12px; color: var(--muted); }}
    .cards {{ grid-template-columns: repeat(4, minmax(0, 1fr)); margin-bottom: 14px; }}
    .card, .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }}
    .card {{ padding: 14px; min-height: 92px; }}
    .card .label {{ color: var(--muted); font-size: 12px; margin-bottom: 8px; }}
    .card .value {{ font-size: 22px; font-weight: 600; }}
    .card .delta {{ margin-top: 6px; font-size: 12px; color: var(--muted); }}
    .positive {{ color: var(--accent-2); }}
    .negative {{ color: var(--warn); }}
    .panel {{ padding: 14px; margin-bottom: 14px; }}
    .chart-wrap {{ width: 100%; height: 390px; position: relative; }}
    svg {{ display: block; width: 100%; height: 100%; }}
    .axis text {{ fill: var(--muted); font-size: 12px; }}
    .axis line, .axis path, .grid-line {{ stroke: var(--line); stroke-width: 1; }}
    .line-total {{ fill: none; stroke: var(--accent); stroke-width: 2.2; }}
    .line-category {{ fill: none; stroke: var(--accent-2); stroke-width: 2; }}
    .line-category-ratio {{ fill: none; stroke: var(--accent-3); stroke-width: 2; stroke-dasharray: 5 4; }}
    .dot {{ fill: var(--panel); stroke-width: 2; }}
    .tooltip {{
      position: absolute;
      pointer-events: none;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px 10px;
      box-shadow: var(--shadow);
      font-size: 12px;
      opacity: 0;
      transform: translate(-50%, -105%);
      white-space: nowrap;
    }}
    .grid {{ grid-template-columns: 1fr 1fr; }}
    .panel h2 {{ font-size: 17px; font-weight: 600; margin: 0 0 12px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ padding: 9px 8px; border-bottom: 1px solid var(--line); text-align: right; white-space: nowrap; }}
    th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) {{ text-align: left; }}
    th {{ color: var(--muted); font-weight: 500; background: #fafbfc; position: sticky; top: 0; }}
    .table-scroll {{ overflow: auto; max-height: 390px; border: 1px solid var(--line); border-radius: 8px; }}
    .mover-tables {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
    .mover-windows {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }}
    .mover-windows button {{ font-size: 13px; }}
    .subhead {{ display: flex; flex-wrap: wrap; justify-content: space-between; gap: 12px; align-items: baseline; margin-bottom: 10px; }}
    .small {{ color: var(--muted); font-size: 12px; }}
    @media (max-width: 860px) {{
      main {{ padding: 14px; }}
      header, .toolbar {{ grid-template-columns: 1fr; display: grid; align-items: start; }}
      .cards, .grid, .mover-tables {{ grid-template-columns: 1fr; }}
      .chart-wrap {{ height: 320px; }}
      th, td {{ padding: 8px 6px; }}
    }}
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>股票型ETF份额流向观察</h1>
      <p>展示区间：2024-01-01 至 <span id="last-date"></span>；份额单位：亿份，资产净值单位：亿元。</p>
    </div>
    <p>生成时间：{data["generatedAt"]}</p>
  </header>

  <section class="toolbar">
    <div class="category-buttons" id="category-buttons"></div>
    <label>选择日期
      <select id="date-select"></select>
    </label>
  </section>

  <section class="cards">
    <div class="card"><div class="label">股票型ETF总份额</div><div class="value" id="total-share"></div><div class="delta" id="total-change"></div></div>
    <div class="card"><div class="label">当前子类别总份额</div><div class="value" id="cat-share"></div><div class="delta" id="cat-change"></div></div>
    <div class="card"><div class="label">当前子类别基金数</div><div class="value" id="cat-count"></div><div class="delta" id="cat-assets"></div></div>
    <div class="card"><div class="label">当日最大净申购方向</div><div class="value" id="top-flow"></div><div class="delta" id="top-flow-detail"></div></div>
  </section>

  <section class="panel">
    <div class="subhead">
      <h2>总份额与子类别份额曲线</h2>
      <span class="small" id="chart-label"></span>
    </div>
    <div class="chart-wrap">
      <svg id="line-chart" role="img" aria-label="ETF份额及子类别占比变化曲线"></svg>
      <div class="tooltip" id="tooltip"></div>
    </div>
  </section>

  <section class="grid">
    <div class="panel">
      <h2>每日汇总数据</h2>
      <div class="table-scroll"><table id="summary-table"></table></div>
    </div>
    <div class="panel">
    <div class="subhead">
        <h2>子类别ETF份额增减前五</h2>
        <span class="small" id="mover-label"></span>
      </div>
      <div class="mover-windows" id="mover-windows">
        <button type="button" data-window="daily">当日增减</button>
        <button type="button" data-window="fiveTradingDays">最近五个交易日</button>
        <button type="button" data-window="thirtyCalendarDays">最近三十天</button>
      </div>
      <div class="mover-tables">
        <div><h2 class="small" id="up-title">份额增加前五</h2><div class="small" id="up-summary"></div><div class="table-scroll"><table id="up-table"></table></div></div>
        <div><h2 class="small" id="down-title">份额减少前五</h2><div class="small" id="down-summary"></div><div class="table-scroll"><table id="down-table"></table></div></div>
      </div>
    </div>
  </section>
</main>

<script id="dashboard-data" type="application/json">{payload}</script>
<script>
const data = JSON.parse(document.getElementById("dashboard-data").textContent);
let selectedCategory = data.categories[0];
let selectedDate = data.total[data.total.length - 1].d;
let selectedMoverWindow = "daily";

const fmt = new Intl.NumberFormat("zh-CN", {{ maximumFractionDigits: 2, minimumFractionDigits: 0 }});
const fmt2 = new Intl.NumberFormat("zh-CN", {{ maximumFractionDigits: 2, minimumFractionDigits: 2 }});
const byDate = arr => Object.fromEntries(arr.map(x => [x.d, x]));
const totalByDate = byDate(data.total);
const catByDate = Object.fromEntries(data.categories.map(c => [c, byDate(data.series[c])]));

function formatDelta(v) {{
  if (v === null || v === undefined) return "首个交易日无环比";
  const cls = v >= 0 ? "positive" : "negative";
  const sign = v >= 0 ? "+" : "";
  return `<span class="${{cls}}">${{sign}}${{fmt2.format(v)}} 亿份</span> 环比`;
}}

function setText(id, value) {{ document.getElementById(id).textContent = value; }}

function initControls() {{
  document.getElementById("last-date").textContent = data.sourceDateRange.end;
  const buttons = document.getElementById("category-buttons");
  buttons.innerHTML = data.categories.map(c => `<button type="button" data-category="${{c}}">${{c}}</button>`).join("");
  buttons.addEventListener("click", event => {{
    const button = event.target.closest("button[data-category]");
    if (!button) return;
    selectedCategory = button.dataset.category;
    render();
  }});
  const dateSelect = document.getElementById("date-select");
  dateSelect.innerHTML = data.total.map(x => `<option value="${{x.d}}">${{x.d}}</option>`).join("");
  dateSelect.value = selectedDate;
  dateSelect.addEventListener("change", () => {{
    selectedDate = dateSelect.value;
    render();
  }});
  const moverWindows = document.getElementById("mover-windows");
  moverWindows.addEventListener("click", event => {{
    const button = event.target.closest("button[data-window]");
    if (!button) return;
    selectedMoverWindow = button.dataset.window;
    renderMovers();
  }});
}}

function renderCards() {{
  const total = totalByDate[selectedDate];
  const cat = catByDate[selectedCategory][selectedDate] || {{ s: 0, a: 0, n: 0, c: null }};
  setText("total-share", fmt2.format(total.s));
  document.getElementById("total-change").innerHTML = formatDelta(total.c);
  setText("cat-share", fmt2.format(cat.s));
  document.getElementById("cat-change").innerHTML = formatDelta(cat.c);
  setText("cat-count", fmt.format(cat.n || 0));
  setText("cat-assets", `资产净值 ${{fmt2.format(cat.a || 0)}} 亿元`);
  const movers = (((data.movers[selectedDate] || {{}})[selectedCategory] || {{}}).daily || {{ up: [] }}).up || [];
  if (movers.length) {{
    setText("top-flow", movers[0].name);
    setText("top-flow-detail", `${{movers[0].code}}：+${{fmt2.format(movers[0].change)}} 亿份`);
  }} else {{
    setText("top-flow", "-");
    setText("top-flow-detail", "无可比环比数据");
  }}
}}

function renderChart() {{
  const svg = document.getElementById("line-chart");
  const tooltip = document.getElementById("tooltip");
  const rect = svg.getBoundingClientRect();
  const width = Math.max(320, rect.width);
  const height = Math.max(260, rect.height);
  const compactChart = width < 650;
  const margin = {{ top: compactChart ? 18 : 28, right: compactChart ? 132 : 170, bottom: 34, left: 66 }};
  const plotW = width - margin.left - margin.right;
  const plotH = height - margin.top - margin.bottom;
  const total = data.total;
  const cat = data.series[selectedCategory];
  const ratio = cat.map((point, i) => ({{ d: point.d, s: total[i] && total[i].s ? point.s / total[i].s * 100 : 0 }}));
  const dates = total.map(x => x.d);
  const bounds = values => {{
    const min = Math.min(...values);
    const max = Math.max(...values);
    const pad = Math.max((max - min) * 0.08, 1);
    return [min - pad, max + pad];
  }};
  const [totalY0, totalY1] = bounds(total.map(x => x.s).filter(Number.isFinite));
  const [catY0, catY1] = bounds(cat.map(x => x.s).filter(Number.isFinite));
  const [ratioY0, ratioY1] = bounds(ratio.map(x => x.s).filter(Number.isFinite));
  const x = i => margin.left + (dates.length <= 1 ? 0 : i * plotW / (dates.length - 1));
  const yTotal = v => margin.top + (totalY1 - v) * plotH / (totalY1 - totalY0);
  const yCategory = v => margin.top + (catY1 - v) * plotH / (catY1 - catY0);
  const yRatio = v => margin.top + (ratioY1 - v) * plotH / (ratioY1 - ratioY0);
  const line = (arr, yScale) => arr.map((p, i) => `${{i ? "L" : "M"}}${{x(i).toFixed(1)}},${{yScale(p.s).toFixed(1)}}`).join(" ");
  const ticks = 5;
  const tickValues = Array.from({{ length: ticks }}, (_, i) => {{
    const progress = i / (ticks - 1);
    return {{
      total: totalY0 + (totalY1 - totalY0) * progress,
      category: catY0 + (catY1 - catY0) * progress,
      ratio: ratioY0 + (ratioY1 - ratioY0) * progress,
    }};
  }});
  const dateTicks = compactChart
    ? [0, dates.length - 1]
    : [0, Math.floor(dates.length / 4), Math.floor(dates.length / 2), Math.floor(dates.length * 3 / 4), dates.length - 1];
  const dateTickLabel = date => compactChart ? date.slice(0, 7) : date;
  const selectedIndex = dates.indexOf(selectedDate);
  const totalPoint = total[selectedIndex];
  const catPoint = cat[selectedIndex] || {{ s: 0 }};
  const ratioPoint = ratio[selectedIndex] || {{ s: 0 }};
  const shareAxisTitle = width < 650 ? "右一：份额（亿份）" : `右一：${{selectedCategory}}份额（亿份）`;
  const ratioAxisTitle = width < 650 ? "右二：占比（%）" : "右二：子类占比（%）";

  svg.setAttribute("viewBox", `0 0 ${{width}} ${{height}}`);
  svg.innerHTML = `
    <g class="axis">
      ${{tickValues.map(t => `<line class="grid-line" x1="${{margin.left}}" x2="${{width - margin.right}}" y1="${{yTotal(t.total)}}" y2="${{yTotal(t.total)}}"></line><text x="${{margin.left - 8}}" y="${{yTotal(t.total) + 4}}" text-anchor="end">${{fmt.format(t.total)}}</text><text x="${{width - margin.right + 8}}" y="${{yTotal(t.total) + 4}}">${{fmt.format(t.category)}}</text><text x="${{width - 8}}" y="${{yTotal(t.total) + 4}}" text-anchor="end">${{fmt2.format(t.ratio)}}%</text>`).join("")}}
      ${{dateTicks.map(i => `<text x="${{x(i)}}" y="${{height - 10}}" text-anchor="${{i === 0 ? "start" : i === dates.length - 1 ? "end" : "middle"}}">${{dateTickLabel(dates[i])}}</text>`).join("")}}
    </g>
    ${{compactChart ? "" : `<text x="${{margin.left}}" y="${{margin.top - 5}}" fill="var(--accent)" font-size="12">股票型ETF（左轴，亿份）</text><text x="${{width - margin.right}}" y="${{margin.top - 5}}" fill="var(--accent-2)" font-size="12" text-anchor="end">${{shareAxisTitle}}</text><text x="${{width - 8}}" y="${{margin.top - 5}}" fill="var(--accent-3)" font-size="12" text-anchor="end">${{ratioAxisTitle}}</text>`}}
    <path class="line-total" d="${{line(total, yTotal)}}"></path>
    <path class="line-category" d="${{line(cat, yCategory)}}"></path>
    <path class="line-category-ratio" d="${{line(ratio, yRatio)}}"></path>
    <line x1="${{x(selectedIndex)}}" x2="${{x(selectedIndex)}}" y1="${{margin.top}}" y2="${{height - margin.bottom}}" stroke="var(--line)"></line>
    <circle class="dot" cx="${{x(selectedIndex)}}" cy="${{yTotal(totalPoint.s)}}" r="4" stroke="var(--accent)"></circle>
    <circle class="dot" cx="${{x(selectedIndex)}}" cy="${{yCategory(catPoint.s)}}" r="4" stroke="var(--accent-2)"></circle>
    <circle class="dot" cx="${{x(selectedIndex)}}" cy="${{yRatio(ratioPoint.s)}}" r="4" stroke="var(--accent-3)"></circle>
    <rect x="${{margin.left}}" y="${{margin.top}}" width="${{plotW}}" height="${{plotH}}" fill="transparent" id="hover-zone"></rect>
  `;
  document.getElementById("chart-label").textContent = `左轴：股票型ETF总份额；右一：${{selectedCategory}}份额；右二：子类占比`;
  const hover = svg.querySelector("#hover-zone");
  hover.addEventListener("mousemove", event => {{
    const point = svg.createSVGPoint();
    point.x = event.clientX; point.y = event.clientY;
    const local = point.matrixTransform(svg.getScreenCTM().inverse());
    const idx = Math.max(0, Math.min(dates.length - 1, Math.round((local.x - margin.left) / plotW * (dates.length - 1))));
    selectedDate = dates[idx];
    document.getElementById("date-select").value = selectedDate;
    renderCards(); renderTables(); renderMovers(); renderChartMarkerOnly(idx, x, yTotal, yCategory, yRatio, total, cat, ratio, svg);
    tooltip.innerHTML = `${{selectedDate}}<br>股票型：${{fmt2.format(total[idx].s)}} 亿份<br>${{selectedCategory}}：${{fmt2.format((cat[idx] || {{s:0}}).s)}} 亿份<br>子类占比：${{fmt2.format((ratio[idx] || {{s:0}}).s)}}%`;
    tooltip.style.left = `${{Math.min(width - 90, Math.max(90, x(idx)))}}px`;
    tooltip.style.top = `${{Math.min(height - 20, Math.max(60, Math.min(yTotal(total[idx].s), yCategory((cat[idx] || {{s:0}}).s), yRatio((ratio[idx] || {{s:0}}).s))))}}px`;
    tooltip.style.opacity = "1";
  }});
  hover.addEventListener("mouseleave", () => {{ tooltip.style.opacity = "0"; }});
}}

function renderChartMarkerOnly(idx, x, yTotal, yCategory, yRatio, total, cat, ratio, svg) {{
  const markers = svg.querySelectorAll(".dot, line[stroke='var(--line)']");
  markers.forEach(node => node.remove());
  const height = svg.viewBox.baseVal.height;
  const marginTop = 18;
  const marginBottom = 34;
  svg.insertAdjacentHTML("beforeend", `
    <line x1="${{x(idx)}}" x2="${{x(idx)}}" y1="${{marginTop}}" y2="${{height - marginBottom}}" stroke="var(--line)"></line>
    <circle class="dot" cx="${{x(idx)}}" cy="${{yTotal(total[idx].s)}}" r="4" stroke="var(--accent)"></circle>
    <circle class="dot" cx="${{x(idx)}}" cy="${{yCategory((cat[idx] || {{s:0}}).s)}}" r="4" stroke="var(--accent-2)"></circle>
    <circle class="dot" cx="${{x(idx)}}" cy="${{yRatio((ratio[idx] || {{s:0}}).s)}}" r="4" stroke="var(--accent-3)"></circle>
  `);
}}

function renderTables() {{
  const table = document.getElementById("summary-table");
  const rows = data.total.map(t => {{
    const c = catByDate[selectedCategory][t.d] || {{ n: 0, s: 0, a: 0, c: null }};
    const ratio = t.s ? c.s / t.s * 100 : null;
    return `<tr><td>${{t.d}}</td><td>${{selectedCategory}}</td><td>${{fmt.format(t.n || 0)}}</td><td>${{fmt2.format(t.s)}}</td><td>${{t.c == null ? "-" : fmt2.format(t.c)}}</td><td>${{fmt.format(c.n || 0)}}</td><td>${{fmt2.format(c.s || 0)}}</td><td>${{ratio == null ? "-" : fmt2.format(ratio) + "%"}}</td><td>${{c.c == null ? "-" : fmt2.format(c.c)}}</td></tr>`;
  }}).reverse().join("");
  table.innerHTML = `<thead><tr><th>日期</th><th>子类别</th><th>股票型数量</th><th>股票型份额</th><th>股票型环比</th><th>子类数量</th><th>子类份额</th><th>子类占比</th><th>子类环比</th></tr></thead><tbody>${{rows}}</tbody>`;
}}

function moverTable(id, rows) {{
  const table = document.getElementById(id);
  table.innerHTML = `<thead><tr><th>代码</th><th>基金简称</th><th>增减</th><th>最新份额</th></tr></thead><tbody>${{rows.map(r => `<tr><td>${{r.code}}</td><td>${{r.name}}</td><td class="${{r.change >= 0 ? "positive" : "negative"}}">${{fmt2.format(r.change)}}</td><td>${{fmt2.format(r.share)}}</td></tr>`).join("") || `<tr><td colspan="4">无可比数据</td></tr>`}}</tbody>`;
}}

function renderMovers() {{
  const movers = ((((data.movers[selectedDate] || {{}})[selectedCategory] || {{}})[selectedMoverWindow]) || {{ up: [], down: [], totals: {{ up: 0, down: 0, upCount: 0, downCount: 0 }} }});
  const period = ((data.moverPeriods[selectedDate] || {{}})[selectedMoverWindow]) || {{ start: null, end: selectedDate }};
  const windowLabels = {{
    daily: "当日增减",
    fiveTradingDays: "最近五个交易日",
    thirtyCalendarDays: "最近三十天",
  }};
  const range = period.start ? `${{period.start}} 至 ${{period.end}}` : "暂无足够历史数据";
  document.getElementById("mover-label").textContent = `${{windowLabels[selectedMoverWindow]}}｜${{range}}｜${{selectedCategory}}`;
  document.getElementById("up-title").textContent = `${{windowLabels[selectedMoverWindow]}}增加前五`;
  document.getElementById("down-title").textContent = `${{windowLabels[selectedMoverWindow]}}减少前五`;
  document.querySelectorAll("button[data-window]").forEach(button => {{
    button.classList.toggle("active", button.dataset.window === selectedMoverWindow);
  }});
  moverTable("up-table", movers.up || []);
  moverTable("down-table", movers.down || []);
  renderMoverSummary("up-summary", movers.up || [], movers.totals || {{}}, "up");
  renderMoverSummary("down-summary", movers.down || [], movers.totals || {{}}, "down");
}}

function renderMoverSummary(id, rows, totals, direction) {{
  const total = Number(totals[direction] || 0);
  const count = Number(totals[`${{direction}}Count`] || 0);
  const topFive = rows.reduce((sum, row) => sum + Math.abs(Number(row.change || 0)), 0);
  if (!total) {{
    setText(id, direction === "up" ? "无正向增量" : "无负向减少");
    return;
  }}
  const label = direction === "up" ? "全部增加" : "全部减少";
  setText(id, `前五合计 ${{fmt2.format(topFive)}} 亿份，占${{label}} ${{fmt2.format(topFive / total * 100)}}%（${{fmt.format(count)}}只）`);
}}

function render() {{
  document.querySelectorAll("button[data-category]").forEach(b => b.classList.toggle("active", b.dataset.category === selectedCategory));
  document.getElementById("date-select").value = selectedDate;
  renderCards();
  renderChart();
  renderTables();
  renderMovers();
}}

window.addEventListener("resize", () => renderChart());
initControls();
render();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    dashboard_data = build_data()
    OUTPUT.write_text(render_html(dashboard_data), encoding="utf-8")
    print(json.dumps({
        "output": str(OUTPUT),
        "dates": len(dashboard_data["total"]),
        "categories": dashboard_data["categoryCounts"],
        "sourceDateRange": dashboard_data["sourceDateRange"],
        "htmlBytes": OUTPUT.stat().st_size,
    }, ensure_ascii=False, indent=2))
