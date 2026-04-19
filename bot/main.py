"""
LionBrain Bot — Zeabur 雲端版（GitHub API）
=================================================
所有資料讀寫透過 GitHub API，不依賴本地檔案系統。
可部署到 Zeabur / Railway / Render 等任何雲端平台 24/7 運行。

功能：
  • 丟語音 → Gemini 轉錄 → 存入 GitHub ideas/
  • 丟連結 → Gemini 摘要 → 存入 GitHub ideas/
  • 丟文字 / 自然語言指令 → 判斷意圖並執行
  • 每天 08:00 推送早報
  • 每週五 08:00 推送週報

指令：
  /status  — 今日代辦 + 案子狀態
  /test    — 立即觸發早報測試
  /myid    — 取得 Chat ID
"""

import base64
import datetime
import html
import json
import logging
import os
import re
import tempfile
import time

import pytz
import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ─── 設定 ─────────────────────────────────────────────────────────────────────
load_dotenv()

TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID_STR       = os.getenv("TELEGRAM_CHAT_ID", "")
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL      = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GITHUB_TOKEN      = os.getenv("GITHUB_TOKEN", "")
GITHUB_OWNER      = os.getenv("GITHUB_OWNER", "pcsfanfan01-AI")
GITHUB_REPO       = os.getenv("GITHUB_REPO", "LionBrain")
MORNING_HOUR      = int(os.getenv("MORNING_HOUR", "8"))
MORNING_MINUTE    = int(os.getenv("MORNING_MINUTE", "0"))
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL      = "claude-sonnet-4-6"

TZ = pytz.timezone("Asia/Taipei")
CHAT_ID_INT = int(CHAT_ID_STR) if CHAT_ID_STR.lstrip("-").isdigit() else None

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# GitHub API 封裝
# ══════════════════════════════════════════════════════════════════════════════

GH_API = "https://api.github.com"

def gh_headers() -> dict:
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }

def gh_list_dir(path: str) -> list:
    """列出資料夾內的檔案"""
    url = f"{GH_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
    resp = requests.get(url, headers=gh_headers(), timeout=30)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    result = resp.json()
    return result if isinstance(result, list) else []

def gh_read_file(path: str) -> tuple:
    """讀取檔案，回傳 (content, sha)"""
    url = f"{GH_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
    resp = requests.get(url, headers=gh_headers(), timeout=30)
    if resp.status_code == 404:
        return "", ""
    resp.raise_for_status()
    data = resp.json()
    content = base64.b64decode(data["content"].replace("\n", "")).decode("utf-8")
    return content, data["sha"]

def gh_write_file(path: str, content: str, message: str) -> bool:
    """建立或更新 GitHub 上的檔案"""
    url = f"{GH_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
    existing = requests.get(url, headers=gh_headers(), timeout=30)
    payload = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode(),
        "branch": "main",
    }
    if existing.status_code == 200:
        payload["sha"] = existing.json()["sha"]
    resp = requests.put(url, headers=gh_headers(), json=payload, timeout=30)
    resp.raise_for_status()
    return True

_BOT_NAME_CACHE: str = ""
_OWNER_NAME_CACHE: str = ""

def get_bot_name() -> str:
    global _BOT_NAME_CACHE
    if _BOT_NAME_CACHE:
        return _BOT_NAME_CACHE
    content, _ = gh_read_file("identity/background.md")
    if content:
        m = re.search(r"## Bot 名稱\n(.+)", content)
        if m:
            _BOT_NAME_CACHE = m.group(1).strip()
            return _BOT_NAME_CACHE
    _BOT_NAME_CACHE = "AI 管家"
    return _BOT_NAME_CACHE

def get_owner_name() -> str:
    global _OWNER_NAME_CACHE
    if _OWNER_NAME_CACHE:
        return _OWNER_NAME_CACHE
    content, _ = gh_read_file("identity/background.md")
    if content:
        m = re.search(r"## 稱呼\n(.+)", content)
        if m:
            _OWNER_NAME_CACHE = m.group(1).strip()
            return _OWNER_NAME_CACHE
    _OWNER_NAME_CACHE = "業主"
    return _OWNER_NAME_CACHE

def gh_delete_file(path: str, sha: str) -> bool:
    """刪除 GitHub 上的檔案"""
    url = f"{GH_API}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{path}"
    payload = {"message": f"processed: {path}", "sha": sha, "branch": "main"}
    resp = requests.delete(url, headers=gh_headers(), json=payload, timeout=30)
    return resp.status_code in (200, 204)


# ══════════════════════════════════════════════════════════════════════════════
# Gemini REST API 封裝
# ══════════════════════════════════════════════════════════════════════════════

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

def gemini_call(url: str, payload: dict, max_retries: int = 4) -> dict:
    """Gemini API 呼叫，自動重試 429"""
    for attempt in range(max_retries):
        resp = requests.post(url, json=payload, timeout=120)
        if resp.status_code == 429:
            wait = 2 ** attempt * 10
            logger.warning(f"Gemini 限流，{wait}s 後重試...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()

def gemini_text(prompt: str) -> str:
    url = f"{GEMINI_BASE}/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    data = gemini_call(url, {"contents": [{"parts": [{"text": prompt}]}]})
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()

def gemini_text_with_search(prompt: str) -> str:
    url = f"{GEMINI_BASE}/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    data = gemini_call(url, {
        "tools": [{"google_search": {}}],
        "contents": [{"parts": [{"text": prompt}]}],
    })
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()

def claude_analyze(prompt: str) -> str:
    """使用 Claude API 進行深度分析"""
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 1500,
        "messages": [{"role": "user", "content": prompt}],
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=120)
    resp.raise_for_status()
    return resp.json()["content"][0]["text"].strip()

def gemini_transcribe(audio_path: str) -> str:
    """上傳語音到 Gemini Files API 轉錄"""
    with open(audio_path, "rb") as f:
        audio_bytes = f.read()
    upload_resp = requests.post(
        f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={GEMINI_API_KEY}",
        headers={"X-Goog-Upload-Protocol": "raw", "Content-Type": "audio/ogg"},
        data=audio_bytes,
        timeout=120,
    )
    upload_resp.raise_for_status()
    file_uri  = upload_resp.json()["file"]["uri"]
    file_name = upload_resp.json()["file"]["name"]

    url = f"{GEMINI_BASE}/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    data = gemini_call(url, {"contents": [{"parts": [
        {"text": "請將這段語音完整轉錄為繁體中文文字。直接輸出內容："},
        {"file_data": {"mime_type": "audio/ogg", "file_uri": file_uri}},
    ]}]})
    text = data["candidates"][0]["content"]["parts"][0]["text"].strip()

    try:
        requests.delete(f"{GEMINI_BASE}/{file_name}?key={GEMINI_API_KEY}", timeout=10)
    except Exception:
        pass
    return text

def gemini_analyze_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    """上傳圖片到 Gemini Files API，回傳內容描述與洞見"""
    upload_resp = requests.post(
        f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={GEMINI_API_KEY}",
        headers={"X-Goog-Upload-Protocol": "raw", "Content-Type": mime_type},
        data=image_bytes,
        timeout=120,
    )
    upload_resp.raise_for_status()
    file_uri  = upload_resp.json()["file"]["uri"]
    file_name = upload_resp.json()["file"]["name"]

    url = f"{GEMINI_BASE}/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    data = gemini_call(url, {"contents": [{"parts": [
        {"text": (
            "請分析這張圖片，用繁體中文輸出：\n"
            "1. 圖片內容描述（1-2句）\n"
            "2. 關鍵資訊或文字（若有）\n"
            "3. 對傳統產業老闆在商業/AI應用上的潛在價值（1句，若無關聯填「無」）\n"
            "直接輸出，不要標題。"
        )},
        {"file_data": {"mime_type": mime_type, "file_uri": file_uri}},
    ]}]})
    text = data["candidates"][0]["content"]["parts"][0]["text"].strip()

    try:
        requests.delete(f"{GEMINI_BASE}/{file_name}?key={GEMINI_API_KEY}", timeout=10)
    except Exception:
        pass
    return text


# ══════════════════════════════════════════════════════════════════════════════
# 工具函式
# ══════════════════════════════════════════════════════════════════════════════

def now_taipei() -> datetime.datetime:
    return datetime.datetime.now(tz=TZ)

def today_str() -> str:
    return now_taipei().strftime("%Y-%m-%d")

def now_str() -> str:
    return now_taipei().strftime("%Y-%m-%d-%H%M%S")

def classify_content(content: str) -> dict:
    """用 Gemini 判斷內容屬於哪個專案"""
    files = gh_list_dir("projects")
    project_names = [f["name"].replace(".md", "") for f in files if f["name"].endswith(".md")]
    project_list  = "、".join(project_names) if project_names else "無"
    prompt = f"""
分析以下內容，只輸出 JSON：
{{"project": "最相關的專案（從清單選一個，或填'一般'）", "tags": ["標籤1"], "summary": "一句話摘要15字內"}}
可選專案：{project_list}、一般
內容：{content[:400]}
"""
    try:
        result = re.sub(r"```json|```", "", gemini_text(prompt)).strip()
        return json.loads(result)
    except Exception:
        return {"project": "一般", "tags": [], "summary": ""}

def save_idea(content: str, tag: str = "text") -> tuple:
    """存入 GitHub inbox/，等待每日整理"""
    meta    = classify_content(content)
    project = meta.get("project", "一般")
    tags    = meta.get("tags", [])
    summary = meta.get("summary", "")

    front_matter = (
        f"---\ndate: {today_str()}\ntype: {tag}\n"
        f"project: {project}\ntags: [{', '.join(tags)}]\nsummary: {summary}\n---\n\n"
    )
    # 把 summary 清理成安全檔名（去掉特殊符號、限20字）
    safe_title = re.sub(r'[\\/:*?"<>|，。！？、\s]+', '-', summary[:20]).strip('-') if summary else tag
    path = f"inbox/{today_str()}-{safe_title}.md"
    gh_write_file(path, front_matter + content, f"inbox: {summary or tag}")
    logger.info(f"📥 已存入 inbox → {path}（專案：{project}）")
    return path, meta

def read_yesterday_ideas() -> str:
    """讀昨日已整理進 knowledge/ 的內容"""
    yesterday = (now_taipei().date() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    items = []
    for folder in ["knowledge/areas", "knowledge/resources"]:
        files = gh_list_dir(folder)
        for f in sorted(files, key=lambda x: x["name"]):
            if f["name"].startswith(yesterday):
                content, _ = gh_read_file(f"{folder}/{f['name']}")
                m = re.search(r"summary:\s*(.+)", content)
                label = m.group(1).strip() if m else f["name"]
                items.append(f"• {html.escape(label[:100])}")
    return "\n".join(items) if items else "<i>昨日無新整理</i>"


# ══════════════════════════════════════════════════════════════════════════════
# 每日整理任務：inbox → knowledge + zettel
# ══════════════════════════════════════════════════════════════════════════════

def classify_for_knowledge(content: str) -> dict:
    """Gemini 判斷分類 + 是否建立 zettel"""
    prompt = f"""
分析以下內容，只輸出 JSON：
{{
  "folder": "areas 或 resources",
  "tags": ["標籤1", "標籤2"],
  "summary": "一句話摘要15字內",
  "create_zettel": true或false,
  "zettel_title": "卡片標題",
  "zettel_insight": "提煉出的核心洞見，1-2句，用第一人稱觀點"
}}

folder 定義：
- areas = 長期關注的領域知識（AI應用、傳產轉型、商業策略、行銷社群）
- resources = 具體工具、情報、來源參考

create_zettel = 只有當內容包含可複用的洞見或觀點時才 true（純資訊填 false）

內容：{content[:600]}
"""
    try:
        result = re.sub(r"```json|```", "", gemini_text(prompt)).strip()
        return json.loads(result)
    except Exception:
        return {"folder": "resources", "tags": [], "summary": "", "create_zettel": False, "zettel_title": "", "zettel_insight": ""}


def load_zettel_index() -> list:
    """載入現有 zettel 卡片索引（標題 + 標籤）"""
    files = gh_list_dir("zettel")
    index = []
    for f in files:
        if not f["name"].endswith(".md"):
            continue
        content, _ = gh_read_file(f"zettel/{f['name']}")
        title_m = re.search(r"title:\s*(.+)", content)
        tags_m  = re.search(r"tags:\s*\[(.+)\]", content)
        if title_m:
            index.append({
                "file": f["name"],
                "title": title_m.group(1).strip(),
                "tags":  tags_m.group(1).strip() if tags_m else "",
            })
    return index


def find_related_zettel(content: str, zettel_index: list) -> list:
    """找出語意相關的現有 zettel 卡片（最多3個）"""
    if not zettel_index:
        return []
    index_text = "\n".join([f"- {z['file']}: {z['title']} [{z['tags']}]" for z in zettel_index])
    prompt = f"""
現有 zettel 卡片：
{index_text}

新內容：{content[:300]}

找出最相關的卡片（最多3個），只輸出 JSON array of filenames：
["卡片檔名.md", ...]
沒有相關的輸出 []
"""
    try:
        result = re.sub(r"```json|```", "", gemini_text(prompt)).strip()
        return json.loads(result)
    except Exception:
        return []


def create_zettel_card(title: str, insight: str, tags: list, related_files: list) -> str:
    """建立新的 zettel 永久卡片"""
    zettel_id = f"Z-{now_taipei().strftime('%Y%m%d-%H%M%S')}"
    links_str = ", ".join([f"[[{f.replace('.md', '')}]]" for f in related_files])
    content = (
        f"---\nid: {zettel_id}\ntitle: {title}\ndate: {today_str()}\n"
        f"tags: [{', '.join(tags)}]\nlinks: [{links_str}]\n---\n\n{insight}\n"
    )
    gh_write_file(f"zettel/{zettel_id}.md", content, f"zettel: {title}")

    # 在相關卡片加反向連結
    for rel_file in related_files:
        rel_content, _ = gh_read_file(f"zettel/{rel_file}")
        if rel_content and f"[[{zettel_id}]]" not in rel_content:
            rel_content += f"\n<!-- backlink: [[{zettel_id}]] {title} -->"
            gh_write_file(f"zettel/{rel_file}", rel_content, f"zettel: backlink from {zettel_id}")

    return zettel_id


def daily_process_inbox() -> dict:
    """
    每日整理任務：
    1. 讀 inbox/ 所有檔案
    2. Gemini 分類 → 移到 knowledge/areas 或 knowledge/resources
    3. 偵測 zettel 連結 → 建立 / 更新永久卡片
    4. 清空 inbox
    回傳統計 dict
    """
    files = gh_list_dir("inbox")
    if not files:
        return {"processed": 0, "zettel_created": 0, "areas": 0, "resources": 0, "new_zettel": []}

    zettel_index = load_zettel_index()
    stats = {"processed": 0, "zettel_created": 0, "areas": 0, "resources": 0, "new_zettel": []}

    for f in sorted(files, key=lambda x: x["name"]):
        if not f["name"].endswith(".md"):
            continue
        content, sha = gh_read_file(f"inbox/{f['name']}")
        if not content:
            continue

        try:
            meta = classify_for_knowledge(content)
            folder = meta.get("folder", "resources")
            dest   = f"knowledge/{folder}/{f['name']}"

            # 加分類標記後寫到 knowledge/
            classified_content = re.sub(
                r"^---", f"---\nclassified_folder: {folder}", content, count=1
            )
            gh_write_file(dest, classified_content, f"classify: {meta.get('summary', f['name'])}")

            if folder == "areas":
                stats["areas"] += 1
            else:
                stats["resources"] += 1

            # 建立 zettel 卡片
            if meta.get("create_zettel") and meta.get("zettel_title"):
                related = find_related_zettel(content, zettel_index)
                zettel_id = create_zettel_card(
                    title        = meta["zettel_title"],
                    insight      = meta.get("zettel_insight", ""),
                    tags         = meta.get("tags", []),
                    related_files= related,
                )
                stats["zettel_created"] += 1
                stats["new_zettel"].append({"id": zettel_id, "title": meta["zettel_title"], "links": related})
                zettel_index.append({"file": f"{zettel_id}.md", "title": meta["zettel_title"], "tags": ", ".join(meta.get("tags", []))})

            # 從 inbox 刪除
            gh_delete_file(f"inbox/{f['name']}", sha)
            stats["processed"] += 1

        except Exception as e:
            logger.error(f"整理 {f['name']} 失敗: {e}", exc_info=True)

    logger.info(f"✅ 每日整理完成：{stats}")
    return stats

def read_latest_todos() -> str:
    files = gh_list_dir("memory/daily")
    if not files:
        return "<i>尚無代辦記錄</i>"
    latest = sorted(files, key=lambda x: x["name"], reverse=True)[0]
    content, _ = gh_read_file(f"memory/daily/{latest['name']}")
    match = re.search(r"##\s*下次待辦\n(.*?)(?=\n##|\Z)", content, re.DOTALL)
    if match:
        lines = [l.strip() for l in match.group(1).strip().splitlines() if l.strip()]
        return html.escape("\n".join(lines)) if lines else "<i>尚無代辦記錄</i>"
    return "<i>尚無代辦記錄</i>"

def read_projects_status() -> str:
    files = gh_list_dir("projects")
    lines = []
    for f in sorted(files, key=lambda x: x["name"]):
        if not f["name"].endswith(".md"):
            continue
        content, _ = gh_read_file(f"projects/{f['name']}")
        m = re.search(r"##\s*狀態[:：]\s*(.+)", content)
        status = m.group(1).strip() if m else "狀態未記錄"
        name   = f["name"].replace(".md", "")
        lines.append(f"• {html.escape(name)}：{html.escape(status)}")
    return "\n".join(lines) if lines else "<i>無進行中案子</i>"

def read_focus_topics() -> list:
    """從 context/focus.md 讀取早報關注主題"""
    content, _ = gh_read_file("context/focus.md")
    if not content:
        return ["AI 工具與產業應用", "商業策略與市場動態", "社群行銷與內容策略", "Anthropic Claude 最新功能"]
    topics = []
    for line in content.splitlines():
        m = re.match(r"^\d+\.\s*(.+)", line.strip())
        if m:
            topics.append(m.group(1).strip())
    return topics if topics else ["AI 工具與產業應用", "商業策略與市場動態"]

def fetch_news() -> str:
    today  = today_str()
    topics = read_focus_topics()
    topics_str    = "\n".join([f"{i+1}. {t}" for i, t in enumerate(topics)])
    topics_labels = "\n".join([f"{t.split('（')[0].split('/')[0][:6]}：[1句]" for t in topics])
    prompt = f"""
今天是 {today}，請用 Google Search 搜尋以下主題最新動態，整理成繁體中文早報：
{topics_str}

每主題 1 句精華。最後加一句今日商業提示（30字內，針對使用者的行業背景給實用建議）。

格式：
📰 今日情報
{topics_labels}

💡 今日商業提示
[一句話]
"""
    try:
        return html.escape(gemini_text_with_search(prompt))
    except Exception as e:
        try:
            return html.escape(gemini_text(prompt))
        except Exception as e2:
            return f"📰 今日情報（生成失敗：{html.escape(str(e2))}）"


# ══════════════════════════════════════════════════════════════════════════════
# 推送報告
# ══════════════════════════════════════════════════════════════════════════════

async def send_morning_report(context: ContextTypes.DEFAULT_TYPE):
    if not CHAT_ID_INT:
        return
    logger.info("📨 每日整理 + 早報生成中...")
    today = today_str()

    # 先跑每日整理任務
    try:
        stats = daily_process_inbox()
        inbox_summary = (
            f"整理 <b>{stats['processed']}</b> 筆 → "
            f"areas:{stats['areas']} / resources:{stats['resources']}"
            + (f" | 新增 Zettel：{stats['zettel_created']} 張" if stats['zettel_created'] else "")
        ) if stats['processed'] > 0 else "<i>inbox 無新內容</i>"
    except Exception as e:
        logger.error(f"每日整理失敗: {e}", exc_info=True)
        inbox_summary = f"<i>整理任務失敗：{html.escape(str(e))}</i>"

    yesterday_ideas = read_yesterday_ideas()
    todos           = read_latest_todos()
    projects        = read_projects_status()
    news            = fetch_news()
    crm_followups   = crm_read_followups() if crm_enabled() else ""

    # 早報情報存進 knowledge/resources/
    try:
        news_plain = re.sub(r"<[^>]+>", "", news)  # 去掉 html escape 前的標籤痕跡
        news_md = (
            f"---\ndate: {today}\ntype: daily-news\n"
            f"tags: [AI動態, 傳產轉型, 社群行銷, Claude更新]\nsummary: {today} 每日情報\n---\n\n"
            f"{news_plain}"
        )
        gh_write_file(f"knowledge/areas/{today}-每日情報.md", news_md, f"news: {today} 早報情報入庫")
    except Exception as e:
        logger.warning(f"早報情報存檔失敗: {e}")

    report = (
        f"🦁 <b>{get_bot_name()} 早報 | {today}</b>\n\n"
        f"🗂️ <b>昨日整理</b>\n{inbox_summary}\n\n"
        f"📌 <b>已入庫知識</b>\n{yesterday_ideas}\n\n"
        f"✅ <b>今日代辦</b>\n{todos}\n\n"
        f"📊 <b>案子狀態</b>\n{projects}\n\n"
        + (f"👥 <b>今日待跟進客戶</b>\n{crm_followups}\n\n" if crm_followups else "")
        + f"{news}\n\n"
        f"━━━━━━━━━━━━━━━\n<i>{get_bot_name()}為你守好資訊陣地</i>"
    )
    await context.bot.send_message(chat_id=CHAT_ID_INT, text=report, parse_mode="HTML")
    logger.info("✅ 早報推送完成")

    # 自動建立今日 daily log
    log_path = f"memory/daily/{today}.md"
    existing, _ = gh_read_file(log_path)
    if not existing:
        gh_write_file(
            log_path,
            f"# {today} Session 紀錄\n\n## 今日完成\n\n## 下次待辦\n\n## 發現的優化點\n",
            f"daily: 建立 {today} 日誌"
        )

async def send_weekly_summary(context: ContextTypes.DEFAULT_TYPE):
    if not CHAT_ID_INT:
        return
    logger.info("📊 群獅週報生成中...")
    today      = now_taipei().date()
    week_start = today - datetime.timedelta(days=today.weekday())

    # ── Gemini：收集本週所有資料 ──────────────────────────────────────
    week_knowledge = {"areas": [], "resources": []}
    for folder in ["knowledge/areas", "knowledge/resources"]:
        key = folder.split("/")[1]
        for f in gh_list_dir(folder):
            try:
                if datetime.date.fromisoformat(f["name"][:10]) >= week_start:
                    content, _ = gh_read_file(f"{folder}/{f['name']}")
                    m = re.search(r"summary:\s*(.+)", content)
                    week_knowledge[key].append(m.group(1).strip() if m else f["name"])
            except (ValueError, IndexError):
                pass

    week_zettel = []
    zettel_tags_all = []
    for f in gh_list_dir("zettel"):
        try:
            if datetime.date.fromisoformat(f["name"][2:12].replace("-", "-")) >= week_start:
                content, _ = gh_read_file(f"zettel/{f['name']}")
                title_m = re.search(r"title:\s*(.+)", content)
                links_m = re.search(r"links:\s*\[(.+)\]", content)
                tags_m  = re.search(r"tags:\s*\[(.+)\]", content)
                week_zettel.append({
                    "title": title_m.group(1).strip() if title_m else f["name"],
                    "links": links_m.group(1).strip() if links_m else "",
                    "tags":  tags_m.group(1).strip()  if tags_m  else "",
                })
                if tags_m:
                    zettel_tags_all.extend([t.strip() for t in tags_m.group(1).split(",")])
        except Exception:
            pass

    projects_status = read_projects_status()

    # Gemini 找 zettel 共同主題
    zettel_theme = ""
    if week_zettel:
        zettel_list_text = "\n".join([f"- {z['title']} [{z['tags']}]" for z in week_zettel])
        try:
            zettel_theme = gemini_text(
                f"以下是本週新增的 zettel 卡片：\n{zettel_list_text}\n\n"
                f"找出這些卡片的共同主題，用10字內說明，繁體中文："
            )
        except Exception:
            zettel_theme = "（主題分析失敗）"

    # ── Claude：深度策略分析 ───────────────────────────────────────────
    week_data_summary = f"""
本週（{week_start} ~ {today}）LionBrain 資料摘要：

【領域知識 areas（{len(week_knowledge['areas'])} 筆）】
{chr(10).join(['• ' + i for i in week_knowledge['areas']]) or '無'}

【參考資源 resources（{len(week_knowledge['resources'])} 筆）】
{chr(10).join(['• ' + i for i in week_knowledge['resources']]) or '無'}

【新增 zettel 卡片（{len(week_zettel)} 張）】
{chr(10).join(['• ' + z['title'] + ' | 連結：' + z['links'] for z in week_zettel]) or '無'}

【案子狀態】
{projects_status}
"""
    claude_prompt = f"""你是{get_owner_name()}的商業顧問。

以下是她這週的完整知識輸入摘要：
{week_data_summary}

請給出：

**延伸機會（2-3點）**
從這週的輸入中，哪些方向有潛力可以深挖、轉成服務或 SOP？

**優化建議（2-3點）**
哪些想法在重複但沒有結論？哪個現有做法可以做得更好？

**下週聚焦**
一句話，最值得投入的一件事。

風格：結論先行，直接，不廢話，商業思維優先。繁體中文。
"""
    try:
        claude_insight = claude_analyze(claude_prompt) if ANTHROPIC_API_KEY else gemini_text(claude_prompt)
    except Exception as e:
        claude_insight = f"（分析失敗：{e}）"

    # ── 組合週報 ──────────────────────────────────────────────────────
    zettel_section = "<i>本週無新卡片</i>"
    if week_zettel:
        zettel_lines = "\n".join([f"• {html.escape(z['title'])}" + (f" → {html.escape(z['links'])}" if z['links'] else "") for z in week_zettel])
        zettel_section = f"{zettel_lines}\n共同主題：<b>{html.escape(zettel_theme)}</b>"

    areas_section     = "\n".join([f"• {html.escape(i)}" for i in week_knowledge["areas"]]) or "<i>無</i>"
    resources_section = "\n".join([f"• {html.escape(i)}" for i in week_knowledge["resources"]]) or "<i>無</i>"

    total = len(week_knowledge["areas"]) + len(week_knowledge["resources"])
    report = (
        f"🦁 <b>{get_bot_name()} 週報 | {week_start} ~ {today}</b>\n\n"
        f"📊 <b>本週數字</b>\n"
        f"整理入庫：{total} 筆｜Zettel：{len(week_zettel)} 張\n\n"
        f"🗂️ <b>領域知識</b>\n{areas_section}\n\n"
        f"📎 <b>參考資源</b>\n{resources_section}\n\n"
        f"🃏 <b>本週 Zettel（{len(week_zettel)} 張）</b>\n{zettel_section}\n\n"
        f"📁 <b>案子狀態</b>\n{projects_status}\n\n"
        f"🧠 <b>Claude 本週分析</b>\n{html.escape(claude_insight)}\n\n"
        f"━━━━━━━━━━━━━━━\n<i>{get_bot_name()}週結，下週繼續前進</i>"
    )
    await context.bot.send_message(chat_id=CHAT_ID_INT, text=report, parse_mode="HTML")

    # 存週報到 memory/weekly/
    week_log_path = f"memory/weekly/{today}.md"
    gh_write_file(
        week_log_path,
        f"# 群獅週報 {week_start} ~ {today}\n\n## 數字\n整理：{total} 筆，Zettel：{len(week_zettel)} 張\n\n## Claude 分析\n{claude_insight}\n",
        f"weekly: {today}"
    )
    logger.info("✅ 群獅週報推送完成")


# ══════════════════════════════════════════════════════════════════════════════
# 對話式指令系統
# ══════════════════════════════════════════════════════════════════════════════

def detect_intent(text: str) -> dict:
    files = gh_list_dir("projects")
    projects = [f["name"].replace(".md","") for f in files if f["name"].endswith(".md")]
    prompt = f"""
判斷意圖，只輸出 JSON：
{{"type":"command或idea或query","action":"create_project/update_project/create_folder/list_ideas/query_project/query/idea","name":"名稱","status":"新狀態","project":"相關專案"}}

action 說明：
- query：使用者在問問題、回查資訊（例如「...到哪了？」「現在...狀況？」「...進度？」「幫我確認...」）
- query_project：只想看某個專案的原始資料
- idea：單純記錄想法或備忘，不是問句

可選專案：{', '.join(projects)}
訊息：{text}
"""
    try:
        result = re.sub(r"```json|```", "", gemini_text(prompt)).strip()
        return json.loads(result)
    except Exception:
        return {"type": "idea", "action": "idea", "name": "", "status": "", "project": ""}

def execute_create_project(name: str) -> str:
    if not name:
        return "❌ 請提供專案名稱"
    path = f"projects/{name}.md"
    existing, _ = gh_read_file(path)
    if existing:
        return f"⚠️ 專案「{name}」已存在"
    gh_write_file(path, f"# {name}\n\n## 狀態：進行中\n## 目標：\n## 下一步：\n", f"feat: 新增專案 {name}")
    return f"✅ 已建立專案：{name}"

def execute_update_project(project: str, status: str) -> str:
    if not project:
        return "❌ 請提供專案名稱"
    files = gh_list_dir("projects")
    matches = [f for f in files if project in f["name"]]
    if not matches:
        return f"❌ 找不到專案「{project}」"
    path = f"projects/{matches[0]['name']}"
    content, _ = gh_read_file(path)
    if "## 狀態：" in content:
        content = re.sub(r"## 狀態：.+", f"## 狀態：{status}", content)
    else:
        content += f"\n## 狀態：{status}\n"
    gh_write_file(path, content, f"update: {matches[0]['name']} 狀態 → {status}")
    return f"✅ 已更新「{matches[0]['name'].replace('.md','')}」→ {status}"

def execute_create_folder(name: str) -> str:
    if not name:
        return "❌ 請提供分類名稱"
    gh_write_file(f"{name}/README.md", f"# {name}\n\n", f"feat: 新增分類 {name}")
    return f"✅ 已建立分類：{name}/"

def execute_list_ideas(period: str = "今天") -> str:
    today = now_taipei().date()
    files = gh_list_dir("ideas")
    if "今天" in period or "today" in period.lower():
        target = today.strftime("%Y-%m-%d")
        filtered = [f for f in files if f["name"].startswith(target)]
        label = "今日"
    else:
        week_start = today - datetime.timedelta(days=today.weekday())
        filtered = []
        for f in files:
            try:
                if datetime.date.fromisoformat(f["name"][:10]) >= week_start:
                    filtered.append(f)
            except (ValueError, IndexError):
                pass
        label = "本週"

    if not filtered:
        return f"📭 {label}尚無 ideas"

    lines = []
    for f in sorted(filtered, key=lambda x: x["name"]):
        content, _ = gh_read_file(f"ideas/{f['name']}")
        m  = re.search(r"summary:\s*(.+)", content)
        m2 = re.search(r"project:\s*(.+)", content)
        summary = m.group(1).strip() if m else f["name"]
        proj    = m2.group(1).strip() if m2 else "一般"
        lines.append(f"• [{proj}] {summary[:80]}")

    return f"📋 {label} ideas（{len(filtered)} 筆）\n" + "\n".join(lines)

def execute_query_project(project: str) -> str:
    if not project:
        return "❌ 請提供專案名稱"
    files = gh_list_dir("projects")
    matches = [f for f in files if project in f["name"]]
    if not matches:
        return f"❌ 找不到專案「{project}」"
    content, _ = gh_read_file(f"projects/{matches[0]['name']}")
    return f"📁 {matches[0]['name'].replace('.md','')}\n\n{content[:500]}"


def build_vault_context() -> str:
    """組合 LionBrain 核心檔案內容，供 Gemini 回答問題用"""
    sections = []

    # identity + context
    for path in ["identity/background.md", "context/business-model.md"]:
        content, _ = gh_read_file(path)
        if content:
            sections.append(f"=== {path} ===\n{content[:600]}")

    # 所有 projects
    proj_files = gh_list_dir("projects")
    for f in sorted(proj_files, key=lambda x: x["name"]):
        if f["name"].endswith(".md"):
            content, _ = gh_read_file(f"projects/{f['name']}")
            if content:
                sections.append(f"=== projects/{f['name']} ===\n{content[:400]}")

    # 最新兩天 daily log
    daily_files = gh_list_dir("memory/daily")
    recent = sorted(daily_files, key=lambda x: x["name"], reverse=True)[:2]
    for f in recent:
        content, _ = gh_read_file(f"memory/daily/{f['name']}")
        if content:
            sections.append(f"=== memory/daily/{f['name']} ===\n{content[:400]}")

    return "\n\n".join(sections)


def execute_vault_query(question: str) -> str:
    """讀 LionBrain 檔案，用 Gemini 直接回答問題"""
    vault_ctx = build_vault_context()
    bot_name   = get_bot_name()
    owner_name = get_owner_name()
    prompt = f"""你是{bot_name}，{owner_name}的 AI 工作夥伴。
以下是知識庫的內容：

{vault_ctx}

---
業老闆問：{question}

請根據知識庫內容直接回答，結論先行，簡潔有力。
如果知識庫裡沒有相關資料，直接說「目前沒有記錄」並建議怎麼補充。
不要複述問題，不要廢話。
"""
    try:
        return gemini_text(prompt)
    except Exception as e:
        return f"❌ 查詢失敗：{e}"


# ══════════════════════════════════════════════════════════════════════════════
# CRM 模組（加購技能包）
# ══════════════════════════════════════════════════════════════════════════════

def crm_enabled() -> bool:
    """檢查是否開啟 CRM 模組（clients/ 資料夾有 README 即視為開啟）"""
    files = gh_list_dir("clients")
    return len(files) > 0

def crm_new_client(name: str) -> str:
    if not name:
        return "❌ 請提供客戶名稱，例如：/newclient 小美"
    path = f"clients/{name}.md"
    existing, _ = gh_read_file(path)
    if existing:
        return f"⚠️ 客戶「{name}」已存在，傳 /client {name} 查看"
    content = (
        f"---\nname: {name}\nstatus: 新客戶\n"
        f"first_contact: {today_str()}\nlast_contact: {today_str()}\nfollow_up: \n---\n\n"
        f"# 客戶：{name}\n\n## 基本資訊\n- 首次接觸：{today_str()}\n- 服務類型：\n- 來源管道：\n\n"
        f"## 諮詢記錄\n\n## 待辦跟進\n"
    )
    gh_write_file(path, content, f"crm: 新增客戶 {name}")
    return f"✅ 已建立客戶：{name}\n傳訊息時加 @{name} 可自動記錄到他的檔案"

def crm_get_client(name: str) -> str:
    if not name:
        return "❌ 請提供客戶名稱"
    files = gh_list_dir("clients")
    matches = [f for f in files if name in f["name"] and f["name"].endswith(".md")]
    if not matches:
        return f"❌ 找不到客戶「{name}」，用 /newclient {name} 建立"
    content, _ = gh_read_file(f"clients/{matches[0]['name']}")
    return f"👤 {matches[0]['name'].replace('.md','')}\n\n{content[:600]}"

def crm_list_clients() -> str:
    files = gh_list_dir("clients")
    lines = []
    for f in sorted(files, key=lambda x: x["name"]):
        if not f["name"].endswith(".md") or f["name"] == "README.md":
            continue
        content, _ = gh_read_file(f"clients/{f['name']}")
        status_m  = re.search(r"status:\s*(.+)", content)
        followup_m = re.search(r"follow_up:\s*(.+)", content)
        name   = f["name"].replace(".md", "")
        status = status_m.group(1).strip() if status_m else "未設定"
        fu     = followup_m.group(1).strip() if followup_m and followup_m.group(1).strip() else ""
        line   = f"• {name}｜{status}"
        if fu:
            line += f"｜跟進：{fu}"
        lines.append(line)
    return "👥 客戶清單\n" + "\n".join(lines) if lines else "<i>尚無客戶資料</i>"

def crm_append_note(client_name: str, note: str) -> str:
    """把備注追加到客戶檔案的諮詢記錄"""
    path = f"clients/{client_name}.md"
    content, _ = gh_read_file(path)
    if not content:
        return f"❌ 找不到客戶「{client_name}」"
    entry = f"\n### {today_str()}\n{note}\n"
    content = content.replace("## 諮詢記錄\n", f"## 諮詢記錄\n{entry}")
    # 更新 last_contact
    content = re.sub(r"last_contact:\s*.+", f"last_contact: {today_str()}", content)
    gh_write_file(path, content, f"crm: {client_name} 新增記錄")
    return f"✅ 已記錄到客戶：{client_name}"

def crm_read_followups() -> str:
    """讀取今日或近期需要跟進的客戶"""
    today = today_str()
    files = gh_list_dir("clients")
    due = []
    for f in files:
        if not f["name"].endswith(".md") or f["name"] == "README.md":
            continue
        content, _ = gh_read_file(f"clients/{f['name']}")
        fu_m = re.search(r"follow_up:\s*(.+)", content)
        if fu_m and fu_m.group(1).strip() and fu_m.group(1).strip() <= today:
            name = f["name"].replace(".md", "")
            status_m = re.search(r"status:\s*(.+)", content)
            status = status_m.group(1).strip() if status_m else ""
            due.append(f"• {name}｜{status}")
    return "\n".join(due) if due else ""


# ══════════════════════════════════════════════════════════════════════════════
# /setup 自助開通問卷（新客戶第一次使用）
# ══════════════════════════════════════════════════════════════════════════════

SETUP_BOT_NAME, SETUP_NAME, SETUP_BUSINESS, SETUP_PROJECTS, SETUP_TOPICS, SETUP_GOAL = range(6)

def _setup_already_done() -> bool:
    content, _ = gh_read_file("identity/background.md")
    return bool(content) and "使用者是誰" not in content and len(content) > 80

async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    if _setup_already_done():
        bot_name = get_bot_name()
        await update.message.reply_text(
            f"{bot_name}已完成設定。\n\n"
            "要重新設定請傳 /setup reset，或直接開始使用！"
        )
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text(
        "你好！我是你的 AI 管家。\n\n"
        "我需要了解你一點，才能每天給你最有用的早報和整理。\n"
        "6 個問題，大約 2 分鐘。\n\n"
        "第 1 題：你想叫我什麼名字？\n"
        "（例如：小幫手、小晶、AI管家、小助理）"
    )
    return SETUP_BOT_NAME

async def setup_receive_bot_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_name = update.message.text.strip()
    context.user_data["bot_name"] = bot_name
    await update.message.reply_text(
        f"好的，以後叫我{bot_name}！\n\n"
        "第 2 題：你怎麼稱呼？（例如：小美、Leo、老闆）"
    )
    return SETUP_NAME

async def setup_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text.strip()
    await update.message.reply_text(
        f"好的，{context.user_data['name']}！\n\n"
        "第 3 題：你的主要行業或服務是什麼？\n"
        "（例如：命理塔羅諮詢、室內設計、烘焙教學、行銷顧問）"
    )
    return SETUP_BUSINESS

async def setup_receive_business(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["business"] = update.message.text.strip()
    await update.message.reply_text(
        "第 4 題：你現在有哪些進行中的業務線或案子？\n"
        "（用逗號分隔，例如：個案諮詢、科儀服務、線上課程）"
    )
    return SETUP_PROJECTS

async def setup_receive_projects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["projects"] = update.message.text.strip()
    await update.message.reply_text(
        "第 5 題：你每天最想追蹤哪幾個資訊主題？\n"
        "（用逗號分隔，例如：塔羅靈性趨勢、IG短影音策略、情感諮詢市場）"
    )
    return SETUP_TOPICS

async def setup_receive_topics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["topics"] = update.message.text.strip()
    await update.message.reply_text(
        "最後一題！第 6 題：你目前最大的業務目標是什麼？\n"
        "（一句話，例如：今年做到月收 10 萬，靠個案諮詢為主）"
    )
    return SETUP_GOAL

async def setup_receive_goal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _BOT_NAME_CACHE, _OWNER_NAME_CACHE
    context.user_data["goal"] = update.message.text.strip()

    bot_name = context.user_data.get("bot_name", "AI 管家")
    name     = context.user_data.get("name", "")
    business = context.user_data.get("business", "")
    projects = context.user_data.get("projects", "")
    topics   = context.user_data.get("topics", "")
    goal     = context.user_data.get("goal", "")

    await update.message.reply_text("正在建立你的個人知識庫...")

    try:
        # 解析主題清單
        sep = "，" if "，" in topics else ","
        topic_list = [t.strip() for t in topics.split(sep) if t.strip()][:4]
        topic_list.append("Anthropic Claude 最新功能")
        topics_numbered = "\n".join([f"{i+1}. {t}" for i, t in enumerate(topic_list)])

        # 解析業務線清單
        sep2 = "，" if "，" in projects else ","
        project_list = [p.strip() for p in projects.split(sep2) if p.strip()]
        projects_bullet = "\n".join([f"- {p}" for p in project_list])

        gh_write_file(
            "identity/background.md",
            f"# 身份背景\n\n## Bot 名稱\n{bot_name}\n\n## 稱呼\n{name}\n\n## 行業 / 服務\n{business}\n\n## 業務目標\n{goal}\n\n## 最後更新\n{today_str()}\n",
            f"setup: 身份設定 - {name}",
        )
        # 更新記憶體快取
        _BOT_NAME_CACHE = bot_name
        _OWNER_NAME_CACHE = name
        gh_write_file(
            "context/business-model.md",
            f"# 商業模式\n\n## 主要服務\n{business}\n\n## 業務線\n{projects_bullet}\n\n## 業務目標\n{goal}\n\n## 最後更新\n{today_str()}\n",
            "setup: 商業模式設定",
        )
        gh_write_file(
            "context/focus.md",
            f"# 每日情報關注主題\n\n{topics_numbered}\n",
            "setup: 關注主題設定",
        )

        # 自動建立業務線 projects/ 檔案
        for proj in project_list:
            safe = re.sub(r'[\\/:*?"<>|\s，,]+', '-', proj)[:30].strip('-')
            path = f"projects/{safe}.md"
            existing, _ = gh_read_file(path)
            if not existing:
                gh_write_file(path, f"# {proj}\n\n## 狀態：進行中\n## 目標：\n## 下一步：\n", f"setup: 建立專案 {proj}")

        await update.message.reply_text(
            f"設定完成！以後叫我{bot_name}。\n\n"
            f"稱呼：{name}\n"
            f"服務：{business}\n"
            f"追蹤主題：{', '.join(topic_list[:-1])}\n"
            f"目標：{goal}\n\n"
            f"每天早上 8:00 你會收到早報。\n"
            f"現在就可以把想法、連結、語音丟給{bot_name}！"
        )
    except Exception as e:
        logger.error(f"/setup 寫入失敗: {e}", exc_info=True)
        await update.message.reply_text(f"設定失敗，請稍後再試：{e}")

    context.user_data.clear()
    return ConversationHandler.END

async def setup_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("設定已取消，隨時傳 /setup 重新開始。")
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# Telegram Handlers
# ══════════════════════════════════════════════════════════════════════════════

def is_authorized(update: Update) -> bool:
    if not CHAT_ID_INT:
        return True
    return update.effective_chat.id == CHAT_ID_INT

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text("🎙️ 收到語音，轉錄中...")
    try:
        voice_file = await update.message.voice.get_file()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        await voice_file.download_to_drive(tmp_path)
        transcribed = gemini_transcribe(tmp_path)
        os.unlink(tmp_path)
        content = f"# 語音備忘 {now_str()}\n\n{transcribed}"
        path, meta = save_idea(content, "voice")
        preview = transcribed[:200] + ("..." if len(transcribed) > 200 else "")
        await update.message.reply_text(
            f"✅ 已存入 GitHub\n📁 專案：{meta.get('project','一般')}\n📌 {meta.get('summary','')}\n\n轉錄：{preview}"
        )
    except Exception as e:
        logger.error(f"語音處理失敗: {e}", exc_info=True)
        await update.message.reply_text(f"❌ 語音處理失敗：{e}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text("🖼️ 收到圖片，分析中...")
    try:
        photo_file = await update.message.photo[-1].get_file()  # 取最高解析度
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = tmp.name
        await photo_file.download_to_drive(tmp_path)
        with open(tmp_path, "rb") as f:
            image_bytes = f.read()
        os.unlink(tmp_path)

        analysis = gemini_analyze_image(image_bytes)
        caption  = (update.message.caption or "").strip()
        content  = f"# 圖片分析 {now_str()}\n\n{('備註：' + caption + chr(10) + chr(10)) if caption else ''}{analysis}"
        path, meta = save_idea(content, "image")
        await update.message.reply_text(
            f"✅ 已存入 GitHub\n📁 專案：{meta.get('project','一般')}\n📌 {meta.get('summary','')}\n\n{analysis[:300]}"
        )
    except Exception as e:
        logger.error(f"圖片處理失敗: {e}", exc_info=True)
        await update.message.reply_text(f"❌ 圖片處理失敗：{e}")

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    text = update.message.text or ""
    urls = re.findall(r"https?://[^\s]+", text)
    if not urls:
        return
    await update.message.reply_text("🔗 收到連結，摘要中...")
    try:
        url = urls[0]
        summary = gemini_text(
            f"請整理這個連結的重點，繁體中文：\n1.標題\n2.核心重點3-5點\n3.對傳產數位轉型的啟發\n\n連結：{url}"
        )
        content = f"# 連結摘要 {now_str()}\n\n來源：{url}\n\n{summary}"
        path, meta = save_idea(content, "link")
        preview = summary[:300] + ("..." if len(summary) > 300 else "")
        await update.message.reply_text(
            f"✅ 已存入 GitHub\n📁 專案：{meta.get('project','一般')}\n📌 {meta.get('summary','')}\n\n{preview}"
        )
    except Exception as e:
        logger.error(f"連結處理失敗: {e}", exc_info=True)
        await update.message.reply_text(f"❌ 連結處理失敗：{e}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    text = (update.message.text or "").strip()
    if not text:
        return

    intent = detect_intent(text)
    action = intent.get("action", "idea")

    if action == "create_project":
        await update.message.reply_text(execute_create_project(intent.get("name", "")))
    elif action == "update_project":
        await update.message.reply_text(execute_update_project(intent.get("project",""), intent.get("status","")))
    elif action == "create_folder":
        await update.message.reply_text(execute_create_folder(intent.get("name", "")))
    elif action == "list_ideas":
        await update.message.reply_text(execute_list_ideas(text))
    elif action == "query_project":
        await update.message.reply_text(execute_query_project(intent.get("project", intent.get("name", ""))))
    elif action == "query":
        await update.message.reply_text("🔍 查詢中...")
        await update.message.reply_text(execute_vault_query(text))
    else:
        # 檢查是否有 @客戶名 標記（CRM 快速記錄）
        client_match = re.search(r"@(\S+)", text)
        if client_match and crm_enabled():
            client_name = client_match.group(1)
            note = re.sub(r"@\S+", "", text).strip()
            await update.message.reply_text(crm_append_note(client_name, note))
        else:
            content = f"# 文字備忘 {now_str()}\n\n{text}"
            path, meta = save_idea(content, "text")
            await update.message.reply_text(
                f"✅ 已存入 GitHub\n📁 專案：{meta.get('project','一般')}\n📌 {meta.get('summary','')}"
            )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    todos    = read_latest_todos()
    projects = read_projects_status()
    await update.message.reply_text(
        f"📊 <b>目前狀態</b>\n\n✅ <b>今日代辦</b>\n{todos}\n\n📁 <b>案子狀態</b>\n{projects}",
        parse_mode="HTML",
    )

async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text("🧪 測試早報生成中...")
    await send_morning_report(context)

async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    await update.message.reply_text(
        f"你的 Chat ID：<code>{cid}</code>",
        parse_mode="HTML",
    )

async def cmd_newclient(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    name = " ".join(context.args).strip() if context.args else ""
    await update.message.reply_text(crm_new_client(name))

async def cmd_client(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    name = " ".join(context.args).strip() if context.args else ""
    await update.message.reply_text(crm_get_client(name))

async def cmd_clients(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text(crm_list_clients(), parse_mode="HTML")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _setup_already_done():
        await update.message.reply_text(
            "你好！我是你的 AI 管家小獅。\n\n"
            "看起來你還沒完成初始設定。\n"
            "傳 /setup 開始 2 分鐘的問卷，我就能為你量身打造每日早報！"
        )
    else:
        bot_name = get_bot_name()
        await update.message.reply_text(
            f"{bot_name}就位！\n"
            "有任何想法、連結、語音都可以直接丟給我。\n\n"
            "/status — 查看案子與代辦\n"
            "/test   — 立即觸發早報\n"
            "/setup  — 重新設定個人資料"
        )


# ══════════════════════════════════════════════════════════════════════════════
# 主程式
# ══════════════════════════════════════════════════════════════════════════════

def main():
    if not TELEGRAM_TOKEN:
        raise ValueError("❌ TELEGRAM_TOKEN 未設定")

    logger.info("🦁 LionBrain Bot 啟動中（GitHub API 版）...")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # /setup ConversationHandler（必須最先註冊）
    setup_conv = ConversationHandler(
        entry_points=[CommandHandler("setup", cmd_setup)],
        states={
            SETUP_BOT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_receive_bot_name)],
            SETUP_NAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_receive_name)],
            SETUP_BUSINESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_receive_business)],
            SETUP_PROJECTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_receive_projects)],
            SETUP_TOPICS:   [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_receive_topics)],
            SETUP_GOAL:     [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_receive_goal)],
        },
        fallbacks=[CommandHandler("cancel", setup_cancel)],
    )
    app.add_handler(setup_conv)

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("test",   cmd_test))
    app.add_handler(CommandHandler("myid",      cmd_myid))
    app.add_handler(CommandHandler("newclient", cmd_newclient))
    app.add_handler(CommandHandler("client",    cmd_client))
    app.add_handler(CommandHandler("clients",   cmd_clients))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"https?://"), handle_url))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    jq = app.job_queue
    morning_time = datetime.time(MORNING_HOUR, MORNING_MINUTE, tzinfo=TZ)
    jq.run_daily(send_morning_report, time=morning_time)
    jq.run_daily(send_weekly_summary, time=morning_time, days=(4,))

    logger.info(f"✅ 排程：每天 {MORNING_HOUR:02d}:{MORNING_MINUTE:02d} Asia/Taipei")
    logger.info(f"📡 監聽中... Chat ID: {CHAT_ID_STR}")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
