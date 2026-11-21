#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
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
from instagrapi.exceptions import BadPassword, ClientNeedsUpgradeError, TwoFactorRequired
from pydantic import BaseModel

# ======================== CONFIG ========================

PORT = int(os.environ.get("PORT", "8000"))
PANEL_TOKEN = os.environ.get("PANEL_TOKEN", "").strip()

IG_APP_VERSION = os.environ.get("IG_APP_VERSION", "355.0.0.47.96").strip()
IG_VERSION_CODE = os.environ.get("IG_VERSION_CODE", "656018442").strip()
IG_USER_AGENT = os.environ.get(
    "IG_USER_AGENT",
    f"Instagram {IG_APP_VERSION} Android (34/14; 480dpi; 1080x2400; samsung; SM-S918B; dm3q; qcom; en_US; {IG_VERSION_CODE})",
).strip()

DATA_DIR = os.environ.get("DATA_DIR", "/data" if os.path.isdir("/data") else "./data")
os.makedirs(DATA_DIR, exist_ok=True)
ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.json")
TEMPLATE_FILE = os.path.join(DATA_DIR, "template.json")

DEFAULT_SINGLE_LINE_TEMPLATE = "<{TARGET}> Kɪ Mᴀᴀ Mᴏᴛɪ !< Gᴀɴᴅ Wᴀʟɪ> Rᴀɴᴅ!> -🌒-"
DEFAULT_GROUP_TITLE_TEMPLATE = "{TARGET}"
# 11 lines matching the exact line count and blank-line gaps of the original BASE_TEMPLATE:
# 5 blank lines after lines 1, 2, 3; 6 blank lines after lines 4..10; no trailing blank lines after line 11.
LINE_GAPS_AFTER = [6, 6, 6, 7, 7, 7, 7, 7, 7, 7, 1]

EMOJI_POOL = [
    "🌒", "⚡", "🌀", "🪷", "🌱", "😂", "🤣", "🥳",
    "❄️", "☔", "🌈", "🔥", "💀", "🤡", "😈", "👻",
]

SUBTLE_LINK_SLUGS = [
    "r", "x", "q", "v", "k", "m", "n", "t",
]

MAX_RETRIES = 5

active_tasks: dict = {}
group_creator_jobs: dict = {}
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


def register_saved_account(username: str, ig_user_id=None, auth_method: str = "password"):
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
            "ig_user_id": str(ig_user_id) if ig_user_id is not None else prev.get("ig_user_id"),
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


def _read_template_payload() -> dict:
    if os.path.exists(TEMPLATE_FILE):
        try:
            with open(TEMPLATE_FILE, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass
    return {}


def get_active_single_line_template() -> str:
    with store_lock:
        payload = _read_template_payload()
        tpl = payload.get("template")
        if isinstance(tpl, str) and tpl.strip():
            lines = [ln.strip() for ln in tpl.splitlines() if ln.strip()]
            if lines:
                return lines[0]
        return DEFAULT_SINGLE_LINE_TEMPLATE


def get_active_group_title_template() -> str:
    with store_lock:
        payload = _read_template_payload()
        gt = payload.get("group_title_template")
        if isinstance(gt, str) and gt.strip():
            return gt.strip()
        return DEFAULT_GROUP_TITLE_TEMPLATE


def set_active_template(tpl: str, group_title_template: Optional[str] = None):
    lines = [ln.strip() for ln in tpl.splitlines() if ln.strip()]
    if not lines:
        raise ValueError("Template cannot be empty.")
    single_line = lines[0]
    with store_lock:
        existing = _read_template_payload()
        gt = (
            group_title_template.strip()
            if isinstance(group_title_template, str) and group_title_template.strip()
            else existing.get("group_title_template") or DEFAULT_GROUP_TITLE_TEMPLATE
        )
        with open(TEMPLATE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "template": single_line,
                    "group_title_template": gt,
                    "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                },
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


def render_single_line_with_random_emoji(base_line: str, target_name: str) -> str:
    chosen_emoji = random.choice(EMOJI_POOL)
    rendered = base_line.replace("{TARGET}", target_name).replace("{EMOJI}", chosen_emoji)
    if "-🌒-" in rendered:
        return rendered.replace("-🌒-", f"-{chosen_emoji}-")
    if re.search(r"-[^\s-]+-\s*$", rendered):
        return re.sub(r"-[^\s-]+-(\s*)$", f"-{chosen_emoji}-\\1", rendered)
    return f"{rendered} -{chosen_emoji}-"


def build_expanded_message(single_line_template: str, target_name: str) -> str:
    parts = []
    for idx, newlines_after in enumerate(LINE_GAPS_AFTER):
        parts.append(render_single_line_with_random_emoji(single_line_template, target_name))
        if idx < len(LINE_GAPS_AFTER) - 1:
            parts.append("\n" * newlines_after)
    return "".join(parts)


def make_subtle_z_link() -> str:
    slug = random.choice(SUBTLE_LINK_SLUGS)
    suffix = uuid.uuid4().hex[:3]
    return f"https://z.in/{slug}{suffix}"


# ======================== IG HELPERS ========================

def make_ig_client() -> Client:
    cl = Client()
    cl.delay_range = [0, 0]
    try:
        if IG_APP_VERSION:
            cl.set_app_version(IG_APP_VERSION)
    except Exception:
        pass
    try:
        if IG_USER_AGENT:
            cl.set_user_agent(IG_USER_AGENT)
    except Exception:
        pass
    if IG_VERSION_CODE:
        try:
            cl.version_code = IG_VERSION_CODE
        except Exception:
            pass
    return cl


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
    cl = make_ig_client()
    cl.load_settings(session_path)
    cl.account_info()
    cl.dump_settings(session_path)
    client_cache[username] = cl
    return cl


def list_threads(client: Client):
    threads = client.direct_threads(amount=25)
    out = []
    for t in threads:
        tid_str = str(t.id).strip()
        if getattr(t, "thread_title", None):
            title = str(t.thread_title)
        elif getattr(t, "users", None):
            names = [u.username for u in t.users if getattr(u, "username", None)]
            title = ", ".join(names) if names else "Direct Chat"
        else:
            title = "Thread"
        is_group = bool(getattr(t, "is_group", False))
        tag = "GC" if is_group else "DM"
        out.append({
            "id": tid_str,
            "is_group": is_group,
            "label": f"[{tag}] {title[:32]}",
        })
    return out


def parse_thread_ids(raw_ids) -> list[str]:
    if not raw_ids:
        return []
    result = []
    for item in raw_ids:
        s = str(item).strip()
        if s.isdigit() and s not in result:
            result.append(s)
    return result


def parse_usernames(raw_usernames: Optional[list]) -> list[str]:
    if not raw_usernames:
        return []
    out = []
    for item in raw_usernames:
        clean = str(item).strip().lstrip("@").lower()
        if clean and clean not in out:
            out.append(clean)
    return out


def resolve_usernames_to_ids(cl: Client, usernames: list[str]) -> tuple[list[int], list[str]]:
    resolved_ids: list[int] = []
    failed: list[str] = []
    my_uid = int(getattr(cl, "user_id", 0) or 0)

    for idx, uname in enumerate(usernames):
        try:
            uid_str = cl.user_id_from_username(uname)
            uid_int = int(str(uid_str).strip())
            if uid_int > 0 and uid_int != my_uid and uid_int not in resolved_ids:
                resolved_ids.append(uid_int)
        except Exception as exc:
            failed.append(f"@{uname} ({str(exc)[:30]})")
        if idx < len(usernames) - 1:
            time.sleep(random.uniform(1.2, 2.5))

    return resolved_ids, failed


def _find_invite_url_in_obj(obj) -> Optional[str]:
    if obj is None:
        return None
    if isinstance(obj, str):
        m = re.search(r"https?://(?:www\.)?ig\.me/j/[A-Za-z0-9_-]+/?", obj)
        if m:
            return m.group(0)
        m2 = re.search(r"https?://(?:www\.)?instagram\.com/j/[A-Za-z0-9_-]+/?", obj)
        if m2:
            return m2.group(0)
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if ("invite" in kl or "join" in kl or "url" in kl or "link" in kl) and isinstance(v, str):
                found = _find_invite_url_in_obj(v)
                if found:
                    return found
            found = _find_invite_url_in_obj(v)
            if found:
                return found
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            found = _find_invite_url_in_obj(item)
            if found:
                return found
    else:
        d = getattr(obj, "__dict__", None)
        if isinstance(d, dict):
            return _find_invite_url_in_obj(d)
    return None


def extract_group_invite_link(cl: Client, tid_str: str) -> dict:
    tid_int = int(tid_str)
    title = f"Thread {tid_str}"
    is_group = True

    try:
        th = cl.direct_thread(tid_int, amount=1)
        if getattr(th, "thread_title", None):
            title = str(th.thread_title)
        is_group = bool(getattr(th, "is_group", True))
        link = _find_invite_url_in_obj(th)
        if link:
            return {"thread_id": tid_str, "title": title, "is_group": is_group, "invite_link": link, "status": "ok"}
    except Exception:
        pass

    for endpoint, method in [
        (f"direct_v2/threads/{tid_int}/get_invite_link/", "GET"),
        (f"direct_v2/threads/{tid_int}/create_invite_link/", "POST"),
        (f"direct_v2/threads/{tid_int}/invite_link/", "GET"),
    ]:
        try:
            if hasattr(cl, "private_request"):
                data = (
                    cl.private_request(endpoint, data={})
                    if method == "POST"
                    else cl.private_request(endpoint)
                )
                link = _find_invite_url_in_obj(data)
                if link:
                    return {
                        "thread_id": tid_str,
                        "title": title,
                        "is_group": is_group,
                        "invite_link": link,
                        "status": "ok",
                    }
        except Exception:
            continue

    return {
        "thread_id": tid_str,
        "title": title,
        "is_group": is_group,
        "invite_link": None,
        "status": "unavailable (enable invite link on mobile or verify group admin rights)",
    }


def _create_single_group(cl: Client, title: str, member_user_ids: list[int], send_ping: bool) -> str:
    my_uid = int(getattr(cl, "user_id", 0) or 0)
    participants = [uid for uid in member_user_ids if uid and uid != my_uid]
    if len(participants) < 2:
        raise RuntimeError("Instagram requires at least 2 valid other usernames to create a group thread.")
    if my_uid and my_uid not in participants:
        participants.insert(0, my_uid)

    created = cl.direct_thread_create(participants, title=title[:75])
    if hasattr(created, "id"):
        tid_str = str(created.id).strip()
    elif isinstance(created, dict) and created.get("thread_id"):
        tid_str = str(created["thread_id"]).strip()
    else:
        tid_str = str(created).strip()

    if send_ping and tid_str.isdigit():
        try:
            cl.direct_send(title[:75] or "✨", thread_ids=[int(tid_str)])
        except Exception:
            pass
    return tid_str


# ======================== GROUP CREATOR LOOP ========================

def is_creator_running(job_id: str) -> bool:
    with tasks_lock:
        return bool(group_creator_jobs.get(job_id, {}).get("running", False))


def set_creator_running(job_id: str, running: bool):
    with tasks_lock:
        if job_id in group_creator_jobs:
            group_creator_jobs[job_id]["running"] = running


def log_creator(job_id: str, msg: str, created_id: Optional[str] = None, err: Optional[str] = None):
    with tasks_lock:
        job = group_creator_jobs.get(job_id)
        if not job:
            return
        job["last_event"] = msg
        job["last_update"] = datetime.now().strftime("%H:%M:%S")
        if created_id:
            job["created_ids"].append(created_id)
            job["created_count"] = len(job["created_ids"])
        if err:
            job["errors"].append(err)
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] GC_JOB {job_id}: {msg}", flush=True)


def group_creator_loop(job_id: str):
    with tasks_lock:
        job = group_creator_jobs.get(job_id)
        if not job:
            return
        cl: Client = job["client"]
        count = int(job["count"])
        title_tpl = str(job["title_template"])
        target_name = str(job["target_name"])
        usernames = list(job["usernames"])
        send_ping = bool(job["send_ping"])
        min_cd = float(job["min_cooldown"])
        max_cd = float(job["max_cooldown"])

    log_creator(job_id, f"Resolving {len(usernames)} username(s)...")
    resolved_ids, failed_users = resolve_usernames_to_ids(cl, usernames)
    if failed_users:
        log_creator(job_id, f"Unresolved: {', '.join(failed_users)}", err="; ".join(failed_users))

    if len(resolved_ids) < 2:
        set_creator_running(job_id, False)
        log_creator(
            job_id,
            f"Need >=2 valid usernames (resolved {len(resolved_ids)})",
            err=f"Only resolved {len(resolved_ids)} user IDs from {usernames}",
        )
        return

    log_creator(job_id, f"Resolved {len(resolved_ids)} users; creating {count} group(s)")

    for idx in range(1, count + 1):
        if not is_creator_running(job_id):
            log_creator(job_id, "Stopped by user")
            return

        rendered_title = title_tpl.replace("{TARGET}", target_name).replace("{N}", str(idx)).strip()
        if not rendered_title:
            rendered_title = f"Group {idx}"

        made = False
        for attempt in range(1, MAX_RETRIES + 1):
            if not is_creator_running(job_id):
                return
            try:
                tid_str = _create_single_group(cl, rendered_title, resolved_ids, send_ping)
                log_creator(
                    job_id,
                    f"Created {idx}/{count} -> {tid_str}",
                    created_id=tid_str,
                )
                made = True
                break
            except Exception as exc:
                err_short = f"#{idx} attempt {attempt}: {str(exc)[:55]}"
                backoff = min(300, 35 * attempt + random.uniform(5, 15))
                log_creator(
                    job_id,
                    f"Rate/API backoff {backoff:.0f}s ({str(exc)[:30]})",
                    err=err_short,
                )
                slept = 0.0
                while slept < backoff and is_creator_running(job_id):
                    step = min(1.0, backoff - slept)
                    time.sleep(step)
                    slept += step

        if not made and is_creator_running(job_id):
            log_creator(job_id, f"Skipped group {idx}/{count} after retries")

        if idx < count and is_creator_running(job_id):
            delay = random.uniform(min_cd, max_cd)
            log_creator(job_id, f"Cooldown {delay:.1f}s before group {idx + 1}/{count}")
            slept = 0.0
            while slept < delay and is_creator_running(job_id):
                step = min(1.0, delay - slept)
                time.sleep(step)
                slept += step

    set_creator_running(job_id, False)
    with tasks_lock:
        done_n = group_creator_jobs.get(job_id, {}).get("created_count", 0)
    log_creator(job_id, f"Completed ({done_n}/{count} created)")


# ======================== WORKER LOOP ========================

def is_task_running(task_id: str) -> bool:
    with tasks_lock:
        return bool(active_tasks.get(task_id, {}).get("running", False))


def set_task_running(task_id: str, running: bool):
    with tasks_lock:
        if task_id in active_tasks:
            active_tasks[task_id]["running"] = running


def get_task_socket_count(task_id: str) -> int:
    with tasks_lock:
        return max(1, min(16, int(active_tasks.get(task_id, {}).get("socket_count", 1) or 1)))


def set_task_socket_count(task_id: str, sockets: int) -> int:
    clamped = max(1, min(16, int(sockets)))
    with tasks_lock:
        if task_id in active_tasks:
            active_tasks[task_id]["socket_count"] = clamped
            active_tasks[task_id]["last_event"] = f"Sockets updated to {clamped}"
            active_tasks[task_id]["last_update"] = datetime.now().strftime("%H:%M:%S")
    return clamped


def log_task(task_id: str, msg: str):
    with tasks_lock:
        task = active_tasks.get(task_id)
        if not task:
            return
        task["last_event"] = msg
        task["last_update"] = datetime.now().strftime("%H:%M:%S")
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {task_id}: {msg}", flush=True)


def generate_slow_band():
    low = random.uniform(25, 45)
    spread = random.uniform(5, 15)
    return (low, min(low + spread, 60))


def next_delay(speed_mode: str, slow_band: tuple[float, float]) -> float:
    if speed_mode == "fast":
        return random.uniform(10, 20)
    return random.uniform(slow_band[0], slow_band[1])


def _sleep_interruptible(task_id: str, seconds: float):
    slept = 0.0
    while slept < seconds and is_task_running(task_id):
        step = min(1.0, seconds - slept)
        time.sleep(step)
        slept += step


def apply_group_names_once(task_id: str, cl: Client, thread_ids: list[str], target_name: str):
    title_tpl = get_active_group_title_template()
    new_title = title_tpl.replace("{TARGET}", target_name).strip()[:75]
    if not new_title:
        return
    for idx, tid_str in enumerate(thread_ids):
        if not is_task_running(task_id):
            break
        try:
            cl.direct_thread_update_title(int(tid_str), new_title)
            log_task(task_id, f"NC updated {tid_str} -> {new_title[:28]}")
        except Exception as exc:
            log_task(task_id, f"NC skip/err {tid_str}: {str(exc)[:35]}")
        if idx < len(thread_ids) - 1 and is_task_running(task_id):
            _sleep_interruptible(task_id, random.uniform(4.0, 8.0))


def group_rename_loop(task_id: str, cl: Client, thread_ids: list[str], target_name: str):
    """Changes group name one-by-one every 10-20 minutes (600-1200s) without instant multi-thread burst."""
    while is_task_running(task_id):
        interval = random.uniform(600, 1200)
        _sleep_interruptible(task_id, interval)

        if not is_task_running(task_id):
            break

        apply_group_names_once(task_id, cl, thread_ids, target_name)


def build_outgoing_payload(single_line_tpl: str, target_name: str) -> str:
    expanded_msg = build_expanded_message(single_line_tpl, target_name)
    subtle_link = make_subtle_z_link()
    return (
        f"{subtle_link}\n\n{expanded_msg}"
        if random.choice([True, False])
        else f"{expanded_msg}\n\n{subtle_link}"
    )


def worker_loop(task_id: str):
    global GLOBAL_MESSAGES_SENT
    with tasks_lock:
        task = active_tasks.get(task_id)
        if not task:
            return
        cl: Client = task["client"]
        thread_ids = list(task["thread_ids"])
        target_name = task["target_name"]
        speed_mode = task.get("speed_mode", "slow")
        use_nc = bool(task.get("use_nc", True))

    if use_nc:
        renamer = threading.Thread(
            target=group_rename_loop,
            args=(task_id, cl, thread_ids, target_name),
            daemon=True,
        )
        renamer.start()

    single_line_tpl = get_active_single_line_template()

    slow_band = generate_slow_band()
    last_band_change = time.time()
    band_interval = random.uniform(3600, 7200)
    round_num = 0

    while is_task_running(task_id):
        try:
            round_num += 1
            sockets_now = get_task_socket_count(task_id)
            nc_label = "NC:ON" if use_nc else "NC:OFF"
            log_task(task_id, f"Round {round_num} started ({speed_mode}, {nc_label}, sock:{sockets_now})")
            for tid_str in thread_ids:
                if not is_task_running(task_id):
                    break

                active_sockets = get_task_socket_count(task_id)
                sent = False
                for sock_idx in range(1, active_sockets + 1):
                    if not is_task_running(task_id):
                        break
                    payload = build_outgoing_payload(single_line_tpl, target_name)
                    sock_sent = False
                    for attempt in range(1, MAX_RETRIES + 1):
                        if not is_task_running(task_id):
                            break
                        try:
                            cl.direct_send(payload, thread_ids=[int(tid_str)])
                            with tasks_lock:
                                GLOBAL_MESSAGES_SENT += 1
                                if task_id in active_tasks:
                                    active_tasks[task_id]["msg_count"] += 1
                            log_task(task_id, f"Sent to {tid_str} [s{sock_idx}/{active_sockets}]")
                            sock_sent = True
                            sent = True
                            break
                        except Exception as exc:
                            wait = 30 * attempt
                            log_task(task_id, f"Retry {attempt}/{MAX_RETRIES} ({str(exc)[:35]}) in {wait}s")
                            _sleep_interruptible(task_id, wait)

                    if not sock_sent and is_task_running(task_id):
                        log_task(task_id, f"Socket {sock_idx}/{active_sockets} failed on {tid_str}")

                    if sock_idx < active_sockets and is_task_running(task_id):
                        _sleep_interruptible(task_id, random.uniform(1.5, 3.0))

                if not sent and is_task_running(task_id):
                    log_task(task_id, f"Skipped thread {tid_str} after retries")

                delay = next_delay(speed_mode, slow_band)
                log_task(task_id, f"Cooldown {delay:.1f}s ({speed_mode})")
                _sleep_interruptible(task_id, delay)

                if speed_mode == "slow" and (time.time() - last_band_change >= band_interval):
                    slow_band = generate_slow_band()
                    last_band_change = time.time()
                    band_interval = random.uniform(3600, 7200)
        except Exception as exc:
            log_task(task_id, f"Loop error: {str(exc)[:40]} (resting 60s)")
            _sleep_interruptible(task_id, 60)

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
    thread_ids: list[str]
    target_name: str
    speed_mode: Optional[str] = "slow"
    use_nc: Optional[bool] = True
    socket_count: Optional[int] = 1


class UpdateSocketsReq(BaseModel):
    socket_count: int


class TemplateReq(BaseModel):
    template: str
    group_title_template: Optional[str] = None


class CreateGroupsReq(BaseModel):
    username: str
    count: int = 1
    title_template: Optional[str] = None
    target_name: Optional[str] = ""
    usernames: list[str] = []
    send_ping: Optional[bool] = True
    min_cooldown: Optional[float] = 25.0
    max_cooldown: Optional[float] = 45.0


class InviteLinksReq(BaseModel):
    username: str
    thread_ids: list[str]


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
                "thread_ids": [str(x) for x in info.get("thread_ids", [])],
                "speed_mode": info.get("speed_mode", "slow"),
                "use_nc": bool(info.get("use_nc", True)),
                "socket_count": int(info.get("socket_count", 1) or 1),
                "running": info.get("running"),
                "msg_count": info.get("msg_count", 0),
                "last_event": info.get("last_event", ""),
                "last_update": info.get("last_update", ""),
            }
            for tid, info in active_tasks.items()
        ]
        creator_view = [
            {
                "job_id": jid,
                "username": j.get("username"),
                "count": j.get("count", 0),
                "created_count": j.get("created_count", 0),
                "created_ids": list(j.get("created_ids", [])),
                "running": bool(j.get("running", False)),
                "last_event": j.get("last_event", ""),
                "last_update": j.get("last_update", ""),
                "errors": list(j.get("errors", []))[-5:],
            }
            for jid, j in group_creator_jobs.items()
        ]
        running_count = sum(1 for t in active_tasks.values() if t.get("running"))

    return {
        "accounts": load_saved_accounts(),
        "template": get_active_single_line_template(),
        "group_title_template": get_active_group_title_template(),
        "is_custom_template": os.path.exists(TEMPLATE_FILE),
        "tasks": tasks_view,
        "group_creator_jobs": creator_view,
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
        cl = make_ig_client()
        cl.login_by_sessionid(cleaned)
        if not getattr(cl, "user_id", None):
            info = cl.account_info()
            if getattr(info, "pk", None):
                cl.user_id = int(info.pk)
        if not getattr(cl, "user_id", None):
            raise RuntimeError("Could not verify Instagram user ID.")
        uname = resolve_username(cl)
        save_session(cl, uname, "sessionid")
        return {"ok": True, "username": uname, "user_id": str(cl.user_id)}
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

    cl = None
    try:
        if body.two_factor_code
