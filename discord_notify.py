"""Discord Webhook 通知ユーティリティ"""

import urllib.request
import json
import os
from datetime import datetime
from pathlib import Path

WEBHOOK_CONFIG = Path(__file__).parent / "data" / ".discord_webhook"


def load_webhook_url():
    url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if url:
        return url
    if WEBHOOK_CONFIG.exists():
        return WEBHOOK_CONFIG.read_text().strip()
    return ""


def save_webhook_url(url: str):
    WEBHOOK_CONFIG.parent.mkdir(exist_ok=True)
    WEBHOOK_CONFIG.write_text(url.strip())


def send_check_result(ym: str, results: list, webhook_url: str = "") -> tuple[bool, str]:
    """
    請求前チェック結果をDiscordに送信する。
    results: [{"name": str, "status": "ok"|"warn"|"error", "msg": str}, ...]
    """
    url = webhook_url or load_webhook_url()
    if not url:
        return False, "Webhook URLが未設定です"

    errors = [r for r in results if r["status"] == "error"]
    warns  = [r for r in results if r["status"] == "warn"]
    oks    = [r for r in results if r["status"] == "ok"]

    if errors:
        color = 0xFF3B30   # 赤
        title = f"【サードストリート】請求前チェック ❌ 要修正あり"
    elif warns:
        color = 0xFF9500   # オレンジ
        title = f"【サードストリート】請求前チェック ⚠️ 確認事項あり"
    else:
        color = 0x34C759   # 緑
        title = f"【サードストリート】請求前チェック ✅ 問題なし"

    y, m = ym[:4], ym[4:]
    desc = f"**{y}年{m}月分** の請求前チェックが完了しました。\n"
    desc += f"✅ OK: {len(oks)}件　⚠️ 注意: {len(warns)}件　❌ エラー: {len(errors)}件"

    fields = []
    status_icon = {"ok": "✅", "warn": "⚠️", "error": "❌"}
    for r in results:
        fields.append({
            "name": f"{status_icon[r['status']]} {r['name']}",
            "value": r["msg"],
            "inline": False
        })

    if errors or warns:
        fields.append({
            "name": "次のアクション",
            "value": "らくらく請求 → ③ 請求前チェック タブで詳細を確認してください",
            "inline": False
        })

    embed = {
        "title": title,
        "description": desc,
        "color": color,
        "fields": fields,
        "footer": {
            "text": f"サードストリート 請求前チェックBot • {datetime.now().strftime('%Y/%m/%d %H:%M')}"
        }
    }

    payload = json.dumps({"embeds": [embed]}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 204, "送信しました"
    except Exception as e:
        return False, str(e)
