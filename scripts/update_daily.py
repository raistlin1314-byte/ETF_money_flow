# -*- coding: utf-8 -*-
"""
ETF 份额流向看板 —— 每日增量更新管道
====================================================

数据源: Tushare Pro fund_share (与 codex 历史库同源同口径, 单位=万份)
思路:
  1. 找出本地 alternative_v2/etf/ 里已有的最新交易日
  2. 用 Tushare trade_cal 列出之后到今天的所有交易日(自愈式回补)
  3. 逐日拉 fund_share, 造 etf_share_size_YYYYMMDD.parquet (schema 与历史库完全一致)
  4. 导入 codex 的 build_etf_dashboard.py 复用其 build_data()/render_html()
     - series / movers 天然含新日期 (build 扫全部 parquet)
     - total 总曲线只到 XLSX 最后一天(07-10), 用"偏移法"向后延伸保持连续
  5. 输出 HTML -> 改名 index.html -> 拷到发布仓库 -> git push (带重试)

用法:
  python update_daily.py                # 全自动: 取数->重建->推送
  python update_daily.py --no-push      # 只重建本地, 不推 GitHub
  python update_daily.py --date 20260711  # 只补指定日期
  python update_daily.py --rebuild-only   # 不取数, 只用现有 parquet 重建+推送

限制(用户已知悉并接受):
  - 新上市ETF在 XLSX 分类表补充前"暂缺分类"(不进5大类, 只影响归类不影响取数)
  - 份额 T+1 披露, 晚21:00 通常只能取到最近已披露交易日, 非当天
  - total 顶部曲线: 5大类之和 + 固定偏移(约264亿份, 代表~46只未分类ETF), 漂移<1.3%
  - 规模(亿元)'a': 按份额比例缩放延伸(size≈share×nav, 短期nav稳定), 非独立取数
"""
import argparse
import importlib.util
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

# ---- 路径与常量 ----
ROOT = Path(r"D:\ETF_MONEY_FLOW")
DAILY_DIR = ROOT / "ETF_Data" / "data" / "parquet" / "alternative_v2" / "etf"
BUILD_SCRIPT = ROOT / "scripts" / "build_etf_dashboard.py"
OUTPUT_HTML = ROOT / "etf_money_flow_dashboard.html"
INDEX_LOCAL = ROOT / "index.html"
PUBLISH_REPO = Path(r"D:\ETF_money_flow_repo")
PUBLISH_INDEX = PUBLISH_REPO / "index.html"
SPLIT_REGISTRY_PATH = ROOT / "scripts" / "split_registry.json"

TUSHARE_TOKEN = "e401e7f024d6ac28c9f10e81560ebc6c1928f24c558f6376fb157236"

MARKET_TO_EXCHANGE = {"SH": "SSE", "SZ": "SZSE"}

# 防呆: 全市场股票+全品种ETF正常应有 1500+ 只。份额未披露/过早抓取时 Tushare
# 会返回极少行(如凌晨返回1行)。低于阈值一律当"未披露"拒绝, 防止假数据污染看板。
MIN_ABS_ROWS = 500          # 绝对下限(2024年初最少也有863只)
MIN_FRAC_OF_RECENT = 0.5    # 不得低于近期典型行数的50%(一夜不可能腰斩)



def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def get_pro():
    import tushare as ts
    ts.set_token(TUSHARE_TOKEN)
    return ts.pro_api()


def existing_dates():
    ds = []
    for f in DAILY_DIR.glob("etf_share_size_*.parquet"):
        s = "".join(ch for ch in f.stem if ch.isdigit())
        if len(s) == 8:
            ds.append(s)
    return sorted(ds)


def target_trade_days(pro, explicit_date=None):
    """返回需要抓取的交易日列表(YYYYMMDD)。"""
    if explicit_date:
        return [explicit_date]
    have = set(existing_dates())
    last = max(have) if have else "20240102"
    today = datetime.now().strftime("%Y%m%d")
    cal = pro.trade_cal(exchange="SSE", start_date=last, end_date=today, is_open="1")
    days = sorted(cal["cal_date"].astype(str).tolist())
    # 排除已存在的日期(last 本身已在库里)
    return [d for d in days if d not in have]


def recent_typical_rows(sample=8):
    """近期已入库文件的典型行数(中位数), 用于防呆阈值。破损的07-10(902 SSE-only)会被中位数稀释。"""
    ds = existing_dates()
    counts = []
    for d in ds[-sample:]:
        try:
            p = pd.read_parquet(DAILY_DIR / f"etf_share_size_{d}.parquet", columns=["ts_code"])
            counts.append(len(p))
        except Exception:
            pass
    if not counts:
        return 0
    counts.sort()
    return counts[len(counts) // 2]


def fetch_and_save(pro, trade_date, force=False):
    """拉 fund_share + fund_nav 造 parquet（含 total_size=资产净值万元，拆分免疫）。
    返回 (saved:bool, rows:int)。"""
    out = DAILY_DIR / f"etf_share_size_{trade_date}.parquet"
    if out.exists() and not force:
        log(f"  {trade_date} 已存在, 跳过")
        return False, 0
    df = pro.fund_share(trade_date=trade_date)
    if df is None or df.empty:
        log(f"  {trade_date} Tushare 暂无数据(可能未披露), 跳过")
        return False, 0
    df = df[df["fund_type"] == "ETF"].copy()
    if df.empty:
        log(f"  {trade_date} 无ETF数据, 跳过")
        return False, 0
    # 防呆: 行数过少 = 未完整披露(如凌晨过早抓取), 拒绝入库
    n = len(df)
    typical = recent_typical_rows()
    floor = max(MIN_ABS_ROWS, int(typical * MIN_FRAC_OF_RECENT))
    if n < floor:
        log(f"  {trade_date} 仅 {n} 行 < 阈值 {floor}(近期典型{typical}) —— 疑似未完整披露, 拒绝入库")
        return False, 0

    # --- 拉取单位净值以计算资产总值(total_size) ---
    # 注: Tushare 2025-11-03 起禁止多 ts_code 同时提取，必须逐只查询
    # fund_nav 不传 ts_code 仅返回场外基金(.OF)，ETF 只能逐只调用
    # Tushare 限流 500 次/分钟 → 0.15s 间隔 = 400次/分钟, 安全边际内, ~1600只需~4分钟
    nav_map = {}
    nav_errors = 0
    try:
        etf_codes = df["ts_code"].dropna().unique().tolist()
        total_codes = len(etf_codes)
        for i, code in enumerate(etf_codes):
            try:
                ndf = pro.fund_nav(ts_code=code, nav_date=trade_date)
                if len(ndf) > 0:
                    nav_val = ndf["unit_nav"].iloc[0]
                    if pd.notna(nav_val):
                        nav_map[code] = float(nav_val)
                time.sleep(0.15)
            except Exception:
                nav_errors += 1
            if (i + 1) % 300 == 0:
                log(f"  fund_nav 进度 {i+1}/{total_codes}, 已匹配 {len(nav_map)}, 错误 {nav_errors}")
        df["_unit_nav"] = df["ts_code"].map(nav_map)
        df["total_size"] = pd.to_numeric(df["fd_share"], errors="coerce") * df["_unit_nav"]
        nav_hit = df["total_size"].notna().sum()
        log(f"  {trade_date} fund_nav 逐只匹配 {nav_hit}/{n} 只 (错误 {nav_errors}), total_size 已计算 (万元)")
    except Exception as e:
        log(f"  {trade_date} fund_nav 拉取失败: {e!r}, total_size 将为 None")
        df["total_size"] = None

    rec = pd.DataFrame({
        "trade_date": trade_date,
        "ts_code": df["ts_code"].astype(str),
        "etf_name": None,
        "total_share": pd.to_numeric(df["fd_share"], errors="coerce"),
        "total_size": df["total_size"],
        "exchange": df["market"].map(MARKET_TO_EXCHANGE).fillna(df["market"]),
    })
    rec = rec.dropna(subset=["total_share"])
    rec.to_parquet(out, index=False)
    sh = int((rec["exchange"] == "SSE").sum())
    sz = int((rec["exchange"] == "SZSE").sum())
    log(f"  {trade_date} 已保存 {len(rec)} 行 (SSE={sh}, SZSE={sz})")
    return True, len(rec)


def load_build_module():
    spec = importlib.util.spec_from_file_location("build_etf_dashboard", str(BUILD_SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # 有 __main__ 保护, 导入不触发构建
    return mod


def extend_total(data):
    """把 total 从 XLSX 最后一天向后延伸到 series 的最新日期。
    偏移法: total.s = 5大类之和 + offset; offset 锁定在 final_total_date 那天。
    n 沿用 XLSX 末值(代表同一~1256只全口径); a 按份额比例缩放。
    """
    series = data["series"]
    total = data["total"]
    if not total or not series:
        return data
    any_ser = next(iter(series.values()))
    dates = [row["d"] for row in any_ser]
    final_d = total[-1]["d"]
    if final_d not in dates:
        log(f"  [warn] final_total_date {final_d} 不在 series 日期中, 跳过延伸")
        return data
    idx_final = dates.index(final_d)

    def cat_sum(i):
        return sum(series[c][i]["s"] for c in series if i < len(series[c]) and series[c][i]["s"] is not None)

    cat_sum_final = cat_sum(idx_final)
    offset = round(total[-1]["s"] - cat_sum_final, 6)
    base_n = total[-1]["n"]
    final_s = total[-1]["s"]
    final_a = total[-1]["a"]
    prev_s = final_s
    added = 0
    for i in range(idx_final + 1, len(dates)):
        s = round(cat_sum(i) + offset, 6)
        a = round(final_a * (s / final_s), 6) if (final_a and final_s) else final_a
        total.append({
            "d": dates[i],
            "n": base_n,
            "s": s,
            "a": a,
            "c": round(s - prev_s, 6),
        })
        prev_s = s
        added += 1
    if added:
        data["sourceDateRange"]["end"] = dates[-1]
        log(f"  total 曲线延伸 {added} 个交易日 (offset={offset} 亿份), 末日={dates[-1]}")
    return data


def rebuild_html():
    mod = load_build_module()
    log("  build_data() 中 (扫全部 parquet)...")
    data = mod.build_data()
    data = postprocess_splits(data)      # 拆分份额修复（必须在 extend_total 之前）
    data = extend_total(data)
    html = mod.render_html(data)
    OUTPUT_HTML.write_text(html, encoding="utf-8")
    INDEX_LOCAL.write_text(html, encoding="utf-8")
    log(f"  HTML 生成: {OUTPUT_HTML.name} ({OUTPUT_HTML.stat().st_size} bytes), 本地 index.html 已更新")
    return data


def load_split_registry():
    """加载本地拆分登记册。若尚未建立则扫描全部 parquet 自动生成。"""
    if SPLIT_REGISTRY_PATH.exists():
        return json.loads(SPLIT_REGISTRY_PATH.read_text("utf-8"))
    return scan_all_splits()


def scan_all_splits():
    """全量扫描 parquet 文件, 检测份额 >=1.8x 的单日跳变作为拆分候选, 写入登记册。"""
    files = []
    for f in DAILY_DIR.glob("etf_share_size_*.parquet"):
        s = "".join(ch for ch in f.stem if ch.isdigit())
        if len(s) == 8:
            files.append((s, f))
    files.sort()
    reg = {}
    # 逐对比较, 检测份额暴增
    for i in range(len(files) - 1):
        d0, f0 = files[i]
        d1, f1 = files[i + 1]
        df0 = pd.read_parquet(f0, columns=["ts_code", "total_share"])
        df1 = pd.read_parquet(f1, columns=["ts_code", "total_share"])
        m = df1[["ts_code", "total_share"]].merge(
            df0[["ts_code", "total_share"]], on="ts_code", suffixes=("_1", "_0"), how="inner")
        m["ratio"] = m["total_share_1"] / m["total_share_0"].replace(0, pd.NA)
        split_candidates = m[(m["ratio"] >= 1.8) & (m["ratio"] < 10) & (m["total_share_0"] > 0)]
        for _, row in split_candidates.iterrows():
            code = row["ts_code"]
            ratio = round(row["ratio"], 4)
            entry = reg.get(code, {"splits": [], "cumulative_ratio": 1.0})
            entry["splits"].append({"date": d1, "ratio": ratio})
            entry["cumulative_ratio"] = round(entry["cumulative_ratio"] * ratio, 4)
            entry["splits"].sort(key=lambda x: x["date"])
            reg[code] = entry
            log(f"  🔍 拆分检测: {code} {d0}→{d1} x{ratio:.2f}")
    SPLIT_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    SPLIT_REGISTRY_PATH.write_text(json.dumps(reg, ensure_ascii=False, indent=2), "utf-8")
    log(f"  拆分登记册已生成: {len(reg)} 只 ETF 检测到拆分事件")
    return reg


def postprocess_splits(data):
    """对 build_data() 产出的 data dict 进行拆分修复:
    - 对拆分 ETF 的日变动(daily movers)，用 total_size 变化替代 total_share 变化
    - 修正后的 movers 重新排序
    """
    reg = load_split_registry()
    if not reg:
        log("  [拆分修复] 无拆分记录, 跳过")
        return data
    # 建立按日期的拆分ETF集合
    splits_by_date = {}
    for code, entry in reg.items():
        for s in entry.get("splits", []):
            d = s["date"][:4] + "-" + s["date"][4:6] + "-" + s["date"][6:8]  # YYYYMMDD -> YYYY-MM-DD
            splits_by_date.setdefault(d, set()).add(code)

    fixed_count = 0
    movers = data.get("movers", {})
    for date_key, cats in movers.items():
        affected_codes = splits_by_date.get(date_key, set())
        if not affected_codes:
            continue
        for cat_key, periods in cats.items():
            daily = periods.get("daily")
            if not daily:
                continue
            up_fixed = []
            down_fixed = []
            for row in daily.get("up", []):
                if row.get("code") in affected_codes:
                    # 用资产净值变动替代份额变动（如果可用）
                    size_change = row.get("size_change")
                    if size_change is not None:
                        row["change"] = size_change
                        row["_split_adjusted"] = True
                        fixed_count += 1
                    else:
                        row["change"] = 0  # 无法修正则归零
                        row["_split_adjusted"] = True
                up_fixed.append(row)
            for row in daily.get("down", []):
                if row.get("code") in affected_codes:
                    size_change = row.get("size_change")
                    if size_change is not None:
                        row["change"] = size_change
                        row["_split_adjusted"] = True
                        fixed_count += 1
                    else:
                        row["change"] = 0
                        row["_split_adjusted"] = True
                down_fixed.append(row)
            # 重新排序
            up_fixed.sort(key=lambda r: r.get("change", 0) or 0, reverse=True)
            down_fixed.sort(key=lambda r: r.get("change", 0) or 0)
            daily["up"] = up_fixed[:5]
            daily["down"] = down_fixed[:5]
            # 重算 totals
            up_sum = sum(r.get("change", 0) or 0 for r in up_fixed if (r.get("change") or 0) > 0)
            down_sum = abs(sum(r.get("change", 0) or 0 for r in down_fixed if (r.get("change") or 0) < 0))
            up_count = sum(1 for r in up_fixed if (r.get("change") or 0) > 0)
            down_count = sum(1 for r in down_fixed if (r.get("change") or 0) < 0)
            daily["totals"] = {"up": up_sum, "down": down_sum, "upCount": up_count, "downCount": down_count}

    if fixed_count:
        log(f"  [拆分修复] {fixed_count} 条记录修正, 受影响 ETF: {len(reg)} 只")
    return data


def git_push(commit_msg, retries=3):
    if not PUBLISH_REPO.exists():
        log(f"  [错误] 发布仓库不存在: {PUBLISH_REPO}")
        return False
    import shutil
    shutil.copyfile(INDEX_LOCAL, PUBLISH_INDEX)
    log(f"  已拷贝 index.html 到发布仓库")

    def run(args):
        return subprocess.run(["git"] + args, cwd=str(PUBLISH_REPO),
                              capture_output=True, text=True, encoding="utf-8", errors="replace")

    # 铁律校验: 只能 main 分支, 文件名 index.html, 根目录
    br = run(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    if br != "main":
        log(f"  [错误] 当前分支 {br} != main, 拒绝推送")
        return False
    run(["add", "index.html"])
    st = run(["status", "--porcelain"]).stdout.strip()
    if not st:
        log("  无变更, 跳过提交")
        return True
    r = run(["commit", "-m", commit_msg])
    if r.returncode != 0:
        log(f"  [错误] commit 失败: {r.stderr.strip()[:200]}")
        return False
    for attempt in range(1, retries + 1):
        r = run(["push", "origin", "main"])
        if r.returncode == 0:
            log(f"  push 成功 (第{attempt}次)")
            return True
        log(f"  push 第{attempt}次失败: {(r.stderr or r.stdout).strip()[:180]}")
        if attempt < retries:
            time.sleep(8)
    log("  [错误] push 多次失败 —— 本地 index.html 已就绪, 可手工贴到仓库")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-push", action="store_true", help="只重建本地不推GitHub")
    ap.add_argument("--date", help="只补指定交易日 YYYYMMDD")
    ap.add_argument("--rebuild-only", action="store_true", help="不取数, 只重建+推送")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的parquet")
    args = ap.parse_args()

    log("=== ETF 份额流向看板 每日更新 ===")
    fetched = 0
    if not args.rebuild_only:
        pro = get_pro()
        days = target_trade_days(pro, args.date)
        if not days:
            log("无新交易日需要抓取")
        else:
            log(f"待抓取交易日: {days}")
            for d in days:
                try:
                    ok, _ = fetch_and_save(pro, d, force=args.force)
                    fetched += int(ok)
                    time.sleep(0.4)
                except Exception as e:
                    log(f"  {d} 抓取异常: {e!r}")
        if fetched == 0 and not args.date:
            log("无新数据入库。仍重建以确保产物最新。")

    data = rebuild_html()
    last_d = data["total"][-1]["d"] if data.get("total") else "?"

    if args.no_push:
        log("--no-push: 跳过 GitHub 推送")
    else:
        msg = f"ETF份额流向看板自动更新 至{last_d} ({datetime.now():%Y-%m-%d %H:%M})"
        git_push(msg)

    log(f"=== 完成. 最新数据日={last_d}, 新增交易日={fetched} ===")


if __name__ == "__main__":
    main()
