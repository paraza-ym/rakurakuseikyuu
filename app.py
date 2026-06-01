"""
実績記録票 OCR → 国保連CSV 生成システム
Streamlit アプリ
"""

import streamlit as st
import anthropic
import base64
import json
import csv
import io
import pandas as pd
import fitz  # PyMuPDF
from pathlib import Path
from datetime import datetime

# ============================================================
# 設定
# ============================================================
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

JIGYOSHO = {
    "number":       "2150600183",
    "kokuhoren_id": "K611",       # 交換情報識別番号（データ行）4桁
    "data_type":    "K61",        # データ種別（コントロールレコード）3桁
}

# ============================================================
# OCR（Claude Vision API）
# ============================================================
def pdf_to_png(pdf_bytes: bytes) -> list[bytes]:
    """PDFの各ページをPNG画像に変換して返す"""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    images = []
    for page in doc:
        # 解像度2倍で読み取り精度を上げる
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
        images.append(pix.tobytes("png"))
    doc.close()
    return images


def ocr_jisseki(image_bytes: bytes, mime_type: str, api_key: str) -> dict:
    client = anthropic.Anthropic(api_key=api_key)
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    prompt = """これは放課後等デイサービスの「提供実績記録票」（手書き）です。
以下の情報を正確にJSON形式で抽出してください。

【出力形式】以下のJSONのみ返してください（説明文不要）：
{
  "受給者証番号": "数字のみ10桁",
  "児童名": "漢字氏名",
  "保護者名": "漢字氏名（保護者欄）",
  "サービス提供年月": "YYYYMM形式（例：令和7年4月→202504）",
  "契約支給量": 数字,
  "実績": [
    {
      "日": 数字,
      "提供形態": 数字またはnull,
      "開始時間": "HH:MM"またはnull,
      "終了時間": "HH:MM"またはnull,
      "送迎往": 1または0,
      "送迎復": 1または0,
      "欠席": trueまたはfalse
    }
  ]
}

【読み取りルール】
- 「欠」「欠席」と書かれた日 → 欠席=true、提供形態/時間=null
- 空欄の日（記録なし）→ 実績配列に含めない
- 提供形態：「サービス提供状況」「提供形態」欄に書かれた数字（1または2）をそのまま読み取る
- 送迎往・復列に「1」「/」「✓」がある → 1、ない → 0
- 時間は「10:20」「17:00」の形式
- 受給者証番号はスペースを除去した数字のみ
- 保護者名は「（　様）」の括弧内の氏名"""

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2000,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime_type, "data": b64}
                },
                {"type": "text", "text": prompt}
            ]
        }]
    )

    text = response.content[0].text
    # JSONを抽出
    start = text.find("{")
    end   = text.rfind("}") + 1
    if start == -1:
        raise ValueError(f"JSONが見つかりません:\n{text[:300]}")
    data = json.loads(text[start:end])

    # 集計
    data["算定日数"]   = sum(1 for r in data["実績"] if not r["欠席"])
    data["送迎往合計"] = sum(r["送迎往"] for r in data["実績"])
    data["送迎復合計"] = sum(r["送迎復"] for r in data["実績"])
    return data


# ============================================================
# データ保存 / 読み込み
# ============================================================
def get_csv_path(year_month: str) -> Path:
    return DATA_DIR / f"実績明細_{year_month}.csv"

MEISAI_COLS = [
    "サービス提供年月", "受給者証番号", "児童名", "保護者名",
    "日", "提供形態", "開始時間", "終了時間", "送迎往", "送迎復", "状況"
]

def save_meisai(data: dict):
    year_month = data["サービス提供年月"]
    csv_path = get_csv_path(year_month)

    # 既存データ読み込み
    if csv_path.exists():
        df = pd.read_csv(csv_path, dtype=str)
        # 同じ受給者証番号のデータを削除（上書き）
        df = df[df["受給者証番号"] != str(data["受給者証番号"])]
    else:
        df = pd.DataFrame(columns=MEISAI_COLS)

    # 新データ追加
    rows = []
    for r in data["実績"]:
        rows.append({
            "サービス提供年月": year_month,
            "受給者証番号":     str(data["受給者証番号"]),
            "児童名":           data["児童名"],
            "保護者名":         data["保護者名"],
            "日":               r["日"],
            "提供形態":         "" if r["欠席"] else (r["提供形態"] or ""),
            "開始時間":         "" if r["欠席"] else (r["開始時間"] or ""),
            "終了時間":         "" if r["欠席"] else (r["終了時間"] or ""),
            "送迎往":           r["送迎往"],
            "送迎復":           r["送迎復"],
            "状況":             "欠席" if r["欠席"] else "提供",
        })

    new_df = pd.DataFrame(rows, columns=MEISAI_COLS)
    df = pd.concat([df, new_df], ignore_index=True)
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    # 児童マスターへ自動登録（様式種別番号の管理用）
    upsert_master(str(data["受給者証番号"]), data["児童名"])

def load_meisai(year_month: str) -> pd.DataFrame:
    csv_path = get_csv_path(year_month)
    if not csv_path.exists():
        return pd.DataFrame(columns=MEISAI_COLS)
    return pd.read_csv(csv_path, dtype=str)


# ============================================================
# 児童マスター（受給者証番号 → 市町村番号・様式種別番号）
# ============================================================
MASTER_PATH = DATA_DIR / "児童マスター.csv"
MASTER_COLS = ["受給者証番号", "児童名", "市町村番号", "様式種別番号", "算定時間記載"]

DEFAULT_SHOSHIKI = "0501"   # 放課後等デイサービス実績記録票（児発は0301）
DEFAULT_MUNI     = "212019" # 既知の市町村番号（児童ごとに要確認・編集可）
DEFAULT_SANSEI   = "なし"    # 算定時間記載（児発の一部のみ「あり」）

def load_master() -> pd.DataFrame:
    if not MASTER_PATH.exists():
        return pd.DataFrame(columns=MASTER_COLS)
    df = pd.read_csv(MASTER_PATH, dtype=str)
    # 旧フォーマット（区分列）からの移行
    for col in MASTER_COLS:
        if col not in df.columns:
            df[col] = ""
    return df[MASTER_COLS]

def save_master(df: pd.DataFrame):
    df.to_csv(MASTER_PATH, index=False, encoding="utf-8-sig")

def get_shoshiki(jukyu_no: str) -> str:
    """受給者証番号 → 様式種別番号（未登録なら0501）"""
    master = load_master()
    hit = master[master["受給者証番号"] == str(jukyu_no)]
    if not hit.empty and str(hit.iloc[0]["様式種別番号"]).strip():
        return str(hit.iloc[0]["様式種別番号"]).strip()
    return DEFAULT_SHOSHIKI

def get_muni(jukyu_no: str) -> str:
    """受給者証番号 → 市町村番号（受給者証記載の市町村）"""
    master = load_master()
    hit = master[master["受給者証番号"] == str(jukyu_no)]
    if not hit.empty and str(hit.iloc[0]["市町村番号"]).strip():
        return str(hit.iloc[0]["市町村番号"]).strip()
    return ""

def get_sansei(jukyu_no: str) -> bool:
    """受給者証番号 → 算定時間を記載するか（児発の一部のみ True）"""
    master = load_master()
    hit = master[master["受給者証番号"] == str(jukyu_no)]
    if not hit.empty:
        return str(hit.iloc[0]["算定時間記載"]).strip() == "あり"
    return False

def upsert_master(jukyu_no: str, child_name: str):
    """OCR時に児童マスターへ自動登録（様式種別=0501・市町村=デフォルト）"""
    master = load_master()
    if str(jukyu_no) not in master["受給者証番号"].astype(str).values:
        new = pd.DataFrame([{
            "受給者証番号": str(jukyu_no),
            "児童名":       child_name,
            "市町村番号":   DEFAULT_MUNI,
            "様式種別番号": DEFAULT_SHOSHIKI,
            "算定時間記載": DEFAULT_SANSEI,
        }])
        master = pd.concat([master, new], ignore_index=True)
        save_master(master)

def get_summary(year_month: str) -> pd.DataFrame:
    df = load_meisai(year_month)
    if df.empty:
        return pd.DataFrame()

    teikyo = df[df["状況"] == "提供"].copy()
    teikyo["提供形態"] = pd.to_numeric(teikyo["提供形態"], errors="coerce")
    teikyo["送迎往"]   = pd.to_numeric(teikyo["送迎往"],   errors="coerce").fillna(0)
    teikyo["送迎復"]   = pd.to_numeric(teikyo["送迎復"],   errors="coerce").fillna(0)

    summary = teikyo.groupby(["受給者証番号", "児童名", "保護者名"]).agg(
        算定日数=("日", "count"),
        短時間_1=("提供形態", lambda x: (x == 1).sum()),
        長時間_2=("提供形態", lambda x: (x == 2).sum()),
        送迎往合計=("送迎往", "sum"),
        送迎復合計=("送迎復", "sum"),
    ).reset_index()

    summary["送迎往合計"] = summary["送迎往合計"].astype(int)
    summary["送迎復合計"] = summary["送迎復合計"].astype(int)
    return summary


# ============================================================
# 国保連 CSV 生成（実物テンプレート方式・Shift_JIS）
# ============================================================
from kokuhoren_template import (
    BASIC_TEMPLATE, DETAIL_TEMPLATE,
    CTRL_QUOTE, BASIC_QUOTE, DETAIL_QUOTE,
)

def _line(fields: list, quote_idx: set) -> str:
    """指定位置だけ引用符で囲んでカンマ連結"""
    parts = []
    for i, v in enumerate(fields):
        s = str(v)
        parts.append('"' + s + '"' if i in quote_idx else s)
    return ",".join(parts)

def _next_month(ym: str) -> str:
    """YYYYMM → 翌月 YYYYMM（処理対象年月＝請求月）"""
    y, m = int(ym[:4]), int(ym[4:6])
    m += 1
    if m > 12:
        y += 1; m = 1
    return f"{y}{m:02d}"

def _hhmm(t) -> str:
    """'10:00' → '1000'。空なら '0000'"""
    s = str(t or "").replace(":", "").strip()
    return s.zfill(4) if s else "0000"

def _to_min(hhmm: str) -> int:
    """'1000' → 600分"""
    s = str(hhmm or "").replace(":", "").strip().zfill(4)
    if not s.isdigit() or len(s) < 3:
        return 0
    return int(s[:-2]) * 60 + int(s[-2:])

def _min_to_hhmm(m: int, width: int) -> str:
    """分 → 'HMM'（時+2桁分）を width 桁ゼロ埋め。360→'0600'(4), 2520→'04200'(5)"""
    h, mm = m // 60, m % 60
    return f"{h}{mm:02d}".zfill(width)

def generate_kokuhoren_csv(year_month: str) -> bytes:
    df      = load_meisai(year_month)
    summary = get_summary(year_month)

    if summary.empty:
        raise ValueError(f"{year_month} のデータがありません")

    proc_ym = _next_month(year_month)   # 処理対象年月＝請求月

    data_lines = []
    rec_no = 1  # コントロールレコード = 1

    for _, t in summary.iterrows():
        jukyu    = str(t["受給者証番号"])
        muni     = get_muni(jukyu)        # 市町村番号（児童ごと）
        shoshiki = get_shoshiki(jukyu)    # 様式種別番号（児童ごと）
        sansei   = get_sansei(jukyu)      # 算定時間を記載するか（児発の一部）
        is_jihatsu = shoshiki.startswith("03")   # 児童発達支援
        sougei   = int(t["送迎往合計"]) + int(t["送迎復合計"])

        if not muni:
            raise ValueError(f"児童マスターに市町村番号が未設定です：{t['児童名']}（{jukyu}）")

        # 明細（提供日のみ・日付順）
        day_df = df[(df["受給者証番号"] == jukyu) & (df["状況"] == "提供")].copy()
        day_df["日_num"] = pd.to_numeric(day_df["日"], errors="coerce")
        day_df = day_df.sort_values("日_num")

        # 算定時間合計（児発・算定時間記載ありの場合）
        total_min = 0
        if sansei:
            for _, dr in day_df.iterrows():
                total_min += _to_min(_hhmm(dr["終了時間"])) - _to_min(_hhmm(dr["開始時間"]))

        # ── 基本情報レコード ──
        rec_no += 1
        b = BASIC_TEMPLATE.copy()
        b[1]  = str(rec_no)      # レコード番号
        b[4]  = year_month       # サービス提供年月
        b[5]  = muni             # 市町村番号
        b[6]  = JIGYOSHO["number"]
        b[7]  = jukyu            # 受給者証番号
        b[8]  = shoshiki         # 様式種別番号
        b[20] = _min_to_hhmm(total_min, 5) if sansei else "00000"  # 算定時間数合計
        b[35] = str(sougei)      # 送迎加算 合計
        data_lines.append(_line(b, BASIC_QUOTE))

        # ── 明細情報レコード ──
        for _, day_row in day_df.iterrows():
            rec_no += 1
            start = _hhmm(day_row["開始時間"])
            end   = _hhmm(day_row["終了時間"])
            d = DETAIL_TEMPLATE.copy()
            d[1]  = str(rec_no)
            d[4]  = year_month
            d[5]  = muni
            d[6]  = JIGYOSHO["number"]
            d[7]  = jukyu
            d[8]  = shoshiki
            d[10] = str(int(day_row["日_num"]))               # 日
            d[15] = start                                      # 開始時間
            d[16] = end                                        # 終了時間
            d[17] = _min_to_hhmm(_to_min(end) - _to_min(start), 4) if sansei else "0000"  # 算定時間数
            d[22] = str(int(float(day_row["送迎往"] or 0)))    # 送迎往
            d[23] = str(int(float(day_row["送迎復"] or 0)))    # 送迎復
            keitai = str(day_row["提供形態"] or "").strip()
            # 提供形態：放デイは1/2、児発は0
            d[35] = "0" if is_jihatsu else (keitai if keitai else "1")
            # d[36] 備考（全角スペース）はテンプレートのまま
            data_lines.append(_line(d, DETAIL_QUOTE))

    data_count = len(data_lines)

    # ── コントロールレコード（11項目）──
    control = [
        "1", "1", "0", str(data_count),
        JIGYOSHO["data_type"],   # K61
        "0",
        JIGYOSHO["number"],
        "0",                     # 都道府県番号（0）
        "1",                     # 媒体区分（"1"）
        proc_ym,                 # 処理対象年月＝請求月
        "",                      # 末尾ブランク
    ]
    ctrl_line = _line(control, CTRL_QUOTE)

    # ── エンドレコード ──
    end_line = "3," + str(rec_no + 1)

    all_lines = [ctrl_line] + data_lines + [end_line]
    csv_str = "\r\n".join(all_lines) + "\r\n"

    return csv_str.encode("cp932", errors="replace")


# ============================================================
# Streamlit UI
# ============================================================
st.set_page_config(page_title="実績記録票OCR・CSV生成", page_icon="📋", layout="wide")
st.title("📋 実績記録票 OCR → 国保連CSV 生成")

# APIキー（ローカル保存して次回から自動読み込み）
import os
API_KEY_FILE = DATA_DIR / ".api_key"

def load_saved_api_key() -> str:
    # 1. 環境変数 2. 保存ファイル の順で読み込み
    env_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if env_key:
        return env_key
    if API_KEY_FILE.exists():
        return API_KEY_FILE.read_text().strip()
    return ""

# 初回ロード時に保存済みキーを反映
if "api_key" not in st.session_state:
    st.session_state["api_key"] = load_saved_api_key()

api_key = st.sidebar.text_input("Anthropic API Key", type="password",
                                 value=st.session_state.get("api_key", ""))
if api_key:
    st.session_state["api_key"] = api_key

# 保存チェックボックス
if api_key and not API_KEY_FILE.exists():
    if st.sidebar.checkbox("🔑 このキーを保存（次回から自動入力）"):
        API_KEY_FILE.write_text(api_key)
        st.sidebar.success("保存しました。次回から自動入力されます。")
elif API_KEY_FILE.exists():
    st.sidebar.caption("✅ APIキー保存済み")
    if st.sidebar.button("キーを削除"):
        API_KEY_FILE.unlink()
        st.session_state["api_key"] = ""
        st.rerun()

tab1, tab2, tab3 = st.tabs(["📷 OCR・入力", "📊 実績確認", "📁 CSV生成"])

# ============================================================
# TAB 1: OCR・入力
# ============================================================
with tab1:
    st.header("実績記録票を読み取る")
    st.info("📸 実績記録票の写真をアップロードすると、自動でデータを抽出します。")

    uploaded_files = st.file_uploader(
        "実績記録票をアップロード（画像・PDF、複数可）",
        type=["jpg", "jpeg", "png", "webp", "pdf"],
        accept_multiple_files=True
    )

    if uploaded_files and st.button("🔍 OCR実行", type="primary"):
        if not api_key:
            st.error("Anthropic API Keyを設定してください")
        else:
            for uf in uploaded_files:
                ext = uf.name.rsplit(".", 1)[-1].lower()
                raw = uf.read()

                # PDFは1ページ=1児童として各ページを個別にOCR
                if ext == "pdf":
                    pages = pdf_to_png(raw)
                    jobs  = [(p, "image/png", f"{uf.name} p.{i+1}") for i, p in enumerate(pages)]
                else:
                    mime_map = {"jpg":"image/jpeg","jpeg":"image/jpeg",
                                "png":"image/png","webp":"image/webp"}
                    jobs = [(raw, mime_map.get(ext, "image/jpeg"), uf.name)]

                for img_bytes, mime, label in jobs:
                    with st.spinner(f"{label} を読み取り中..."):
                        try:
                            data = ocr_jisseki(img_bytes, mime, api_key)
                            save_meisai(data)
                            st.success(
                                f"✅ {data['児童名']}（{data['受給者証番号']}）"
                                f" | {data['サービス提供年月']} "
                                f"| {data['算定日数']}日 "
                                f"| 送迎往{data['送迎往合計']}・復{data['送迎復合計']}"
                            )
                        except Exception as e:
                            st.error(f"❌ {label}: {e}")

    # 手動入力フォーム
    with st.expander("✏️ 手動で1件入力"):
        with st.form("manual_input"):
            col1, col2 = st.columns(2)
            year_month  = col1.text_input("サービス提供年月（YYYYMM）", "202504")
            jukyu_no    = col2.text_input("受給者証番号")
            child_name  = col1.text_input("児童名")
            parent_name = col2.text_input("保護者名")
            date_input  = st.text_input("利用日（カンマ区切り、例：1,4,7,8）")
            katachi     = st.selectbox("提供形態", [2, 1], format_func=lambda x: f"{x}（{'長時間3h以上' if x==2 else '短時間3h未満'}）")
            start_time  = st.text_input("開始時間（HH:MM）", "10:00")
            end_time    = st.text_input("終了時間（HH:MM）", "17:00")
            sougei_ou   = st.checkbox("送迎往あり", value=True)
            sougei_fu   = st.checkbox("送迎復あり", value=True)

            if st.form_submit_button("保存"):
                days = [int(d.strip()) for d in date_input.split(",") if d.strip()]
                manual_data = {
                    "受給者証番号": jukyu_no,
                    "児童名": child_name,
                    "保護者名": parent_name,
                    "サービス提供年月": year_month,
                    "契約支給量": 0,
                    "実績": [{
                        "日": d, "提供形態": katachi,
                        "開始時間": start_time, "終了時間": end_time,
                        "送迎往": 1 if sougei_ou else 0,
                        "送迎復": 1 if sougei_fu else 0,
                        "欠席": False
                    } for d in days],
                    "算定日数": len(days),
                    "送迎往合計": len(days) if sougei_ou else 0,
                    "送迎復合計": len(days) if sougei_fu else 0,
                }
                save_meisai(manual_data)
                st.success(f"✅ 保存しました（{child_name} {len(days)}日）")

# ============================================================
# TAB 2: 実績確認・編集
# ============================================================
with tab2:
    st.header("実績データ確認")

    # 利用可能な年月一覧
    csv_files = sorted(DATA_DIR.glob("実績明細_*.csv"), reverse=True)
    year_months = [f.stem.replace("実績明細_", "") for f in csv_files]

    if not year_months:
        st.info("データがありません。TAB1でOCRまたは手動入力してください。")
    else:
        selected_ym = st.selectbox("対象年月", year_months)

        st.subheader("📊 月次サマリー")
        summary = get_summary(selected_ym)
        if not summary.empty:
            st.dataframe(summary, use_container_width=True, hide_index=True)

        st.subheader("📋 日別明細")
        df = load_meisai(selected_ym)
        if not df.empty:
            # 児童フィルター
            children = ["全員"] + sorted(df["児童名"].unique().tolist())
            sel_child = st.selectbox("児童", children)

            show_df = df if sel_child == "全員" else df[df["児童名"] == sel_child]
            edited = st.data_editor(show_df, use_container_width=True, hide_index=True)

            if st.button("💾 変更を保存"):
                edited.to_csv(get_csv_path(selected_ym), index=False, encoding="utf-8-sig")
                st.success("保存しました")

# ============================================================
# TAB 3: 国保連CSV生成
# ============================================================
with tab3:
    st.header("国保連 取込送信CSV 生成")

    csv_files2 = sorted(DATA_DIR.glob("実績明細_*.csv"), reverse=True)
    year_months2 = [f.stem.replace("実績明細_", "") for f in csv_files2]

    if not year_months2:
        st.info("データがありません。先に実績データを入力してください。")
    else:
        selected_ym2 = st.selectbox("対象年月", year_months2, key="csv_ym")

        # ── 児童マスター（市町村番号・様式種別番号・算定時間）──
        st.subheader("⚙️ 児童マスター")
        st.caption("市町村番号＝受給者証記載の6桁。様式種別番号＝放デイ0501 / 児発0301。"
                   "算定時間記載＝児発で算定時間を記載する児童のみ「あり」。")

        summary2 = get_summary(selected_ym2)
        master = load_master()

        # 今月の対象児童をマスターに反映（未登録はデフォルト追加）
        if not summary2.empty:
            for _, r in summary2.iterrows():
                if str(r["受給者証番号"]) not in master["受給者証番号"].astype(str).values:
                    master = pd.concat([master, pd.DataFrame([{
                        "受給者証番号": str(r["受給者証番号"]),
                        "児童名":       r["児童名"],
                        "市町村番号":   DEFAULT_MUNI,
                        "様式種別番号": DEFAULT_SHOSHIKI,
                        "算定時間記載": DEFAULT_SANSEI,
                    }])], ignore_index=True)

        master_edited = st.data_editor(
            master,
            use_container_width=True, hide_index=True, key="master_editor",
            column_config={
                "市町村番号": st.column_config.TextColumn(
                    "市町村番号", help="受給者証記載の市町村番号（6桁・例212019）", required=True,
                ),
                "様式種別番号": st.column_config.SelectboxColumn(
                    "様式種別番号", options=["0501", "0301"],
                    help="放課後等デイ＝0501 / 児童発達支援＝0301", required=True,
                ),
                "算定時間記載": st.column_config.SelectboxColumn(
                    "算定時間記載", options=["なし", "あり"],
                    help="児発で算定時間数を記載する児童のみ「あり」", required=True,
                ),
            }
        )
        if st.button("💾 児童マスターを保存"):
            save_master(master_edited[MASTER_COLS])
            st.success("児童マスターを保存しました")
            st.rerun()

        st.divider()

        # ── 対象データ確認 ──
        if not summary2.empty:
            st.subheader("対象データ確認")
            disp = summary2.copy()
            disp["市町村番号"]   = disp["受給者証番号"].apply(lambda x: get_muni(str(x)))
            disp["様式種別番号"] = disp["受給者証番号"].apply(lambda x: get_shoshiki(str(x)))
            st.dataframe(disp, use_container_width=True, hide_index=True)

            # 市町村番号未設定の警告
            missing = [r["児童名"] for _, r in disp.iterrows() if not get_muni(str(r["受給者証番号"]))]
            if missing:
                st.warning(f"⚠️ 市町村番号が未設定の児童：{', '.join(missing)}　→ 上のマスターで設定してください")

            st.info(f"""
**生成されるCSVの仕様（国保連の実物と同形式）**
- 文字コード：**Shift_JIS**
- 様式種別番号：児童ごとのマスター値（放デイ＝0501）
- 市町村番号：児童ごとのマスター値
- 処理対象年月：請求月（提供月の翌月）
- 事業所番号：{JIGYOSHO['number']}
            """)

            if st.button("📁 CSV生成・ダウンロード", type="primary"):
                try:
                    csv_bytes = generate_kokuhoren_csv(selected_ym2)
                    file_name = f"{selected_ym2}_国保連実績_{JIGYOSHO['number']}.csv"
                    st.download_button(
                        label="⬇️ CSVダウンロード",
                        data=csv_bytes,
                        file_name=file_name,
                        mime="text/csv"
                    )
                    st.success(f"✅ {file_name} を生成しました")
                    st.info("📌 このCSVをそのまま国保連の取込送信システムにインポートできます。")
                except Exception as e:
                    st.error(f"エラー: {e}")
