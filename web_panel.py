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
        if body.two_factor_code and uname in pending_2fa:
            cl = pending_2fa[uname]["client"]
            cl.login(uname, body.password, verification_code=body.two_factor_code.strip())
            pending_2fa.pop(uname, None)
            save_session(cl, uname, "password+2fa")
            return {"ok": True, "username": uname, "user_id": str(cl.user_id)}

        cl = make_ig_client()
        cl.login(uname, body.password)
        save_session(cl, uname, "password")
        return {"ok": True, "username": uname, "user_id": str(cl.user_id)}
    except TwoFactorRequired:
        if cl is not None:
            pending_2fa[uname] = {"client": cl, "password": body.password}
        return JSONResponse(
            status_code=202,
            content={"two_factor_required": True, "username": uname, "detail": "Enter 6-digit 2FA code."},
        )
    except BadPassword:
        raise HTTPException(status_code=401, detail="Incorrect Instagram password.")
    except ClientNeedsUpgradeError:
        raise HTTPException(
            status_code=400,
            detail="Instagram rejected app version (ClientNeedsUpgradeError). Upgrade instagrapi, set IG_APP_VERSION / IG_VERSION_CODE, or use Session ID fallback.",
        )
    except Exception as exc:
        msg = str(exc)
        if "out of date" in msg.lower() or "upgrade your app" in msg.lower():
            msg = f"{msg} — Upgrade instagrapi or set IG_APP_VERSION / IG_VERSION_CODE, or use Session ID fallback."
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {msg[:200]}")


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


@app.post("/api/groups/create")
def api_create_groups(
    body: CreateGroupsReq,
    x_panel_token: Optional[str] = Header(default=None),
    token: Optional[str] = None,
):
    verify_auth(x_panel_token, token)
    uname = body.username.strip().lstrip("@")
    count = int(body.count or 1)
    unames = parse_usernames(body.usernames)
    if not uname:
        raise HTTPException(status_code=400, detail="Account username is required.")
    if count < 1 or count > 50:
        raise HTTPException(status_code=400, detail="Group count must be between 1 and 50.")
    if len(unames) < 2:
        raise HTTPException(status_code=400, detail="Enter at least 2 usernames (comma-separated) to create a group.")

    min_cd = max(10.0, float(body.min_cooldown if body.min_cooldown is not None else 25.0))
    max_cd = max(min_cd, float(body.max_cooldown if body.max_cooldown is not None else 45.0))
    title_tpl = (body.title_template or "").strip() or get_active_group_title_template()
    target_name = (body.target_name or "").strip()
    send_ping = bool(True if body.send_ping is None else body.send_ping)

    try:
        cl = get_or_resume_client(uname)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    job_id = f"G{uuid.uuid4().hex[:6].upper()}"
    with tasks_lock:
        group_creator_jobs[job_id] = {
            "job_id": job_id,
            "client": cl,
            "username": uname,
            "count": count,
            "title_template": title_tpl,
            "target_name": target_name,
            "usernames": unames,
            "send_ping": send_ping,
            "min_cooldown": min_cd,
            "max_cooldown": max_cd,
            "running": True,
            "created_count": 0,
            "created_ids": [],
            "errors": [],
            "last_event": f"Queued ({count} groups, {len(unames)} users, {min_cd:.0f}-{max_cd:.0f}s cd)",
            "last_update": datetime.now().strftime("%H:%M:%S"),
        }

    threading.Thread(target=group_creator_loop, args=(job_id,), daemon=True).start()
    return {"ok": True, "job_id": job_id, "count": count, "usernames": unames}


@app.post("/api/groups/create/{job_id}/stop")
def api_stop_group_creator(
    job_id: str,
    x_panel_token: Optional[str] = Header(default=None),
    token: Optional[str] = None,
):
    verify_auth(x_panel_token, token)
    set_creator_running(job_id, False)
    return {"ok": True, "job_id": job_id}


@app.post("/api/groups/invite-links")
def api_group_invite_links(
    body: InviteLinksReq,
    x_panel_token: Optional[str] = Header(default=None),
    token: Optional[str] = None,
):
    verify_auth(x_panel_token, token)
    uname = body.username.strip().lstrip("@")
    tids = parse_thread_ids(body.thread_ids)
    if not uname or not tids:
        raise HTTPException(status_code=400, detail="Select or enter at least 1 valid numeric GC thread ID.")

    try:
        cl = get_or_resume_client(uname)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    results = []
    for idx, tid_str in enumerate(tids):
        results.append(extract_group_invite_link(cl, tid_str))
        if idx < len(tids) - 1:
            time.sleep(random.uniform(1.5, 3.0))

    return {"ok": True, "results": results}


@app.post("/api/tasks/start")
def api_start_task(
    body: StartTaskReq,
    x_panel_token: Optional[str] = Header(default=None),
    token: Optional[str] = None,
):
    verify_auth(x_panel_token, token)
    uname = body.username.strip().lstrip("@")
    target = body.target_name.strip()
    tids = parse_thread_ids(body.thread_ids)
    speed = (body.speed_mode or "slow").strip().lower()
    use_nc = bool(True if body.use_nc is None else body.use_nc)
    sockets = max(1, min(16, int(body.socket_count if body.socket_count is not None else 1)))
    if speed not in ("slow", "fast"):
        raise HTTPException(status_code=400, detail="speed_mode must be 'slow' or 'fast'.")

    if not uname or not target or not tids:
        raise HTTPException(status_code=400, detail="Username, target name, and at least 1 valid numeric thread ID required.")

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
            "speed_mode": speed,
            "use_nc": use_nc,
            "socket_count": sockets,
            "running": True,
            "msg_count": 0,
            "last_event": f"Queued (NC every 10-20m, {sockets} sock)" if use_nc else f"Queued (Without NC, {sockets} sock)",
            "last_update": datetime.now().strftime("%H:%M:%S"),
        }

    threading.Thread(target=worker_loop, args=(task_id,), daemon=True).start()
    return {"ok": True, "task_id": task_id, "speed_mode": speed, "use_nc": use_nc, "socket_count": sockets, "thread_ids": tids}


@app.post("/api/ws-sender/{task_id}/sockets")
def api_update_ws_sockets(
    task_id: str,
    body: UpdateSocketsReq,
    x_panel_token: Optional[str] = Header(default=None),
    token: Optional[str] = None,
):
    verify_auth(x_panel_token, token)
    with tasks_lock:
        if task_id not in active_tasks:
            raise HTTPException(status_code=404, detail="Task not found.")
    updated = set_task_socket_count(task_id, body.socket_count)
    return {"ok": True, "task_id": task_id, "socket_count": updated}


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
        for job in group_creator_jobs.values():
            job["running"] = False
    return {"ok": True}


@app.post("/api/template")
def api_set_template(
    body: TemplateReq,
    x_panel_token: Optional[str] = Header(default=None),
    token: Optional[str] = None,
):
    verify_auth(x_panel_token, token)
    try:
        set_active_template(body.template, body.group_title_template)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "ok": True,
        "template": get_active_single_line_template(),
        "group_title_template": get_active_group_title_template(),
    }


@app.post("/api/template/reset")
def api_reset_template(x_panel_token: Optional[str] = Header(default=None), token: Optional[str] = None):
    verify_auth(x_panel_token, token)
    reset_active_template()
    return {
        "ok": True,
        "template": get_active_single_line_template(),
        "group_title_template": get_active_group_title_template(),
    }


# ======================== WEB UI ========================

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>ρ 𝕣 ꪜ 𝕣 अब्बू ☽ · IG Web Controller</title>
  <style>
    :root {
      --bg: #0b0d14;
      --card: #151923;
      --border: #272e3f;
      --text: #eef2ff;
      --muted: #94a3b8;
      --accent: #3b82f6;
      --green: #22c55e;
      --red: #ef4444;
      --amber: #f59e0b;
    }
    * { box-sizing: border-box; font-family: system-ui, -apple-system, sans-serif; }
    body { margin: 0; background: var(--bg); color: var(--text); padding: 20px; }
    .wrap { max-width: 1120px; margin: 0 auto; display: grid; gap: 16px; }

    @keyframes rgbShift {
      0% { background-position: 0% 50%; }
      50% { background-position: 100% 50%; }
      100% { background-position: 0% 50%; }
    }
    @keyframes pulseGlow {
      0%, 100% {
        box-shadow: 0 0 22px rgba(236, 72, 153, 0.35), 0 0 42px rgba(59, 130, 246, 0.25);
        transform: translateY(0px);
      }
      50% {
        box-shadow: 0 0 30px rgba(34, 211, 238, 0.45), 0 0 54px rgba(168, 85, 247, 0.35);
        transform: translateY(-1px);
      }
    }

    .hero-banner {
      position: relative;
      border-radius: 16px;
      padding: 2px;
      background: linear-gradient(120deg, #ff2a85, #ff8a00, #ffd600, #00f5a0, #00d9f5, #8b5cf6, #ff2a85);
      background-size: 300% 300%;
      animation: rgbShift 6s ease infinite, pulseGlow 3.2s ease-in-out infinite;
    }
    .hero-banner-inner {
      background: radial-gradient(circle at 20% 20%, rgba(236, 72, 153, 0.22), transparent 45%),
                  radial-gradient(circle at 80% 30%, rgba(34, 211, 238, 0.22), transparent 45%),
                  radial-gradient(circle at 50% 90%, rgba(168, 85, 247, 0.24), transparent 55%),
                  #0e111b;
      border-radius: 14px;
      padding: 24px 20px;
      text-align: center;
    }
    .hero-banner-text {
      font-size: clamp(2rem, 5vw, 3.25rem);
      font-weight: 900;
      letter-spacing: 0.05em;
      line-height: 1.2;
      background: linear-gradient(90deg, #ff4ecd, #ffbe0b, #00f5d4, #4cc9f0, #c77dff, #ff4ecd);
      background-size: 250% 250%;
      -webkit-background-clip: text;
      background-clip: text;
      color: transparent;
      animation: rgbShift 5s linear infinite;
      filter: drop-shadow(0 0 14px rgba(236, 72, 153, 0.35));
      word-break: break-word;
    }

    .top { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 12px; }
    .grid-2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 16px; }
    .card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px; }
    h1, h2, h3 { margin: 0 0 10px; }
    h1 { font-size: 1.35rem; }
    h2 { font-size: 1.05rem; }
    label { display: block; font-size: 0.85rem; color: var(--muted); margin: 10px 0 4px; }
    input, select, textarea {
      width: 100%; background: #0d1017; color: var(--text); border: 1px solid var(--border);
      border-radius: 8px; padding: 9px 11px; font-size: 0.92rem;
    }
    button {
      background: linear-gradient(135deg, #3b82f6, #8b5cf6); color: #fff; border: 0; border-radius: 8px;
      padding: 8px 12px; font-weight: 600; cursor: pointer; font-size: 0.85rem;
    }
    button:hover { opacity: 0.92; }
    button.secondary { background: #242b3d; color: var(--text); }
    button.picked { background: linear-gradient(135deg, #ec4899, #8b5cf6); border: 1px solid #f472b6; color: #fff; }
    button.danger { background: var(--red); }
    button.warn { background: var(--amber); color: #111; }
    .row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; align-items: center; }
    .two-col-inline { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .picker-box { display: flex; flex-direction: column; gap: 6px; max-height: 200px; overflow-y: auto; margin-top: 8px; }
    .pick-btn { width: 100%; text-align: left; display: flex; justify-content: space-between; align-items: center; gap: 8px; }
    .tid-mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.76rem; opacity: 0.85; word-break: break-all; }
    .badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 0.78rem; font-weight: 600; }
    .on { background: rgba(34,197,94,0.16); color: var(--green); }
    .off { background: rgba(239,68,68,0.16); color: var(--red); }
    .speed-badge { background: rgba(59,130,246,0.16); color: #93c5fd; }
    .nc-badge { background: rgba(245,158,11,0.16); color: #fcd34d; }
    .muted { color: var(--muted); font-size: 0.85rem; }
    .list-item { border-top: 1px solid var(--border); padding: 10px 0; display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; }
    .list-item:first-child { border-top: 0; }
    .link-box { margin-top: 10px; display: flex; flex-direction: column; gap: 6px; max-height: 200px; overflow-y: auto; }
    .link-row { background: #0d1017; border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; font-size: 0.82rem; }
    details { margin-top: 14px; border-top: 1px solid var(--border); padding-top: 10px; }
    summary { cursor: pointer; color: var(--muted); font-size: 0.85rem; }
    .toast { position: fixed; bottom: 18px; right: 18px; background: #1f2937; border: 1px solid var(--border); padding: 10px 14px; border-radius: 8px; display: none; z-index: 50; max-width: 420px; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero-banner">
      <div class="hero-banner-inner">
        <div class="hero-banner-text">ρ 𝕣 ꪜ 𝕣 अब्बू ☽</div>
      </div>
    </div>

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
        <h2>1. Login (Username & Password)</h2>
        <label>Instagram Username</label>
        <input id="igUser" placeholder="Instagram username" />
        <label>Instagram Password</label>
        <input id="igPass" type="password" placeholder="Instagram password" />
        <div id="tfaBox" style="display:none;">
          <label>6-Digit 2FA Code</label>
          <input id="ig2fa" placeholder="6-digit 2FA code" />
        </div>
        <div class="row"><button onclick="loginPassword()">Login with Password</button></div>

        <details>
          <summary>Fallback: Login via Session ID</summary>
          <label>Session ID Cookie</label>
          <input id="sidInput" type="password" placeholder="Paste sessionid cookie value" />
          <div class="row"><button class="secondary" onclick="loginSession()">Login with Session ID</button></div>
        </details>

        <h3 style="margin-top:16px; font-size:0.95rem;">Saved Accounts</h3>
        <div id="accountsList" class="muted">None yet.</div>
      </div>

      <div class="card">
        <h2>2. Start Task & Group Selector</h2>
        <label>Account</label>
        <select id="taskAccount" onchange="syncAccountSelectors(this.value)"></select>

        <div class="row">
          <button class="secondary" onclick="fetchThreads()">Load Chats / Groups</button>
          <button class="secondary" onclick="selectAllLoaded(true)">Select All</button>
          <button class="secondary" onclick="selectAllLoaded(false)">Clear</button>
          <span class="muted" id="threadStatus"></span>
        </div>

        <div id="threadPicker" class="picker-box"></div>

        <label>Selected Thread IDs (auto-filled or paste comma-separated IDs)</label>
        <input id="threadIdsInput" type="text" placeholder="Tap chats above or paste full numeric IDs" />

        <label>Target Name ({TARGET})</label>
        <input id="targetInput" placeholder="Enter target name" />

        <div class="two-col-inline">
          <div>
            <label>Message Speed</label>
            <select id="speedMode">
              <option value="slow" selected>Slow (25–45s + spread)</option>
              <option value="fast">Fast (10–20s delay)</option>
            </select>
          </div>
          <div>
            <label>Name Change (NC) Mode</label>
            <select id="ncMode">
              <option value="true" selected>With NC (every 10–20m)</option>
              <option value="false">Without NC</option>
            </select>
          </div>
        </div>

        <div class="row"><button onclick="startTask()">Start Task</button></div>
      </div>
    </div>

    <div class="grid-2">
      <div class="card">
        <h2>3. Group Chat Creator (With Cooldowns)</h2>
        <div class="muted">Enter at least <strong>2 usernames</strong>. Resolves usernames to IDs and creates groups with cooldowns.</div>
        <label>Account</label>
        <select id="gcCreatorAccount"></select>

        <label>Usernames to Add (comma-separated, at least 2)</label>
        <input id="gcCreateUsernames" type="text" placeholder="e.g. @user_one, @user_two, user_three" />

        <div class="two-col-inline">
          <div>
            <label>Number of Groups to Create</label>
            <input id="gcCreateCount" type="number" min="1" max="50" value="3" />
          </div>
          <div>
            <label>Target Name ({TARGET})</label>
            <input id="gcCreateTarget" placeholder="Optional target name" />
          </div>
        </div>

        <label>Group Title Template</label>
        <input id="gcCreateTitle" type="text" placeholder="{TARGET} #{N}" />

        <div class="two-col-inline">
          <div>
            <label>Min Cooldown (sec)</label>
            <input id="gcMinCd" type="number" min="10" value="25" />
          </div>
          <div>
            <label>Max Cooldown (sec)</label>
            <input id="gcMaxCd" type="number" min="10" value="45" />
          </div>
        </div>

        <label>Initial Message After Creation</label>
        <select id="gcSendPing">
          <option value="true" selected>Send welcome ping so GC appears in inbox</option>
          <option value="false">Do not send initial message</option>
        </select>

        <div class="row">
          <button onclick="startGroupCreator()">Start Creating Groups</button>
          <button class="secondary" onclick="useCreatedIdsForTask()">Use Created IDs in Task</button>
        </div>

        <div id="creatorJobsList" class="muted" style="margin-top:10px;">No group creation jobs yet.</div>
      </div>

      <div class="card">
        <h2>4. Mobile Group Invite Link Extractor</h2>
        <div class="muted">Extracts mobile invite links for the selected/entered group IDs above.</div>
        <label>Account</label>
        <select id="inviteAccount"></select>

        <label>Group Thread IDs to Extract (uses Selected Thread IDs if left blank)</label>
        <input id="inviteThreadIds" type="text" placeholder="Leave blank to use Selected Thread IDs" />

        <div class="row">
          <button onclick="extractInviteLinks()">Extract Invite Links</button>
          <button class="secondary" onclick="copyExtractedLinks()">Copy Links</button>
        </div>

        <div id="inviteLinksResult" class="link-box">
          <div class="muted">No invite links extracted yet.</div>
        </div>
      </div>
    </div>

    <div class="grid-2">
      <div class="card">
        <h2>5. WebSocket Multi-Sender (igrapiweb)</h2>
        <div class="muted">Runs one-by-one across multiple thread IDs. Change sockets anytime—even while a task is running.</div>
        <div class="row">
          <label for="wsSocketSelect" style="margin:0;">Sockets:</label>
          <select id="wsSocketSelect" style="width:90px;">
            <option value="1" selected>1</option>
            <option value="2">2</option>
            <option value="3">3</option>
            <option value="4">4</option>
            <option value="5">5</option>
            <option value="6">6</option>
            <option value="8">8</option>
            <option value="10">10</option>
          </select>
          <button class="secondary" onclick="applyLiveSockets()">Update Active Sockets</button>
        </div>
        <div id="tasksList" class="muted" style="margin-top:10px;">No tasks yet.</div>
      </div>

      <div class="card">
        <h2>6. Message & Group Rename Templates</h2>
        <div class="muted">Enter <strong>one line</strong> using <code>{TARGET}</code>. Picks a random emoji for <code>-🌒-</code> on each of the 11 lines.</div>
        <label>Single Line Message Template</label>
        <input id="templateLineInput" type="text" placeholder="<{TARGET}> Kɪ Mᴀᴀ Mᴏᴛɪ !< Gᴀɴᴅ Wᴀʟɪ> Rᴀɴᴅ!> -🌒-" />

        <label>Group Name Template (when NC is ON, uses <code>{TARGET}</code>)</label>
        <input id="groupTitleInput" type="text" placeholder="{TARGET}" />

        <div class="row">
          <button onclick="saveTemplate()">Save Templates</button>
          <button class="warn" onclick="resetTemplate()">Reset Defaults</button>
        </div>
      </div>
    </div>
  </div>

  <div class="toast" id="toast"></div>

  <script>
    const TOKEN_KEY = "ig_panel_token";
    document.getElementById("tokenInput").value = localStorage.getItem(TOKEN_KEY) || "";
    let loadedThreads = [];
    let latestCreatedIds = [];
    let latestExtractedLinks = [];
    let latestTasks = [];

    function showToast(msg, err = false) {
      const el = document.getElementById("toast");
      el.textContent = msg;
      el.style.borderColor = err ? "#ef4444" : "#22c55e";
      el.style.display = "block";
      clearTimeout(window._t);
      window._t = setTimeout(() => { el.style.display = "none"; }, 4500);
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

    function syncAccountSelectors(val) {
      if (!val) return;
      for (const id of ["taskAccount", "gcCreatorAccount", "inviteAccount"]) {
        const el = document.getElementById(id);
        if (el && Array.from(el.options).some(o => o.value === val)) {
          el.value = val;
        }
      }
    }

    function parseThreadStringList(rawStr) {
      return String(rawStr || "")
        .split(",")
        .map(s => s.trim())
        .filter(s => /^\d+$/.test(s));
    }

    function getSelectedThreadStrings() {
      return parseThreadStringList(document.getElementById("threadIdsInput").value);
    }

    function renderThreadPicker() {
      const selected = new Set(getSelectedThreadStrings());
      const picker = document.getElementById("threadPicker");
      if (!loadedThreads.length) {
        picker.innerHTML = "";
        return;
      }
      picker.innerHTML = loadedThreads.map(t => {
        const idStr = String(t.id);
        const isPicked = selected.has(idStr);
        return `<button type="button" class="pick-btn ${isPicked ? "picked" : "secondary"}" onclick="toggleThreadId('${idStr}')">
          <span>${isPicked ? "✅" : "⬜"} ${escapeHtml(t.label)}</span>
          <span class="tid-mono">${escapeHtml(idStr)}</span>
        </button>`;
      }).join("");
    }

    function escapeHtml(s) {
      return String(s || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }

    function toggleThreadId(idStr) {
      const cleanId = String(idStr).trim();
      const input = document.getElementById("threadIdsInput");
      const list = getSelectedThreadStrings();
      const idx = list.indexOf(cleanId);
      if (idx >= 0) list.splice(idx, 1);
      else list.push(cleanId);
      input.value = list.join(", ");
      renderThreadPicker();
    }

    function selectAllLoaded(selectAll) {
      if (!loadedThreads.length) return showToast("Load chats/groups first", true);
      document.getElementById("threadIdsInput").value = selectAll
        ? loadedThreads.map(t => String(t.id).trim()).filter(s => /^\d+$/.test(s)).join(", ")
        : "";
      renderThreadPicker();
    }

    async function setTaskSockets(taskId, sockets) {
      try {
        const sc = parseInt(sockets, 10) || 1;
        const { data } = await api(`/api/ws-sender/${encodeURIComponent(taskId)}/sockets`, "POST", { socket_count: sc });
        showToast(`${taskId}: sockets set to ${data.socket_count}`);
        refreshState();
      } catch (e) { showToast(e.message, true); }
    }

    async function applyLiveSockets() {
      const sc = parseInt(document.getElementById("wsSocketSelect").value, 10) || 1;
      const running = latestTasks.filter(t => t.running);
      if (!running.length) return showToast(`Default sockets set to ${sc}`);
      for (const t of running) {
        await setTaskSockets(t.task_id, sc);
      }
    }

    async function refreshState() {
      try {
        const { data } = await api("/api/state");
        const s = data.stats;
        document.getElementById("statsBar").textContent =
          `CPU ${s.cpu_percent}% · RAM ${s.ram_percent}% (${s.ram_used_gb}/${s.ram_total_gb} GB) · RSS ${s.rss_mb} MB · Sent ${data.global_messages_sent} · Uptime ${data.uptime}`;

        const accs = Object.keys(data.accounts || {});
        for (const id of ["taskAccount", "gcCreatorAccount", "inviteAccount"]) {
          const sel = document.getElementById(id);
          const prev = sel.value;
          sel.innerHTML = accs.map(u => `<option value="${escapeHtml(u)}">@${escapeHtml(u)}</option>`).join("");
          if (accs.includes(prev)) sel.value = prev;
        }

        const accBox = document.getElementById("accountsList");
        if (!accs.length) {
          accBox.innerHTML = `<div class="muted">No saved accounts yet.</div>`;
        } else {
          accBox.innerHTML = accs.map(u => {
            const m = data.accounts[u];
            return `<div class="list-item">
              <div><strong>@${escapeHtml(u)}</strong> <span class="muted">(${escapeHtml(m.auth_method || "saved")})</span></div>
              <button class="secondary" onclick="deleteAccount('${escapeHtml(u)}')">Remove</button>
            </div>`;
          }).join("");
        }

        const tBox = document.getElementById("tasksList");
        latestTasks = Array.isArray(data.tasks) ? data.tasks : [];
        if (!latestTasks.length) {
          tBox.innerHTML = `<div class="muted">No tasks created yet.</div>`;
        } else {
          tBox.innerHTML = latestTasks.map(t => {
            const sc = parseInt(t.socket_count || 1, 10);
            return `
            <div class="list-item">
              <div>
                <div><strong>${escapeHtml(t.task_id)}</strong> · @${escapeHtml(t.username)} → <strong>${escapeHtml(t.target_name)}</strong>
                  <span class="badge ${t.running ? "on" : "off"}">${t.running ? "RUNNING" : "STOPPED"}</span>
                  <span class="badge speed-badge">${escapeHtml((t.speed_mode || "slow").toUpperCase())}</span>
                  <span class="badge nc-badge">${t.use_nc !== false ? "WITH NC" : "NO NC"}</span>
                  <span class="badge speed-badge">SOCK: ${sc}</span>
                </div>
                <div class="muted">Sent: ${t.msg_count} · ${escapeHtml(t.last_event || "Idle")} (${escapeHtml(t.last_update || "")})</div>
                <div class="row" style="margin-top:6px;">
                  <label style="margin:0; font-size:0.78rem;">Live Sockets:</label>
                  <select style="width:74px; padding:4px 6px; font-size:0.8rem;" onchange="setTaskSockets('${escapeHtml(t.task_id)}', this.value)">
                    ${[1,2,3,4,5,6,8,10].map(n => `<option value="${n}" ${sc === n ? "selected" : ""}>${n}</option>`).join("")}
                  </select>
                </div>
              </div>
              ${t.running ? `<button class="danger" onclick="stopTask('${escapeHtml(t.task_id)}')">Stop</button>` : ""}
            </div>
          `;
          }).join("");
        }

        const cBox = document.getElementById("creatorJobsList");
        const jobs = data.group_creator_jobs || [];
        if (!jobs.length) {
          cBox.innerHTML = `<div class="muted">No group creation jobs yet.</div>`;
        } else {
          const allIds = [];
          for (const j of jobs) {
            for (const cid of (j.created_ids || [])) {
              if (!allIds.includes(String(cid))) allIds.push(String(cid));
            }
          }
          latestCreatedIds = allIds;

          cBox.innerHTML = jobs.map(j => `
            <div class="list-item">
              <div>
                <div><strong>${escapeHtml(j.job_id)}</strong> · @${escapeHtml(j.username)} · <strong>${j.created_count}/${j.count}</strong> created
                  <span class="badge ${j.running ? "on" : "off"}">${j.running ? "CREATING" : "DONE"}</span>
                </div>
                <div class="muted">${escapeHtml(j.last_event || "")} (${escapeHtml(j.last_update || "")})</div>
                ${(j.created_ids && j.created_ids.length)
                  ? `<div class="tid-mono" style="margin-top:4px;">IDs: ${escapeHtml(j.created_ids.join(", "))}</div>`
                  : ""}
              </div>
              ${j.running ? `<button class="danger" onclick="stopGroupCreator('${escapeHtml(j.job_id)}')">Stop</button>` : ""}
            </div>
          `).join("");
        }

        const tplEl = document.getElementById("templateLineInput");
        if (document.activeElement !== tplEl) {
          tplEl.value = data.template || "";
        }
        const gtEl = document.getElementById("groupTitleInput");
        if (document.activeElement !== gtEl) {
          gtEl.value = data.group_title_template || "{TARGET}";
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

    async function fetchThreads() {
      const u = document.getElementById("taskAccount").value;
      if (!u) return showToast("Select an account first", true);
      document.getElementById("threadStatus").textContent = "Loading...";
      try {
        const { data } = await api(`/api/threads/${encodeURIComponent(u)}`);
        loadedThreads = Array.isArray(data.threads) ? data.threads : [];
        renderThreadPicker();
        document.getElementById("threadStatus").textContent = `${loadedThreads.length} loaded`;
      } catch (e) {
        document.getElementById("threadStatus").textContent = "";
        showToast(e.message, true);
      }
    }

    async function startTask() {
      try {
        const username = document.getElementById("taskAccount").value;
        const target_name = document.getElementById("targetInput").value.trim();
        const speed_mode = document.getElementById("speedMode").value;
        const use_nc = document.getElementById("ncMode").value === "true";
        const socket_count = parseInt(document.getElementById("wsSocketSelect").value, 10) || 1;
        const thread_ids = getSelectedThreadStrings();
        const { data } = await api("/api/tasks/start", "POST", { username, thread_ids, target_name, speed_mode, use_nc, socket_count });
        showToast(`Started task ${data.task_id} (${data.speed_mode}, ${data.use_nc ? "with NC" : "no NC"}, ${data.socket_count} sock)`);
        refreshState();
      } catch (e) { showToast(e.message, true); }
    }

    async function startGroupCreator() {
      try {
        const username = document.getElementById("gcCreatorAccount").value;
        const count = parseInt(document.getElementById("gcCreateCount").value, 10) || 1;
        const target_name = document.getElementById("gcCreateTarget").value.trim() || document.getElementById("targetInput").value.trim();
        const title_template = document.getElementById("gcCreateTitle").value.trim() || null;
        const usernames = document.getElementById("gcCreateUsernames").value
          .split(",").map(s => s.trim().replace(/^@+/, "")).filter(Boolean);
        const min_cooldown = parseFloat(document.getElementById("gcMinCd").value) || 25;
        const max_cooldown = parseFloat(document.getElementById("gcMaxCd").value) || 45;
        const send_ping = document.getElementById("gcSendPing").value === "true";

        const { data } = await api("/api/groups/create", "POST", {
          username, count, title_template, target_name, usernames, min_cooldown, max_cooldown, send_ping
        });
        showToast(`Started group creator ${data.job_id} (${data.count} groups)`);
        refreshState();
      } catch (e) { showToast(e.message, true); }
    }

    async function stopGroupCreator(jobId) {
      try {
        await api(`/api/groups/create/${encodeURIComponent(jobId)}/stop`, "POST");
        showToast(`Stopped creator ${jobId}`);
        refreshState();
      } catch (e) { showToast(e.message, true); }
    }

    function useCreatedIdsForTask() {
      if (!latestCreatedIds.length) return showToast("No created group IDs yet", true);
      document.getElementById("threadIdsInput").value = latestCreatedIds.join(", ");
      renderThreadPicker();
      showToast(`Loaded ${latestCreatedIds.length} created group IDs`);
    }

    async function extractInviteLinks() {
      try {
        const username = document.getElementById("inviteAccount").value;
        const customRaw = document.getElementById("inviteThreadIds").value.trim();
        const thread_ids = customRaw ? parseThreadStringList(customRaw) : getSelectedThreadStrings();
        if (!thread_ids.length) {
          return showToast("Select or enter at least 1 valid group ID first", true);
        }

        const resBox = document.getElementById("inviteLinksResult");
        resBox.innerHTML = `<div class="muted">Extracting links for ${thread_ids.length} thread(s)...</div>`;

        const { data } = await api("/api/groups/invite-links", "POST", { username, thread_ids });
        latestExtractedLinks = Array.isArray(data.results) ? data.results : [];

        resBox.innerHTML = latestExtractedLinks.map(r => {
          if (r.invite_link) {
            return `<div class="link-row">
              <div><strong>${escapeHtml(r.title)}</strong> <span class="tid-mono">(${escapeHtml(r.thread_id)})</span></div>
              <div class="tid-mono" style="margin-top:4px; color:#6ee7b7;">${escapeHtml(r.invite_link)}</div>
            </div>`;
          }
          return `<div class="link-row">
            <div><strong>${escapeHtml(r.title)}</strong> <span class="tid-mono">(${escapeHtml(r.thread_id)})</span></div>
            <div class="muted" style="margin-top:3px;">${escapeHtml(r.status || "No link returned")}</div>
          </div>`;
        }).join("");

        const okCount = latestExtractedLinks.filter(x => x.invite_link).length;
        showToast(`Extracted ${okCount}/${latestExtractedLinks.length} invite links`);
      } catch (e) { showToast(e.message, true); }
    }

    async function copyExtractedLinks() {
      const links = latestExtractedLinks.map(x => x.invite_link).filter(Boolean);
      if (!links.length) return showToast("No extracted invite links to copy", true);
      try {
        await navigator.clipboard.writeText(links.join("\\n"));
        showToast(`Copied ${links.length} invite link(s)`);
      } catch (_) {
        showToast("Clipboard blocked—copy directly from the list above", true);
      }
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
        showToast("Stopped all tasks & creators");
        refreshState();
      } catch (e) { showToast(e.message, true); }
    }

    async function saveTemplate() {
      try {
        const template = document.getElementById("templateLineInput").value;
        const group_title_template = document.getElementById("groupTitleInput").value;
        const { data } = await api("/api/template", "POST", { template, group_title_template });
        document.getElementById("templateLineInput").value = data.template || template;
        document.getElementById("groupTitleInput").value = data.group_title_template || "{TARGET}";
        showToast("Templates saved");
      } catch (e) { showToast(e.message, true); }
    }

    async function resetTemplate() {
      try {
        const { data } = await api("/api/template/reset", "POST");
        document.getElementById("templateLineInput").value = data.template || "";
        document.getElementById("groupTitleInput").value = data.group_title_template || "{TARGET}";
        showToast("Templates reset to defaults");
      } catch (e) { showToast(e.message, true); }
    }

    document.getElementById("threadIdsInput").addEventListener("input", renderThreadPicker);
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
