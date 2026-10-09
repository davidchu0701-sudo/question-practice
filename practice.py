#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
國中題庫 - AI 智慧複習系統
Junior High School Question Bank & AI Practice System
"""

import os
import sys

# Ensure UTF-8 output on Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import io
import re
import json
import uuid
import time
import base64
import sqlite3
import socket
import datetime
import threading
import subprocess
import webbrowser
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import urllib.request
import urllib.error
from urllib.parse import urlparse, parse_qs
from PIL import Image

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "question_bank.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
CLOUDFLARED_PATH = os.path.join(BASE_DIR, "cloudflared.exe")
NGROK_PATH = os.path.join(BASE_DIR, "ngrok.exe")
PORT = int(os.environ.get("PORT", 8080))

DEFAULT_MODEL = "gemini-3.5-flash"
BACKUP_MODELS = ["gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-flash-latest"]

os.makedirs(UPLOAD_DIR, exist_ok=True)

# 全域公網網址 (由 ngrok 或 Cloudflare Tunnel 提供)
PUBLIC_TUNNEL_URL = ""
PUBLIC_TUNNEL_TYPE = "cloudflare" # "ngrok" or "cloudflare"

# 支援的科目清單
SUBJECTS = [
    {"name": "國文", "icon": "📖", "color": "blue"},
    {"name": "英文", "icon": "🔤", "color": "emerald"},
    {"name": "數學", "icon": "📐", "color": "amber"},
    {"name": "歷史", "icon": "🏛️", "color": "rose"},
    {"name": "地理", "icon": "🗺️", "color": "teal"},
    {"name": "公民", "icon": "⚖️", "color": "purple"},
    {"name": "生物", "icon": "🔬", "color": "green"},
]

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject TEXT NOT NULL,
        unit TEXT NOT NULL,
        content TEXT NOT NULL DEFAULT '',
        options_json TEXT DEFAULT '[]',
        question_type TEXT DEFAULT 'choice',
        image_filename TEXT DEFAULT '',
        standard_answer TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS ai_usage_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        date_str TEXT NOT NULL,
        model TEXT NOT NULL,
        action_type TEXT NOT NULL,
        status TEXT NOT NULL
    )
    """)

    # 欄位遷移檢查
    cur.execute("PRAGMA table_info(questions)")
    cols = [r[1] for r in cur.fetchall()]
    if "content" not in cols:
        cur.execute("ALTER TABLE questions ADD COLUMN content TEXT DEFAULT ''")
    if "options_json" not in cols:
        cur.execute("ALTER TABLE questions ADD COLUMN options_json TEXT DEFAULT '[]'")
    if "question_type" not in cols:
        cur.execute("ALTER TABLE questions ADD COLUMN question_type TEXT DEFAULT 'choice'")

    # 自動將過期或額度受限的舊模型更新為穩定大配額的 gemini-3.5-flash
    cur.execute("SELECT value FROM settings WHERE key = 'gemini_model'")
    row = cur.fetchone()
    if not row or row[0] in ["gemini-2.5-flash", "gemini-3.8-flash"]:
        cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('gemini_model', ?)", (DEFAULT_MODEL,))

    # 取消預設的範例題目 (ID 1, 2, 3 或 sample_*.svg)
    cur.execute("DELETE FROM questions WHERE id IN (1, 2, 3) OR image_filename IN ('sample_math.svg', 'sample_chi.svg', 'sample_bio.svg')")

    conn.commit()
    conn.close()

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_setting(key, default=""):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    if row:
        return row["value"]
    return os.environ.get(key.upper(), default)

def set_setting(key, value):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def log_ai_usage(model, action_type, status="success"):
    try:
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO ai_usage_logs (date_str, model, action_type, status) VALUES (?, ?, ?, ?)",
                    (today_str, model, action_type, status))
        conn.commit()
        conn.close()
    except Exception as e:
        print("Log AI usage error:", e)

def get_ai_quota_stats():
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    current_model = get_setting("gemini_model", DEFAULT_MODEL)
    
    # 免費版模型每日上限配額 (Requests Per Day)
    LIMIT_MAP = {
        "gemini-3.5-flash": 1500,
        "gemini-3.1-flash-lite": 1500,
        "gemini-flash-latest": 1500,
        "gemini-3.8-flash": 20
    }
    daily_limit = LIMIT_MAP.get(current_model, 1500)

    conn = get_db()
    cur = conn.cursor()
    # 今日總使用次數
    cur.execute("SELECT COUNT(*) FROM ai_usage_logs WHERE date_str = ? AND status = 'success'", (today_str,))
    today_used = cur.fetchone()[0]

    # 今日按動作分類
    cur.execute("SELECT action_type, COUNT(*) FROM ai_usage_logs WHERE date_str = ? AND status = 'success' GROUP BY action_type", (today_str,))
    by_action = {r[0]: r[1] for r in cur.fetchall()}

    # 今日按模型分類
    cur.execute("SELECT model, COUNT(*) FROM ai_usage_logs WHERE date_str = ? AND status = 'success' GROUP BY model", (today_str,))
    by_model = {r[0]: r[1] for r in cur.fetchall()}
    conn.close()

    today_remaining = max(0, daily_limit - today_used)
    percent_remaining = round((today_remaining / daily_limit) * 100, 1) if daily_limit > 0 else 0

    return {
        "current_model": current_model,
        "today_date": today_str,
        "today_used": today_used,
        "daily_limit": daily_limit,
        "today_remaining": today_remaining,
        "percent_remaining": percent_remaining,
        "by_action": {
            "extract": by_action.get("extract", 0),
            "hint": by_action.get("hint", 0),
            "solve": by_action.get("solve", 0),
            "grade": by_action.get("grade", 0)
        },
        "by_model": by_model,
        "reset_time_desc": "每日台灣時間 08:00 (UTC 00:00) 自動重置免費額度"
    }

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"

# 壓縮並調整圖片大小以提高 AI 辨識速度並防止 503 超載
def optimize_image_base64(raw_base64, max_dim=1024):
    try:
        if "," in raw_base64:
            raw_base64 = raw_base64.split(",", 1)[1]
        img_bytes = base64.b64decode(raw_base64)
        img = Image.open(io.BytesIO(img_bytes))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > max_dim:
            img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception:
        return raw_base64

def call_gemini(prompt, image_path=None, image_base64=None, action_type="other"):
    api_key = get_setting("gemini_api_key", "").strip()
    if not api_key:
        return {
            "error": "NO_API_KEY",
            "message": "尚未設定 Gemini API Key。請點擊右上角「⚙️ 設定」輸入您的 Google Gemini API Key 即可啟用 AI 功能。"
        }
    
    preferred_model = get_setting("gemini_model", DEFAULT_MODEL).strip()
    if not preferred_model or preferred_model in ["gemini-2.5-flash", "gemini-3.8-flash"]:
        preferred_model = DEFAULT_MODEL
    
    # 建構備援模型鏈：若優先模型額度用盡 (429) 或忙碌 (503)，自動切換至備援模型
    model_chain = [preferred_model]
    for bm in BACKUP_MODELS:
        if bm not in model_chain:
            model_chain.append(bm)
    
    parts = [{"text": prompt}]
    
    if image_base64:
        opt_b64 = optimize_image_base64(image_base64)
        parts.append({
            "inlineData": {
                "mimeType": "image/jpeg",
                "data": opt_b64
            }
        })
    elif image_path and os.path.exists(image_path):
        ext = os.path.splitext(image_path)[1].lower()
        if ext in [".jpg", ".jpeg", ".png", ".webp"]:
            try:
                img = Image.open(image_path)
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                if max(img.size) > 1024:
                    img.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=80)
                img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
                parts.append({
                    "inlineData": {
                        "mimeType": "image/jpeg",
                        "data": img_b64
                    }
                })
            except Exception:
                pass
    
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": 0.2
        }
    }
    req_data = json.dumps(payload).encode("utf-8")

    last_error = None
    for current_model in model_chain:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{current_model}:generateContent?key={api_key}"
        req = urllib.request.Request(
            url,
            data=req_data,
            headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                candidates = data.get("candidates", [])
                if candidates:
                    content_parts = candidates[0].get("content", {}).get("parts", [])
                    text = "".join([p.get("text", "") for p in content_parts if "text" in p])
                    log_ai_usage(current_model, action_type, status="success")
                    return {"success": True, "text": text, "used_model": current_model}
                return {"error": "EMPTY_RESPONSE", "message": "AI 未能產生內容，請重試。"}
        except urllib.error.HTTPError as e:
            err_msg = e.read().decode("utf-8")
            try:
                err_json = json.loads(err_msg)
                msg = err_json.get("error", {}).get("message", err_msg)
            except Exception:
                msg = err_msg
            
            # 若為額度用盡 (429) 或 伺服器忙碌 (503) 或 模型不存在 (404)，自動換下一個備援模型嘗試
            if e.code in [429, 503, 404]:
                print(f"[Model Fallback] {current_model} returned {e.code}, automatically trying next model in chain...")
                log_ai_usage(current_model, action_type, status=f"fallback_{e.code}")
                last_error = {"error": f"HTTP_{e.code}", "message": f"{current_model} 錯誤 ({e.code}): {msg}"}
                continue
            log_ai_usage(current_model, action_type, status=f"error_{e.code}")
            return {"error": f"HTTP_{e.code}", "message": f"Gemini API 錯誤 ({e.code}): {msg}"}
        except Exception as e:
            last_error = {"error": "REQUEST_FAILED", "message": f"連線失敗: {str(e)}"}
            continue

    return last_error or {"error": "ALL_MODELS_FAILED", "message": "所有備援 AI 模型均回傳錯誤，請稍後再試。"}

CURRENT_TUNNEL_PROC = None

def start_public_tunnel():
    global PUBLIC_TUNNEL_URL, PUBLIC_TUNNEL_TYPE, CURRENT_TUNNEL_PROC
    
    ngrok_token = get_setting("ngrok_authtoken", "").strip()
    ngrok_domain = get_setting("ngrok_domain", "").strip()

    def run_tunnel():
        global PUBLIC_TUNNEL_URL, PUBLIC_TUNNEL_TYPE, CURRENT_TUNNEL_PROC
        
        # 優先方案：若有設定 ngrok Token，使用 ngrok 永久固定專屬網址
        if ngrok_token and os.path.exists(NGROK_PATH):
            try:
                # 設定 authtoken
                subprocess.run([NGROK_PATH, "config", "add-authtoken", ngrok_token], capture_output=True)
                cmd = [NGROK_PATH, "http", str(PORT)]
                if ngrok_domain:
                    cmd.extend(["--url", ngrok_domain])
                
                print("🌐 正在啟動 ngrok 永久固定網址隧道...")
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                CURRENT_TUNNEL_PROC = proc
                PUBLIC_TUNNEL_TYPE = "ngrok"
                
                # 透過 ngrok 本地 API (127.0.0.1:4040) 獲取固定公網網址
                for _ in range(15):
                    time.sleep(1)
                    try:
                        req = urllib.request.Request("http://127.0.0.1:4040/api/tunnels")
                        with urllib.request.urlopen(req, timeout=2) as resp:
                            tdata = json.loads(resp.read().decode("utf-8"))
                            tunnels = tdata.get("tunnels", [])
                            if tunnels:
                                PUBLIC_TUNNEL_URL = tunnels[0].get("public_url", "")
                                print("\n" + "=" * 65)
                                print(" 🎉 ngrok 永久固定網址已建立！")
                                print(f" 🌐 永久固定網址: {PUBLIC_TUNNEL_URL}")
                                print("=" * 65 + "\n")
                                break
                    except Exception:
                        pass
                if PUBLIC_TUNNEL_URL:
                    proc.wait()
                    return
            except Exception as e:
                print("ngrok 啟動失敗，切換回 Cloudflare 備援:", e)

        # 備援方案：Cloudflare 隨身臨時隧道
        if os.path.exists(CLOUDFLARED_PATH):
            PUBLIC_TUNNEL_TYPE = "cloudflare"
            print("🌐 正在建立 Cloudflare 免費外出公網隧道...")
            cmd = [CLOUDFLARED_PATH, "tunnel", "--url", f"http://127.0.0.1:{PORT}"]
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )
                CURRENT_TUNNEL_PROC = proc
                for line in iter(proc.stdout.readline, ''):
                    if not line:
                        break
                    match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
                    if match and not PUBLIC_TUNNEL_URL:
                        PUBLIC_TUNNEL_URL = match.group(0)
                        print("\n" + "=" * 65)
                        print(" 🎉 外出公網連線隧道建立成功！")
                        print(f" 🌐 外出隨身網址: {PUBLIC_TUNNEL_URL}")
                        print("=" * 65 + "\n")
                proc.wait()
            except Exception as e:
                print("Cloudflare Tunnel 啟動失敗:", e)

    t = threading.Thread(target=run_tunnel, daemon=True)
    t.start()

class RequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def log_message(self, format, *args):
        pass

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/api/subjects":
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT subject, COUNT(*) as count FROM questions GROUP BY subject")
            counts = {row["subject"]: row["count"] for row in cur.fetchall()}
            conn.close()
            result = []
            for s in SUBJECTS:
                result.append({
                    "name": s["name"],
                    "icon": s["icon"],
                    "color": s["color"],
                    "count": counts.get(s["name"], 0)
                })
            self.send_json(result)
            return

        elif path == "/api/units":
            subject = query.get("subject", [""])[0]
            conn = get_db()
            cur = conn.cursor()
            if subject:
                cur.execute("SELECT DISTINCT unit FROM questions WHERE subject = ? ORDER BY unit", (subject,))
            else:
                cur.execute("SELECT DISTINCT unit FROM questions ORDER BY unit")
            units = [row["unit"] for row in cur.fetchall() if row["unit"]]
            conn.close()
            self.send_json(units)
            return

        elif path == "/api/questions":
            subject = query.get("subject", [""])[0]
            unit = query.get("unit", [""])[0]
            units = query.get("units[]", [])
            is_random = query.get("random", ["0"])[0] == "1"
            limit = query.get("limit", [""])[0]

            conn = get_db()
            cur = conn.cursor()
            sql = "SELECT * FROM questions WHERE 1=1"
            params = []

            if subject:
                sql += " AND subject = ?"
                params.append(subject)
            if unit:
                sql += " AND unit = ?"
                params.append(unit)
            elif units:
                placeholders = ",".join(["?"] * len(units))
                sql += f" AND unit IN ({placeholders})"
                params.extend(units)

            if is_random:
                sql += " ORDER BY RANDOM()"
            else:
                sql += " ORDER BY id DESC"

            if limit and limit.isdigit():
                sql += f" LIMIT {int(limit)}"

            cur.execute(sql, params)
            rows = []
            for row in cur.fetchall():
                d = dict(row)
                try:
                    d["options"] = json.loads(d.get("options_json") or "[]")
                except Exception:
                    d["options"] = []
                rows.append(d)
            conn.close()
            self.send_json(rows)
            return

        elif path == "/api/ai/quota":
            stats = get_ai_quota_stats()
            self.send_json(stats)
            return

        elif path == "/api/settings":
            api_key = get_setting("gemini_api_key", "")
            masked_key = ""
            if api_key:
                masked_key = api_key[:6] + "..." + api_key[-4:] if len(api_key) > 10 else "***"
            model = get_setting("gemini_model", DEFAULT_MODEL)
            ngrok_token = get_setting("ngrok_authtoken", "")
            masked_token = ""
            if ngrok_token:
                masked_token = ngrok_token[:6] + "..." + ngrok_token[-4:] if len(ngrok_token) > 10 else "***"
            ngrok_domain = get_setting("ngrok_domain", "")
            self.send_json({
                "has_key": bool(api_key),
                "masked_key": masked_key,
                "model": model,
                "local_ip": get_local_ip(),
                "port": PORT,
                "public_url": PUBLIC_TUNNEL_URL,
                "tunnel_type": PUBLIC_TUNNEL_TYPE,
                "has_ngrok_token": bool(ngrok_token),
                "masked_ngrok_token": masked_token,
                "ngrok_domain": ngrok_domain
            })
            return

        elif path == "/":
            self.path = "/index.html"
            return super().do_GET()

        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length)

        try:
            body = json.loads(post_data.decode("utf-8")) if post_data else {}
        except Exception:
            body = {}

        # ------------------------------------------------------------------
        # AI 拍照提取考題內文 (支援單張或多張照片批量提取)
        # ------------------------------------------------------------------
        if path == "/api/ai/extract":
            images = body.get("images_base64", [])
            single_image = body.get("image_base64", "")
            if not images and single_image:
                images = [single_image]
            
            subject = body.get("subject", "").strip()
            unit_hint = body.get("unit", "").strip()

            if not images:
                self.send_json({"error": "請提供照片以供 AI 提取內文"}, status=400)
                return

            all_extracted_questions = []

            for img_index, img_b64 in enumerate(images):
                # 儲存原圖備份
                if "," in img_b64:
                    raw_b64 = img_b64.split(",", 1)[1]
                else:
                    raw_b64 = img_b64

                page_filename = f"q_page_{int(time.time())}_{uuid.uuid4().hex[:6]}.jpg"
                page_filepath = os.path.join(UPLOAD_DIR, page_filename)
                orig_img = None
                try:
                    raw_bytes = base64.b64decode(raw_b64)
                    with open(page_filepath, "wb") as f:
                        f.write(raw_bytes)
                    orig_img = Image.open(io.BytesIO(raw_bytes))
                except Exception:
                    pass

                prompt = f"""
你是一位專業的台灣國中考卷題目數位化與整理專家。
目前科目設定：【{subject or '未指定'}】
單元參考：【{unit_hint or '依考題推測'}】
請仔細辨識此張考卷照片（第 {img_index + 1} 頁）。照片中可能有 1 題或多題。
請將照片中的題目完整提取為清晰、排版工整的繁體中文文字題目。

特別要求：
1. 一定要【以文字題目】提取，讓學生在手機或電腦螢幕上看文字就能清楚做題。
2. 提取題幹文字與選項 (A)、(B)、(C)、(D)，選項放入 options 陣列。
3. 若考卷上有手寫或老師批改答案，請嘗試推導並填入 standard_answer。
4. 【關鍵：插圖擷取】：
   - 若題目有幾何圖、函數圖、統計圖表、地圖或插圖，請設 has_diagram 為 true。
   - 請精準給出【該幾何圖形/插圖本身】的邊界框 diagram_box（格式為 [ymin, xmin, ymax, xmax]，數值為 0 到 1000 之整數）。
   - 注意：diagram_box 只能框取該題目的「幾何圖形本身」（包含圖上的字母頂點 A, B, C 等標記），【絕對不可】框到題幹文字、其他題目或整張考卷！
   - 若題目為純文字無圖形，請設 has_diagram 為 false，diagram_box 為 null。
5. 請輸出標準 JSON 格式：
{{
  "questions": [
    {{
      "content": "題幹完整文字",
      "options": ["(A) ...", "(B) ...", "(C) ...", "(D) ..."],
      "question_type": "choice",
      "suggested_unit": "推測的章節單元名稱",
      "standard_answer": "推測的標準答案",
      "has_diagram": false,
      "diagram_box": null,
      "notes": "題型備註"
    }}
  ]
}}
"""
                ai_res = call_gemini(prompt, image_base64=img_b64, action_type="extract")
                if ai_res.get("success"):
                    raw_text = ai_res.get("text", "")
                    try:
                        clean_json = raw_text.strip()
                        if clean_json.startswith("```json"):
                            clean_json = clean_json[7:]
                        elif clean_json.startswith("```"):
                            clean_json = clean_json[3:]
                        if clean_json.endswith("```"):
                            clean_json = clean_json[:-3]
                        clean_json = clean_json.strip()
                        parsed = json.loads(clean_json)
                        q_list = parsed.get("questions", [])

                        for q_item in q_list:
                            diag_filename = ""
                            # 若有插圖且有原圖，自動使用 Pillow 裁切插圖存檔
                            if q_item.get("has_diagram") and orig_img and q_item.get("diagram_box"):
                                box = q_item["diagram_box"]
                                if isinstance(box, list) and len(box) == 4:
                                    ymin, xmin, ymax, xmax = box
                                    w_o, h_o = orig_img.size
                                    pad_x = int(w_o * 0.015)
                                    pad_y = int(h_o * 0.015)
                                    crop_box = (
                                        int(max(0, xmin * w_o / 1000 - pad_x)),
                                        int(max(0, ymin * h_o / 1000 - pad_y)),
                                        int(min(w_o, xmax * w_o / 1000 + pad_x)),
                                        int(min(h_o, ymax * h_o / 1000 + pad_y))
                                    )
                                    if crop_box[2] > crop_box[0] and crop_box[3] > crop_box[1]:
                                        diag_filename = f"diag_{int(time.time())}_{uuid.uuid4().hex[:6]}.jpg"
                                        cropped = orig_img.crop(crop_box)
                                        if cropped.mode in ("RGBA", "P"):
                                            cropped = cropped.convert("RGB")
                                        cropped.save(os.path.join(UPLOAD_DIR, diag_filename), quality=85)

                            all_extracted_questions.append({
                                "content": q_item.get("content", ""),
                                "options": q_item.get("options", []),
                                "question_type": q_item.get("question_type", "choice"),
                                "suggested_unit": q_item.get("suggested_unit") or unit_hint or "第一單元",
                                "standard_answer": q_item.get("standard_answer", ""),
                                "image_filename": diag_filename,
                                "page_image": page_filename,
                                "notes": q_item.get("notes", "")
                            })
                    except Exception as e:
                        print("Parse error on image", img_index, e)
                else:
                    print("AI extract error on image", img_index, ai_res.get("message"))

            if not all_extracted_questions:
                self.send_json({"error": "未能成功從照片中提取考題，請確認照片清晰度與網路狀態。"}, status=500)
                return

            self.send_json({"success": True, "data": all_extracted_questions})
            return

        # ------------------------------------------------------------------
        # 批次新增多題
        # ------------------------------------------------------------------
        elif path == "/api/questions/batch":
            questions_list = body.get("questions", [])
            if not questions_list:
                self.send_json({"error": "沒有題目可供批次新增"}, status=400)
                return

            conn = get_db()
            cur = conn.cursor()
            inserted_count = 0
            for q in questions_list:
                sub = q.get("subject", "").strip()
                unit = q.get("unit", "").strip()
                content = q.get("content", "").strip()
                options = q.get("options", [])
                q_type = q.get("question_type", "choice").strip()
                std_ans = q.get("standard_answer", "").strip()
                img_fn = q.get("image_filename", "").strip()
                notes = q.get("notes", "").strip()

                if not sub or not unit or not content:
                    continue

                opt_json = json.dumps(options, ensure_ascii=False) if isinstance(options, list) else "[]"
                cur.execute("""
                INSERT INTO questions (subject, unit, content, options_json, question_type, image_filename, standard_answer, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (sub, unit, content, opt_json, q_type, img_fn, std_ans, notes))
                inserted_count += 1

            conn.commit()
            conn.close()
            self.send_json({"success": True, "inserted_count": inserted_count})
            return

        # ------------------------------------------------------------------
        # 設定
        # ------------------------------------------------------------------
        elif path == "/api/settings":
            api_key = body.get("gemini_api_key")
            model = body.get("gemini_model")
            ngrok_token = body.get("ngrok_authtoken")
            ngrok_domain = body.get("ngrok_domain")

            if api_key is not None:
                set_setting("gemini_api_key", api_key.strip())
            if model is not None:
                m = model.strip()
                if m in ["gemini-2.5-flash", "gemini-3.8-flash"]:
                    m = DEFAULT_MODEL
                set_setting("gemini_model", m)
            
            restarted_tunnel = False
            if ngrok_token is not None:
                set_setting("ngrok_authtoken", ngrok_token.strip())
                restarted_tunnel = True
            if ngrok_domain is not None:
                set_setting("ngrok_domain", ngrok_domain.strip())
                restarted_tunnel = True

            if restarted_tunnel:
                # 重新觸發一次 tunnel
                global CURRENT_TUNNEL_PROC, PUBLIC_TUNNEL_URL
                PUBLIC_TUNNEL_URL = ""
                if CURRENT_TUNNEL_PROC:
                    try:
                        CURRENT_TUNNEL_PROC.terminate()
                    except Exception:
                        pass
                threading.Thread(target=start_public_tunnel, daemon=True).start()

            self.send_json({"success": True})
            return

        # ------------------------------------------------------------------
        # AI 提示
        # ------------------------------------------------------------------
        elif path == "/api/ai/hint":
            q_id = body.get("question_id")
            if not q_id:
                self.send_json({"error": "未提供題目 ID"}, status=400)
                return

            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT * FROM questions WHERE id = ?", (q_id,))
            q = cur.fetchone()
            conn.close()

            if not q:
                self.send_json({"error": "找不到該題目"}, status=404)
                return

            img_path = os.path.join(UPLOAD_DIR, q["image_filename"]) if q["image_filename"] else None
            options_text = ""
            try:
                opts = json.loads(q["options_json"] or "[]")
                if opts:
                    options_text = "\n選項：\n" + "\n".join(opts)
            except Exception:
                pass

            prompt = f"""
你是一位親切且教學經驗豐富的台灣國中老師。
科目：{q['subject']}
單元：{q['unit']}
題目文字內容：
{q['content'] or '（請參考圖片）'}{options_text}

學生正在做這道題目，但卡住了並點擊了「💡 AI 提示」。
請針對這道題目的文字與考點：
1. 找出這道題的核心考點、關鍵觀念或解題思路。
2. 提供清晰的解題線索或思考步驟提示（例如相關的定理、公式、文意關鍵字、解題切入點）。
3. 【嚴格規定】：請勿直接透露最終答案或選項字母！引導學生自行思考得出答案。
4. 請使用繁體中文，語氣親切熱情、具鼓勵性。長度約 80-180 字。
"""
            ai_res = call_gemini(prompt, image_path=img_path, action_type="hint")
            self.send_json(ai_res)
            return

        # ------------------------------------------------------------------
        # AI 解答
        # ------------------------------------------------------------------
        elif path == "/api/ai/solve":
            q_id = body.get("question_id")
            if not q_id:
                self.send_json({"error": "未提供題目 ID"}, status=400)
                return

            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT * FROM questions WHERE id = ?", (q_id,))
            q = cur.fetchone()
            conn.close()

            if not q:
                self.send_json({"error": "找不到該題目"}, status=404)
                return

            img_path = os.path.join(UPLOAD_DIR, q["image_filename"]) if q["image_filename"] else None
            ref_ans = f"\n出題者設定的參考答案：{q['standard_answer']}" if q['standard_answer'] else ""
            options_text = ""
            try:
                opts = json.loads(q["options_json"] or "[]")
                if opts:
                    options_text = "\n選項：\n" + "\n".join(opts)
            except Exception:
                pass

            prompt = f"""
你是一位權威的台灣國中名師。
科目：{q['subject']}
單元：{q['unit']}{ref_ans}
題目文字內容：
{q['content'] or '（請參考圖片）'}{options_text}

請針對這道題目，給出完整詳盡的解題分析：
請包含以下結構：
【正確答案】：清楚標示出答案（選擇題請標註選項與文字）。
【詳細解題步驟】：一步一步條理分明地列出計算過程、推導觀念或文意解析。
【觀念重點】：此題涉及的國中核心觀念或公式複習。
【易錯陷阱提醒】：國中生容易粗心或誤選的常見盲點。

請使用流暢易讀的繁體中文，排版整齊清晰。
"""
            ai_res = call_gemini(prompt, image_path=img_path, action_type="solve")
            self.send_json(ai_res)
            return

        # ------------------------------------------------------------------
        # AI 自動批改
        # ------------------------------------------------------------------
        elif path == "/api/ai/grade":
            submissions = body.get("submissions", [])
            if not submissions:
                self.send_json({"error": "沒有提交任何作答紀錄"}, status=400)
                return

            conn = get_db()
            cur = conn.cursor()
            q_ids = [s.get("question_id") for s in submissions if s.get("question_id")]
            if not q_ids:
                conn.close()
                self.send_json({"error": "題目無效"}, status=400)
                return

            placeholders = ",".join(["?"] * len(q_ids))
            cur.execute(f"SELECT * FROM questions WHERE id IN ({placeholders})", q_ids)
            q_map = {row["id"]: dict(row) for row in cur.fetchall()}
            conn.close()

            results = []
            correct_count = 0
            api_key = get_setting("gemini_api_key", "").strip()

            for item in submissions:
                qid = item.get("question_id")
                student_ans = str(item.get("student_answer", "")).strip()
                q = q_map.get(qid)
                if not q:
                    continue

                img_path = os.path.join(UPLOAD_DIR, q["image_filename"]) if q["image_filename"] else None
                std_ans = (q["standard_answer"] or "").strip()

                # 標準答案快速精確比對
                quick_match = False
                if std_ans and student_ans:
                    clean_std = std_ans.upper().replace(" ", "")
                    clean_stu = student_ans.upper().replace(" ", "")
                    if clean_std == clean_stu:
                        quick_match = True

                if quick_match:
                    results.append({
                        "question_id": qid,
                        "status": "correct",
                        "student_answer": student_ans,
                        "standard_answer": std_ans,
                        "feedback": "回答完全正確！掌握度良好。",
                        "explanation": f"標準答案為【{std_ans}】。"
                    })
                    correct_count += 1
                    continue

                if not api_key:
                    is_correct = (std_ans.upper() == student_ans.upper()) if (std_ans and student_ans) else False
                    status = "correct" if is_correct else "incorrect"
                    if is_correct:
                        correct_count += 1
                    results.append({
                        "question_id": qid,
                        "status": status,
                        "student_answer": student_ans,
                        "standard_answer": std_ans or "（待 AI 解析）",
                        "feedback": "完全正確！" if is_correct else "作答未匹配標準答案。（請設定 Gemini API Key 以啟用完整 AI 智能圖像題目精準批改）",
                        "explanation": f"您的作答：{student_ans or '未填寫'}。標準答案：{std_ans or '未設定'}。"
                    })
                    continue

                # 呼叫 Gemini AI 批改
                options_text = ""
                try:
                    opts = json.loads(q["options_json"] or "[]")
                    if opts:
                        options_text = "\n選項：\n" + "\n".join(opts)
                except Exception:
                    pass

                prompt = f"""
你是一位專業的台灣國中老師。正在進行考卷批改。
科目：{q['subject']}
單元：{q['unit']}
題目文字內容：
{q['content'] or '（請參考圖片）'}{options_text}
出題者設定之參考答案（若有）：{std_ans or '未提供，請由題目文字判斷正確答案'}
學生的作答內容：{student_ans if student_ans else '（空白未作答）'}

請審查學生的作答：
以嚴格但公平的標準判定學生的答案是否正確。
請輸出 JSON 格式：
{{
  "is_correct": true 或 false 或 "partial",
  "correct_answer": "正確答案（含選項字母或數值）",
  "feedback": "一句話簡短評語（例如：計算正確、觀念有誤混淆了XX觀念、未寫單位等）",
  "explanation": "簡要解析與訂正建議（約 50-120 字）"
}}
"""
                ai_res = call_gemini(prompt, image_path=img_path, action_type="grade")
                if ai_res.get("success"):
                    raw_text = ai_res.get("text", "")
                    try:
                        clean_json = raw_text.strip()
                        if clean_json.startswith("```json"):
                            clean_json = clean_json[7:]
                        elif clean_json.startswith("```"):
                            clean_json = clean_json[3:]
                        if clean_json.endswith("```"):
                            clean_json = clean_json[:-3]
                        clean_json = clean_json.strip()
                        parsed_ans = json.loads(clean_json)

                        is_c = parsed_ans.get("is_correct")
                        status = "correct" if is_c is True else ("partial" if is_c == "partial" else "incorrect")
                        if status == "correct":
                            correct_count += 1

                        results.append({
                            "question_id": qid,
                            "status": status,
                            "student_answer": student_ans,
                            "standard_answer": parsed_ans.get("correct_answer", std_ans),
                            "feedback": parsed_ans.get("feedback", ""),
                            "explanation": parsed_ans.get("explanation", "")
                        })
                    except Exception:
                        results.append({
                            "question_id": qid,
                            "status": "manual_review",
                            "student_answer": student_ans,
                            "standard_answer": std_ans,
                            "feedback": "AI 批改完成",
                            "explanation": raw_text
                        })
                else:
                    results.append({
                        "question_id": qid,
                        "status": "error",
                        "student_answer": student_ans,
                        "standard_answer": std_ans,
                        "feedback": "AI 批改暫時無法連線",
                        "explanation": ai_res.get("message", "請檢查網路與 API Key 設定。")
                    })

            total = len(submissions)
            score = round((correct_count / total) * 100) if total > 0 else 0

            self.send_json({
                "score": score,
                "total": total,
                "correct_count": correct_count,
                "results": results
            })
            return

        self.send_json({"error": "Not Found"}, status=404)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/api/questions":
            q_id = query.get("id", [""])[0]
            if not q_id or not q_id.isdigit():
                self.send_json({"error": "請提供有效的題目 ID"}, status=400)
                return

            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT image_filename FROM questions WHERE id = ?", (int(q_id),))
            row = cur.fetchone()

            if row:
                if row["image_filename"]:
                    img_path = os.path.join(UPLOAD_DIR, row["image_filename"])
                    if os.path.exists(img_path):
                        try:
                            os.remove(img_path)
                        except Exception:
                            pass
                cur.execute("DELETE FROM questions WHERE id = ?", (int(q_id),))
                conn.commit()
                conn.close()
                self.send_json({"success": True})
            else:
                conn.close()
                self.send_json({"error": "題目不存在"}, status=404)
            return

        self.send_json({"error": "Not Found"}, status=404)

def run_server():
    init_db()
    local_ip = get_local_ip()
    local_url = f"http://localhost:{PORT}"
    mobile_url = f"http://{local_ip}:{PORT}"

    # 啟動公網穿透隧道 (ngrok 永久固定網址 / Cloudflare 備援)
    start_public_tunnel()

    print("=" * 65)
    print(" 🎓 國中題庫 - AI 智慧複習系統已成功啟動！")
    print("=" * 65)
    print(f" 💻 電腦本機網址:  {local_url}")
    print(f" 🏠 同 Wi-Fi 網址:  {mobile_url}")
    print(" 🌐 外出公網網址正在啟動中，稍後將顯示於終端機及網頁「📱 手機連線」視窗")
    print("=" * 65)
    print(" 按下 Ctrl+C 即可停止伺服器。\n")

    try:
        webbrowser.open(local_url)
    except Exception:
        pass

    server = ThreadingHTTPServer(("0.0.0.0", PORT), RequestHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n系統已停止運作。再見！")
        server.server_close()

if __name__ == "__main__":
    run_server()
