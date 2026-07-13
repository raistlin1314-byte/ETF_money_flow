"""Initialize split registry by scanning parquet files for share jumps >1.8x."""
import json
import pandas as pd
from pathlib import Path

DAILY_DIR = Path(r"D:\ETF_MONEY_FLOW\ETF_Data\data\parquet\alternative_v2\etf")
REGISTRY_PATH = Path(r"D:\ETF_MONEY_FLOW\scripts\split_registry.json")

files = []
for f in DAILY_DIR.glob("etf_share_size_*.parquet"):
    s = "".join(ch for ch in f.stem if ch.isdigit())
    if len(s) == 8:
        files.append((s, f))
files.sort()
print(f"Scanning {len(files)} parquet files...")

reg = {}
for i in range(len(files) - 1):
    d0, f0 = files[i]
    d1, f1 = files[i + 1]
    df0 = pd.read_parquet(f0, columns=["ts_code", "total_share"])
    df1 = pd.read_parquet(f1, columns=["ts_code", "total_share"])
    m = df1[["ts_code", "total_share"]].merge(
        df0[["ts_code", "total_share"]], on="ts_code", suffixes=("_1", "_0"), how="inner")
    m["ratio"] = m["total_share_1"] / m["total_share_0"].replace(0, None)
    splits = m[(m["ratio"] >= 1.8) & (m["ratio"] < 10) & (m["total_share_0"] > 0)]
    for _, row in splits.iterrows():
        code = row["ts_code"]
        ratio = round(row["ratio"], 4)
        entry = reg.get(code, {"splits": [], "cumulative_ratio": 1.0})
        entry["splits"].append({"date": d1, "ratio": ratio})
        entry["cumulative_ratio"] = round(entry["cumulative_ratio"] * ratio, 4)
        entry["splits"].sort(key=lambda x: x["date"])
        reg[code] = entry
    if i % 50 == 0:
        print(f"  {d1}: {i+1}/{len(files)-1} files scanned, {len(reg)} split events found")

# Deduplicate cumulative ratios
for code in reg:
    entries = reg[code]["splits"]
    cum = 1.0
    for s in entries:
        cum *= s["ratio"]
    reg[code]["cumulative_ratio"] = round(cum, 4)

REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
REGISTRY_PATH.write_text(json.dumps(reg, ensure_ascii=False, indent=2), "utf-8")
print(f"\nDone. {len(reg)} ETFs with split events.")

# Show recent 7月 splits
jul = {k: [s for s in v["splits"] if "202607" in s["date"]] for k, v in reg.items()}
jul = {k: v for k, v in jul.items() if v}
print(f"\nJuly 2026 splits ({len(jul)} ETFs):")
for k, v in jul.items():
    print(f"  {k}: {v}")
