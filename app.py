"""
実績記録票 OCR → 国保連CSV 生成システム
Streamlit アプリ
"""

import os
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
    "kokuhoren_id": "K611",
    "data_type":    "K61",
}

APP_VERSION = "v1.1"

# ============================================================
# OCR（Claude Vision API）
# ============================================================
def pdf_to_png(pdf_bytes: bytes) -> list[bytes]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    images = []
    for page in doc:
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
    start = text.find("{")
    end   = text.rfind("}") + 1
    if start == -1:
        raise ValueError(f"JSONが見つかりません:\n{text[:300]}")
    data = json.loads(text[start:end])

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

    if csv_path.exists():
        df = pd.read_csv(csv_path, dtype=str)
        df = df[df["受給者証番号"] != str(data["受給者証番号"])]
    else:
        df = pd.DataFrame(columns=MEISAI_COLS)

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
    upsert_master(str(data["受給者証番号"]), data["児童名"])

def load_meisai(year_month: str) -> pd.DataFrame:
    csv_path = get_csv_path(year_month)
    if not csv_path.exists():
        return pd.DataFrame(columns=MEISAI_COLS)
    return pd.read_csv(csv_path, dtype=str)


# ============================================================
# 児童マスター
# ============================================================
MASTER_PATH = DATA_DIR / "児童マスター.csv"
MASTER_COLS = ["受給者証番号", "児童名", "市町村番号", "様式種別番号", "算定時間記載"]

DEFAULT_SHOSHIKI = "0501"
DEFAULT_MUNI     = "212019"
DEFAULT_SANSEI   = "なし"

def load_master() -> pd.DataFrame:
    if not MASTER_PATH.exists():
        return pd.DataFrame(columns=MASTER_COLS)
    df = pd.read_csv(MASTER_PATH, dtype=str)
    for col in MASTER_COLS:
        if col not in df.columns:
            df[col] = ""
    return df[MASTER_COLS]

def save_master(df: pd.DataFrame):
    df.to_csv(MASTER_PATH, index=False, encoding="utf-8-sig")

def get_shoshiki(jukyu_no: str) -> str:
    master = load_master()
    hit = master[master["受給者証番号"] == str(jukyu_no)]
    if not hit.empty and str(hit.iloc[0]["様式種別番号"]).strip():
        return str(hit.iloc[0]["様式種別番号"]).strip()
    return DEFAULT_SHOSHIKI

def get_muni(jukyu_no: str) -> str:
    master = load_master()
    hit = master[master["受給者証番号"] == str(jukyu_no)]
    if not hit.empty and str(hit.iloc[0]["市町村番号"]).strip():
        return str(hit.iloc[0]["市町村番号"]).strip()
    return ""

def get_sansei(jukyu_no: str) -> bool:
    master = load_master()
    hit = master[master["受給者証番号"] == str(jukyu_no)]
    if not hit.empty:
        return str(hit.iloc[0]["算定時間記載"]).strip() == "あり"
    return False

def upsert_master(jukyu_no: str, child_name: str):
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
# 国保連 CSV 生成
# ============================================================
from kokuhoren_template import (
    BASIC_TEMPLATE, DETAIL_TEMPLATE,
    CTRL_QUOTE, BASIC_QUOTE, DETAIL_QUOTE,
)

def _line(fields: list, quote_idx: set) -> str:
    parts = []
    for i, v in enumerate(fields):
        s = str(v)
        parts.append('"' + s + '"' if i in quote_idx else s)
    return ",".join(parts)

def _next_month(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[4:6])
    m += 1
    if m > 12:
        y += 1; m = 1
    return f"{y}{m:02d}"

def _hhmm(t) -> str:
    s = str(t or "").replace(":", "").strip()
    return s.zfill(4) if s else "0000"

def _to_min(hhmm: str) -> int:
    s = str(hhmm or "").replace(":", "").strip().zfill(4)
    if not s.isdigit() or len(s) < 3:
        return 0
    return int(s[:-2]) * 60 + int(s[-2:])

def _min_to_hhmm(m: int, width: int) -> str:
    h, mm = m // 60, m % 60
    return f"{h}{mm:02d}".zfill(width)

def generate_kokuhoren_csv(year_month: str) -> bytes:
    df      = load_meisai(year_month)
    summary = get_summary(year_month)

    if summary.empty:
        raise ValueError(f"{year_month} のデータがありません")

    proc_ym = _next_month(year_month)
    data_lines = []
    rec_no = 1

    for _, t in summary.iterrows():
        jukyu    = str(t["受給者証番号"])
        muni     = get_muni(jukyu)
        shoshiki = get_shoshiki(jukyu)
        sansei   = get_sansei(jukyu)
        is_jihatsu = shoshiki.startswith("03")
        sougei   = int(t["送迎往合計"]) + int(t["送迎復合計"])

        if not muni:
            raise ValueError(f"児童マスターに市町村番号が未設定です：{t['児童名']}（{jukyu}）")

        day_df = df[(df["受給者証番号"] == jukyu) & (df["状況"] == "提供")].copy()
        day_df["日_num"] = pd.to_numeric(day_df["日"], errors="coerce")
        day_df = day_df.sort_values("日_num")

        total_min = 0
        if sansei:
            for _, dr in day_df.iterrows():
                total_min += _to_min(_hhmm(dr["終了時間"])) - _to_min(_hhmm(dr["開始時間"]))

        rec_no += 1
        b = BASIC_TEMPLATE.copy()
        b[1]  = str(rec_no)
        b[4]  = year_month
        b[5]  = muni
        b[6]  = JIGYOSHO["number"]
        b[7]  = jukyu
        b[8]  = shoshiki
        b[20] = _min_to_hhmm(total_min, 5) if sansei else "00000"
        b[35] = str(sougei)
        data_lines.append(_line(b, BASIC_QUOTE))

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
            d[10] = str(int(day_row["日_num"]))
            d[15] = start
            d[16] = end
            d[17] = _min_to_hhmm(_to_min(end) - _to_min(start), 4) if sansei else "0000"
            d[22] = str(int(float(day_row["送迎往"] or 0)))
            d[23] = str(int(float(day_row["送迎復"] or 0)))
            keitai = str(day_row["提供形態"] or "").strip()
            d[35] = "0" if is_jihatsu else (keitai if keitai else "1")
            data_lines.append(_line(d, DETAIL_QUOTE))

    data_count = len(data_lines)

    control = [
        "1", "1", "0", str(data_count),
        JIGYOSHO["data_type"],
        "0",
        JIGYOSHO["number"],
        "0",
        "1",
        proc_ym,
        "",
    ]
    ctrl_line  = _line(control, CTRL_QUOTE)
    end_line   = "3," + str(rec_no + 1)
    all_lines  = [ctrl_line] + data_lines + [end_line]
    csv_str    = "\r\n".join(all_lines) + "\r\n"
    return csv_str.encode("cp932", errors="replace")


# ============================================================
# Streamlit UI
# ============================================================
st.set_page_config(
    page_title="らくらく請求",
    page_icon="🏥",
    layout="wide",
    initial_sidebar_state="expanded"
)

# カスタムCSS
st.markdown("""
<style>
#MainMenu {visibility: hidden;}
footer     {visibility: hidden;}
header     {visibility: hidden;}

/* メインタイトル */
.main-title {
    background: linear-gradient(135deg, #1a3a5c 0%, #2c5f8a 100%);
    color: white;
    padding: 20px 28px;
    border-radius: 12px;
    margin-bottom: 24px;
}

/* ステップヘッダー */
.step-header {
    background: #eef2f7;
    border-left: 4px solid #2c5f8a;
    padding: 10px 16px;
    border-radius: 0 8px 8px 0;
    margin: 24px 0 14px 0;
    font-weight: bold;
    font-size: 15px;
    color: #1a3a5c;
}

/* メトリクスカード */
[data-testid="metric-container"] {
    background: white;
    border: 1px solid #e2e8f0;
    border-radius: 12px;
    padding: 16px;
    box-shadow: 0 2px 6px rgba(0,0,0,0.06);
}

/* ファイルアップローダー */
[data-testid="stFileUploadDropzone"] {
    border: 2px dashed #2c5f8a !important;
    border-radius: 10px !important;
    background: #f7faff !important;
}

/* タブ */
.stTabs [data-baseweb="tab"] {
    font-size: 14px;
    font-weight: 600;
    padding: 10px 20px;
}
</style>
""", unsafe_allow_html=True)

# メインタイトル
st.markdown("""
<div class="main-title">
    <div style="font-size:26px; font-weight:bold; margin-bottom:4px;">🏥 らくらく請求</div>
    <div style="font-size:13px; opacity:0.85;">実績記録票 OCR → 国保連CSV 自動生成システム</div>
</div>
""", unsafe_allow_html=True)

# ============================================================
# サイドバー
# ============================================================
API_KEY_FILE = DATA_DIR / ".api_key"

def load_saved_api_key() -> str:
    env_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if env_key:
        return env_key
    if API_KEY_FILE.exists():
        return API_KEY_FILE.read_text().strip()
    return ""

if "api_key" not in st.session_state:
    st.session_state["api_key"] = load_saved_api_key()

with st.sidebar:
    st.markdown("""
    <div style="text-align:center; padding:8px 0 20px 0;
         border-bottom:2px solid #e2e8f0; margin-bottom:20px;">
        <div style="font-size:32px;">🏥</div>
        <div style="font-size:20px; font-weight:bold; color:#1a3a5c; margin-top:4px;">
            らくらく請求
        </div>
        <div style="font-size:11px; color:#aaa; margin-top:2px;">
            実績OCR・CSV自動生成
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("**🔑 Anthropic API Key**")
    api_key = st.text_input(
        "", type="password",
        value=st.session_state.get("api_key", ""),
        placeholder="sk-ant-api03-..."
    )
    if api_key:
        st.session_state["api_key"] = api_key

    if api_key:
        st.success("✅ APIキー設定済み")
    else:
        st.warning("⚠️ APIキーを入力してください")

    if api_key and not API_KEY_FILE.exists():
        if st.checkbox("次回から自動入力する"):
            API_KEY_FILE.write_text(api_key)
            st.success("保存しました")
    elif API_KEY_FILE.exists():
        st.caption("✅ APIキー保存済み")
        if st.button("🗑️ 保存キーを削除"):
            API_KEY_FILE.unlink()
            st.session_state["api_key"] = ""
            st.rerun()

    st.divider()

    st.markdown("""
**📖 使い方**

**① OCR・入力** タブ
　実績記録票をアップロード

**② 実績確認** タブ
　データを確認・修正

**③ CSV生成** タブ
　国保連用CSVをダウンロード
    """)

    st.divider()
    st.caption(f"パラザ合同会社　{APP_VERSION}")

# タブ
tab1, tab2, tab3 = st.tabs(["📷  OCR・入力", "📊  実績確認", "📁  CSV生成"])

# ============================================================
# TAB 1: OCR・入力
# ============================================================
with tab1:
    st.markdown('<div class="step-header">📷 実績記録票を読み取る</div>', unsafe_allow_html=True)

    col_up, col_tip = st.columns([3, 1])

    with col_up:
        uploaded_files = st.file_uploader(
            "実績記録票をドラッグ＆ドロップ（JPG・PNG・PDF、複数可）",
            type=["jpg", "jpeg", "png", "webp", "pdf"],
            accept_multiple_files=True,
            help="PDFは1ページ=1児童として自動分割します"
        )

    with col_tip:
        st.markdown("""
        <div style="background:#f7faff; border:1px solid #bee3f8;
             border-radius:8px; padding:14px; font-size:13px; margin-top:8px;">
        <b>💡 撮影のコツ</b><br><br>
        ・明るい場所で撮影する<br>
        ・真上からまっすぐ撮る<br>
        ・複数人分まとめてOK<br>
        ・PDFも対応しています
        </div>
        """, unsafe_allow_html=True)

    if uploaded_files:
        if not api_key:
            st.error("⚠️ サイドバーにAnthropicのAPIキーを入力してください")
        else:
            if st.button("🔍 OCR実行", type="primary"):
                results = []
                for uf in uploaded_files:
                    ext = uf.name.rsplit(".", 1)[-1].lower()
                    raw = uf.read()
                    if ext == "pdf":
                        pages = pdf_to_png(raw)
                        jobs  = [(p, "image/png", f"{uf.name} p.{i+1}") for i, p in enumerate(pages)]
                    else:
                        mime_map = {"jpg":"image/jpeg","jpeg":"image/jpeg",
                                    "png":"image/png","webp":"image/webp"}
                        jobs = [(raw, mime_map.get(ext, "image/jpeg"), uf.name)]

                    for img_bytes, mime, label in jobs:
                        with st.spinner(f"📖 {label} を読み取り中..."):
                            try:
                                data = ocr_jisseki(img_bytes, mime, api_key)
                                save_meisai(data)
                                results.append(("ok", data, label))
                            except Exception as e:
                                results.append(("err", str(e), label))

                ok_count  = sum(1 for r in results if r[0] == "ok")
                err_count = len(results) - ok_count

                if ok_count:
                    st.success(f"✅ {ok_count}件の読み取りが完了しました")
                if err_count:
                    st.error(f"❌ {err_count}件でエラーが発生しました")

                for status, data_or_err, label in results:
                    if status == "ok":
                        d = data_or_err
                        st.markdown(f"""
                        <div style="background:#f0fff4; border:1px solid #9ae6b4;
                             border-radius:8px; padding:14px; margin:6px 0;">
                            <b>✅ {d['児童名']}</b>（{d['受給者証番号']}）<br>
                            <span style="color:#555; font-size:13px;">
                            📅 {d['サービス提供年月'][:4]}年{d['サービス提供年月'][4:]}月　／
                            {d['算定日数']}日　／
                            送迎往 {d['送迎往合計']} ・ 復 {d['送迎復合計']}
                            </span>
                        </div>
                        """, unsafe_allow_html=True)
                    else:
                        st.markdown(f"""
                        <div style="background:#fff5f5; border:1px solid #fc8181;
                             border-radius:8px; padding:14px; margin:6px 0;">
                            <b>❌ {label}</b><br>
                            <span style="color:#c53030; font-size:13px;">{data_or_err}</span>
                        </div>
                        """, unsafe_allow_html=True)

    st.divider()

    with st.expander("✏️ 手動で1件入力する"):
        with st.form("manual_input"):
            col1, col2 = st.columns(2)
            year_month  = col1.text_input("サービス提供年月（YYYYMM）", "202504")
            jukyu_no    = col2.text_input("受給者証番号")
            child_name  = col1.text_input("児童名")
            parent_name = col2.text_input("保護者名")
            date_input  = st.text_input("利用日（カンマ区切り、例：1,4,7,8）")
            katachi     = st.selectbox("提供形態", [2, 1],
                                       format_func=lambda x: f"{x}（{'長時間 3h以上' if x==2 else '短時間 3h未満'}）")
            col3, col4  = st.columns(2)
            start_time  = col3.text_input("開始時間（HH:MM）", "10:00")
            end_time    = col4.text_input("終了時間（HH:MM）", "17:00")
            col5, col6  = st.columns(2)
            sougei_ou   = col5.checkbox("送迎往あり", value=True)
            sougei_fu   = col6.checkbox("送迎復あり", value=True)

            if st.form_submit_button("💾 保存", type="primary"):
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
    st.markdown('<div class="step-header">📊 実績データを確認・修正する</div>', unsafe_allow_html=True)

    csv_files = sorted(DATA_DIR.glob("実績明細_*.csv"), reverse=True)
    year_months = [f.stem.replace("実績明細_", "") for f in csv_files]

    if not year_months:
        st.info("📭 データがありません。TAB1で実績記録票を読み取ってください。")
    else:
        selected_ym = st.selectbox(
            "📅 対象年月", year_months,
            format_func=lambda x: f"{x[:4]}年{x[4:]}月"
        )

        summary = get_summary(selected_ym)

        if not summary.empty:
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("👶 登録児童数",  f"{len(summary)} 名")
            c2.metric("📅 総算定日数",  f"{int(summary['算定日数'].sum())} 日")
            c3.metric("🚌 送迎往合計",  f"{int(summary['送迎往合計'].sum())} 回")
            c4.metric("🚌 送迎復合計",  f"{int(summary['送迎復合計'].sum())} 回")

            st.divider()
            st.markdown("**月次サマリー**")
            st.dataframe(summary, use_container_width=True, hide_index=True)

        st.divider()
        st.markdown("**日別明細**")

        df = load_meisai(selected_ym)
        if not df.empty:
            children  = ["全員"] + sorted(df["児童名"].unique().tolist())
            sel_child = st.selectbox("👶 児童フィルター", children)
            show_df   = df if sel_child == "全員" else df[df["児童名"] == sel_child]
            edited    = st.data_editor(show_df, use_container_width=True, hide_index=True)

            if st.button("💾 変更を保存"):
                edited.to_csv(get_csv_path(selected_ym), index=False, encoding="utf-8-sig")
                st.success("✅ 保存しました")

# ============================================================
# TAB 3: 国保連CSV生成
# ============================================================
with tab3:
    csv_files2   = sorted(DATA_DIR.glob("実績明細_*.csv"), reverse=True)
    year_months2 = [f.stem.replace("実績明細_", "") for f in csv_files2]

    if not year_months2:
        st.info("📭 データがありません。TAB1で実績を入力してください。")
    else:
        selected_ym2 = st.selectbox(
            "📅 対象年月", year_months2, key="csv_ym",
            format_func=lambda x: f"{x[:4]}年{x[4:]}月"
        )

        # ── STEP 1 ──
        st.markdown('<div class="step-header">⚙️ STEP 1 ｜ 児童マスターを確認する</div>',
                    unsafe_allow_html=True)
        st.caption("市町村番号 = 受給者証に記載の6桁　／　様式種別番号 = 放デイ: 0501 ／ 児発: 0301")

        summary2 = get_summary(selected_ym2)
        master   = load_master()

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
        if st.button("💾 マスターを保存"):
            save_master(master_edited[MASTER_COLS])
            st.success("✅ 児童マスターを保存しました")
            st.rerun()

        # ── STEP 2 ──
        st.markdown('<div class="step-header">✅ STEP 2 ｜ 対象データを確認する</div>',
                    unsafe_allow_html=True)

        if not summary2.empty:
            disp = summary2.copy()
            disp["市町村番号"]   = disp["受給者証番号"].apply(lambda x: get_muni(str(x)))
            disp["様式種別番号"] = disp["受給者証番号"].apply(lambda x: get_shoshiki(str(x)))
            st.dataframe(disp, use_container_width=True, hide_index=True)

            missing = [r["児童名"] for _, r in disp.iterrows()
                       if not get_muni(str(r["受給者証番号"]))]
            if missing:
                st.warning(f"⚠️ 市町村番号が未設定：{', '.join(missing)}　→ STEP 1 で設定してください")
            else:
                st.success("✅ 全児童の市町村番号が設定されています")

            # ── STEP 3 ──
            st.markdown('<div class="step-header">📁 STEP 3 ｜ CSV を生成・ダウンロードする</div>',
                        unsafe_allow_html=True)

            proc_ym = _next_month(selected_ym2)
            col_info, col_btn = st.columns([2, 1])

            with col_info:
                st.markdown(f"""
| 項目 | 内容 |
|------|------|
| 文字コード | Shift_JIS |
| 請求月 | {proc_ym[:4]}年{proc_ym[4:]}月 |
| 事業所番号 | {JIGYOSHO['number']} |
| 対象児童数 | {len(summary2)} 名 |
                """)

            with col_btn:
                st.markdown("<br>", unsafe_allow_html=True)
                generate_disabled = bool(missing)
                if st.button("📁 CSV生成・ダウンロード", type="primary",
                             disabled=generate_disabled):
                    try:
                        csv_bytes = generate_kokuhoren_csv(selected_ym2)
                        file_name = f"{selected_ym2}_国保連実績_{JIGYOSHO['number']}.csv"
                        st.download_button(
                            label="⬇️ CSVダウンロード",
                            data=csv_bytes,
                            file_name=file_name,
                            mime="text/csv",
                        )
                        st.success(f"✅ {file_name} を生成しました")
                        st.info("📌 このCSVを国保連の取込送信システムにインポートしてください")
                    except Exception as e:
                        st.error(f"❌ エラー: {e}")
