import json
import re
from pathlib import Path

import pandas as pd


ROOT = Path(r"D:\ETF_MONEY_FLOW")
HTML = ROOT / "etf_money_flow_dashboard.html"
WORKBOOK = ROOT / "ETF市场概况.xlsx"
SHEETS = {
    "规模指数ETF": "股票型ETF-规模指数ETF",
    "行业指数ETF": "股票型ETF-行业指数ETF",
    "策略指数ETF": "股票型ETF-策略指数ETF",
    "风格指数ETF": "股票型ETF-风格指数ETF",
    "主题指数ETF": "股票ETF-主题指数ETF",
}


html = HTML.read_text(encoding="utf-8")
match = re.search(r'<script id="dashboard-data" type="application/json">(.*?)</script>', html)
if not match:
    raise RuntimeError("dashboard-data script block not found")
data = json.loads(match.group(1))

checks = {}
for category, sheet in SHEETS.items():
    df = pd.read_excel(WORKBOOK, sheet_name=sheet, header=1)
    code = df["代码"].astype(str)
    df = df[code.str.endswith(".SZ") | code.str.endswith(".SH")]
    excel_share = float(df["最新份额(亿份)"].sum())
    html_share = float(data["series"][category][-1]["s"])
    checks[category] = {
        "excel": round(excel_share, 4),
        "html": round(html_share, 4),
        "diff": round(html_share - excel_share, 6),
    }

latest_date = data["total"][-1]["d"]
mover_checks = {}
for category in data["categories"]:
    category_checks = {}
    for window in ("daily", "fiveTradingDays", "thirtyCalendarDays"):
        movers = data["movers"][latest_date][category][window]
        up = movers["up"]
        down = movers["down"]
        totals = movers["totals"]
        if any(row["change"] <= 0 for row in up):
            raise AssertionError(f"{category} {window} contains a non-positive increase row")
        if any(row["change"] >= 0 for row in down):
            raise AssertionError(f"{category} {window} contains a non-negative decrease row")
        if up != sorted(up, key=lambda row: row["change"], reverse=True):
            raise AssertionError(f"{category} {window} increases are not ranked descending")
        if down != sorted(down, key=lambda row: row["change"]):
            raise AssertionError(f"{category} {window} decreases are not ranked ascending")
        if len(up) > 5 or len(down) > 5:
            raise AssertionError(f"{category} {window} has more than five movers")
        top_up = sum(row["change"] for row in up)
        top_down = -sum(row["change"] for row in down)
        if top_up - totals["up"] > 0.000001 or top_down - totals["down"] > 0.000001:
            raise AssertionError(f"{category} {window} top-five total exceeds the all-ETF directional total")
        if totals["up"] < 0 or totals["down"] < 0:
            raise AssertionError(f"{category} {window} has a negative directional total")
        category_checks[window] = {
            "up": len(up),
            "down": len(down),
            "top5_up_share": round(top_up / totals["up"], 6) if totals["up"] else None,
            "top5_down_share": round(top_down / totals["down"], 6) if totals["down"] else None,
        }
    mover_checks[category] = category_checks

print(json.dumps({
    "date_range": data["sourceDateRange"],
    "total_dates": len(data["total"]),
    "category_latest_share_check": checks,
    "latest_date": latest_date,
    "latest_mover_row_counts": mover_checks,
}, ensure_ascii=False, indent=2))
