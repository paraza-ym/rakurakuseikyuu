"""
らくらく書類作成 v1.0
障害福祉施設向け：代理受領通知書・保護者請求書を一括生成
"""
import io
import re
import json
import hashlib
import zipfile
import datetime
from pathlib import Path

import streamlit as st
import pdfplumber
from pdf2image import convert_from_bytes
from PIL import Image
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont

# ============================================================
# フォント登録（一度だけ）
# ============================================================
for fname in ("HeiseiMin-W3", "HeiseiKakuGo-W5"):
    try:
        pdfmetrics.registerFont(UnicodeCIDFont(fname))
    except Exception:
        pass

MIN  = "HeiseiMin-W3"
BOLD = "HeiseiKakuGo-W5"

# ============================================================
# ブランド設定（コーラル）
# ============================================================
APP_NAME = "らくらく書類"              # 「らくらく請求」シリーズの書類作成版
TAGLINE  = "障害福祉の書類を、らくらく。"
CORAL    = "#F26749"


def brand_mark(size: int = 36, radius: int = 10, font: int = 18) -> str:
    """コーラルのロゴマーク（角丸四角＋📄）のHTMLを返す"""
    return (
        f'<div style="width:{size}px;height:{size}px;border-radius:{radius}px;'
        f'background:linear-gradient(135deg,#FF8A6B,{CORAL});color:#fff;'
        f'display:inline-flex;align-items:center;justify-content:center;'
        f'font-size:{font}px;box-shadow:0 2px 8px rgba(242,103,73,0.35);'
        f'flex-shrink:0;">📄</div>'
    )

# ============================================================
# 保存用フォルダ（印鑑などを次回以降も使えるように保持）
# ============================================================
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
STAMP_PATH = DATA_DIR / "stamp.png"
SETTINGS_PATH = DATA_DIR / "settings.json"

# 施設ごとの初期値（販売時は空にして各施設に入力してもらう）
DEFAULTS = {
    "company_name":     "一般社団法人PARAZA KIDS",
    "facility_name":    "サードストリート",
    "manager_title":    "代表理事",
    "manager_name":     "森　康行",
    "postal":           "501-6004",
    "address":          "岐阜県羽島郡岐南町野中5-57-1",
    "default_pay":      "銀行振込",
    "dairi_doc_title":  "障がい児通所給付受領通知書",
    "seikyu_doc_title": "御請求書",
    "pay_due":          "ご利用月の翌月末日（土日祝の場合は翌平日）",
    "footer_note":      "恐れ入りますが、お振込み手数料は負担お願い申し上げます。",
    "bank_name":        "住信SBIネット銀行",
    "bank_branch":      "法人第一支店（106）",
    "bank_account":     "（普通）2047071",
    "bank_holder":      "シヤ）パラザキツズ",
    "jigyosho_number":  "2150600183",
    "anthropic_api_key": "",
}


def load_settings() -> dict:
    if SETTINGS_PATH.exists():
        try:
            return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_settings(d: dict) -> None:
    SETTINGS_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def hash_pw(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def sv(settings: dict, key: str) -> str:
    """保存済みの値があればそれを、なければ初期値を返す"""
    return settings.get(key, DEFAULTS.get(key, ""))


def load_stamp_img():
    if STAMP_PATH.exists():
        try:
            return Image.open(io.BytesIO(STAMP_PATH.read_bytes()))
        except Exception:
            return None
    return None

# ============================================================
# ページ設定
# ============================================================
st.set_page_config(
    page_title=APP_NAME,
    page_icon="📄",
    layout="wide",
    initial_sidebar_state="expanded",
)

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
.stApp { background: #FAF6F3; }
.main .block-container { padding-top: 32px; padding-bottom: 48px; }
section[data-testid="stSidebar"] {
    background: #FFFFFF; border-right: 1px solid #EFE6E0;
}
.stTabs [data-baseweb="tab-list"] {
    background: #F1E7E1; border-radius: 10px; padding: 3px; gap: 2px;
}
.stTabs [data-baseweb="tab"] {
    font-size: 14px; font-weight: 500; color: #8A7F79;
    border-radius: 8px; padding: 7px 18px; border: none; background: transparent;
}
.stTabs [aria-selected="true"] {
    background: #FFFFFF !important; color: #C0492E !important;
    font-weight: 600; box-shadow: 0 1px 3px rgba(0,0,0,0.12);
}
.stButton > button {
    border-radius: 980px; font-weight: 500; font-size: 15px;
    padding: 9px 22px; border: 1.5px solid #E3D8D1;
    background: #FFFFFF; color: #2A2420;
}
.stButton > button[kind="primary"] {
    background: #F26749; color: #FFFFFF; border: none;
    box-shadow: 0 2px 10px rgba(242,103,73,0.32);
}
.stButton > button[kind="primary"]:hover { background: #E0573B; }
[data-testid="stFileUploadDropzone"] {
    background: #FFFFFF !important; border: 1.5px dashed #E3C9BF !important;
    border-radius: 14px !important;
}
[data-testid="stFileUploadDropzone"]:hover {
    border-color: #F26749 !important;
}

/* ---- 入力欄を「クリックして編集できる枠」として明確に見せる ---- */
.stTextInput label, .stDateInput label, .stTextArea label {
    font-size: 13px !important; font-weight: 600 !important;
    color: #3C3C43 !important; margin-bottom: 2px !important;
}
.stTextInput [data-baseweb="input"],
.stTextInput [data-baseweb="base-input"],
.stDateInput [data-baseweb="input"],
.stTextArea [data-baseweb="textarea"] {
    background: #FFFFFF !important;
    border: 1.5px solid #C7C7CC !important;
    border-radius: 10px !important;
    box-shadow: 0 1px 2px rgba(0,0,0,0.05) !important;
    transition: border-color .15s, box-shadow .15s;
}
.stTextInput [data-baseweb="input"]:hover,
.stDateInput [data-baseweb="input"]:hover {
    border-color: #F26749 !important;
}
.stTextInput [data-baseweb="input"]:focus-within,
.stDateInput [data-baseweb="input"]:focus-within,
.stTextArea [data-baseweb="textarea"]:focus-within {
    border-color: #F26749 !important;
    box-shadow: 0 0 0 3px rgba(242,103,73,0.18) !important;
}
.stTextInput input, .stDateInput input, .stTextArea textarea {
    background: transparent !important;
    font-size: 15px !important; color: #1C1C1E !important;
    padding: 9px 12px !important;
}
.stTextInput input::placeholder { color: #B0B0B5 !important; }

/* 設定フォーム内の見出し（#### …）を見やすく */
[data-testid="stForm"] h4 {
    font-size: 15px !important; font-weight: 700 !important;
    color: #1C1C1E !important; margin: 18px 0 2px !important;
    padding-bottom: 6px; border-bottom: 1px solid #E5E5EA;
}
[data-testid="stForm"] {
    background: #FFFFFF; border: 1px solid #E5E5EA;
    border-radius: 16px; padding: 8px 24px 20px !important;
    box-shadow: 0 1px 4px rgba(0,0,0,0.05);
}
</style>
""", unsafe_allow_html=True)


# ============================================================
# ログイン画面
# ============================================================
def render_login(settings: dict) -> None:
    st.markdown(
        '<div style="max-width:360px;margin:10vh auto 0;text-align:center;">'
        '<div style="display:flex;justify-content:center;margin-bottom:14px;">'
        + brand_mark(56, 16, 28) +
        '</div>'
        f'<div style="font-size:26px;font-weight:700;color:#2A2420;">{APP_NAME}</div>'
        f'<div style="color:{CORAL};font-size:13px;font-weight:600;margin-top:4px;">{TAGLINE}</div>'
        '<div style="color:#9B8E86;margin:18px 0 26px;font-size:14px;">'
        'パスワードを入力してください</div></div>',
        unsafe_allow_html=True,
    )
    _, mid, _ = st.columns([1, 2, 1])
    with mid:
        pw = st.text_input("パスワード", type="password", key="login_pw",
                           label_visibility="collapsed", placeholder="パスワード")
        if st.button("ログイン", type="primary", use_container_width=True):
            if hash_pw(pw) == settings.get("password_hash"):
                st.session_state["authed"] = True
                st.rerun()
            else:
                st.error("パスワードが違います")


# ============================================================
# 設定・初期設定（オンボーディング）画面
# ============================================================
def render_settings_page(settings: dict, configured: bool) -> None:
    if not configured:
        st.markdown(
            '<div style="background:#FFF1EC;border-radius:14px;padding:20px 24px;'
            'margin-bottom:10px;border-left:3px solid #F26749;">'
            '<div style="font-size:19px;font-weight:700;color:#1C1C1E;">ようこそ 👋</div>'
            '<div style="font-size:14px;color:#3C3C43;margin-top:6px;line-height:1.8;">'
            'はじめに施設の情報を登録しましょう。ここで入力した内容は保存され、'
            '<b>次回からは入力不要</b>です。<br>登録が終わると「書類作成」が使えるようになります。'
            '</div></div>',
            unsafe_allow_html=True,
        )

    st.markdown('<div style="font-size:28px;font-weight:700;color:#1C1C1E;margin:10px 0 2px;">'
                '⚙️ 設定</div>', unsafe_allow_html=True)
    st.markdown(
        '<div style="background:#FFF8E6;border-radius:10px;padding:10px 14px;margin:6px 0 14px;'
        'border-left:3px solid #FF9500;font-size:13px;color:#7A5200;">'
        '✏️ 各入力欄を<b>クリックすると文字を編集</b>できます。'
        '変更したら一番下の「💾 保存する」を押してください。</div>',
        unsafe_allow_html=True,
    )

    with st.form("settings_form"):
        st.markdown("#### 施設情報")
        c1, c2 = st.columns(2)
        company  = c1.text_input("法人名",       sv(settings, "company_name"))
        facility = c2.text_input("施設名",       sv(settings, "facility_name"))
        mtitle   = c1.text_input("管理者肩書き", sv(settings, "manager_title"))
        mname    = c2.text_input("管理者名",     sv(settings, "manager_name"))
        postal   = c1.text_input("郵便番号",     sv(settings, "postal"))
        address  = c2.text_input("住所",         sv(settings, "address"))

        st.markdown("#### 書類テンプレート")
        c3, c4 = st.columns(2)
        dairi_t  = c3.text_input("代理受領通知書タイトル", sv(settings, "dairi_doc_title"))
        seikyu_t = c4.text_input("請求書タイトル",         sv(settings, "seikyu_doc_title"))
        pay_due  = st.text_input("支払期日",               sv(settings, "pay_due"))
        footer   = st.text_input("請求書フッター注記",      sv(settings, "footer_note"))
        dpay     = st.text_input("デフォルト支払方法",      sv(settings, "default_pay"))

        st.markdown("#### 振込先")
        c5, c6 = st.columns(2)
        bname    = c5.text_input("銀行名",   sv(settings, "bank_name"))
        bbranch  = c6.text_input("支店",     sv(settings, "bank_branch"))
        baccount = c5.text_input("口座",     sv(settings, "bank_account"))
        bholder  = c6.text_input("口座名義", sv(settings, "bank_holder"))

        st.markdown("#### 国保連請求")
        jigyosho = st.text_input("事業所番号（10桁）", sv(settings, "jigyosho_number"),
                                 help="国保連CSVに使う事業所番号")
        api_key  = st.text_input("Anthropic APIキー", sv(settings, "anthropic_api_key"),
                                 type="password",
                                 help="実績記録票の自動読み取り（OCR）に使います。sk-ant- で始まるキー")

        st.markdown("#### セキュリティ")
        has_pw = bool(settings.get("password_hash"))
        pw_label = ("パスワードを変更（空欄なら変更なし）" if has_pw
                    else "パスワードを設定（空欄ならログイン不要）")
        new_pw = st.text_input(pw_label, type="password")
        if has_pw:
            st.caption("🔒 現在パスワードが設定されています")

        submitted = st.form_submit_button("💾 保存する", type="primary")

    if submitted:
        new = {
            "company_name": company, "facility_name": facility,
            "manager_title": mtitle, "manager_name": mname,
            "postal": postal, "address": address,
            "default_pay": dpay, "dairi_doc_title": dairi_t,
            "seikyu_doc_title": seikyu_t, "pay_due": pay_due,
            "footer_note": footer, "bank_name": bname,
            "bank_branch": bbranch, "bank_account": baccount, "bank_holder": bholder,
            "jigyosho_number": jigyosho, "anthropic_api_key": api_key,
        }
        if new_pw:
            new["password_hash"] = hash_pw(new_pw)
        elif settings.get("password_hash"):
            new["password_hash"] = settings["password_hash"]
        if settings.get("guardian_notes"):      # 備考の記憶は維持
            new["guardian_notes"] = settings["guardian_notes"]
        save_settings(new)
        st.session_state["authed"] = True   # 設定直後にロックアウトされないように
        st.success("保存しました")
        st.rerun()

    # 印鑑（フォーム外：アップロード即反映）
    st.markdown("#### 印鑑")
    if not STAMP_PATH.exists():
        up = st.file_uploader("印鑑画像（PNG・任意）", type=["png"], key="stamp_set",
                              help="透過PNG推奨。一度登録すれば次回から自動で押印されます")
        if up:
            STAMP_PATH.write_bytes(up.read())
            st.rerun()
    else:
        col1, col2 = st.columns([1, 4])
        with col1:
            st.image(io.BytesIO(STAMP_PATH.read_bytes()), width=84)
        with col2:
            st.markdown('<div style="color:#34C759;font-weight:600;padding-top:18px;">'
                        '✓ 印鑑 登録済み</div>', unsafe_allow_html=True)
            if st.button("印鑑を削除して登録し直す", key="del_stamp_set"):
                STAMP_PATH.unlink()
                st.rerun()


# ============================================================
# ヘルパー関数：PDF抽出
# ============================================================

def crop_image(pil_img: Image.Image, top: float, bottom: float,
               left: float, right: float) -> Image.Image:
    w, h = pil_img.size
    return pil_img.crop((int(w * left), int(h * top), int(w * right), int(h * bottom)))


def pil_to_buf(pil_img: Image.Image) -> io.BytesIO:
    buf = io.BytesIO()
    if pil_img.mode == "RGBA":
        pil_img = pil_img.convert("RGB")
    pil_img.save(buf, format="JPEG", quality=88)
    buf.seek(0)
    return buf


def extract_guardian_name(page) -> str:
    try:
        for table in page.extract_tables():
            for row in table:
                for i, cell in enumerate(row):
                    if cell is None:
                        continue
                    clean = cell.replace(" ", "").replace("　", "").replace("\n", "")
                    if "給付決定保護者" in clean and i + 1 < len(row):
                        name = (row[i + 1] or "").replace(" ", "").replace("　", "").strip()
                        if len(name) > 1:
                            return name
    except Exception:
        pass
    for line in (page.extract_text() or "").split("\n")[:15]:
        clean = line.replace(" ", "").replace("　", "").strip()
        if "様" in clean and "PARAZA" not in clean and "通知書" not in clean:
            return clean.replace("様", "").strip()
    return "利用者"


def extract_service_month(page) -> str:
    t = (page.extract_text() or "").replace(" ", "").replace("　", "")
    m = re.search(r"令和(\d+)年(\d+)月", t)
    if m:
        return f"令和{m.group(1)}年{m.group(2)}月"
    m = re.search(r"(\d{4})年(\d{1,2})月", t)
    if m:
        return f"{m.group(1)}年{m.group(2)}月"
    return ""


def to_seireki(wareki: str) -> str:
    """『令和8年4月』→『2026年4月』に変換。変換できなければそのまま返す"""
    m = re.match(r"令和(\d+)年(\d+)月", wareki or "")
    if m:
        return f"{2018 + int(m.group(1))}年{int(m.group(2))}月"
    return wareki or ""


def extract_seikyu_data(page) -> dict:
    data = {"jukyusha_id": "", "guardian_name": "利用者", "bill_date": "", "bill_amount": "0"}
    try:
        for table in page.extract_tables():
            for row in table:
                for i, cell in enumerate(row):
                    if cell is None:
                        continue
                    clean = cell.replace(" ", "").replace("　", "").replace("\n", "")
                    if "給付決定保護者" in clean and i + 1 < len(row):
                        name = (row[i + 1] or "").replace(" ", "").replace("　", "").strip()
                        if len(name) > 1:
                            data["guardian_name"] = name
    except Exception:
        pass
    t = (page.extract_text() or "").replace(" ", "").replace("　", "")
    m = re.search(r"受給者証番号\s*(\d{10})", t)
    if m:
        data["jukyusha_id"] = m.group(1)
    if data["guardian_name"] == "利用者":
        m = re.search(r"給付決定保護者[氏名]*[\n\r\s]+([^\n\r\s]{2,6})", t)
        if m:
            data["guardian_name"] = m.group(1).strip()
    m = re.search(r"令和(\d+)年(\d+)月分", t)
    if m:
        data["bill_date"] = f"令和{m.group(1)}年{m.group(2)}月"
    matches = re.findall(r"決定利用者負担額\s*(\d{1,3}(?:,\d{3})*)", t)
    if matches:
        raw = matches[-1]
        try:
            data["bill_amount"] = f"{int(raw.replace(',', '')):,}"
        except ValueError:
            data["bill_amount"] = raw
    return data


@st.cache_data(show_spinner=False)
def parse_seikyu_pdf(pdf_bytes: bytes):
    """明細書PDFを解析。保護者ごとの情報リストと、切り出し済み画像を返す（結果はキャッシュ）"""
    images = convert_from_bytes(pdf_bytes, dpi=200)
    rows = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for i, page in enumerate(pdf.pages):
            d = extract_seikyu_data(page)
            rows.append({
                "index":        i,
                "jukyusha_id":  d["jukyusha_id"],
                "guardian":     d["guardian_name"],
                "bill_date":    d["bill_date"],
                "bill_amount":  d["bill_amount"],
            })
    crops = [crop_image(im, 0.41, 0.91, 0.06, 0.94) for im in images]
    return rows, crops


# ============================================================
# PDF生成ヘルパー
# ============================================================

def _draw_wrapped_text(c, text: str, x: float, y: float,
                       max_w: float, font: str, size: float, leading: float) -> float:
    """テキストを折り返して描画し、次の描画Y座標を返す"""
    char_w = size  # CJK全角 ≈ font size pt
    chars_per_line = max(1, int(max_w / char_w))
    lines = []
    while text:
        lines.append(text[:chars_per_line])
        text = text[chars_per_line:]
    c.setFont(font, size)
    for line in lines:
        c.drawString(x, y, line)
        y -= leading
    return y


def _draw_image_fit(c, pil_img: Image.Image,
                    x: float, y_top: float, max_w: float, max_h: float):
    """アスペクト比を保ちながら指定領域に収めて画像を描画"""
    buf = pil_to_buf(pil_img)
    aspect = pil_img.width / pil_img.height
    if max_w / max_h > aspect:
        h = max_h
        w = h * aspect
    else:
        w = max_w
        h = w / aspect
    draw_x = x + (max_w - w) / 2
    draw_y = y_top - h
    c.drawImage(ImageReader(buf), draw_x, draw_y, width=w, height=h, preserveAspectRatio=True)


# ============================================================
# PDF生成：代理受領通知書（canvas API・A4 1枚）
# ============================================================

def make_dairi_pdf(guardian: str, rec_date: str, notif_date: str,
                   pil_img: Image.Image, facility: dict) -> bytes:
    buf = io.BytesIO()
    PAGE_W, PAGE_H = A4
    LM = 18 * mm
    RM = PAGE_W - 18 * mm
    USABLE_W = RM - LM
    BM = 15 * mm

    c = rl_canvas.Canvas(buf, pagesize=A4)
    y = PAGE_H - 20 * mm  # 上余白 20mm

    # ① 通知日（右寄せ）
    c.setFont(MIN, 10)
    c.drawRightString(RM, y, notif_date)
    y -= 8 * mm

    # ② 受取人名（左寄せ・大）
    c.setFont(MIN, 14)
    c.drawString(LM, y, f"{guardian}　様")
    y -= 12 * mm

    # ③ タイトル（中央）
    title = facility["dairi_doc_title"]
    c.setFont(MIN, 17)
    tw = c.stringWidth(title, MIN, 17)
    c.drawString((PAGE_W - tw) / 2, y, title)
    y -= 10 * mm

    # ④ 発行者（右寄せ）
    c.setFont(MIN, 10)
    for line in [facility["company_name"],
                 f"{facility['manager_title']}　{facility['manager_name']}"]:
        c.drawRightString(RM, y, line)
        y -= 5.5 * mm
    y -= 3 * mm

    # ⑤ 本文（折り返し）
    body = (
        "このたび下記の内容で提供しましたサービスに要した費用について、"
        "市町村から支払いを受けましたのでお知らせします。"
        "このお知らせの内容に疑義がある場合は、当法人もしくは受給者証に"
        "記載された市町村にお問い合わせください。"
    )
    y = _draw_wrapped_text(c, body, LM, y, USABLE_W, MIN, 10, 18)
    y -= 5 * mm

    # ⑥ 受領日
    c.setFont(MIN, 11)
    c.drawString(LM, y, f"受領日：{rec_date}")
    y -= 10 * mm

    # ⑦ 罫線
    c.setLineWidth(0.8)
    c.line(LM, y, RM, y)
    y -= 5 * mm

    # ⑧ 明細書画像（残スペースを全て使う）
    available_h = y - BM
    _draw_image_fit(c, pil_img, LM, y, USABLE_W, available_h)

    c.save()
    return buf.getvalue()


# ============================================================
# PDF生成：保護者請求書（canvas API・A4 1枚・御請求書レイアウト）
# ============================================================

def make_seikyu_pdf(guardian: str, issue_date: str, bill_date: str,
                    bill_amount: str, biko: str,
                    pil_img: Image.Image, facility: dict,
                    stamp_img: Image.Image = None) -> bytes:
    buf = io.BytesIO()
    PAGE_W, PAGE_H = A4
    LM = 18 * mm
    RM = PAGE_W - 18 * mm
    USABLE_W = RM - LM
    BM = 12 * mm
    GRAY_BAR = colors.Color(0.92, 0.92, 0.92)
    GRAY_LBL = colors.Color(0.90, 0.90, 0.90)

    c = rl_canvas.Canvas(buf, pagesize=A4)
    y = PAGE_H - 15 * mm

    # ① タイトル（中央・大）
    title = facility["seikyu_doc_title"]
    c.setFont(MIN, 22)
    tw = c.stringWidth(title, MIN, 22)
    c.drawString((PAGE_W - tw) / 2, y, title)

    # ② 発行日（右寄せ）
    c.setFont(MIN, 10)
    c.drawRightString(RM, y - 10 * mm, issue_date)
    y -= 24 * mm

    # ③ 受取人名（左寄せ・下線）
    nm, sama = guardian, "様"
    gap = 4 * mm
    name_x = LM + 8 * mm
    c.setFont(MIN, 16)
    c.drawString(name_x, y, nm)
    nw = c.stringWidth(nm, MIN, 16)
    c.setFont(MIN, 11)
    c.drawString(name_x + nw + gap, y, sama)
    c.setLineWidth(0.8)
    c.line(LM, y - 2 * mm, name_x + nw + 4 * mm, y - 2 * mm)
    y -= 16 * mm

    # ④ 発行者ブロック（右側）＋ 印鑑
    issuer_x = PAGE_W * 0.60
    issuer_lines = [
        facility["company_name"],
        f'（{facility["facility_name"]}）',
        facility["manager_name"],
        f'〒{facility["postal"]}',
        facility["address"],
    ]
    c.setFont(MIN, 10)
    iy = y + 4 * mm
    for line in issuer_lines:
        c.drawString(issuer_x, iy, line)
        iy -= 5.2 * mm
    if stamp_img is not None:
        sbuf = io.BytesIO()
        stamp_img.save(sbuf, format="PNG")
        sbuf.seek(0)
        ss = 20 * mm
        c.drawImage(ImageReader(sbuf), RM - ss, y - 14 * mm,
                    width=ss, height=ss, mask="auto", preserveAspectRatio=True)

    # ④' 請求文（左側）
    c.setFont(MIN, 10.5)
    c.drawString(LM, y, "下記の通り利用者負担額をご請求申し上げます")
    y -= 9 * mm

    # ⑤ 合計（税込）ボックス（左側）
    box_w, box_h, lbl_w = 100 * mm, 14 * mm, 45 * mm
    c.setLineWidth(0.8)
    c.setFillColor(GRAY_LBL)
    c.rect(LM, y - box_h, lbl_w, box_h, fill=1, stroke=0)
    c.setFillColor(colors.black)
    c.rect(LM, y - box_h, box_w, box_h, fill=0, stroke=1)
    c.line(LM + lbl_w, y - box_h, LM + lbl_w, y)
    c.setFont(MIN, 12)
    c.drawCentredString(LM + lbl_w / 2, y - box_h + 4.5 * mm, "合計（税込）")
    c.setFont(MIN, 16)
    c.drawRightString(LM + box_w - 5 * mm, y - box_h + 4 * mm, f"¥{bill_amount}")
    y -= box_h + 6 * mm

    # ⑥ 支払期日
    c.setFont(MIN, 10)
    c.drawString(LM, y, f"支払期日：{facility['pay_due']}")
    y -= 9 * mm

    # ⑦ 明細・集計バー
    bar_h = 7 * mm
    c.setFillColor(GRAY_BAR)
    c.rect(LM, y - bar_h, USABLE_W, bar_h, fill=1, stroke=1)
    c.setFillColor(colors.black)
    c.setFont(MIN, 10)
    c.drawString(LM + 3 * mm, y - bar_h + 2 * mm, "明細・集計")
    c.drawRightString(RM - 3 * mm, y - bar_h + 2 * mm, f"{to_seireki(bill_date)}ご利用分")
    y -= bar_h + 3 * mm

    # 下部（振込先テーブル＋フッター）の予約領域
    footer_h = 6 * mm
    th_h, tb_h = 7 * mm, 26 * mm        # テーブル ヘッダー / ボディ
    table_h = th_h + tb_h
    table_top = BM + footer_h + 3 * mm + table_h

    # ⑧ 明細書画像（残り領域いっぱい）
    img_bottom = table_top + 4 * mm
    _draw_image_fit(c, pil_img, LM, y, USABLE_W, y - img_bottom)

    # ⑨ 振込先 / 備考 テーブル（下部固定）
    col_w = USABLE_W / 2
    c.setLineWidth(0.5)
    # ヘッダー
    c.setFillColor(GRAY_BAR)
    c.rect(LM, table_top - th_h, col_w, th_h, fill=1, stroke=1)
    c.rect(LM + col_w, table_top - th_h, col_w, th_h, fill=1, stroke=1)
    c.setFillColor(colors.black)
    c.setFont(MIN, 10)
    c.drawString(LM + 3 * mm, table_top - th_h + 2 * mm, "振込先")
    c.drawString(LM + col_w + 3 * mm, table_top - th_h + 2 * mm, "備考")
    # ボディ
    body_top = table_top - th_h
    c.rect(LM, body_top - tb_h, col_w, tb_h, fill=0, stroke=1)
    c.rect(LM + col_w, body_top - tb_h, col_w, tb_h, fill=0, stroke=1)
    c.setFont(MIN, 9.5)
    by = body_top - 5 * mm
    for line in [facility["bank_name"], facility["bank_branch"],
                 facility["bank_account"], facility["bank_holder"]]:
        c.drawString(LM + 3 * mm, by, line)
        by -= 5 * mm
    # 備考（自由記入・長い場合は折り返し）
    biko_x = LM + col_w + 3 * mm
    biko_w = col_w - 6 * mm
    chars = max(1, int(biko_w / (9.5)))
    by2 = body_top - 5 * mm
    rest = biko or ""
    while rest:
        c.drawString(biko_x, by2, rest[:chars])
        rest = rest[chars:]
        by2 -= 5 * mm

    # ⑩ フッター注記
    c.setFont(MIN, 9)
    c.drawString(LM, BM, facility["footer_note"])

    c.save()
    return buf.getvalue()


# ============================================================
# UI共通
# ============================================================

def result_card(name, detail=""):
    st.markdown(
        f'<div style="background:#F0FFF4;border-radius:12px;padding:12px 16px;margin:4px 0;'
        f'border-left:3px solid #34C759;">'
        f'<span style="font-size:14px;color:#166534;font-weight:500;">✓ {name}様　{detail}　作成完了</span>'
        f"</div>",
        unsafe_allow_html=True,
    )


# ============================================================
# 設定読込・認証・ナビゲーション
# ============================================================
settings = load_settings()
configured = bool(settings)

# --- パスワード認証 ---
if configured and settings.get("password_hash") and not st.session_state.get("authed"):
    render_login(settings)
    st.stop()

# --- サイドバー ---
with st.sidebar:
    st.markdown(
        '<div style="display:flex;align-items:center;gap:10px;padding:18px 0 18px 0;'
        'border-bottom:1px solid #EFE6E0;margin-bottom:18px;">'
        + brand_mark(34, 9, 17) +
        '<div><div style="font-size:17px;font-weight:700;color:#2A2420;line-height:1.2;">'
        f'{APP_NAME}</div>'
        '<div style="font-size:11px;color:#9B8E86;margin-top:2px;">障害福祉の書類作成ツール</div>'
        '</div></div>',
        unsafe_allow_html=True,
    )

    if configured:
        nav = st.radio(
            "メニュー",
            ["📩 代理受領通知書", "🧾 保護者請求書", "📊 国保連請求", "⚙️ 設定"],
            label_visibility="collapsed",
        )
    else:
        nav = "⚙️ 設定"

    st.divider()
    if configured:
        st.markdown(f'<div style="font-size:12px;color:#6E6E73;">施設：'
                    f'<b>{settings.get("facility_name", "")}</b></div>', unsafe_allow_html=True)
    stamp_state = "✓ 登録済み" if STAMP_PATH.exists() else "未登録"
    stamp_color = "#34C759" if STAMP_PATH.exists() else "#FF9500"
    st.markdown(f'<div style="font-size:12px;color:#8E8E93;margin-top:4px;">印鑑：'
                f'<span style="color:{stamp_color};font-weight:600;">{stamp_state}</span></div>',
                unsafe_allow_html=True)

    if settings.get("password_hash") and st.session_state.get("authed"):
        st.divider()
        if st.button("ログアウト"):
            st.session_state["authed"] = False
            st.rerun()

    st.markdown('<div style="font-size:11px;color:#C7C7CC;margin-top:18px;">'
                'パラザ合同会社　v1.1</div>', unsafe_allow_html=True)

# --- 設定 / オンボーディング ページ ---
if (not configured) or nav == "⚙️ 設定":
    render_settings_page(settings, configured)
    st.stop()

# --- 国保連請求ページ（らくらく請求） ---
if nav == "📊 国保連請求":
    import kokuhoren
    kokuhoren.render(settings)
    st.stop()

# --- 書類作成ページで使う設定値 ---
fi = {k: settings.get(k, DEFAULTS.get(k, "")) for k in DEFAULTS}
stamp_img = load_stamp_img()


# ============================================================
# ページヘッダー
# ============================================================
st.markdown(
    '<div style="display:flex;align-items:center;gap:16px;margin-bottom:6px;">'
    + brand_mark(52, 14, 26) +
    '<div>'
    f'<div style="font-size:32px;font-weight:700;color:#2A2420;letter-spacing:-0.5px;">{APP_NAME}</div>'
    '<div style="font-size:15px;color:#9B8E86;margin-top:4px;">'
    '国保連から取得した明細書PDFをアップロードするだけで書類を一括生成します'
    '</div></div></div>',
    unsafe_allow_html=True,
)
st.divider()


# ============================================================
# 代理受領通知書
# ============================================================
if nav == "📩 代理受領通知書":
    st.markdown("""<div style="margin:28px 0 16px;">
        <div style="font-size:20px;font-weight:700;color:#1C1C1E;">代理受領通知書を一括作成</div>
        <div style="font-size:13px;color:#8E8E93;margin-top:4px;">
            国保連から取得した明細書PDF（全保護者分1ファイル）をアップロードしてください
        </div>
    </div>""", unsafe_allow_html=True)

    col_pdf, col_date = st.columns([3, 1])
    with col_pdf:
        up_dairi = st.file_uploader("明細書PDF", type=["pdf"], key="dairi_pdf")
    with col_date:
        rec_date = st.date_input(
            "受領日",
            value=datetime.date.today().replace(day=20),
            key="rec_date",
        )

    if up_dairi:
        pdf_bytes = up_dairi.read()
        if st.button("代理受領通知書を作成する", type="primary", key="btn_dairi"):
            progress = st.progress(0, text="処理中...")
            results = []

            try:
                images = convert_from_bytes(pdf_bytes, dpi=200)
            except Exception as e:
                st.error(f"PDF読み込みエラー: {e}")
                st.stop()

            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                total = len(pdf.pages)
                for i, page in enumerate(pdf.pages):
                    progress.progress((i + 1) / total, text=f"処理中... {i+1}/{total}件")
                    guardian  = extract_guardian_name(page)
                    cropped   = crop_image(images[i], 0.09, 0.91, 0.05, 0.95)
                    rec_str   = rec_date.strftime("%Y年%m月%d日")
                    notif_str = (rec_date + datetime.timedelta(days=1)).strftime("%Y年%m月%d日")

                    try:
                        pdf_out  = make_dairi_pdf(guardian, rec_str, notif_str, cropped, fi)
                        ym       = rec_date.strftime("%Y%m")
                        filename = f"{ym}_{guardian}様_代理受領通知書.pdf"
                        results.append({"filename": filename, "data": pdf_out, "name": guardian, "ok": True})
                    except Exception as e:
                        results.append({"name": guardian, "ok": False, "error": str(e)})

            progress.empty()
            ok = [r for r in results if r["ok"]]
            ng = [r for r in results if not r["ok"]]

            for r in ok:
                result_card(r["name"])
            for r in ng:
                st.error(f"✗ {r['name']}様　エラー: {r.get('error', '')}")

            if ok:
                st.success(f"{len(ok)}件の代理受領通知書を作成しました")
                zip_buf = io.BytesIO()
                with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for r in ok:
                        zf.writestr(r["filename"], r["data"])
                zip_buf.seek(0)
                ym = rec_date.strftime("%Y%m")
                st.download_button(
                    "📥 ZIPでまとめてダウンロード",
                    data=zip_buf.getvalue(),
                    file_name=f"{ym}_代理受領通知書_一括.zip",
                    mime="application/zip",
                    type="primary",
                )


# ============================================================
# 保護者請求書
# ============================================================
if nav == "🧾 保護者請求書":
    st.markdown("""<div style="margin:28px 0 16px;">
        <div style="font-size:20px;font-weight:700;color:#1C1C1E;">保護者請求書を一括作成</div>
        <div style="font-size:13px;color:#8E8E93;margin-top:4px;">
            明細書PDF（全保護者分1ファイル）をアップロードしてください。保護者名は自動で読み取ります。
        </div>
    </div>""", unsafe_allow_html=True)

    col_pdf2, col_date2 = st.columns([3, 1])
    with col_pdf2:
        up_seikyu = st.file_uploader("明細書PDF", type=["pdf"], key="seikyu_pdf")
    with col_date2:
        issue_date = st.date_input("発行日", value=datetime.date.today(), key="issue_date")

    if up_seikyu:
        pdf_bytes = up_seikyu.read()
        try:
            rows, crops = parse_seikyu_pdf(pdf_bytes)
        except Exception as e:
            st.error(f"PDF読み込みエラー: {e}")
            st.stop()

        saved_notes = settings.get("guardian_notes", {})

        st.markdown('<div style="font-size:15px;font-weight:600;color:#1C1C1E;margin:18px 0 4px;">'
                    '各保護者の備考</div>', unsafe_allow_html=True)
        st.caption("支払方法などを記入します。一度入力すると受給者証番号で記憶され、翌月から自動で入ります。")

        # index→備考。ウィジェットのキーはページ番号で一意化（同じ受給者証番号が複数ページに出ても衝突しない）
        edited_by_index = {}
        for r in rows:
            mem_key = r["jukyusha_id"] or f"idx{r['index']}"
            default_note = saved_notes.get(mem_key, f"お支払方法：{fi['default_pay']}")
            label = (f"{r['guardian']}様　"
                     f"（{to_seireki(r['bill_date'])}分 ／ ¥{r['bill_amount']}）")
            edited_by_index[r["index"]] = st.text_input(
                label, value=default_note, key=f"note_p{r['index']}")

        if st.button("請求書を作成する", type="primary", key="btn_seikyu"):
            progress = st.progress(0, text="処理中...")
            results = []
            issue_str = issue_date.strftime("%Y年%m月%d日")
            total = len(rows)

            for r in rows:
                progress.progress((r["index"] + 1) / total,
                                  text=f"処理中... {r['index']+1}/{total}件")
                biko     = edited_by_index[r["index"]]
                guardian = r["guardian"]
                try:
                    pdf_out = make_seikyu_pdf(
                        guardian, issue_str, r["bill_date"],
                        r["bill_amount"], biko, crops[r["index"]], fi, stamp_img,
                    )
                    ym       = issue_date.strftime("%Y%m")
                    filename = f"{ym}_{guardian}様_請求書.pdf"
                    results.append({
                        "filename": filename, "data": pdf_out,
                        "name": guardian, "bill_amount": r["bill_amount"], "ok": True,
                    })
                except Exception as e:
                    results.append({"name": guardian, "ok": False, "error": str(e)})

            progress.empty()

            # 備考を記憶（受給者証番号ごと。同じ番号が複数ページなら最後の値を採用）
            new_notes = dict(settings.get("guardian_notes", {}))
            for r in rows:
                mem_key = r["jukyusha_id"] or f"idx{r['index']}"
                new_notes[mem_key] = edited_by_index[r["index"]]
            updated = dict(settings)
            updated["guardian_notes"] = new_notes
            save_settings(updated)

            ok = [r for r in results if r["ok"]]
            ng = [r for r in results if not r["ok"]]
            for r in ok:
                result_card(r["name"], f"¥{r['bill_amount']}")
            for r in ng:
                st.error(f"✗ {r['name']}様　エラー: {r.get('error', '')}")

            if ok:
                total_amt = sum(
                    int(r["bill_amount"].replace(",", ""))
                    for r in ok if r["bill_amount"] not in ("0", "")
                )
                st.success(f"{len(ok)}件　合計請求額: ¥{total_amt:,}")
                zip_buf = io.BytesIO()
                with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for r in ok:
                        zf.writestr(r["filename"], r["data"])
                zip_buf.seek(0)
                ym = issue_date.strftime("%Y%m")
                st.download_button(
                    "📥 ZIPでまとめてダウンロード",
                    data=zip_buf.getvalue(),
                    file_name=f"{ym}_保護者請求書_一括.zip",
                    mime="application/zip",
                    type="primary",
                )
