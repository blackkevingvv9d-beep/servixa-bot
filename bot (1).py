"""
================================================================================
 Servixa Host — Single-file Telegram Mini App + Python Hosting Panel
================================================================================

A self-contained Python file. Just run it on any host (Replit, Railway,
Render, Fly.io, a VPS, etc.) and it will:

  • Start a Flask web server (the Telegram Mini App panel).
  • Start a long-polling Telegram bot (`/start` opens the panel).
  • Let users paste/upload Python scripts, install pip libraries,
    run/stop them, watch live logs, and feed interactive stdin
    (e.g. Telethon phone/code login).

────────────────────────────────────────────────────────────────────────────────
REQUIREMENTS (install once)
────────────────────────────────────────────────────────────────────────────────
    pip install flask "python-telegram-bot==22.7"

────────────────────────────────────────────────────────────────────────────────
ENVIRONMENT VARIABLES
────────────────────────────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN   (REQUIRED)  — token from @BotFather
    PORT                 (optional)  — default 8080
    REPLIT_DOMAINS       (optional)  — comma-separated public HTTPS domains
    REPLIT_DEV_DOMAIN    (optional)  — fallback dev HTTPS domain

The bot needs a public HTTPS URL to open the WebApp. On Replit both
REPLIT_DOMAINS / REPLIT_DEV_DOMAIN are set automatically. On other hosts,
set REPLIT_DOMAINS to your public domain (e.g. "myapp.up.railway.app").

────────────────────────────────────────────────────────────────────────────────
RUN
────────────────────────────────────────────────────────────────────────────────
    python servixa_host.py

================================================================================
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import parse_qsl

from flask import Flask, request, jsonify, Response

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
    MenuButtonWebApp,
)
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode


# =============================================================================
# Config & logging
# =============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("servixa")

DEFAULT_BOT_TOKEN = "8739910357:AAGzWqylExYYLnrftMUUhfuYjGMVzsO4d3g"
DEFAULT_PORT = 5000
DEFAULT_PUBLIC_DOMAIN = "d3ddcdcd-f438-4d6c-8eaf-2735b84a2a86-00-3aztatsdsw85r.kirk.replit.dev"
DEFAULT_ADMIN_IDS = "6778167412"

NGROK_AUTHTOKEN = "3D5fpwuHPV8Th3UmrOsuWyLgIbk_3KEaTRzfmYKoCEkTGrwtG"
USE_NGROK = os.environ.get("USE_NGROK", "auto").lower()

PORT = int(os.environ.get("PORT", str(DEFAULT_PORT)))
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", DEFAULT_BOT_TOKEN).strip()
PUBLIC_DOMAIN = os.environ.get("REPLIT_DOMAINS", DEFAULT_PUBLIC_DOMAIN).strip()
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _banner(msg: str):
    line = "=" * 60
    print(f"\n{line}\n{msg}\n{line}\n", flush=True)


def _start_ngrok_if_needed():
    global PUBLIC_DOMAIN
    on_replit = bool(os.environ.get("REPLIT_DOMAINS") or os.environ.get("REPL_ID"))
    if USE_NGROK == "off":
        _banner(f"⚠️ ngrok معطّل (USE_NGROK=off)\nالرابط الحالي: https://{PUBLIC_DOMAIN}")
        return
    if USE_NGROK == "auto" and on_replit:
        _banner(f"✅ تشتغل على Replit — لا حاجة لـ ngrok\nالرابط: https://{PUBLIC_DOMAIN}")
        return
    try:
        from pyngrok import ngrok, conf, installer
    except ImportError:
        _banner(
            "❌ مكتبة pyngrok غير مثبّتة!\n"
            "افتح Pip في Pydroid 3 ونزّل: pyngrok\n"
            "أو من الترمنال: pip install pyngrok\n"
            f"الرابط الحالي (لن يعمل من الجوال): https://{PUBLIC_DOMAIN}"
        )
        return

    pyngrok_cfg = conf.PyngrokConfig(
        request_timeout=600.0,
        monitor_thread=False,
        max_logs=100,
    )
    if NGROK_AUTHTOKEN:
        pyngrok_cfg.auth_token = NGROK_AUTHTOKEN
    conf.set_default(pyngrok_cfg)

    ngrok_path = conf.get_default().ngrok_path
    if not os.path.exists(ngrok_path):
        _banner(
            f"⏬ يجري تنزيل ملف ngrok التنفيذي (~20MB)...\n"
            f"المسار: {ngrok_path}\n"
            f"قد يستغرق هذا عدة دقائق على شبكة بطيئة. الرجاء الانتظار..."
        )
        download_ok = False
        for attempt in range(1, 4):
            try:
                installer.install_ngrok(ngrok_path)
                download_ok = True
                _banner(f"✅ تم تنزيل ngrok بنجاح (المحاولة {attempt}).")
                break
            except Exception as e:
                log.warning("فشل تنزيل ngrok (المحاولة %d/3): %s", attempt, e)
                if attempt < 3:
                    time.sleep(5)
        if not download_ok:
            _banner(
                "❌ فشل تنزيل ملف ngrok بعد 3 محاولات (الإنترنت بطيء).\n\n"
                "الحل اليدوي:\n"
                "  1) افتح المتصفح ونزّل من:\n"
                "     https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-arm64.tgz\n"
                "  2) فك الضغط واستخرج ملف ngrok\n"
                f"  3) ضعه في المسار التالي:\n     {ngrok_path}\n"
                "  4) أعد تشغيل البرنامج\n\n"
                "أو جرّب تشغيل البرنامج مرة أخرى عند تحسّن الإنترنت."
            )
            return

    try:
        log.info("بدء نفق ngrok على المنفذ %d ...", PORT)
        tunnel = ngrok.connect(PORT, "http", bind_tls=True)
        url = tunnel.public_url
        if url.startswith("https://"):
            PUBLIC_DOMAIN = url[len("https://"):].rstrip("/")
        elif url.startswith("http://"):
            PUBLIC_DOMAIN = url[len("http://"):].rstrip("/")
        else:
            PUBLIC_DOMAIN = url.rstrip("/")
        _banner(
            f"✅ نفق ngrok جاهز!\n"
            f"رابط اللوحة: https://{PUBLIC_DOMAIN}\n"
            f"المنفذ المحلي: {PORT}\n"
            f"افتح البوت في تيليجرام ثم اضغط زر اللوحة."
        )
    except Exception as e:
        _banner(
            f"❌ فشل تشغيل ngrok: {e}\n"
            "أسباب محتملة:\n"
            "  1) لا يوجد إنترنت كافٍ\n"
            "  2) ngrok binary لا يعمل على Android (جرّب cloudflared بدلاً منه)\n"
            "  3) رمز المصادقة غير صحيح\n"
            f"الرابط الحالي (لن يعمل من الجوال): https://{PUBLIC_DOMAIN}"
        )


def _parse_admin_ids(raw: str):
    out = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(part)
    return out


# Comma-separated Telegram user IDs that get the admin panel (stop-all, etc).
ADMIN_IDS = _parse_admin_ids(os.environ.get("ADMIN_TELEGRAM_IDS", DEFAULT_ADMIN_IDS))


def is_admin(uid: str) -> bool:
    return bool(uid) and str(uid) in ADMIN_IDS


# =============================================================================
# Telegram WebApp initData verification
# https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
# =============================================================================
def _data_check_string(init_data: str):
    pairs = parse_qsl(init_data, keep_blank_values=True)
    received_hash = None
    items = []
    for key, value in pairs:
        if key == "hash":
            received_hash = value
            continue
        items.append((key, value))
    items.sort(key=lambda kv: kv[0])
    dcs = "\n".join(f"{k}={v}" for k, v in items)
    return dcs, received_hash


def verify_init_data(init_data: str, bot_token: str) -> bool:
    if not init_data or not bot_token:
        return False
    try:
        dcs, received_hash = _data_check_string(init_data)
        if not received_hash:
            return False
        secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
        computed = hmac.new(secret_key, dcs.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(computed, received_hash)
    except Exception:
        return False


def parse_init_data(init_data: str):
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        user_raw = pairs.get("user")
        if not user_raw:
            return None
        return json.loads(user_raw)
    except Exception:
        return None


# =============================================================================
# HostingManager — process lifecycle, file storage, pip install, log capture
# =============================================================================
_FORBIDDEN_LIB_TOKENS = ("..", "/", "\\", " ", ";", "&", "|", "$", "`", "\n", "\r", "<", ">")


class HostingManager:
    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.users_dir = self.data_dir / "users"
        self.logs_dir = self.data_dir / "logs"
        self.pids_dir = self.data_dir / "pids"
        self.db_path = self.data_dir / "db.json"
        for p in (self.data_dir, self.users_dir, self.logs_dir, self.pids_dir):
            p.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # In-memory map of file_id -> Popen for processes started in this lifetime.
        # Used to send stdin lines to interactive scripts (Telethon phone/code).
        self._procs: dict = {}
        # Cache the on-disk DB in memory. We only re-read on startup; writes
        # update the cache and persist to disk atomically. This avoids hitting
        # the filesystem for every list/get call.
        self._db_cache: dict = self._read_db_from_disk()
        # Status cache: file_id -> (status, expires_at). Avoids repeated
        # `os.kill(pid, 0)` syscalls on hot paths like list_files.
        self._status_cache: dict = {}
        if not self.db_path.exists():
            self._persist_db(self._db_cache)

    # --------------------------- DB helpers ---------------------------
    def _read_db_from_disk(self) -> dict:
        try:
            with open(self.db_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "files" not in data:
                    data["files"] = {}
                if "banned" not in data:
                    data["banned"] = {}
                if "extra_admins" not in data:
                    data["extra_admins"] = []
                if "mode" not in data:
                    data["mode"] = "free"
                if "subscribed" not in data:
                    data["subscribed"] = {}
                if "points" not in data:
                    data["points"] = {}
                if "plans" not in data:
                    data["plans"] = {
                        "basic":  {"name": "الباقة الأساسية",   "points_cost": 100,  "duration_days": 30,  "description": "استضافة لمدة شهر كامل"},
                        "pro":    {"name": "الباقة الاحترافية", "points_cost": 500,  "duration_days": 90,  "description": "استضافة لمدة 3 أشهر"},
                        "vip":    {"name": "باقة VIP",          "points_cost": 1000, "duration_days": 365, "description": "استضافة سنة كاملة"},
                    }
                return data
        except Exception:
            return {
                "files": {}, "banned": {}, "extra_admins": [],
                "mode": "free", "subscribed": {}, "points": {},
                "plans": {
                    "basic":  {"name": "الباقة الأساسية",   "points_cost": 100,  "duration_days": 30,  "description": "استضافة لمدة شهر كامل"},
                    "pro":    {"name": "الباقة الاحترافية", "points_cost": 500,  "duration_days": 90,  "description": "استضافة لمدة 3 أشهر"},
                    "vip":    {"name": "باقة VIP",          "points_cost": 1000, "duration_days": 365, "description": "استضافة سنة كاملة"},
                },
            }

    def _persist_db(self, db: dict) -> None:
        tmp = self.db_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
        tmp.replace(self.db_path)

    def _load_db(self) -> dict:
        # Fast path: in-memory cache.
        return self._db_cache

    def _save_db(self, db: dict) -> None:
        # Update cache + persist.
        self._db_cache = db
        self._persist_db(db)

    # --------------------------- File CRUD ---------------------------
    def list_files(self, uid: str):
        with self._lock:
            db = self._load_db()
            out = []
            for fid, info in db["files"].items():
                if str(info.get("uid")) != str(uid):
                    continue
                meta = dict(info)
                meta["id"] = fid
                meta["status"] = self.get_status(fid)
                out.append(meta)
            out.sort(key=lambda x: x.get("created_at", 0), reverse=True)
            return out

    def get_file(self, uid: str, file_id: str):
        with self._lock:
            db = self._load_db()
            info = db["files"].get(file_id)
            if not info or str(info.get("uid")) != str(uid):
                return None
            meta = dict(info)
            meta["id"] = file_id
            meta["status"] = self.get_status(file_id)
            return meta

    def _user_dir(self, uid: str, file_id: str) -> Path:
        return self.users_dir / str(uid) / file_id

    def _safe_name(self, name: str) -> str:
        name = os.path.basename(name).strip()
        name = re.sub(r"[^A-Za-z0-9._\-]", "_", name)
        if not name:
            name = "script.py"
        return name

    def create_file(self, uid: str, name: str, code: str) -> dict:
        with self._lock:
            file_id = uuid.uuid4().hex[:12]
            safe = self._safe_name(name)
            if not safe.endswith(".py"):
                safe += ".py"
            folder = self._user_dir(uid, file_id)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / safe).write_text(code, encoding="utf-8")
            db = self._load_db()
            db["files"][file_id] = {
                "uid": str(uid),
                "name": safe,
                "main": safe,
                "path": str(folder),
                "created_at": int(time.time()),
            }
            self._save_db(db)
            meta = dict(db["files"][file_id])
            meta["id"] = file_id
            meta["status"] = "stopped"
            return meta

    def update_file(self, uid: str, file_id: str, code=None, name=None) -> dict:
        with self._lock:
            db = self._load_db()
            info = db["files"].get(file_id)
            if not info or str(info.get("uid")) != str(uid):
                raise FileNotFoundError(file_id)
            folder = Path(info["path"])
            main = info["main"]
            if name:
                new_name = self._safe_name(name)
                if not new_name.endswith(".py"):
                    new_name += ".py"
                if new_name != main:
                    src = folder / main
                    dst = folder / new_name
                    if src.exists():
                        src.rename(dst)
                    info["main"] = new_name
                    info["name"] = new_name
                    main = new_name
            if code is not None:
                (folder / main).write_text(code, encoding="utf-8")
            db["files"][file_id] = info
            self._save_db(db)
            meta = dict(info)
            meta["id"] = file_id
            meta["status"] = self.get_status(file_id)
            return meta

    def delete_file(self, uid: str, file_id: str) -> bool:
        with self._lock:
            db = self._load_db()
            info = db["files"].get(file_id)
            if not info or str(info.get("uid")) != str(uid):
                return False
            self.stop_file(uid, file_id)
            try:
                import shutil
                shutil.rmtree(info["path"], ignore_errors=True)
            except Exception:
                pass
            log_path = self.logs_dir / f"{file_id}.log"
            pid = self.pids_dir / f"{file_id}.pid"
            for p in (log_path, pid):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
            del db["files"][file_id]
            self._save_db(db)
            return True

    def read_code(self, uid: str, file_id: str) -> str:
        info = self.get_file(uid, file_id)
        if not info:
            return ""
        path = Path(info["path"]) / info["main"]
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return ""

    def upload_file(self, uid: str, filename: str, data: bytes) -> dict:
        with self._lock:
            file_id = uuid.uuid4().hex[:12]
            folder = self._user_dir(uid, file_id)
            folder.mkdir(parents=True, exist_ok=True)
            lower = filename.lower()
            main_name = None
            if lower.endswith(".zip"):
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    for member in zf.namelist():
                        member_path = os.path.normpath(member)
                        if member_path.startswith("..") or os.path.isabs(member_path):
                            continue
                        if "__MACOSX" in member_path or member_path.endswith(".DS_Store"):
                            continue
                        zf.extract(member, folder)
                main_name = self._find_main(folder)
                if not main_name:
                    raise ValueError("لم أجد ملف Python داخل الأرشيف.")
            elif lower.endswith(".py"):
                safe = self._safe_name(filename)
                (folder / safe).write_text(data.decode("utf-8", errors="replace"), encoding="utf-8")
                main_name = safe
            else:
                raise ValueError("الملف يجب أن يكون .py أو .zip")

            db = self._load_db()
            display_name = filename if lower.endswith(".zip") else main_name
            db["files"][file_id] = {
                "uid": str(uid),
                "name": display_name,
                "main": main_name,
                "path": str(folder),
                "created_at": int(time.time()),
            }
            self._save_db(db)
            meta = dict(db["files"][file_id])
            meta["id"] = file_id
            meta["status"] = "stopped"
            return meta

    def _find_main(self, folder: Path):
        candidates = list(folder.rglob("*.py"))
        candidates = [c for c in candidates if "__MACOSX" not in str(c) and "__pycache__" not in str(c)]
        if not candidates:
            return None
        priority_names = ["main.py", "app.py", "bot.py", "run.py", "start.py", "index.py"]

        def score(p: Path) -> int:
            s = 0
            if p.name.lower() in priority_names:
                s += 100
            if p.parent == folder:
                s += 50
            try:
                size = p.stat().st_size
                if 50 < size < 500_000:
                    s += min(size / 1000, 20)
            except Exception:
                pass
            return int(s)

        candidates.sort(key=score, reverse=True)
        best = candidates[0]
        return str(best.relative_to(folder))

    # --------------------------- Process control ---------------------------
    def get_status(self, file_id: str) -> str:
        # Tiny TTL cache to avoid hammering the filesystem when polling logs
        # or rendering the file list.
        now = time.monotonic()
        cached = self._status_cache.get(file_id)
        if cached and cached[1] > now:
            return cached[0]
        pid_path = self.pids_dir / f"{file_id}.pid"
        if not pid_path.exists():
            self._status_cache[file_id] = ("stopped", now + 0.5)
            return "stopped"
        try:
            pid = int(pid_path.read_text().strip())
        except Exception:
            self._status_cache[file_id] = ("stopped", now + 0.5)
            return "stopped"
        try:
            os.kill(pid, 0)
            self._status_cache[file_id] = ("running", now + 0.5)
            return "running"
        except OSError:
            try:
                pid_path.unlink()
            except FileNotFoundError:
                pass
            self._status_cache[file_id] = ("stopped", now + 0.5)
            return "stopped"

    def _invalidate_status(self, file_id: str) -> None:
        self._status_cache.pop(file_id, None)

    def start_file(self, uid: str, file_id: str) -> dict:
        with self._lock:
            info = self.get_file(uid, file_id)
            if not info:
                return {"ok": False, "error": "File not found."}
            self.stop_file(uid, file_id)
            folder = Path(info["path"])
            main = folder / info["main"]
            if not main.exists():
                return {"ok": False, "error": "Main script missing on disk."}
            log_path = self.logs_dir / f"{file_id}.log"
            pid_path = self.pids_dir / f"{file_id}.pid"
            try:
                log_path.write_text(
                    f"--- Started at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n",
                    encoding="utf-8",
                )
            except Exception:
                pass
            try:
                log_f = open(log_path, "ab", buffering=0)
                env = os.environ.copy()
                env["PYTHONUNBUFFERED"] = "1"
                env["PYTHONIOENCODING"] = "utf-8"
                proc = subprocess.Popen(
                    [sys.executable, "-u", str(main)],
                    stdin=subprocess.PIPE,
                    stdout=log_f,
                    stderr=subprocess.STDOUT,
                    cwd=str(folder),
                    env=env,
                    start_new_session=True,
                    shell=False,
                )
                pid_path.write_text(str(proc.pid))
                self._procs[file_id] = proc
                self._invalidate_status(file_id)
            except Exception as e:
                return {"ok": False, "error": f"Failed to start: {e}"}
            # Adaptive wait: poll up to ~0.6s instead of always sleeping 0.7s.
            deadline = time.monotonic() + 0.6
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
            if proc.poll() is not None:
                try:
                    log_f.close()
                except Exception:
                    pass
                self._procs.pop(file_id, None)
                logs_text = self.read_logs(file_id, tail=200)
                missing = self.detect_missing_modules(logs_text)
                try:
                    pid_path.unlink()
                except FileNotFoundError:
                    pass
                return {
                    "ok": False,
                    "error": "توقف السكربت فور التشغيل.",
                    "exit_code": proc.returncode,
                    "logs": logs_text,
                    "missing_modules": missing,
                }
            return {"ok": True, "pid": proc.pid}

    def send_input(self, uid: str, file_id: str, text: str) -> dict:
        """Send a line of text to a running script's stdin (Telethon phone/code)."""
        with self._lock:
            info = self.get_file(uid, file_id)
            if not info:
                return {"ok": False, "error": "File not found."}
            proc = self._procs.get(file_id)
            if not proc:
                return {"ok": False, "error": "السكربت ليس قيد التشغيل من هذه الجلسة."}
            if proc.poll() is not None:
                self._procs.pop(file_id, None)
                return {"ok": False, "error": "السكربت توقف."}
            if not proc.stdin or proc.stdin.closed:
                return {"ok": False, "error": "قناة الإدخال مغلقة."}
            line = (text or "").rstrip("\r\n") + "\n"
            try:
                proc.stdin.write(line.encode("utf-8"))
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                return {"ok": False, "error": f"تعذّر إرسال الإدخال: {e}"}
            try:
                with open(self.logs_dir / f"{file_id}.log", "ab") as f:
                    f.write(("> " + line).encode("utf-8"))
            except Exception:
                pass
            return {"ok": True}

    def stop_file(self, uid: str, file_id: str) -> bool:
        with self._lock:
            info = self.get_file(uid, file_id)
            if not info:
                return False
            return self._stop_by_id(file_id)

    def _stop_by_id(self, file_id: str) -> bool:
        with self._lock:
            self._procs.pop(file_id, None)
            self._invalidate_status(file_id)
            pid_path = self.pids_dir / f"{file_id}.pid"
            if not pid_path.exists():
                return True
            try:
                pid = int(pid_path.read_text().strip())
            except Exception:
                pid_path.unlink(missing_ok=True)
                return True
            killed = False
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(os.getpgid(pid), sig)
                    killed = True
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        os.kill(pid, sig)
                        killed = True
                    except OSError:
                        pass
                for _ in range(10):
                    try:
                        os.kill(pid, 0)
                    except OSError:
                        try:
                            pid_path.unlink()
                        except FileNotFoundError:
                            pass
                        try:
                            with open(self.logs_dir / f"{file_id}.log", "a", encoding="utf-8") as f:
                                f.write(f"\n--- Stopped at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
                        except Exception:
                            pass
                        return True
                    time.sleep(0.1)
            try:
                pid_path.unlink()
            except FileNotFoundError:
                pass
            return killed

    def stop_all_files(self) -> dict:
        """Admin: stop every running uploaded script across all users."""
        stopped, failed, scanned = [], [], 0
        with self._lock:
            db = self._load_db()
            for fid, info in list(db["files"].items()):
                scanned += 1
                if self.get_status(fid) != "running":
                    continue
                ok = self._stop_by_id(fid)
                entry = {
                    "id": fid,
                    "name": info.get("name", fid),
                    "uid": str(info.get("uid", "")),
                }
                if ok:
                    stopped.append(entry)
                else:
                    failed.append(entry)
        return {
            "ok": True,
            "scanned": scanned,
            "stopped_count": len(stopped),
            "failed_count": len(failed),
            "stopped": stopped,
            "failed": failed,
        }

    def admin_overview(self) -> dict:
        """Admin: counts of files / running scripts / users."""
        with self._lock:
            db = self._load_db()
            files = list(db["files"].items())
            users = {str(info.get("uid", "")) for _, info in files}
            running = sum(1 for fid, _ in files if self.get_status(fid) == "running")
            return {
                "total_files": len(files),
                "running": running,
                "users": len(users),
            }

    # --------------------------- Admin (cross-user) ---------------------------
    def admin_list_all_files(self) -> list:
        """Admin: list every file across all users with status."""
        with self._lock:
            db = self._load_db()
            out = []
            for fid, info in db["files"].items():
                meta = dict(info)
                meta["id"] = fid
                meta["status"] = self.get_status(fid)
                out.append(meta)
            out.sort(key=lambda x: (x.get("status") != "running", -(x.get("created_at") or 0)))
            return out

    def admin_get_file(self, file_id: str):
        """Admin: get any file by id, regardless of owner."""
        with self._lock:
            db = self._load_db()
            info = db["files"].get(file_id)
            if not info:
                return None
            meta = dict(info)
            meta["id"] = file_id
            meta["status"] = self.get_status(file_id)
            return meta

    def admin_read_code(self, file_id: str) -> str:
        """Admin: read any file's main script content."""
        info = self.admin_get_file(file_id)
        if not info:
            return ""
        try:
            return (Path(info["path"]) / info["main"]).read_text(encoding="utf-8")
        except Exception:
            return ""

    def admin_stop_file(self, file_id: str) -> bool:
        """Admin: stop any file by id, regardless of owner."""
        with self._lock:
            if not self.admin_get_file(file_id):
                return False
            return self._stop_by_id(file_id)

    def admin_delete_file(self, file_id: str) -> bool:
        """Admin: delete any file by id, regardless of owner."""
        with self._lock:
            db = self._load_db()
            info = db["files"].get(file_id)
            if not info:
                return False
            self._stop_by_id(file_id)
            try:
                import shutil
                shutil.rmtree(info["path"], ignore_errors=True)
            except Exception:
                pass
            log_path = self.logs_dir / f"{file_id}.log"
            pid = self.pids_dir / f"{file_id}.pid"
            for p in (log_path, pid):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
            del db["files"][file_id]
            self._save_db(db)
            return True

    def admin_start_file(self, file_id: str) -> dict:
        """Admin: start any file regardless of owner."""
        with self._lock:
            db = self._load_db()
            info = db["files"].get(file_id)
            if not info:
                return {"ok": False, "error": "File not found."}
            uid = str(info.get("uid", ""))
            return self.start_file(uid, file_id)

    def admin_list_users(self) -> list:
        """Admin: list all unique users with their file/running counts, points, subscription."""
        with self._lock:
            db = self._load_db()
            users: dict = {}
            now = int(time.time())
            for fid, info in db["files"].items():
                uid = str(info.get("uid", "unknown"))
                uname = info.get("username") or info.get("first_name") or uid
                if uid not in users:
                    exp = db.get("subscribed", {}).get(uid)
                    sub_active = exp is not None and (exp == -1 or now < exp)
                    users[uid] = {
                        "uid": uid,
                        "name": uname,
                        "files": 0,
                        "running": 0,
                        "banned": uid in db.get("banned", {}),
                        "is_admin": is_admin(uid),
                        "points": int(db.get("points", {}).get(uid, 0)),
                        "subscribed": sub_active,
                        "sub_expiry": exp,
                    }
                users[uid]["files"] += 1
                if self.get_status(fid) == "running":
                    users[uid]["running"] += 1
            return sorted(users.values(), key=lambda u: -u["files"])

    def admin_ban_user(self, uid: str, reason: str = "") -> bool:
        """Admin: ban a user (blocks their API access and stops their files)."""
        with self._lock:
            db = self._load_db()
            db.setdefault("banned", {})[str(uid)] = {
                "reason": reason,
                "at": int(time.time()),
            }
            for fid, info in db["files"].items():
                if str(info.get("uid", "")) == str(uid):
                    self._stop_by_id(fid)
            self._save_db(db)
            return True

    def admin_unban_user(self, uid: str) -> bool:
        """Admin: remove ban from a user."""
        with self._lock:
            db = self._load_db()
            banned = db.get("banned", {})
            if str(uid) not in banned:
                return False
            del banned[str(uid)]
            self._save_db(db)
            return True

    def admin_is_banned(self, uid: str) -> bool:
        with self._lock:
            db = self._load_db()
            return str(uid) in db.get("banned", {})

    def admin_delete_user_files(self, uid: str) -> int:
        """Admin: delete ALL files belonging to a user. Returns count deleted."""
        with self._lock:
            import shutil
            db = self._load_db()
            to_delete = [fid for fid, info in db["files"].items()
                         if str(info.get("uid", "")) == str(uid)]
            for fid in to_delete:
                info = db["files"][fid]
                self._stop_by_id(fid)
                try:
                    shutil.rmtree(info["path"], ignore_errors=True)
                except Exception:
                    pass
                for p in (self.logs_dir / f"{fid}.log", self.pids_dir / f"{fid}.pid"):
                    try:
                        p.unlink()
                    except FileNotFoundError:
                        pass
                del db["files"][fid]
            self._save_db(db)
            return len(to_delete)

    def admin_add_admin(self, uid: str) -> bool:
        """Admin: grant admin privileges to a user (persisted in DB)."""
        global ADMIN_IDS
        with self._lock:
            db = self._load_db()
            extras = db.get("extra_admins", [])
            if str(uid) not in extras:
                extras.append(str(uid))
            db["extra_admins"] = extras
            self._save_db(db)
            ADMIN_IDS.add(str(uid))
            return True

    def admin_remove_admin(self, uid: str) -> bool:
        """Admin: revoke admin privileges from a user."""
        global ADMIN_IDS
        with self._lock:
            db = self._load_db()
            extras = db.get("extra_admins", [])
            if str(uid) in extras:
                extras.remove(str(uid))
            db["extra_admins"] = extras
            self._save_db(db)
            ADMIN_IDS.discard(str(uid))
            return True

    def admin_system_info(self) -> dict:
        """Admin: basic system resource info."""
        info: dict = {}
        try:
            import shutil as _shutil
            total, used, free = _shutil.disk_usage(str(self.data_dir))
            info["disk_total_mb"] = round(total / 1024 / 1024)
            info["disk_used_mb"] = round(used / 1024 / 1024)
            info["disk_free_mb"] = round(free / 1024 / 1024)
        except Exception:
            pass
        try:
            with open("/proc/meminfo") as f:
                lines = {l.split(":")[0]: l.split(":")[1].strip() for l in f if ":" in l}
            def _kb(k):
                return int(lines.get(k, "0 kB").split()[0])
            total_kb = _kb("MemTotal")
            avail_kb = _kb("MemAvailable")
            info["ram_total_mb"] = round(total_kb / 1024)
            info["ram_used_mb"] = round((total_kb - avail_kb) / 1024)
            info["ram_free_mb"] = round(avail_kb / 1024)
        except Exception:
            pass
        try:
            with open("/proc/loadavg") as f:
                parts = f.read().split()
            info["load_1m"] = parts[0]
            info["load_5m"] = parts[1]
            info["load_15m"] = parts[2]
        except Exception:
            pass
        try:
            with open("/proc/uptime") as f:
                secs = float(f.read().split()[0])
            h = int(secs // 3600)
            m = int((secs % 3600) // 60)
            info["uptime"] = f"{h}h {m}m"
        except Exception:
            pass
        info["data_dir"] = str(self.data_dir)
        return info

    # --------------------------- Subscription / Mode ---------------------------
    def get_mode(self) -> str:
        """Returns current mode: 'free' or 'paid'."""
        with self._lock:
            return self._load_db().get("mode", "free")

    def set_mode(self, mode: str) -> None:
        """Set mode to 'free' or 'paid'."""
        with self._lock:
            db = self._load_db()
            db["mode"] = "paid" if mode == "paid" else "free"
            self._save_db(db)

    def subscribe_user(self, uid: str, days: int = -1) -> None:
        """Grant subscription. days=-1 = دائمة، days>0 = محدودة."""
        with self._lock:
            db = self._load_db()
            expiry = -1 if days <= 0 else int(time.time()) + days * 86400
            db.setdefault("subscribed", {})[str(uid)] = expiry
            self._save_db(db)

    def unsubscribe_user(self, uid: str) -> None:
        """Remove subscription from a user."""
        with self._lock:
            db = self._load_db()
            db.get("subscribed", {}).pop(str(uid), None)
            self._save_db(db)

    def is_subscribed(self, uid: str) -> bool:
        """Check if user has active (non-expired) subscription."""
        with self._lock:
            db = self._load_db()
            exp = db.get("subscribed", {}).get(str(uid))
            if exp is None:
                return False
            if exp == -1:
                return True
            return int(time.time()) < exp

    def get_subscription_expiry(self, uid: str):
        """Returns expiry timestamp or -1 (permanent) or None (not subscribed)."""
        with self._lock:
            return self._load_db().get("subscribed", {}).get(str(uid))

    def can_upload(self, uid: str) -> bool:
        """Returns True if user is allowed to upload (free mode OR subscribed OR admin)."""
        if is_admin(uid):
            return True
        mode = self.get_mode()
        if mode == "free":
            return True
        return self.is_subscribed(uid)

    def list_subscribed(self) -> list:
        """Return list of subscribed user IDs with expiry info."""
        with self._lock:
            db = self._load_db()
            now = int(time.time())
            result = []
            for uid, exp in db.get("subscribed", {}).items():
                active = (exp == -1) or (now < exp)
                result.append({"uid": uid, "expiry": exp, "active": active})
            return result

    # --------------------------- Points system ---------------------------
    def get_points(self, uid: str) -> int:
        with self._lock:
            return int(self._load_db().get("points", {}).get(str(uid), 0))

    def add_points(self, uid: str, amount: int) -> int:
        """Add points to user. Returns new balance."""
        with self._lock:
            db = self._load_db()
            pts = db.setdefault("points", {})
            pts[str(uid)] = int(pts.get(str(uid), 0)) + int(amount)
            self._save_db(db)
            return pts[str(uid)]

    def set_points(self, uid: str, amount: int) -> int:
        """Set user points to exact amount."""
        with self._lock:
            db = self._load_db()
            db.setdefault("points", {})[str(uid)] = int(amount)
            self._save_db(db)
            return int(amount)

    # --------------------------- Plans ---------------------------
    def get_plans(self) -> dict:
        with self._lock:
            return dict(self._load_db().get("plans", {}))

    def admin_save_plan(self, plan_id: str, name: str, points_cost: int,
                        duration_days: int, description: str = "") -> None:
        with self._lock:
            db = self._load_db()
            db.setdefault("plans", {})[plan_id] = {
                "name": name,
                "points_cost": int(points_cost),
                "duration_days": int(duration_days),
                "description": description,
            }
            self._save_db(db)

    def admin_delete_plan(self, plan_id: str) -> None:
        with self._lock:
            db = self._load_db()
            db.get("plans", {}).pop(plan_id, None)
            self._save_db(db)

    def buy_plan(self, uid: str, plan_id: str) -> dict:
        """User spends points to buy a plan/subscription."""
        with self._lock:
            db = self._load_db()
            plans = db.get("plans", {})
            if plan_id not in plans:
                raise ValueError("الباقة غير موجودة.")
            plan = plans[plan_id]
            cost = int(plan.get("points_cost", 0))
            pts = db.setdefault("points", {})
            current = int(pts.get(str(uid), 0))
            if current < cost:
                raise ValueError(f"نقاطك ({current}) غير كافية. تحتاج {cost} نقطة.")
            # Deduct points
            pts[str(uid)] = current - cost
            # Add subscription (extend if already subscribed)
            days = int(plan.get("duration_days", 30))
            now = int(time.time())
            existing = db.get("subscribed", {}).get(str(uid))
            if existing and existing != -1 and existing > now:
                # Extend existing
                new_expiry = existing + days * 86400
            else:
                new_expiry = now + days * 86400
            db.setdefault("subscribed", {})[str(uid)] = new_expiry
            self._save_db(db)
            return {
                "new_points": pts[str(uid)],
                "expiry": new_expiry,
                "plan_name": plan["name"],
            }

    def resume_all(self) -> int:
        """Restart any scripts that were marked running before the server restarted."""
        count = 0
        with self._lock:
            db = self._load_db()
            for fid, info in list(db["files"].items()):
                pid_path = self.pids_dir / f"{fid}.pid"
                if not pid_path.exists():
                    continue
                try:
                    pid = int(pid_path.read_text().strip())
                    os.kill(pid, 0)
                    continue  # still alive
                except Exception:
                    try:
                        pid_path.unlink()
                    except FileNotFoundError:
                        pass
                res = self.start_file(info["uid"], fid)
                if res.get("ok"):
                    count += 1
        return count

    # --------------------------- Logs ---------------------------
    def read_logs(self, file_id: str, tail: int = 400) -> str:
        path = self.logs_dir / f"{file_id}.log"
        if not path.exists():
            return ""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            return "".join(lines[-tail:])
        except Exception as e:
            return f"<could not read logs: {e}>"

    # --------------------------- Pip install ---------------------------
    @staticmethod
    def _safe_lib(name: str) -> bool:
        if not name or len(name) < 1 or len(name) > 80:
            return False
        for tok in _FORBIDDEN_LIB_TOKENS:
            if tok in name:
                return False
        if not re.fullmatch(r"[A-Za-z0-9_.\-+\[\]=<>!~]+", name):
            return False
        return True

    def install_libraries(self, libraries, timeout: int = 240) -> dict:
        clean, rejected = [], []
        for lib in libraries:
            lib = (lib or "").strip()
            if self._safe_lib(lib):
                clean.append(lib)
            elif lib:
                rejected.append(lib)
        if not clean:
            return {
                "success": False,
                "message": "لم يتم توفير أسماء مكتبات صالحة.",
                "installed": [], "failed": rejected,
                "stderr": "", "stdout": "",
            }
        cmd = [
            sys.executable, "-m", "pip", "install",
            "--no-input",
            "--disable-pip-version-check",
            "--no-warn-script-location",
            "--timeout", "60",
            "--retries", "2",
            *clean,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=False)
            success = proc.returncode == 0
            return {
                "success": success,
                "message": (
                    f"✅ تم تثبيت {len(clean)} مكتبة بنجاح" if success
                    else f"❌ فشل تثبيت بعض المكتبات (رمز الخطأ: {proc.returncode})"
                ),
                "installed": clean if success else [],
                "failed": [] if success else clean,
                "stdout": (proc.stdout or "")[-4000:],
                "stderr": (proc.stderr or "")[-4000:],
                "rejected": rejected,
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "message": f"⏰ انتهت مهلة التثبيت ({timeout} ثانية)",
                "installed": [], "failed": clean,
                "stdout": "", "stderr": "", "rejected": rejected,
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"❌ خطأ غير متوقع: {e}",
                "installed": [], "failed": clean,
                "stdout": "", "stderr": "", "rejected": rejected,
            }

    @staticmethod
    def detect_missing_modules(logs_text: str):
        if not logs_text:
            return []
        modules = set()
        for m in re.finditer(r"No module named ['\"]([A-Za-z0-9_\-]+)", logs_text):
            modules.add(m.group(1))
        for m in re.finditer(r"cannot import name ['\"][^'\"]+['\"] from ['\"]([A-Za-z0-9_\-]+)", logs_text):
            modules.add(m.group(1))
        stdlib = {"os", "sys", "re", "json", "time", "datetime", "math", "random",
                  "subprocess", "threading", "asyncio", "typing", "pathlib",
                  "collections", "itertools", "functools", "hashlib", "hmac",
                  "urllib", "socket", "logging", "io", "uuid", "shutil",
                  "tempfile", "argparse", "string", "csv", "sqlite3"}
        return sorted([m for m in modules if m.lower() not in stdlib])


# =============================================================================
# Embedded front-end assets (HTML / CSS / JS)
# =============================================================================
EMBEDDED_HTML = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <title>Servixa Host</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <link rel="stylesheet" href="/static/app.css?v=7" />
</head>
<body>
  <header class="topbar">
    <div class="brand">
      <span class="logo">🐍</span>
      <div>
        <div class="brand-title">Servixa Host</div>
        <div class="brand-sub">لوحة استضافة بايثون</div>
      </div>
    </div>
    <div class="user" id="userBox">…</div>
  </header>

  <nav class="tabs">
    <button class="tab active" data-tab="new">＋ إنشاء استضافة</button>
    <button class="tab" data-tab="files">📁 ملفاتي <span class="count" id="filesCount" hidden>0</span></button>
    <button class="tab" data-tab="install">📦 المكتبات</button>
    <button class="tab" data-tab="admin" id="adminTab" hidden>👑 المشرف</button>
  </nav>

  <!-- User status bar (points + subscription) -->
  <div id="userStatusBar" hidden style="background:var(--bg2);border-bottom:1px solid var(--border);padding:8px 16px;display:flex;align-items:center;gap:12px;font-size:13px;flex-wrap:wrap">
    <span>💎 نقاطي: <b id="userPointsBadge">—</b></span>
    <span id="userSubBadge" class="status-pill" style="font-size:12px">⏳</span>
    <span id="userModeBadge" style="color:var(--fg2)"></span>
  </div>

  <!-- Paywall overlay (shown when mode=paid and not subscribed) -->
  <div id="paywallBanner" hidden style="margin:16px;background:var(--bg2);border:2px solid var(--accent);border-radius:14px;padding:20px;text-align:center">
    <div style="font-size:40px;margin-bottom:8px">🔒</div>
    <h2 style="margin:0 0 6px">الخدمة مدفوعة</h2>
    <p style="color:var(--fg2);margin:0 0 16px">اشترِ باقة بنقاطك لتتمكن من رفع وتشغيل الملفات.</p>
    <div id="paywallPlans" style="display:grid;gap:10px;max-width:480px;margin:0 auto 16px"></div>
    <p style="color:var(--fg2);font-size:12px">💎 نقاطك الحالية: <b id="paywallPoints">0</b> — تواصل مع المشرف للحصول على نقاط.</p>
  </div>

  <main>
    <section id="tab-new" class="tab-pane active">
      <h2>إنشاء استضافة جديدة</h2>
      <p class="hint">الصق كود البايثون أو ارفع ملفًا، ثم اضغط <b>تشغيل</b>.</p>

      <label class="field">
        <span>اسم الملف</span>
        <input id="newName" type="text" placeholder="main.py" value="main.py" />
      </label>

      <label class="field">
        <span>المكتبات المطلوبة (اختياري — مفصولة بفاصلة)</span>
        <input id="newLibs" type="text" placeholder="requests, telethon" />
      </label>

      <label class="field">
        <span>الكود</span>
        <textarea id="newCode" spellcheck="false" placeholder="print('Hello from Servixa Host!')"></textarea>
      </label>

      <div class="actions">
        <button class="btn primary big" id="saveAndRunBtn">▶️ حفظ وتشغيل</button>
        <button class="btn big" id="saveOnlyBtn">💾 حفظ فقط</button>
      </div>

      <div class="divider"><span>أو ارفع ملفًا جاهزًا</span></div>

      <div class="upload">
        <input type="file" id="fileInput" accept=".py,.zip" hidden />
        <button class="btn block" id="pickBtn">⬆️ اختر ملف <code>.py</code> أو <code>.zip</code></button>
        <div id="uploadStatus" class="hint"></div>
      </div>
    </section>

    <section id="tab-files" class="tab-pane">
      <div class="row-between">
        <h2>الملفات المرفوعة</h2>
        <button class="btn ghost" id="refreshBtn">🔄 تحديث</button>
      </div>
      <div id="fileList" class="card-list">
        <div class="empty">جارٍ التحميل…</div>
      </div>
    </section>

    <section id="tab-install" class="tab-pane">
      <h2>تثبيت مكتبات</h2>
      <p class="hint">اكتب أسماء المكتبات مفصولة بمسافة أو فاصلة. مثال: <code>requests flask</code></p>

      <label class="field">
        <span>المكتبات</span>
        <input id="libsInput" type="text" placeholder="requests, beautifulsoup4" />
      </label>

      <div class="actions">
        <button class="btn primary" id="installBtn">📦 تثبيت</button>
      </div>

      <pre id="installOutput" class="terminal" hidden></pre>
    </section>

    <section id="tab-admin" class="tab-pane">
      <h2>👑 لوحة المشرف</h2>

      <div class="admin-stats">
        <div class="stat"><div class="stat-num" id="adminTotal">—</div><div class="stat-lbl">ملفات</div></div>
        <div class="stat"><div class="stat-num" id="adminRunning">—</div><div class="stat-lbl">شغّالة</div></div>
        <div class="stat"><div class="stat-num" id="adminUsers">—</div><div class="stat-lbl">مستخدمين</div></div>
      </div>

      <div class="admin-subtabs">
        <button class="admin-stab active" data-stab="files">📂 الملفات</button>
        <button class="admin-stab" data-stab="users">👥 المستخدمين</button>
        <button class="admin-stab" data-stab="system">📊 النظام</button>
        <button class="admin-stab" data-stab="settings">⚙️ الإعدادات</button>
      </div>

      <pre id="adminOutput" class="terminal" hidden></pre>

      <!-- FILES SUB-TAB -->
      <div id="astab-files" class="admin-stab-pane active">
        <div class="admin-files-head">
          <input id="adminSearch" type="text" placeholder="🔍 ابحث بالاسم أو UID…" />
          <button class="btn ghost" id="adminRefreshBtn">🔄</button>
          <button class="btn danger" id="stopAllBtn">⏹️ إيقاف الكل</button>
        </div>
        <div id="adminFileList" class="file-list">
          <div class="empty">اضغط 🔄 لعرض الملفات</div>
        </div>
      </div>

      <!-- USERS SUB-TAB -->
      <div id="astab-users" class="admin-stab-pane" hidden>
        <div class="admin-files-head">
          <input id="adminUserSearch" type="text" placeholder="🔍 ابحث بالاسم أو ID…" />
          <button class="btn ghost" id="adminUsersRefreshBtn">🔄</button>
        </div>
        <div class="actions" style="margin-bottom:8px">
          <button class="btn primary" id="adminBroadcastBtn">📢 رسالة جماعية</button>
        </div>
        <div id="adminUserList" class="file-list">
          <div class="empty">اضغط 🔄 لعرض المستخدمين</div>
        </div>
      </div>

      <!-- SYSTEM SUB-TAB -->
      <div id="astab-system" class="admin-stab-pane" hidden>
        <div class="actions"><button class="btn ghost" id="adminSysRefreshBtn">🔄 تحديث</button></div>
        <div id="adminSysInfo" class="sys-info-grid">
          <div class="empty">اضغط 🔄 لعرض معلومات النظام</div>
        </div>
      </div>

      <!-- SETTINGS SUB-TAB -->
      <div id="astab-settings" class="admin-stab-pane" hidden>

        <!-- Mode toggle -->
        <div class="settings-card">
          <h3>🔒 وضع الخدمة</h3>
          <p style="margin:0 0 10px;font-size:13px;color:var(--fg2)">في الوضع المدفوع، المستخدمون يحتاجون اشتراكاً أو نقاطاً لرفع الملفات.</p>
          <div class="mode-toggle-row">
            <span id="modeLabel" class="mode-badge free">مجاني ✅</span>
            <label class="toggle-switch">
              <input type="checkbox" id="modeToggle" onchange="adminToggleMode(this.checked)">
              <span class="toggle-slider"></span>
            </label>
            <span style="font-size:13px;color:var(--fg2)">مدفوع 💰</span>
          </div>
        </div>

        <!-- Plans management -->
        <div class="settings-card">
          <h3>📦 إدارة الباقات</h3>
          <div id="adminPlansList" style="margin-bottom:10px"><div class="empty">⏳ جارٍ التحميل…</div></div>
          <details style="margin-top:8px">
            <summary style="cursor:pointer;font-weight:600;color:var(--accent)">➕ إضافة / تعديل باقة</summary>
            <div style="margin-top:10px;display:grid;gap:8px">
              <input id="planIdInput"   type="text"   placeholder="plan_id (مثل: gold)" style="padding:7px;border-radius:8px;border:1px solid var(--border);background:var(--bg2);color:var(--fg)"/>
              <input id="planNameInput" type="text"   placeholder="اسم الباقة (مثل: الباقة الذهبية)" style="padding:7px;border-radius:8px;border:1px solid var(--border);background:var(--bg2);color:var(--fg)"/>
              <input id="planCostInput" type="number" placeholder="النقاط المطلوبة" min="1" style="padding:7px;border-radius:8px;border:1px solid var(--border);background:var(--bg2);color:var(--fg)"/>
              <input id="planDaysInput" type="number" placeholder="عدد أيام الاشتراك" min="1" style="padding:7px;border-radius:8px;border:1px solid var(--border);background:var(--bg2);color:var(--fg)"/>
              <input id="planDescInput" type="text"   placeholder="وصف اختياري" style="padding:7px;border-radius:8px;border:1px solid var(--border);background:var(--bg2);color:var(--fg)"/>
              <button class="btn primary" onclick="adminSavePlan()">💾 حفظ الباقة</button>
            </div>
          </details>
        </div>

        <!-- Admin management -->
        <div class="settings-card">
          <h3>👑 إدارة المشرفين</h3>
          <div class="inline-form">
            <input id="addAdminInput" type="text" placeholder="➕ أضف مشرفاً (Telegram ID)" />
            <button class="btn primary" id="addAdminBtn">إضافة</button>
          </div>
          <div class="inline-form" style="margin-top:8px">
            <input id="removeAdminInput" type="text" placeholder="➖ أزل مشرفاً (Telegram ID)" />
            <button class="btn danger" id="removeAdminBtn">إزالة</button>
          </div>
        </div>
      </div>
    </section>

    <section id="adminDetailPanel" class="detail" hidden>
      <div class="detail-head">
        <button class="btn ghost" id="adminCloseDetail">⟵ رجوع</button>
        <div class="detail-title" id="adminDetailTitle">—</div>
        <span class="status-pill" id="adminDetailStatus">stopped</span>
      </div>

      <div class="detail-meta" id="adminDetailMeta"></div>

      <div class="detail-actions">
        <button class="btn primary" id="adminStartBtn">▶️ تشغيل</button>
        <button class="btn warn" id="adminStopBtn">⏹️ إيقاف</button>
        <button class="btn danger" id="adminDeleteBtn">🗑️ حذف</button>
        <button class="btn ghost" id="adminRefreshLogsBtn">🔄 سجلات</button>
      </div>

      <h3>📜 الكود</h3>
      <pre id="adminCode" class="terminal"></pre>

      <h3>📋 السجلات</h3>
      <pre id="adminLogs" class="terminal"></pre>
    </section>

    <section id="adminUserDetailPanel" class="detail" hidden>
      <div class="detail-head">
        <button class="btn ghost" id="adminUserCloseDetail">⟵ رجوع</button>
        <div class="detail-title" id="adminUserDetailTitle">—</div>
        <span class="status-pill" id="adminUserBadge">—</span>
      </div>
      <div class="detail-meta" id="adminUserDetailMeta"></div>
      <div class="detail-actions" id="adminUserActions"></div>

      <!-- Subscription & Points actions -->
      <div class="settings-card" style="margin-top:12px">
        <h3>🎟️ الاشتراك والنقاط</h3>
        <div id="adminUserSubInfo" style="margin-bottom:10px;font-size:13px;color:var(--fg2)"></div>
        <div style="display:grid;gap:8px">
          <div class="inline-form">
            <button class="btn primary" id="adminSubPermBtn">🔑 اشتراك دائم</button>
            <input id="adminSubDaysInput" type="number" min="1" placeholder="أيام…" style="width:80px;padding:6px;border-radius:8px;border:1px solid var(--border);background:var(--bg2);color:var(--fg)"/>
            <button class="btn ghost" id="adminSubDaysBtn">📅 اشتراك مؤقت</button>
            <button class="btn danger" id="adminUnsubBtn">❌ إلغاء اشتراك</button>
          </div>
          <div class="inline-form">
            <input id="adminPointsInput" type="number" min="1" placeholder="نقاط (سالبة للخصم)" style="flex:1;padding:6px;border-radius:8px;border:1px solid var(--border);background:var(--bg2);color:var(--fg)"/>
            <button class="btn primary" id="adminAddPointsBtn">💎 إرسال نقاط</button>
          </div>
        </div>
      </div>

      <!-- Send message -->
      <div class="settings-card" style="margin-top:8px">
        <h3>📨 إرسال رسالة</h3>
        <textarea id="adminMsgText" rows="3" placeholder="اكتب الرسالة هنا…" style="width:100%;box-sizing:border-box;padding:8px;border-radius:8px;border:1px solid var(--border);background:var(--bg2);color:var(--fg);resize:vertical"></textarea>
        <div class="actions" style="margin-top:6px">
          <button class="btn primary" id="adminSendMsgBtn">📨 إرسال</button>
        </div>
      </div>
    </section>

    <section id="detailPanel" class="detail" hidden>
      <div class="detail-head">
        <button class="btn ghost" id="closeDetail">⟵ رجوع</button>
        <div class="detail-title" id="detailTitle">—</div>
        <span class="status-pill" id="detailStatus">stopped</span>
      </div>

      <div class="detail-actions">
        <button class="btn primary" id="runBtn">▶️ تشغيل</button>
        <button class="btn warn" id="stopBtn">⏹️ إيقاف</button>
        <button class="btn" id="saveBtn">💾 حفظ</button>
        <button class="btn danger" id="deleteBtn">🗑️ حذف</button>
      </div>

      <div class="inline-install">
        <span class="inline-install-label">📦 تثبيت مكتبات:</span>
        <input id="detailLibsInput" type="text" placeholder="requests, telethon" />
        <button class="btn primary small" id="detailInstallBtn">تثبيت</button>
      </div>
      <pre id="detailInstallOutput" class="terminal" hidden></pre>

      <div class="editor-wrap">
        <textarea id="editor" spellcheck="false"></textarea>
        <div class="save-hint" id="saveHint" hidden></div>
      </div>

      <div class="console-wrap">
        <div class="console-head">
          <h3>🖥️ وحدة التحكم — مخرجات السكربت</h3>
          <div>
            <label class="check"><input type="checkbox" id="autoRefresh" checked /> تحديث تلقائي</label>
            <button class="btn ghost small" id="refreshLogsBtn">🔄</button>
          </div>
        </div>
        <div id="runStatusBanner" class="run-status" hidden></div>
        <pre id="logsBox" class="terminal" tabindex="0"></pre>

        <form id="stdinForm" class="stdin-bar" hidden>
          <span class="stdin-prompt">⌨️</span>
          <input id="stdinInput" type="text" autocomplete="off"
                 placeholder="اكتب الإجابة (رقم الهاتف، الكود، …) ثم اضغط إرسال" />
          <button type="submit" class="btn primary small" id="stdinSendBtn">📨 إرسال</button>
        </form>

        <div id="installSuggest" class="suggest" hidden>
          <div class="suggest-title">⚠️ مكتبات مفقودة:</div>
          <div id="missingList"></div>
          <button class="btn primary block" id="installMissingBtn">📦 تثبيت المكتبات المفقودة وإعادة التشغيل</button>
        </div>
      </div>
    </section>
  </main>

  <div id="toast" class="toast" hidden></div>

  <script src="/static/app.js?v=7"></script>
</body>
</html>
"""

EMBEDDED_CSS = r""":root {
  --bg: #0f1117;
  --bg-2: #161922;
  --bg-3: #1d2230;
  --border: #2a3142;
  --text: #e8ecf3;
  --muted: #8a93a8;
  --accent: #00d4a6;
  --accent-2: #2bb8e6;
  --warn: #f7a93b;
  --danger: #ef4757;
  --ok: #4ade80;
  --shadow: 0 4px 20px rgba(0, 0, 0, 0.35);
  --radius: 12px;
  --mono: ui-monospace, SFMono-Regular, "JetBrains Mono", Menlo, Consolas, monospace;
  --font: -apple-system, BlinkMacSystemFont, "Segoe UI", "Cairo", system-ui, sans-serif;
}

* { box-sizing: border-box; }

[hidden] { display: none !important; }

html, body {
  margin: 0;
  padding: 0;
  background: var(--bg);
  color: var(--text);
  font-family: var(--font);
  min-height: 100dvh;
  -webkit-tap-highlight-color: transparent;
}

body { padding-bottom: env(safe-area-inset-bottom, 12px); }

a { color: var(--accent-2); }

code {
  background: rgba(255,255,255,0.06);
  padding: 1px 5px;
  border-radius: 4px;
  font-family: var(--mono);
  font-size: 0.9em;
}

.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 16px;
  background: linear-gradient(180deg, var(--bg-2), transparent);
  border-bottom: 1px solid var(--border);
  position: sticky;
  top: 0;
  z-index: 5;
  backdrop-filter: blur(8px);
}
.brand { display: flex; gap: 10px; align-items: center; }
.logo {
  font-size: 28px;
  background: linear-gradient(135deg, var(--accent), var(--accent-2));
  -webkit-background-clip: text;
  background-clip: text;
}
.brand-title { font-weight: 700; font-size: 16px; }
.brand-sub { color: var(--muted); font-size: 12px; }
.user {
  font-size: 13px;
  color: var(--muted);
  text-align: end;
  max-width: 50%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tabs {
  display: flex;
  gap: 6px;
  padding: 10px 12px;
  overflow-x: auto;
  border-bottom: 1px solid var(--border);
  background: var(--bg);
  position: sticky;
  top: 56px;
  z-index: 4;
}
.tab {
  background: var(--bg-2);
  color: var(--text);
  border: 1px solid var(--border);
  padding: 8px 14px;
  border-radius: 999px;
  font-size: 13px;
  cursor: pointer;
  white-space: nowrap;
  transition: 0.15s;
}
.tab.active {
  background: var(--accent);
  color: #0a1410;
  border-color: var(--accent);
  font-weight: 700;
}

main { padding: 14px; max-width: 720px; margin: 0 auto; }

.tab-pane { display: none; }
.tab-pane.active { display: block; }

h2 { margin: 4px 0 12px; font-size: 18px; }
h3 { margin: 14px 0 8px; font-size: 15px; color: var(--muted); }
.row-between { display: flex; justify-content: space-between; align-items: center; }

.field { display: block; margin-bottom: 12px; }
.field > span {
  display: block;
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 6px;
}
.field input, .field textarea {
  width: 100%;
  padding: 12px;
  background: var(--bg-2);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-family: var(--font);
  font-size: 14px;
  outline: none;
  transition: 0.15s;
}
.field input:focus, .field textarea:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 2px rgba(0, 212, 166, 0.2);
}
.field textarea {
  font-family: var(--mono);
  font-size: 13px;
  min-height: 200px;
  resize: vertical;
  line-height: 1.5;
  white-space: pre;
  direction: ltr;
  text-align: left;
}

.btn {
  background: var(--bg-3);
  color: var(--text);
  border: 1px solid var(--border);
  padding: 10px 14px;
  border-radius: var(--radius);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: 0.15s;
  font-family: inherit;
}
.btn:hover { filter: brightness(1.1); }
.btn:active { transform: translateY(1px); }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.btn.primary { background: var(--accent); color: #0a1410; border-color: var(--accent); }
.btn.warn    { background: var(--warn); color: #2a1a00; border-color: var(--warn); }
.btn.danger  { background: var(--danger); color: #fff; border-color: var(--danger); }
.btn.ghost   { background: transparent; }
.btn.small   { padding: 6px 10px; font-size: 12px; }
.btn.block   { display: block; width: 100%; text-align: center; }
.btn.big     { font-size: 15px; padding: 12px 18px; }

.actions { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }

.divider {
  display: flex;
  align-items: center;
  text-align: center;
  margin: 22px 0 14px;
  color: var(--muted);
  font-size: 12px;
}
.divider::before, .divider::after {
  content: '';
  flex: 1;
  height: 1px;
  background: var(--border);
}
.divider span { padding: 0 10px; }

.upload .hint { margin-top: 10px; font-size: 12px; color: var(--muted); }
.hint { color: var(--muted); font-size: 12px; }

.card-list { display: flex; flex-direction: column; gap: 10px; }
.empty { padding: 30px; text-align: center; color: var(--muted); }

.file-card {
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 14px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  cursor: pointer;
  transition: 0.15s;
}
.file-card:hover { border-color: var(--accent); }
.file-name { font-weight: 600; font-size: 14px; word-break: break-all; }
.file-meta { color: var(--muted); font-size: 11px; margin-top: 4px; }

.status-pill {
  font-size: 11px;
  font-weight: 700;
  padding: 4px 10px;
  border-radius: 999px;
  text-transform: uppercase;
}
.status-running { background: rgba(74, 222, 128, 0.15); color: var(--ok); border: 1px solid rgba(74, 222, 128, 0.4); }
.status-stopped { background: rgba(138, 147, 168, 0.12); color: var(--muted); border: 1px solid var(--border); }

.detail {
  position: fixed;
  inset: 0;
  background: var(--bg);
  z-index: 10;
  display: flex;
  flex-direction: column;
  overflow-y: auto;
}
.detail-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-2);
  position: sticky;
  top: 0;
  z-index: 2;
}
.detail-title {
  flex: 1;
  font-weight: 700;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  direction: ltr;
  text-align: start;
}
.detail-actions {
  display: flex;
  gap: 6px;
  padding: 10px 14px;
  flex-wrap: wrap;
  border-bottom: 1px solid var(--border);
}
.inline-install {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 14px 10px;
  flex-wrap: wrap;
}
.inline-install-label {
  font-size: 12px;
  color: var(--muted);
  font-weight: 600;
}
.inline-install input {
  flex: 1;
  min-width: 140px;
  padding: 8px 12px;
  background: var(--bg-2);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-family: var(--mono);
  font-size: 13px;
  outline: none;
  direction: ltr;
  text-align: left;
}
.inline-install input:focus { border-color: var(--accent); }
#detailInstallOutput { margin: 0 14px 10px; max-height: 200px; }

.editor-wrap { padding: 10px 14px; }
#editor {
  width: 100%;
  min-height: 260px;
  background: var(--bg-2);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  font-family: var(--mono);
  font-size: 13px;
  padding: 12px;
  white-space: pre;
  direction: ltr;
  text-align: left;
  resize: vertical;
  outline: none;
}
.check { font-size: 12px; color: var(--muted); display: inline-flex; align-items: center; gap: 4px; }

.terminal {
  margin: 8px 14px 14px;
  background: #0a0d14;
  color: #d4d8e0;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 12px;
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.55;
  max-height: 360px;
  overflow: auto;
  direction: ltr;
  text-align: left;
  white-space: pre-wrap;
  word-break: break-word;
}
.terminal:empty::before {
  content: '(لا توجد سجلات بعد — اضغط تشغيل)';
  color: var(--muted);
  font-style: italic;
}

.suggest {
  margin: 0 14px 16px;
  background: rgba(247, 169, 59, 0.08);
  border: 1px solid rgba(247, 169, 59, 0.4);
  border-radius: var(--radius);
  padding: 12px;
}
.suggest-title { font-weight: 700; color: var(--warn); margin-bottom: 6px; }
#missingList {
  font-family: var(--mono);
  font-size: 13px;
  margin-bottom: 10px;
  word-break: break-all;
}

.toast {
  position: fixed;
  bottom: 24px;
  left: 50%;
  transform: translateX(-50%);
  background: var(--bg-3);
  color: var(--text);
  padding: 10px 18px;
  border-radius: 10px;
  font-size: 13px;
  border: 1px solid var(--border);
  box-shadow: var(--shadow);
  z-index: 100;
  max-width: 90%;
  text-align: center;
}
.toast.error { border-color: var(--danger); }
.toast.ok    { border-color: var(--ok); }

.tab .count {
  display: inline-block;
  background: var(--accent);
  color: #003;
  font-weight: 700;
  font-size: 11px;
  padding: 1px 7px;
  border-radius: 999px;
  margin-inline-start: 6px;
}

.console-wrap {
  border-top: 1px solid var(--border);
  background: #0b0d14;
  padding: 12px 14px 16px;
}
.console-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
  flex-wrap: wrap;
  gap: 8px;
}
.console-head h3 { margin: 0; font-size: 14px; color: var(--accent); }

.run-status {
  padding: 10px 14px;
  border-radius: 10px;
  margin-bottom: 10px;
  font-weight: 700;
  font-size: 14px;
  border: 1px solid var(--border);
}
.run-status.ok   { background: rgba(74, 222, 128, 0.10); border-color: rgba(74, 222, 128, 0.5); color: var(--ok); }
.run-status.err  { background: rgba(239, 71, 87, 0.10);  border-color: rgba(239, 71, 87, 0.5);  color: var(--danger); }
.run-status.info { background: rgba(43, 184, 230, 0.10); border-color: rgba(43, 184, 230, 0.5); color: var(--accent-2); }

.save-hint {
  margin-top: 6px;
  padding: 6px 10px;
  border-radius: 8px;
  font-size: 12px;
  display: inline-block;
}
.save-hint.ok  { background: rgba(74, 222, 128, 0.12); color: var(--ok); }
.save-hint.err { background: rgba(239, 71, 87, 0.12);  color: var(--danger); }

.stdin-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
  background: var(--bg-2);
  border: 1px solid var(--accent);
  border-radius: var(--radius);
  padding: 8px;
  box-shadow: 0 0 0 3px rgba(0, 212, 166, 0.12);
}
.stdin-prompt { font-size: 18px; padding: 0 4px; }
#stdinInput {
  flex: 1;
  min-width: 0;
  padding: 10px 12px;
  background: var(--bg);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 8px;
  font-family: var(--mono);
  font-size: 14px;
  outline: none;
  direction: ltr;
  text-align: left;
}
#stdinInput:focus { border-color: var(--accent); }

.admin-stats {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
  margin: 12px 0 16px;
}
.admin-stats .stat {
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 14px 10px;
  text-align: center;
}
.stat-num {
  font-size: 22px;
  font-weight: 800;
  color: var(--accent);
  font-family: var(--mono);
}
.admin-subtabs {
  display: flex;
  gap: 6px;
  margin: 12px 0 10px;
  flex-wrap: wrap;
}
.admin-stab {
  padding: 7px 14px;
  border-radius: 20px;
  border: 1px solid var(--border);
  background: var(--bg-2);
  color: var(--fg);
  font-size: 13px;
  cursor: pointer;
}
.admin-stab.active {
  background: var(--accent);
  color: #fff;
  border-color: var(--accent);
}
.admin-stab-pane { display: block; }
.admin-stab-pane[hidden] { display: none; }
.admin-files-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin: 10px 0 8px;
  flex-wrap: wrap;
}
.admin-files-head h3 { margin: 0; }
#adminSearch, #adminUserSearch {
  flex: 1;
  min-width: 140px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-2);
  color: var(--fg);
  font-size: 14px;
}
#adminSearch:focus, #adminUserSearch:focus { border-color: var(--accent); outline: none; }
.sys-info-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  margin-top: 12px;
}
.sys-card {
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 14px;
}
.sys-card-title { font-size: 12px; color: var(--fg2); margin-bottom: 6px; }
.sys-card-val { font-size: 20px; font-weight: 800; color: var(--accent); font-family: var(--mono); }
.sys-card-sub { font-size: 11px; color: var(--fg2); margin-top: 2px; }
.settings-card {
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 14px;
  margin-bottom: 12px;
}
.settings-card h3 { margin: 0 0 10px; font-size: 15px; }
.inline-form {
  display: flex;
  gap: 8px;
  align-items: center;
}
.inline-form input {
  flex: 1;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg);
  color: var(--fg);
  font-size: 14px;
}
.badge-banned { background: #e53935; color: #fff; padding: 2px 8px; border-radius: 10px; font-size: 11px; }
.badge-admin { background: var(--accent); color: #fff; padding: 2px 8px; border-radius: 10px; font-size: 11px; }
.badge-sub { background: #22c55e; color: #fff; padding: 2px 8px; border-radius: 10px; font-size: 11px; }
.badge-pts { background: #f59e0b; color: #fff; padding: 2px 8px; border-radius: 10px; font-size: 11px; }
/* Mode toggle */
.mode-toggle-row { display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
.mode-badge { padding:4px 12px; border-radius:20px; font-size:13px; font-weight:700; }
.mode-badge.free { background:#22c55e22; color:#22c55e; border:1px solid #22c55e55; }
.mode-badge.paid { background:#f59e0b22; color:#f59e0b; border:1px solid #f59e0b55; }
.toggle-switch { position:relative; display:inline-block; width:48px; height:26px; }
.toggle-switch input { opacity:0; width:0; height:0; }
.toggle-slider { position:absolute; cursor:pointer; inset:0; background:var(--border); border-radius:26px; transition:.3s; }
.toggle-slider:before { content:""; position:absolute; width:20px; height:20px; left:3px; top:3px; background:#fff; border-radius:50%; transition:.3s; }
input:checked + .toggle-slider { background:var(--accent); }
input:checked + .toggle-slider:before { transform:translateX(22px); }
/* Plan cards (admin) */
.plan-card-admin { display:flex; align-items:center; gap:10px; padding:10px 12px; background:var(--bg); border:1px solid var(--border); border-radius:10px; }
.plan-card-admin .plan-info { flex:1; }
.plan-card-admin .plan-name { font-weight:700; font-size:14px; }
.plan-card-admin .plan-meta { font-size:12px; color:var(--fg2); margin-top:2px; }
/* Plan cards (user) */
.plan-card-user { background:var(--bg); border:1px solid var(--border); border-radius:12px; padding:14px 16px; display:flex; flex-direction:column; gap:6px; text-align:right; }
.plan-card-user .plan-name { font-weight:800; font-size:16px; }
.plan-card-user .plan-desc { font-size:12px; color:var(--fg2); }
.plan-card-user .plan-cost { font-size:14px; color:var(--accent); font-weight:700; }
.detail-meta {
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 10px 12px;
  margin: 10px 0;
  font-size: 13px;
  color: var(--muted);
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.detail-meta code {
  background: var(--bg);
  padding: 1px 6px;
  border-radius: 4px;
  color: var(--text);
}
.stat-lbl {
  font-size: 11px;
  color: var(--muted);
  margin-top: 4px;
}
.btn.danger {
  background: var(--danger);
  color: #fff;
  border-color: var(--danger);
}
.btn.danger:hover { filter: brightness(1.08); }
"""

EMBEDDED_JS = r"""/* Servixa Host — Mini App client */
(() => {
  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  if (tg) {
    tg.ready();
    tg.expand();
    try { tg.setHeaderColor && tg.setHeaderColor("#0f1117"); } catch (_) {}
    try { tg.setBackgroundColor && tg.setBackgroundColor("#0f1117"); } catch (_) {}
  }

  const initData = tg ? tg.initData : "";
  const tgUser = tg && tg.initDataUnsafe ? tg.initDataUnsafe.user : null;

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => document.querySelectorAll(s);

  function toast(msg, type = "") {
    const el = $("#toast");
    if (!el) return;
    el.textContent = msg;
    el.className = "toast " + type;
    el.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { el.hidden = true; }, 2800);
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  async function api(path, opts = {}) {
    const headers = Object.assign({}, opts.headers || {}, {
      "X-Init-Data": initData || "",
    });
    if (opts.json !== undefined) {
      headers["Content-Type"] = "application/json";
      opts = { ...opts, body: JSON.stringify(opts.json) };
      delete opts.json;
    } else if (opts.body && !(opts.body instanceof FormData) && typeof opts.body !== "string") {
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    const res = await fetch(path, { ...opts, headers });
    let json = null;
    try { json = await res.json(); } catch (_) { json = null; }
    if (!res.ok) {
      const msg = (json && json.error) || `HTTP ${res.status}`;
      const err = new Error(msg);
      err.payload = json;
      throw err;
    }
    return json;
  }

  function renderUser() {
    const box = $("#userBox");
    if (!box) return;
    if (tgUser) {
      const name = tgUser.first_name || "User";
      box.textContent = `👤 ${name}`;
    } else {
      box.textContent = "⚠️ افتح من داخل تيليجرام";
    }
  }

  async function checkAdmin() {
    try {
      const me = await api("/api/me");
      if (me && me.is_admin) {
        const tab = $("#adminTab");
        if (tab) tab.hidden = false;
        loadAdminOverview();
        loadAdminFiles();
      }
    } catch (_) {}
  }

  async function loadAdminOverview() {
    try {
      const data = await api("/api/admin/overview");
      const ov = (data && data.overview) || {};
      $("#adminTotal").textContent = ov.total_files ?? "—";
      $("#adminRunning").textContent = ov.running ?? "—";
      $("#adminUsers").textContent = ov.users ?? "—";
    } catch (_) {}
  }

  /* ——— Admin sub-tabs ——— */
  function switchAdminStab(name) {
    $$(".admin-stab").forEach(b => b.classList.toggle("active", b.dataset.stab === name));
    $$(".admin-stab-pane").forEach(p => p.hidden = (p.id !== `astab-${name}`));
  }

  document.addEventListener("click", (ev) => {
    const t = ev.target;
    if (!t) return;
    /* sub-tabs */
    if (t.classList.contains("admin-stab") && t.dataset.stab) {
      switchAdminStab(t.dataset.stab);
      if (t.dataset.stab === "users") loadAdminUsers();
      if (t.dataset.stab === "system") loadAdminSystem();
      if (t.dataset.stab === "settings") adminLoadSettings();
      return;
    }
    if (t.id === "adminRefreshBtn") { loadAdminOverview(); loadAdminFiles(); }
    if (t.id === "stopAllBtn") stopAllFiles();
    if (t.id === "adminCloseDetail") closeAdminDetail();
    if (t.id === "adminStartBtn") adminStartCurrent();
    if (t.id === "adminStopBtn") adminStopCurrent();
    if (t.id === "adminDeleteBtn") adminDeleteCurrent();
    if (t.id === "adminRefreshLogsBtn") loadAdminLogs(currentAdminFileId);
    if (t.id === "adminUsersRefreshBtn") loadAdminUsers();
    if (t.id === "adminBroadcastBtn") adminBroadcast();
    if (t.id === "adminSysRefreshBtn") loadAdminSystem();
    if (t.id === "addAdminBtn") adminAddAdmin();
    if (t.id === "removeAdminBtn") adminRemoveAdmin();
    if (t.id === "adminUserCloseDetail") closeAdminUserDetail();
    if (t.id === "adminSendMsgBtn") adminSendMsg();
  });

  document.addEventListener("input", (ev) => {
    if (ev.target && ev.target.id === "adminSearch") renderAdminFiles();
    if (ev.target && ev.target.id === "adminUserSearch") renderAdminUsers();
  });

  let adminAllFiles = [];
  let currentAdminFileId = null;

  async function loadAdminFiles() {
    const list = $("#adminFileList");
    if (!list) return;
    list.innerHTML = `<div class="empty">⏳ جارٍ التحميل…</div>`;
    try {
      const data = await api("/api/admin/files");
      adminAllFiles = data.files || [];
      renderAdminFiles();
    } catch (e) {
      list.innerHTML = `<div class="empty">⚠️ ${escapeHtml(e.message)}</div>`;
    }
  }

  function renderAdminFiles() {
    const list = $("#adminFileList");
    if (!list) return;
    const q = ($("#adminSearch")?.value || "").trim().toLowerCase();
    let files = adminAllFiles;
    if (q) files = files.filter(f =>
      (f.name || "").toLowerCase().includes(q) || String(f.uid || "").includes(q));
    if (!files.length) { list.innerHTML = `<div class="empty">لا توجد ملفات.</div>`; return; }
    list.innerHTML = files.map((f) => `
      <div class="file-card" data-id="${f.id}">
        <div>
          <div class="file-name">${escapeHtml(f.name)}</div>
          <div class="file-meta">👤 <code>${escapeHtml(String(f.uid || ""))}</code> · ${new Date((f.created_at || 0) * 1000).toLocaleString("ar")}</div>
        </div>
        ${statusBadge(f.status)}
      </div>`).join("");
    $$("#adminFileList .file-card").forEach((card) =>
      card.addEventListener("click", () => openAdminDetail(card.dataset.id)));
  }

  async function openAdminDetail(id) {
    currentAdminFileId = id;
    $("#adminDetailPanel").hidden = false;
    $("#tab-admin").classList.remove("active");
    $("#adminDetailTitle").textContent = "⏳ جارٍ التحميل…";
    $("#adminCode").textContent = "";
    $("#adminLogs").textContent = "";
    try {
      const data = await api(`/api/admin/files/${id}`);
      const f = data.file;
      $("#adminDetailTitle").textContent = f.name;
      const badge = $("#adminDetailStatus");
      badge.className = "status-pill " + (f.status === "running" ? "status-running" : "status-stopped");
      badge.textContent = f.status === "running" ? "يعمل" : "متوقف";
      $("#adminDetailMeta").innerHTML = `
        <div>👤 المالك (UID): <code>${escapeHtml(String(f.uid || ""))}</code></div>
        <div>🆔 معرّف الملف: <code>${escapeHtml(f.id)}</code></div>
        <div>📅 ${new Date((f.created_at || 0) * 1000).toLocaleString("ar")}</div>`;
      $("#adminCode").textContent = data.code || "(لا يوجد كود)";
      await loadAdminLogs(id);
    } catch (e) { $("#adminDetailTitle").textContent = "⚠️ " + e.message; }
  }

  async function loadAdminLogs(id) {
    if (!id) return;
    try {
      const data = await api(`/api/admin/files/${id}/logs`);
      $("#adminLogs").textContent = data.logs || "(لا توجد سجلات)";
      const badge = $("#adminDetailStatus");
      badge.className = "status-pill " + (data.status === "running" ? "status-running" : "status-stopped");
      badge.textContent = data.status === "running" ? "يعمل" : "متوقف";
    } catch (e) { $("#adminLogs").textContent = "⚠️ " + e.message; }
  }

  function closeAdminDetail() {
    currentAdminFileId = null;
    $("#adminDetailPanel").hidden = true;
    $("#tab-admin").classList.add("active");
    loadAdminOverview(); loadAdminFiles();
  }

  async function adminStartCurrent() {
    if (!currentAdminFileId) return;
    try {
      const res = await api(`/api/admin/files/${currentAdminFileId}/start`, { method: "POST" });
      if (res.ok) { toast("تم التشغيل ▶️", "ok"); await loadAdminLogs(currentAdminFileId); }
      else toast(res.error || "فشل التشغيل", "error");
    } catch (e) { toast(e.message, "error"); }
  }

  async function adminStopCurrent() {
    if (!currentAdminFileId) return;
    if (!confirm("إيقاف هذا الملف؟")) return;
    try {
      await api(`/api/admin/files/${currentAdminFileId}/stop`, { method: "POST" });
      toast("تم الإيقاف ⏹️", "ok");
      await loadAdminLogs(currentAdminFileId);
    } catch (e) { toast(e.message, "error"); }
  }

  async function adminDeleteCurrent() {
    if (!currentAdminFileId) return;
    if (!confirm("حذف هذا الملف نهائياً؟ لا يمكن التراجع.")) return;
    try {
      await api(`/api/admin/files/${currentAdminFileId}`, { method: "DELETE" });
      toast("تم الحذف 🗑️", "ok"); closeAdminDetail();
    } catch (e) { toast(e.message, "error"); }
  }

  async function stopAllFiles() {
    if (!confirm("إيقاف جميع ملفات كل المستخدمين؟")) return;
    const btn = $("#stopAllBtn"), out = $("#adminOutput"), orig = btn.textContent;
    btn.disabled = true; btn.textContent = "⏳…";
    if (out) { out.hidden = false; out.textContent = "⏳ جارٍ الإيقاف…"; }
    try {
      const res = await api("/api/admin/stop_all", { method: "POST" });
      const lines = [`✅ فحص ${res.scanned}`, `⏹️ أُوقف: ${res.stopped_count}`, `⚠️ فشل: ${res.failed_count}`];
      if (out) out.textContent = lines.join("\n");
      toast(`أُوقف ${res.stopped_count} ملف`, "ok");
      loadAdminOverview(); loadFiles();
    } catch (e) {
      if (out) out.textContent = "❌ " + e.message;
      toast(e.message, "error");
    } finally { btn.disabled = false; btn.textContent = orig; }
  }

  /* ——— Users sub-tab ——— */
  let adminAllUsers = [];
  let currentAdminUserId = null;

  async function loadAdminUsers() {
    const list = $("#adminUserList");
    if (!list) return;
    list.innerHTML = `<div class="empty">⏳ جارٍ التحميل…</div>`;
    try {
      const data = await api("/api/admin/users");
      adminAllUsers = data.users || [];
      renderAdminUsers();
      loadAdminOverview();
    } catch (e) { list.innerHTML = `<div class="empty">⚠️ ${escapeHtml(e.message)}</div>`; }
  }

  function renderAdminUsers() {
    const list = $("#adminUserList");
    if (!list) return;
    const q = ($("#adminUserSearch")?.value || "").trim().toLowerCase();
    let users = adminAllUsers;
    if (q) users = users.filter(u =>
      (u.name || "").toLowerCase().includes(q) || String(u.uid || "").includes(q));
    if (!users.length) { list.innerHTML = `<div class="empty">لا يوجد مستخدمون.</div>`; return; }
    list.innerHTML = users.map(u => `
      <div class="file-card" data-uid="${escapeHtml(u.uid)}">
        <div>
          <div class="file-name">
            ${escapeHtml(u.name)}
            ${u.is_admin ? `<span class="badge-admin">مشرف</span>` : ""}
            ${u.banned ? `<span class="badge-banned">محظور</span>` : ""}
            ${u.subscribed ? `<span class="badge-sub">مشترك</span>` : ""}
          </div>
          <div class="file-meta">🆔 <code>${escapeHtml(u.uid)}</code> · 📂 ${u.files} ملف · ▶️ ${u.running} شغّال · 💎 ${u.points ?? 0}</div>
        </div>
      </div>`).join("");
    $$("#adminUserList .file-card").forEach(card =>
      card.addEventListener("click", () => openAdminUserDetail(card.dataset.uid)));
  }

  async function openAdminUserDetail(uid) {
    currentAdminUserId = uid;
    const u = adminAllUsers.find(x => x.uid === uid);
    if (!u) return;
    $("#adminUserDetailPanel").hidden = false;
    $("#tab-admin").classList.remove("active");
    $("#adminUserDetailTitle").textContent = u.name || uid;
    const badge = $("#adminUserBadge");
    badge.className = "status-pill " + (u.banned ? "status-stopped" : "status-running");
    badge.textContent = u.banned ? "محظور" : "نشط";

    // Subscription expiry display
    let subText = "❌ لا يوجد اشتراك";
    if (u.subscribed) {
      if (u.sub_expiry === -1) subText = "🔑 اشتراك دائم";
      else {
        const d = new Date((u.sub_expiry || 0) * 1000);
        subText = `🎟️ مشترك حتى: ${d.toLocaleDateString("ar")}`;
      }
    }
    const subInfoEl = $("#adminUserSubInfo");
    if (subInfoEl) subInfoEl.innerHTML = `
      <div>${subText}</div>
      <div>💎 النقاط: <b>${u.points ?? 0}</b></div>`;

    $("#adminUserDetailMeta").innerHTML = `
      <div>🆔 ID: <code>${escapeHtml(uid)}</code></div>
      <div>📂 ${u.files} ملف · ▶️ ${u.running} شغّال</div>
      <div>${u.is_admin ? "👑 مشرف" : "👤 مستخدم عادي"} · ${u.subscribed ? '<span class="badge-sub">مشترك</span>' : '<span style="color:var(--fg2)">غير مشترك</span>'} · <span class="badge-pts">💎 ${u.points ?? 0}</span></div>`;

    const acts = $("#adminUserActions");
    acts.innerHTML = `
      ${u.banned
        ? `<button class="btn primary" id="uUnbanBtn">✅ رفع الحظر</button>`
        : `<button class="btn danger" id="uBanBtn">🚫 حظر</button>`}
      ${u.is_admin
        ? `<button class="btn warn" id="uRemAdminBtn">➖ إزالة مشرف</button>`
        : `<button class="btn ghost" id="uAddAdminBtn">👑 ترقية مشرف</button>`}
      <button class="btn danger" id="uDelFilesBtn">🗑️ حذف جميع ملفاته</button>`;
    acts.onclick = async (ev) => {
      const t = ev.target;
      if (t.id === "uBanBtn") { const r = prompt("سبب الحظر (اختياري):") ?? ""; await adminBanUser(uid, r); }
      if (t.id === "uUnbanBtn") await adminUnbanUser(uid);
      if (t.id === "uAddAdminBtn") await adminGrantAdmin(uid);
      if (t.id === "uRemAdminBtn") await adminRevokeAdmin(uid);
      if (t.id === "uDelFilesBtn") await adminDeleteUserFiles(uid);
    };

    // Subscription/Points buttons
    const subPerm = $("#adminSubPermBtn");
    const subDays = $("#adminSubDaysBtn");
    const unsub   = $("#adminUnsubBtn");
    const addPts  = $("#adminAddPointsBtn");
    if (subPerm) subPerm.onclick = () => adminSubscribeUser(uid, -1);
    if (subDays) subDays.onclick = () => {
      const d = parseInt($("#adminSubDaysInput")?.value || "0");
      if (!d || d < 1) { toast("أدخل عدد الأيام", "error"); return; }
      adminSubscribeUser(uid, d);
    };
    if (unsub) unsub.onclick = () => adminUnsubscribeUser(uid);
    if (addPts) addPts.onclick = () => adminSendPoints(uid);

    $("#adminMsgText").value = "";
  }

  function closeAdminUserDetail() {
    currentAdminUserId = null;
    $("#adminUserDetailPanel").hidden = true;
    $("#tab-admin").classList.add("active");
    loadAdminUsers();
  }

  async function adminBanUser(uid, reason) {
    if (!confirm(`حظر المستخدم ${uid}؟ سيتوقف عن الوصول فوراً.`)) return;
    try {
      await api(`/api/admin/users/${uid}/ban`, { method: "POST", json: { reason } });
      toast("تم الحظر 🚫", "ok"); closeAdminUserDetail();
    } catch (e) { toast(e.message, "error"); }
  }

  async function adminUnbanUser(uid) {
    try {
      await api(`/api/admin/users/${uid}/unban`, { method: "POST" });
      toast("تم رفع الحظر ✅", "ok"); closeAdminUserDetail();
    } catch (e) { toast(e.message, "error"); }
  }

  async function adminGrantAdmin(uid) {
    if (!confirm(`منح صلاحيات المشرف للمستخدم ${uid}؟`)) return;
    try {
      await api(`/api/admin/admins/${uid}`, { method: "POST" });
      toast("تمت الترقية 👑", "ok"); closeAdminUserDetail();
    } catch (e) { toast(e.message, "error"); }
  }

  async function adminRevokeAdmin(uid) {
    if (!confirm(`إزالة صلاحيات المشرف من ${uid}؟`)) return;
    try {
      await api(`/api/admin/admins/${uid}`, { method: "DELETE" });
      toast("تمت الإزالة", "ok"); closeAdminUserDetail();
    } catch (e) { toast(e.message, "error"); }
  }

  async function adminDeleteUserFiles(uid) {
    if (!confirm(`حذف جميع ملفات المستخدم ${uid}؟ لا يمكن التراجع!`)) return;
    try {
      const res = await api(`/api/admin/users/${uid}`, { method: "DELETE" });
      toast(`تم حذف ${res.deleted} ملف 🗑️`, "ok"); closeAdminUserDetail();
    } catch (e) { toast(e.message, "error"); }
  }

  async function adminSendMsg() {
    const uid = currentAdminUserId;
    const text = ($("#adminMsgText")?.value || "").trim();
    if (!uid || !text) return;
    try {
      await api(`/api/admin/message/${uid}`, { method: "POST", json: { text } });
      toast("تم الإرسال 📨", "ok");
      $("#adminMsgText").value = "";
    } catch (e) { toast(e.message, "error"); }
  }

  async function adminBroadcast() {
    const text = prompt("اكتب الرسالة الجماعية التي ستُرسل لجميع المستخدمين:");
    if (!text || !text.trim()) return;
    if (!confirm(`إرسال الرسالة لـ ${adminAllUsers.length} مستخدم؟`)) return;
    try {
      const res = await api("/api/admin/broadcast", { method: "POST", json: { text } });
      toast(`أُرسلت لـ ${res.sent} · فشل ${res.failed} 📢`, "ok");
    } catch (e) { toast(e.message, "error"); }
  }

  /* ——— System sub-tab ——— */
  async function loadAdminSystem() {
    const box = $("#adminSysInfo");
    if (!box) return;
    box.innerHTML = `<div class="empty">⏳ جارٍ التحميل…</div>`;
    try {
      const data = await api("/api/admin/system");
      const s = data.system || {};
      const cards = [
        { title: "💾 استخدام القرص", val: `${s.disk_used_mb ?? "—"} MB`, sub: `من ${s.disk_total_mb ?? "—"} MB · متبقي ${s.disk_free_mb ?? "—"} MB` },
        { title: "🧠 استخدام الذاكرة", val: `${s.ram_used_mb ?? "—"} MB`, sub: `من ${s.ram_total_mb ?? "—"} MB · متبقي ${s.ram_free_mb ?? "—"} MB` },
        { title: "⚡ حمل المعالج (1م)", val: s.load_1m ?? "—", sub: `5م: ${s.load_5m ?? "—"} · 15م: ${s.load_15m ?? "—"}` },
        { title: "⏱️ وقت التشغيل", val: s.uptime ?? "—", sub: s.data_dir ?? "" },
      ];
      box.innerHTML = cards.map(c => `
        <div class="sys-card">
          <div class="sys-card-title">${c.title}</div>
          <div class="sys-card-val">${c.val}</div>
          <div class="sys-card-sub">${c.sub}</div>
        </div>`).join("");
    } catch (e) { box.innerHTML = `<div class="empty">⚠️ ${escapeHtml(e.message)}</div>`; }
  }

  /* ——— Settings sub-tab ——— */
  async function adminAddAdmin() {
    const uid = ($("#addAdminInput")?.value || "").trim();
    if (!uid) { toast("أدخل ID المستخدم", "error"); return; }
    try {
      await api(`/api/admin/admins/${uid}`, { method: "POST" });
      toast("تمت الإضافة 👑", "ok");
      if ($("#addAdminInput")) $("#addAdminInput").value = "";
    } catch (e) { toast(e.message, "error"); }
  }

  async function adminRemoveAdmin() {
    const uid = ($("#removeAdminInput")?.value || "").trim();
    if (!uid) { toast("أدخل ID المستخدم", "error"); return; }
    try {
      await api(`/api/admin/admins/${uid}`, { method: "DELETE" });
      toast("تمت الإزالة ✅", "ok");
      if ($("#removeAdminInput")) $("#removeAdminInput").value = "";
    } catch (e) { toast(e.message, "error"); }
  }

  /* ——— Mode toggle ——— */
  async function adminLoadSettings() {
    try {
      const data = await api("/api/admin/mode");
      const isPaid = data.mode === "paid";
      const tog = $("#modeToggle");
      const lbl = $("#modeLabel");
      if (tog) tog.checked = isPaid;
      if (lbl) {
        lbl.textContent = isPaid ? "مدفوع 💰" : "مجاني ✅";
        lbl.className = "mode-badge " + (isPaid ? "paid" : "free");
      }
      renderAdminPlans(data.plans || {});
    } catch (e) { toast(e.message, "error"); }
  }

  async function adminToggleMode(checked) {
    const mode = checked ? "paid" : "free";
    try {
      await api("/api/admin/mode", { method: "POST", json: { mode } });
      const lbl = $("#modeLabel");
      if (lbl) {
        lbl.textContent = checked ? "مدفوع 💰" : "مجاني ✅";
        lbl.className = "mode-badge " + (checked ? "paid" : "free");
      }
      toast(checked ? "الوضع: مدفوع 💰" : "الوضع: مجاني ✅", "ok");
    } catch (e) { toast(e.message, "error"); }
  }

  /* ——— Plans (admin) ——— */
  let adminPlansCache = {};

  function renderAdminPlans(plans) {
    adminPlansCache = plans || {};
    const box = $("#adminPlansList");
    if (!box) return;
    const entries = Object.entries(plans);
    if (!entries.length) { box.innerHTML = `<div class="empty">لا توجد باقات.</div>`; return; }
    box.innerHTML = entries.map(([id, p]) => `
      <div class="plan-card-admin">
        <div class="plan-info">
          <div class="plan-name">${escapeHtml(p.name)}</div>
          <div class="plan-meta">💎 ${p.points_cost} نقطة · 📅 ${p.duration_days} يوم${p.description ? ' · ' + escapeHtml(p.description) : ''}</div>
        </div>
        <button class="btn danger small" data-planid="${escapeHtml(id)}" onclick="adminDeletePlan('${escapeHtml(id)}')">🗑️</button>
      </div>`).join("");
  }

  async function adminSavePlan() {
    const pid  = ($("#planIdInput")?.value || "").trim().replace(/\s+/g,"_");
    const name = ($("#planNameInput")?.value || "").trim();
    const cost = parseInt($("#planCostInput")?.value || "0");
    const days = parseInt($("#planDaysInput")?.value || "0");
    const desc = ($("#planDescInput")?.value || "").trim();
    if (!pid || !name || !cost || !days) { toast("أكمل جميع الحقول", "error"); return; }
    try {
      await api(`/api/admin/plans/${pid}`, { method: "POST", json: { name, points_cost: cost, duration_days: days, description: desc } });
      toast("تم حفظ الباقة 📦", "ok");
      ["planIdInput","planNameInput","planCostInput","planDaysInput","planDescInput"].forEach(id => { if ($(id)) $(id).value = ""; });
      adminLoadSettings();
    } catch (e) { toast(e.message, "error"); }
  }

  async function adminDeletePlan(planId) {
    if (!confirm(`حذف الباقة "${planId}"؟`)) return;
    try {
      await api(`/api/admin/plans/${planId}`, { method: "DELETE" });
      toast("تم حذف الباقة 🗑️", "ok");
      adminLoadSettings();
    } catch (e) { toast(e.message, "error"); }
  }

  /* ——— Subscription actions (admin on user) ——— */
  async function adminSubscribeUser(uid, days) {
    const msg = days === -1 ? "منح اشتراك دائم" : `منح اشتراك ${days} يوم`;
    if (!confirm(`${msg} للمستخدم ${uid}؟`)) return;
    try {
      await api(`/api/admin/users/${uid}/subscribe`, { method: "POST", json: { days } });
      toast("تم منح الاشتراك 🎟️", "ok");
      await loadAdminUsers();
      openAdminUserDetail(uid);
    } catch (e) { toast(e.message, "error"); }
  }

  async function adminUnsubscribeUser(uid) {
    if (!confirm(`إلغاء اشتراك المستخدم ${uid}؟`)) return;
    try {
      await api(`/api/admin/users/${uid}/subscribe`, { method: "DELETE" });
      toast("تم إلغاء الاشتراك ❌", "ok");
      await loadAdminUsers();
      openAdminUserDetail(uid);
    } catch (e) { toast(e.message, "error"); }
  }

  async function adminSendPoints(uid) {
    const amount = parseInt($("#adminPointsInput")?.value || "0");
    if (!amount) { toast("أدخل عدد النقاط", "error"); return; }
    try {
      const res = await api(`/api/admin/users/${uid}/points`, { method: "POST", json: { amount } });
      toast(`✅ رصيد النقاط الجديد: ${res.new_balance} 💎`, "ok");
      if ($("#adminPointsInput")) $("#adminPointsInput").value = "";
      await loadAdminUsers();
      openAdminUserDetail(uid);
    } catch (e) { toast(e.message, "error"); }
  }

  /* ——— User-facing status + paywall ——— */
  let userStatus = null;

  async function loadUserStatus() {
    try {
      userStatus = await api("/api/me/status");
      // Status bar
      const bar = $("#userStatusBar");
      const ptsBadge = $("#userPointsBadge");
      const subBadge = $("#userSubBadge");
      const modeBadge = $("#userModeBadge");
      if (bar) bar.hidden = false;
      if (ptsBadge) ptsBadge.textContent = userStatus.points ?? 0;
      if (subBadge) {
        if (userStatus.subscribed) {
          subBadge.className = "status-pill status-running";
          if (userStatus.sub_expiry === -1) subBadge.textContent = "🔑 اشتراك دائم";
          else {
            const d = new Date((userStatus.sub_expiry || 0) * 1000);
            subBadge.textContent = `🎟️ حتى ${d.toLocaleDateString("ar")}`;
          }
        } else {
          subBadge.className = "status-pill status-stopped";
          subBadge.textContent = "غير مشترك";
        }
      }
      if (modeBadge) modeBadge.textContent = userStatus.mode === "paid" ? "💰 الخدمة مدفوعة" : "✅ الخدمة مجانية";

      // Paywall
      const pw = $("#paywallBanner");
      if (pw) {
        const locked = userStatus.mode === "paid" && !userStatus.can_upload;
        pw.hidden = !locked;
        if (locked) {
          const pts = userStatus.points ?? 0;
          const pwPts = $("#paywallPoints");
          if (pwPts) pwPts.textContent = pts;
          renderPaywallPlans(userStatus.plans || {}, pts);
        }
      }
    } catch (_) {}
  }

  function renderPaywallPlans(plans, myPoints) {
    const box = $("#paywallPlans");
    if (!box) return;
    const entries = Object.entries(plans);
    if (!entries.length) { box.innerHTML = `<div class="empty">لا توجد باقات متاحة.</div>`; return; }
    box.innerHTML = entries.map(([id, p]) => {
      const canAfford = myPoints >= p.points_cost;
      return `
      <div class="plan-card-user">
        <div class="plan-name">${escapeHtml(p.name)}</div>
        <div class="plan-desc">${escapeHtml(p.description || "")} — ${p.duration_days} يوم</div>
        <div class="plan-cost">💎 ${p.points_cost} نقطة</div>
        <button class="btn ${canAfford ? "primary" : "ghost"} small" ${canAfford ? "" : "disabled"} onclick="buyPlan('${escapeHtml(id)}')">
          ${canAfford ? "🛒 شراء الباقة" : "💸 نقاطك غير كافية"}
        </button>
      </div>`;
    }).join("");
  }

  async function buyPlan(planId) {
    if (!confirm("شراء هذه الباقة؟")) return;
    try {
      const res = await api(`/api/plans/${planId}/buy`, { method: "POST" });
      const exp = new Date(res.expiry * 1000).toLocaleDateString("ar");
      toast(`🎉 اشتركت بـ "${res.plan_name}" حتى ${exp}! رصيد النقاط: ${res.new_points}`, "ok");
      await loadUserStatus();
    } catch (e) { toast(e.message, "error"); }
  }

  function switchTab(name) {
    $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    $$(".tab-pane").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
  }
  $$(".tab").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  let allFiles = [];
  let currentFileId = null;
  let logsTimer = null;

  function statusBadge(status) {
    const cls = status === "running" ? "status-running" : "status-stopped";
    const txt = status === "running" ? "يعمل" : "متوقف";
    return `<span class="status-pill ${cls}">${txt}</span>`;
  }

  async function loadFiles() {
    const list = $("#fileList");
    if (!list) return;
    try {
      const data = await api("/api/files");
      allFiles = data.files || [];
      const cnt = $("#filesCount");
      if (cnt) {
        cnt.textContent = String(allFiles.length);
        cnt.hidden = allFiles.length === 0;
      }
      if (allFiles.length === 0) {
        list.innerHTML = `<div class="empty">لا توجد ملفات بعد. ابدأ من تبويب <b>＋ إنشاء استضافة</b>.</div>`;
        return;
      }
      list.innerHTML = allFiles.map((f) => `
        <div class="file-card" data-id="${f.id}">
          <div>
            <div class="file-name">${escapeHtml(f.name)}</div>
            <div class="file-meta">${new Date((f.created_at || 0) * 1000).toLocaleString("ar")}</div>
          </div>
          ${statusBadge(f.status)}
        </div>
      `).join("");
      $$("#fileList .file-card").forEach((card) => {
        card.addEventListener("click", () => openDetail(card.dataset.id));
      });
    } catch (e) {
      list.innerHTML = `<div class="empty">⚠️ ${escapeHtml(e.message)}</div>`;
    }
  }

  $("#saveAndRunBtn").addEventListener("click", () => createNew(true));
  $("#saveOnlyBtn").addEventListener("click", () => createNew(false));

  async function createNew(autoRun) {
    const name = $("#newName").value.trim() || "main.py";
    const code = $("#newCode").value;
    const libs = $("#newLibs").value.trim();
    if (!code.trim()) { toast("الكود فارغ", "error"); return; }

    const btn = autoRun ? $("#saveAndRunBtn") : $("#saveOnlyBtn");
    const orig = btn.textContent;
    btn.disabled = true;
    btn.textContent = "⏳ جارٍ الحفظ…";

    try {
      if (libs && autoRun) {
        btn.textContent = "📦 تثبيت المكتبات…";
        await runInstall(libs);
      }
      const { file } = await api("/api/files", { method: "POST", body: { name, code } });
      toast("تم الحفظ ✅", "ok");
      $("#newCode").value = "";
      $("#newName").value = "main.py";
      $("#newLibs").value = "";
      await loadFiles();
      await openDetail(file.id);
      if (autoRun) await runCurrent();
    } catch (e) {
      toast(e.message, "error");
    } finally {
      btn.disabled = false;
      btn.textContent = orig;
    }
  }

  $("#pickBtn").addEventListener("click", () => $("#fileInput").click());
  $("#fileInput").addEventListener("change", async (ev) => {
    const f = ev.target.files[0];
    if (!f) return;
    const status = $("#uploadStatus");
    status.textContent = "جارٍ الرفع…";
    try {
      const fd = new FormData();
      fd.append("file", f);
      const res = await fetch("/api/upload", {
        method: "POST",
        body: fd,
        headers: { "X-Init-Data": initData || "" },
      });
      const json = await res.json();
      if (!res.ok) throw new Error(json.error || "فشل الرفع");
      status.textContent = `✅ تم رفع: ${json.file.name}`;
      ev.target.value = "";
      await loadFiles();
      await openDetail(json.file.id);
    } catch (e) {
      status.textContent = "❌ " + e.message;
    }
  });

  $("#installBtn").addEventListener("click", async () => {
    const libs = $("#libsInput").value.trim();
    if (!libs) { toast("اكتب أسماء المكتبات", "error"); return; }
    await runInstall(libs);
  });

  async function runInstall(libs, outputEl) {
    const out = outputEl || $("#installOutput");
    if (out) {
      out.hidden = false;
      out.textContent = "⏳ جارٍ التثبيت…\n";
    }
    try {
      const res = await fetch("/api/install", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Init-Data": initData || "",
        },
        body: JSON.stringify({ libraries: libs }),
      });
      const json = await res.json();
      const lines = [];
      lines.push(json.message || "");
      if (json.installed && json.installed.length) lines.push("Installed: " + json.installed.join(", "));
      if (json.failed && json.failed.length) lines.push("Failed: " + json.failed.join(", "));
      if (json.stdout) lines.push("\n--- pip stdout ---\n" + json.stdout);
      if (json.stderr) lines.push("\n--- pip stderr ---\n" + json.stderr);
      if (out) out.textContent = lines.join("\n").trim() || "(no output)";
      if (json.success) toast("تم التثبيت ✅", "ok");
      else toast("فشل التثبيت — راجع المخرجات", "error");
      return json;
    } catch (e) {
      if (out) out.textContent = "❌ " + e.message;
      toast(e.message, "error");
      return { success: false };
    }
  }

  async function openDetail(id) {
    currentFileId = id;
    const panel = $("#detailPanel");
    panel.hidden = false;
    $("#editor").value = "";
    $("#logsBox").textContent = "";
    $("#installSuggest").hidden = true;
    $("#runStatusBanner").hidden = true;
    $("#saveHint").hidden = true;
    $("#detailInstallOutput").hidden = true;
    try {
      const data = await api(`/api/files/${id}`);
      $("#detailTitle").textContent = data.file.name;
      updateStatusPill(data.file.status || "stopped");
      $("#editor").value = data.code || "";
      await refreshLogs();
      startLogsPolling();
    } catch (e) {
      toast(e.message, "error");
      closeDetail();
    }
  }

  function closeDetail() {
    $("#detailPanel").hidden = true;
    currentFileId = null;
    stopLogsPolling();
    switchTab("files");
  }

  $("#closeDetail").addEventListener("click", closeDetail);

  $("#detailInstallBtn").addEventListener("click", async () => {
    const libs = $("#detailLibsInput").value.trim();
    if (!libs) { toast("اكتب أسماء المكتبات", "error"); return; }
    await runInstall(libs, $("#detailInstallOutput"));
    $("#detailLibsInput").value = "";
  });

  $("#runBtn").addEventListener("click", runCurrent);
  $("#stopBtn").addEventListener("click", stopCurrent);
  $("#saveBtn").addEventListener("click", saveCurrent);
  $("#deleteBtn").addEventListener("click", deleteCurrent);
  $("#refreshLogsBtn").addEventListener("click", refreshLogs);
  $("#refreshBtn").addEventListener("click", loadFiles);
  $("#autoRefresh").addEventListener("change", () => {
    if ($("#autoRefresh").checked) startLogsPolling();
    else stopLogsPolling();
  });

  $("#stdinForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (!currentFileId) return;
    const input = $("#stdinInput");
    const text = input.value;
    if (!text.trim()) return;
    const sendBtn = $("#stdinSendBtn");
    sendBtn.disabled = true;
    try {
      await api(`/api/files/${currentFileId}/input`, { method: "POST", body: { text } });
      input.value = "";
      setTimeout(refreshLogs, 250);
      setTimeout(refreshLogs, 1000);
    } catch (e) {
      toast(e.message, "error");
    } finally {
      sendBtn.disabled = false;
      input.focus();
    }
  });

  function showRunBanner(kind, text) {
    const b = $("#runStatusBanner");
    if (!b) return;
    b.hidden = false;
    b.className = "run-status " + kind;
    b.textContent = text;
  }

  async function runCurrent() {
    if (!currentFileId) return;
    showRunBanner("info", "⏳ جارٍ بدء التشغيل…");
    try {
      await api(`/api/files/${currentFileId}`, { method: "PUT", body: { code: $("#editor").value } });
    } catch (_) {}
    try {
      await api(`/api/files/${currentFileId}/start`, { method: "POST" });
      toast("بدأ التشغيل ▶️", "ok");
      updateStatusPill("running");
      showRunBanner("ok", "✅ تم بدء تشغيل السكربت — راقب المخرجات أدناه");
    } catch (e) {
      updateStatusPill("stopped");
      showRunBanner("err", "❌ فشل البدء: " + e.message);
      toast("فشل البدء — راجع السجلات", "error");
    }
    setTimeout(refreshLogs, 600);
  }

  async function stopCurrent() {
    if (!currentFileId) return;
    try {
      await api(`/api/files/${currentFileId}/stop`, { method: "POST" });
      toast("تم الإيقاف ⏹️", "ok");
      updateStatusPill("stopped");
      showRunBanner("info", "⏹️ تم إيقاف السكربت");
    } catch (e) {
      toast(e.message, "error");
    }
    await refreshLogs();
  }

  async function saveCurrent() {
    if (!currentFileId) return;
    const btn = $("#saveBtn");
    const orig = btn.textContent;
    btn.disabled = true;
    btn.textContent = "⏳ حفظ…";
    try {
      await api(`/api/files/${currentFileId}`, { method: "PUT", body: { code: $("#editor").value } });
      const data = await api(`/api/files/${currentFileId}`);
      const saved = data.code === $("#editor").value;
      const hint = $("#saveHint");
      hint.hidden = false;
      const t = new Date().toLocaleTimeString("ar");
      if (saved) {
        hint.className = "save-hint ok";
        hint.textContent = `✅ تم الحفظ بنجاح (${t})`;
        toast("تم الحفظ 💾", "ok");
      } else {
        hint.className = "save-hint err";
        hint.textContent = "⚠️ المحتوى المحفوظ لا يطابق المحرر — حاول مرة أخرى";
      }
    } catch (e) {
      toast(e.message, "error");
      const hint = $("#saveHint");
      hint.hidden = false;
      hint.className = "save-hint err";
      hint.textContent = "❌ فشل الحفظ: " + e.message;
    } finally {
      btn.disabled = false;
      btn.textContent = orig;
    }
  }

  async function deleteCurrent() {
    if (!currentFileId) return;
    if (!confirm("حذف هذا الملف نهائيًا؟")) return;
    try {
      await api(`/api/files/${currentFileId}`, { method: "DELETE" });
      toast("تم الحذف 🗑️", "ok");
      closeDetail();
      await loadFiles();
    } catch (e) { toast(e.message, "error"); }
  }

  function updateStatusPill(status) {
    const el = $("#detailStatus");
    if (el) {
      const cls = status === "running" ? "status-running" : "status-stopped";
      const txt = status === "running" ? "يعمل" : "متوقف";
      el.className = `status-pill ${cls}`;
      el.textContent = txt;
    }
    const form = $("#stdinForm");
    if (form) form.hidden = status !== "running";
  }

  async function refreshLogs() {
    if (!currentFileId) return;
    try {
      const data = await api(`/api/files/${currentFileId}/logs`);
      const box = $("#logsBox");
      const wasAtBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
      box.textContent = data.logs || "(لا توجد مخرجات بعد)";
      if (wasAtBottom) box.scrollTop = box.scrollHeight;
      updateStatusPill(data.status || "stopped");
      if (data.missing_modules && data.missing_modules.length) {
        $("#installSuggest").hidden = false;
        $("#missingList").textContent = data.missing_modules.join("  ");
        $("#installMissingBtn").onclick = async () => {
          await runInstall(data.missing_modules.join(" "));
          toast("جرب التشغيل مرة أخرى ▶️", "ok");
          await refreshLogs();
        };
      } else {
        $("#installSuggest").hidden = true;
      }
    } catch (_) {}
  }

  function startLogsPolling() {
    stopLogsPolling();
    if (!$("#autoRefresh").checked) return;
    logsTimer = setInterval(refreshLogs, 2500);
  }
  function stopLogsPolling() {
    if (logsTimer) { clearInterval(logsTimer); logsTimer = null; }
  }

  renderUser();
  loadFiles();
  checkAdmin();
  loadUserStatus();
})();
"""


# =============================================================================
# Telegram bot
# =============================================================================
def _webapp_url() -> str:
    domains = (os.environ.get("REPLIT_DOMAINS", "") or PUBLIC_DOMAIN).strip()
    if domains:
        first = domains.split(",")[0].strip()
        if first:
            return f"https://{first}/"
    dev = os.environ.get("REPLIT_DEV_DOMAIN", "").strip()
    if dev:
        return f"https://{dev}/"
    return ""


WELCOME_AR = (
    "👋 <b>أهلًا بك في Servixa Host</b>\n\n"
    "🐍 منصة استضافة سكربتات بايثون 24/7.\n\n"
    "اضغط على الزر أدناه لفتح <b>لوحة الاستضافة</b> ✨\n"
    "حيث تستطيع:\n"
    "• 📋 لصق الكود وتشغيله مباشرة\n"
    "• 📁 رفع ملف <code>.py</code> أو أرشيف <code>.zip</code>\n"
    "• 📦 تثبيت المكتبات (pip install)\n"
    "• ▶️ تشغيل / ⏹️ إيقاف بضغطة زر\n"
    "• 📜 متابعة السجلات والأخطاء بشكل حي"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = (user.first_name if user else None) or "صديقي"
    url = _webapp_url()
    if not url:
        await update.message.reply_text("البوت غير مهيأ بعد. (REPLIT_DOMAINS غير متوفر)")
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(text="🚀 فتح لوحة الاستضافة", web_app=WebAppInfo(url=url))],
    ])
    text = f"👤 مرحبًا <b>{name}</b>\n\n" + WELCOME_AR
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("أرسل /start لفتح لوحة الاستضافة.", parse_mode=ParseMode.HTML)


async def _post_init(application: Application) -> None:
    url = _webapp_url()
    if not url:
        return
    try:
        await application.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Open Panel", web_app=WebAppInfo(url=url))
        )
        log.info("Set default chat menu button to WebApp: %s", url)
    except Exception as e:
        log.warning("Could not set chat menu button: %s", e)


def run_bot(token: str) -> None:
    from telegram.request import HTTPXRequest
    backoff = 5
    while True:
        try:
            request = HTTPXRequest(
                connect_timeout=30.0,
                read_timeout=60.0,
                write_timeout=60.0,
                pool_timeout=30.0,
            )
            get_updates_request = HTTPXRequest(
                connect_timeout=30.0,
                read_timeout=60.0,
                write_timeout=60.0,
                pool_timeout=30.0,
            )
            app = (
                Application.builder()
                .token(token)
                .request(request)
                .get_updates_request(get_updates_request)
                .post_init(_post_init)
                .build()
            )
            app.add_handler(CommandHandler("start", cmd_start))
            app.add_handler(CommandHandler("help", cmd_help))
            log.info("Telegram bot polling…")
            app.run_polling(
                close_loop=False,
                stop_signals=None,
                drop_pending_updates=True,
                timeout=30,
            )
            break
        except Exception as e:
            log.error("Telegram bot crashed: %s — retrying in %ds", e, backoff)
            try:
                time.sleep(backoff)
            except Exception:
                pass
            backoff = min(backoff * 2, 60)


# =============================================================================
# Flask app + routes
# =============================================================================
app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
hosting = HostingManager(DATA_DIR)

# Load extra admins persisted in DB into ADMIN_IDS at startup
try:
    _extra = hosting._load_db().get("extra_admins", [])
    for _uid in _extra:
        ADMIN_IDS.add(str(_uid))
except Exception:
    pass


@app.after_request
def _no_cache(resp):
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


def _auth_user():
    init_data = request.headers.get("X-Init-Data", "")
    if not BOT_TOKEN:
        return None, "Server is not configured (missing TELEGRAM_BOT_TOKEN)."
    if not init_data:
        return None, "Missing Telegram authentication data."
    if not verify_init_data(init_data, BOT_TOKEN):
        return None, "Invalid Telegram authentication."
    user = parse_init_data(init_data)
    if not user or "id" not in user:
        return None, "Could not extract user identity."
    uid = str(user["id"])
    if hosting.admin_is_banned(uid):
        return None, "تم حظرك من استخدام هذه الخدمة."
    return uid, user


@app.get("/")
def index():
    return Response(EMBEDDED_HTML, mimetype="text/html; charset=utf-8")


@app.get("/static/app.css")
def static_css():
    return Response(EMBEDDED_CSS, mimetype="text/css; charset=utf-8")


@app.get("/static/app.js")
def static_js():
    return Response(EMBEDDED_JS, mimetype="application/javascript; charset=utf-8")


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.get("/api/me")
def api_me():
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    return jsonify({"user": user, "uid": uid, "is_admin": is_admin(uid)})


@app.get("/api/admin/overview")
def api_admin_overview():
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    if not is_admin(uid):
        return jsonify({"error": "Admins only."}), 403
    return jsonify({"ok": True, "overview": hosting.admin_overview()})


@app.post("/api/admin/stop_all")
def api_admin_stop_all():
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    if not is_admin(uid):
        return jsonify({"error": "Admins only."}), 403
    result = hosting.stop_all_files()
    log.info(
        "Admin %s triggered stop_all: scanned=%d stopped=%d failed=%d",
        uid, result.get("scanned", 0),
        result.get("stopped_count", 0),
        result.get("failed_count", 0),
    )
    return jsonify(result)


@app.get("/api/admin/files")
def api_admin_list_all_files():
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    if not is_admin(uid):
        return jsonify({"error": "Admins only."}), 403
    return jsonify({"files": hosting.admin_list_all_files()})


@app.get("/api/admin/files/<file_id>")
def api_admin_get_file(file_id):
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    if not is_admin(uid):
        return jsonify({"error": "Admins only."}), 403
    meta = hosting.admin_get_file(file_id)
    if not meta:
        return jsonify({"error": "File not found."}), 404
    code = hosting.admin_read_code(file_id)
    return jsonify({"file": meta, "code": code})


@app.get("/api/admin/files/<file_id>/logs")
def api_admin_logs(file_id):
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    if not is_admin(uid):
        return jsonify({"error": "Admins only."}), 403
    meta = hosting.admin_get_file(file_id)
    if not meta:
        return jsonify({"error": "File not found."}), 404
    logs_text = hosting.read_logs(file_id, tail=400)
    status = hosting.get_status(file_id)
    return jsonify({"logs": logs_text, "status": status})


@app.post("/api/admin/files/<file_id>/stop")
def api_admin_stop(file_id):
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    if not is_admin(uid):
        return jsonify({"error": "Admins only."}), 403
    ok = hosting.admin_stop_file(file_id)
    if not ok:
        return jsonify({"error": "File not found."}), 404
    log.info("Admin %s stopped file %s", uid, file_id)
    return jsonify({"ok": True})


@app.delete("/api/admin/files/<file_id>")
def api_admin_delete(file_id):
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    if not is_admin(uid):
        return jsonify({"error": "Admins only."}), 403
    ok = hosting.admin_delete_file(file_id)
    if not ok:
        return jsonify({"error": "File not found."}), 404
    log.info("Admin %s deleted file %s", uid, file_id)
    return jsonify({"ok": True})


@app.post("/api/admin/files/<file_id>/start")
def api_admin_start(file_id):
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    if not is_admin(uid):
        return jsonify({"error": "Admins only."}), 403
    result = hosting.admin_start_file(file_id)
    log.info("Admin %s started file %s: %s", uid, file_id, result)
    return jsonify(result)


@app.get("/api/admin/users")
def api_admin_users():
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    if not is_admin(uid):
        return jsonify({"error": "Admins only."}), 403
    return jsonify({"users": hosting.admin_list_users()})


@app.post("/api/admin/users/<target_uid>/ban")
def api_admin_ban(target_uid):
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    if not is_admin(uid):
        return jsonify({"error": "Admins only."}), 403
    if is_admin(target_uid):
        return jsonify({"error": "لا يمكن حظر مشرف آخر."}), 400
    body = request.get_json(silent=True) or {}
    reason = body.get("reason", "")
    hosting.admin_ban_user(target_uid, reason)
    log.info("Admin %s banned user %s reason=%s", uid, target_uid, reason)
    return jsonify({"ok": True})


@app.post("/api/admin/users/<target_uid>/unban")
def api_admin_unban(target_uid):
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    if not is_admin(uid):
        return jsonify({"error": "Admins only."}), 403
    hosting.admin_unban_user(target_uid)
    log.info("Admin %s unbanned user %s", uid, target_uid)
    return jsonify({"ok": True})


@app.delete("/api/admin/users/<target_uid>")
def api_admin_delete_user(target_uid):
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    if not is_admin(uid):
        return jsonify({"error": "Admins only."}), 403
    count = hosting.admin_delete_user_files(target_uid)
    log.info("Admin %s deleted all files of user %s (count=%d)", uid, target_uid, count)
    return jsonify({"ok": True, "deleted": count})


@app.get("/api/admin/system")
def api_admin_system():
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    if not is_admin(uid):
        return jsonify({"error": "Admins only."}), 403
    return jsonify({"system": hosting.admin_system_info()})


@app.post("/api/admin/admins/<target_uid>")
def api_admin_add_admin(target_uid):
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    if not is_admin(uid):
        return jsonify({"error": "Admins only."}), 403
    hosting.admin_add_admin(target_uid)
    log.info("Admin %s granted admin to %s", uid, target_uid)
    return jsonify({"ok": True})


@app.delete("/api/admin/admins/<target_uid>")
def api_admin_remove_admin(target_uid):
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    if not is_admin(uid):
        return jsonify({"error": "Admins only."}), 403
    if target_uid in DEFAULT_ADMIN_IDS.split(","):
        return jsonify({"error": "لا يمكن إزالة المشرف الأصلي."}), 400
    hosting.admin_remove_admin(target_uid)
    log.info("Admin %s revoked admin from %s", uid, target_uid)
    return jsonify({"ok": True})


@app.post("/api/admin/message/<target_uid>")
def api_admin_message_user(target_uid):
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    if not is_admin(uid):
        return jsonify({"error": "Admins only."}), 403
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "النص فارغ."}), 400
    async def _send():
        from telegram import Bot
        bot = Bot(token=BOT_TOKEN)
        await bot.send_message(chat_id=int(target_uid), text=text)
    import asyncio
    try:
        asyncio.run(_send())
        log.info("Admin %s messaged user %s", uid, target_uid)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/admin/broadcast")
def api_admin_broadcast():
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    if not is_admin(uid):
        return jsonify({"error": "Admins only."}), 403
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "النص فارغ."}), 400
    users = hosting.admin_list_users()
    async def _broadcast():
        from telegram import Bot
        bot = Bot(token=BOT_TOKEN)
        sent, failed = 0, 0
        for u in users:
            try:
                await bot.send_message(chat_id=int(u["uid"]), text=text)
                sent += 1
            except Exception:
                failed += 1
        return sent, failed
    import asyncio
    try:
        sent, failed = asyncio.run(_broadcast())
        log.info("Admin %s broadcast: sent=%d failed=%d", uid, sent, failed)
        return jsonify({"ok": True, "sent": sent, "failed": failed})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/admin/mode")
def api_admin_get_mode():
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    if not is_admin(uid):
        return jsonify({"error": "Admins only."}), 403
    return jsonify({
        "mode": hosting.get_mode(),
        "subscribed": hosting.list_subscribed(),
        "plans": hosting.get_plans(),
    })


@app.post("/api/admin/mode")
def api_admin_set_mode():
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    if not is_admin(uid):
        return jsonify({"error": "Admins only."}), 403
    body = request.get_json(silent=True) or {}
    mode = body.get("mode", "free")
    hosting.set_mode(mode)
    log.info("Admin %s set mode to %s", uid, mode)
    return jsonify({"ok": True, "mode": hosting.get_mode()})


@app.post("/api/admin/users/<target_uid>/subscribe")
def api_admin_subscribe_user(target_uid):
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    if not is_admin(uid):
        return jsonify({"error": "Admins only."}), 403
    body = request.get_json(silent=True) or {}
    days = int(body.get("days", -1))  # -1 = permanent
    hosting.subscribe_user(target_uid, days=days)
    log.info("Admin %s subscribed user %s (days=%s)", uid, target_uid, days)
    return jsonify({"ok": True, "days": days})


@app.delete("/api/admin/users/<target_uid>/subscribe")
def api_admin_unsubscribe_user(target_uid):
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    if not is_admin(uid):
        return jsonify({"error": "Admins only."}), 403
    hosting.unsubscribe_user(target_uid)
    log.info("Admin %s unsubscribed user %s", uid, target_uid)
    return jsonify({"ok": True})


@app.post("/api/admin/users/<target_uid>/points")
def api_admin_add_points(target_uid):
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    if not is_admin(uid):
        return jsonify({"error": "Admins only."}), 403
    body = request.get_json(silent=True) or {}
    try:
        amount = int(body.get("amount", 0))
    except (ValueError, TypeError):
        return jsonify({"error": "amount يجب أن يكون رقماً."}), 400
    if amount == 0:
        return jsonify({"error": "amount لا يمكن أن يكون صفراً."}), 400
    new_bal = hosting.add_points(target_uid, amount)
    log.info("Admin %s added %d points to user %s (new=%d)", uid, amount, target_uid, new_bal)
    return jsonify({"ok": True, "new_balance": new_bal})


@app.get("/api/plans")
def api_get_plans():
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    plans = hosting.get_plans()
    return jsonify({"plans": plans})


@app.post("/api/plans/<plan_id>/buy")
def api_buy_plan(plan_id):
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    try:
        result = hosting.buy_plan(uid, plan_id)
        log.info("User %s bought plan %s", uid, plan_id)
        return jsonify({"ok": True, **result})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/admin/plans/<plan_id>")
def api_admin_save_plan(plan_id):
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    if not is_admin(uid):
        return jsonify({"error": "Admins only."}), 403
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name مطلوب."}), 400
    try:
        hosting.admin_save_plan(
            plan_id=plan_id,
            name=name,
            points_cost=int(body.get("points_cost", 100)),
            duration_days=int(body.get("duration_days", 30)),
            description=(body.get("description") or "").strip(),
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.delete("/api/admin/plans/<plan_id>")
def api_admin_delete_plan(plan_id):
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    if not is_admin(uid):
        return jsonify({"error": "Admins only."}), 403
    hosting.admin_delete_plan(plan_id)
    return jsonify({"ok": True})


@app.get("/api/me/status")
def api_me_status():
    """Returns current mode, subscription, and points for the calling user."""
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    mode = hosting.get_mode()
    subscribed = hosting.is_subscribed(uid)
    expiry = hosting.get_subscription_expiry(uid)
    points = hosting.get_points(uid)
    admin = is_admin(uid)
    plans = hosting.get_plans()
    return jsonify({
        "mode": mode,
        "subscribed": subscribed,
        "sub_expiry": expiry,
        "points": points,
        "admin": admin,
        "can_upload": hosting.can_upload(uid),
        "plans": plans,
    })


@app.get("/api/files")
def api_files():
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    return jsonify({"files": hosting.list_files(uid)})


@app.post("/api/files")
def api_create_file():
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    if not hosting.can_upload(uid):
        return jsonify({"error": "🔒 الخدمة مدفوعة. تواصل مع المشرف للحصول على اشتراك.", "locked": True}), 403
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    code = body.get("code") or ""
    if not name:
        return jsonify({"error": "Name is required."}), 400
    if not name.endswith(".py"):
        name += ".py"
    try:
        meta = hosting.create_file(uid, name, code)
        return jsonify({"file": meta})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.put("/api/files/<file_id>")
def api_update_file(file_id):
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    body = request.get_json(silent=True) or {}
    code = body.get("code")
    name = body.get("name")
    try:
        meta = hosting.update_file(uid, file_id, code=code, name=name)
        return jsonify({"file": meta})
    except FileNotFoundError:
        return jsonify({"error": "File not found."}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.delete("/api/files/<file_id>")
def api_delete_file(file_id):
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    ok = hosting.delete_file(uid, file_id)
    if not ok:
        return jsonify({"error": "File not found."}), 404
    return jsonify({"ok": True})


@app.get("/api/files/<file_id>")
def api_get_file(file_id):
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    meta = hosting.get_file(uid, file_id)
    if not meta:
        return jsonify({"error": "File not found."}), 404
    code = hosting.read_code(uid, file_id)
    return jsonify({"file": meta, "code": code})


@app.post("/api/upload")
def api_upload():
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    if not hosting.can_upload(uid):
        return jsonify({"error": "🔒 الخدمة مدفوعة. تواصل مع المشرف للحصول على اشتراك.", "locked": True}), 403
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400
    f = request.files["file"]
    filename = f.filename or "upload"
    data = f.read()
    try:
        meta = hosting.upload_file(uid, filename, data)
        return jsonify({"file": meta})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.post("/api/files/<file_id>/start")
def api_start(file_id):
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    result = hosting.start_file(uid, file_id)
    if not result.get("ok"):
        return jsonify(result), 400
    return jsonify(result)


@app.post("/api/files/<file_id>/stop")
def api_stop(file_id):
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    ok = hosting.stop_file(uid, file_id)
    return jsonify({"ok": ok})


@app.post("/api/files/<file_id>/input")
def api_send_input(file_id):
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    body = request.get_json(silent=True) or {}
    text = body.get("text", "")
    if not isinstance(text, str):
        return jsonify({"error": "text must be a string"}), 400
    if len(text) > 4000:
        return jsonify({"error": "النص طويل جدًا"}), 400
    result = hosting.send_input(uid, file_id, text)
    if not result.get("ok"):
        return jsonify(result), 400
    return jsonify(result)


@app.get("/api/files/<file_id>/logs")
def api_logs(file_id):
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    meta = hosting.get_file(uid, file_id)
    if not meta:
        return jsonify({"error": "File not found."}), 404
    logs_text = hosting.read_logs(file_id, tail=400)
    status = hosting.get_status(file_id)
    missing = hosting.detect_missing_modules(logs_text)
    return jsonify({"logs": logs_text, "status": status, "missing_modules": missing})


@app.post("/api/install")
def api_install():
    uid, user = _auth_user()
    if uid is None:
        return jsonify({"error": user}), 401
    body = request.get_json(silent=True) or {}
    libs = body.get("libraries") or []
    if isinstance(libs, str):
        libs = [s.strip() for s in libs.replace(",", " ").split() if s.strip()]
    if not libs:
        return jsonify({"error": "No libraries provided."}), 400
    result = hosting.install_libraries(libs)
    code = 200 if result.get("success") else 400
    return jsonify(result), code


@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found."}), 404
    return Response(EMBEDDED_HTML, mimetype="text/html; charset=utf-8")


# =============================================================================
# Entry point
# =============================================================================
def _start_bot_thread():
    if not BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN is not set — Telegram bot will not start.")
        return
    t = threading.Thread(target=run_bot, args=(BOT_TOKEN,), daemon=True, name="telegram-bot")
    t.start()
    log.info("Telegram bot thread started.")


def main():
    log.info("Servixa Host starting on port %d", PORT)
    log.info("Data directory: %s", DATA_DIR)
    _start_ngrok_if_needed()
    _start_bot_thread()
    try:
        resumed = hosting.resume_all()
        if resumed:
            log.info("Resumed %d running script(s).", resumed)
    except Exception as e:
        log.warning("Resume on startup failed: %s", e)
    from werkzeug.serving import make_server
    server = make_server("0.0.0.0", PORT, app, threaded=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
