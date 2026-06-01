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
    proc_ym    = _next_month(year_month)
    data_lines = []
    rec_no     = 1
    for _, t in summary.iterrows():
        jukyu      = str(t["受給者証番号"])
        muni       = get_muni(jukyu)
        shoshiki   = get_shoshiki(jukyu)
        sansei     = get_sansei(jukyu)
        is_jihatsu = shoshiki.startswith("03")
        sougei     = int(t["送迎往合計"]) + int(t["送迎復合計"])
        if not muni:
            raise ValueError(f"市町村番号が未設定です：{t['児童名']}（{jukyu}）")
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
            d[35]  = "0" if is_jihatsu else (keitai if keitai else "1")
            data_lines.append(_line(d, DETAIL_QUOTE))
    data_count = len(data_lines)
    control   = ["1","1","0",str(data_count),JIGYOSHO["data_type"],"0",
                 JIGYOSHO["number"],"0","1",proc_ym,""]
    ctrl_line = _line(control, CTRL_QUOTE)
    end_line  = "3," + str(rec_no + 1)
    all_lines = [ctrl_line] + data_lines + [end_line]
    csv_str   = "\r\n".join(all_lines) + "\r\n"
    return csv_str.encode("cp932", errors="replace")


# ============================================================
# Streamlit UI  —  Apple Design
# ============================================================
st.set_page_config(
    page_title="らくらく請求",
    page_icon="🏥",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown("""
<style>
/* ── Base ── */
html, body, [class*="css"] {
    font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue",
                 "Hiragino Sans", "Yu Gothic UI", Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
}
#MainMenu, footer, header { visibility: hidden; }
.stApp { background: #F2F2F7; }
.main .block-container { padding-top: 36px; padding-bottom: 48px; }

/* ── Sidebar ── */
section[data-testid="stSidebar"] {
    background: #FFFFFF;
    border-right: 1px solid #D1D1D6;
}
section[data-testid="stSidebar"] .stTextInput input {
    border-radius: 10px;
    border: 1.5px solid #D1D1D6;
    font-size: 14px;
    padding: 8px 12px;
    background: #F9F9F9;
}
section[data-testid="stSidebar"] .stTextInput input:focus {
    border-color: #007AFF;
    background: #FFF;
}

/* ── Tabs ── */
.stTabs [data-baseweb="tab-list"] {
    background: #E5E5EA;
    border-radius: 10px;
    padding: 3px;
    gap: 2px;
}
.stTabs [data-baseweb="tab"] {
    font-size: 14px;
    font-weight: 500;
    color: #6E6E73;
    border-radius: 8px;
    padding: 7px 18px;
    border: none;
    background: transparent;
}
.stTabs [aria-selected="true"] {
    background: #FFFFFF !important;
    color: #1C1C1E !important;
    font-weight: 600;
    box-shadow: 0 1px 3px rgba(0,0,0,0.12);
}
.stTabs [data-baseweb="tab-panel"] { padding-top: 24px; }

/* ── Metric cards ── */
[data-testid="metric-container"] {
    background: #FFFFFF;
    border: none;
    border-radius: 16px;
    padding: 20px 22px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.08), 0 0 0 0.5px rgba(0,0,0,0.04);
}
[data-testid="stMetricValue"] {
    font-size: 28px !important;
    font-weight: 700 !important;
    color: #1C1C1E !important;
    letter-spacing: -0.5px;
}
[data-testid="stMetricLabel"] p {
    font-size: 12px !important;
    color: #8E8E93 !important;
    font-weight: 500 !important;
    text-transform: uppercase;
    letter-spacing: 0.3px;
}

/* ── File uploader ── */
[data-testid="stFileUploadDropzone"] {
    background: #FFFFFF !important;
    border: 1.5px dashed #C7C7CC !important;
    border-radius: 14px !important;
    transition: border-color 0.2s;
}

/* ── Buttons ── */
.stButton > button {
    border-radius: 980px;
    font-weight: 500;
    font-size: 15px;
    padding: 9px 22px;
    border: 1.5px solid #D1D1D6;
    background: #FFFFFF;
    color: #1C1C1E;
    transition: all 0.15s ease;
}
.stButton > button:hover {
    background: #F2F2F7;
    border-color: #C7C7CC;
}
.stButton > button[kind="primary"] {
    background: #007AFF;
    color: #FFFFFF;
    border: none;
    box-shadow: 0 2px 8px rgba(0,122,255,0.28);
}
.stButton > button[kind="primary"]:hover {
    background: #0071E3;
}

/* ── Alerts ── */
.stAlert { border-radius: 12px; border: none; }

/* ── Data editor / DataFrame ── */
[data-testid="stDataFrame"], [data-testid="data-grid-canvas"] {
    border-radius: 12px;
    overflow: hidden;
}

/* ── Divider ── */
hr { border: none; border-top: 1px solid #D1D1D6; margin: 20px 0; }
</style>
""", unsafe_allow_html=True)


# ── Helpers ────────────────────────────────────────────────
def section(title: str, subtitle: str = ""):
    sub = f'<div style="font-size:14px;color:#8E8E93;margin-top:4px;">{subtitle}</div>' if subtitle else ""
    st.markdown(f"""
    <div style="margin: 32px 0 18px 0;">
        <div style="font-size:22px;font-weight:700;color:#1C1C1E;letter-spacing:-0.3px;">{title}</div>
        {sub}
    </div>""", unsafe_allow_html=True)

def card_ok(name, jukyu, ym, days, og, ret):
    st.markdown(f"""
    <div style="background:#FFFFFF;border-radius:14px;padding:16px 20px;margin:8px 0;
         box-shadow:0 1px 4px rgba(0,0,0,0.07);">
        <div style="display:flex;align-items:center;gap:10px;">
            <div style="width:8px;height:8px;border-radius:50%;background:#34C759;flex-shrink:0;"></div>
            <div>
                <div style="font-size:15px;font-weight:600;color:#1C1C1E;">{name}</div>
                <div style="font-size:13px;color:#8E8E93;margin-top:3px;">
                    受給者証 {jukyu}&ensp;·&ensp;{ym[:4]}年{ym[4:]}月&ensp;·&ensp;{days}日&ensp;·&ensp;送迎 往{og} / 復{ret}
                </div>
            </div>
        </div>
    </div>""", unsafe_allow_html=True)

def card_err(label, msg):
    st.markdown(f"""
    <div style="background:#FFFFFF;border-radius:14px;padding:16px 20px;margin:8px 0;
         box-shadow:0 1px 4px rgba(0,0,0,0.07);">
        <div style="display:flex;align-items:center;gap:10px;">
            <div style="width:8px;height:8px;border-radius:50%;background:#FF3B30;flex-shrink:0;"></div>
            <div>
                <div style="font-size:15px;font-weight:600;color:#1C1C1E;">{label}</div>
                <div style="font-size:13px;color:#FF3B30;margin-top:3px;">{msg}</div>
            </div>
        </div>
    </div>""", unsafe_allow_html=True)


# ── Page title ─────────────────────────────────────────────
st.markdown("""
<div style="margin-bottom:8px;">
    <div style="font-size:34px;font-weight:700;color:#1C1C1E;letter-spacing:-0.5px;">
        らくらく請求
    </div>
    <div style="font-size:16px;color:#8E8E93;margin-top:6px;font-weight:400;">
        実績記録票の読み取りから国保連CSVの生成まで
    </div>
</div>
""", unsafe_allow_html=True)

st.divider()


# ── API key management ──────────────────────────────────────
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


# ── Sidebar ─────────────────────────────────────────────────
with st.sidebar:
    st.markdown("""
    <div style="padding:20px 0 22px 0;border-bottom:1px solid #E5E5EA;margin-bottom:22px;">
        <div style="font-size:18px;font-weight:700;color:#1C1C1E;letter-spacing:-0.2px;">
            らくらく請求
        </div>
        <div style="font-size:12px;color:#8E8E93;margin-top:3px;">
            障害福祉請求 自動化ツール
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown('<div style="font-size:13px;font-weight:600;color:#1C1C1E;margin-bottom:6px;">API Key</div>',
                unsafe_allow_html=True)
    api_key = st.text_input(
        "", type="password",
        value=st.session_state.get("api_key", ""),
        placeholder="sk-ant-api03-...",
        label_visibility="collapsed"
    )
    if api_key:
        st.session_state["api_key"] = api_key

    if api_key:
        st.markdown('<div style="font-size:13px;color:#34C759;margin:6px 0 14px 0;">● 設定済み</div>',
                    unsafe_allow_html=True)
    else:
        st.markdown('<div style="font-size:13px;color:#FF9500;margin:6px 0 14px 0;">● 未設定</div>',
                    unsafe_allow_html=True)

    if api_key and not API_KEY_FILE.exists():
        if st.checkbox("次回から自動入力する", key="save_key"):
            API_KEY_FILE.write_text(api_key)
    elif API_KEY_FILE.exists():
        st.caption("保存済み")
        if st.button("削除"):
            API_KEY_FILE.unlink()
            st.session_state["api_key"] = ""
            st.rerun()

    st.divider()

    st.markdown("""
<div style="font-size:13px;color:#3C3C43;line-height:2.1;">
<span style="font-weight:600;color:#1C1C1E;">① OCR・入力</span><br>
<span style="color:#8E8E93;padding-left:14px;">実績記録票をアップロード</span><br>
<span style="font-weight:600;color:#1C1C1E;">② 実績確認</span><br>
<span style="color:#8E8E93;padding-left:14px;">データを確認・修正</span><br>
<span style="font-weight:600;color:#1C1C1E;">③ CSV生成</span><br>
<span style="color:#8E8E93;padding-left:14px;">国保連CSVをダウンロード</span>
</div>
    """, unsafe_allow_html=True)

    st.divider()
    st.markdown(f'<div style="font-size:11px;color:#C7C7CC;">パラザ合同会社　{APP_VERSION}</div>',
                unsafe_allow_html=True)


# ── Tabs ────────────────────────────────────────────────────
tab1, tab2, tab3 = st.tabs(["OCR・入力", "実績確認", "CSV生成"])


# ============================================================
# TAB 1
# ============================================================
with tab1:
    section("実績記録票を読み取る", "写真またはPDFをアップロードしてデータを自動抽出します")

    col_up, col_tip = st.columns([3, 1])

    with col_up:
        uploaded_files = st.file_uploader(
            "ファイルをドラッグ＆ドロップ、または選択",
            type=["jpg", "jpeg", "png", "webp", "pdf"],
            accept_multiple_files=True,
            help="PDFは1ページ=1児童として自動分割します",
            label_visibility="visible"
        )

    with col_tip:
        st.markdown("""
        <div style="background:#FFFFFF;border-radius:14px;padding:18px 20px;
             box-shadow:0 1px 4px rgba(0,0,0,0.07);margin-top:8px;">
            <div style="font-size:13px;font-weight:600;color:#1C1C1E;margin-bottom:10px;">
                撮影のコツ
            </div>
            <div style="font-size:13px;color:#8E8E93;line-height:1.9;">
                明るい場所で撮影する<br>
                真上からまっすぐ撮る<br>
                複数人分まとめてOK<br>
                PDFにも対応
            </div>
        </div>
        """, unsafe_allow_html=True)

    if uploaded_files:
        if not api_key:
            st.warning("サイドバーでAPIキーを設定してください")
        else:
            if st.button("読み取り開始", type="primary"):
                results = []
                for uf in uploaded_files:
                    ext = uf.name.rsplit(".", 1)[-1].lower()
                    raw = uf.read()
                    if ext == "pdf":
                        pages = pdf_to_png(raw)
                        jobs  = [(p, "image/png", f"{uf.name}  p.{i+1}") for i, p in enumerate(pages)]
                    else:
                        mime_map = {"jpg":"image/jpeg","jpeg":"image/jpeg",
                                    "png":"image/png","webp":"image/webp"}
                        jobs = [(raw, mime_map.get(ext, "image/jpeg"), uf.name)]

                    for img_bytes, mime, label in jobs:
                        with st.spinner(f"{label} を読み取り中..."):
                            try:
                                data = ocr_jisseki(img_bytes, mime, api_key)
                                save_meisai(data)
                                results.append(("ok", data, label))
                            except Exception as e:
                                results.append(("err", str(e), label))

                ok_count  = sum(1 for r in results if r[0] == "ok")
                err_count = len(results) - ok_count

                st.markdown(f"""
                <div style="background:#FFFFFF;border-radius:14px;padding:16px 20px;
                     margin:16px 0 8px 0;box-shadow:0 1px 4px rgba(0,0,0,0.07);">
                    <span style="font-size:15px;font-weight:600;color:#1C1C1E;">
                        読み取り完了
                    </span>
                    <span style="font-size:14px;color:#34C759;margin-left:12px;">
                        {ok_count}件 成功
                    </span>
                    {"<span style='font-size:14px;color:#FF3B30;margin-left:8px;'>" + str(err_count) + "件 失敗</span>" if err_count else ""}
                </div>
                """, unsafe_allow_html=True)

                for status, data_or_err, label in results:
                    if status == "ok":
                        d = data_or_err
                        card_ok(d["児童名"], d["受給者証番号"], d["サービス提供年月"],
                                d["算定日数"], d["送迎往合計"], d["送迎復合計"])
                    else:
                        card_err(label, str(data_or_err))

    st.divider()

    with st.expander("手動で1件入力する"):
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

            if st.form_submit_button("保存", type="primary"):
                days = [int(d.strip()) for d in date_input.split(",") if d.strip()]
                manual_data = {
                    "受給者証番号": jukyu_no, "児童名": child_name,
                    "保護者名": parent_name, "サービス提供年月": year_month,
                    "契約支給量": 0,
                    "実績": [{"日": d, "提供形態": katachi,
                              "開始時間": start_time, "終了時間": end_time,
                              "送迎往": 1 if sougei_ou else 0,
                              "送迎復": 1 if sougei_fu else 0,
                              "欠席": False} for d in days],
                    "算定日数": len(days),
                    "送迎往合計": len(days) if sougei_ou else 0,
                    "送迎復合計": len(days) if sougei_fu else 0,
                }
                save_meisai(manual_data)
                st.success(f"{child_name}（{len(days)}日）を保存しました")


# ============================================================
# TAB 2
# ============================================================
with tab2:
    section("実績データを確認する", "OCRで読み取ったデータを確認・修正できます")

    csv_files   = sorted(DATA_DIR.glob("実績明細_*.csv"), reverse=True)
    year_months = [f.stem.replace("実績明細_", "") for f in csv_files]

    if not year_months:
        st.markdown("""
        <div style="background:#FFFFFF;border-radius:16px;padding:40px;text-align:center;
             box-shadow:0 1px 4px rgba(0,0,0,0.07);">
            <div style="font-size:17px;font-weight:600;color:#1C1C1E;">データがありません</div>
            <div style="font-size:14px;color:#8E8E93;margin-top:8px;">
                「OCR・入力」タブで実績記録票を読み取ってください
            </div>
        </div>
        """, unsafe_allow_html=True)
    else:
        selected_ym = st.selectbox(
            "対象年月", year_months,
            format_func=lambda x: f"{x[:4]}年{x[4:]}月"
        )

        summary = get_summary(selected_ym)

        if not summary.empty:
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("児童数",    f"{len(summary)}")
            c2.metric("総算定日数", f"{int(summary['算定日数'].sum())}")
            c3.metric("送迎往",    f"{int(summary['送迎往合計'].sum())}")
            c4.metric("送迎復",    f"{int(summary['送迎復合計'].sum())}")

            st.markdown('<div style="height:8px;"></div>', unsafe_allow_html=True)
            st.markdown('<div style="font-size:14px;font-weight:600;color:#1C1C1E;margin-bottom:8px;">月次サマリー</div>',
                        unsafe_allow_html=True)
            st.dataframe(summary, use_container_width=True, hide_index=True)

        st.divider()
        st.markdown('<div style="font-size:14px;font-weight:600;color:#1C1C1E;margin-bottom:8px;">日別明細</div>',
                    unsafe_allow_html=True)

        df = load_meisai(selected_ym)
        if not df.empty:
            children  = ["全員"] + sorted(df["児童名"].unique().tolist())
            sel_child = st.selectbox("児童", children, label_visibility="collapsed")
            show_df   = df if sel_child == "全員" else df[df["児童名"] == sel_child]
            edited    = st.data_editor(show_df, use_container_width=True, hide_index=True)

            if st.button("変更を保存"):
                edited.to_csv(get_csv_path(selected_ym), index=False, encoding="utf-8-sig")
                st.success("保存しました")


# ============================================================
# TAB 3
# ============================================================
with tab3:
    section("CSVを生成する", "国保連の取込送信システムに使用するCSVファイルを作成します")

    csv_files2   = sorted(DATA_DIR.glob("実績明細_*.csv"), reverse=True)
    year_months2 = [f.stem.replace("実績明細_", "") for f in csv_files2]

    if not year_months2:
        st.markdown("""
        <div style="background:#FFFFFF;border-radius:16px;padding:40px;text-align:center;
             box-shadow:0 1px 4px rgba(0,0,0,0.07);">
            <div style="font-size:17px;font-weight:600;color:#1C1C1E;">データがありません</div>
            <div style="font-size:14px;color:#8E8E93;margin-top:8px;">
                「OCR・入力」タブで実績を入力してください
            </div>
        </div>
        """, unsafe_allow_html=True)
    else:
        selected_ym2 = st.selectbox(
            "対象年月", year_months2, key="csv_ym",
            format_func=lambda x: f"{x[:4]}年{x[4:]}月"
        )

        # Step 1
        st.markdown("""
        <div style="display:flex;align-items:center;gap:10px;margin:28px 0 14px 0;">
            <div style="width:26px;height:26px;border-radius:50%;background:#007AFF;
                 color:white;font-size:13px;font-weight:700;display:flex;
                 align-items:center;justify-content:center;flex-shrink:0;">1</div>
            <div style="font-size:17px;font-weight:600;color:#1C1C1E;">児童マスターを確認する</div>
        </div>
        <div style="font-size:13px;color:#8E8E93;margin-bottom:14px;">
            市町村番号（受給者証記載の6桁）と様式種別番号を確認してください
        </div>
        """, unsafe_allow_html=True)

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
            master, use_container_width=True, hide_index=True, key="master_editor",
            column_config={
                "市町村番号": st.column_config.TextColumn(
                    "市町村番号", help="受給者証記載の6桁（例：212019）", required=True),
                "様式種別番号": st.column_config.SelectboxColumn(
                    "様式種別番号", options=["0501", "0301"],
                    help="放デイ＝0501 / 児発＝0301", required=True),
                "算定時間記載": st.column_config.SelectboxColumn(
                    "算定時間記載", options=["なし", "あり"],
                    help="児発で算定時間を記載する場合のみ「あり」", required=True),
            }
        )
        if st.button("マスターを保存"):
            save_master(master_edited[MASTER_COLS])
            st.success("保存しました")
            st.rerun()

        st.divider()

        # Step 2
        st.markdown("""
        <div style="display:flex;align-items:center;gap:10px;margin:8px 0 14px 0;">
            <div style="width:26px;height:26px;border-radius:50%;background:#007AFF;
                 color:white;font-size:13px;font-weight:700;display:flex;
                 align-items:center;justify-content:center;flex-shrink:0;">2</div>
            <div style="font-size:17px;font-weight:600;color:#1C1C1E;">対象データを確認する</div>
        </div>
        """, unsafe_allow_html=True)

        if not summary2.empty:
            disp = summary2.copy()
            disp["市町村番号"]   = disp["受給者証番号"].apply(lambda x: get_muni(str(x)))
            disp["様式種別番号"] = disp["受給者証番号"].apply(lambda x: get_shoshiki(str(x)))
            st.dataframe(disp, use_container_width=True, hide_index=True)

            missing = [r["児童名"] for _, r in disp.iterrows()
                       if not get_muni(str(r["受給者証番号"]))]
            if missing:
                st.warning(f"市町村番号が未設定の児童があります：{', '.join(missing)}")
            else:
                st.success("すべての児童の市町村番号が設定されています")

            st.divider()

            # Step 3
            proc_ym = _next_month(selected_ym2)
            st.markdown(f"""
            <div style="display:flex;align-items:center;gap:10px;margin:8px 0 14px 0;">
                <div style="width:26px;height:26px;border-radius:50%;background:#007AFF;
                     color:white;font-size:13px;font-weight:700;display:flex;
                     align-items:center;justify-content:center;flex-shrink:0;">3</div>
                <div style="font-size:17px;font-weight:600;color:#1C1C1E;">CSVを生成する</div>
            </div>
            <div style="background:#FFFFFF;border-radius:14px;padding:18px 22px;
                 margin-bottom:20px;box-shadow:0 1px 4px rgba(0,0,0,0.07);">
                <table style="font-size:14px;color:#3C3C43;border-collapse:collapse;width:auto;">
                    <tr><td style="color:#8E8E93;padding:4px 20px 4px 0;">請求月</td>
                        <td style="font-weight:500;">{proc_ym[:4]}年{proc_ym[4:]}月</td></tr>
                    <tr><td style="color:#8E8E93;padding:4px 20px 4px 0;">事業所番号</td>
                        <td style="font-weight:500;">{JIGYOSHO['number']}</td></tr>
                    <tr><td style="color:#8E8E93;padding:4px 20px 4px 0;">対象児童</td>
                        <td style="font-weight:500;">{len(summary2)}名</td></tr>
                    <tr><td style="color:#8E8E93;padding:4px 20px 4px 0;">文字コード</td>
                        <td style="font-weight:500;">Shift_JIS</td></tr>
                </table>
            </div>
            """, unsafe_allow_html=True)

            if st.button("CSVを生成してダウンロード", type="primary",
                         disabled=bool(missing)):
                try:
                    csv_bytes = generate_kokuhoren_csv(selected_ym2)
                    file_name = f"{selected_ym2}_国保連実績_{JIGYOSHO['number']}.csv"
                    st.download_button(
                        label="ダウンロード",
                        data=csv_bytes,
                        file_name=file_name,
                        mime="text/csv",
                    )
                    st.success(f"{file_name} を生成しました。国保連の取込送信システムにインポートしてください。")
                except Exception as e:
                    st.error(f"エラーが発生しました：{e}")
