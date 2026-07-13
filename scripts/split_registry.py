"""
ETF share split registry.
Tracks cumulative split ratios so we can compute adjusted (split-normalized) shares.
"""
import json
from pathlib import Path

REGISTRY_PATH = Path(r"D:\ETF_MONEY_FLOW\scripts\split_registry.json")


def load():
    if REGISTRY_PATH.exists():
        return json.loads(REGISTRY_PATH.read_text("utf-8"))
    return {}


def save(registry):
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(registry, ensure_ascii=False, indent=2), "utf-8")


def register_split(ts_code, date, ratio):
    """Record a new split. ratio = post_share / pre_share (e.g. 2.0 for 1:2)"""
    reg = load()
    entry = reg.get(ts_code, {"splits": [], "cumulative_ratio": 1.0})
    # Avoid duplicate
    for s in entry["splits"]:
        if s["date"] == date:
            return reg
    entry["splits"].append({"date": date, "ratio": round(ratio, 6)})
    entry["cumulative_ratio"] = round(entry["cumulative_ratio"] * ratio, 6)
    entry["splits"].sort(key=lambda x: x["date"])
    reg[ts_code] = entry
    save(reg)
    return reg


def get_cumulative_ratio(ts_code, as_of_date=None):
    """Get cumulative split ratio for an ETF. Use to adjust: adjusted_share = raw_share / ratio"""
    reg = load()
    entry = reg.get(ts_code, {"splits": [], "cumulative_ratio": 1.0})
    if as_of_date is None:
        return entry["cumulative_ratio"]
    ratio = 1.0
    for s in entry["splits"]:
        if s["date"] <= as_of_date:
            ratio *= s["ratio"]
    return ratio
