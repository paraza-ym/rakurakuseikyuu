"""
実績記録票 OCR → 国保連CSV 生成システム
Streamlit アプリ
"""

import os
import streamlit as st
import anthropic
import base64
import json
import pandas as pd
import fitz
from pathlib import Path

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

APP_VERSION = "v1.2"

# ============================================================
# OCR
# ============================================================
def pdf_to_png(pdf_bytes):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    images = []
    for page in doc:
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
        images.append(pix.tobytes("png"))
    doc.close()
    return images

def ocr_jisseki(image_bytes, mime_type, api_key):
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
- 受給者証番号は「受給者証番号」欄に記載された利用者個人の番号。スペースを除去した数字のみ
- 【重要】2150600183 は事業所番号であり受給者証番号ではない。この値が読み取れた場合は読み取りエラーとして扱い、正しい受給者証番号を再度探すこと
- 保護者名は「（　様）」の括弧内の氏名"""

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2000,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64}},
            {"type": "text", "text": prompt}
        ]}]
    )
    text  = response.content[0].text
    start = text.find("{")
    end   = text.rfind("}") + 1
    if start == -1:
        raise ValueError(f"読み取りに失敗しました。写真をより鮮明に撮り直してください。")
    data = json.loads(text[start:end])
    # 事業所番号を受給者証番号として誤読した場合はエラー
    if str(data.get("受給者証番号","")).replace(" ","") == JIGYOSHO["number"]:
        raise ValueError(
            f"受給者証番号の読み取りに失敗しました（事業所番号と混同されました）。"
            f"「{data.get('児童名','')}」さんの受給者証番号欄を確認して手動入力してください。"
        )
    data["算定日数"]   = sum(1 for r in data["実績"] if not r["欠席"])
    data["送迎往合計"] = sum(r["送迎往"] for r in data["実績"])
    data["送迎復合計"] = sum(r["送迎復"] for r in data["実績"])
    return data

# ============================================================
# データ管理
# ============================================================
def get_csv_path(year_month):
    return DATA_DIR / f"実績明細_{year_month}.csv"

MEISAI_COLS = ["サービス提供年月","受給者証番号","児童名","保護者名",
               "日","提供形態","開始時間","終了時間","送迎往","送迎復","状況"]

def save_meisai(data):
    ym       = data["サービス提供年月"]
    csv_path = get_csv_path(ym)
    if csv_path.exists():
        df = pd.read_csv(csv_path, dtype=str)
        # 受給者証番号＋児童名の組み合わせで上書き（同番号でも別の子は消えない）
        same = (df["受給者証番号"] == str(data["受給者証番号"])) & (df["児童名"] == data["児童名"])
        df = df[~same]
    else:
        df = pd.DataFrame(columns=MEISAI_COLS)
    rows = []
    for r in data["実績"]:
        rows.append({
            "サービス提供年月": ym,
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
    df = pd.concat([df, pd.DataFrame(rows, columns=MEISAI_COLS)], ignore_index=True)
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    upsert_master(str(data["受給者証番号"]), data["児童名"])

def load_meisai(ym):
    p = get_csv_path(ym)
    if not p.exists():
        return pd.DataFrame(columns=MEISAI_COLS)
    return pd.read_csv(p, dtype=str)

# ============================================================
# 児童マスター
# ============================================================
MASTER_PATH  = DATA_DIR / "児童マスター.csv"
MASTER_COLS  = ["受給者証番号","児童名","市町村番号","様式種別番号","算定時間記載"]
DEFAULT_SHOSHIKI = "0501"
DEFAULT_MUNI     = "212019"
DEFAULT_SANSEI   = "なし"

def load_master():
    if not MASTER_PATH.exists():
        return pd.DataFrame(columns=MASTER_COLS)
    df = pd.read_csv(MASTER_PATH, dtype=str)
    for col in MASTER_COLS:
        if col not in df.columns:
            df[col] = ""
    return df[MASTER_COLS]

def save_master(df):
    df.to_csv(MASTER_PATH, index=False, encoding="utf-8-sig")

def get_shoshiki(jukyu_no):
    m = load_master()
    h = m[m["受給者証番号"] == str(jukyu_no)]
    return str(h.iloc[0]["様式種別番号"]).strip() if not h.empty and str(h.iloc[0]["様式種別番号"]).strip() else DEFAULT_SHOSHIKI

def get_muni(jukyu_no):
    m = load_master()
    h = m[m["受給者証番号"] == str(jukyu_no)]
    return str(h.iloc[0]["市町村番号"]).strip() if not h.empty and str(h.iloc[0]["市町村番号"]).strip() else ""

def get_sansei(jukyu_no):
    m = load_master()
    h = m[m["受給者証番号"] == str(jukyu_no)]
    return not h.empty and str(h.iloc[0]["算定時間記載"]).strip() == "あり"

def upsert_master(jukyu_no, child_name):
    m = load_master()
    if str(jukyu_no) not in m["受給者証番号"].astype(str).values:
        m = pd.concat([m, pd.DataFrame([{
            "受給者証番号": str(jukyu_no), "児童名": child_name,
            "市町村番号": DEFAULT_MUNI, "様式種別番号": DEFAULT_SHOSHIKI,
            "算定時間記載": DEFAULT_SANSEI,
        }])], ignore_index=True)
        save_master(m)

def get_summary(ym):
    df = load_meisai(ym)
    if df.empty:
        return pd.DataFrame()
    t = df[df["状況"] == "提供"].copy()
    t["提供形態"] = pd.to_numeric(t["提供形態"], errors="coerce")
    t["送迎往"]   = pd.to_numeric(t["送迎往"],   errors="coerce").fillna(0)
    t["送迎復"]   = pd.to_numeric(t["送迎復"],   errors="coerce").fillna(0)
    s = t.groupby(["受給者証番号","児童名","保護者名"]).agg(
        算定日数=("日","count"),
        短時間_1=("提供形態", lambda x:(x==1).sum()),
        長時間_2=("提供形態", lambda x:(x==2).sum()),
        送迎往合計=("送迎往","sum"),
        送迎復合計=("送迎復","sum"),
    ).reset_index()
    s["送迎往合計"] = s["送迎往合計"].astype(int)
    s["送迎復合計"] = s["送迎復合計"].astype(int)
    return s

# ============================================================
# CSV生成
# ============================================================
from kokuhoren_template import BASIC_TEMPLATE, DETAIL_TEMPLATE, CTRL_QUOTE, BASIC_QUOTE, DETAIL_QUOTE

def _line(fields, quote_idx):
    return ",".join('"' + str(v) + '"' if i in quote_idx else str(v) for i, v in enumerate(fields))

def _next_month(ym):
    y, m = int(ym[:4]), int(ym[4:6])
    m += 1
    if m > 12: y += 1; m = 1
    return f"{y}{m:02d}"

def _hhmm(t):
    s = str(t or "").replace(":", "").strip()
    return s.zfill(4) if s else "0000"

def _to_min(hhmm):
    s = str(hhmm or "").replace(":", "").strip().zfill(4)
    if not s.isdigit() or len(s) < 3: return 0
    return int(s[:-2]) * 60 + int(s[-2:])

def _min_to_hhmm(m, width):
    return f"{m//60}{m%60:02d}".zfill(width)

def generate_kokuhoren_csv(ym):
    df      = load_meisai(ym)
    summary = get_summary(ym)
    if summary.empty:
        raise ValueError("データがありません")
    proc_ym = _next_month(ym)
    lines   = []
    rec_no  = 1
    for _, t in summary.iterrows():
        jukyu = str(t["受給者証番号"])
        muni  = get_muni(jukyu)
        if not muni:
            raise ValueError(f"市町村番号が未設定です：{t['児童名']}")
        shoshiki   = get_shoshiki(jukyu)
        sansei     = get_sansei(jukyu)
        is_jihatsu = shoshiki.startswith("03")
        sougei     = int(t["送迎往合計"]) + int(t["送迎復合計"])
        day_df     = df[(df["受給者証番号"]==jukyu)&(df["状況"]=="提供")].copy()
        day_df["日_num"] = pd.to_numeric(day_df["日"], errors="coerce")
        day_df = day_df.sort_values("日_num")
        total_min = sum(_to_min(_hhmm(r["終了時間"])) - _to_min(_hhmm(r["開始時間"]))
                        for _, r in day_df.iterrows()) if sansei else 0
        rec_no += 1
        b = BASIC_TEMPLATE.copy()
        b[1]=str(rec_no); b[4]=ym; b[5]=muni; b[6]=JIGYOSHO["number"]
        b[7]=jukyu; b[8]=shoshiki
        b[20]=_min_to_hhmm(total_min,5) if sansei else "00000"
        b[35]=str(sougei)
        lines.append(_line(b, BASIC_QUOTE))
        for _, dr in day_df.iterrows():
            rec_no += 1
            s = _hhmm(dr["開始時間"]); e = _hhmm(dr["終了時間"])
            d = DETAIL_TEMPLATE.copy()
            d[1]=str(rec_no); d[4]=ym; d[5]=muni; d[6]=JIGYOSHO["number"]
            d[7]=jukyu; d[8]=shoshiki; d[10]=str(int(dr["日_num"]))
            d[15]=s; d[16]=e
            d[17]=_min_to_hhmm(_to_min(e)-_to_min(s),4) if sansei else "0000"
            d[22]=str(int(float(dr["送迎往"] or 0)))
            d[23]=str(int(float(dr["送迎復"] or 0)))
            k=str(dr["提供形態"] or "").strip()
            d[35]="0" if is_jihatsu else (k if k else "1")
            lines.append(_line(d, DETAIL_QUOTE))
    ctrl = _line(["1","1","0",str(len(lines)),JIGYOSHO["data_type"],"0",
                  JIGYOSHO["number"],"0","1",proc_ym,""], CTRL_QUOTE)
    return ("\r\n".join([ctrl]+lines+["3,"+str(rec_no+1)])+"\r\n").encode("cp932", errors="replace")


# ============================================================
# UI
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

/* サイドバー */
section[data-testid="stSidebar"] {
    background: #FFFFFF;
    border-right: 1px solid #D1D1D6;
}
section[data-testid="stSidebar"] .stTextInput input {
    border-radius: 10px; border: 1.5px solid #D1D1D6;
    font-size: 14px; padding: 8px 12px; background: #F9F9F9;
}
section[data-testid="stSidebar"] .stTextInput input:focus {
    border-color: #007AFF; background: #FFF;
}

/* タブ（セグメントコントロール） */
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

/* メトリクスカード */
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

/* ファイルアップローダー */
[data-testid="stFileUploadDropzone"] {
    background: #FFFFFF !important; border: 1.5px dashed #C7C7CC !important;
    border-radius: 14px !important;
}

/* ボタン */
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

/* アラート */
.stAlert { border-radius: 12px; border: none; }
hr { border: none; border-top: 1px solid #D1D1D6; margin: 20px 0; }
</style>
""", unsafe_allow_html=True)


# ── ヘルパー ────────────────────────────────────────────────

def page_title(title, subtitle=""):
    sub = f'<div style="font-size:15px;color:#8E8E93;margin-top:6px;">{subtitle}</div>' if subtitle else ""
    st.markdown(f"""
    <div style="margin-bottom:6px;">
        <div style="font-size:34px;font-weight:700;color:#1C1C1E;letter-spacing:-0.5px;">{title}</div>
        {sub}
    </div>""", unsafe_allow_html=True)

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

def card_ok(name, jukyu, ym, days, og, ret):
    st.markdown(f"""
    <div style="background:#FFFFFF;border-radius:14px;padding:16px 20px;margin:8px 0;
         box-shadow:0 1px 4px rgba(0,0,0,0.07);">
        <div style="display:flex;align-items:center;gap:12px;">
            <div style="width:10px;height:10px;border-radius:50%;
                 background:#34C759;flex-shrink:0;"></div>
            <div>
                <div style="font-size:15px;font-weight:600;color:#1C1C1E;">{name}</div>
                <div style="font-size:13px;color:#8E8E93;margin-top:4px;">
                    受給者証 {jukyu}　·　{ym[:4]}年{ym[4:]}月　·　{days}日
                    　·　送迎 往{og} / 復{ret}
                </div>
            </div>
        </div>
    </div>""", unsafe_allow_html=True)

def card_err(label, msg):
    st.markdown(f"""
    <div style="background:#FFFFFF;border-radius:14px;padding:16px 20px;margin:8px 0;
         box-shadow:0 1px 4px rgba(0,0,0,0.07);">
        <div style="display:flex;align-items:center;gap:12px;">
            <div style="width:10px;height:10px;border-radius:50%;
                 background:#FF3B30;flex-shrink:0;"></div>
            <div>
                <div style="font-size:15px;font-weight:600;color:#1C1C1E;">{label}</div>
                <div style="font-size:13px;color:#FF3B30;margin-top:4px;">{msg}</div>
            </div>
        </div>
    </div>""", unsafe_allow_html=True)

def empty_state(title, body):
    st.markdown(f"""
    <div style="background:#FFFFFF;border-radius:16px;padding:48px 32px;
         text-align:center;box-shadow:0 1px 4px rgba(0,0,0,0.07);margin:8px 0;">
        <div style="font-size:17px;font-weight:600;color:#1C1C1E;margin-bottom:8px;">{title}</div>
        <div style="font-size:14px;color:#8E8E93;line-height:1.7;">{body}</div>
    </div>""", unsafe_allow_html=True)


# ── APIキー管理 ────────────────────────────────────────────
API_KEY_FILE = DATA_DIR / ".api_key"

def load_saved_api_key():
    k = os.environ.get("ANTHROPIC_API_KEY", "")
    if k: return k
    if API_KEY_FILE.exists(): return API_KEY_FILE.read_text().strip()
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

    # APIキーは Streamlit Secrets から自動取得（UIには表示しない）
    api_key = st.session_state.get("api_key", "")

    st.divider()

    st.markdown("""
<div style="font-size:13px;line-height:2.2;">
<div style="font-weight:700;color:#1C1C1E;margin-bottom:2px;">使い方（3ステップ）</div>
<div style="color:#007AFF;font-weight:600;">① 実績記録票を読み取る</div>
<div style="color:#8E8E93;font-size:12px;padding-left:12px;margin-bottom:4px;">
写真・PDFをアップロード</div>
<div style="color:#007AFF;font-weight:600;">② 内容を確認する</div>
<div style="color:#8E8E93;font-size:12px;padding-left:12px;margin-bottom:4px;">
読み取り結果をチェック・修正</div>
<div style="color:#007AFF;font-weight:600;">③ CSVを作って提出する</div>
<div style="color:#8E8E93;font-size:12px;padding-left:12px;">
国保連に提出するCSVをダウンロード</div>
</div>
    """, unsafe_allow_html=True)

    st.divider()
    st.markdown(f'<div style="font-size:11px;color:#C7C7CC;">パラザ合同会社　{APP_VERSION}</div>',
                unsafe_allow_html=True)


# ── ページタイトル ─────────────────────────────────────────
page_title("らくらく請求", "実績記録票を写真で撮るだけで、国保連への提出データを自動で作ります")
st.divider()


# ── タブ ─────────────────────────────────────────────────
tab1, tab2, tab3 = st.tabs(["① 読み取り", "② 内容確認", "③ CSV生成"])


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
            type=["jpg","jpeg","png","webp","pdf"],
            accept_multiple_files=True,
            help="1人1枚ずつでも、まとめてでもOKです"
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
        </div>
        """, unsafe_allow_html=True)

    if not uploaded_files:
        st.markdown("""
        <div style="background:#FFFFFF;border-radius:14px;padding:20px 24px;
             margin-top:16px;box-shadow:0 1px 4px rgba(0,0,0,0.05);">
            <div style="font-size:14px;color:#8E8E93;text-align:center;">
                上のエリアにファイルをドラッグするか、クリックして選択してください
            </div>
        </div>
        """, unsafe_allow_html=True)

    elif not api_key:
        st.markdown("""
        <div style="background:#FEF2F2;border-radius:12px;padding:16px 20px;margin-top:12px;
             border-left:3px solid #FF3B30;">
            <div style="font-size:14px;font-weight:600;color:#991B1B;">
                APIキーが設定されていません
            </div>
            <div style="font-size:13px;color:#7F1D1D;margin-top:4px;">
                左のサイドバーでAPIキーを入力してください
            </div>
        </div>
        """, unsafe_allow_html=True)

    else:
        st.markdown(f"""
        <div style="background:#FFFFFF;border-radius:12px;padding:14px 18px;
             margin:12px 0;box-shadow:0 1px 4px rgba(0,0,0,0.05);">
            <span style="font-size:14px;color:#1C1C1E;">
                <b>{len(uploaded_files)}件</b>のファイルが選択されています
            </span>
        </div>
        """, unsafe_allow_html=True)

        if st.button("読み取りを開始する", type="primary"):
            results = []
            progress = st.progress(0, text="読み取り中...")
            all_jobs = []

            for uf in uploaded_files:
                ext = uf.name.rsplit(".",1)[-1].lower()
                raw = uf.read()
                if ext == "pdf":
                    pages = pdf_to_png(raw)
                    for i, p in enumerate(pages):
                        all_jobs.append((p, "image/png", f"{uf.name}（{i+1}ページ目）"))
                else:
                    mime_map = {"jpg":"image/jpeg","jpeg":"image/jpeg",
                                "png":"image/png","webp":"image/webp"}
                    all_jobs.append((raw, mime_map.get(ext,"image/jpeg"), uf.name))

            for idx, (img_bytes, mime, label) in enumerate(all_jobs):
                progress.progress((idx+1)/len(all_jobs),
                                  text=f"読み取り中... {idx+1}/{len(all_jobs)}件")
                try:
                    data = ocr_jisseki(img_bytes, mime, api_key)
                    save_meisai(data)
                    results.append(("ok", data, label))
                except Exception as e:
                    results.append(("err", str(e), label))

            progress.empty()

            ok_count  = sum(1 for r in results if r[0]=="ok")
            err_count = len(results) - ok_count

            if ok_count:
                st.markdown(f"""
                <div style="background:#F0FFF4;border-radius:14px;padding:18px 22px;
                     margin:16px 0 8px 0;border-left:3px solid #34C759;">
                    <div style="font-size:16px;font-weight:700;color:#166534;">
                        {ok_count}件の読み取りが完了しました
                    </div>
                    <div style="font-size:13px;color:#166534;margin-top:4px;opacity:0.8;">
                        {f"{err_count}件は読み取れませんでした" if err_count else "すべて正常に読み取れました"}
                    </div>
                </div>
                """, unsafe_allow_html=True)

            for status, data_or_err, label in results:
                if status == "ok":
                    d = data_or_err
                    card_ok(d["児童名"], d["受給者証番号"], d["サービス提供年月"],
                            d["算定日数"], d["送迎往合計"], d["送迎復合計"])
                else:
                    card_err(label, str(data_or_err))

            if ok_count:
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
                           format_func=lambda x: "長時間（3時間以上）" if x==2 else "短時間（3時間未満）")
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
                    "保護者名": parent_name, "サービス提供年月": year_month,
                    "契約支給量": 0,
                    "実績": [{"日":d,"提供形態":katachi,"開始時間":start_time,
                              "終了時間":end_time,"送迎往":1 if sougei_ou else 0,
                              "送迎復":1 if sougei_fu else 0,"欠席":False} for d in days],
                    "算定日数": len(days),
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

    csv_files   = sorted(DATA_DIR.glob("実績明細_*.csv"), reverse=True)
    year_months = [f.stem.replace("実績明細_","") for f in csv_files]

    if not year_months:
        empty_state(
            "まだデータがありません",
            "「① 読み取り」タブで実績記録票をアップロードしてください"
        )
    else:
        selected_ym = st.selectbox("確認する月を選んでください", year_months,
                                   format_func=lambda x: f"{x[:4]}年{x[4:]}月")
        summary = get_summary(selected_ym)

        if not summary.empty:
            st.markdown('<div style="height:4px;"></div>', unsafe_allow_html=True)
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("登録人数", f"{len(summary)} 人")
            c2.metric("合計日数", f"{int(summary['算定日数'].sum())} 日")
            c3.metric("送迎（往）", f"{int(summary['送迎往合計'].sum())} 回")
            c4.metric("送迎（復）", f"{int(summary['送迎復合計'].sum())} 回")

            st.markdown('<div style="height:12px;"></div>', unsafe_allow_html=True)
            st.markdown('<div style="font-size:14px;font-weight:600;color:#1C1C1E;'
                        'margin-bottom:8px;">お子さんごとのまとめ</div>', unsafe_allow_html=True)
            st.dataframe(summary, use_container_width=True, hide_index=True)

        st.divider()
        st.markdown('<div style="font-size:14px;font-weight:600;color:#1C1C1E;'
                    'margin-bottom:4px;">日ごとの詳細</div>', unsafe_allow_html=True)
        st.caption("表のセルをクリックすると直接修正できます")

        df = load_meisai(selected_ym)
        if not df.empty:
            children  = ["全員表示"] + sorted(df["児童名"].unique().tolist())
            sel_child = st.selectbox("", children, label_visibility="collapsed")
            show_df   = df if sel_child=="全員表示" else df[df["児童名"]==sel_child]
            edited    = st.data_editor(show_df, use_container_width=True, hide_index=True)

            col_save, col_hint = st.columns([1, 3])
            with col_save:
                if st.button("修正を保存する"):
                    edited.to_csv(get_csv_path(selected_ym), index=False, encoding="utf-8-sig")
                    st.success("保存しました")

        if not year_months:
            pass
        else:
            next_step_hint("内容が確認できたら「③ CSV生成」タブへ進んでください")


# ============================================================
# TAB 3 — CSV生成
# ============================================================
with tab3:
    section_title("CSVを作って提出する",
                  "3つのステップで国保連に提出するCSVファイルを作ります")

    csv_files2   = sorted(DATA_DIR.glob("実績明細_*.csv"), reverse=True)
    year_months2 = [f.stem.replace("実績明細_","") for f in csv_files2]

    if not year_months2:
        empty_state(
            "まだデータがありません",
            "「① 読み取り」タブで実績記録票を読み取ってから、こちらに戻ってください"
        )
    else:
        selected_ym2 = st.selectbox("対象の月を選んでください", year_months2, key="csv_ym",
                                    format_func=lambda x: f"{x[:4]}年{x[4:]}月")

        # ── ステップ 1 ──────────────────────────────────────
        step_badge(1, "お子さんの情報を確認する",
                   "市町村番号と様式種別番号が正しいか確認してください")

        st.markdown("""
        <div style="background:#F8F8F8;border-radius:12px;padding:14px 18px;
             margin-bottom:14px;font-size:13px;color:#6E6E73;line-height:1.9;">
            <b style="color:#1C1C1E;">市町村番号</b>　→　受給者証に記載されている6桁の数字<br>
            <b style="color:#1C1C1E;">様式種別番号</b>　→　放課後等デイサービスは <b>0501</b>、児童発達支援は <b>0301</b>
        </div>
        """, unsafe_allow_html=True)

        summary2 = get_summary(selected_ym2)
        master   = load_master()

        if not summary2.empty:
            for _, r in summary2.iterrows():
                if str(r["受給者証番号"]) not in master["受給者証番号"].astype(str).values:
                    master = pd.concat([master, pd.DataFrame([{
                        "受給者証番号": str(r["受給者証番号"]), "児童名": r["児童名"],
                        "市町村番号": DEFAULT_MUNI, "様式種別番号": DEFAULT_SHOSHIKI,
                        "算定時間記載": DEFAULT_SANSEI,
                    }])], ignore_index=True)

        master_edited = st.data_editor(
            master, use_container_width=True, hide_index=True, key="master_editor",
            column_config={
                "市町村番号": st.column_config.TextColumn(
                    "市町村番号", help="受給者証に記載の6桁（例：212019）", required=True),
                "様式種別番号": st.column_config.SelectboxColumn(
                    "様式種別番号", options=["0501","0301"],
                    help="放デイ＝0501　児発＝0301", required=True),
                "算定時間記載": st.column_config.SelectboxColumn(
                    "算定時間記載", options=["なし","あり"],
                    help="児発で算定時間を記載する場合のみ「あり」", required=True),
            }
        )
        if st.button("内容を保存する"):
            save_master(master_edited[MASTER_COLS])
            st.success("保存しました")
            st.rerun()

        st.divider()

        # ── ステップ 2 ──────────────────────────────────────
        step_badge(2, "提出内容を最終確認する", "以下の内容で国保連に提出されます")

        if not summary2.empty:
            disp = summary2.copy()
            disp["市町村番号"]   = disp["受給者証番号"].apply(lambda x: get_muni(str(x)))
            disp["様式種別番号"] = disp["受給者証番号"].apply(lambda x: get_shoshiki(str(x)))
            st.dataframe(disp, use_container_width=True, hide_index=True)

            missing = [r["児童名"] for _, r in disp.iterrows()
                       if not get_muni(str(r["受給者証番号"]))]
            if missing:
                st.markdown(f"""
                <div style="background:#FEF2F2;border-radius:12px;padding:14px 18px;
                     margin:12px 0;border-left:3px solid #FF3B30;">
                    <div style="font-size:14px;font-weight:600;color:#991B1B;">
                        市町村番号が未設定のお子さんがいます
                    </div>
                    <div style="font-size:13px;color:#7F1D1D;margin-top:4px;">
                        {', '.join(missing)}さん　→ ステップ1の表で設定してください
                    </div>
                </div>
                """, unsafe_allow_html=True)
            else:
                st.markdown("""
                <div style="background:#F0FFF4;border-radius:12px;padding:14px 18px;
                     margin:12px 0;border-left:3px solid #34C759;">
                    <div style="font-size:14px;font-weight:600;color:#166534;">
                        全員の設定が完了しています
                    </div>
                </div>
                """, unsafe_allow_html=True)

            st.divider()

            # ── ステップ 3 ──────────────────────────────────
            proc_ym = _next_month(selected_ym2)
            step_badge(3, "CSVをダウンロードする", "ボタンを押すとファイルが作成されます")

            st.markdown(f"""
            <div style="background:#FFFFFF;border-radius:14px;padding:20px 24px;
                 margin-bottom:20px;box-shadow:0 1px 4px rgba(0,0,0,0.07);">
                <div style="font-size:13px;color:#8E8E93;margin-bottom:12px;font-weight:500;">
                    生成されるファイルの情報
                </div>
                <table style="font-size:14px;color:#3C3C43;border-collapse:collapse;">
                    <tr>
                        <td style="color:#8E8E93;padding:5px 24px 5px 0;white-space:nowrap;">
                            請求月</td>
                        <td style="font-weight:600;color:#1C1C1E;">
                            {proc_ym[:4]}年{proc_ym[4:]}月</td>
                    </tr>
                    <tr>
                        <td style="color:#8E8E93;padding:5px 24px 5px 0;">対象人数</td>
                        <td style="font-weight:600;color:#1C1C1E;">{len(summary2)}名</td>
                    </tr>
                    <tr>
                        <td style="color:#8E8E93;padding:5px 24px 5px 0;">事業所番号</td>
                        <td style="font-weight:600;color:#1C1C1E;">{JIGYOSHO['number']}</td>
                    </tr>
                </table>
            </div>
            """, unsafe_allow_html=True)

            if st.button("CSVファイルを作成する", type="primary", disabled=bool(missing)):
                try:
                    csv_bytes = generate_kokuhoren_csv(selected_ym2)
                    file_name = f"{selected_ym2}_国保連実績_{JIGYOSHO['number']}.csv"
                    st.download_button(
                        label="ダウンロードする",
                        data=csv_bytes,
                        file_name=file_name,
                        mime="text/csv",
                    )
                    st.markdown("""
                    <div style="background:#F0FFF4;border-radius:12px;padding:16px 20px;
                         margin-top:12px;border-left:3px solid #34C759;">
                        <div style="font-size:15px;font-weight:700;color:#166534;margin-bottom:6px;">
                            CSVファイルが作成されました
                        </div>
                        <div style="font-size:13px;color:#166534;line-height:1.8;">
                            上の「ダウンロードする」ボタンでファイルを保存してください。<br>
                            保存したファイルを国保連の取込送信システムにインポートすれば完了です。
                        </div>
                    </div>
                    """, unsafe_allow_html=True)
                except Exception as e:
                    st.markdown(f"""
                    <div style="background:#FEF2F2;border-radius:12px;padding:16px 20px;
                         margin-top:12px;border-left:3px solid #FF3B30;">
                        <div style="font-size:15px;font-weight:700;color:#991B1B;margin-bottom:6px;">
                            エラーが発生しました
                        </div>
                        <div style="font-size:13px;color:#7F1D1D;">{e}</div>
                    </div>
                    """, unsafe_allow_html=True)
