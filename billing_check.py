#!/usr/bin/env python3
"""
請求前チェック 自動実行スクリプト
/loops や cron から呼び出す用

使い方:
  python billing_check.py          # 最新月のデータを自動検出
  python billing_check.py 202506   # 月を指定
"""

import sys
from pathlib import Path
from datetime import datetime

DATA_DIR = Path(__file__).parent / "data"

# app.py のロジックを再利用
sys.path.insert(0, str(Path(__file__).parent))
import pandas as pd


# ── データ読み込み（app.py と共通） ────────────────────────────
MEISAI_COLS = ["サービス提供年月", "受給者証番号", "児童名", "保護者名",
               "日", "提供形態", "開始時間", "終了時間", "送迎往", "送迎復", "状況"]
MASTER_COLS = ["受給者証番号", "児童名", "市町村番号", "様式種別番号", "算定時間記載"]


def load_meisai(ym):
    p = DATA_DIR / f"実績明細_{ym}.csv"
    if not p.exists():
        return pd.DataFrame(columns=MEISAI_COLS)
    return pd.read_csv(p, dtype=str, keep_default_na=False)


def load_master():
    p = DATA_DIR / "児童マスター.csv"
    if not p.exists():
        return pd.DataFrame(columns=MASTER_COLS)
    return pd.read_csv(p, dtype=str, keep_default_na=False)


def get_summary(ym):
    df = load_meisai(ym)
    if df.empty:
        return pd.DataFrame()
    t = df[df["状況"] == "提供"].copy()
    t["提供形態"] = pd.to_numeric(t["提供形態"], errors="coerce")
    t["送迎往"]   = pd.to_numeric(t["送迎往"],   errors="coerce").fillna(0)
    t["送迎復"]   = pd.to_numeric(t["送迎復"],   errors="coerce").fillna(0)
    return t.groupby(["受給者証番号", "児童名", "保護者名"]).agg(
        算定日数=("日", "count"),
        短時間_1=("提供形態", lambda x: (x == 1).sum()),
        長時間_2=("提供形態", lambda x: (x == 2).sum()),
        送迎往合計=("送迎往", "sum"),
        送迎復合計=("送迎復", "sum"),
    ).reset_index()


def detect_latest_ym():
    """data/ フォルダから最新月を自動検出"""
    files = sorted(DATA_DIR.glob("実績明細_*.csv"), reverse=True)
    if not files:
        now = datetime.now()
        return f"{now.year}{now.month:02d}"
    return files[0].stem.replace("実績明細_", "")


# ── チェック本体 ────────────────────────────────────────────
def run_billing_checks(ym: str) -> list[dict]:
    results = []
    df     = load_meisai(ym)
    master = load_master()

    # 1. 児童マスター
    if master.empty:
        results.append({"name": "児童マスター", "status": "error", "msg": "児童マスターが空です"})
        return results
    results.append({"name": "児童マスター", "status": "ok", "msg": f"{len(master)}名登録済み"})

    # 2. 実績入力（全員分あるか）
    if df.empty:
        results.append({"name": "実績入力", "status": "error",
                        "msg": f"{ym[:4]}年{ym[4:]}月の実績データがありません"})
        return results

    registered = set(master["受給者証番号"].astype(str))
    entered    = set(df["受給者証番号"].astype(str).unique())
    missing    = registered - entered
    if missing:
        missing_names = []
        for j in missing:
            matched = master[master["受給者証番号"].astype(str) == j]["児童名"]
            if not matched.empty:
                missing_names.append(str(matched.iloc[0]))
        results.append({"name": "実績入力", "status": "warn",
                        "msg": f"未入力 {len(missing)}名: {', '.join(missing_names)}"})
    else:
        results.append({"name": "実績入力", "status": "ok",
                        "msg": f"全{len(registered)}名分入力済み"})

    # 3. 市町村番号
    no_muni = master[master["市町村番号"].astype(str).str.strip() == ""]["児童名"].tolist()
    if no_muni:
        results.append({"name": "市町村番号", "status": "error",
                        "msg": f"未設定 {len(no_muni)}名: {', '.join(no_muni)}"})
    else:
        results.append({"name": "市町村番号", "status": "ok", "msg": "全員設定済み"})

    # 4. 提供形態（空欄チェック）
    service_df = df[df["状況"] == "提供"].copy()
    bad_katachi = service_df[
        service_df["提供形態"].astype(str).str.strip().isin(["", "nan", "None"])
    ]
    if not bad_katachi.empty:
        names = bad_katachi["児童名"].unique().tolist()
        results.append({"name": "提供形態", "status": "error",
                        "msg": f"未入力 {len(bad_katachi)}件 ({', '.join(names)})"})
    else:
        results.append({"name": "提供形態", "status": "ok",
                        "msg": f"全{len(service_df)}件入力済み"})

    # 5. 提供時間（空欄チェック）
    bad_time = service_df[
        service_df["開始時間"].astype(str).str.strip().isin(["", "nan", "None"]) |
        service_df["終了時間"].astype(str).str.strip().isin(["", "nan", "None"])
    ]
    if not bad_time.empty:
        names = bad_time["児童名"].unique().tolist()
        results.append({"name": "提供時間", "status": "warn",
                        "msg": f"時間未入力 {len(bad_time)}件 ({', '.join(names)})"})
    else:
        results.append({"name": "提供時間", "status": "ok", "msg": "全件入力済み"})

    # 6. 算定日数（0日チェック）
    summary = get_summary(ym)
    if not summary.empty:
        zero_days = summary[summary["算定日数"] == 0]["児童名"].tolist()
        if zero_days:
            results.append({"name": "算定日数", "status": "warn",
                            "msg": f"算定0日 {len(zero_days)}名: {', '.join(zero_days)}"})
        else:
            results.append({"name": "算定日数", "status": "ok",
                            "msg": f"合計 {int(summary['算定日数'].sum())}日"})

    # 7. 送迎整合性（送迎あり・時間なし）
    sougei_df = service_df[
        (service_df["送迎往"].astype(str) == "1") |
        (service_df["送迎復"].astype(str) == "1")
    ]
    sougei_no_time = sougei_df[
        sougei_df["開始時間"].astype(str).str.strip().isin(["", "nan", "None"]) |
        sougei_df["終了時間"].astype(str).str.strip().isin(["", "nan", "None"])
    ]
    if not sougei_no_time.empty:
        names = sougei_no_time["児童名"].unique().tolist()
        results.append({"name": "送迎整合性", "status": "warn",
                        "msg": f"送迎あり・時間未入力 {len(sougei_no_time)}件 ({', '.join(names)})"})
    else:
        results.append({"name": "送迎整合性", "status": "ok",
                        "msg": f"送迎 {len(sougei_df)}件すべて正常"})

    return results


def print_results(ym, results):
    """ターミナル用の表示"""
    icon = {"ok": "✅", "warn": "⚠️", "error": "❌"}
    errors = sum(1 for r in results if r["status"] == "error")
    warns  = sum(1 for r in results if r["status"] == "warn")

    print(f"\n{'='*50}")
    print(f"  サードストリート 請求前チェック")
    print(f"  対象: {ym[:4]}年{ym[4:]}月")
    print(f"{'='*50}")
    for r in results:
        print(f"  {icon[r['status']]}  {r['name']:<14} {r['msg']}")
    print(f"{'='*50}")
    if errors > 0:
        print(f"  結果: ❌ エラー {errors}件 / ⚠️ 注意 {warns}件 — 修正が必要です")
    elif warns > 0:
        print(f"  結果: ⚠️ 注意 {warns}件 — 確認してください")
    else:
        print(f"  結果: ✅ すべて問題なし — CSV生成に進めます")
    print()


if __name__ == "__main__":
    ym = sys.argv[1] if len(sys.argv) > 1 else detect_latest_ym()
    print(f"チェック対象: {ym[:4]}年{ym[4:]}月")

    results = run_billing_checks(ym)
    print_results(ym, results)

    # Discord通知（Webhook URL が設定されていれば送信）
    from discord_notify import send_check_result
    ok, msg = send_check_result(ym, results)
    if ok:
        print(f"Discord通知: {msg}")
    else:
        print(f"Discord通知スキップ: {msg}")

    # エラーがあれば終了コード1（CI等での利用を考慮）
    errors = sum(1 for r in results if r["status"] == "error")
    sys.exit(1 if errors > 0 else 0)
