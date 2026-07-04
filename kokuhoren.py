"""
実績記録票 OCR → 国保連CSV 生成システム
"""
import os
import re
import csv as csv_module
import base64
import json
import io
from pathlib import Path

import math

import anthropic
import fitz
import pdfplumber
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
                "日", "提供形態", "開始時間", "終了時間", "算定時間数", "送迎往", "送迎復", "状況"]
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
    prompt = f"""これは放課後等デイサービスの「提供実績記録票」です。
記録票の情報をすべて抽出してください。

【出力形式】以下のJSONのみ返してください（説明文不要）：
{{
  "サービス提供年月": "YYYYMM形式（例：令和7年4月→202504）",
  "受給者証番号": "10桁の番号（文字列）",
  "児童名": "お子さんの名前",
  "保護者名": "保護者の名前またはnull",
  "実績": [
    {{
      "日": 数字,
      "提供形態": 数字またはnull,
      "開始時間": "HH:MM"またはnull,
      "終了時間": "HH:MM"またはnull,
      "送迎往": 1または0,
      "送迎復": 1または0,
      "欠席": trueまたはfalse
    }}
  ]
}}

【フォームの列構成】
日付 | サービス提供の状況 | 提供形態 | 開始時間 | 終了時間 | 算定時間数 | 送迎往 | 送迎復 | …

【フォーム上部の読み取りルール】
- 受給者証番号：「受給者証番号」「受給者番号」と書かれた欄の隣にある10桁の数字。子どもごとに異なる。
- 事業所番号（={JIGYOSHO['number']}）はこの施設の番号であり、受給者証番号ではない。混同しないこと。
- 児童名：「お子さんの名前」「利用者名」「児童氏名」「児童名」と書かれた欄の値。子どもの名前。
- 保護者名：「保護者氏名」「保護者名」と書かれた欄の値。保護者（親）の名前。児童名と混同しないこと。

【実績の読み取りルール】
- 「サービス提供の状況」欄に「欠席」「欠」と書かれている日 → 欠席=true、提供形態/時間=null、送迎=0
- 「サービス提供の状況」欄が空欄 かつ 提供形態も空欄の日 → 実績配列に含めない（利用なし）
- 「サービス提供の状況」欄に数字がある、または提供形態に1か2がある日 → 欠席=false
- 提供形態：1（短時間）または2（長時間）をそのまま読み取る。空欄の場合はnull
- 送迎往・復：「1」「○」「✓」がある → 1、ない・空欄 → 0
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
    data = _fix_name_swap(data)
    return data

# ============================================================
# データ管理
# ============================================================
def calc_santei_jikan(start_str, end_str):
    """開始・終了時間から算定時間数を計算（30分=0.5、以降15分刻みで+0.25）"""
    def to_min(t):
        s = str(t).replace(":", "").strip().zfill(4)
        return int(s[:2]) * 60 + int(s[2:]) if s.isdigit() else 0
    total = to_min(end_str) - to_min(start_str)
    if total < 30:
        return 0.0
    return math.ceil(total / 15) * 0.25

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
    df = _read_csv(get_csv_path(ym), MEISAI_COLS)
    if "算定時間数" not in df.columns:
        df["算定時間数"] = df.apply(
            lambda r: str(calc_santei_jikan(r["開始時間"], r["終了時間"]))
                      if r.get("状況") == "提供" else "",
            axis=1,
        )
    return df

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
            "提供形態":   "" if r["欠席"] else str(r["提供形態"] or ""),
            "開始時間":   "" if r["欠席"] else str(r["開始時間"] or ""),
            "終了時間":   "" if r["欠席"] else str(r["終了時間"] or ""),
            "算定時間数": "" if r["欠席"] else str(calc_santei_jikan(
                              r["開始時間"] or "", r["終了時間"] or "")),
            "送迎往":     r["送迎往"],
            "送迎復":           r["送迎復"],
            "状況":             "欠席" if r["欠席"] else "提供",
        }
        for r in data["実績"]
    ]
    df = pd.concat([df, pd.DataFrame(rows, columns=MEISAI_COLS)], ignore_index=True)
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")

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

def _fix_name_swap(data):
    """マスターと照合して児童名・保護者名の入れ替わりを自動修正"""
    master = load_master()
    jukyu = str(data.get("受給者証番号", "")).strip()
    if not jukyu:
        return data
    matched = master[master["受給者証番号"] == jukyu]
    if matched.empty:
        return data
    known_child  = str(matched.iloc[0]["児童名"]).strip()
    ocr_child    = str(data.get("児童名", "")).strip()
    ocr_guardian = str(data.get("保護者名", "")).strip()
    if ocr_child == known_child:
        return data
    if ocr_guardian == known_child:
        data["児童名"]   = ocr_guardian
        data["保護者名"] = ocr_child
    else:
        data["児童名"] = known_child
    return data

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
        # 全日（提供＋欠席）を取得してソート
        all_df = df[df["受給者証番号"] == jukyu].copy()
        all_df["日_num"] = pd.to_numeric(all_df["日"], errors="coerce")
        all_df = all_df[all_df["状況"].isin(["提供", "欠席"])].sort_values("日_num")
        # 算定時間合計は提供日のみ
        day_df = all_df[all_df["状況"] == "提供"]
        # 算定時間数は15分刻み切り上げ後の小数時間×100を4桁（国保連CSV形式）
        # 例: 1h40m → calc_santei_jikan → 1.75 → 175 → "0175"
        total_jikan_hundred = (
            sum(int(round(calc_santei_jikan(r["開始時間"], r["終了時間"]) * 100))
                for _, r in day_df.iterrows())
            if sansei else 0
        )
        rec_no += 1
        b = BASIC_TEMPLATE.copy()
        b[1] = str(rec_no); b[4] = ym;       b[5] = muni; b[6] = JIGYOSHO["number"]
        b[7] = jukyu;       b[8] = shoshiki
        b[20] = str(total_jikan_hundred).zfill(5) if sansei else "00000"
        b[35] = str(sougei)
        lines.append(_line(b, BASIC_QUOTE))
        for _, dr in all_df.iterrows():
            rec_no += 1
            is_absent = str(dr.get("状況", "")) == "欠席"
            s = "0000" if is_absent else _hhmm(dr["開始時間"])
            e = "0000" if is_absent else _hhmm(dr["終了時間"])
            katachi = str(dr["提供形態"]).strip()
            k = katachi if katachi in ("1", "2") else "1"
            day_jikan_hundred = (
                int(round(calc_santei_jikan(dr["開始時間"], dr["終了時間"]) * 100))
                if (sansei and not is_absent) else 0
            )
            d = DETAIL_TEMPLATE.copy()
            d[1]  = str(rec_no); d[4]  = ym;      d[5]  = muni; d[6] = JIGYOSHO["number"]
            d[7]  = jukyu;       d[8]  = shoshiki; d[10] = str(int(dr["日_num"]))
            d[11] = "2" if is_absent else "0"   # サービス提供の状況: 0=提供, 2=欠席(加算なし)
            d[15] = s;           d[16] = e
            d[17] = str(day_jikan_hundred).zfill(4) if sansei else "0000"
            d[22] = "0" if is_absent else _int_field(dr["送迎往"])
            d[23] = "0" if is_absent else _int_field(dr["送迎復"])
            d[35] = "0" if (is_absent or is_jihatsu) else k
            lines.append(_line(d, DETAIL_QUOTE))
    ctrl = _line(["1", "1", "0", str(len(lines)), JIGYOSHO["data_type"], "0",
                  JIGYOSHO["number"], "0", "1", proc_ym, ""], CTRL_QUOTE)
    return ("\r\n".join([ctrl] + lines + [f"3,{rec_no + 1}"]) + "\r\n").encode("cp932", errors="replace")


# ============================================================
# UI ヘルパー
# ============================================================


# ── v2 デザイントークン ───────────────────────────────────────
_C = {
    "card":   "#FCFBF8",
    "prim":   "#3F7A5C",
    "pmid":   "#4E8F6F",
    "plight": "#DCEBE0",
    "txt":    "#2E2A22",
    "txt2":   "#7A7469",
    "muted":  "#9C978C",
    "warn":   "#C97A3D",
    "border": "#E4DED2",
    "ok_bg":  "#EBF5F0",
    "ok_dot": "#2E7D5A",
    "ow_bg":  "#FEF6EC",
    "ow_dot": "#C97A3D",
    "err_bg": "#FEF2F2",
    "err_dot":"#C0392B",
}

def section_title(title, subtitle=""):
    sub = (f'<div style="font-size:13.5px;color:{_C["txt2"]};margin-top:5px;line-height:1.6;">'
           f'{subtitle}</div>') if subtitle else ""
    st.markdown(f"""
    <div style="margin:28px 0 16px 0;">
        <div style="font-size:21px;font-weight:700;color:{_C["txt"]};letter-spacing:-0.2px;">{title}</div>
        {sub}
    </div>""", unsafe_allow_html=True)

def step_badge(num, title, subtitle=""):
    sub = (f'<div style="font-size:13px;color:{_C["txt2"]};margin-top:3px;">'
           f'{subtitle}</div>') if subtitle else ""
    st.markdown(f"""
    <div style="display:flex;align-items:flex-start;gap:13px;margin:28px 0 14px 0;">
        <div style="min-width:32px;height:32px;border-radius:50%;background:{_C["prim"]};
             color:white;font-size:14px;font-weight:700;display:flex;align-items:center;
             justify-content:center;margin-top:2px;flex-shrink:0;">{num}</div>
        <div>
            <div style="font-size:18px;font-weight:700;color:{_C["txt"]};">{title}</div>
            {sub}
        </div>
    </div>""", unsafe_allow_html=True)

def next_step_hint(text):
    st.markdown(f"""
    <div style="background:{_C["plight"]};border-radius:12px;padding:14px 18px;margin:18px 0;
         border-left:3px solid {_C["prim"]};">
        <span style="font-size:14px;color:{_C["prim"]};font-weight:600;">次のステップ　→　{text}</span>
    </div>""", unsafe_allow_html=True)

def empty_state(title, body):
    st.markdown(f"""
    <div style="background:{_C["card"]};border-radius:20px;padding:52px 32px;text-align:center;
         box-shadow:0 2px 8px rgba(0,0,0,0.05);margin:8px 0;border:1px solid {_C["border"]};">
        <div style="font-size:17px;font-weight:700;color:{_C["txt"]};margin-bottom:8px;">{title}</div>
        <div style="font-size:14px;color:{_C["txt2"]};line-height:1.8;">{body}</div>
    </div>""", unsafe_allow_html=True)

def alert(status, msg):
    cfg = {
        "ok":    (_C["ok_bg"],  _C["ok_dot"],  "#1A5C3A"),
        "warn":  (_C["ow_bg"],  _C["warn"],    "#7A4A1A"),
        "error": (_C["err_bg"], _C["err_dot"], "#8B1A1A"),
    }
    bg, bc, tc = cfg[status]
    st.markdown(f"""
    <div style="background:{bg};border-radius:14px;padding:16px 20px;
         margin:14px 0;border-left:3px solid {bc};">
        <div style="font-size:15px;font-weight:700;color:{tc};">{msg}</div>
    </div>""", unsafe_allow_html=True)

def _card(dot_color, title_html, detail="", bg=None):
    bg = bg or _C["card"]
    detail_html = (f'<div style="font-size:13px;color:{_C["txt2"]};margin-top:5px;">{detail}</div>'
                   if detail else "")
    st.markdown(f"""
    <div style="background:{bg};border-radius:16px;padding:17px 22px;margin:8px 0;
         box-shadow:0 2px 8px rgba(0,0,0,0.05);border:1px solid {_C["border"]};">
        <div style="display:flex;align-items:center;gap:13px;">
            <div style="width:10px;height:10px;border-radius:50%;
                 background:{dot_color};flex-shrink:0;"></div>
            <div style="flex:1;">
                <div style="font-size:15px;font-weight:600;color:{_C["txt"]};">{title_html}</div>
                {detail_html}
            </div>
        </div>
    </div>""", unsafe_allow_html=True)

def _child_detail(jukyu, ym, days, og, ret):
    return (f'受給者証 {jukyu}　·　{ym[:4]}年{ym[4:]}月　·　'
            f'<b style="color:{_C["txt"]};">{days}日</b>　·　送迎 往{og} / 復{ret}')

def card_ok(name, jukyu, ym, days, og, ret):
    _card(_C["ok_dot"], name, _child_detail(jukyu, ym, days, og, ret), bg=_C["ok_bg"])

def card_overwrite(name, jukyu, ym, days, og, ret):
    title = (f'{name}　<span style="font-size:12px;font-weight:500;color:{_C["warn"]};">'
             f'⚠ 同じ月のデータが既にありました。上書きしました。</span>')
    _card(_C["ow_dot"], title, _child_detail(jukyu, ym, days, og, ret), bg=_C["ow_bg"])

def card_err(label, msg):
    _card(_C["err_dot"], label,
          f'<span style="color:{_C["err_dot"]};">{msg}</span>', bg=_C["err_bg"])



def _parse_meisai_pdf_for_master(pdf_bytes: bytes, api_key: str) -> list:
    """明細書PDFをClaude Vision APIで読み取りマスター候補を返す"""
    client = anthropic.Anthropic(api_key=api_key)
    prompt = """これは国保連（国民健康保険団体連合会）から届いた障害福祉サービスの「明細書」です。
以下の情報を抽出してJSONのみ返してください（説明文不要）：
{
  "受給者証番号": "10桁の数字（文字列）またはnull",
  "児童名": "受給者・利用者のお子さんの名前またはnull",
  "市町村番号": "6桁の数字（文字列）またはnull",
  "様式種別番号": "放課後等デイサービスなら0501、児童発達支援なら0301"
}
【読み取りのヒント】
- 受給者証番号：「受給者証番号」「受給者番号」と書かれた欄の隣の10桁の数字
- 市町村番号：「市町村番号」「市区町村番号」と書かれた欄の6桁の数字
- 児童名：「受給者氏名」「利用者名」「お子さんの名前」などの欄の名前
- サービス種別：「放課後等デイサービス」か「児童発達支援」のどちらかが記載されている"""

    pages_png = pdf_to_png(pdf_bytes)
    rows, seen = [], set()
    for png_bytes in pages_png:
        b64 = base64.standard_b64encode(png_bytes).decode("utf-8")
        try:
            resp = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=500,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64",
                                                 "media_type": "image/png", "data": b64}},
                    {"type": "text", "text": prompt},
                ]}],
            )
            text = resp.content[0].text
            start, end = text.find("{"), text.rfind("}") + 1
            if start == -1:
                continue
            d = json.loads(text[start:end])
            jukyu = str(d.get("受給者証番号") or "").strip()
            if not jukyu or jukyu in seen:
                continue
            seen.add(jukyu)
            rows.append({
                "受給者証番号": jukyu,
                "児童名":       str(d.get("児童名") or "").strip(),
                "市町村番号":   str(d.get("市町村番号") or "").strip(),
                "様式種別番号": str(d.get("様式種別番号") or "0501").strip(),
                "算定時間記載": "なし",
            })
        except Exception:
            continue
    return rows


def _parse_kokuhoren_csv_for_master(csv_bytes: bytes) -> list:
    """過去生成した国保連CSV（Shift-JIS）から受給者証番号・市町村番号・様式種別番号を抽出"""
    rows, seen = [], set()
    try:
        text = csv_bytes.decode("cp932", errors="replace")
    except Exception:
        text = csv_bytes.decode("utf-8", errors="replace")
    for row in csv_module.reader(io.StringIO(text)):
        if len(row) < 9 or row[3].strip() != "01":
            continue
        jukyu = row[7].strip()
        if not jukyu or jukyu in seen:
            continue
        seen.add(jukyu)
        rows.append({"受給者証番号": jukyu, "児童名": "",
                     "市町村番号": row[5].strip(), "様式種別番号": row[8].strip() or "0501",
                     "算定時間記載": "なし"})
    return rows


def _merge_into_master(new_rows: list) -> tuple:
    """new_rowsを既存マスターにマージ。戻り値は (追加数, 更新数)"""
    m = load_master()
    added, updated = 0, 0
    for r in new_rows:
        jukyu = str(r["受給者証番号"]).strip()
        existing = m[m["受給者証番号"] == jukyu]
        if existing.empty:
            row_df = pd.DataFrame([{c: r.get(c, "") for c in MASTER_COLS}])
            m = pd.concat([m, row_df], ignore_index=True)
            added += 1
        else:
            idx = existing.index[0]
            for col in ["市町村番号", "様式種別番号", "算定時間記載"]:
                if r.get(col):
                    m.at[idx, col] = r[col]
            if r.get("児童名") and not m.at[idx, "児童名"]:
                m.at[idx, "児童名"] = r["児童名"]
            updated += 1
    save_master(m)
    return added, updated


def render(settings):
    """国保連請求ページ（実績記録票→CSV）"""
    # 事業所番号・APIキーは共通設定から取得
    JIGYOSHO["number"] = settings.get("jigyosho_number") or JIGYOSHO["number"]
    if settings.get("anthropic_api_key"):
        st.session_state["api_key"] = settings["anthropic_api_key"]

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


    # ── 国保連ページ専用 CSS ────────────────────────────────────
    st.markdown(f"""<style>
/* タブをグリーン系に上書き */
.stTabs [data-baseweb="tab-list"] {{
    background: #E8E2D8 !important;
    border-radius: 12px !important;
    padding: 4px !important;
    gap: 3px !important;
}}
.stTabs [data-baseweb="tab"] {{
    border-radius: 9px !important;
    font-weight: 500 !important;
    color: {_C["txt2"]} !important;
    font-size: 13px !important;
}}
.stTabs [aria-selected="true"] {{
    background: {_C["card"]} !important;
    color: {_C["txt"]} !important;
    font-weight: 700 !important;
    box-shadow: 0 1px 4px rgba(0,0,0,0.09) !important;
}}
/* プライマリボタンをグリーンに上書き */
section[data-testid="stMain"] .stButton > button[kind="primary"] {{
    background: {_C["prim"]} !important;
    border-color: {_C["prim"]} !important;
    box-shadow: 0 2px 10px rgba(63,122,92,0.28) !important;
}}
section[data-testid="stMain"] .stButton > button[kind="primary"]:hover {{
    background: {_C["pmid"]} !important;
    border-color: {_C["pmid"]} !important;
}}
/* ファイルアップローダー */
[data-testid="stFileUploaderDropzone"] {{
    background: #F5F1EB !important;
    border: 1.5px dashed #C4BAB0 !important;
    border-radius: 16px !important;
}}
[data-testid="stFileUploaderDropzone"]:hover {{
    border-color: {_C["prim"]} !important;
}}
/* メトリクスカード */
[data-testid="stMetric"] {{
    background: {_C["card"]};
    border-radius: 14px;
    padding: 14px;
    border: 1px solid {_C["border"]};
}}
/* エクスパンダー */
[data-testid="stExpander"] {{
    border: 1px solid {_C["border"]} !important;
    border-radius: 12px !important;
}}
/* データエディタ角丸 */
[data-testid="stDataFrameResizable"] {{
    border-radius: 12px;
    overflow: hidden;
    border: 1px solid {_C["border"]};
}}
</style>""", unsafe_allow_html=True)

    api_key = st.session_state.get("api_key", "")

    # ── ページヘッダー ─────────────────────────────────────────
    st.markdown(f"""
    <div style="display:flex;align-items:center;gap:16px;margin-bottom:4px;">
      <div style="width:52px;height:52px;border-radius:15px;background:{_C["plight"]};
           display:flex;align-items:center;justify-content:center;flex-shrink:0;">
        <div style="display:flex;align-items:flex-end;gap:3.5px;height:20px;">
          <div style="width:5px;height:10px;background:{_C["prim"]};border-radius:1.5px;"></div>
          <div style="width:5px;height:20px;background:{_C["prim"]};border-radius:1.5px;"></div>
          <div style="width:5px;height:14px;background:{_C["prim"]};border-radius:1.5px;"></div>
        </div>
      </div>
      <div>
        <div style="font-size:26px;font-weight:800;color:{_C["txt"]};
             letter-spacing:-0.3px;line-height:1.2;">国保連請求</div>
        <div style="font-size:14px;color:{_C["txt2"]};margin-top:3px;">
             実績記録票を写真で撮るだけで、国保連への提出データを自動で作ります</div>
      </div>
    </div>
    <div style="height:1px;background:{_C["border"]};margin:16px 0 20px 0;"></div>
    """, unsafe_allow_html=True)

    # ── タブ ─────────────────────────────────────────────────
    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        ["① 読み取り", "② 内容確認", "③ 請求前チェック", "④ CSV生成", "⑤ 児童マスター"]
    )


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
            tips = [("明るい場所で撮る"), ("真上からまっすぐ撮る"), ("ピントを合わせる"), ("PDFでもOK")]
            tips_html = "".join(
                f'<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">'
                f'<div style="width:18px;height:18px;border-radius:50%;background:{_C["plight"]};'
                f'flex-shrink:0;display:flex;align-items:center;justify-content:center;">'
                f'<div style="width:4px;height:7px;border-right:2px solid {_C["prim"]};'
                f'border-bottom:2px solid {_C["prim"]};transform:rotate(40deg);'
                f'margin-top:-2px;"></div></div>'
                f'<span>{t}</span></div>'
                for t in tips
            )
            st.markdown(f"""
            <div style="background:{_C["card"]};border-radius:16px;padding:18px 20px;
                 box-shadow:0 2px 8px rgba(0,0,0,0.05);margin-top:6px;
                 border:1px solid {_C["border"]};">
                <div style="font-size:13px;font-weight:700;color:{_C["prim"]};margin-bottom:12px;">
                    うまく読み取れないときは
                </div>
                <div style="font-size:13px;color:{_C["txt2"]};line-height:1.5;">
                    {tips_html}
                </div>
            </div>""", unsafe_allow_html=True)

        if not uploaded_files:
            st.markdown(f"""
            <div style="background:{_C["plight"]};border-radius:14px;padding:16px 22px;
                 margin-top:14px;border:1px solid {_C["border"]};">
                <div style="font-size:13.5px;color:{_C["prim"]};text-align:center;font-weight:500;">
                    ↑ ファイルをドラッグするか、クリックして選択してください<br>
                    <span style="font-size:12px;font-weight:400;color:{_C["txt2"]};">
                    200MB以内 · JPG, PNG, WEBP, PDF</span>
                </div>
            </div>""", unsafe_allow_html=True)

        elif not api_key:
            alert("error", "APIキーが設定されていません。Streamlit Secrets に ANTHROPIC_API_KEY を設定してください。")

        else:
            st.markdown(f"""
            <div style="background:{_C["ok_bg"]};border-radius:12px;padding:13px 18px;margin:12px 0;
                 border:1px solid {_C["border"]};">
                <span style="font-size:14px;color:{_C["ok_dot"]};font-weight:600;">
                    ✓ <b>{len(uploaded_files)}件</b>のファイルが選択されています
                </span>
            </div>""", unsafe_allow_html=True)

            if st.button("読み取りを開始する", type="primary"):
                all_jobs = []
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

                ocr_done, ocr_pending, ocr_err = [], [], []
                progress = st.progress(0, text="読み取り中...")
                master_jukyus = set(load_master()["受給者証番号"].astype(str))
                for idx, (img_bytes, mime, label) in enumerate(all_jobs):
                    progress.progress((idx + 1) / len(all_jobs),
                                      text=f"読み取り中... {idx+1}/{len(all_jobs)}件")
                    try:
                        data = ocr_jisseki(img_bytes, mime, api_key)
                        jukyu = str(data["受給者証番号"])
                        if jukyu in master_jukyus:
                            is_dup = check_duplicate(data)
                            save_meisai(data)
                            ocr_done.append({"status": "overwrite" if is_dup else "ok",
                                             "label": label, "data": data})
                        else:
                            ocr_pending.append({"label": label, "data": data})
                    except Exception as e:
                        ocr_err.append({"label": label, "error": str(e)})
                progress.empty()
                st.session_state["ocr_done"]    = ocr_done
                st.session_state["ocr_pending"] = ocr_pending
                st.session_state["ocr_err"]     = ocr_err
                st.rerun()

            # ── OCR結果の表示 ──────────────────────────────
            ocr_done    = st.session_state.get("ocr_done", [])
            ocr_pending = st.session_state.get("ocr_pending", [])
            ocr_err     = st.session_state.get("ocr_err", [])

            for r in ocr_done:
                d = r["data"]
                fn = card_overwrite if r["status"] == "overwrite" else card_ok
                fn(d["児童名"], d["受給者証番号"], d["サービス提供年月"],
                   d["算定日数"], d["送迎往合計"], d["送迎復合計"])
            for r in ocr_err:
                card_err(r["label"], r["error"])

            if ocr_done:
                ow = sum(1 for r in ocr_done if r["status"] == "overwrite")
                msg = f"{len(ocr_done)}件を保存しました" + (f"（うち{ow}件は上書き）" if ow else "")
                st.success(msg)

            # ── 未登録児童の確認 ───────────────────────────
            if ocr_pending:
                st.markdown(f"""
                    <div style="background:{_C["ow_bg"]};border-radius:16px;padding:16px 20px;
                         margin:16px 0;border-left:3px solid {_C["warn"]};">
                      <div style="font-size:15px;font-weight:700;color:{_C["warn"]};">
                        ⚠️ マスター未登録のお子さんが {len(ocr_pending)} 名います
                      </div>
                      <div style="font-size:13px;color:{_C["txt2"]};margin-top:5px;">
                        読み取り間違いの可能性があります。内容を確認して「追加して保存」か「無視」を選んでください。
                      </div>
                    </div>""", unsafe_allow_html=True)

                remove_idx = []
                for i, r in enumerate(ocr_pending):
                    d = r["data"]
                    st.markdown(f"""
                        <div style="background:{_C["card"]};border-radius:16px;padding:17px 22px;
                             margin:8px 0;box-shadow:0 2px 8px rgba(0,0,0,0.05);
                             border:1px solid {_C["border"]};border-left:3px solid {_C["warn"]};">
                          <div style="font-size:15px;font-weight:600;color:{_C["txt"]};">{d["児童名"]}</div>
                          <div style="font-size:13px;color:{_C["txt2"]};margin-top:5px;">
                            受給者証 {d["受給者証番号"]}　·
                            {d["サービス提供年月"][:4]}年{d["サービス提供年月"][4:]}月　·
                            <b style="color:{_C["txt"]};">{d["算定日数"]}日</b>
                            　·　送迎 往{d["送迎往合計"]} / 復{d["送迎復合計"]}
                          </div>
                        </div>""", unsafe_allow_html=True)
                    col_add, col_ign = st.columns(2)
                    with col_add:
                        if st.button("追加して保存", key=f"pend_add_{i}", type="primary",
                                     use_container_width=True):
                            save_meisai(d)
                            upsert_master(str(d["受給者証番号"]), d["児童名"])
                            remove_idx.append(i)
                    with col_ign:
                        if st.button("無視する", key=f"pend_ign_{i}",
                                     use_container_width=True):
                            remove_idx.append(i)

                if remove_idx:
                    st.session_state["ocr_pending"] = [
                        r for j, r in enumerate(ocr_pending) if j not in remove_idx
                    ]
                    st.rerun()

            if ocr_done or ocr_pending:
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

    # ============================================================
    # TAB 5 — 児童マスター管理
    # ============================================================
    with tab5:
        section_title("児童マスター管理", "受給者証番号・児童名・市町村番号などを登録・編集できます")

        # ── データ取り込み ──────────────────────────────────
        st.markdown('<div style="font-size:14px;font-weight:600;color:#1C1C1E;margin-bottom:8px;">データ取り込み</div>',
                    unsafe_allow_html=True)
        imp_col1, imp_col2, imp_col3 = st.columns(3)

        # ① テンプレートダウンロード
        with imp_col1:
            tpl = pd.DataFrame([{
                "受給者証番号": "2010089882",
                "児童名":       "山田 太郎",
                "市町村番号":   "212019",
                "様式種別番号": "0501",
                "算定時間記載": "なし",
            }])
            st.download_button(
                "📥 CSVテンプレート",
                data=tpl.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig"),
                file_name="児童マスター_テンプレート.csv",
                mime="text/csv",
                use_container_width=True,
                help="Excelで開いて入力し、アップロードしてください",
            )

        # ② 明細書PDFから自動取得
        with imp_col2:
            with st.popover("📄 明細書PDFから取込", use_container_width=True):
                st.caption("国保連から毎月届く明細書PDFをアップロードすると、受給者証番号・サービス種別を自動で取り込みます。")
                up_meisai = st.file_uploader("明細書PDF", type=["pdf"], key="master_meisai_pdf")
                if up_meisai and st.button("取り込む", key="import_meisai", type="primary"):
                    try:
                        with st.spinner("AIで読み取り中..."):
                            rows = _parse_meisai_pdf_for_master(up_meisai.read(), api_key)
                        if rows:
                            added, updated = _merge_into_master(rows)
                            st.success(f"完了：追加 {added}名 / 更新 {updated}名")
                            if any(not r["児童名"] for r in rows):
                                st.warning("児童名が取得できなかった行があります。下の一覧で直接入力してください。")
                            st.rerun()
                        else:
                            st.error("受給者証番号が見つかりませんでした")
                    except Exception as e:
                        st.error(f"エラー: {e}")

        # ③ 国保連CSVから取込
        with imp_col3:
            with st.popover("📊 国保連CSVから取込", use_container_width=True):
                st.caption("過去に生成した国保連提出用CSVをアップロードすると、受給者証番号・市町村番号・様式種別番号を取り込みます（児童名は別途入力が必要）。")
                up_kkr = st.file_uploader("国保連CSV", type=["csv"], key="master_kkr_csv")
                if up_kkr and st.button("取り込む", key="import_kkr", type="primary"):
                    try:
                        rows = _parse_kokuhoren_csv_for_master(up_kkr.read())
                        if rows:
                            added, updated = _merge_into_master(rows)
                            st.success(f"完了：追加 {added}名 / 更新 {updated}名")
                            if any(not r["児童名"] for r in rows):
                                st.warning("児童名はCSVに含まれていません。下の一覧で直接入力してください。")
                            st.rerun()
                        else:
                            st.error("基本情報レコードが見つかりませんでした")
                    except Exception as e:
                        st.error(f"エラー: {e}")

        st.divider()

        master_all = load_master()

        with st.expander("＋ 新しいお子さんを追加する", expanded=master_all.empty):
            with st.form("add_child_form"):
                c1, c2 = st.columns(2)
                new_jukyu  = c1.text_input("受給者証番号（10桁）", placeholder="2010089882")
                new_name   = c2.text_input("児童名", placeholder="山田 太郎")
                c3, c4, c5_col = st.columns(3)
                new_muni   = c3.text_input("市町村番号（6桁）", value=DEFAULT_MUNI,
                                           help="受給者証に記載の6桁（例：212019）")
                new_shoshi = c4.selectbox("様式種別番号", options=["0501", "0301"],
                                          help="放デイ＝0501　児発＝0301")
                new_sansei = c5_col.selectbox("算定時間記載", options=["なし", "あり"],
                                              help="児発で算定時間を記載する場合のみ「あり」")
                submitted = st.form_submit_button("追加する", type="primary")

            if submitted:
                if not new_jukyu.strip() or not new_name.strip():
                    st.error("受給者証番号と児童名は必須です")
                elif new_jukyu.strip() in load_master()["受給者証番号"].values:
                    st.error(f"受給者証番号 {new_jukyu.strip()} はすでに登録されています")
                else:
                    m = load_master()
                    new_row = pd.DataFrame([{
                        "受給者証番号": new_jukyu.strip(),
                        "児童名":       new_name.strip(),
                        "市町村番号":   new_muni.strip(),
                        "様式種別番号": new_shoshi,
                        "算定時間記載": new_sansei,
                    }])
                    save_master(pd.concat([m, new_row], ignore_index=True))
                    st.success(f"{new_name.strip()}さんを追加しました")
                    st.rerun()

        st.markdown('<div style="height:8px;"></div>', unsafe_allow_html=True)

        master_all = load_master()
        if master_all.empty:
            empty_state("まだ登録されていません", "上の「＋ 新しいお子さんを追加する」から登録してください")
        else:
            st.markdown(f'<div style="font-size:14px;font-weight:600;color:#1C1C1E;margin-bottom:8px;">'
                        f'登録済み　{len(master_all)}名</div>', unsafe_allow_html=True)
            st.caption("表のセルをクリックすると直接修正できます。修正後は「変更を保存する」を押してください。")

            edited_master = st.data_editor(
                master_all,
                use_container_width=True,
                hide_index=True,
                key="master_all_editor",
                column_config={
                    "受給者証番号": st.column_config.TextColumn("受給者証番号", required=True),
                    "児童名":       st.column_config.TextColumn("児童名",       required=True),
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

            col_save, col_del = st.columns([3, 1])
            with col_save:
                if st.button("変更を保存する", type="primary", key="save_master_all"):
                    save_master(edited_master[MASTER_COLS])
                    st.success("保存しました")
                    st.rerun()
            with col_del:
                with st.popover("削除する"):
                    del_options = master_all["受給者証番号"].tolist()
                    del_target = st.selectbox(
                        "削除するお子さんの受給者証番号",
                        del_options,
                        format_func=lambda x: f"{x}　{master_all[master_all['受給者証番号']==x]['児童名'].values[0]}",
                        key="del_select",
                    )
                    if st.button("削除を実行する", type="primary", key="do_delete"):
                        m = load_master()
                        m = m[m["受給者証番号"] != del_target]
                        save_master(m)
                        st.success("削除しました")
                        st.rerun()

        st.divider()

        # ── バックアップ ─────────────────────────────────────────
        with st.expander("児童マスターのバックアップ"):
            st.caption("コンテナ停止後の復元用に手元に保存しておいてください")
            if MASTER_PATH.exists():
                st.download_button(
                    "マスターをダウンロード",
                    data=MASTER_PATH.read_bytes(),
                    file_name="児童マスター.csv",
                    mime="text/csv",
                )
            else:
                st.caption("まだマスターデータがありません")
            uploaded_master = st.file_uploader("マスターを復元（CSVをアップロード）",
                                               type="csv", key="master_upload")
            if uploaded_master:
                save_master(pd.read_csv(uploaded_master, dtype=str, keep_default_na=False))
                st.success("復元しました")
                st.rerun()

        # ── データ管理 ─────────────────────────────────────────
        with st.expander("データ管理（削除）"):
            if st.session_state.pop("kokuhoren_reset_done", False):
                st.success("削除しました")

            months = get_meisai_months()
            has_master = MASTER_PATH.exists()

            if months:
                st.caption(f"実績データ：{', '.join(m[:4]+'年'+m[4:]+'月' for m in months)}")
            else:
                st.caption("実績データ：なし")
            st.caption(f"児童マスター：{'あり（' + str(len(load_master())) + '名）' if has_master else 'なし'}")

            del_jisseki = st.checkbox("実績データを削除", key="del_jisseki",
                                      value=False, disabled=not months)
            del_master_chk = st.checkbox("児童マスターを削除", key="del_master",
                                         value=False, disabled=not has_master)

            nothing_selected = not del_jisseki and not del_master_chk
            confirm = st.checkbox("削除に同意する", key="reset_confirm",
                                  disabled=nothing_selected)

            if st.button("削除を実行する", type="secondary", key="reset_all",
                         disabled=nothing_selected or not confirm):
                if del_jisseki:
                    for f in DATA_DIR.glob("実績明細_*.csv"):
                        f.unlink()
                if del_master_chk and MASTER_PATH.exists():
                    MASTER_PATH.unlink()
                st.session_state["kokuhoren_reset_done"] = True
                st.rerun()

