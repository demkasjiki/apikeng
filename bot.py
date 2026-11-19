#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import time
import uuid
import random
import threading
from datetime import datetime
from typing import Optional

import psutil
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from instagrapi import Client
from instagrapi.exceptions import BadPassword, TwoFactorRequired
from pydantic import BaseModel

# ======================== CONFIG ========================

PORT = int(os.environ.get("PORT", "8000"))
PANEL_TOKEN = os.environ.get("PANEL_TOKEN", "").strip()

DATA_DIR = os.environ.get("DATA_DIR", "/data" if os.path.isdir("/data") else "./data")
os.makedirs(DATA_DIR, exist_ok=True)
ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.json")
TEMPLATE_FILE = os.path.join(DATA_DIR, "template.json")

BASE_TEMPLATE = """<{TARGET}> Kɪ Mᴀᴀ Mᴏᴛɪ !< Gᴀɴᴅ Wᴀʟɪ> Rᴀɴᴅ!> -⚡-





<{TARGET}> Kɪ Mᴀᴀ Mᴏᴛɪ !< Gᴀɴᴅ Wᴀʟɪ> Rᴀɴᴅ!> -🌀-





<{TARGET}> Kɪ Mᴀᴀ Mᴏᴛɪ !< Gᴀɴᴅ Wᴀʟɪ> Rᴀɴᴅ!> -🪷-






<{TARGET}> Kɪ Mᴀᴀ Mᴏᴛɪ !< Gᴀɴᴅ Wᴀʟɪ> Rᴀɴᴅ!> -🌱-






<{TARGET}> Kɪ Mᴀᴀ Mᴏᴛɪ !< Gᴀɴᴅ Wᴀʟɪ> Rᴀɴᴅ!> -😂-






<{TARGET}> Kɪ Mᴀᴀ Mᴏᴛɪ !< Gᴀɴᴅ Wᴀʟɪ> Rᴀɴᴅ!> -🤣-






<{TARGET}> Kɪ Mᴀᴀ Mᴏᴛɪ !< Gᴀɴᴅ Wᴀʟɪ> Rᴀɴᴅ!> -🥳-






<{TARGET}> Kɪ Mᴀᴀ Mᴏᴛɪ !< Gᴀɴᴅ Wᴀʟɪ> Rᴀɴᴅ!> -❄️-






<{TARGET}> Kɪ Mᴀᴀ Mᴏᴛɪ !< Gᴀɴᴅ Wᴀʟɪ> Rᴀɴᴅ!> -☔-






<{TARGET}> Kɪ Mᴀᴀ Mᴏᴛɪ !< Gᴀɴᴅ Wᴀʟɪ> Rᴀɴᴅ!> -🌈-






<{TARGET}> Kɪ Mᴀᴀ Mᴏᴛɪ !< Gᴀɴᴅ Wᴀʟɪ> Rᴀɴᴅ!> -🌒-"""

MAX_RETRIES = 5

active_tasks: dict = {}
client_cache: dict = {}
pending_2fa: dict = {}
tasks_lock = threading.Lock()
store_lock = threading.Lock()
GLOBAL_MESSAGES_SENT = 0
BOT_START_TIME = datetime.now()

app = FastAPI(title="IG Web Controller")


# ======================== AUTH HELPER ========================

def verify_auth(x_panel_token: Optional[str] = None, token_query: Optional[str] = None):
    if not PANEL_TOKEN:
        return
    provided = (x_panel_token or token_query or "").strip()
    if provided != PANEL_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized: invalid or missing panel token")


# ======================== PERSISTENCE ========================

def load_saved_accounts() -> dict:
    with store_lock:
        if not os.path.exists(ACCOUNTS_FILE):
            return {}
        try:
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


def register_saved_account(username: str, ig_user_id=None, auth_method: str = "sessionid"):
    with store_lock:
        accounts = {}
        if os.path.exists(ACCOUNTS_FILE):
            try:
                with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        accounts = loaded
            except Exception:
                accounts = {}

        prev = accounts.get(username, {})
        accounts[username] = {
            "username": username,
            "ig_user_id": ig_user_id or prev.get("ig_user_id"),
            "auth_method": auth_method,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
            json.dump(accounts, f, indent=2)


def delete_saved_account(username: str):
    with store_lock:
        if os.path.exists(ACCOUNTS_FILE):
            try:
                with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                    accounts = json.load(f)
                if isinstance(accounts, dict) and username in accounts:
                    del accounts[username]
                    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
                        json.dump(accounts, f, indent=2)
            except Exception:
                pass
        session_path = os.path.join(DATA_DIR, f"ig_{username}.json")
        if os.path.exists(session_path):
            try:
                os.remove(session_path)
            except Exception:
                pass
    client_cache.pop(username, None)


def get_active_template() -> str:
    with store_lock:
        if os.path.exists(TEMPLATE_FILE):
            try:
                with open(TEMPLATE_FILE, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                tpl = payload.get("template")
                if isinstance(tpl, str) and tpl.strip():
                    return tpl
            except Exception:
                pass
        return BASE_TEMPLATE


def set_active_template(tpl: str):
    with store_lock:
        with open(TEMPLATE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {"template": tpl, "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                f,
                indent=2,
            )


def reset_active_template():
    with store_lock:
        if os.path.exists(TEMPLATE_FILE):
            try:
                os.remove(TEMPLATE_FILE)
            except Exception:
                pass


# ======================== IG HELPERS ========================

def clean_session_id(raw: str) -> str:
    val = raw.strip().strip('"').strip("'")
    if "sessionid=" in val:
        val = val.split("sessionid=", 1)[1].split(";", 1)[0].strip()
    return val


def resolve_username(client: Client, fallback: str = "") -> str:
    if fallback:
        return fallback.strip().lstrip("@")
    u = getattr(client, "username", None)
    if isinstance(u, str) and u.strip():
        return u.strip().lstrip("@")
    try:
        info = client.account_info()
        if getattr(info, "username", None):
            return str(info.username).strip().lstrip("@")
    except Exception:
        pass
    return f"user_{getattr(client, 'user_id', 'session')}"


def save_session(client: Client, username: str, auth_method: str):
    session_path = os.path.join(DATA_DIR, f"ig_{username}.json")
    client.dump_settings(session_path)
    register_saved_account(username, getattr(client, "user_id", None), auth_method=auth_method)
    client_cache[username] = client


def get_or_resume_client(username: str) -> Client:
    if username in client_cache:
        return client_cache[username]
    session_path = os.path.join(DATA_DIR, f"ig_{username}.json")
    if not os.path.exists(session_path):
        raise RuntimeError(f"Saved session for @{username} not found.")
    cl = Client()
    cl.delay_range = [0, 0]
    cl.load_settings(session_path)
    cl.account_info()
    cl.dump_settings(session_path)
    client_cache[username] = cl
    return cl


def list_threads(client: Client):
    threads = client.direct_threads(amount=20)
    out = []
    for t in threads:
        if getattr(t, "thread_title", None):
            title = t.thread_title
        elif getattr(t, "users", None):
            names = [u.username for u in t.users if getattr(u, "username", None)]
            title = ", ".join(names) if names else "Direct Chat"
        else:
            title = "Thread"
        tag = "GC" if getattr(t, "is_group", False) else "DM"
        out.append({"id": str(t.id), "label": f"[{tag}] {title[:36]}"})
    return out


# ======================== WORKER LOOP ========================

def is_task_running(task_id: str) -> bool:
    with tasks_lock:
        return bool(active_tasks.get(task_id, {}).get("running", False))


def set_task_running(task_id: str, running: bool):
    with tasks_lock:
        if task_id in active_tasks:
            active_tasks[task_id]["running"] = running


def log_task(task_id: str, msg: str):
    with tasks_lock:
        task = active_tasks.get(task_id)
        if not task:
            return
        task["last_event"] = msg
        task["last_update"] = datetime.now().strftime("%H:%M:%S")
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {task_id}: {msg}", flush=True)


def generate_random_band():
    low = random.uniform(25, 45)
    spread = random.uniform(5, 15)
    return (low, min(low + spread, 60))


def worker_loop(task_id: str):
    global GLOBAL_MESSAGES_SENT
    with tasks_lock:
        task = active_tasks.get(task_id)
        if not task:
            return
        cl: Client = task["client"]
        thread_ids = list(task["thread_ids"])
        target_name = task["target_name"]

    msg_template = get_active_template().replace("{TARGET}", target_name)
    band = generate_random_band()
    last_band_change = time.time()
    band_interval = random.uniform(3600, 7200)
    round_num = 0

    while is_task_running(task_id):
        try:
            round_num += 1
            log_task(task_id, f"Round {round_num} started")
            for tid in thread_ids:
                if not is_task_running(task_id):
                    break

                num = random.randint(1, 10**10)
                payload = (
                    f"{num}\n\n{msg_template}"
                    if random.choice([True, False])
                    else f"{msg_template}\n\n{num}"
                )

                sent = False
                for attempt in range(1, MAX_RETRIES + 1):
                    if not is_task_running(task_id):
                        break
                    try:
                        cl.direct_send(payload, thread_ids=[int(tid)])
                        with tasks_lock:
                            GLOBAL_MESSAGES_SENT += 1
                            if task_id in active_tasks:
                                active_tasks[task_id]["msg_count"] += 1
                        log_task(task_id, f"Sent to {tid}")
                        sent = True
                        break
                    except Exception as exc:
                        wait = 30 * attempt
                        log_task(task_id, f"Retry {attempt}/{MAX_RETRIES} ({str(exc)[:35]}) in {wait}s")
                        time.sleep(wait)

                if not sent and is_task_running(task_id):
                    log_task(task_id, f"Skipped thread {tid} after retries")

                delay = random.uniform(band[0], band[1])
                log_task(task_id, f"Cooldown {delay:.1f}s")
                slept = 0.0
                while slept < delay and is_task_running(task_id):
                    step = min(1.0, delay - slept)
                    time.sleep(step)
                    slept += step

                if time.time() - last_band_change >= band_interval:
                    band = generate_random_band()
                    last_band_change = time.time()
                    band_interval = random.uniform(3600, 7200)
        except Exception as exc:
            log_task(task_id, f"Loop error: {str(exc)[:40]} (resting 60s)")
            time.sleep(60)

    log_task(task_id, "Stopped")


# ======================== REQUEST SCHEMAS ========================

class SessionLoginReq(BaseModel):
    session_id: str


class PasswordLoginReq(BaseModel):
    username: str
    password: str
    two_factor_code: Optional[str] = None


class StartTaskReq(BaseModel):
    username: str
    thread_ids: list[int]
    target_name: str


class TemplateReq(BaseModel):
    template: str


# ======================== API ROUTES ========================

@app.get("/api/state")
def api_state(x_panel_token: Optional[str] = Header(default=None), token: Optional[str] = None):
    verify_auth(x_panel_token, token)
    cpu = psutil.cpu_percent(interval=None)
    ram = psutil.virtual_memory()
    proc_mb = psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)

    with tasks_lock:
        tasks_view = [
            {
                "task_id": tid,
                "username": info.get("username"),
                "target_name": info.get("target_name"),
                "thread_ids": info.get("thread_ids"),
                "running": info.get("running"),
                "msg_count": info.get("msg_count", 0),
                "last_event": info.get("last_event", ""),
                "last_update": info.get("last_update", ""),
            }
            for tid, info in active_tasks.items()
        ]
        running_count = sum(1 for t in active_tasks.values() if t.get("running"))

    return {
        "accounts": load_saved_accounts(),
        "template": get_active_template(),
        "is_custom_template": os.path.exists(TEMPLATE_FILE),
        "tasks": tasks_view,
        "global_messages_sent": GLOBAL_MESSAGES_SENT,
        "uptime": str(datetime.now() - BOT_START_TIME).split(".")[0],
        "stats": {
            "cpu_percent": round(cpu, 1),
            "ram_percent": round(ram.percent, 1),
            "ram_used_gb": round(ram.used / (1024**3), 2),
            "ram_total_gb": round(ram.total / (1024**3), 2),
            "rss_mb": round(proc_mb, 1),
            "running_tasks": running_count,
        },
    }


@app.post("/api/login/session")
def api_login_session(
    body: SessionLoginReq,
    x_panel_token: Optional[str] = Header(default=None),
    token: Optional[str] = None,
):
    verify_auth(x_panel_token, token)
    cleaned = clean_session_id(body.session_id)
    if not cleaned:
        raise HTTPException(status_code=400, detail="Session ID is empty.")
    try:
        cl = Client()
        cl.delay_range = [0, 0]
        cl.login_by_sessionid(cleaned)
        if not getattr(cl, "user_id", None):
            info = cl.account_info()
            if getattr(info, "pk", None):
                cl.user_id = int(info.pk)
        if not getattr(cl, "user_id", None):
            raise RuntimeError("Could not verify Instagram user ID.")
        uname = resolve_username(cl)
        save_session(cl, uname, "sessionid")
        return {"ok": True, "username": uname, "user_id": cl.user_id}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {str(exc)[:180]}")


@app.post("/api/login/password")
def api_login_password(
    body: PasswordLoginReq,
    x_panel_token: Optional[str] = Header(default=None),
    token: Optional[str] = None,
):
    verify_auth(x_panel_token, token)
    uname = body.username.strip().lstrip("@")
    if not uname or not body.password:
        raise HTTPException(status_code=400, detail="Username and password are required.")

    try:
        if body.two_factor_code and uname in pending_2fa:
            cl = pending_2fa[uname]["client"]
            cl.login(uname, body.password, verification_code=body.two_factor_code.strip())
            pending_2fa.pop(uname, None)
            save_session(cl, uname, "password+2fa")
            return {"ok": True, "username": uname, "user_id": cl.user_id}

        cl = Client()
        cl.delay_range = [0, 0]
        cl.login(uname, body.password)
        save_session(cl, uname, "password")
        return {"ok": True, "username": uname, "user_id": cl.user_id}
    except TwoFactorRequired:
        pending_2fa[uname] = {"client": cl, "password": body.password}
        return JSONResponse(
            status_code=202,
            content={"two_factor_required": True, "username": uname, "detail": "Enter 6-digit 2FA code."},
        )
    except BadPassword:
        raise HTTPException(status_code=401, detail="Incorrect Instagram password.")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {str(exc)[:180]}")


@app.delete("/api/accounts/{username}")
def api_delete_account(
    username: str,
    x_panel_token: Optional[str] = Header(default=None),
    token: Optional[str] = None,
):
    verify_auth(x_panel_token, token)
    delete_saved_account(username)
    return {"ok": True, "removed": username}


@app.get("/api/threads/{username}")
def api_threads(
    username: str,
    x_panel_token: Optional[str] = Header(default=None),
    token: Optional[str] = None,
):
    verify_auth(x_panel_token, token)
    try:
        cl = get_or_resume_client(username)
        return {"ok": True, "threads": list_threads(cl)}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {str(exc)[:180]}")


@app.post("/api/tasks/start")
def api_start_task(
    body: StartTaskReq,
    x_panel_token: Optional[str] = Header(default=None),
    token: Optional[str] = None,
):
    verify_auth(x_panel_token, token)
    uname = body.username.strip().lstrip("@")
    target = body.target_name.strip()
    tids = [int(x) for x in body.thread_ids if int(x) > 0]

    if not uname or not target or not tids:
        raise HTTPException(status_code=400, detail="Username, target name, and at least 1 thread ID required.")

    try:
        cl = get_or_resume_client(uname)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    task_id = f"T{uuid.uuid4().hex[:6].upper()}"
    with tasks_lock:
        active_tasks[task_id] = {
            "client": cl,
            "username": uname,
            "thread_ids": tids,
            "target_name": target,
            "running": True,
            "msg_count": 0,
            "last_event": "Queued",
            "last_update": datetime.now().strftime("%H:%M:%S"),
        }

    threading.Thread(target=worker_loop, args=(task_id,), daemon=True).start()
    return {"ok": True, "task_id": task_id}


@app.post("/api/tasks/{task_id}/stop")
def api_stop_task(
    task_id: str,
    x_panel_token: Optional[str] = Header(default=None),
    token: Optional[str] = None,
):
    verify_auth(x_panel_token, token)
    set_task_running(task_id, False)
    return {"ok": True, "task_id": task_id}


@app.post("/api/tasks/stop-all")
def api_stop_all(x_panel_token: Optional[str] = Header(default=None), token: Optional[str] = None):
    verify_auth(x_panel_token, token)
    with tasks_lock:
        for info in active_tasks.values():
            info["running"] = False
    return {"ok": True}


@app.post("/api/template")
def api_set_template(
    body: TemplateReq,
    x_panel_token: Optional[str] = Header(default=None),
    token: Optional[str] = None,
):
    verify_auth(x_panel_token, token)
    if not body.template.strip():
        raise HTTPException(status_code=400, detail="Template cannot be empty.")
    set_active_template(body.template)
    return {"ok": True}


@app.post("/api/template/reset")
def api_reset_template(x_panel_token: Optional[str] = Header(default=None), token: Optional[str] = None):
    verify_auth(x_panel_token, token)
    reset_active_template()
    return {"ok": True, "template": get_active_template()}


# ======================== WEB UI ========================

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>IG Web Controller</title>
  <style>
    :root {
      --bg: #0f1117;
      --card: #181c24;
      --border: #2a303c;
      --text: #e6edf3;
      --muted: #8b949e;
      --accent: #3b82f6;
      --green: #22c55e;
      --red: #ef4444;
      --amber: #f59e0b;
    }
    * { box-sizing: border-box; font-family: system-ui, -apple-system, sans-serif; }
    body { margin: 0; background: var(--bg); color: var(--text); padding: 20px; }
    .wrap { max-width: 1050px; margin: 0 auto; display: grid; gap: 16px; }
    .top { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 12px; }
    .grid-2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; }
    .card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; }
    h1, h2, h3 { margin: 0 0 10px; }
    h1 { font-size: 1.35rem; }
    h2 { font-size: 1.05rem; }
    label { display: block; font-size: 0.85rem; color: var(--muted); margin: 10px 0 4px; }
    input, textarea, select {
      width: 100%; background: #0d1017; color: var(--text); border: 1px solid var(--border);
      border-radius: 8px; padding: 9px 11px; font-size: 0.92rem;
    }
    textarea { min-height: 110px; font-family: monospace; }
    button {
      background: var(--accent); color: #fff; border: 0; border-radius: 8px;
      padding: 9px 14px; font-weight: 600; cursor: pointer; font-size: 0.88rem;
    }
    button:hover { opacity: 0.92; }
    button.secondary { background: #262c3a; color: var(--text); }
    button.danger { background: var(--red); }
    button.warn { background: var(--amber); color: #111; }
    .row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; align-items: center; }
    .badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 0.78rem; font-weight: 600; }
    .on { background: rgba(34,197,94,0.16); color: var(--green); }
    .off { background: rgba(239,68,68,0.16); color: var(--red); }
    .muted { color: var(--muted); font-size: 0.85rem; }
    .list-item { border-top: 1px solid var(--border); padding: 10px 0; display: flex; justify-content: space-between; align-items: center; gap: 10px; }
    .list-item:first-child { border-top: 0; }
    .toast { position: fixed; bottom: 18px; right: 18px; background: #1f2937; border: 1px solid var(--border); padding: 10px 14px; border-radius: 8px; display: none; z-index: 50; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <div>
        <h1>Instagram Automation Web Panel</h1>
        <div class="muted" id="statsBar">Loading stats...</div>
      </div>
      <div class="row" style="margin:0;">
        <input id="tokenInput" type="password" placeholder="Panel Token (if set)" style="width:190px;" />
        <button class="secondary" onclick="saveToken()">Save Token</button>
        <button class="danger" onclick="stopAll()">Stop All</button>
      </div>
    </div>

    <div class="grid-2">
      <div class="card">
        <h2>1. Login & Saved Accounts</h2>
        <label>Login via Session ID</label>
        <input id="sidInput" type="password" placeholder="Paste sessionid cookie value" />
        <div class="row"><button onclick="loginSession()">Login with Session ID</button></div>

        <label style="margin-top:16px;">Or Login with Username & Password</label>
        <input id="igUser" placeholder="Instagram username" />
        <input id="igPass" type="password" placeholder="Instagram password" style="margin-top:6px;" />
        <div id="tfaBox" style="display:none;">
          <input id="ig2fa" placeholder="6-digit 2FA code" style="margin-top:6px;" />
        </div>
        <div class="row"><button class="secondary" onclick="loginPassword()">Login with Password</button></div>

        <h3 style="margin-top:16px; font-size:0.95rem;">Saved Accounts</h3>
        <div id="accountsList" class="muted">None yet.</div>
      </div>

      <div class="card">
        <h2>2. Start Task</h2>
        <label>Account</label>
        <select id="taskAccount"></select>

        <div class="row">
          <button class="secondary" onclick="fetchThreads()">Load Recent Chats</button>
          <span class="muted" id="threadStatus"></span>
        </div>

        <label>Thread IDs (comma-separated or pick below)</label>
        <input id="threadIdsInput" placeholder="e.g. 178412345, 178998877" />
        <div id="threadPicker" class="row"></div>

        <label>Target Name ({TARGET})</label>
        <input id="targetInput" placeholder="Enter target name" />

        <div class="row"><button onclick="startTask()">Start Task</button></div>
      </div>
    </div>

    <div class="grid-2">
      <div class="card">
        <h2>3. Active Tasks</h2>
        <div id="tasksList" class="muted">No tasks yet.</div>
      </div>

      <div class="card">
        <h2>4. Message Template</h2>
        <div class="muted">Use <code>{TARGET}</code> where target name should be inserted.</div>
        <textarea id="templateArea"></textarea>
        <div class="row">
          <button onclick="saveTemplate()">Save Template</button>
          <button class="warn" onclick="resetTemplate()">Reset Default</button>
        </div>
      </div>
    </div>
  </div>

  <div class="toast" id="toast"></div>

  <script>
    const TOKEN_KEY = "ig_panel_token";
    document.getElementById("tokenInput").value = localStorage.getItem(TOKEN_KEY) || "";

    function showToast(msg, err = false) {
      const el = document.getElementById("toast");
      el.textContent = msg;
      el.style.borderColor = err ? "#ef4444" : "#22c55e";
      el.style.display = "block";
      clearTimeout(window._t);
      window._t = setTimeout(() => { el.style.display = "none"; }, 3200);
    }

    function saveToken() {
      localStorage.setItem(TOKEN_KEY, document.getElementById("tokenInput").value.trim());
      showToast("Token saved");
      refreshState();
    }

    async function api(path, method = "GET", body = null) {
      const tok = localStorage.getItem(TOKEN_KEY) || "";
      const headers = { "Content-Type": "application/json" };
      if (tok) headers["X-Panel-Token"] = tok;
      const res = await fetch(path, {
        method,
        headers,
        body: body ? JSON.stringify(body) : null,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok && res.status !== 202) {
        throw new Error(data.detail || `Request failed (${res.status})`);
      }
      return { status: res.status, data };
    }

    async function refreshState() {
      try {
        const { data } = await api("/api/state");
        const s = data.stats;
        document.getElementById("statsBar").textContent =
          `CPU ${s.cpu_percent}% · RAM ${s.ram_percent}% (${s.ram_used_gb}/${s.ram_total_gb} GB) · RSS ${s.rss_mb} MB · Sent ${data.global_messages_sent} · Uptime ${data.uptime}`;

        const accs = Object.keys(data.accounts || {});
        const sel = document.getElementById("taskAccount");
        const prev = sel.value;
        sel.innerHTML = accs.map(u => `<option value="${u}">@${u}</option>`).join("");
        if (accs.includes(prev)) sel.value = prev;

        const accBox = document.getElementById("accountsList");
        if (!accs.length) {
          accBox.innerHTML = `<div class="muted">No saved accounts yet.</div>`;
        } else {
          accBox.innerHTML = accs.map(u => {
            const m = data.accounts[u];
            return `<div class="list-item">
              <div><strong>@${u}</strong> <span class="muted">(${m.auth_method || "saved"})</span></div>
              <button class="secondary" onclick="deleteAccount('${u}')">Remove</button>
            </div>`;
          }).join("");
        }

        const tBox = document.getElementById("tasksList");
        if (!data.tasks || !data.tasks.length) {
          tBox.innerHTML = `<div class="muted">No tasks created yet.</div>`;
        } else {
          tBox.innerHTML = data.tasks.map(t => `
            <div class="list-item">
              <div>
                <div><strong>${t.task_id}</strong> · @${t.username} → <strong>${t.target_name}</strong>
                  <span class="badge ${t.running ? "on" : "off"}">${t.running ? "RUNNING" : "STOPPED"}</span>
                </div>
                <div class="muted">Sent: ${t.msg_count} · ${t.last_event || "Idle"} (${t.last_update || ""})</div>
              </div>
              ${t.running ? `<button class="danger" onclick="stopTask('${t.task_id}')">Stop</button>` : ""}
            </div>
          `).join("");
        }

        const tplEl = document.getElementById("templateArea");
        if (document.activeElement !== tplEl) {
          tplEl.value = data.template || "";
        }
      } catch (e) {
        document.getElementById("statsBar").textContent = e.message;
      }
    }

    async function loginSession() {
      try {
        const sid = document.getElementById("sidInput").value.trim();
        const { data } = await api("/api/login/session", "POST", { session_id: sid });
        document.getElementById("sidInput").value = "";
        showToast(`Logged in as @${data.username}`);
        refreshState();
      } catch (e) { showToast(e.message, true); }
    }

    async function loginPassword() {
      try {
        const username = document.getElementById("igUser").value.trim();
        const password = document.getElementById("igPass").value;
        const two_factor_code = document.getElementById("ig2fa").value.trim() || null;
        const { status, data } = await api("/api/login/password", "POST", { username, password, two_factor_code });
        if (status === 202 && data.two_factor_required) {
          document.getElementById("tfaBox").style.display = "block";
          showToast("2FA code required—enter it and click again", true);
          return;
        }
        document.getElementById("igPass").value = "";
        document.getElementById("ig2fa").value = "";
        document.getElementById("tfaBox").style.display = "none";
        showToast(`Logged in as @${data.username}`);
        refreshState();
      } catch (e) { showToast(e.message, true); }
    }

    async function deleteAccount(u) {
      try {
        await api(`/api/accounts/${encodeURIComponent(u)}`, "DELETE");
        showToast(`Removed @${u}`);
        refreshState();
      } catch (e) { showToast(e.message, true); }
    }

    function toggleThreadId(id) {
      const input = document.getElementById("threadIdsInput");
      const list = input.value.split(",").map(s => s.trim()).filter(Boolean);
      const idx = list.indexOf(String(id));
      if (idx >= 0) list.splice(idx, 1); else list.push(String(id));
      input.value = list.join(", ");
    }

    async function fetchThreads() {
      const u = document.getElementById("taskAccount").value;
      if (!u) return showToast("Select an account first", true);
      document.getElementById("threadStatus").textContent = "Loading...";
      try {
        const { data } = await api(`/api/threads/${encodeURIComponent(u)}`);
        const picker = document.getElementById("threadPicker");
        picker.innerHTML = (data.threads || []).map(t =>
          `<button class="secondary" onclick="toggleThreadId('${t.id}')">${t.label} (${t.id})</button>`
        ).join("");
        document.getElementById("threadStatus").textContent = `${(data.threads || []).length} loaded`;
      } catch (e) {
        document.getElementById("threadStatus").textContent = "";
        showToast(e.message, true);
      }
    }

    async function startTask() {
      try {
        const username = document.getElementById("taskAccount").value;
        const target_name = document.getElementById("targetInput").value.trim();
        const thread_ids = document.getElementById("threadIdsInput").value
          .split(",").map(s => parseInt(s.trim(), 10)).filter(n => !isNaN(n) && n > 0);
        const { data } = await api("/api/tasks/start", "POST", { username, thread_ids, target_name });
        showToast(`Started task ${data.task_id}`);
        refreshState();
      } catch (e) { showToast(e.message, true); }
    }

    async function stopTask(id) {
      try {
        await api(`/api/tasks/${encodeURIComponent(id)}/stop`, "POST");
        showToast(`Stopped ${id}`);
        refreshState();
      } catch (e) { showToast(e.message, true); }
    }

    async function stopAll() {
      try {
        await api("/api/tasks/stop-all", "POST");
        showToast("Stopped all tasks");
        refreshState();
      } catch (e) { showToast(e.message, true); }
    }

    async function saveTemplate() {
      try {
        const template = document.getElementById("templateArea").value;
        await api("/api/template", "POST", { template });
        showToast("Template saved");
      } catch (e) { showToast(e.message, true); }
    }

    async function resetTemplate() {
      try {
        await api("/api/template/reset", "POST");
        showToast("Template reset to default");
        refreshState();
      } catch (e) { showToast(e.message, true); }
    }

    refreshState();
    setInterval(refreshState, 4000);
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return HTMLResponse(content=HTML_PAGE)


if __name__ == "__main__":
    print(f"🌐 Web panel listening on 0.0.0.0:{PORT} (DATA_DIR={DATA_DIR})", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
