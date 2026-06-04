"""
実績記録票 OCR → 国保連CSV 生成システム
"""
import os
import base64
import json
from pathlib import Path

import anthropic
import fitz
import pandas as pd
import streamlit as st

# ============================================================
# 設定
# ============================================================
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

JIGYOSHO     = {"number": "2150600183", "kokuhoren_id": "K611", "data_type": "K61"}
APP_VERSION  = "v1.3"
MASTER_PATH  = DATA_DIR / "児童マスター.csv"
MASTER_COLS  = ["受給者証番号", "児童名", "市町村番号", "様式種別番号", "算定時間記載"]
MEISAI_COLS  = ["サービス提供年月", "受給者証番号", "児童名", "保護者名",
                "日", "提供形態", "開始時間", "終了時間", "送迎往", "送迎復", "状況"]
DEFAULT_MUNI     = "212019"
DEFAULT_SHOSHIKI = "0501"
DEFAULT_SANSEI   = "なし"

# ============================================================
# OCR
# ============================================================
def pdf_to_png(pdf_bytes):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = [page.get_pixmap(matrix=fitz.Matrix(2, 2)).tobytes("png") for page in doc]
    doc.close()
    return pages

def ocr_jisseki(image_bytes, mime_type, api_key):
    client = anthropic.Anthropic(api_key=api_key)
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    prompt = """これは放課後等デイサービスの「提供実績記録票」です。
記録票の情報をすべて抽出してください。

【出力形式】以下のJSONのみ返してください（説明文不要）：
{
  "サービス提供年月": "YYYYMM形式（例：令和7年4月→202504）",
  "受給者証番号": "10桁の番号（文字列）",
  "児童名": "お子さんの名前",
  "保護者名": "保護者の名前またはnull",
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
- 提供形態：「サービス提供状況」「提供形態」欄の数字（1または2）をそのまま読み取る
- 送迎往・復列に「1」「/」「✓」がある → 1、ない → 0
- 時間は「10:20」「17:00」の形式"""

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2000,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64}},
            {"type": "text", "text": prompt},
        ]}],
    )
    text  = response.content[0].text
    start = text.find("{")
    end   = text.rfind("}") + 1
    if start == -1:
        raise ValueError("読み取りに失敗しました。写真をより鮮明に撮り直してください。")
    data = json.loads(text[start:end])
    data["保護者名"]   = data.get("保護者名") or ""
    data["算定日数"]   = sum(1 for r in data["実績"] if not r["欠席"])
    data["送迎往合計"] = sum(r["送迎往"] for r in data["実績"])
    data["送迎復合計"] = sum(r["送迎復"] for r in data["実績"])
    return data

# ============================================================
# データ管理
# ============================================================
def _read_csv(path, cols):
    if not Path(path).exists():
        return pd.DataFrame(columns=cols)
    return pd.read_csv(path, dtype=str, keep_default_na=False)

def get_csv_path(ym):
    return DATA_DIR / f"実績明細_{ym}.csv"

def get_meisai_months():
    return [f.stem.replace("実績明細_", "")
            for f in sorted(DATA_DIR.glob("実績明細_*.csv"), reverse=True)]

def load_meisai(ym):
    return _read_csv(get_csv_path(ym), MEISAI_COLS)

def save_meisai(data):
    ym       = data["サービス提供年月"]
    csv_path = get_csv_path(ym)
    df = _read_csv(csv_path, MEISAI_COLS)
    same = (df["受給者証番号"] == str(data["受給者証番号"])) & (df["児童名"] == data["児童名"])
    df = df[~same]
    rows = [
        {
            "サービス提供年月": ym,
            "受給者証番号":     str(data["受給者証番号"]),
            "児童名":           data["児童名"],
            "保護者名":         data["保護者名"],
            "日":               r["日"],
            "提供形態":         "" if r["欠席"] else str(r["提供形態"] or ""),
            "開始時間":         "" if r["欠席"] else str(r["開始時間"] or ""),
            "終了時間":         "" if r["欠席"] else str(r["終了時間"] or ""),
            "送迎往":           r["送迎往"],
            "送迎復":           r["送迎復"],
            "状況":             "欠席" if r["欠席"] else "提供",
        }
        for r in data["実績"]
    ]
    df = pd.concat([df, pd.DataFrame(rows, columns=MEISAI_COLS)], ignore_index=True)
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    upsert_master(str(data["受給者証番号"]), data["児童名"])

def check_duplicate(data):
    df = load_meisai(data["サービス提供年月"])
    return ((df["受給者証番号"] == str(data["受給者証番号"])) &
            (df["児童名"] == data["児童名"])).any()

def delete_child(ym, jukyu_no):
    csv_path = get_csv_path(ym)
    if not csv_path.exists():
        return
    df = _read_csv(csv_path, MEISAI_COLS)
    df = df[df["受給者証番号"] != str(jukyu_no)]
    if df.empty:
        csv_path.unlink()
    else:
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")

# ============================================================
# 児童マスター
# ============================================================
def load_master():
    df = _read_csv(MASTER_PATH, MASTER_COLS)
    for col in MASTER_COLS:
        if col not in df.columns:
            df[col] = ""
    return df[MASTER_COLS]

def save_master(df):
    df.to_csv(MASTER_PATH, index=False, encoding="utf-8-sig")

def _master_val(jukyu_no, col, default=""):
    m = load_master()
    h = m[m["受給者証番号"] == str(jukyu_no)]
    v = str(h.iloc[0][col]).strip() if not h.empty else ""
    return v or default

def get_shoshiki(jukyu_no): return _master_val(jukyu_no, "様式種別番号", DEFAULT_SHOSHIKI)
def get_muni(jukyu_no):     return _master_val(jukyu_no, "市町村番号")
def get_sansei(jukyu_no):   return _master_val(jukyu_no, "算定時間記載") == "あり"

def upsert_master(jukyu_no, child_name):
    m = load_master()
    if str(jukyu_no) not in m["受給者証番号"].values:
        new_row = pd.DataFrame([{
            "受給者証番号": str(jukyu_no), "児童名": child_name,
            "市町村番号": DEFAULT_MUNI, "様式種別番号": DEFAULT_SHOSHIKI,
            "算定時間記載": DEFAULT_SANSEI,
        }])
        save_master(pd.concat([m, new_row], ignore_index=True))

def get_summary(ym):
    df = load_meisai(ym)
    if df.empty:
        return pd.DataFrame()
    t = df[df["状況"] == "提供"].copy()
    t["提供形態"] = pd.to_numeric(t["提供形態"], errors="coerce")
    t["送迎往"]   = pd.to_numeric(t["送迎往"],   errors="coerce").fillna(0)
    t["送迎復"]   = pd.to_numeric(t["送迎復"],   errors="coerce").fillna(0)
    s = t.groupby(["受給者証番号", "児童名", "保護者名"]).agg(
        算定日数   =("日",       "count"),
        短時間_1   =("提供形態", lambda x: (x == 1).sum()),
        長時間_2   =("提供形態", lambda x: (x == 2).sum()),
        送迎往合計 =("送迎往",   "sum"),
        送迎復合計 =("送迎復",   "sum"),
    ).reset_index()
    s["送迎往合計"] = s["送迎往合計"].astype(int)
    s["送迎復合計"] = s["送迎復合計"].astype(int)
    return s

# ============================================================
# CSV生成
# ============================================================
from kokuhoren_template import BASIC_TEMPLATE, DETAIL_TEMPLATE, CTRL_QUOTE, BASIC_QUOTE, DETAIL_QUOTE

def _line(fields, quote_idx):
    return ",".join(f'"{v}"' if i in quote_idx else str(v) for i, v in enumerate(fields))

def _next_month(ym):
    y, m = int(ym[:4]), int(ym[4:])
    m += 1
    if m > 12:
        y, m = y + 1, 1
    return f"{y}{m:02d}"

def _hhmm(t):
    s = str(t).replace(":", "").strip()
    return "0000" if s in ("", "nan", "None") else s.zfill(4)

def _to_min(hhmm):
    s = str(hhmm).replace(":", "").strip().zfill(4)
    return int(s[:2]) * 60 + int(s[2:]) if s.isdigit() else 0

def _min_to_hhmm(m, width):
    return f"{m // 60}{m % 60:02d}".zfill(width)

def _int_field(v):
    s = str(v).strip()
    return str(int(float(s))) if s not in ("", "nan") else "0"

def generate_kokuhoren_csv(ym):
    df      = load_meisai(ym)
    summary = get_summary(ym)
    if summary.empty:
        raise ValueError("データがありません")
    proc_ym = _next_month(ym)
    lines, rec_no = [], 1
    for _, t in summary.iterrows():
        jukyu = str(t["受給者証番号"])
        muni  = get_muni(jukyu)
        if not muni:
            raise ValueError(f"市町村番号が未設定です：{t['児童名']}")
        shoshiki   = get_shoshiki(jukyu)
        sansei     = get_sansei(jukyu)
        is_jihatsu = shoshiki.startswith("03")
        sougei     = int(t["送迎往合計"]) + int(t["送迎復合計"])
        day_df = df[(df["受給者証番号"] == jukyu) & (df["状況"] == "提供")].copy()
        day_df["日_num"] = pd.to_numeric(day_df["日"], errors="coerce")
        day_df = day_df.sort_values("日_num")
        total_min = (sum(_to_min(_hhmm(r["終了時間"])) - _to_min(_hhmm(r["開始時間"]))
                        for _, r in day_df.iterrows())
                    if sansei else 0)
        rec_no += 1
        b = BASIC_TEMPLATE.copy()
        b[1] = str(rec_no); b[4] = ym;       b[5] = muni; b[6] = JIGYOSHO["number"]
        b[7] = jukyu;       b[8] = shoshiki
        b[20] = _min_to_hhmm(total_min, 5) if sansei else "00000"
        b[35] = str(sougei)
        lines.append(_line(b, BASIC_QUOTE))
        for _, dr in day_df.iterrows():
            rec_no += 1
            s = _hhmm(dr["開始時間"]); e = _hhmm(dr["終了時間"])
            katachi = str(dr["提供形態"]).strip()
            k = katachi if katachi in ("1", "2") else "1"
            d = DETAIL_TEMPLATE.copy()
            d[1]  = str(rec_no); d[4]  = ym;      d[5]  = muni; d[6] = JIGYOSHO["number"]
            d[7]  = jukyu;       d[8]  = shoshiki; d[10] = str(int(dr["日_num"]))
            d[15] = s;           d[16] = e
            d[17] = _min_to_hhmm(_to_min(e) - _to_min(s), 4) if sansei else "0000"
            d[22] = _int_field(dr["送迎往"]); d[23] = _int_field(dr["送迎復"])
            d[35] = "0" if is_jihatsu else k
            lines.append(_line(d, DETAIL_QUOTE))
    ctrl = _line(["1", "1", "0", str(len(lines)), JIGYOSHO["data_type"], "0",
                  JIGYOSHO["number"], "0", "1", proc_ym, ""], CTRL_QUOTE)
    return ("\r\n".join([ctrl] + lines + [f"3,{rec_no + 1}"]) + "\r\n").encode("cp932", errors="replace")


# ============================================================
# UI ヘルパー
# ============================================================
st.set_page_config(page_title="らくらく請求", page_icon="🏥", layout="wide",
                   initial_sidebar_state="expanded")

st.markdown("""
<style>
html, body, [class*="css"] {
    font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue",
                 "Hiragino Sans", "Yu Gothic UI", Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
}
#MainMenu { visibility: hidden; }
footer { visibility: hidden; }
header[data-testid="stHeader"] { background: transparent !important; }
.stDeployButton { visibility: hidden; }
.stApp { background: #F2F2F7; }
.main .block-container { padding-top: 32px; padding-bottom: 48px; }

section[data-testid="stSidebar"] {
    background: #FFFFFF; border-right: 1px solid #D1D1D6;
}
section[data-testid="stSidebar"] .stTextInput input {
    border-radius: 10px; border: 1.5px solid #D1D1D6;
    font-size: 14px; padding: 8px 12px; background: #F9F9F9;
}
section[data-testid="stSidebar"] .stTextInput input:focus {
    border-color: #007AFF; background: #FFF;
}

.stTabs [data-baseweb="tab-list"] {
    background: #E5E5EA; border-radius: 10px; padding: 3px; gap: 2px;
}
.stTabs [data-baseweb="tab"] {
    font-size: 14px; font-weight: 500; color: #6E6E73;
    border-radius: 8px; padding: 7px 18px; border: none; background: transparent;
}
.stTabs [aria-selected="true"] {
    background: #FFFFFF !important; color: #1C1C1E !important;
    font-weight: 600; box-shadow: 0 1px 3px rgba(0,0,0,0.12);
}
.stTabs [data-baseweb="tab-panel"] { padding-top: 28px; }

[data-testid="metric-container"] {
    background: #FFFFFF; border: none; border-radius: 16px; padding: 20px 22px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.08), 0 0 0 0.5px rgba(0,0,0,0.04);
}
[data-testid="stMetricValue"] {
    font-size: 28px !important; font-weight: 700 !important;
    color: #1C1C1E !important; letter-spacing: -0.5px;
}
[data-testid="stMetricLabel"] p {
    font-size: 12px !important; color: #8E8E93 !important;
    font-weight: 500 !important; text-transform: uppercase; letter-spacing: 0.3px;
}

[data-testid="stFileUploadDropzone"] {
    background: #FFFFFF !important; border: 1.5px dashed #C7C7CC !important;
    border-radius: 14px !important;
}

.stButton > button {
    border-radius: 980px; font-weight: 500; font-size: 15px;
    padding: 9px 22px; border: 1.5px solid #D1D1D6;
    background: #FFFFFF; color: #1C1C1E;
}
.stButton > button[kind="primary"] {
    background: #007AFF; color: #FFFFFF; border: none;
    box-shadow: 0 2px 8px rgba(0,122,255,0.28);
}
.stButton > button[kind="primary"]:hover { background: #0071E3; }
.stButton > button:disabled { opacity: 0.4; }

.stAlert { border-radius: 12px; border: none; }
hr { border: none; border-top: 1px solid #D1D1D6; margin: 20px 0; }
</style>
""", unsafe_allow_html=True)


def section_title(title, subtitle=""):
    sub = f'<div style="font-size:13px;color:#8E8E93;margin-top:4px;">{subtitle}</div>' if subtitle else ""
    st.markdown(f"""
    <div style="margin:28px 0 16px 0;">
        <div style="font-size:20px;font-weight:700;color:#1C1C1E;letter-spacing:-0.3px;">{title}</div>
        {sub}
    </div>""", unsafe_allow_html=True)

def step_badge(num, title, subtitle=""):
    sub = f'<div style="font-size:13px;color:#8E8E93;margin-top:3px;">{subtitle}</div>' if subtitle else ""
    st.markdown(f"""
    <div style="display:flex;align-items:flex-start;gap:12px;margin:28px 0 16px 0;">
        <div style="min-width:30px;height:30px;border-radius:50%;background:#007AFF;color:white;
             font-size:14px;font-weight:700;display:flex;align-items:center;
             justify-content:center;margin-top:2px;">{num}</div>
        <div>
            <div style="font-size:18px;font-weight:600;color:#1C1C1E;">{title}</div>
            {sub}
        </div>
    </div>""", unsafe_allow_html=True)

def next_step_hint(text):
    st.markdown(f"""
    <div style="background:#EFF6FF;border-radius:12px;padding:14px 18px;margin:16px 0;
         border-left:3px solid #007AFF;">
        <span style="font-size:14px;color:#1D4ED8;font-weight:500;">次のステップ　→　{text}</span>
    </div>""", unsafe_allow_html=True)

def empty_state(title, body):
    st.markdown(f"""
    <div style="background:#FFFFFF;border-radius:16px;padding:48px 32px;text-align:center;
         box-shadow:0 1px 4px rgba(0,0,0,0.07);margin:8px 0;">
        <div style="font-size:17px;font-weight:600;color:#1C1C1E;margin-bottom:8px;">{title}</div>
        <div style="font-size:14px;color:#8E8E93;line-height:1.7;">{body}</div>
    </div>""", unsafe_allow_html=True)

def alert(status, msg):
    cfg = {
        "ok":    ("#F0FFF4", "#34C759", "#166534"),
        "warn":  ("#FFFBEB", "#FF9500", "#92400E"),
        "error": ("#FEF2F2", "#FF3B30", "#991B1B"),
    }
    bg, bc, tc = cfg[status]
    st.markdown(f"""
    <div style="background:{bg};border-radius:14px;padding:18px 22px;
         margin:16px 0;border-left:3px solid {bc};">
        <div style="font-size:16px;font-weight:700;color:{tc};">{msg}</div>
    </div>""", unsafe_allow_html=True)

def _card(dot_color, title_html, detail="", bg="#FFFFFF"):
    detail_html = (f'<div style="font-size:13px;color:#8E8E93;margin-top:4px;">{detail}</div>'
                   if detail else "")
    st.markdown(f"""
    <div style="background:{bg};border-radius:14px;padding:16px 20px;margin:8px 0;
         box-shadow:0 1px 4px rgba(0,0,0,0.07);">
        <div style="display:flex;align-items:center;gap:12px;">
            <div style="width:10px;height:10px;border-radius:50%;
                 background:{dot_color};flex-shrink:0;"></div>
            <div>
                <div style="font-size:15px;font-weight:600;color:#1C1C1E;">{title_html}</div>
                {detail_html}
            </div>
        </div>
    </div>""", unsafe_allow_html=True)

def _child_detail(jukyu, ym, days, og, ret):
    return f"受給者証 {jukyu}　·　{ym[:4]}年{ym[4:]}月　·　{days}日　·　送迎 往{og} / 復{ret}"

def card_ok(name, jukyu, ym, days, og, ret):
    _card("#34C759", name, _child_detail(jukyu, ym, days, og, ret))

def card_overwrite(name, jukyu, ym, days, og, ret):
    title = (f'{name}　<span style="font-size:12px;font-weight:500;color:#FF9500;">'
             f'⚠ 同じ月のデータが既にありました。上書きしました。</span>')
    _card("#FF9500", title, _child_detail(jukyu, ym, days, og, ret), bg="#FFFBEB")

def card_err(label, msg):
    _card("#FF3B30", label, f'<span style="color:#FF3B30;">{msg}</span>')


# ── APIキー ────────────────────────────────────────────────
API_KEY_FILE = DATA_DIR / ".api_key"

def load_saved_api_key():
    k = os.environ.get("ANTHROPIC_API_KEY", "")
    if k:
        return k
    if API_KEY_FILE.exists():
        return API_KEY_FILE.read_text().strip()
    return ""

if "api_key" not in st.session_state:
    st.session_state["api_key"] = load_saved_api_key()


# ── サイドバー ──────────────────────────────────────────────
with st.sidebar:
    st.markdown("""
    <div style="padding:20px 0 22px 0;border-bottom:1px solid #E5E5EA;margin-bottom:22px;">
        <div style="font-size:18px;font-weight:700;color:#1C1C1E;">らくらく請求</div>
        <div style="font-size:12px;color:#8E8E93;margin-top:3px;">障害福祉請求 自動化ツール</div>
    </div>
    """, unsafe_allow_html=True)

    api_key = st.session_state.get("api_key", "")

    st.divider()
    st.markdown("""
<div style="font-size:13px;line-height:2.2;">
<div style="font-weight:700;color:#1C1C1E;margin-bottom:2px;">使い方（3ステップ）</div>
<div style="color:#007AFF;font-weight:600;">① 実績記録票を読み取る</div>
<div style="color:#8E8E93;font-size:12px;padding-left:12px;margin-bottom:4px;">写真・PDFをアップロード</div>
<div style="color:#007AFF;font-weight:600;">② 内容を確認する</div>
<div style="color:#8E8E93;font-size:12px;padding-left:12px;margin-bottom:4px;">読み取り結果をチェック・修正</div>
<div style="color:#007AFF;font-weight:600;">③ CSVを作って提出する</div>
<div style="color:#8E8E93;font-size:12px;padding-left:12px;">国保連に提出するCSVをダウンロード</div>
</div>
    """, unsafe_allow_html=True)

    st.divider()
    with st.expander("データ管理"):
        st.caption("全データを削除してまっさらにします")
        months = get_meisai_months()
        if months:
            st.caption(f"保存中：{', '.join(m[:4]+'年'+m[4:]+'月' for m in months)}")
        if st.button("全データをリセット", type="secondary", key="reset_all"):
            for f in DATA_DIR.glob("実績明細_*.csv"):
                f.unlink()
            if MASTER_PATH.exists():
                MASTER_PATH.unlink()
            st.success("リセットしました")
            st.rerun()

    st.divider()
    st.markdown(f'<div style="font-size:11px;color:#C7C7CC;">パラザ合同会社　{APP_VERSION}</div>',
                unsafe_allow_html=True)


# ── ページタイトル ─────────────────────────────────────────
st.markdown("""
<div style="margin-bottom:6px;">
    <div style="font-size:34px;font-weight:700;color:#1C1C1E;letter-spacing:-0.5px;">らくらく請求</div>
    <div style="font-size:15px;color:#8E8E93;margin-top:6px;">
        実績記録票を写真で撮るだけで、国保連への提出データを自動で作ります
    </div>
</div>""", unsafe_allow_html=True)
st.divider()


# ── タブ ─────────────────────────────────────────────────
tab1, tab2, tab3, tab4 = st.tabs(["① 読み取り", "② 内容確認", "③ 請求前チェック", "④ CSV生成"])


# ============================================================
# TAB 1 — 読み取り
# ============================================================
with tab1:
    section_title("実績記録票を読み取る",
                  "実績記録票の写真またはPDFをアップロードしてください。AIが自動でデータを読み取ります。")

    col_up, col_tip = st.columns([3, 1])
    with col_up:
        uploaded_files = st.file_uploader(
            "ファイルをここにドラッグ、またはクリックして選択",
            type=["jpg", "jpeg", "png", "webp", "pdf"],
            accept_multiple_files=True,
            help="1人1枚ずつでも、まとめてでもOKです",
        )
    with col_tip:
        st.markdown("""
        <div style="background:#FFFFFF;border-radius:14px;padding:18px 20px;
             box-shadow:0 1px 4px rgba(0,0,0,0.07);margin-top:8px;">
            <div style="font-size:13px;font-weight:700;color:#1C1C1E;margin-bottom:10px;">
                うまく読み取れないときは
            </div>
            <div style="font-size:13px;color:#8E8E93;line-height:2.0;">
                ✓ 明るい場所で撮る<br>
                ✓ 真上からまっすぐ撮る<br>
                ✓ ピントを合わせる<br>
                ✓ PDFでもOK
            </div>
        </div>""", unsafe_allow_html=True)

    if not uploaded_files:
        st.markdown("""
        <div style="background:#FFFFFF;border-radius:14px;padding:20px 24px;margin-top:16px;
             box-shadow:0 1px 4px rgba(0,0,0,0.05);">
            <div style="font-size:14px;color:#8E8E93;text-align:center;">
                上のエリアにファイルをドラッグするか、クリックして選択してください
            </div>
        </div>""", unsafe_allow_html=True)

    elif not api_key:
        alert("error", "APIキーが設定されていません。Streamlit Secrets に ANTHROPIC_API_KEY を設定してください。")

    else:
        st.markdown(f"""
        <div style="background:#FFFFFF;border-radius:12px;padding:14px 18px;margin:12px 0;
             box-shadow:0 1px 4px rgba(0,0,0,0.05);">
            <span style="font-size:14px;color:#1C1C1E;">
                <b>{len(uploaded_files)}件</b>のファイルが選択されています
            </span>
        </div>""", unsafe_allow_html=True)

        if st.button("読み取りを開始する", type="primary"):
            results, all_jobs = [], []
            for uf in uploaded_files:
                ext = uf.name.rsplit(".", 1)[-1].lower()
                raw = uf.read()
                if ext == "pdf":
                    for i, p in enumerate(pdf_to_png(raw)):
                        all_jobs.append((p, "image/png", f"{uf.name}（{i+1}ページ目）"))
                else:
                    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                            "png": "image/png",  "webp": "image/webp"}.get(ext, "image/jpeg")
                    all_jobs.append((raw, mime, uf.name))

            progress = st.progress(0, text="読み取り中...")
            for idx, (img_bytes, mime, label) in enumerate(all_jobs):
                progress.progress((idx + 1) / len(all_jobs),
                                  text=f"読み取り中... {idx+1}/{len(all_jobs)}件")
                try:
                    data   = ocr_jisseki(img_bytes, mime, api_key)
                    is_dup = check_duplicate(data)
                    save_meisai(data)
                    results.append({"status": "overwrite" if is_dup else "ok",
                                    "label": label, "data": data})
                except Exception as e:
                    results.append({"status": "err", "label": label, "error": str(e)})
            progress.empty()

            ok_list  = [r for r in results if r["status"] in ("ok", "overwrite")]
            err_list = [r for r in results if r["status"] == "err"]
            for r in ok_list:
                d = r["data"]
                fn = card_overwrite if r["status"] == "overwrite" else card_ok
                fn(d["児童名"], d["受給者証番号"], d["サービス提供年月"],
                   d["算定日数"], d["送迎往合計"], d["送迎復合計"])
            for r in err_list:
                card_err(r["label"], r["error"])

            if ok_list:
                ow = sum(1 for r in ok_list if r["status"] == "overwrite")
                msg = f"{len(ok_list)}件を保存しました" + (f"（うち{ow}件は上書き）" if ow else "")
                st.success(msg)
                next_step_hint("「② 内容確認」タブで読み取り結果を確認してください")

    st.divider()
    with st.expander("手動で1件だけ入力する場合はこちら"):
        st.caption("OCRがうまくいかないときや、1件だけ追加したいときに使います")
        with st.form("manual_input"):
            col1, col2 = st.columns(2)
            year_month  = col1.text_input("対象年月（例：202504）", "202504")
            jukyu_no    = col2.text_input("受給者証番号（10桁）")
            child_name  = col1.text_input("お子さんの名前")
            parent_name = col2.text_input("保護者の名前")
            date_input  = st.text_input("利用した日（カンマ区切り　例：1,4,7,8,11）")
            katachi     = st.selectbox("サービスの種類", [2, 1],
                            format_func=lambda x: "長時間（3時間以上）" if x == 2 else "短時間（3時間未満）")
            col3, col4  = st.columns(2)
            start_time  = col3.text_input("開始時間", "10:00")
            end_time    = col4.text_input("終了時間", "17:00")
            col5, col6  = st.columns(2)
            sougei_ou   = col5.checkbox("送迎あり（往）", value=True)
            sougei_fu   = col6.checkbox("送迎あり（復）", value=True)
            if st.form_submit_button("保存する", type="primary"):
                days = [int(d.strip()) for d in date_input.split(",") if d.strip()]
                save_meisai({
                    "受給者証番号": jukyu_no, "児童名": child_name,
                    "保護者名": parent_name,  "サービス提供年月": year_month,
                    "実績": [{"日": d, "提供形態": katachi, "開始時間": start_time,
                              "終了時間": end_time, "送迎往": 1 if sougei_ou else 0,
                              "送迎復": 1 if sougei_fu else 0, "欠席": False} for d in days],
                    "算定日数":   len(days),
                    "送迎往合計": len(days) if sougei_ou else 0,
                    "送迎復合計": len(days) if sougei_fu else 0,
                })
                st.success(f"{child_name}さんの{len(days)}日分を保存しました")


# ============================================================
# TAB 2 — 内容確認
# ============================================================
with tab2:
    section_title("読み取り結果を確認する",
                  "AIが読み取ったデータを確認してください。間違いがあればその場で修正できます。")

    year_months = get_meisai_months()
    if not year_months:
        empty_state("まだデータがありません", "「① 読み取り」タブで実績記録票をアップロードしてください")
    else:
        selected_ym = st.selectbox("確認する月を選んでください", year_months,
                                   format_func=lambda x: f"{x[:4]}年{x[4:]}月")
        summary = get_summary(selected_ym)

        if not summary.empty:
            st.markdown('<div style="height:4px;"></div>', unsafe_allow_html=True)
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("登録人数",  f"{len(summary)} 人")
            c2.metric("合計日数",  f"{int(summary['算定日数'].sum())} 日")
            c3.metric("送迎（往）", f"{int(summary['送迎往合計'].sum())} 回")
            c4.metric("送迎（復）", f"{int(summary['送迎復合計'].sum())} 回")

            st.markdown('<div style="height:12px;"></div>', unsafe_allow_html=True)
            st.markdown('<div style="font-size:14px;font-weight:600;color:#1C1C1E;'
                        'margin-bottom:8px;">お子さんごとのまとめ</div>', unsafe_allow_html=True)
            for _, r in summary.iterrows():
                col_info, col_del = st.columns([6, 1])
                with col_info:
                    card_ok(r["児童名"], r["受給者証番号"], selected_ym,
                            int(r["算定日数"]), int(r["送迎往合計"]), int(r["送迎復合計"]))
                with col_del:
                    st.markdown('<div style="height:14px;"></div>', unsafe_allow_html=True)
                    if st.button("削除", key=f"del_{r['受給者証番号']}_{selected_ym}"):
                        delete_child(selected_ym, r["受給者証番号"])
                        st.rerun()

        st.divider()
        st.markdown('<div style="font-size:14px;font-weight:600;color:#1C1C1E;'
                    'margin-bottom:4px;">日ごとの詳細</div>', unsafe_allow_html=True)
        st.caption("表のセルをクリックすると直接修正できます")

        df = load_meisai(selected_ym)
        if not df.empty:
            children  = ["全員表示"] + sorted(df["児童名"].dropna().unique().tolist())
            sel_child = st.selectbox("", children, label_visibility="collapsed")
            show_df   = df if sel_child == "全員表示" else df[df["児童名"] == sel_child]
            edited    = st.data_editor(show_df, use_container_width=True, hide_index=True)
            if st.button("修正を保存する"):
                edited.to_csv(get_csv_path(selected_ym), index=False, encoding="utf-8-sig")
                st.success("保存しました")

        next_step_hint("内容が確認できたら「③ 請求前チェック」タブへ進んでください")


# ============================================================
# TAB 3 — 請求前チェック
# ============================================================
with tab3:
    from billing_check import run_billing_checks, detect_latest_ym

    section_title("請求前チェック", "CSV生成の前に、入力漏れ・設定ミスがないか自動で確認します")

    year_months3 = get_meisai_months()
    if not year_months3:
        empty_state("まだデータがありません", "「① 読み取り」タブで実績記録票をアップロードしてください")
    else:
        selected_check_ym = st.selectbox(
            "チェックする月を選んでください", year_months3, key="check_ym",
            format_func=lambda x: f"{x[:4]}年{x[4:]}月",
        )
        if st.button("チェックを実行する", type="primary", key="run_check"):
            with st.spinner("チェック中..."):
                check_results = run_billing_checks(selected_check_ym)

            errors = [r for r in check_results if r["status"] == "error"]
            warns  = [r for r in check_results if r["status"] == "warn"]

            if errors:
                alert("error", f"❌ エラー {len(errors)}件 — CSV生成前に修正してください")
            elif warns:
                alert("warn",  f"⚠️ 注意 {len(warns)}件 — 確認のうえCSV生成へ進んでください")
            else:
                alert("ok",    "✅ 問題なし — 「④ CSV生成」へ進んでください")

            icon = {"ok": "✅", "warn": "⚠️", "error": "❌"}
            for r in check_results:
                bg = {"ok": "#F0FFF4", "warn": "#FFFBEB", "error": "#FEF2F2"}[r["status"]]
                bc = {"ok": "#34C759", "warn": "#FF9500", "error": "#FF3B30"}[r["status"]]
                st.markdown(f"""
                <div style="background:{bg};border-radius:12px;padding:14px 18px;
                     margin:6px 0;border-left:3px solid {bc};">
                    <div style="font-size:14px;font-weight:600;color:#1C1C1E;">
                        {icon[r['status']]} {r['name']}
                    </div>
                    <div style="font-size:13px;color:#3C3C43;margin-top:4px;">{r['msg']}</div>
                </div>""", unsafe_allow_html=True)


# ============================================================
# TAB 4 — CSV生成
# ============================================================
with tab4:
    section_title("CSVを作って提出する", "3つのステップで国保連に提出するCSVファイルを作ります")

    year_months4 = get_meisai_months()
    if not year_months4:
        empty_state("まだデータがありません",
                    "「① 読み取り」タブで実績記録票を読み取ってから、こちらに戻ってください")
    else:
        selected_ym4 = st.selectbox("対象の月を選んでください", year_months4, key="csv_ym",
                                    format_func=lambda x: f"{x[:4]}年{x[4:]}月")

        # ── ステップ 1 ──────────────────────────────────────
        step_badge(1, "お子さんの情報を確認する", "市町村番号と様式種別番号が正しいか確認してください")
        st.markdown("""
        <div style="background:#F8F8F8;border-radius:12px;padding:14px 18px;
             margin-bottom:14px;font-size:13px;color:#6E6E73;line-height:1.9;">
            <b style="color:#1C1C1E;">市町村番号</b>　→　受給者証に記載されている6桁の数字<br>
            <b style="color:#1C1C1E;">様式種別番号</b>　→　放課後等デイサービスは <b>0501</b>、児童発達支援は <b>0301</b>
        </div>""", unsafe_allow_html=True)

        summary4 = get_summary(selected_ym4)
        master   = load_master()

        if not summary4.empty:
            month_jukyu    = summary4["受給者証番号"].astype(str).tolist()
            master_display = master[master["受給者証番号"].astype(str).isin(month_jukyu)].copy()
            for _, r in summary4.iterrows():
                if str(r["受給者証番号"]) not in master_display["受給者証番号"].astype(str).values:
                    master_display = pd.concat([master_display, pd.DataFrame([{
                        "受給者証番号": str(r["受給者証番号"]), "児童名": r["児童名"],
                        "市町村番号": DEFAULT_MUNI, "様式種別番号": DEFAULT_SHOSHIKI,
                        "算定時間記載": DEFAULT_SANSEI,
                    }])], ignore_index=True)
        else:
            master_display = pd.DataFrame(columns=MASTER_COLS)

        master_edited = st.data_editor(
            master_display, use_container_width=True, hide_index=True, key="master_editor",
            column_config={
                "市町村番号":   st.column_config.TextColumn("市町村番号",
                                    help="受給者証に記載の6桁（例：212019）", required=True),
                "様式種別番号": st.column_config.SelectboxColumn("様式種別番号",
                                    options=["0501", "0301"],
                                    help="放デイ＝0501　児発＝0301", required=True),
                "算定時間記載": st.column_config.SelectboxColumn("算定時間記載",
                                    options=["なし", "あり"],
                                    help="児発で算定時間を記載する場合のみ「あり」", required=True),
            },
        )
        col_save4, col_reset = st.columns([2, 1])
        with col_save4:
            if st.button("内容を保存する"):
                full = load_master()
                edited_jukyu = master_edited[MASTER_COLS]["受給者証番号"].astype(str).tolist()
                full = full[~full["受給者証番号"].astype(str).isin(edited_jukyu)]
                save_master(pd.concat([full, master_edited[MASTER_COLS]], ignore_index=True))
                st.success("保存しました")
                st.rerun()
        with col_reset:
            if st.button("マスターをリセット", type="secondary"):
                if MASTER_PATH.exists():
                    MASTER_PATH.unlink()
                st.success("リセットしました")
                st.rerun()

        st.divider()

        # ── ステップ 2 ──────────────────────────────────────
        step_badge(2, "提出内容を最終確認する", "以下の内容で国保連に提出されます")
        if not summary4.empty:
            disp = summary4.copy()
            disp["市町村番号"]   = disp["受給者証番号"].apply(lambda x: get_muni(str(x)))
            disp["様式種別番号"] = disp["受給者証番号"].apply(lambda x: get_shoshiki(str(x)))
            st.dataframe(disp, use_container_width=True, hide_index=True)

            missing = [r["児童名"] for _, r in disp.iterrows() if not get_muni(str(r["受給者証番号"]))]
            if missing:
                alert("error", f"市町村番号が未設定のお子さんがいます：{', '.join(missing)}さん")
            else:
                alert("ok", "全員の設定が完了しています")

            st.divider()

            # ── ステップ 3 ──────────────────────────────────
            proc_ym = _next_month(selected_ym4)
            step_badge(3, "CSVをダウンロードする", "ボタンを押すとファイルが作成されます")
            st.markdown(f"""
            <div style="background:#FFFFFF;border-radius:14px;padding:20px 24px;
                 margin-bottom:20px;box-shadow:0 1px 4px rgba(0,0,0,0.07);">
                <div style="font-size:13px;color:#8E8E93;margin-bottom:12px;font-weight:500;">
                    生成されるファイルの情報
                </div>
                <table style="font-size:14px;color:#3C3C43;border-collapse:collapse;">
                    <tr>
                        <td style="color:#8E8E93;padding:5px 24px 5px 0;white-space:nowrap;">請求月</td>
                        <td style="font-weight:600;color:#1C1C1E;">{proc_ym[:4]}年{proc_ym[4:]}月</td>
                    </tr>
                    <tr>
                        <td style="color:#8E8E93;padding:5px 24px 5px 0;">対象人数</td>
                        <td style="font-weight:600;color:#1C1C1E;">{len(summary4)}名</td>
                    </tr>
                    <tr>
                        <td style="color:#8E8E93;padding:5px 24px 5px 0;">事業所番号</td>
                        <td style="font-weight:600;color:#1C1C1E;">{JIGYOSHO['number']}</td>
                    </tr>
                </table>
            </div>""", unsafe_allow_html=True)

            if st.button("CSVファイルを作成する", type="primary", disabled=bool(missing)):
                try:
                    csv_bytes = generate_kokuhoren_csv(selected_ym4)
                    file_name = f"{selected_ym4}_国保連実績_{JIGYOSHO['number']}.csv"
                    st.download_button("ダウンロードする", data=csv_bytes,
                                       file_name=file_name, mime="text/csv")
                    alert("ok", "CSVファイルが作成されました。「ダウンロードする」ボタンで保存してください。")
                except Exception as e:
                    alert("error", f"エラーが発生しました：{e}")
