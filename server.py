"""

微信点歌 API 服务 - 服务器版

基于网易云音乐 + QQ音乐，支持 VIP 歌曲（需会员 cookie）

QQ音乐支持自动续期 musickey（24小时定时）

"""



import hashlib

import base64

import io

import json

import os

import re

import random

import secrets

import sys

import time

import threading

import uuid

import shutil

import subprocess

import zlib

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

from datetime import datetime, timedelta

from pathlib import Path

import logging

import hmac

import ipaddress

from urllib.parse import parse_qs, urlsplit



import requests

import qrcode

from cryptography.fernet import Fernet, InvalidToken

from fastapi import FastAPI, Query, HTTPException, Request

from fastapi.responses import PlainTextResponse, JSONResponse, HTMLResponse, FileResponse, RedirectResponse, StreamingResponse

from fastapi.staticfiles import StaticFiles

from fastapi.middleware.cors import CORSMiddleware

import uvicorn



from qishui_adapter import QishuiError, start_qr as qishui_start_qr, poll_qr as qishui_poll_qr, me as qishui_me, playlists as qishui_playlists, playlist_detail as qishui_playlist_detail, public_playlist as qishui_public_playlist, search as qishui_search, lyric as qishui_lyric

from kugou_adapter import renew_session as kugou_renew_session

from migu_adapter import search as migu_search, playback_url as migu_playback_url, lyric as migu_lyric, playback_url_with_cookie as migu_playback_url_with_cookie, check_cookie as migu_check_cookie, renew_cookie as migu_renew_session, user_info as migu_user_info, merge_cookie as migu_merge_cookie



from music_sources import (

    try_third_sources,

    try_third_sources_parallel,

    test_source as _test_source,

    test_source_detailed as _test_source_detailed,

    detect_source_candidates as _detect_source_candidates,

    BUILTIN_SOURCES as _BUILTIN_SOURCES,

    get_url_from_source_def as _get_url_from_source_def,

)



logger = logging.getLogger("music-sources")

THIRD_SOURCE_USAGE_LOCK = threading.Lock()


# ==================== 配置 ====================

DATA_DIR = Path(__file__).parent / "data"

DATA_DIR.mkdir(parents=True, exist_ok=True)

STATIC_DIR = Path(__file__).parent / "static"

SOURCE_LIBRARY_DIR = Path(__file__).parent / "音乐音源"

CONFIG_FILE = DATA_DIR / "config.json"

LOG_FILE = DATA_DIR / "call_log.json"

PLAYLIST_FILE = DATA_DIR / "playlists.json"

USER_FILE = DATA_DIR / "users.json"

CONNECTED_ACCOUNT_FILE = DATA_DIR / "connected_accounts.json"

QISHUI_QR_DIAGNOSTIC_FILE = DATA_DIR / "qishui_qr_diagnostics.json"

CREDENTIAL_KEY_FILE = DATA_DIR / ".connected_account.key"

SESSION_COOKIE = "music_api_session"

SESSION_FILE = DATA_DIR / "sessions.json"

SESSION_TTL = timedelta(days=14)

SESSIONS = {}

# Single-server presence lives only in memory. Browsers renew every 30 seconds;
# entries expire after 90 seconds without a heartbeat.
ONLINE_PRESENCE_TTL = 90

ONLINE_PRESENCE = {}

ONLINE_PRESENCE_LOCK = threading.Lock()

ACCOUNT_QR_TTL = timedelta(minutes=5)

ACCOUNT_QR_SESSIONS = {}

ACCOUNT_QR_LOCK = threading.Lock()

ADMIN_QR_SESSIONS = {}

ADMIN_QR_LOCK = threading.Lock()

NCM_QR_ADAPTER_PORT = int(os.environ.get("NCM_QR_ADAPTER_PORT", "3210"))

NCM_QR_ADAPTER_URL = f"http://127.0.0.1:{NCM_QR_ADAPTER_PORT}"

NCM_QR_ADAPTER_APP = Path(__file__).parent / ".ncm-qr-adapter" / "node_modules" / "NeteaseCloudMusicApi" / "app.js"

NCM_QR_ADAPTER_LOCK = threading.Lock()

NCM_QR_ADAPTER_PROCESS = None

QISHUI_BRIDGE_PORT = int(os.environ.get("QISHUI_BRIDGE_PORT", "3211"))

QISHUI_BRIDGE_URL = f"http://127.0.0.1:{QISHUI_BRIDGE_PORT}"

QISHUI_BRIDGE_DIR = Path(__file__).parent / ".qishui-qr-bridge"

QISHUI_BRIDGE_APP = QISHUI_BRIDGE_DIR / "bridge-server.js"

QISHUI_BRIDGE_ELECTRON = QISHUI_BRIDGE_DIR / "node_modules" / "electron" / "dist" / ("electron.exe" if os.name == "nt" else "electron")

QISHUI_BRIDGE_CONFIG_FILE = DATA_DIR / ".qishui-qr-login.json"

QISHUI_BRIDGE_USER_DATA = DATA_DIR / "qishui-electron-user-data"

QISHUI_BRIDGE_LOCK = threading.Lock()

QISHUI_BRIDGE_PROCESS = None

QISHUI_AUDIO_TOKEN_LOCK = threading.Lock()

QISHUI_AUDIO_TOKENS = {}

DAILY_RECOMMENDATION_CACHE = {}

DAILY_PLAYLIST_CACHE = {}



DEFAULT_CONFIG = {

    "cookie": "",                # 网易云 MUSIC_U cookie

    "api_key": "",               # API 鉴权密钥

    "port": 8100,

    "host": "0.0.0.0",

    "log_retention": 1000,       # 保留最近 N 条调用记录

    "cookie_status": "unknown",  # ok / expired / empty / unknown

    "cookie_checked_at": None,

    "last_error": None,

    "nickname": None,

    "ncm_refreshed_at": None,

    "ncm_next_refresh_at": None,

    # QQ音乐字段

    "qq_cookie": "",

    "qq_musickey": "",

    "qq_refresh_token": "",

    "qq_access_token": "",

    "qq_openid": "",

    "qq_uin": "",

    "qq_unionid": "",

    "euin": "",

    "qq_nickname": "",

    "qq_status": "unknown",      # ok / expired / empty / unknown

    "qq_checked_at": None,

    "qq_last_error": None,

    "qq_refresh_key": "",

    "qq_refreshed_at": None,

    "qq_next_refresh_at": None,

    # Kugou provider fields

    "kugou_cookie": "",

    "kugou_status": "empty",      # ok / invalid / empty / unknown

    "kugou_checked_at": None,

    "kugou_last_error": None,

    # 酷狗概念版与普通酷狗保持独立的管理员会话；播放器仍只显示“酷狗音乐”。

    "kugou_concept_cookie": "",

    "kugou_concept_device": {},

    "kugou_concept_status": "empty",

    "kugou_concept_checked_at": None,

    "kugou_concept_last_error": None,

    "kugou_player_priority": "concept_first",  # concept_first / standard_first

    # Migu provider fields

    "migu_cookie": "",

    "migu_status": "empty",          # ok / invalid / empty / unknown

    "migu_checked_at": None,

    "migu_last_error": None,

    "migu_refreshed_at": None,

    "migu_next_refresh_at": None,

    # 自动保活：到点用各平台自身的刷新接口滚动会话，避免频繁重新扫码。
    "auto_renew_enabled": True,

    "ncm_renew_interval_hours": 24,

    "qq_renew_interval_hours": 24,

    "migu_renew_interval_hours": 3,

    # 键名沿用历史配置（原为健康检查间隔），现语义为概念版自动续期间隔。
    "kugou_concept_check_interval_hours": 6,

    "kugou_renew_interval_hours": 24,

    "kugou_refreshed_at": None,

    "kugou_concept_refreshed_at": None,

    "kugou_concept_checked_by_scheduler_at": None,

    "qishui_cookie": "",

    "qishui_status": "empty",

    "qishui_checked_at": None,

    "qishui_last_error": None,

    "qishui_next_check_at": None,

    "qishui_checked_by_scheduler_at": None,

    "qishui_session_refreshed_at": None,

    "qishui_check_interval_hours": 6,

    # 第三方音源配置

    "third_sources": {},    # {源标识: 源配置}

    "third_source_quality": "320k",
    "player_quality": "320k",

    # 自动播放回源的等待上限（秒）

    "player_third_source_timeout": 10,

    "player_resolve_timeout": 12,

    # 首轮无可用地址后，跨平台官方补源的最大等待时间（秒）

    "player_cross_platform_timeout": 4,

    # 跨平台官方补源顺序；当前平台会自动跳过，其余缺失项会补到末尾
    "player_cross_platform_priority": ["netease", "qq", "kugou", "migu", "qishui"],

    "announcement": "",

    # 是否允许新用户注册；旧配置缺失时由 load_config() 使用此默认值。
    "allow_registration": True,

}



app = FastAPI(title="点歌API", version="1.1.0")



# CORS

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")



app.add_middleware(

    CORSMiddleware,

    allow_origins=["*"],

    allow_credentials=True,

    allow_methods=["*"],

    allow_headers=["*"],

)



# ==================== 配置管理 ====================



def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)

        merged = {**DEFAULT_CONFIG, **cfg}
        # Configurations saved before the unified quality setting only have
        # third_source_quality. Do not let the new default (320k) mask it.
        if "player_quality" not in cfg and "third_source_quality" in cfg:
            merged["player_quality"] = cfg.get("third_source_quality")

        # Huibq/Bugu were retired completely. Remove stale records once so
        # they cannot reappear after a restart or in the admin source list.
        third_sources = merged.get("third_sources")
        if isinstance(third_sources, dict):
            retired = {sid for sid in ("huibq", "bugu") if sid in third_sources}
            if retired:
                for sid in retired:
                    third_sources.pop(sid, None)
                try:
                    with open(CONFIG_FILE, "w", encoding="utf-8") as output:
                        json.dump(merged, output, ensure_ascii=False, indent=2)
                except OSError:
                    logger.exception("Unable to remove retired third-party sources")
        return merged
    return dict(DEFAULT_CONFIG)

def save_config(cfg):

    with open(CONFIG_FILE, "w", encoding="utf-8") as f:

        json.dump(cfg, f, ensure_ascii=False, indent=2)





def _save_qishui_qr_diagnostic(scope, status, message, diagnostic=None):

    """Persist only non-sensitive Passport QR facts for troubleshooting."""

    raw = diagnostic if isinstance(diagnostic, dict) else {}

    safe = {

        "updated_at": datetime.now().isoformat(),

        "scope": "admin" if scope == "admin" else "account",

        "status": str(status or "unknown"),

        "message": str(message or "")[:240],

        "passport_status": str(raw.get("upstream_status") or raw.get("passport_status") or ""),

        "passport_error_code": str(raw.get("error_code") or raw.get("passport_error_code") or ""),

        "body_keys": [str(item) for item in raw.get("body_keys", [])][:30],

        "data_keys": [str(item) for item in raw.get("data_keys", [])][:30],

        "cookie_names": [str(item) for item in raw.get("cookie_names", [])][:30],

        "redirect_statuses": [int(item) for item in raw.get("redirect_statuses", []) if str(item).isdigit()][:10],

        "has_authorization_cookie": bool(raw.get("has_authorization_cookie")),

        "has_sessionid": bool(raw.get("has_sessionid")),

        "has_authorization_redirect": bool(raw.get("has_authorization_redirect")),

        "has_session_cookie": bool(raw.get("has_session_cookie")),

        "profile_valid": bool(raw.get("profile_valid")),

    }

    try:

        QISHUI_QR_DIAGNOSTIC_FILE.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")

    except OSError:

        logger.warning("Unable to save Qishui QR diagnostic")

    return safe





def load_logs():

    if LOG_FILE.exists():

        with open(LOG_FILE, "r", encoding="utf-8") as f:

            return json.load(f)

    return []





def save_log(log_entry):

    cfg = load_config()

    logs = load_logs()

    logs.insert(0, log_entry)

    retention = cfg.get("log_retention", 1000)

    if len(logs) > retention:

        logs = logs[:retention]

    with open(LOG_FILE, "w", encoding="utf-8") as f:

        json.dump(logs, f, ensure_ascii=False, indent=2)







def _credential_cipher():

    """Create or load the server-only key used for third-party sessions."""

    try:

        key = CREDENTIAL_KEY_FILE.read_bytes() if CREDENTIAL_KEY_FILE.exists() else b""

        if not key:

            key = Fernet.generate_key()

            CREDENTIAL_KEY_FILE.write_bytes(key)

        return Fernet(key)

    except (OSError, ValueError) as exc:

        raise RuntimeError("????????????????") from exc





def _encrypt_credential(payload):

    return _credential_cipher().encrypt(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")





def _decrypt_credential(value):

    try:

        return json.loads(_credential_cipher().decrypt(str(value or "").encode("ascii")).decode("utf-8"))

    except (InvalidToken, UnicodeDecodeError, ValueError, TypeError):

        return {}





def load_connected_accounts(user_id=None):

    if CONNECTED_ACCOUNT_FILE.exists():

        try:

            data = json.loads(CONNECTED_ACCOUNT_FILE.read_text(encoding="utf-8"))

            accounts = data.get("accounts", []) if isinstance(data, dict) else []

            if isinstance(accounts, list):

                if user_id is not None:

                    accounts = [item for item in accounts if str(item.get("user_id")) == str(user_id)]

                return {"accounts": accounts}

        except (OSError, ValueError, TypeError):

            logger.warning("Unable to read connected accounts file; recreating it")

    return {"accounts": []}





def save_connected_accounts(data, user_id=None):

    incoming = data.get("accounts", []) if isinstance(data, dict) else []

    try:

        current = json.loads(CONNECTED_ACCOUNT_FILE.read_text(encoding="utf-8")) if CONNECTED_ACCOUNT_FILE.exists() else {"accounts": []}

    except (OSError, ValueError, TypeError):

        current = {"accounts": []}

    current_items = current.get("accounts", []) if isinstance(current, dict) else []

    if user_id is not None:

        owner = str(user_id)

        current_items = [item for item in current_items if str(item.get("user_id") or "") != owner]

        current_items.extend(incoming)

        payload = {"accounts": current_items}

    else:

        # Whole-file writes are reserved for callers that explicitly provide every user record.

        payload = {"accounts": incoming}

    CONNECTED_ACCOUNT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")





def _public_connected_account(account):

    return {

        key: account.get(key)

        for key in (

            "id", "platform", "nickname", "platform_user_id", "auth_type", "status",

            "status_message", "last_synced_at", "created_at", "updated_at",

        )

    }





def _normalize_playlist_cover(source, cover):

    """Normalize provider-specific cover URL templates before storing them."""

    if not isinstance(cover, str):

        return ""

    if str(source or "").lower() == "kugou":

        return cover.replace("{size}", "400")

    return cover





def _normalize_playlist_data(data):

    """Repair legacy playlist metadata without changing the deployment scheme."""

    if not isinstance(data, dict) or not isinstance(data.get("playlists"), list):

        return data, False

    changed = False

    for playlist in data["playlists"]:

        if not isinstance(playlist, dict) or not isinstance(playlist.get("songs"), list):

            continue

        for song in playlist["songs"]:

            if not isinstance(song, dict):

                continue

            source = song.get("source") or song.get("origin_platform")

            old_cover = song.get("cover", "")

            new_cover = _normalize_playlist_cover(source, old_cover)

            if new_cover != old_cover:

                song["cover"] = new_cover

                changed = True

    return data, changed





def _playlist_song(song):

    """Keep only stable, safe metadata in a local playlist."""

    if not isinstance(song, dict):

        return None

    song_id = str(song.get("id") or song.get("song_id") or song.get("songmid") or "").strip()

    title = str(song.get("title") or song.get("name") or "").strip()

    if not song_id or not title:

        return None

    source = str(song.get("source") or "netease")

    return {

        "id": song_id,

        "source": source,

        "title": title,

        "artist": str(song.get("artist") or song.get("singer") or "未知歌手"),

        "album": str(song.get("album") or ""),

        "cover": _normalize_playlist_cover(source, str(song.get("cover") or song.get("album_pic") or "")),

        "duration": int(song.get("duration") or 0),

    }





def load_playlists(user_id=None):

    if PLAYLIST_FILE.exists():

        try:

            data = json.loads(PLAYLIST_FILE.read_text(encoding="utf-8"))

            data, changed = _normalize_playlist_data(data)

            if changed:

                PLAYLIST_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

            playlists = data.get("playlists", []) if isinstance(data, dict) else []

            if isinstance(playlists, list):

                if user_id is not None:

                    playlists = [

                        item for item in playlists

                        if str(item.get("user_id") or "") == str(user_id)

                    ]

                return {"playlists": playlists}

        except (OSError, ValueError, TypeError):

            logger.warning("Unable to read playlists file; recreating it")

    now = datetime.now().isoformat()

    default_playlist = {

        "id": "liked",

        "name": "我喜欢的音乐",

        "created_at": now,

        "updated_at": now,

        "songs": [],

    }

    if user_id is not None:

        default_playlist["user_id"] = str(user_id)

    return {"playlists": [default_playlist]}





def save_playlists(data):

    data, _ = _normalize_playlist_data(data)

    incoming = data.get("playlists", []) if isinstance(data, dict) else []

    try:

        current = json.loads(PLAYLIST_FILE.read_text(encoding="utf-8")) if PLAYLIST_FILE.exists() else {"playlists": []}

    except (OSError, ValueError, TypeError):

        current = {"playlists": []}

    current_items = current.get("playlists", []) if isinstance(current, dict) else []

    owners = {str(item.get("user_id")) for item in incoming if item.get("user_id")}

    if owners:

        current_items = [

            item for item in current_items

            if str(item.get("user_id") or "") not in owners

        ]

        current_items.extend(incoming)

        payload = {"playlists": current_items}

    else:

        payload = {"playlists": incoming}

    PLAYLIST_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")





def _find_playlist(data, playlist_id):

    return next(

        (item for item in data.get("playlists", [])

         if str(item.get("id")) == str(playlist_id)),

        None,

    )





def load_users():

    if USER_FILE.exists():

        try:

            data = json.loads(USER_FILE.read_text(encoding="utf-8"))

            if isinstance(data, dict) and isinstance(data.get("users"), list):

                return data

        except (OSError, ValueError, TypeError):

            logger.warning("Unable to read users file; recreating it")

    return {"users": []}





def save_users(data):

    USER_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")





def _save_sessions(sessions=None):

    active = sessions if sessions is not None else SESSIONS

    payload = {

        token: {"user_id": session["user_id"], "expires_at": session["expires_at"].isoformat()}

        for token, session in active.items()

    }

    SESSION_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")





def _load_sessions():

    if not SESSION_FILE.exists():

        return {}

    try:

        raw = json.loads(SESSION_FILE.read_text(encoding="utf-8"))

    except (OSError, ValueError, TypeError):

        logger.warning("Unable to read persisted sessions; starting with no sessions")

        return {}

    sessions, now, changed = {}, datetime.now(), False

    for token, session in (raw.items() if isinstance(raw, dict) else []):

        try:

            expires_at = datetime.fromisoformat(str(session.get("expires_at") or ""))

            user_id = str(session.get("user_id") or "")

        except (AttributeError, TypeError, ValueError):

            changed = True

            continue

        if not token or not user_id or expires_at <= now:

            changed = True

            continue

        sessions[str(token)] = {"user_id": user_id, "expires_at": expires_at}

    if changed:

        _save_sessions(sessions)

    return sessions





SESSIONS.update(_load_sessions())



def _public_user(user):

    return {

        "id": user.get("id"),

        "username": user.get("username"),

        "display_name": user.get("display_name") or user.get("username"),

        "role": user.get("role", "user"),

        "banned": bool(user.get("banned")),

        "created_at": user.get("created_at"),

        "banned_at": user.get("banned_at"),

    }




def _client_ip(request: Request):

    """Return a trustworthy client IP, honoring proxy headers only from trusted peers."""

    peer = str(request.client.host if request.client else "").strip()

    configured = {

        item.strip() for item in os.getenv("TRUSTED_PROXY_IPS", "").split(",") if item.strip()

    }

    trusted_peers = {"127.0.0.1", "::1", "localhost", *configured}

    if peer not in trusted_peers:

        return peer

    candidates = []

    for header in ("cf-connecting-ip", "x-real-ip"):

        value = str(request.headers.get(header) or "").strip()

        if value:

            candidates.append(value)

    candidates.extend(

        item.strip() for item in str(request.headers.get("x-forwarded-for") or "").split(",") if item.strip()

    )

    for candidate in candidates:

        try:

            return str(ipaddress.ip_address(candidate))

        except ValueError:

            continue

    return peer





def _revoke_user_sessions(user_id):

    """Invalidate every active browser session for one user."""

    revoked = [token for token, session in SESSIONS.items() if str(session.get("user_id")) == str(user_id)]

    for token in revoked:

        SESSIONS.pop(token, None)

    if revoked:

        _save_sessions()





def _delete_user_data(user_id):

    """Permanently remove playlists and encrypted connected-account data for a deleted user."""

    owner = str(user_id)

    try:

        payload = json.loads(PLAYLIST_FILE.read_text(encoding="utf-8")) if PLAYLIST_FILE.exists() else {"playlists": []}

    except (OSError, ValueError, TypeError):

        payload = {"playlists": []}

    playlists = payload.get("playlists", []) if isinstance(payload, dict) else []

    PLAYLIST_FILE.write_text(json.dumps({"playlists": [item for item in playlists if str(item.get("user_id") or "") != owner]}, ensure_ascii=False, indent=2), encoding="utf-8")



    try:

        payload = json.loads(CONNECTED_ACCOUNT_FILE.read_text(encoding="utf-8")) if CONNECTED_ACCOUNT_FILE.exists() else {"accounts": []}

    except (OSError, ValueError, TypeError):

        payload = {"accounts": []}

    accounts = payload.get("accounts", []) if isinstance(payload, dict) else []

    CONNECTED_ACCOUNT_FILE.write_text(json.dumps({"accounts": [item for item in accounts if str(item.get("user_id") or "") != owner]}, ensure_ascii=False, indent=2), encoding="utf-8")





def _hash_password(password, salt=None):

    salt = salt or secrets.token_hex(16)

    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 240000).hex()

    return f"pbkdf2_sha256$240000${salt}${digest}"





def _check_password(password, encoded):

    try:

        algorithm, rounds, salt, expected = encoded.split("$", 3)

        if algorithm != "pbkdf2_sha256":

            return False

        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), int(rounds)).hex()

        return hmac.compare_digest(actual, expected)

    except (AttributeError, TypeError, ValueError):

        return False





def _user_by_id(user_id):

    return next((item for item in load_users().get("users", []) if str(item.get("id")) == str(user_id)), None)





def _session_user(request):

    token = request.cookies.get(SESSION_COOKIE)

    session = SESSIONS.get(token) if token else None

    if not session:

        return None

    if session["expires_at"] <= datetime.now():

        SESSIONS.pop(token, None)

        _save_sessions()

        return None

    user = _user_by_id(session["user_id"])

    if not user or user.get("banned"):

        SESSIONS.pop(token, None)

        _save_sessions()

        return None

    return user





def _require_user(request):

    user = _session_user(request)

    if not user:

        raise HTTPException(status_code=401, detail="请先登录")

    return user





def _require_admin(request):

    user = _require_user(request)

    if user.get("role") != "admin":

        raise HTTPException(status_code=403, detail="只有管理员可以访问后台")

    return user



def _online_presence_cleanup(now=None):

    now = time.monotonic() if now is None else now

    expired = [

        client_id for client_id, presence in ONLINE_PRESENCE.items()

        if now - float(presence.get("last_seen") or 0) > ONLINE_PRESENCE_TTL

    ]

    for client_id in expired:

        ONLINE_PRESENCE.pop(client_id, None)



def _online_presence_count():

    return len({

        str(presence.get("user_id")) for presence in ONLINE_PRESENCE.values()

        if presence.get("user_id")

    })



def _online_presence_client_id(value):

    client_id = str(value or "").strip()

    if not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", client_id):

        raise HTTPException(status_code=422, detail="在线会话标识无效")

    return client_id





def _migrate_legacy_playlists(user_id):

    if not PLAYLIST_FILE.exists():

        return

    try:

        data = json.loads(PLAYLIST_FILE.read_text(encoding="utf-8"))

    except (OSError, ValueError, TypeError):

        return

    changed = False

    for playlist in data.get("playlists", []):

        if isinstance(playlist, dict) and not playlist.get("user_id"):

            playlist["user_id"] = user_id

            changed = True

    if changed:

        # This is a one-time whole-file migration, not a per-user playlist update.

        PLAYLIST_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")





@app.middleware("http")

async def access_control_middleware(request: Request, call_next):

    path = request.url.path

    if path in ("/music", "/music/"):

        if not _session_user(request):

            return RedirectResponse("/login?next=/music", status_code=303)

    elif path.startswith("/admin"):

        user = _session_user(request)

        if not user or user.get("role") != "admin":

            if path in ("/admin", "/admin/"):

                return RedirectResponse("/login?next=/admin", status_code=303)

            return JSONResponse({"code": -1, "msg": "管理员登录后才能访问"}, status_code=403)

    elif path.startswith("/api/playlists") and not path.startswith("/api/auth"):

        if not _session_user(request):

            return JSONResponse({"code": -1, "msg": "请先登录"}, status_code=401)

    return await call_next(request)





# ==================== Cookie 解析工具 ====================



def parse_cookie_kv(cookie_str, key):

    """从 cookie 字符串中提取指定 key 的值"""

    if not cookie_str or not key:

        return ""

    for item in cookie_str.split(";"):

        item = item.strip()

        if not item:

            continue

        if "=" not in item:

            continue

        k, _, v = item.partition("=")

        if k.strip() == key:

            return v.strip()

    return ""





def extract_qq_cookie_fields(cookie_str):

    """从完整 QQ音乐 cookie 中提取关键字段"""

    fields = {

        "qq_musickey": parse_cookie_kv(cookie_str, "qqmusic_key") or parse_cookie_kv(cookie_str, "qm_keyst"),

        "qq_uin": parse_cookie_kv(cookie_str, "uin"),

        "euin": parse_cookie_kv(cookie_str, "euin"),

        "qq_refresh_token": parse_cookie_kv(cookie_str, "psrf_qqrefresh_token"),

        "qq_access_token": parse_cookie_kv(cookie_str, "psrf_qqaccess_token"),

        "qq_openid": parse_cookie_kv(cookie_str, "psrf_qqopenid"),

        "qq_unionid": parse_cookie_kv(cookie_str, "psrf_qqunionid"),

    }

    # uin 通常形如 o0123456789，去掉前缀 o

    if fields["qq_uin"].startswith("o") or fields["qq_uin"].startswith("O"):

        uin_digits = fields["qq_uin"][1:]

        if uin_digits.isdigit():

            fields["qq_uin"] = uin_digits

    return fields





# ==================== 网易云 API ====================



NCM_SESSION = requests.Session()

NCM_BASE = "https://music.163.com"





def make_ncm_headers(cookie=None):

    """构造网易云请求头"""

    config = load_config()

    ck = cookie or config.get("cookie", "")

    headers = {

        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",

        "Referer": "https://music.163.com/",

        "Origin": "https://music.163.com",

    }

    if ck:

        headers["Cookie"] = ck if "MUSIC_U=" in ck else f"MUSIC_U={ck}; appver=2.10.0; os=pc;"

    return headers





def ncm_request(endpoint, params=None, method="GET", cookie=None):

    """调用网易云 API"""

    url = f"{NCM_BASE}{endpoint}"

    headers = make_ncm_headers(cookie)

    

    try:

        if method == "POST":

            resp = NCM_SESSION.post(url, data=params or {}, headers=headers, timeout=15)

        else:

            resp = NCM_SESSION.get(url, params=params or {}, headers=headers, timeout=15)

        

        if resp.status_code == 200:

            return resp.json()

        return {"code": resp.status_code, "msg": f"HTTP {resp.status_code}"}

    except Exception as e:

        return {"code": -1, "msg": str(e)}





def search_song(keyword, page=1, limit=5, cookie=None):

    """

    搜索歌曲 - 使用 cloudsearch 接口（返回完整专辑封面）

    """

    offset = (page - 1) * limit

    params = {

        "s": keyword,

        "type": 1,      # 1=单曲

        "limit": limit,

        "offset": offset,

    }

    # cloudsearch/pc 返回更完整数据（含 album.picUrl），需要 POST

    result = ncm_request("/api/cloudsearch/pc", params=params, method="POST", cookie=cookie)

    if result.get("code") != 200:

        # 降级用 search/get

        result = ncm_request("/api/search/get", params=params, cookie=cookie)

    return result





def normalize_player_quality(value):
    """Normalize the admin/player quality setting to supported levels."""
    value = str(value or "320k").strip().lower()
    value = {"standard": "128k", "high": "320k", "lossless": "flac", "sq": "flac", "hq": "320k"}.get(value, value)
    return value if value in ("128k", "320k", "flac", "hires") else "320k"


def quality_fallbacks(value):
    return {"hires": ["hires", "flac", "320k", "128k"], "flac": ["flac", "320k", "128k"], "320k": ["320k", "128k"], "128k": ["128k"]}[normalize_player_quality(value)]


def configured_player_quality(cfg):
    """Read the unified quality setting, including configs saved before this feature."""
    cfg = cfg or {}
    return normalize_player_quality(cfg.get("player_quality") or cfg.get("third_source_quality") or "320k")


OFFICIAL_PLAYER_PLATFORMS = ("netease", "qq", "kugou", "migu", "qishui")


def configured_cross_platform_priority(cfg):
    """Return a validated, complete order for cross-platform official fallback."""
    raw = (cfg or {}).get("player_cross_platform_priority") or []
    if isinstance(raw, str):
        raw = [x.strip() for x in raw.split(",") if x.strip()]
    result = []
    if isinstance(raw, (list, tuple)):
        for platform in raw:
            platform = str(platform or "").strip().lower()
            if platform in OFFICIAL_PLAYER_PLATFORMS and platform not in result:
                result.append(platform)
    for platform in OFFICIAL_PLAYER_PLATFORMS:
        if platform not in result:
            result.append(platform)
    return result


def netease_bitrate_for_quality(value):
    return {"128k": 128000, "320k": 320000, "flac": 999000, "hires": 999000}.get(
        normalize_player_quality(value), 320000
    )


def get_song_url(song_id, br=320000, cookie=None):

    """

    获取歌曲播放地址

    br: 128000/192000/320000/999000(FLAC)

    """

    params = {

        "ids": f"[{song_id}]",

        "br": br,

    }

    result = ncm_request("/api/song/enhance/player/url/v1", params=params, method="POST", cookie=cookie)

    if result.get("code") != 200:

        # 尝试旧接口

        result = ncm_request("/api/song/enhance/player/url", params=params, method="POST", cookie=cookie)

    return result





def get_song_lyric(song_id, cookie=None):

    """获取歌词"""

    result = ncm_request("/api/song/lyric", params={"id": song_id, "lv": -1}, cookie=cookie)

    return result





# ==================== QQ音乐 API ====================



QQ_SESSION = requests.Session()

QQ_SESSION.headers.update({

    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",

    "Referer": "https://y.qq.com/",

    "Origin": "https://y.qq.com",

})





def qq_get_gtk(cookie_str):

    """QQ音乐 g_tk 计算 — 从 p_skey 或 qqmusic_key 派生"""

    if not cookie_str:

        return 5381

    skey = ""

    for item in cookie_str.split(";"):

        item = item.strip()

        if item.startswith("p_skey=") or item.startswith("qqmusic_key="):

            skey = item.split("=", 1)[1] if "=" in item else ""

            break

    if not skey:

        return 5381

    h = 5381

    for c in skey:

        h += (h << 5) + ord(c)

    return h & 0x7fffffff





def get_qq_cookie_str(cfg=None):

    """构建请求用的 QQ音乐 cookie 字符串: uin=xxx; qm_keyst=xxx; qqmusic_key=xxx

    qm_keyst 是 QQ 新版鉴权键，缺失时 musicu.fcg 一律按未登录处理（vkey 无 purl）；

    qqmusic_key 需同时保留，qq_get_gtk() 依赖它计算 g_tk。

    """

    cfg = load_config() if not cfg else cfg

    parts = []

    uin = cfg.get("qq_uin", "")

    musickey = cfg.get("qq_musickey", "")

    if uin:

        parts.append(f"uin=o{uin}")

    if musickey:

        parts.append(f"qm_keyst={musickey}")

        parts.append(f"qqmusic_key={musickey}")

    return "; ".join(parts)





def qq_request_cookie(cfg=None):

    """播放/搜索/歌单链路统一使用的 QQ cookie。

    以后台保存的完整 cookie 串为底，再用 config 权威字段（qq_musickey/qq_uin）覆盖或补齐

    uin / qm_keyst / qqmusic_key。原因：扫码下发的串可能没有 qm_keyst，或串里的 key 已被

    续期作废，此时 vkey 会静默返回空 purl（后台判活却仍显示已登录）。

    """

    cfg = load_config() if not cfg else cfg

    raw = str(cfg.get("qq_cookie", "") or "")

    if not raw:

        return get_qq_cookie_str(cfg)

    musickey = str(cfg.get("qq_musickey", "") or "")

    uin = str(cfg.get("qq_uin", "") or "")

    overrides = {}

    if musickey:

        overrides["qm_keyst"] = musickey

        overrides["qqmusic_key"] = musickey

    parts = []

    seen = set()

    for item in raw.split(";"):

        item = item.strip()

        if not item or "=" not in item:

            continue

        name, _, value = item.partition("=")

        name = name.strip()

        if name in seen:

            continue

        seen.add(name)

        parts.append(f"{name}={overrides.get(name, value.strip())}")

    for name, value in overrides.items():

        if name not in seen:

            parts.append(f"{name}={value}")

    if uin and "uin" not in seen:

        parts.append(f"uin=o{uin}")

    return "; ".join(parts)





def qq_search_song(keyword, page=1, limit=5, cookie=None):

    """QQ音乐搜索歌曲 - 使用 search_for_qq_cp 接口"""

    params = {

        "format": "json",

        "n": limit,

        "p": page,

        "w": keyword,

        "cr": 1,

        "g_tk": qq_get_gtk(cookie) if cookie else 5381,

        "t": 0,

        "searchid": int(time.time() * 1000),

        "aggr": 1,

        "lossless": 0,

        "flag_qc": 0,

    }

    try:

        headers = {"Referer": "https://y.qq.com/"}

        if cookie:

            headers["Cookie"] = cookie

        resp = QQ_SESSION.get(

            "https://c.y.qq.com/soso/fcgi-bin/search_for_qq_cp",

            params=params,

            headers=headers,

            timeout=15

        )

        if resp.status_code == 200:

            return resp.json()

        return {"code": resp.status_code, "msg": f"HTTP {resp.status_code}"}

    except Exception as e:

        return {"code": -1, "msg": str(e)}





KUGOU_SESSION = requests.Session()

KUGOU_SESSION.headers.update({

    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",

    "Referer": "https://www.kugou.com/",

})





def _kugou_headers(cookie=None):

    headers = {"Referer": "https://www.kugou.com/yy/html/search.html"}

    if cookie:

        headers["Cookie"] = cookie

    return headers





def kugou_search_song(keyword, page=1, limit=20, cookie=None):

    """Search Kugou's public web catalogue; a configured session is sent when available."""

    params = {

        "keyword": keyword, "page": page, "pagesize": limit, "platform": "WebFilter",

        "filter": 2, "iscorrection": 1, "privilege_filter": 0,

    }

    try:

        response = KUGOU_SESSION.get("https://songsearch.kugou.com/song_search_v2", params=params, headers=_kugou_headers(cookie), timeout=20)

        result = response.json() if response.ok else {}

        if response.ok and result.get("status") == 1:

            return result

        return {"status": 0, "msg": result.get("error_msg") or f"HTTP {response.status_code}"}

    except (requests.RequestException, ValueError) as exc:

        return {"status": 0, "msg": str(exc)}





def kugou_get_play_data(file_hash, album_id="", cookie=None, quality="320k"):

    """Resolve Kugou playback. Entitlement remains controlled by Kugou and the supplied session."""

    fields = _kugou_cookie_fields(cookie or "")

    params = {

        "r": "play/getdata", "hash": file_hash, "album_id": album_id or "0",

        "dfid": fields.get("dfid") or "-", "mid": fields.get("mid") or "",

        "platid": 4, "userid": fields.get("userid") or fields.get("web_userid") or "0",
        "quality": {"128k": 128, "320k": 320, "flac": 1000, "hires": 1000}.get(normalize_player_quality(quality), 320),
        "_": int(time.time() * 1000),

    }

    try:

        response = KUGOU_SESSION.get("https://wwwapi.kugou.com/yy/index.php", params=params, headers=_kugou_headers(cookie), timeout=20)

        result = response.json() if response.ok else {}

        if not response.ok:

            return {"status": 0, "msg": f"HTTP {response.status_code}", "data": {}}

        return result if isinstance(result, dict) else {"status": 0, "msg": "Kugou response was invalid", "data": {}}

    except (requests.RequestException, ValueError) as exc:

        return {"status": 0, "msg": str(exc), "data": {}}







KUGOU_APP_APPID = "1005"

KUGOU_APP_CLIENTVER = "20489"

KUGOU_APP_SIGNATURE_SALT = "OIlwieks28dk2k092lksi2UIkp"





def _kugou_app_sign_key(data):

    """Kugou Android `key` parameter: md5(appid + salt + clientver + data)."""

    return hashlib.md5(f"{KUGOU_APP_APPID}{KUGOU_APP_SIGNATURE_SALT}{KUGOU_APP_CLIENTVER}{data}".encode()).hexdigest()





def _kugou_app_signature(params, body=""):

    """Standard-edition Android signature; JSON values are serialized like the client does."""

    pieces = []

    for key in sorted(params):

        value = params[key]

        if isinstance(value, (dict, list)):

            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))

        pieces.append(f"{key}={value}")

    canonical = "".join(pieces)

    return hashlib.md5(f"{KUGOU_APP_SIGNATURE_SALT}{canonical}{body}{KUGOU_APP_SIGNATURE_SALT}".encode()).hexdigest()



KUGOU_CONCEPT_APPID = "3116"

KUGOU_CONCEPT_CLIENTVER = "11440"

KUGOU_CONCEPT_SIGNATURE_SALT = "LnT6xpN3khm36zse0QzvmgTZ3waWdRSA"





def _kugou_concept_device_from_cookie(cookie, fallback=None):

    """Build a stable Concept Edition device record from a QR-authorized session."""

    fallback = fallback if isinstance(fallback, dict) else {}

    guid = parse_cookie_kv(cookie or "", "KUGOU_API_GUID") or fallback.get("guid") or uuid.uuid4().hex.upper()

    mid = parse_cookie_kv(cookie or "", "KUGOU_API_MID") or fallback.get("mid") or hashlib.md5(guid.encode()).hexdigest().upper()

    return {

        "guid": guid,

        "mid": mid,

        "dfid": parse_cookie_kv(cookie or "", "dfid") or fallback.get("dfid") or "-",

        "mac": parse_cookie_kv(cookie or "", "KUGOU_API_MAC") or fallback.get("mac") or secrets.token_hex(6).upper(),

        "dev": parse_cookie_kv(cookie or "", "KUGOU_API_DEV") or fallback.get("dev") or secrets.token_hex(8).upper(),

    }





def _kugou_concept_signature(params, body=""):

    """Match the Android Concept Edition parameter signature, including JSON values."""

    pieces = []

    for key in sorted(params):

        value = params[key]

        if isinstance(value, (dict, list)):

            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))

        pieces.append(f"{key}={value}")

    canonical = "".join(pieces)

    return hashlib.md5(f"{KUGOU_CONCEPT_SIGNATURE_SALT}{canonical}{body}{KUGOU_CONCEPT_SIGNATURE_SALT}".encode()).hexdigest()





def _kugou_concept_headers(cookie, device, clienttime, router=""):

    headers = {

        "User-Agent": "Android15-1070-11083-46-0-DiscoveryDRADProtocol-wifi",

        "dfid": device.get("dfid") or "-",

        "clienttime": str(clienttime),

        "mid": device.get("mid") or "",

        "kg-rc": "1",

        "kg-thash": "5d816a0",

        "kg-rec": "1",

        "kg-rf": "B9EDA08A64250DEFFBCADDEE00F8F25F",

        "Cookie": cookie or "",

    }

    if router:

        headers["x-router"] = router

    return headers





def _kugou_concept_request(method, url, *, cookie, device, router="", params=None, payload=None, timeout=20):

    """Issue a signed Concept Edition request without sharing the standard Kugou session."""

    fields = _kugou_cookie_fields(cookie or "")

    now = str(int(time.time()))

    request_params = {

        "dfid": device.get("dfid") or fields.get("dfid") or "-",

        "mid": device.get("mid") or fields.get("mid") or "",

        "uuid": "-",

        "appid": KUGOU_CONCEPT_APPID,

        "clientver": KUGOU_CONCEPT_CLIENTVER,

        "clienttime": now,

    }

    if fields.get("token"):

        request_params["token"] = fields["token"]

    if fields.get("userid"):

        request_params["userid"] = fields["userid"]

    request_params.update(params or {})

    raw_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if payload is not None else ""

    request_params["signature"] = _kugou_concept_signature(request_params, raw_body)

    headers = _kugou_concept_headers(cookie, device, now, router)

    if payload is not None:

        headers["Content-Type"] = "application/json"

    response = requests.request(method, url, params=request_params, data=raw_body or None, headers=headers, timeout=timeout)

    try:

        data = response.json()

    except ValueError:

        data = {}

    if not response.ok:

        return {"status": 0, "msg": (data.get("msg") if isinstance(data, dict) else "") or f"HTTP {response.status_code}", "data": {}}

    return data if isinstance(data, dict) else {"status": 0, "msg": "Kugou Concept response was invalid", "data": {}}





def kugou_concept_search_song(keyword, page=1, limit=20, cookie="", device=None):

    """Search through the Concept Edition catalogue using its own administrator session."""

    if not cookie:

        return {"status": 0, "msg": "Kugou Concept Edition is not authorized", "data": {}}

    device = _kugou_concept_device_from_cookie(cookie, device)

    result = _kugou_concept_request(

        "GET", "https://gateway.kugou.com/v3/search/song", cookie=cookie, device=device,

        router="complexsearch.kugou.com",

        params={"albumhide": 0, "iscorrection": 1, "keyword": keyword, "nocollect": 0, "page": page, "pagesize": limit, "platform": "AndroidFilter"},

    )

    data = result.get("data") if isinstance(result, dict) else {}

    records = (data or {}).get("lists") or (data or {}).get("info") or (data or {}).get("list") or []

    if isinstance(records, list):

        return {"status": 1, "data": {"lists": records}, "raw": result}

    return {"status": 0, "msg": result.get("msg") or result.get("error") or "Kugou Concept search failed", "data": {}}





def kugou_concept_get_play_data(file_hash, cookie="", device=None, album_id="", album_audio_id="", quality="320k"):

    """Resolve a Concept Edition playback URL; access still follows the authorized account entitlement."""

    if not cookie or not file_hash:

        return {"status": 0, "msg": "Kugou Concept Edition is not authorized", "data": {}}

    device = _kugou_concept_device_from_cookie(cookie, device)

    fields = _kugou_cookie_fields(cookie)

    normalized_hash = str(file_hash).strip().lower()

    # /v1/audio/audio returns metadata only. Concept Edition resolves the

    # authorized stream through the tracker v5/url endpoint.

    try:

        numeric_album_id = int(album_id or 0)

    except (TypeError, ValueError):

        numeric_album_id = 0

    try:

        numeric_album_audio_id = int(album_audio_id or 0)

    except (TypeError, ValueError):

        numeric_album_audio_id = 0

    player_clientver = 11430

    player_params = {

        "album_id": numeric_album_id,

        "area_code": 1,

        "hash": normalized_hash,

        "ssa_flag": "is_fromtrack",

        "version": player_clientver,

        "page_id": 967177915,

        "quality": {"128k": 128, "320k": 320, "flac": 1000, "hires": 1000}.get(normalize_player_quality(quality), 320),

        "album_audio_id": numeric_album_audio_id,

        "behavior": "play",

        "pid": 411,

        "cmd": 26,

        "pidversion": 3001,

        "IsFreePart": 0,

        "ppage_id": "356753938,823673182,967485191",

        "cdnBackup": 1,

        "module": "",

        "clientver": player_clientver,

        "key": hashlib.md5(

            f"{normalized_hash}185672dd44712f60bb1736df5a377e82"

            f"{KUGOU_CONCEPT_APPID}{device.get('mid') or fields.get('mid') or ''}"

            f"{fields.get('userid') or 0}".encode()

        ).hexdigest(),

    }

    result = _kugou_concept_request(

        "GET", "https://gateway.kugou.com/v5/url", cookie=cookie, device=device,

        router="trackercdn.kugou.com", params=player_params,

    )

    urls = result.get("url") if isinstance(result, dict) else []

    if isinstance(urls, str):

        urls = [urls]

    url = next((str(value).strip() for value in urls if str(value).strip()), "") if isinstance(urls, list) else ""

    if url:

        return {"status": 1, "msg": "", "data": {"url": url, "backup_url": result.get("backupUrl") or []}}

    return {

        "status": 0,

        "msg": (result.get("error") or result.get("msg") or "Kugou Concept Edition did not return a playable URL") if isinstance(result, dict) else "Kugou Concept Edition did not return a playable URL",

        "data": {},

    }





def _kugou_concept_available(cfg):

    return bool(str(cfg.get("kugou_concept_cookie") or "").strip())





def _player_kugou_search(keyword, page, limit, cfg):

    """Use the administrator-selected Kugou provider first, then transparently fall back."""

    concept_cookie = str(cfg.get("kugou_concept_cookie") or "").strip()

    concept_device = cfg.get("kugou_concept_device") or {}

    standard_cookie = cfg.get("kugou_cookie", "")

    providers = [("concept", concept_cookie), ("standard", standard_cookie)]

    if cfg.get("kugou_player_priority") == "standard_first":

        providers.reverse()

    last_result = {"status": 0, "msg": "Kugou search failed", "data": {}}

    for provider, cookie in providers:

        if provider == "concept":

            if not cookie:

                continue

            result = kugou_concept_search_song(keyword, page=page, limit=limit, cookie=cookie, device=concept_device)

        else:

            result = kugou_search_song(keyword, page=page, limit=limit, cookie=cookie)

        records = (result.get("data") or {}).get("lists") or []

        if result.get("status") == 1 and records:

            return result, provider

        last_result = result

    return last_result, ""





# 酷狗 native 分支的两个子源与其对外音源名（resolved_by）的对应关系。
KUGOU_NATIVE_PROVIDER_SOURCES = {"concept": "kugou_concept", "standard": "kugou"}


def _player_kugou_native_playback(song, cfg, quality=None, excluded_sources=None):

    """Resolve Kugou playback in the configured provider order and quality fallback order."""
    quality = normalize_player_quality(quality or configured_player_quality(cfg))
    song_id = song.get("id", "")
    concept_cookie = str(cfg.get("kugou_concept_cookie") or "").strip()
    concept_device = cfg.get("kugou_concept_device") or {}
    excluded = {str(item).strip() for item in (excluded_sources or []) if str(item).strip()}
    providers = ["concept", "standard"]
    if cfg.get("kugou_player_priority") == "standard_first":
        providers.reverse()
    # 上一次返回的地址在浏览器里放不出来时，前端会把当次音源名（kugou_concept /
    # kugou）回传到 exclude 再试一次。这里必须按子源名过滤：否则被排除的子源会再
    # 返回同一个死链，请求永远走不到跨平台回源。
    providers = [
        provider
        for provider in providers
        if KUGOU_NATIVE_PROVIDER_SOURCES[provider] not in excluded
    ]
    if not providers:
        return "", ""

    for requested_quality in quality_fallbacks(quality):
        for provider in providers:
            if provider == "concept":
                if not concept_cookie:
                    continue
                result = kugou_concept_get_play_data(
                    song_id,
                    cookie=concept_cookie,
                    device=concept_device,
                    album_id=song.get("extra", ""),
                    quality=requested_quality,
                )
            else:
                result = kugou_get_play_data(
                    song_id,
                    album_id=song.get("extra", ""),
                    cookie=cfg.get("kugou_cookie", ""),
                    quality=requested_quality,
                )

            data = result.get("data") or {}
            if isinstance(data, list):
                data = data[0] if data else {}
            if not isinstance(data, dict):
                data = {}

            if requested_quality == "128k":
                url = data.get("play_url_128") or data.get("play_url") or data.get("url") or ""
            elif requested_quality in ("flac", "hires"):
                url = (
                    data.get("play_url_flac24") or data.get("play_url_flac")
                    or data.get("play_url_lossless") or data.get("play_url") or data.get("url") or ""
                )
            else:
                url = data.get("play_url_320") or data.get("play_url") or data.get("url") or ""

            if isinstance(url, str) and url.startswith(("http://", "https://")):
                return url, KUGOU_NATIVE_PROVIDER_SOURCES[provider]

    return "", ""


def _decode_kugou_lyric_content(content):

    if not content:

        return ""

    try:

        payload = base64.b64decode(content)

        if payload.startswith(b"krc1"):

            key = b"@Gaw^2tGQ61-\xce\xd2ni"

            decrypted = bytes(value ^ key[index % len(key)] for index, value in enumerate(payload[4:]))

            payload = zlib.decompress(decrypted)

        return payload.decode("utf-8-sig", errors="replace")

    except (ValueError, UnicodeDecodeError, zlib.error):

        return ""





def _normalize_kugou_lrc(text):

    """Keep standard LRC timestamps and turn simple KRC timing rows into LRC rows."""

    if not text:

        return ""

    if "[" in text and re.search(r"\[\d{1,2}:\d{2}(?:\.\d{1,3})?\]", text):

        return text.replace("\r\n", "\n")



    lines = []

    for raw in text.replace("\r\n", "\n").split("\n"):

        match = re.match(r"^\[(\d+),(\d+)\](.*)$", raw.strip())

        if not match:

            continue

        milliseconds = int(match.group(1))

        content = re.sub(r"<\d+,\d+,\d+>", "", match.group(3)).strip()

        if not content:

            continue

        minutes, seconds = divmod(milliseconds // 1000, 60)

        centiseconds = (milliseconds % 1000) // 10

        lines.append(f"[{minutes:02d}:{seconds:02d}.{centiseconds:02d}]{content}")

    return "\n".join(lines)





def kugou_get_lyric(file_hash, title="", artist="", duration=0):

    """Look up Kugou lyrics by hash, then download readable LRC before falling back to KRC."""

    if not file_hash:

        return ""

    params = {

        "ver": 1,

        "man": "yes",

        "client": "pc",

        "hash": file_hash,

        "keyword": " ".join(part for part in (artist, title) if part).strip(),

        "timelength": int(duration or 0),

    }

    try:

        response = KUGOU_SESSION.get("https://lyrics.kugou.com/search", params=params, headers=_kugou_headers(), timeout=10)

        result = response.json() if response.ok else {}

        candidates = result.get("candidates") or []

        if not candidates:

            return ""

        candidate = candidates[0]

        for fmt in ("lrc", "krc"):

            download = KUGOU_SESSION.get(

                "https://lyrics.kugou.com/download",

                params={

                    "ver": 1,

                    "client": "pc",

                    "id": candidate.get("id") or candidate.get("download_id"),

                    "accesskey": candidate.get("accesskey", ""),

                    "fmt": fmt,

                    "charset": "utf8",

                },

                headers=_kugou_headers(),

                timeout=10,

            )

            payload = download.json() if download.ok else {}

            lyric = _normalize_kugou_lrc(_decode_kugou_lyric_content(payload.get("content", "")))

            if lyric:

                return lyric

    except (requests.RequestException, ValueError, TypeError) as exc:

        logger.debug("kugou lyric error: %s", exc)

    return ""





QQ_MEDIA_MID_CACHE = {}
QQ_MEDIA_MID_LOCK = threading.Lock()
QQ_MEDIA_MID_TTL = 6 * 3600
QQ_MEDIA_MID_LIMIT = 4096


def qq_media_mid(song_mid, cookie=None):
    """返回 QQ 音乐该曲目在 CDN 上的文件 mid（media_mid）。

    QQ 的播放文件名基于 media_mid，而 search_for_qq_cp 只给 songmid，两者多数情况下
    并不相同。用 songmid 拼文件名时 vkey 接口依然返回 code=0 + 非空 purl，但真实请求
    是 404（errorcode:-46628 file not exists），前端拿到死链后表现为静默卡播放。
    这里改为按 songmid 查真实 media_mid，并做进程内缓存避免每次播放多一次请求。
    """
    song_mid = str(song_mid or "").strip()
    if not song_mid:
        return ""

    now = time.time()
    with QQ_MEDIA_MID_LOCK:
        cached = QQ_MEDIA_MID_CACHE.get(song_mid)
        if cached and now - cached[1] < QQ_MEDIA_MID_TTL:
            return cached[0]

    payload = json.dumps({
        "comm": {"format": "json", "ct": 24, "cv": 0},
        "req_0": {
            "module": "music.trackInfo.UniformRuleCtrl",
            "method": "CgiGetTrackInfo",
            "param": {"ids": [], "mids": [song_mid], "types": [0]},
        },
    })

    media_mid = ""
    try:
        headers = {"Referer": "https://y.qq.com/"}
        if cookie:
            headers["Cookie"] = cookie
        resp = QQ_SESSION.get(
            "https://u.y.qq.com/cgi-bin/musicu.fcg",
            params={"format": "json", "data": payload},
            headers=headers,
            timeout=10,
        )
        if resp.status_code == 200:
            tracks = ((resp.json().get("req_0") or {}).get("data") or {}).get("tracks") or []
            for track in tracks:
                if not isinstance(track, dict):
                    continue
                candidate = str(((track.get("file") or {}).get("media_mid")) or "").strip()
                if candidate:
                    media_mid = candidate
                    break
    except (requests.RequestException, ValueError, TypeError) as exc:
        logger.debug("qq media_mid lookup failed for %s: %s", song_mid, exc)

    if media_mid:
        with QQ_MEDIA_MID_LOCK:
            if len(QQ_MEDIA_MID_CACHE) >= QQ_MEDIA_MID_LIMIT:
                QQ_MEDIA_MID_CACHE.clear()
            QQ_MEDIA_MID_CACHE[song_mid] = (media_mid, now)
    return media_mid


def qq_get_vkey(song_mid, cookie=None, quality="320k"):

    """QQ音乐获取播放链接 vkey"""

    import json as _json

    uin = "0"

    if cookie:

        for item in cookie.split(";"):

            item = item.strip()

            if item.startswith("uin="):

                uin = item.split("=", 1)[1].replace("o", "").strip()

                break

    # 文件名必须用 media_mid；拿不到时宁可不传 filename，让 QQ 自己回填正确文件名，
    # 也绝不用 songmid 去猜（会拿到 404 死链且接口仍报 code=0）。
    media_mid = qq_media_mid(song_mid, cookie=cookie)
    qq_prefix = {"128k": "M500", "320k": "M800", "flac": "F000", "hires": "F000"}.get(normalize_player_quality(quality), "M800")
    qq_extension = "flac" if normalize_player_quality(quality) in ("flac", "hires") else "mp3"

    vkey_param = {
        "guid": "10000",
        "songmid": [song_mid],
        "songtype": [1],
        "uin": uin,
        "loginflag": 1,
        "platform": "20",
    }

    if media_mid:
        vkey_param["filename"] = [f"{qq_prefix}{media_mid}.{qq_extension}"]

    data_payload = _json.dumps({
        "req_0": {
            "module": "vkey.GetVkeyServer",
            "method": "CgiGetVkey",
            "param": vkey_param,
        },
        "comm": {
            "uin": int(uin) if uin.isdigit() else 0,
            "format": "json",
            "ct": 24,
            "cv": 0,
        }
    })

    try:

        headers = {"Referer": "https://y.qq.com/"}

        if cookie:

            headers["Cookie"] = cookie

        resp = QQ_SESSION.get(

            "https://u.y.qq.com/cgi-bin/musicu.fcg",

            params={"format": "json", "data": data_payload},

            headers=headers,

            timeout=15

        )

        if resp.status_code == 200:

            return resp.json()

        return {"code": resp.status_code}

    except Exception as e:

        return {"code": -1, "msg": str(e)}





def qq_get_lyric(song_mid, cookie=None):

    """QQ音乐获取歌词"""

    import base64

    params = {

        "songmid": song_mid,

        "pcachetime": int(time.time() * 1000),

        "g_tk": 5381,

        "loginUin": 0,

        "hostUin": 0,

        "inCharset": "utf8",

        "outCharset": "utf-8",

        "notice": 0,

        "platform": "yqq",

        "needNewCode": 0,

        "format": "json",

    }

    try:

        headers = {"Referer": "https://y.qq.com/"}

        if cookie:

            headers["Cookie"] = cookie

        resp = QQ_SESSION.get(

            "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg",

            params=params,

            headers=headers,

            timeout=15

        )

        if resp.status_code == 200:

            data = resp.json()

            # 歌词是 base64 编码的

            if "lyric" in data and data["lyric"]:

                try:

                    data["lyric"] = base64.b64decode(data["lyric"]).decode("utf-8")

                except Exception:

                    pass

            return data

        return {"code": resp.status_code}

    except Exception as e:

        return {"code": -1, "msg": str(e)}





def get_song_detail(song_ids, cookie=None):

    """批量获取歌曲详情"""

    c = ",".join(str(i) for i in song_ids) if isinstance(song_ids, list) else str(song_ids)

    result = ncm_request(f"/api/v3/song/detail?c=[{c}]", cookie=cookie)

    return result





def get_ncm_user_info(cookie=None):

    """获取网易云用户信息（用于检测 cookie 是否有效）"""

    # 需要 POST，且用更可靠的用户接口

    result = ncm_request("/api/v1/user/detail", params={"uid": 0}, cookie=cookie)

    if result.get("code") != 200:

        # 尝试另一个接口

        result = ncm_request("/api/nuser/account/get", cookie=cookie)

    if result.get("code") != 200:

        # 再尝试用 /api/w/nuser/account/get 

        result = ncm_request("/api/w/nuser/account/get", cookie=cookie)

    return result





def check_ncm_cookie():

    """检测网易云 cookie 是否有效 — 通过搜索一个已知歌曲来验证"""

    cfg = load_config()

    cookie = cfg.get("cookie", "")

    if not cookie:

        cfg["cookie_status"] = "empty"

        cfg["cookie_checked_at"] = datetime.now().isoformat()

        save_config(cfg)

        return False, None

    

    # 用搜索接口验证 cookie（比用户接口更可靠）

    result = search_song("一路向北", page=1, limit=1, cookie=cookie)

    

    if result.get("code") == 200 and result.get("result", {}).get("songs"):

        # cookie 有效，顺带获取用户信息

        user_result = get_ncm_user_info(cookie=cookie)

        nickname = None

        if user_result.get("code") == 200 and user_result.get("profile"):

            nickname = user_result["profile"].get("nickname")

        

        cfg["cookie_status"] = "ok"

        cfg["cookie_checked_at"] = datetime.now().isoformat()

        cfg["nickname"] = nickname or "已验证"

        cfg["last_error"] = None

        save_config(cfg)

        return True, nickname

    else:

        error_msg = result.get("msg", "Cookie 无效或已过期")

        cfg["cookie_status"] = "expired"

        cfg["cookie_checked_at"] = datetime.now().isoformat()

        cfg["last_error"] = error_msg

        save_config(cfg)

        return False, None





# ==================== QQ音乐 cookie 检查与续期 ====================



def qq_probe_login(cookie_str):

    """用 GetLoginUserInfo 判定 QQ 音乐登录态。

    返回 (是否有效, 昵称, 错误信息)。搜索/歌词接口不校验登录，只有该接口能真实反映会话状态。

    """

    if not cookie_str:

        return False, "", "缺少 QQ 音乐 Cookie"

    payload = {

        "req_0": {"module": "music.UserInfo.userInfoServer", "method": "GetLoginUserInfo", "param": {}},

        "comm": {"ct": 24, "cv": 0, "uin": 0, "format": "json"},

    }

    try:

        resp = QQ_SESSION.get(

            "https://u.y.qq.com/cgi-bin/musicu.fcg",

            params={"format": "json", "data": json.dumps(payload, separators=(",", ":"))},

            headers={"Referer": "https://y.qq.com/", "Cookie": cookie_str},

            timeout=15,

        )

        result = resp.json() if resp.status_code == 200 else {}

    except (requests.RequestException, ValueError) as exc:

        return False, "", f"QQ 音乐登录态检测失败：{exc}"

    node = result.get("req_0") if isinstance(result, dict) else None

    node = node if isinstance(node, dict) else {}

    data = node.get("data") if isinstance(node.get("data"), dict) else {}

    info = data.get("info") if isinstance(data.get("info"), dict) else {}

    nickname = str(info.get("nick") or "")

    if node.get("code") == 0 and data.get("errMsg") == "OK":

        return True, nickname, ""

    return False, nickname, str(data.get("errMsg") or f"QQ 音乐会话无效（code={node.get('code')}）")





def check_qq_cookie():

    """用搜索验证 qq_musickey 是否有效"""

    import json as _json

    cfg = load_config()

    musickey = cfg.get("qq_musickey", "")

    if not musickey:

        cfg["qq_status"] = "empty"

        cfg["qq_checked_at"] = datetime.now().isoformat()

        save_config(cfg)

        return False

    

    # 搜索接口不需要登录态，不能用来判活；改用必须登录的 GetLoginUserInfo

    cookie_str = qq_request_cookie(cfg)

    ok, nickname, error_msg = qq_probe_login(cookie_str)

    

    if ok:

        cfg["qq_status"] = "ok"

        cfg["qq_checked_at"] = datetime.now().isoformat()

        cfg["qq_last_error"] = None

        cfg["qq_nickname"] = nickname or cfg.get("qq_nickname", "")

        save_config(cfg)

        return True

    else:

        error_msg = error_msg or "QQ音乐 Cookie 无效或已过期"

        cfg["qq_status"] = "expired"

        cfg["qq_checked_at"] = datetime.now().isoformat()

        cfg["qq_last_error"] = error_msg

        save_config(cfg)

        return False





# ==================== 会话自动保活（各平台官方刷新接口） ====================



RENEW_INTERVAL_LIMITS = {

    # key: (默认小时, 最小小时, 最大小时)

    "ncm_renew_interval_hours": (24, 1, 168),

    # QQ 的 keyExpiresIn 实测为 259200 秒（3 天），上限必须小于该值。

    "qq_renew_interval_hours": (24, 1, 60),

    "migu_renew_interval_hours": (3, 1, 24),

    # 酷狗两端都走 /v5/login_by_token 续期；键名沿用历史配置，语义已是续期间隔。

    "kugou_concept_check_interval_hours": (6, 1, 168),

    "kugou_renew_interval_hours": (24, 1, 168),


}


RENEW_RETRY_HOURS = 1



def renew_interval_hours(cfg, key):

    """读取后台可配置的续期间隔，并夹紧到平台允许范围内。"""

    default, low, high = RENEW_INTERVAL_LIMITS[key]

    try:

        value = int(float((cfg or {}).get(key, default)))

    except (TypeError, ValueError):

        return default

    return max(low, min(high, value))



def _schedule_next(cfg, prefix, hours):

    cfg[f"{prefix}_next_refresh_at"] = (datetime.now() + timedelta(hours=hours)).isoformat()



def _renew_due(cfg, prefix):

    """未设置过下次时间的历史配置视为立即到期，避免保活永不触发。"""

    raw = cfg.get(f"{prefix}_next_refresh_at")

    if not raw:

        return True

    try:

        return datetime.now() >= datetime.fromisoformat(str(raw))

    except ValueError:

        return True



def qq_renew_musickey():

    """用 musickey + refresh_key 续期 QQ 音乐会话

    接口: u.y.qq.com/cgi-bin/musicu.fcg (POST)

    module: music.login.LoginServer, method: Login

    必须带 comm(uin/authst/tmeLoginType) 与 param.str_musicid/musickey，

    否则返回 code=104400。返回体的 keyExpiresIn 实测为 259200 秒。

    """

    cfg = load_config()

    uin = str(cfg.get("qq_uin", "") or "")

    musickey = str(cfg.get("qq_musickey", "") or "")

    retry_hours = RENEW_RETRY_HOURS

    if not uin or not musickey:

        cfg["qq_last_error"] = "缺少 qq_uin / qq_musickey，请先扫码或填写 Cookie"

        _schedule_next(cfg, "qq", retry_hours)

        save_config(cfg)

        return False

    comm = {

        "ct": 11,

        "cv": 12080008,

        "v": 12080008,

        "tmeAppID": "qqmusic",

        "uin": uin,

        "authst": musickey,

        "tmeLoginType": 2,

    }

    param = {

        "str_musicid": uin,

        "musickey": musickey,

        "refresh_key": str(cfg.get("qq_refresh_key", "") or ""),

        "refresh_token": str(cfg.get("qq_refresh_token", "") or ""),

        "loginMode": 2,

        "login_type": 2,

        "openid": str(cfg.get("qq_openid", "") or ""),

        "access_token": str(cfg.get("qq_access_token", "") or ""),

    }

    payload = {

        "comm": comm,

        "music.login.LoginServer": {

            "module": "music.login.LoginServer",

            "method": "Login",

            "param": param,

        },

    }

    headers = {

        "Referer": "https://y.qq.com/",

        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",

        "Cookie": f"uin={uin}; qm_keyst={musickey};",

    }

    try:

        resp = QQ_SESSION.post(

            "https://u.y.qq.com/cgi-bin/musicu.fcg",

            json=payload,

            headers=headers,

            timeout=20,

        )

        if resp.status_code != 200:

            cfg["qq_last_error"] = f"续期请求失败: HTTP {resp.status_code}"

            _schedule_next(cfg, "qq", retry_hours)

            save_config(cfg)

            return False

        data = resp.json()

    except (requests.RequestException, ValueError) as exc:

        cfg["qq_last_error"] = f"续期异常: {type(exc).__name__}"

        _schedule_next(cfg, "qq", retry_hours)

        save_config(cfg)

        print(f"❌ QQ音乐续期异常: {type(exc).__name__}")

        return False



    node = data.get("music.login.LoginServer") or {}

    result_data = node.get("data") or {}

    new_musickey = str(result_data.get("musickey") or "")

    if node.get("code") != 0 or not new_musickey:

        error_msg = str(result_data.get("errMsg") or node.get("code") or "续期返回异常")

        cfg["qq_last_error"] = f"续期失败: {error_msg}"

        cfg["qq_status"] = "expired"

        _schedule_next(cfg, "qq", retry_hours)

        save_config(cfg)

        print(f"❌ QQ音乐续期失败: {error_msg}")

        return False



    cfg["qq_musickey"] = new_musickey

    for field, key in (

        ("qq_refresh_key", "refresh_key"),

        ("qq_refresh_token", "refresh_token"),

        ("qq_access_token", "access_token"),

        ("qq_openid", "openid"),

        ("qq_unionid", "unionid"),

    ):

        value = str(result_data.get(key) or "")

        if value:

            cfg[field] = value

    if result_data.get("encryptUin"):

        cfg["euin"] = str(result_data["encryptUin"])

    # 同步完整 cookie 串里的 key，播放/搜索链路仍读它。

    # 键缺失时必须追加：扫码下发的串可能没有 qm_keyst，只做替换会让 vkey 永久拿不到 purl。

    stored_cookie = str(cfg.get("qq_cookie", "") or "")

    if stored_cookie:

        for cookie_key in ("qm_keyst", "qqmusic_key"):

            if parse_cookie_kv(stored_cookie, cookie_key):

                stored_cookie = re.sub(

                    rf"(^|;\s*){re.escape(cookie_key)}=[^;]*",

                    lambda mo: f"{mo.group(1)}{cookie_key}={new_musickey}",

                    stored_cookie,

                )

            else:

                stored_cookie = f"{stored_cookie.rstrip('; ')}; {cookie_key}={new_musickey}"

        cfg["qq_cookie"] = stored_cookie



    now = datetime.now()

    hours = renew_interval_hours(cfg, "qq_renew_interval_hours")

    expires_in = result_data.get("keyExpiresIn")

    try:

        expires_hours = int(expires_in) / 3600 if expires_in else 0

    except (TypeError, ValueError):

        expires_hours = 0

    if expires_hours:

        # 留出一半余量，避免间隔被设置到超过官方有效期。

        hours = max(1, min(hours, int(expires_hours * 0.5) or 1))

    cfg["qq_refreshed_at"] = now.isoformat()

    _schedule_next(cfg, "qq", hours)

    cfg["qq_status"] = "ok"

    cfg["qq_last_error"] = None

    save_config(cfg)

    print(f"✅ QQ音乐 musickey 续期成功 (下次: {cfg['qq_next_refresh_at']})")

    return True



def migu_renew_cookie():

    """滚动咪咕会话：认证态由 pacmtoken 承载，每次带 cookie 请求会员资料

    接口都会通过 Set-Cookie 下发新的 pacmtoken，合并回配置即可续期。

    """

    cfg = load_config()

    cookie = str(cfg.get("migu_cookie", "") or "").strip()

    now = datetime.now()

    if not cookie:

        cfg.update({

            "migu_status": "empty",

            "migu_last_error": None,

            "migu_checked_at": now.isoformat(),

        })

        _schedule_next(cfg, "migu", renew_interval_hours(cfg, "migu_renew_interval_hours"))

        save_config(cfg)

        return False

    try:

        ok, new_cookie, message = migu_renew_session(cookie)

    except Exception as exc:

        cfg["migu_last_error"] = f"续期异常: {type(exc).__name__}"

        _schedule_next(cfg, "migu", RENEW_RETRY_HOURS)

        save_config(cfg)

        print(f"❌ 咪咕续期异常: {type(exc).__name__}")

        return False

    cfg = load_config()

    if not ok:

        cfg.update({

            "migu_status": "invalid",

            "migu_checked_at": now.isoformat(),

            "migu_last_error": message,

        })

        _schedule_next(cfg, "migu", RENEW_RETRY_HOURS)

        save_config(cfg)

        print(f"❌ 咪咕续期失败: {message}")

        return False

    cfg.update({

        "migu_cookie": new_cookie,

        "migu_status": "ok",

        "migu_checked_at": now.isoformat(),

        "migu_last_error": None,

        "migu_refreshed_at": now.isoformat(),

    })

    _schedule_next(cfg, "migu", renew_interval_hours(cfg, "migu_renew_interval_hours"))

    save_config(cfg)

    print(f"✅ 咪咕会话续期成功 (下次: {cfg['migu_next_refresh_at']})")

    return True



NCM_RENEW_COOKIE_FIELDS = ("MUSIC_U", "MUSIC_A_T", "MUSIC_R_T", "MUSIC_SNS", "__csrf", "NMTID")



def _merge_ncm_cookie(current, adapter_cookie):

    """只合并适配器返回的登录字段，忽略 Path/Max-Age 等属性片段。"""

    updates = {}

    for item in str(adapter_cookie or "").replace("\n", ";").split(";"):

        item = item.strip()

        if not item or "=" not in item:

            continue

        key, _, value = item.partition("=")

        key = key.strip()

        if key in NCM_RENEW_COOKIE_FIELDS and value.strip():

            updates[key] = value.strip()

    if not updates:

        return str(current or "").strip(), []

    merged = migu_merge_cookie(current, updates)

    return merged, sorted(updates)



def ncm_renew_cookie():

    """通过本地网易云适配器调用 /login/refresh 滚动 MUSIC_U。

    该接口需要 weapi/eapi 加签，项目自身没有加密实现，因此必须走 Node 适配器。

    """

    cfg = load_config()

    cookie = str(cfg.get("cookie", "") or "").strip()

    now = datetime.now()

    if not cookie:

        cfg.update({"cookie_status": "empty", "cookie_checked_at": now.isoformat()})

        _schedule_next(cfg, "ncm", renew_interval_hours(cfg, "ncm_renew_interval_hours"))

        save_config(cfg)

        return False

    try:

        payload = _netease_qr_adapter_request("/login/refresh", {

            "cookie": cookie,

            "timestamp": int(time.time() * 1000),

        })

    except Exception as exc:

        cfg["last_error"] = f"续期失败: {exc}"

        _schedule_next(cfg, "ncm", RENEW_RETRY_HOURS)

        save_config(cfg)

        print(f"❌ 网易云续期失败: {exc}")

        return False

    if payload.get("code") != 200:

        message = str(payload.get("message") or payload.get("msg") or payload.get("code"))

        cfg["last_error"] = f"续期失败: {message}"

        cfg["cookie_status"] = "expired"

        _schedule_next(cfg, "ncm", RENEW_RETRY_HOURS)

        save_config(cfg)

        print(f"❌ 网易云续期失败: {message}")

        return False

    merged, updated_fields = _merge_ncm_cookie(cookie, payload.get("cookie"))

    cfg = load_config()

    if updated_fields:

        cfg["cookie"] = merged

    cfg["cookie_status"] = "ok"

    cfg["cookie_checked_at"] = now.isoformat()

    cfg["last_error"] = None

    cfg["ncm_refreshed_at"] = now.isoformat()

    _schedule_next(cfg, "ncm", renew_interval_hours(cfg, "ncm_renew_interval_hours"))

    save_config(cfg)

    print(f"✅ 网易云会话续期成功 (更新字段: {','.join(updated_fields) or '无'})")

    return True



def _kugou_renew_common(cookie_key, prefix, interval_key, label, lite, fallback_check):

    """酷狗两端共用的官方续期流程：调 /v5/login_by_token 滚动 token 与 t1。

    lite=True 走概念版参数（appid=3116），lite=False 走标准酷狗参数（appid=1005）。

    """

    cfg = load_config()

    cookie = str(cfg.get(cookie_key, "") or "").strip()

    now = datetime.now()

    if not cookie:

        cfg[f"{prefix}_checked_by_scheduler_at"] = now.isoformat()

        _schedule_next(cfg, prefix, renew_interval_hours(cfg, interval_key))

        save_config(cfg)

        return False

    try:

        ok, new_cookie, message = kugou_renew_session(cookie, lite=lite)

    except Exception as exc:

        ok, new_cookie, message = False, cookie, f"{type(exc).__name__}: {exc}"

    if ok:

        cfg = load_config()

        cfg[cookie_key] = new_cookie

        cfg[f"{prefix}_status"] = "ok"

        cfg[f"{prefix}_checked_at"] = now.isoformat()

        cfg[f"{prefix}_last_error"] = None

        cfg[f"{prefix}_refreshed_at"] = now.isoformat()

        cfg[f"{prefix}_checked_by_scheduler_at"] = now.isoformat()

        _schedule_next(cfg, prefix, renew_interval_hours(cfg, interval_key))

        save_config(cfg)

        print(f"✅ {message}")

        return True

    # 续期失败不代表会话已死：用平台自身的健康检查确认真实状态，再决定重试节奏。

    try:

        valid = bool(fallback_check())

    except Exception as exc:

        print(f"⚠️ {label} 健康检查异常: {type(exc).__name__}")

        valid = False

    cfg = load_config()

    cfg[f"{prefix}_checked_by_scheduler_at"] = now.isoformat()

    cfg[f"{prefix}_last_error"] = f"续期失败但会话仍可用: {message}" if valid else message

    _schedule_next(cfg, prefix, renew_interval_hours(cfg, interval_key) if valid else RENEW_RETRY_HOURS)

    save_config(cfg)

    print(f"❌ {message}")

    return False





def kugou_concept_renew_cookie():

    """酷狗概念版会话续期（官方 /v5/login_by_token，lite 变体）。"""

    return _kugou_renew_common(

        "kugou_concept_cookie",

        "kugou_concept",

        "kugou_concept_check_interval_hours",

        "酷狗概念版",

        True,

        check_kugou_concept_cookie,

    )





def kugou_renew_cookie():

    """普通酷狗会话续期；官方刷新接口只支持 App 会话，网页 Cookie 退化为健康检查。"""

    cfg = load_config()

    cookie = str(cfg.get("kugou_cookie", "") or "").strip()

    if cookie and _kugou_cookie_mode(_kugou_cookie_fields(cookie)) != "app":

        try:

            valid = bool(check_kugou_cookie())

        except Exception as exc:

            print(f"⚠️ 酷狗音乐健康检查异常: {type(exc).__name__}")

            valid = False

        cfg = load_config()

        cfg["kugou_checked_by_scheduler_at"] = datetime.now().isoformat()

        if not valid and not cfg.get("kugou_last_error"):

            cfg["kugou_last_error"] = "会话已失效，请重新扫码授权"

        _schedule_next(cfg, "kugou", renew_interval_hours(cfg, "kugou_renew_interval_hours") if valid else RENEW_RETRY_HOURS)

        save_config(cfg)

        return valid

    return _kugou_renew_common(

        "kugou_cookie",

        "kugou",

        "kugou_renew_interval_hours",

        "酷狗音乐",

        False,

        check_kugou_cookie,

    )



def auto_renew_scheduler():

    """统一的会话保活调度线程：每 5 分钟检查各平台是否到点。"""

    print("🕐 会话自动保活调度线程已启动")

    tasks = (

        ("qq", qq_renew_musickey, "QQ音乐"),

        ("migu", migu_renew_cookie, "咪咕音乐"),

        ("ncm", ncm_renew_cookie, "网易云音乐"),

        ("kugou_concept", kugou_concept_renew_cookie, "酷狗概念版"),

        ("kugou", kugou_renew_cookie, "酷狗音乐"),

    )

    while True:

        try:

            cfg = load_config()

            if cfg.get("auto_renew_enabled", True):

                for prefix, handler, label in tasks:

                    try:

                        current_cfg = load_config()
                        due = _renew_due(current_cfg, prefix)

                        if due:

                            print(f"⏰ 到期执行 {label} 会话保活...")

                            handler()

                    except Exception as exc:

                        print(f"⚠️ {label} 保活异常: {type(exc).__name__}: {exc}")

        except Exception as exc:

            print(f"⚠️ 会话保活调度异常: {exc}")

        time.sleep(300)



def format_song_result(song, lyric_text=""):

    """将歌曲信息格式化为最终结果"""

    return {

        "id": song.get("id"),

        "name": song.get("name"),

        "artists": "/".join(a.get("name", "") for a in song.get("ar", []) or song.get("artists", [])),

        "album": (song.get("al", {}) or {}).get("name", "") if isinstance(song.get("al"), dict) else song.get("album", {}).get("name", ""),

        "album_pic": (song.get("al", {}) or {}).get("picUrl", "").replace("http:", "https:") if isinstance(song.get("al"), dict) else "",

        "duration": song.get("dt", song.get("duration", 0)),

        "url": song.get("url", ""),

        "br": song.get("br", 0),

        "lyric": lyric_text,

        "fee": song.get("fee", 0),  # 0=免费 1=VIP 4=付费 8=无版权

    }





# ==================== XML 生成 ====================



def build_music_xml(song_info, play_url=None):

    """生成微信音乐卡片 XML"""

    title = song_info.get("name", "未知歌曲")

    artists = song_info.get("artists", "未知歌手")

    album = song_info.get("album", "")

    desc = f"{artists}"

    if album:

        desc += f" - {album}"

    

    url = play_url or song_info.get("url", "")

    album_pic = song_info.get("album_pic", "")

    lyric = song_info.get("lyric", "")

    

    # 截取歌词前 500 字符

    if lyric and len(lyric) > 500:

        lyric = lyric[:500] + "..."

    

    xml = f"""<appmsg appid="wx1ebb9c41ccbfb6d4" sdkver="0">

<title>{escape_xml(title)}</title>

<des>{escape_xml(desc)}</des>

<type>76</type>

<url>{escape_xml(url)}</url>

<lowurl>{escape_xml(url)}</lowurl>

<dataurl>{escape_xml(url)}</dataurl>

<lowdataurl>{escape_xml(url)}</lowdataurl>

<songalbumurl>{escape_xml(album_pic)}</songalbumurl>

<songlyric>

{escape_xml(lyric)}

</songlyric>

<appattach>

<cdnthumbaeskey/>

<aeskey/>

</appattach>

</appmsg>"""

    return xml.strip()





def escape_xml(text):

    """XML 转义"""

    if not text:

        return ""

    text = str(text)

    text = text.replace("&", "&amp;")

    text = text.replace("<", "&lt;")

    text = text.replace(">", "&gt;")

    text = text.replace('"', "&quot;")

    text = text.replace("'", "&apos;")

    return text





# ==================== API 鉴权 ====================



def check_auth(key: str):

    cfg = load_config()

    expected_key = cfg.get("api_key", "")

    if not expected_key:

        return True  # 未设置 key 时允许所有请求

    if key != expected_key:

        return False

    return True





# ==================== 第三方音源辅助函数 ====================



def _get_all_source_configs(cfg):

    """合并内置源 + 用户添加的自定义源"""

    user_sources = cfg.get("third_sources", {}) or {}

    all_sources = {}

    # 内置源

    for sid, sdef in _BUILTIN_SOURCES.items():

        source_config = sdef.copy()

        source_config["enabled"] = user_sources.get(sid, {}).get("enabled", False)

        all_sources[sid] = source_config

    # 自定义源

    for sid, sdef in user_sources.items():

        if sid in _BUILTIN_SOURCES:

            continue

        sdef["enabled"] = sdef.get("enabled", False)

        sdef["builtin"] = False

        all_sources[sid] = sdef

    return all_sources





# ==================== API 路由 ====================



def _player_third_sources(cfg):

    return {

        sid: source

        for sid, source in _get_all_source_configs(cfg).items()

        if source.get("enabled") and _source_usage_snapshot(source)["available"]

    }





def _source_usage_snapshot(source):

    """Return normalized quota facts without exposing any source secret."""

    try:

        limit = int(source.get("usage_limit", 0) or 0)

    except (TypeError, ValueError):

        limit = 0

    try:

        count = max(0, int(source.get("usage_count", 0) or 0))

    except (TypeError, ValueError):

        count = 0

    unlimited = limit <= 0

    return {

        "usage_limit": limit,

        "usage_count": count,

        "usage_remaining": None if unlimited else max(0, limit - count),

        "usage_unlimited": unlimited,

        "available": unlimited or count < limit,

    }





def _public_source_config(source):

    """Strip source API keys before a third-party config reaches the browser."""

    public = dict(source or {})

    api_key = public.pop("api_key", "")

    public["api_key_configured"] = bool(api_key)

    public["api_key_masked"] = "已配置" if api_key else "未配置"

    public.update(_source_usage_snapshot(source or {}))

    return public





def _public_candidate(candidate):

    public = _public_source_config(candidate)

    if "test" in candidate:

        public["test"] = candidate.get("test")

    return public




def _public_inspection(inspection):

    public = dict(inspection or {})

    public["candidates"] = [

        _public_candidate(item)

        for item in (inspection or {}).get("candidates", [])

    ]

    return public




def _consume_third_source_quota(source_id):

    """Consume one unit only after a resolver returned a valid playback URL."""

    with THIRD_SOURCE_USAGE_LOCK:

        cfg = load_config()

        sources = cfg.get("third_sources", {}) or {}

        source = sources.get(source_id)

        if not source:

            return False

        snapshot = _source_usage_snapshot(source)

        if not snapshot["available"]:

            return False

        if not snapshot["usage_unlimited"]:

            source["usage_count"] = snapshot["usage_count"] + 1

            save_config(cfg)

        return True





def _player_netease_song(song):

    return {

        "id": str(song.get("id", "")),

        "source": "netease",

        "title": song.get("name", ""),

        "artist": "/".join(a.get("name", "") for a in (song.get("ar", []) or song.get("artists", []))),

        "album": (song.get("al", {}) or {}).get("name", ""),

        "cover": ((song.get("al", {}) or {}).get("picUrl", "") or "").replace("http:", "https:"),

        "duration": song.get("dt", 0),

    }





def _player_qq_song(song):

    album = song.get("album") if isinstance(song.get("album"), dict) else {}

    album_mid = song.get("albummid") or album.get("mid", "")

    return {

        "id": str(song.get("songmid") or song.get("mid") or ""),

        "source": "qq",

        "title": song.get("songname") or song.get("name", ""),

        "artist": "/".join(

            s.get("name", "") if isinstance(s, dict) else str(s)

            for s in (song.get("singer", []) or [])

        ),

        "album": song.get("albumname") or album.get("name", ""),

        "cover": f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{album_mid}.jpg" if album_mid else "",

        "duration": int(song.get("interval") or 0) * 1000,

    }





def _player_kugou_song(song):

    """Normalize public Kugou search and playlist records for the local player."""

    album = song.get("album_info") if isinstance(song.get("album_info"), dict) else {}

    hash_value = str(song.get("hash") or song.get("filehash") or song.get("FileHash") or song.get("audio_id") or "")

    try:

        raw_duration = float(song.get("duration") or song.get("Duration") or song.get("timelength") or 0)

    except (TypeError, ValueError):

        raw_duration = 0

    cover = song.get("img") or song.get("Image") or song.get("cover") or album.get("img") or ""

    return {

        "id": hash_value,

        "source": "kugou",

        "title": song.get("filename") or song.get("FileName") or song.get("songname") or song.get("SongName") or song.get("song_name") or song.get("name") or "",

        "artist": song.get("singername") or song.get("SingerName") or song.get("author_name") or song.get("singer") or "",

        "album": song.get("album_name") or song.get("AlbumName") or song.get("albumname") or album.get("name") or "",

        "cover": cover.replace("{size}", "400") if isinstance(cover, str) else "",

        "duration": int(raw_duration * 1000) if raw_duration < 10000 else int(raw_duration),

        "extra": str(song.get("AlbumID") or song.get("album_id") or song.get("albumid") or ""),

    }





def _player_migu_song(song):

    """Normalize Migu catalogue records without exposing adapter-only URLs."""

    return {

        "id": str(song.get("id") or ""),

        "source": "migu",

        "title": song.get("title") or song.get("name") or "",

        "artist": song.get("artist") or "",

        "album": song.get("album") or "",

        "cover": song.get("cover") or "",

        "duration": int(song.get("duration") or 0),

        "extra": str(song.get("extra") or ""),

    }


def _player_qishui_song(song):

    """Normalize Qishui bridge records for the shared player/search contract."""

    song = song if isinstance(song, dict) else {}

    try:

        raw_duration = float(song.get("duration") or song.get("duration_ms") or 0)

    except (TypeError, ValueError):

        raw_duration = 0

    duration = int(raw_duration * 1000) if 0 < raw_duration < 10000 else int(raw_duration)

    return {

        "id": str(song.get("id") or song.get("providerSongId") or ""),

        "source": "qishui",

        "title": song.get("name") or song.get("title") or "",

        "artist": song.get("artist") or "",

        "album": song.get("album") or "",

        "cover": song.get("cover") or "",

        "duration": duration,

        "extra": str(song.get("providerSongId") or ""),

        "vip": bool(song.get("fee") or song.get("vip")),

        "playable": bool(song.get("playable")),

        "playback_mode": str(song.get("playbackMode") or ""),

    }





def _player_kuwo_song(song):

    """Normalize Kuwo playlist records for the local player."""

    return {

        "id": str(song.get("rid") or song.get("musicrid") or song.get("id") or "").replace("MUSIC_", ""),

        "source": "kuwo",

        "title": song.get("name") or song.get("songName") or song.get("songname") or "",

        "artist": song.get("artist") or song.get("artistName") or song.get("singer") or "",

        "album": song.get("album") or song.get("albumName") or "",

        "cover": song.get("pic") or song.get("albumpic") or song.get("albumPic") or "",

        "duration": int(song.get("duration") or song.get("durationMs") or 0),

    }





def _player_aggregate_results(netease_songs, qq_songs, limit):

    """按歌名和歌手去重，保留各平台的原始歌曲标识。"""

    results, seen = [], set()

    candidates = (

        [_player_netease_song(item) for item in netease_songs]

        + [_player_qq_song(item) for item in qq_songs]

    )

    for song in candidates:

        fingerprint = (

            "".join((song.get("title") or "").lower().split()),

            "".join((song.get("artist") or "").lower().split()),

        )

        if fingerprint in seen:

            continue

        seen.add(fingerprint)

        results.append(song)

        if len(results) >= limit:

            break

    return results





def _resolve_player_third_source(song_source, song, cfg, preferred_source="", excluded_sources=None, quality=None):

    enabled = _player_third_sources(cfg)

    excluded = {item.strip() for item in (excluded_sources or []) if item and item.strip()}

    if preferred_source and preferred_source not in ("auto", "native"):

        source = enabled.get(preferred_source)

        if not source:

            return None, None

        enabled = {preferred_source: source}

    attempted = set(excluded)

    while enabled:

        candidates = {sid: source for sid, source in enabled.items() if sid not in attempted}

        if not candidates:

            break

        url, resolved_by = try_third_sources(

            song_source,

            song,

            candidates,

            quality or configured_player_quality(cfg),

        )

        if not url or not resolved_by:

            break

        if _consume_third_source_quota(resolved_by):

            return url, resolved_by

        attempted.add(resolved_by)

    return None, None



def _configured_qishui_cookie(cfg):

    """Return the server-only Qishui cookie context without serializing it to clients."""

    credential = _decrypt_credential(cfg.get("qishui_cookie", ""))

    cookie = str(credential.get("cookie") or "").strip()

    if cookie:

        return cookie

    sessionid = str(credential.get("sessionid") or "").strip()

    return f"sessionid={sessionid};" if sessionid else ""





def _user_qishui_cookie(user_id):

    """Return the current user's Qishui cookie context from its encrypted account record."""

    for account in load_connected_accounts(user_id).get("accounts", []):

        if str(account.get("platform") or "").lower() != "qishui":

            continue

        cookie = _account_cookie(account).strip()

        if cookie:

            return cookie

    return ""





def _player_native_lyric(song_source, song, cfg):

    """Fetch lyrics from the search platform while playback may come from a third-party source."""

    song_id = song.get("id", "")

    if song_source == "qishui":

        try:

            cookie = _configured_qishui_cookie(cfg)

            return qishui_lyric(song_id, sessionid=cookie) if cookie else qishui_lyric(song_id)

        except (QishuiError, requests.RequestException, ValueError):

            return ""

    if song_source == "migu":

        return migu_lyric(song_id)

    if song_source == "qq":

        result = qq_get_lyric(song_id, cookie=qq_request_cookie(cfg))

        return result.get("lyric", "") if result.get("code") == 0 else ""

    if song_source == "kugou":

        return kugou_get_lyric(

            song_id,

            title=song.get("title") or song.get("name") or "",

            artist=song.get("artist") or song.get("singer") or "",

            duration=song.get("duration") or 0,

        )

    if song_source == "netease":

        result = get_song_lyric(song_id, cookie=cfg.get("cookie", ""))

        if result.get("code") == 200:

            return (result.get("lrc", {}) or {}).get("lyric", "") or ""

        if result.get("nolyric") and result.get("tlyric"):

            return (result.get("tlyric", {}) or {}).get("lyric", "") or ""

    return ""





def _resolve_qq_play_url(song_mid, cfg, quality=None):
    """Request QQ at the selected quality and fall back only when unavailable."""
    cookie = qq_request_cookie(cfg or None)
    for requested_quality in quality_fallbacks(quality or configured_player_quality(cfg)):
        result = qq_get_vkey(song_mid, cookie=cookie, quality=requested_quality)
        data = result.get("req_0", {}).get("data", {}) if result.get("code") == 0 else {}
        midurlinfo, sip = data.get("midurlinfo", []), data.get("sip", [])
        purl = midurlinfo[0].get("purl", "") if midurlinfo else ""
        if purl:
            return (sip[0] if sip else "https://dl.stream.qqmusic.qq.com/") + purl
    return ""


def _resolve_netease_play_url(song_id, cfg, cookie=None, quality=None):
    """Request NetEase at the selected bitrate and degrade when the track lacks it."""
    cookie = (cfg or {}).get("cookie", "") if cookie is None else cookie
    for requested_quality in quality_fallbacks(quality or configured_player_quality(cfg)):
        result = get_song_url(
            song_id,
            br=netease_bitrate_for_quality(requested_quality),
            cookie=cookie,
        )
        data = result.get("data") or []
        url = data[0].get("url", "") if result.get("code") == 200 and data else ""
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            return url
    return ""


def _player_native_playback(song_source, song, cfg, quality=None, excluded_sources=None):

    """Resolve only the official URL for the track's own platform."""

    quality = normalize_player_quality(quality or configured_player_quality(cfg))
    song_id = song.get("id", "")

    if song_source == "qq":

        url = _resolve_qq_play_url(song_id, cfg, quality)
        return (url, "qq") if url else ("", "")

    if song_source == "kugou":

        return _player_kugou_native_playback(song, cfg, quality, excluded_sources=excluded_sources)

    if song_source == "migu":

        # Use only the configured Migu account and the Migu track metadata.

        # `extra` is Migu's contentId, retained from the search response.

        migu_cookie = cfg.get("migu_cookie", "")

        content_id = str(song.get("extra") or "")

        url = (

            migu_playback_url_with_cookie(song_id, cookie=migu_cookie, content_id=content_id, quality=quality)

            if migu_cookie else migu_playback_url(song_id)

        )

        return (url, "migu") if url else ("", "")

    if song_source == "qishui":

        try:

            _qishui_bridge_ready()

            payload = _qishui_bridge_request(

                "POST",

                "/playback",

                {"id": song_id, "quality": quality},

                timeout=30,

            )

            result = payload.get("result") if isinstance(payload, dict) else {}

            result = result if isinstance(result, dict) else {}

            raw_url = str(result.get("url") or "").strip()

            if result.get("playable") and raw_url:

                return _register_qishui_audio_url(raw_url), "qishui"

        except Exception as exc:

            logger.debug("player qishui native resolver failed: %s", exc)

        return "", ""

    if song_source in ("kuwo", "third"):

        return "", ""

    url = _resolve_netease_play_url(song_id, cfg, quality=quality)
    return (url, "netease") if url else ("", "")





def _player_native_resolver_sources(song_source):

    """Return every resolver name (resolved_by) one platform's native branch can report."""

    if song_source == "kugou":

        return tuple(KUGOU_NATIVE_PROVIDER_SOURCES[provider] for provider in ("concept", "standard"))

    if song_source in ("qq", "migu", "qishui"):

        return (song_source,)

    if song_source in ("kuwo", "third"):

        return ()

    return ("netease",)


def _player_platform_sources_excluded(platform, excluded):

    """True only when every resolver name of that platform is already excluded."""

    names = _player_native_resolver_sources(platform)

    if not names:

        return platform in excluded

    return all(name in excluded for name in names)


def _player_native_excluded(song_source, excluded):

    """True when the track's own platform branch must be skipped entirely."""

    return "native" in excluded or _player_platform_sources_excluded(song_source, excluded)


def _player_timeout_settings(cfg):

    """Return validated playback resolver timeouts in seconds."""

    try:

        third_timeout = int(cfg.get("player_third_source_timeout", 10))

    except (TypeError, ValueError):

        third_timeout = 10

    try:

        resolve_timeout = int(cfg.get("player_resolve_timeout", 12))

    except (TypeError, ValueError):

        resolve_timeout = 12

    try:

        cross_platform_timeout = int(cfg.get("player_cross_platform_timeout", 4))

    except (TypeError, ValueError):

        cross_platform_timeout = 4

    third_timeout = max(3, min(60, third_timeout))

    resolve_timeout = max(5, min(75, resolve_timeout))

    cross_platform_timeout = max(1, min(30, cross_platform_timeout))

    return third_timeout, max(third_timeout, resolve_timeout), cross_platform_timeout





def _player_match_text(value):

    """Build a comparison key that keeps Chinese characters but ignores formatting."""

    return re.sub(r"[\W_]+", "", str(value or "").casefold())





def _player_primary_artist(value):

    """Use the leading credited artist when deciding whether two platform records match."""

    value = str(value or "").strip()

    if not value:

        return ""

    first = re.split(r"\s*(?:[/、,，&＆;；]|feat\.?|ft\.?|featuring)\s*", value, maxsplit=1, flags=re.I)[0]

    return _player_match_text(first)





def _player_clean_track_title(title, artist=""):

    """Kugou often returns a display title in the form 'artist - title'."""

    value = str(title or "").strip()

    credited_artist = str(artist or "").strip()

    if value and credited_artist:

        prefix = re.compile(r"^\s*" + re.escape(credited_artist) + r"\s*[-—–:：]\s*", re.I)

        value = prefix.sub("", value, count=1)

    return value.strip()





def _player_variant_terms(title):

    """Flag common alternate versions so a matching search result is not silently substituted."""

    value = str(title or "").casefold()

    terms = (

        "dj", "remix", "live", "现场", "翻唱", "cover", "伴奏", "纯音乐",

        "加速", "减速", "升调", "降调", "sped up", "slowed", "reverb", "片段", "铃声",

    )

    return {term for term in terms if term in value}





def _player_cross_platform_match(original, candidate):

    """Require the same title, compatible lead artist and close duration before official fallback."""

    original_title = _player_clean_track_title(original.get("title") or original.get("name"), original.get("artist") or original.get("singer"))

    candidate_title = _player_clean_track_title(candidate.get("title") or candidate.get("name"), candidate.get("artist") or candidate.get("singer"))

    if not original_title or not candidate_title:

        return False

    if _player_match_text(original_title) != _player_match_text(candidate_title):

        return False

    if _player_variant_terms(candidate_title) - _player_variant_terms(original_title):

        return False



    original_artist = _player_primary_artist(original.get("artist") or original.get("singer"))

    candidate_artist = _player_primary_artist(candidate.get("artist") or candidate.get("singer"))

    if original_artist and candidate_artist and original_artist not in candidate_artist and candidate_artist not in original_artist:

        return False

    if original_artist and not candidate_artist:

        return False



    try:

        original_duration = int(original.get("duration") or 0)

        candidate_duration = int(candidate.get("duration") or 0)

    except (TypeError, ValueError):

        original_duration = candidate_duration = 0

    if original_duration and candidate_duration and abs(original_duration - candidate_duration) > 8000:

        return False

    return True





def _player_cross_platform_search(platform, query_text, cfg):

    """Search one official catalogue and normalize no more than eight candidates."""

    try:

        if platform == "netease":

            result = search_song(query_text, limit=8, cookie=cfg.get("cookie", ""))

            records = (result.get("result") or {}).get("songs") or []

            return [_player_netease_song(record) for record in records]

        if platform == "qq":

            result = qq_search_song(query_text, limit=8, cookie=qq_request_cookie(cfg))

            records = ((result.get("data") or {}).get("song") or {}).get("list") or []

            return [_player_qq_song(record) for record in records]

        if platform == "migu":

            records, _message = migu_search(query_text, page=1, limit=8)

            return [_player_migu_song(record) for record in records]

        if platform == "qishui":

            try:

                return _qishui_bridge_search(query_text, page=1, limit=8)

            except Exception:

                cookie = _configured_qishui_cookie(cfg)

                return qishui_search(query_text, sessionid=cookie, page=1, limit=8)

        if platform == "kugou":

            result, _provider = _player_kugou_search(query_text, page=1, limit=8, cfg=cfg)

            records = (result.get("data") or {}).get("lists") or []

            return [_player_kugou_song(record) for record in records]

    except Exception as exc:

        logger.debug("cross-platform %s search failed: %s", platform, exc)

    return []





def _player_find_cross_platform_playback(platform, song, cfg, quality=None, excluded_sources=None):

    """Find a verified same-song candidate on one other official platform."""

    title = _player_clean_track_title(song.get("title") or song.get("name"), song.get("artist") or song.get("singer"))

    artist = str(song.get("artist") or song.get("singer") or "").strip()

    query_text = " ".join(part for part in (title, artist) if part).strip()

    if not query_text:

        return "", None

    for candidate in _player_cross_platform_search(platform, query_text, cfg):

        if not _player_cross_platform_match(song, candidate):

            continue

        url, _ = _player_native_playback(platform, candidate, cfg, quality=quality, excluded_sources=excluded_sources)

        if url:

            return url, candidate

    return "", None





def _player_cross_platform_playback(song_source, song, cfg, excluded_sources, timeout, quality=None):
    """\u6309\u540e\u53f0\u914d\u7f6e\u987a\u5e8f\u4f9d\u6b21\u5c1d\u8bd5\u5176\u4ed6\u5b98\u65b9\u5e73\u53f0\uff0c\u786e\u4fdd\u4f18\u5148\u7ea7\u771f\u6b63\u751f\u6548\u3002"""
    excluded = {str(item).strip() for item in (excluded_sources or []) if str(item).strip()}
    platforms = [
        platform
        for platform in configured_cross_platform_priority(cfg)
        if platform != song_source and not _player_platform_sources_excluded(platform, excluded)
    ]

    if not platforms:
        return "", "", None

    deadline = time.monotonic() + max(0, float(timeout or 0))
    for platform in platforms:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(
            _player_find_cross_platform_playback, platform, song, cfg, quality, excluded
        )
        try:
            url, candidate = future.result(timeout=remaining)
        except Exception as exc:
            logger.debug("cross-platform %s resolver failed: %s", platform, exc)
            continue
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        if url and candidate:
            return url, platform, candidate

    return "", "", None


    executor = ThreadPoolExecutor(max_workers=len(platforms))

    futures = {

        executor.submit(_player_find_cross_platform_playback, platform, song, cfg, quality): platform

        for platform in platforms

    }

    try:

        pending = set(futures)

        deadline = time.monotonic() + timeout

        while pending:

            remaining = deadline - time.monotonic()

            if remaining <= 0:

                break

            done, pending = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)

            if not done:

                break

            for future in done:

                platform = futures[future]

                try:

                    url, candidate = future.result()

                except Exception as exc:

                    logger.debug("cross-platform %s resolver failed: %s", platform, exc)

                    continue

                if url and candidate:

                    return url, platform, candidate

    finally:

        executor.shutdown(wait=False, cancel_futures=True)

    return "", "", None





def _player_auto_playback(song_source, song, cfg, excluded_sources, quality=None):

    """Resolve in the agreed order: current official -> other official -> third-party."""

    enabled = _player_third_sources(cfg)
    excluded = {str(item).strip() for item in (excluded_sources or []) if str(item).strip()}
    third_timeout, resolve_timeout, cross_platform_timeout = _player_timeout_settings(cfg)
    quality = normalize_player_quality(quality or configured_player_quality(cfg))

    # 1) Try the track's own official platform first.  This is especially
    # important for Qishui: a valid account/session must get first chance at
    # the platform-owned stream before any fallback is considered.
    native_excluded = _player_native_excluded(song_source, excluded)
    if not native_excluded:
        try:
            url, resolver = _player_native_playback(
                song_source,
                song,
                cfg,
                quality=quality,
                excluded_sources=excluded,
            )
            if url:
                return url, resolver, "native", None
        except Exception as exc:
            logger.debug("player %s native resolver failed: %s", song_source, exc)

    deadline = time.monotonic() + resolve_timeout

    # 2) If the current platform cannot provide a stream, try the other
    # official platforms in the administrator-configured order.
    cross_timeout = min(cross_platform_timeout, max(0, deadline - time.monotonic()))
    if cross_timeout > 0:
        url, platform, candidate = _player_cross_platform_playback(
            song_source,
            song,
            cfg,
            excluded,
            cross_timeout,
            quality,
        )
        if url:
            return url, platform, "cross_platform", candidate

    # 3) Only after every eligible official platform has failed, try custom
    # third-party sources.  Keeping this as a separate stage prevents a fast
    # third-party response from winning over a configured official fallback.
    if enabled:
        third_stage_timeout = min(third_timeout, max(0, deadline - time.monotonic()))
        if third_stage_timeout > 0:
            attempted_third = set(excluded)
            while enabled:
                candidates = {sid: source for sid, source in enabled.items() if sid not in attempted_third}
                if not candidates:
                    break
                try:
                    url, resolver = try_third_sources_parallel(
                        song_source,
                        song,
                        candidates,
                        quality,
                        attempted_third,
                        third_stage_timeout,
                    )
                except Exception as exc:
                    logger.debug("player %s third-party resolver failed: %s", song_source, exc)
                    url, resolver = "", ""
                if not url or not resolver:
                    break
                if _consume_third_source_quota(resolver):
                    return url, resolver, "third", None
                attempted_third.add(resolver)

    return "", "", "", None



@app.get("/api/player/lyric")

async def api_player_lyric(

    song_id: str = Query(..., min_length=1),

    source: str = Query(default="netease"),

    title: str = Query(default="", max_length=240),

    artist: str = Query(default="", max_length=240),

    duration: int = Query(default=0, ge=0),

    extra: str = Query(default="", max_length=120),

    key: str = Query(default=""),

):

    if not check_auth(key):

        raise HTTPException(status_code=403, detail="Invalid API Key")

    song = {

        "id": song_id,

        "title": title,

        "artist": artist,

        "duration": duration,

        "extra": extra,

    }

    lyric = _player_native_lyric(source, song, load_config())

    return JSONResponse({"code": 0, "lyric": lyric})





@app.get("/api/player/sources")

async def api_player_sources(key: str = Query(default="")):

    if not check_auth(key):

        raise HTTPException(status_code=403, detail="Invalid API Key")

    cfg = load_config()

    enabled = _player_third_sources(cfg)

    return JSONResponse({

        "code": 0,
        "quality": configured_player_quality(cfg),
        "qualities": [
            {"id": "128k", "name": "128k 标准"},
            {"id": "320k", "name": "320k 高品质"},
            {"id": "flac", "name": "FLAC 无损"},
            {"id": "hires", "name": "Hi-Res（可用时）"},
        ],

        "sources": {

            "netease": {"name": "网易云音乐", "available": True},

            "qq": {"name": "QQ音乐", "available": True},

            "kugou": {"name": "\u9177\u72d7\u97f3\u4e50", "available": True},

            "migu": {"name": "咪咕音乐", "available": True},

            "qishui": {"name": "汽水音乐", "available": True},

            "third": {

                "name": "自定义回源",

                "available": bool(enabled),

                "description": "用于自动或指定播放回源",

            },

            "aggregate": {"name": "聚合搜索", "available": True},

            "playback": [

                {"id": "native", "name": "官方优先"},

                {"id": "auto", "name": "自动回源"},

                *[{"id": sid, "name": source.get("name", sid)} for sid, source in enabled.items()],

            ],

        },

    })





@app.get("/api/player/search")

async def api_player_search(

    request: Request,

    keyword: str = Query(..., min_length=1, max_length=120),

    source: str = Query(default="netease"),

    page: int = Query(default=1, ge=1, le=20),

    limit: int = Query(default=20, ge=1, le=30),

    key: str = Query(default=""),

):

    if not check_auth(key):

        raise HTTPException(status_code=403, detail="Invalid API Key")



    cfg = load_config()

    if source == "qishui":

        try:

            user = _session_user(request)

            cookie = _user_qishui_cookie(user["id"]) if user else ""

            cookie = cookie or _configured_qishui_cookie(cfg)

            try:

                songs = _qishui_bridge_search(keyword, page=page, limit=limit)

            except Exception:

                songs = qishui_search(keyword, sessionid=cookie, page=page, limit=limit)

            if not songs:

                return JSONResponse({"code": -1, "msg": "汽水音乐官方搜索未返回结果，公开目录搜索也未找到匹配歌曲。", "data": []})

            return JSONResponse({"code": 0, "data": songs, "keyword": keyword})

        except (QishuiError, requests.RequestException, ValueError) as exc:

            return JSONResponse({"code": -1, "msg": str(exc), "data": []})

    if source == "qq":

        result = qq_search_song(keyword, page=page, limit=limit, cookie=qq_request_cookie(cfg))

        songs = result.get("data", {}).get("song", {}).get("list", []) if result.get("code") == 0 else []

        if result.get("code") != 0:

            return JSONResponse({"code": -1, "msg": result.get("msg", "QQ 音乐搜索失败"), "data": []})

        return JSONResponse({"code": 0, "data": [_player_qq_song(song) for song in songs], "keyword": keyword})



    if source == "kugou":

        result, provider = _player_kugou_search(keyword, page=page, limit=limit, cfg=cfg)

        songs = result.get("data", {}).get("lists", []) if result.get("status") == 1 else []

        if result.get("status") != 1:

            return JSONResponse({"code": -1, "msg": result.get("msg", "Kugou search failed"), "data": []})

        return JSONResponse({"code": 0, "data": [_player_kugou_song(song) for song in songs], "keyword": keyword, "provider": provider or "kugou"})



    if source == "migu":

        songs, message = migu_search(keyword, page=page, limit=limit)

        if not songs:

            return JSONResponse({"code": -1, "msg": message or "咪咕音乐未返回搜索结果", "data": []})

        return JSONResponse({"code": 0, "data": [_player_migu_song(song) for song in songs], "keyword": keyword})



    if source == "aggregate":

        netease_result = search_song(keyword, page=page, limit=limit, cookie=cfg.get("cookie", ""))

        qq_result = qq_search_song(keyword, page=page, limit=limit, cookie=qq_request_cookie(cfg))

        netease_songs = netease_result.get("result", {}).get("songs", []) if netease_result.get("code") == 200 else []

        qq_songs = qq_result.get("data", {}).get("song", {}).get("list", []) if qq_result.get("code") == 0 else []

        songs = _player_aggregate_results(netease_songs, qq_songs, limit)

        if not songs and netease_result.get("code") != 200 and qq_result.get("code") != 0:

            return JSONResponse({"code": -1, "msg": "网易云和 QQ 音乐搜索均失败", "data": []})

        return JSONResponse({"code": 0, "data": songs, "keyword": keyword, "aggregated": True})



    if source == "third":

        return JSONResponse({

            "code": -1,

            "msg": "第三方音源仅支持回源播放，请先使用网易云、QQ、酷狗、咪咕或汽水音乐搜索。",

            "data": [],

        })



    result = search_song(keyword, page=page, limit=limit, cookie=cfg.get("cookie", ""))

    if result.get("code") != 200:

        return JSONResponse({"code": -1, "msg": result.get("msg", "网易云音乐搜索失败"), "data": []})

    songs = result.get("result", {}).get("songs", [])

    return JSONResponse({"code": 0, "data": [_player_netease_song(song) for song in songs], "keyword": keyword})





DAILY_RECOMMENDATION_SEEDS = (

    "华语热歌", "流行音乐", "热门新歌", "经典金曲", "抖音热歌", "治愈歌曲",

    "周杰伦", "陈奕迅", "邓紫棋", "林俊杰", "薛之谦", "五月天",

    "告五人", "毛不易", "王菲", "孙燕姿", "梁静茹", "Taylor Swift",

)





def _daily_recommendation_pool(day_key):

    cached = DAILY_RECOMMENDATION_CACHE.get(day_key)

    if cached:

        return cached



    rng = random.Random(f"sound-island:{day_key}")

    seeds = list(DAILY_RECOMMENDATION_SEEDS)

    rng.shuffle(seeds)

    songs, seen = [], set()

    cfg = load_config()

    for keyword in seeds[:7]:

        try:

            netease_result = search_song(keyword, page=1, limit=8, cookie=cfg.get("cookie", ""))

            qq_result = qq_search_song(keyword, page=1, limit=8, cookie=qq_request_cookie(cfg))

            netease_songs = netease_result.get("result", {}).get("songs", []) if netease_result.get("code") == 200 else []

            qq_songs = qq_result.get("data", {}).get("song", {}).get("list", []) if qq_result.get("code") == 0 else []

            candidates = _player_aggregate_results(netease_songs, qq_songs, 8)

        except (requests.RequestException, ValueError, TypeError):

            continue

        rng.shuffle(candidates)

        for song in candidates:

            fingerprint = (str(song.get("title") or "").strip().lower(), str(song.get("artist") or "").strip().lower())

            if not fingerprint[0] or fingerprint in seen:

                continue

            seen.add(fingerprint)

            songs.append(song)

            if len(songs) >= 36:

                DAILY_RECOMMENDATION_CACHE[day_key] = songs

                return songs

    DAILY_RECOMMENDATION_CACHE[day_key] = songs

    return songs





@app.get("/api/player/daily-recommendations")

async def api_daily_recommendations(request: Request, batch: int = Query(default=0, ge=0, le=20)):

    _require_user(request)

    day_key = datetime.now().strftime("%Y-%m-%d")

    pool = _daily_recommendation_pool(day_key)

    if not pool:

        return JSONResponse({"code": -1, "msg": "今日推荐暂时无法生成，请稍后重试", "data": []}, status_code=502)

    page_size = 12

    start = (batch * page_size) % len(pool)

    data = (pool + pool)[start:start + min(page_size, len(pool))]

    return JSONResponse({"code": 0, "date": day_key, "batch": batch, "total": len(pool), "data": data})





DAILY_PLAYLIST_CATEGORIES = (

    "全部", "华语", "流行", "欧美", "民谣", "电子",

    "摇滚", "古风", "说唱", "治愈", "粤语", "ACG",

    "夜晚", "运动", "轻音乐", "怀旧", "浪漫", "清晨",

)





DAILY_PLAYLIST_TARGET = 36





def _daily_playlist_entry(source, playlist_id, name, cover, track_count=0, play_count=0, creator="", description=""):

    """统一每日歌单推荐条目结构，字段与前端渲染保持一致。"""

    playlist_id = str(playlist_id or "").strip()

    name = str(name or "").strip()

    if not playlist_id or not name:

        return None

    cover = str(cover or "").strip().replace("http://", "https://")

    return {

        "id": playlist_id,

        "source": source,

        "name": name,

        "cover": cover,

        "track_count": int(track_count or 0),

        "play_count": int(play_count or 0),

        "creator": str(creator or "").strip(),

        "description": str(description or "").strip()[:120],

    }





def _daily_netease_playlists(rng, cookie):

    """网易云精品歌单 + 个性化推荐歌单。"""

    entries = []

    categories = list(DAILY_PLAYLIST_CATEGORIES)

    rng.shuffle(categories)

    for category in categories[:3]:

        result = ncm_request(

            "/api/playlist/highquality/list",

            params={"cat": category, "limit": 12, "lasttime": 0, "total": "true"},

            cookie=cookie,

        )

        if not isinstance(result, dict) or result.get("code") != 200:

            continue

        for item in (result.get("playlists") or []):

            if not isinstance(item, dict):

                continue

            creator = item.get("creator") if isinstance(item.get("creator"), dict) else {}

            entries.append(_daily_playlist_entry(

                "netease",

                item.get("id"),

                item.get("name"),

                item.get("coverImgUrl") or item.get("picUrl"),

                item.get("trackCount"),

                item.get("playCount"),

                creator.get("nickname"),

                item.get("description") or "",

            ))

    personalized = ncm_request("/api/personalized/playlist", params={"limit": 12}, cookie=cookie)

    if isinstance(personalized, dict) and personalized.get("code") == 200:

        for item in (personalized.get("result") or []):

            if not isinstance(item, dict):

                continue

            entries.append(_daily_playlist_entry(

                "netease",

                item.get("id"),

                item.get("name"),

                item.get("picUrl") or item.get("coverImgUrl"),

                item.get("trackCount"),

                item.get("playCount"),

                "",

                item.get("copywriter") or "",

            ))

    return [entry for entry in entries if entry]





def _daily_qq_request(module, method, param, cookie=""):

    payload = {

        "req_0": {"module": module, "method": method, "param": param},

        "comm": {"ct": 24, "cv": 0, "uin": 0, "format": "json"},

    }

    headers = {"Referer": "https://y.qq.com/"}

    if cookie:

        headers["Cookie"] = cookie

    response = QQ_SESSION.get(

        "https://u.y.qq.com/cgi-bin/musicu.fcg",

        params={"format": "json", "data": json.dumps(payload, separators=(",", ":"))},

        headers=headers,

        timeout=20,

    )

    if response.status_code != 200:

        return {}

    node = response.json().get("req_0") if isinstance(response.json(), dict) else None

    if not isinstance(node, dict) or node.get("code") != 0:

        return {}

    return node.get("data") if isinstance(node.get("data"), dict) else {}





def _daily_qq_playlists(cookie):

    """QQ 音乐编辑推荐歌单 + 推荐流歌单。"""

    entries = []

    try:

        hot = _daily_qq_request("playlist.HotRecommendServer", "get_hot_recommend", {"async": 1, "cmd": 2}, cookie)

    except (requests.RequestException, ValueError, TypeError):

        hot = {}

    for item in (hot.get("v_hot") or []):

        if not isinstance(item, dict):

            continue

        entries.append(_daily_playlist_entry(

            "qq",

            item.get("content_id"),

            item.get("title"),

            item.get("cover"),

            0,

            item.get("listen_num"),

            item.get("username"),

            item.get("rcmdtemplate") or "",

        ))

    try:

        feed = _daily_qq_request("music.playlist.PlaylistSquare", "GetRecommendFeed", {"From": 0, "Size": 12}, cookie)

    except (requests.RequestException, ValueError, TypeError):

        feed = {}

    for node in (feed.get("List") or []):

        playlist = node.get("Playlist") if isinstance(node, dict) else None

        basic = playlist.get("basic") if isinstance(playlist, dict) else None

        if not isinstance(basic, dict):

            continue

        cover = basic.get("cover") if isinstance(basic.get("cover"), dict) else {}

        creator = basic.get("creator") if isinstance(basic.get("creator"), dict) else {}

        entries.append(_daily_playlist_entry(

            "qq",

            basic.get("tid"),

            basic.get("title"),

            cover.get("medium_url") or cover.get("default_url") or cover.get("small_url"),

            basic.get("song_cnt"),

            basic.get("play_cnt"),

            creator.get("nick"),

            basic.get("desc") or "",

        ))

    return [entry for entry in entries if entry]





DAILY_KUGOU_CATEGORIES = (

    (0, "推荐"), (11292, "Hi-Res"), (33, "电子"), (20, "英语"),

    (74, "游戏"), (81, "励志"), (10, "DJ"), (11, "古风"), (12, "轻音乐"),

)





def _daily_kugou_playlists(rng, cookie=""):

    """酷狗音乐歌单广场推荐（specialrec 接口），免登录可用。"""

    entries = []

    categories = list(DAILY_KUGOU_CATEGORIES)

    rng.shuffle(categories)

    fields = _kugou_cookie_fields(cookie or "")

    for category_id, _label in categories[:4]:

        mid = fields.get("mid") or hashlib.md5(uuid.uuid4().hex.upper().encode()).hexdigest().upper()

        now = str(int(time.time()))

        payload = {

            "appid": int(KUGOU_APP_APPID),

            "mid": mid,

            "clientver": int(KUGOU_APP_CLIENTVER),

            "platform": "android",

            "clienttime": now,

            "userid": str(fields.get("userid") or "0"),

            "module_id": 1,

            "page": 1,

            "pagesize": 12,

            "key": _kugou_app_sign_key(now),

            "special_recommend": {"withtag": 1, "withsong": 1, "sort": 1, "ugc": 1, "is_selected": 0, "withrecommend": 1, "area_code": 1, "categoryid": category_id},

            "req_multi": 1,

            "retrun_min": 5,

            "return_special_falg": 1,

        }

        raw_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

        params = {"dfid": fields.get("dfid") or "-", "mid": mid, "uuid": "-", "appid": KUGOU_APP_APPID, "clientver": KUGOU_APP_CLIENTVER, "clienttime": now}

        if fields.get("token"):

            params["token"] = fields["token"]

        if fields.get("userid"):

            params["userid"] = fields["userid"]

        params["signature"] = _kugou_app_signature(params, raw_body)

        headers = {

            "User-Agent": "Android15-1070-11083-46-0-DiscoveryDRADProtocol-wifi",

            "Content-Type": "application/json",

            "x-router": "specialrec.service.kugou.com",

            "dfid": params["dfid"],

            "clienttime": now,

            "mid": mid,

            "kg-rc": "1",

        }

        if cookie:

            headers["Cookie"] = cookie

        try:

            response = requests.post("https://gateway.kugou.com/v2/special_recommend", params=params, data=raw_body.encode("utf-8"), headers=headers, timeout=20)

            result = response.json() if response.ok else {}

        except (requests.RequestException, ValueError):

            continue

        items = ((result or {}).get("data") or {}).get("special_list") or []

        for item in items:

            if not isinstance(item, dict):

                continue

            collection_id = str(item.get("global_collection_id") or "").strip()

            if not re.fullmatch(r"collection_\d+_\d+_\d+_\d+", collection_id):

                continue

            entries.append(_daily_playlist_entry(

                "kugou",

                collection_id,

                item.get("specialname"),

                (item.get("imgurl") or item.get("flexible_cover") or "").replace("{size}", "400"),

                item.get("percount"),

                item.get("play_count"),

                item.get("nickname"),

                item.get("intro") or "",

            ))

    return [entry for entry in entries if entry]



def _daily_playlist_pool(day_key):

    """当天固定的歌单推荐池：日期作为随机种子，保证同一天结果稳定。"""

    cached = DAILY_PLAYLIST_CACHE.get(day_key)

    if cached:

        return cached

    rng = random.Random(f"sound-island-playlists:{day_key}")

    cfg = load_config()

    entries = []

    try:

        entries.extend(_daily_netease_playlists(rng, cfg.get("cookie", "")))

    except (requests.RequestException, ValueError, TypeError):

        pass

    try:

        entries.extend(_daily_qq_playlists(qq_request_cookie(cfg)))

    except (requests.RequestException, ValueError, TypeError):

        pass

    try:

        entries.extend(_daily_kugou_playlists(rng, cfg.get("kugou_cookie", "")))

    except (requests.RequestException, ValueError, TypeError):

        pass

    rng.shuffle(entries)

    playlists, seen_ids, seen_names = [], set(), set()

    for entry in entries:

        key = (entry["source"], entry["id"])

        name_key = "".join(entry["name"].lower().split())

        if key in seen_ids or name_key in seen_names:

            continue

        seen_ids.add(key)

        seen_names.add(name_key)

        playlists.append(entry)

        if len(playlists) >= DAILY_PLAYLIST_TARGET:

            break

    if playlists:

        DAILY_PLAYLIST_CACHE[day_key] = playlists

    return playlists





@app.get("/api/player/daily-playlists")

async def api_daily_playlists(request: Request, batch: int = Query(default=0, ge=0, le=20)):

    _require_user(request)

    day_key = datetime.now().strftime("%Y-%m-%d")

    pool = _daily_playlist_pool(day_key)

    if not pool:

        return JSONResponse({"code": -1, "msg": "今日歌单推荐暂时无法生成，请稍后重试", "data": []}, status_code=502)

    page_size = 12

    start = (batch * page_size) % len(pool)

    data = (pool + pool)[start:start + min(page_size, len(pool))]

    return JSONResponse({"code": 0, "date": day_key, "batch": batch, "total": len(pool), "data": data})





@app.get("/api/player/playlist-tracks")

async def api_player_playlist_tracks(

    request: Request,

    source: str = Query(...),

    playlist_id: str = Query(..., alias="id"),

):

    """读取推荐歌单的曲目，复用歌单导入所用的平台解析逻辑。"""

    _require_user(request)

    platform = (source or "").strip().lower()

    remote_id = str(playlist_id or "").strip()

    if platform not in ("netease", "qq", "kugou"):

        return JSONResponse({"code": -1, "msg": "暂不支持该歌单来源", "data": []})

    if platform == "kugou":

        if not re.fullmatch(r"collection_\d+_\d+_\d+_\d+", remote_id):

            return JSONResponse({"code": -1, "msg": "酷狗音乐歌单 ID 无效", "data": []})

    elif not remote_id.isdigit():

        return JSONResponse({"code": -1, "msg": "暂不支持该歌单来源", "data": []})

    cfg = load_config()

    try:

        if platform == "netease":

            name, songs = _get_netease_playlist(remote_id, cfg.get("cookie", ""))

        elif platform == "kugou":

            name, songs = _get_kugou_collection_playlist(remote_id)

        else:

            name, songs = _get_qq_playlist(remote_id, qq_request_cookie(cfg))

    except (ValueError, TypeError, KeyError, requests.RequestException) as exc:

        logger.info("Daily playlist tracks failed: %s", exc)

        return JSONResponse({"code": -1, "msg": str(exc), "data": []})

    return JSONResponse({"code": 0, "name": name, "source": platform, "id": remote_id, "data": songs})





@app.get("/api/player/qishui-audio")

async def api_player_qishui_audio(request: Request, token: str = Query(..., min_length=20, max_length=160)):

    raw_url = _qishui_audio_url(token)

    if not raw_url:

        raise HTTPException(status_code=404, detail="汽水音频地址已失效，请重新解析")

    headers = {}

    if request.headers.get("range"):

        headers["Range"] = request.headers["range"]

    try:

        upstream = requests.get(

            f"{QISHUI_BRIDGE_URL}/audio",

            params={"url": raw_url},

            headers={**_qishui_bridge_headers(), **headers},

            stream=True,

            timeout=(5, 60),

        )

    except requests.RequestException as exc:

        raise HTTPException(status_code=502, detail="汽水音频代理连接失败") from exc

    if upstream.status_code >= 400:

        message = "汽水音频代理返回失败"

        try:

            payload = upstream.json()

            message = str(payload.get("error") or message)

        except ValueError:

            pass

        upstream.close()

        raise HTTPException(status_code=502, detail=message)

    response_headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}

    for name in ("content-type", "content-length", "content-range"):

        value = upstream.headers.get(name)

        if value:

            response_headers[name.title()] = value

    def body():

        try:

            for chunk in upstream.iter_content(chunk_size=64 * 1024):

                if chunk:

                    yield chunk

        finally:

            upstream.close()

    return StreamingResponse(body(), status_code=upstream.status_code, headers=response_headers)



@app.get("/api/player/resolve")

async def api_player_resolve(

    song_id: str = Query(..., min_length=1),

    source: str = Query(default="netease"),

    strategy: str = Query(default="auto"),

    title: str = Query(default=""),

    artist: str = Query(default=""),

    album: str = Query(default=""),

    duration: int = Query(default=0, ge=0),

    extra: str = Query(default="", max_length=120),

    exclude: str = Query(default="", max_length=240),

    quality: str = Query(default="", max_length=16),

    key: str = Query(default=""),

):

    if not check_auth(key):

        raise HTTPException(status_code=403, detail="Invalid API Key")



    cfg = load_config()
    quality = normalize_player_quality(quality or configured_player_quality(cfg))

    if source == "third":

        return JSONResponse({

            "code": -1,

            "url": "",

            "lyric": "",

            "resolved_by": "",

            "resolved_kind": "",

            "msg": "第三方音源没有独立搜索结果，不能直接按第三方音源播放；请从官方平台歌曲发起回源。",

        })



    song = {

        "id": song_id,

        "songmid": song_id,

        "title": title,

        "name": title,

        "artist": artist,

        "singer": artist,

        "album": album,

        "duration": duration,

        "extra": extra,

    }



    excluded_sources = [item.strip() for item in exclude.split(",") if item.strip()]



    if strategy == "auto":

        music_url, resolved_by, resolved_kind, resolved_song = _player_auto_playback(source, song, cfg, excluded_sources, quality=quality)

        return JSONResponse({

            "code": 0 if music_url else -1,

            "url": music_url,

            "lyric": "",

            "resolved_by": resolved_by,

            "resolved_kind": resolved_kind,

            "resolved_song": resolved_song,

            "msg": "" if music_url else "未能获取播放地址",

        })



    if strategy != "native":

        third_url, third_source = _resolve_player_third_source(

            source, song, cfg, strategy, excluded_sources, quality=quality,

        )

        if third_url:

            lyric = _player_native_lyric(source, song, cfg)

            return JSONResponse({

                "code": 0,

                "url": third_url,

                "lyric": lyric,

                "resolved_by": third_source,

                "resolved_kind": "third",

            })



    if source == "qq":

        cookie = qq_request_cookie(cfg)
        music_url, resolved_by = _player_native_playback("qq", song, cfg, quality=quality)
        lyric_result = qq_get_lyric(song_id, cookie=cookie)
        lyric = lyric_result.get("lyric", "") if lyric_result.get("code") == 0 else ""

        return JSONResponse({"code": 0 if music_url else -1, "url": music_url, "lyric": lyric,

                             "resolved_by": resolved_by if music_url else "", "resolved_kind": "native" if music_url else "",

                             "msg": "" if music_url else "未能获取播放地址"})



    if source == "kugou":

        music_url, resolved_by = _player_native_playback("kugou", song, cfg, quality=quality)

        lyric = _player_native_lyric("kugou", song, cfg)

        return JSONResponse({"code": 0 if music_url else -1, "url": music_url, "lyric": lyric,

                             "resolved_by": resolved_by, "resolved_kind": "native" if music_url else "",

                             "msg": "" if music_url else "酷狗概念版和普通酷狗均未返回可播放地址，请检查授权会话或启用自定义回源"})



    if source == "qishui":

        music_url, resolved_by = _player_native_playback("qishui", song, cfg, quality=quality)

        lyric = _player_native_lyric("qishui", song, cfg)

        return JSONResponse({

            "code": 0 if music_url else -1,

            "url": music_url,

            "lyric": lyric,

            "resolved_by": resolved_by,

            "resolved_kind": "native" if music_url else "",

            "msg": "" if music_url else "汽水音乐未返回可播放地址；请检查 Electron 登录桥、会员权益或歌曲版权限制",

        })



    if source == "migu":

        # “官方优先” keeps the selected Migu song on the configured Migu

        # account.  The `auto` branch above first makes this same call, then

        # falls back only when Migu does not return a usable URL.

        music_url, resolved_by = _player_native_playback("migu", song, cfg, quality=quality)

        lyric = _player_native_lyric("migu", song, cfg)

        return JSONResponse({

            "code": 0 if music_url else -1,

            "url": music_url,

            "lyric": lyric,

            "resolved_by": resolved_by,

            "resolved_kind": "native" if music_url else "",

            "msg": "" if music_url else "咪咕音乐未返回可播放地址；请检查咪咕 Cookie、会员权益或该歌曲的版权/地区限制",

        })



    if source == "kuwo":

        return JSONResponse({

            "code": -1,

            "url": "",

            "lyric": "",

            "msg": "This track needs an enabled fallback source",

        })



    music_url, resolved_by = _player_native_playback("netease", song, cfg, quality=quality)

    lyric_result = get_song_lyric(song_id, cookie=cfg.get("cookie", ""))

    lyric = (lyric_result.get("lrc", {}) or {}).get("lyric", "") if lyric_result.get("code") == 200 else ""

    return JSONResponse({"code": 0 if music_url else -1, "url": music_url, "lyric": lyric,

                         "resolved_by": resolved_by if music_url else "", "resolved_kind": "native" if music_url else "",

                         "msg": "" if music_url else "未能获取播放地址"})



@app.get("/api/search")

async def api_search(

    keyword: str = Query(..., description="搜索关键词"),

    key: str = Query(default="", description="API Key"),

    page: int = Query(default=1, ge=1, le=10),

    limit: int = Query(default=5, ge=1, le=20),

    source: str = Query(default="netease"),

    format: str = Query(default="xml", description="返回格式: xml / json"),

):

    """搜索歌曲并返回音乐卡片 XML"""

    # 鉴权

    if not check_auth(key):

        raise HTTPException(status_code=403, detail="Invalid API Key")

    

    # 日志

    start_time = time.time()

    

    # 搜索

    cfg = load_config()

    cookie = cfg.get("cookie", "")

    

    search_result = search_song(keyword, page=page, limit=limit, cookie=cookie)

    

    elapsed = round((time.time() - start_time) * 1000)

    

    if search_result.get("code") != 200:

        save_log({

            "time": datetime.now().isoformat(),

            "keyword": keyword,

            "page": page,

            "result": "error",

            "error": search_result.get("msg", "搜索失败"),

            "elapsed_ms": elapsed,

            "ip": "",

        })

        if format == "json":

            return JSONResponse({"code": -1, "msg": search_result.get("msg", "搜索失败")})

        return PlainTextResponse(

            f"<!-- 搜索失败: {search_result.get('msg', '未知错误')} -->",

            media_type="application/xml"

        )

    

    # 解析结果

    songs_data = search_result.get("result", {}).get("songs", [])

    if not songs_data:

        save_log({

            "time": datetime.now().isoformat(),

            "keyword": keyword,

            "page": page,

            "result": "empty",

            "error": None,

            "elapsed_ms": elapsed,

            "ip": "",

        })

        if format == "json":

            return JSONResponse({"code": 0, "data": [], "msg": "无结果"})

        return PlainTextResponse(

            f"<!-- 未找到歌曲: {escape_xml(keyword)} -->",

            media_type="application/xml"

        )

    

    # 取第一首歌，获取播放链接和歌词

    first_song = songs_data[0]

    song_id = first_song.get("id")

    

    # 获取播放链接

    play_url = ""

    play_url = _resolve_netease_play_url(song_id, cfg, cookie=cookie)

    if play_url:
        first_song["url"] = play_url

    

    # 获取歌词

    lyric_text = ""

    lyric_result = get_song_lyric(song_id, cookie=cookie)

    if lyric_result.get("code") == 200:

        lrc = lyric_result.get("lrc", {}) or {}

        lyric_text = lrc.get("lyric", "")

    elif lyric_result.get("nolyric"):

        lyric_text = lyric_result.get("tlyric", {}).get("lyric", "") if lyric_result.get("tlyric") else ""

    

    # 组装所有歌曲信息

    all_songs = []

    for s in songs_data:

        sid = s.get("id")

        # 从 search 结果中提取信息

        song_info = format_song_result(s)

        if sid == song_id:

            song_info["url"] = play_url

            song_info["lyric"] = lyric_text

        all_songs.append(song_info)

    

    save_log({

        "time": datetime.now().isoformat(),

        "keyword": keyword,

        "page": page,

        "result": "ok",

        "song": all_songs[0]["name"] if all_songs else "未知",

        "artist": all_songs[0]["artists"] if all_songs else "未知",

        "has_url": bool(play_url),

        "elapsed_ms": elapsed,

        "ip": "",

    })

    

    # 返回

    if format == "json":

        return JSONResponse({

            "code": 0,

            "data": all_songs,

            "total": search_result.get("result", {}).get("songCount", 0),

            "keyword": keyword,

            "page": page,

        })

    

    # 默认返回 XML

    best_song = format_song_result(first_song)

    best_song["url"] = play_url

    best_song["lyric"] = lyric_text

    xml = build_music_xml(best_song, play_url)

    return PlainTextResponse(xml, media_type="application/xml")







def _migu_plugin_result(keyword: str, cfg: dict):

    """Return the stable point-song payload for Migu's standalone plugin API."""

    songs, error = migu_search(keyword, page=1, limit=1)

    if not songs:

        return None, error or "咪咕音乐未找到歌曲"

    song = songs[0]

    song_id = str(song.get("id") or "")

    # Keep the standalone plugin consistent with the web player: use the

    # server-side Migu Cookie when one is configured, never another platform.

    content_id = str(song.get("extra") or "")

    migu_cookie = str(cfg.get("migu_cookie") or "")

    music_url = (

        migu_playback_url_with_cookie(song_id, cookie=migu_cookie, content_id=content_id, quality=cfg.get("player_quality", "320k"))

        if migu_cookie else migu_playback_url(song_id)

    )

    lyric_text = migu_lyric(song_id)

    title = song.get("title") or ""

    artist = song.get("artist") or ""

    cover = song.get("cover") or ""

    # This is a catalogue link only; playback is returned only when Migu's

    # official response supplied an HTTPS URL for this track.

    link = f"https://music.migu.cn/v3/music/song/{song_id}" if song_id else "https://music.migu.cn/"

    return {

        "code": 200,

        "title": title,

        "singer": artist,

        "cover": cover,

        "link": link,

        "music_url": music_url,

        "lyric": lyric_text,

        "provider": "migu",

    }, ""





@app.get("/api/migumusic")

async def api_migumusic(

    keyword: str = Query(default="", description="搜索关键词"),

    key: str = Query(default="", description="API Key"),

):

    """咪咕音乐独立点歌接口，返回与 /api/wxmusic 兼容的 JSON。"""

    if not check_auth(key):

        raise HTTPException(status_code=403, detail="Invalid API Key")

    if not keyword:

        return JSONResponse({"code": -1, "msg": "请输入歌名"})



    started = time.time()

    result, error = _migu_plugin_result(keyword, load_config())

    elapsed = round((time.time() - started) * 1000)

    if not result:

        save_log({

            "time": datetime.now().isoformat(), "keyword": keyword, "source": "migu",

            "result": "empty", "error": error, "elapsed_ms": elapsed, "ip": "",

        })

        return JSONResponse({"code": -1, "msg": error})



    save_log({

        "time": datetime.now().isoformat(), "keyword": keyword, "source": "migu",

        "result": "ok" if result.get("music_url") else "no_url",

        "song": result.get("title", ""), "artist": result.get("singer", ""),

        "has_url": bool(result.get("music_url")), "elapsed_ms": elapsed, "ip": "",

    })

    return JSONResponse(result)





@app.get("/api/wxmusic")

async def api_wxmusic(

    keyword: str = Query(default="", description="搜索关键词"),

    key: str = Query(default="", description="API Key"),

    source: str = Query(default="netease", description="音乐源: netease / qq / kugou / migu / third"),

):

    """微信插件专用点歌接口 - 返回插件要求的 JSON 格式"""

    if not check_auth(key):

        raise HTTPException(status_code=403, detail="Invalid API Key")

    

    if not keyword:

        return JSONResponse({"code": -1, "msg": "请输入歌名"})

    

    start_time = time.time()

    cfg = load_config()



    # ==================== 咪咕音乐 ====================

    if source == "migu":

        result, error = _migu_plugin_result(keyword, cfg)

        elapsed = round((time.time() - start_time) * 1000)

        if not result:

            save_log({"time": datetime.now().isoformat(), "keyword": keyword, "source": "migu", "result": "empty", "error": error, "elapsed_ms": elapsed, "ip": ""})

            return JSONResponse({"code": -1, "msg": error})

        save_log({"time": datetime.now().isoformat(), "keyword": keyword, "source": "migu", "result": "ok" if result.get("music_url") else "no_url", "song": result.get("title", ""), "artist": result.get("singer", ""), "has_url": bool(result.get("music_url")), "elapsed_ms": elapsed, "ip": ""})

        return JSONResponse(result)



    # ==================== 酷狗音乐 ====================

    if source == "kugou":

        # Reuse the same provider order as the music platform: the admin

        # selection decides whether Concept Edition or standard Kugou comes first.

        search_result, search_provider = _player_kugou_search(keyword, page=1, limit=1, cfg=cfg)

        elapsed = round((time.time() - start_time) * 1000)

        songs = search_result.get("data", {}).get("lists", []) if search_result.get("status") == 1 else []

        if not songs:

            save_log({"time": datetime.now().isoformat(), "keyword": keyword, "source": "kugou", "result": "empty", "error": search_result.get("msg"), "provider": search_provider, "elapsed_ms": elapsed, "ip": ""})

            return JSONResponse({"code": -1, "msg": search_result.get("msg", "Kugou search returned no tracks")})

        song = _player_kugou_song(songs[0])

        music_url, resolved_by = _player_kugou_native_playback(song, cfg)

        lyric_text = _player_native_lyric("kugou", song, cfg)

        save_log({"time": datetime.now().isoformat(), "keyword": keyword, "source": "kugou", "result": "ok" if music_url else "no_url", "song": song["title"], "artist": song["artist"], "provider": resolved_by or search_provider, "has_url": bool(music_url), "elapsed_ms": elapsed, "ip": ""})

        return JSONResponse({"code": 200, "title": song["title"], "singer": song["artist"], "cover": song["cover"], "link": f"https://www.kugou.com/song/#hash={song['id']}", "music_url": music_url, "lyric": lyric_text, "provider": resolved_by or search_provider})



    # ==================== 第三方音源 ====================



    if source == "third":

        elapsed = round((time.time() - start_time) * 1000)

        save_log({"time": datetime.now().isoformat(), "keyword": keyword, "source": "third",

                  "result": "error", "error": "第三方音源仅支持回源播放", "elapsed_ms": elapsed, "ip": ""})

        return JSONResponse({

            "code": -1,

            "msg": "第三方音源仅支持回源播放，请先使用官方平台搜索。",

        })



    # ==================== QQ音乐 ====================

    if source == "qq":

        cookie = qq_request_cookie(cfg)

        search_result = qq_search_song(keyword, page=1, limit=1, cookie=cookie)

        elapsed = round((time.time() - start_time) * 1000)



        if search_result.get("code") != 0:

            save_log({"time": datetime.now().isoformat(), "keyword": keyword, "source": "qq", "result": "error", "error": search_result.get("msg", "搜索失败"), "elapsed_ms": elapsed, "ip": ""})

            return JSONResponse({"code": -1, "msg": search_result.get("msg", "搜索失败")})



        songs_data = search_result.get("data", {}).get("song", {}).get("list", [])

        if not songs_data:

            save_log({"time": datetime.now().isoformat(), "keyword": keyword, "source": "qq", "result": "empty", "error": None, "elapsed_ms": elapsed, "ip": ""})

            return JSONResponse({"code": -1, "msg": "未找到歌曲"})



        song = songs_data[0]

        song_mid = song.get("songmid", "")

        song_name = song.get("songname", "")

        singers = "/".join(s.get("name", "") for s in (song.get("singer", []) or []))



        # 封面: 用 albummid 拼接

        album_pic = ""

        album_mid = song.get("albummid", "")

        if album_mid:

            album_pic = f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{album_mid}.jpg"



        # 播放链接

        music_url = ""

        music_url = _resolve_qq_play_url(song_mid, cfg)

        

        # 如果主源没拿到链接，尝试第三方音源 fallback

        if not music_url:

            all_sources = _get_all_source_configs(cfg)

            enabled_dict = {k: v for k, v in all_sources.items() if v.get("enabled")}

            if enabled_dict:

                quality = configured_player_quality(cfg)

                qq_song_info = {"songmid": song_mid, "songname": song_name}

                f_url, f_source = _resolve_player_third_source("qq", qq_song_info, cfg, quality=quality)

                if f_url:

                    music_url = f_url

                    logger.info(f"wxmusic: used third source [{f_source}] for QQ '{keyword}'")



        # 歌词

        lyric_text = ""

        lyric_result = qq_get_lyric(song_mid, cookie=cookie)

        if lyric_result.get("code") == 0:

            lyric_text = lyric_result.get("lyric", "")



        link = f"https://y.qq.com/n/ryqq/songDetail/{song_mid}"



        save_log({"time": datetime.now().isoformat(), "keyword": keyword, "source": "qq", "result": "ok", "song": song_name, "artist": singers, "has_url": bool(music_url), "elapsed_ms": elapsed, "ip": ""})



        return JSONResponse({

            "code": 200,

            "title": song_name,

            "singer": singers,

            "cover": album_pic,

            "link": link,

            "music_url": music_url,

            "lyric": lyric_text,

        })



    # ==================== 网易云（默认） ====================

    cookie = cfg.get("cookie", "")

    search_result = search_song(keyword, page=1, limit=1, cookie=cookie)

    elapsed = round((time.time() - start_time) * 1000)

    

    if search_result.get("code") != 200:

        save_log({"time": datetime.now().isoformat(), "keyword": keyword, "source": "netease", "result": "error", "error": search_result.get("msg"), "elapsed_ms": elapsed, "ip": ""})

        return JSONResponse({"code": -1, "msg": search_result.get("msg", "搜索失败")})

    

    songs_data = search_result.get("result", {}).get("songs", [])

    if not songs_data:

        save_log({"time": datetime.now().isoformat(), "keyword": keyword, "source": "netease", "result": "empty", "error": None, "elapsed_ms": elapsed, "ip": ""})

        return JSONResponse({"code": -1, "msg": "未找到歌曲"})

    

    first_song = songs_data[0]

    song_id = first_song.get("id")

    

    # 获取播放链接

    music_url = _resolve_netease_play_url(song_id, cfg, cookie=cookie)

    

    # 如果主源没拿到链接，尝试第三方音源 fallback

    if not music_url:

        all_sources = _get_all_source_configs(cfg)

        enabled_dict = {k: v for k, v in all_sources.items() if v.get("enabled")}

        if enabled_dict:

            quality = configured_player_quality(cfg)

            f_url, f_source = _resolve_player_third_source("netease", first_song, cfg, quality=quality)

            if f_url:

                music_url = f_url

                logger.info(f"wxmusic: used third source [{f_source}] for '{keyword}'")

    

    # 获取歌词

    lyric_result = get_song_lyric(song_id, cookie=cookie)

    lyric_text = ""

    if lyric_result.get("code") == 200:

        lrc = lyric_result.get("lrc", {}) or {}

        lyric_text = lrc.get("lyric", "")

    

    # 组装信息（cloudsearch 返回的数据里 al 含完整 picUrl）

    name = first_song.get("name", "")

    artists = "/".join(a.get("name", "") for a in (first_song.get("ar", []) or first_song.get("artists", [])))

    al = first_song.get("al", {})

    if not isinstance(al, dict):

        al = {}

    

    album_pic = al.get("picUrl", "")

    if album_pic:

        album_pic = album_pic.replace("http:", "https:")

    

    # 微信音乐主页链接

    link = f"https://music.163.com/song?id={song_id}"

    

    save_log({"time": datetime.now().isoformat(), "keyword": keyword, "source": "netease", "result": "ok", "song": name, "artist": artists, "has_url": bool(music_url), "elapsed_ms": elapsed, "ip": ""})

    

    return JSONResponse({

        "code": 200,

        "title": name,

        "singer": artists,

        "cover": album_pic,

        "link": link,

        "music_url": music_url,

        "lyric": lyric_text,

    })





@app.get("/api/status")

async def api_status(key: str = Query(default="")):

    """服务状态 + cookie 有效性"""

    if not check_auth(key):

        raise HTTPException(status_code=403, detail="Invalid API Key")

    

    cfg = load_config()

    logs = load_logs()

    

    return JSONResponse({

        "code": 0,

        "service": "点歌API",

        "version": "1.1.0",

        "cookie_status": cfg.get("cookie_status", "unknown"),

        "cookie_nickname": cfg.get("nickname"),

        "cookie_checked_at": cfg.get("cookie_checked_at"),

        "last_error": cfg.get("last_error"),

        "qq_status": cfg.get("qq_status", "unknown"),

        "qq_checked_at": cfg.get("qq_checked_at"),

        "qq_refreshed_at": cfg.get("qq_refreshed_at"),

        "qq_next_refresh_at": cfg.get("qq_next_refresh_at"),

        "total_calls": len(logs),

        "recent_calls": logs[:10],

    })





# ==================== 管理后台 ====================



@app.get("/music", response_class=HTMLResponse)

@app.get("/music/", response_class=HTMLResponse)

async def music_player_page():

    return FileResponse(STATIC_DIR / "music.html", media_type="text/html", headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/api/site/announcement")

async def api_site_announcement():

    return JSONResponse({"code": 0, "announcement": str(load_config().get("announcement") or "")})





def _playlist_link_error(message):

    raise ValueError(message)





def _is_allowed_playlist_host(host):

    host = str(host or "").lower().strip(".")

    allowed_hosts = (

        "163cn.tv", "music.163.com", "y.music.163.com",

        "y.qq.com", "i.y.qq.com", "c6.y.qq.com",

        "kugou.com", "t1.kugou.com", "activity.kugou.com",

        "kuwo.cn", "qishui.douyin.com", "music.douyin.com",

    )

    return any(host == item or host.endswith(f".{item}") for item in allowed_hosts)





def _resolve_playlist_share_url(raw_url):

    """Resolve approved music share short links before parsing their playlist id."""

    value = str(raw_url or "").strip()

    if not value or len(value) > 2048:

        _playlist_link_error("???????????????? 2048 ???")

    parsed = urlsplit(value)

    if parsed.scheme not in ("http", "https") or not parsed.netloc:

        _playlist_link_error("????????????????")

    if not _is_allowed_playlist_host(parsed.hostname):

        _playlist_link_error("???????????QQ ????????????????????????")



    # Canonical links do not need a network round-trip. Short/share links are

    # followed with an allow-list at every hop to avoid turning this into a proxy.

    short_hosts = {"163cn.tv", "t1.kugou.com", "qishui.douyin.com", "c6.y.qq.com"}

    if (parsed.hostname or "").lower() not in short_hosts:

        return value



    headers = {

        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",

        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",

        "Accept-Language": "zh-CN,zh;q=0.9",

    }

    current = value

    try:

        for _ in range(5):

            response = requests.get(current, headers=headers, timeout=15, allow_redirects=False)

            if response.status_code not in (301, 302, 303, 307, 308):

                return current

            location = str(response.headers.get("Location") or "").strip()

            if not location:

                break

            next_url = requests.compat.urljoin(current, location)

            next_parsed = urlsplit(next_url)

            if next_parsed.scheme not in ("http", "https") or not _is_allowed_playlist_host(next_parsed.hostname):

                _playlist_link_error("???????????????")

            current = next_url

    except requests.RequestException as exc:

        _playlist_link_error(f"?????????{exc}")

    _playlist_link_error("??????????????????")





def _parse_playlist_share_url(raw_url):

    """Recognize public playlist links from supported music services."""

    value = str(raw_url or "").strip()

    if not value or len(value) > 2048:

        _playlist_link_error("???????????????? 2048 ???")

    parsed = urlsplit(value)

    if parsed.scheme not in ("http", "https") or not parsed.netloc:

        _playlist_link_error("????????????????")



    host = (parsed.hostname or "").lower()

    query = parse_qs(parsed.query)

    fragment_query = parse_qs(parsed.fragment.split("?", 1)[1]) if "?" in parsed.fragment else {}



    if host == "music.163.com" or host.endswith(".music.163.com"):

        path_match = re.search(r"/playlist/(\d+)", parsed.path)

        playlist_id = (

            query.get("id", [None])[0]

            or fragment_query.get("id", [None])[0]

            or (path_match.group(1) if path_match else None)

        )

        if playlist_id and str(playlist_id).isdigit():

            return "netease", str(playlist_id)

        _playlist_link_error("??????????????? ID")



    if host == "y.qq.com" or host.endswith(".y.qq.com") or host == "i.y.qq.com":

        path_match = re.search(r"/playlist/(\d+)", parsed.path)

        playlist_id = (

            query.get("id", [None])[0]

            or query.get("disstid", [None])[0]

            or (path_match.group(1) if path_match else None)

        )

        if playlist_id and str(playlist_id).isdigit():

            return "qq", str(playlist_id)

        _playlist_link_error("??? QQ ????????? ID")



    if host == "kugou.com" or host.endswith(".kugou.com"):

        collection_id = query.get("global_specialid", [None])[0] or query.get("global_collection_id", [None])[0]

        if collection_id and re.fullmatch(r"collection_\d+_\d+_\d+_\d+", str(collection_id)):

            return "kugou_collection", str(collection_id)

        path_match = re.search(r"/(?:playlist|special)/(?:single/)?(\d+)", parsed.path)

        playlist_id = (

            query.get("specialid", [None])[0]

            or query.get("global_id", [None])[0]

            or query.get("sid", [None])[0]

            or query.get("id", [None])[0]

            or (path_match.group(1) if path_match else None)

        )

        if playlist_id and str(playlist_id).isdigit() and str(playlist_id) != "0":

            return "kugou", str(playlist_id)

        _playlist_link_error("???????????????? ID")



    if host == "kuwo.cn" or host.endswith(".kuwo.cn"):

        path_match = re.search(r"/(?:playlist_detail|playlist|??)/(\d+)", parsed.path)

        playlist_id = (

            query.get("pid", [None])[0]

            or query.get("id", [None])[0]

            or (path_match.group(1) if path_match else None)

        )

        if playlist_id and str(playlist_id).isdigit():

            return "kuwo", str(playlist_id)

        _playlist_link_error("?????????????? ID")



    if host == "qishui.douyin.com" or host == "music.douyin.com":

        if host == "qishui.douyin.com" and re.fullmatch(r"/s/[A-Za-z0-9_-]+/?", parsed.path):

            return "qishui", value

        if re.fullmatch(r"/qishui/share/playlist/?", parsed.path):

            playlist_id = query.get("playlist_id", [None])[0]

            if playlist_id and str(playlist_id).isdigit():

                return "qishui", value

        _playlist_link_error("????????????????")



    _playlist_link_error("??????????QQ ????????????????????????")





def _resolve_playlist_share_url_and_parse(raw_url):

    resolved_url = _resolve_playlist_share_url(raw_url)

    return _parse_playlist_share_url(resolved_url)





MAX_PLAYLIST_TRACKS = 10000



def _dedupe_playlist_songs(songs):

    unique = []

    seen = set()

    for song in songs:

        if not song:

            continue

        key = (str(song.get("source") or ""), str(song.get("id") or ""))

        if not key[1] or key in seen:

            continue

        seen.add(key)

        unique.append(song)

        if len(unique) >= MAX_PLAYLIST_TRACKS:

            break

    return unique



def _playlist_total(data):

    if not isinstance(data, dict):

        return 0

    for key in ("total", "total_count", "totalCount", "song_count", "songCount", "total_song_num", "totalNum"):

        try:

            value = int(data.get(key) or 0)

        except (TypeError, ValueError):

            continue

        if value > 0:

            return value

    return 0



def _ensure_playlist_total_within_limit(total):

    if total > MAX_PLAYLIST_TRACKS:

        raise ValueError(f"歌单超过系统安全上限 {MAX_PLAYLIST_TRACKS} 首，暂不支持完整导入")



def _netease_playlist_tracks(playlist, cookie="", limit=MAX_PLAYLIST_TRACKS):

    """网易云歌单详情只内联前 20 首，其余按 trackIds 批量补全。"""

    tracks = [item for item in (playlist.get("tracks") or []) if isinstance(item, dict) and item.get("id")]

    track_ids = [

        str(item.get("id")) for item in (playlist.get("trackIds") or [])

        if isinstance(item, dict) and item.get("id")

    ]

    if len(track_ids) > limit:

        raise ValueError(f"歌单超过系统安全上限 {limit} 首，暂不支持完整导入")

    if not track_ids:

        return tracks

    known = {str(item.get("id")): item for item in tracks}

    missing = [song_id for song_id in track_ids if song_id not in known]

    for start in range(0, len(missing), 100):

        chunk = missing[start:start + 100]

        payload = json.dumps([{"id": int(song_id)} for song_id in chunk], separators=(",", ":"))

        detail = ncm_request("/api/v3/song/detail", params={"c": payload}, method="POST", cookie=cookie)

        if not isinstance(detail, dict) or detail.get("code") != 200:

            raise ValueError(f"网易云音乐歌单歌曲详情读取失败（第 {start // 100 + 1} 批）")

        for song in (detail.get("songs") or []):

            if isinstance(song, dict) and song.get("id"):

                known[str(song.get("id"))] = song

    ordered = [known[song_id] for song_id in track_ids if song_id in known]

    if len(ordered) != len(track_ids):

        raise ValueError(f"网易云音乐歌单读取不完整：应有 {len(track_ids)} 首，实际获得 {len(ordered)} 首")

    return ordered or tracks





def _get_netease_playlist(playlist_id, cookie=""):

    result = ncm_request(

        "/api/v6/playlist/detail",

        params={"id": playlist_id, "n": MAX_PLAYLIST_TRACKS, "s": 0},

        cookie=cookie,

    )

    playlist = result.get("playlist") if isinstance(result, dict) else None

    if result.get("code") != 200 or not isinstance(playlist, dict):

        raise ValueError(result.get("msg") or "网易云音乐歌单读取失败，请确认链接公开且有效")

    songs = [_playlist_song(_player_netease_song(song)) for song in _netease_playlist_tracks(playlist, cookie)]

    songs = _dedupe_playlist_songs(songs)

    if not songs:

        raise ValueError("该网易云音乐歌单没有可导入的歌曲，可能为私密歌单或接口暂未返回歌曲")

    return str(playlist.get("name") or "网易云音乐歌单"), songs





def _get_qq_playlist(playlist_id, cookie=""):

    # uniform_get_Dissinfo 只接受数值型 disstid，传字符串会固定返回 code=10006

    try:

        disstid = int(str(playlist_id).strip())

    except (TypeError, ValueError):

        disstid = str(playlist_id)

    headers = {"Referer": "https://y.qq.com/n/ryqq/playlist/"}

    if cookie:

        headers["Cookie"] = cookie

    page_size = 500

    songs = []

    title = ""

    seen = set()

    uniform_available = True

    for song_begin in range(0, MAX_PLAYLIST_TRACKS, page_size):

        payload = {

            "req_0": {

                "module": "music.srfDissInfo.aiDissInfo",

                "method": "uniform_get_Dissinfo",

                # QQ returns at most song_num - 1 usable rows on the first page.

                "param": {"disstid": disstid, "song_num": page_size + 1, "song_begin": song_begin, "userinfo": 1, "tag": 1, "orderlist": 1},

            },

            "comm": {"ct": 24, "cv": 0, "uin": 0, "format": "json"},

        }

        try:

            response = QQ_SESSION.get("https://u.y.qq.com/cgi-bin/musicu.fcg", params={"format": "json", "data": json.dumps(payload, separators=(",", ":"))}, headers=headers, timeout=20)

            result = response.json() if response.status_code == 200 else {"code": response.status_code}

        except (requests.RequestException, ValueError) as exc:

            if song_begin:

                raise ValueError(f"QQ 音乐歌单第 {song_begin // page_size + 1} 页读取失败：{exc}") from exc

            uniform_available = False

            break

        entry = result.get("req_0", {}) if isinstance(result, dict) else {}

        detail = entry.get("data") if isinstance(entry, dict) else None

        if entry.get("code") != 0 or not isinstance(detail, dict) or detail.get("code") not in (0, None):

            if song_begin:

                raise ValueError(f"QQ 音乐歌单第 {song_begin // page_size + 1} 页读取失败")

            uniform_available = False

            break

        cdlist = detail.get("cdlist") or []

        item = cdlist[0] if cdlist and isinstance(cdlist[0], dict) else detail

        records = item.get("songlist") or []

        if not records and not song_begin:

            uniform_available = False

            break

        dirinfo = item.get("dirinfo") if isinstance(item.get("dirinfo"), dict) else detail.get("dirinfo")

        dirinfo = dirinfo if isinstance(dirinfo, dict) else {}

        title = title or str(item.get("dissname") or item.get("title") or dirinfo.get("title") or "")

        added = 0

        for raw_song in records:

            song = _playlist_song(_player_qq_song(raw_song))

            key = (song.get("source"), song.get("id")) if song else None

            if not song or key in seen:

                continue

            seen.add(key)

            songs.append(song)

            added += 1

        total = _playlist_total(item) or _playlist_total(detail)

        _ensure_playlist_total_within_limit(total)

        if len(songs) > MAX_PLAYLIST_TRACKS:

            _ensure_playlist_total_within_limit(len(songs))

        if not records or not added or (total and len(songs) >= total):

            break

    if not uniform_available:

        songs = []

        seen = set()

        title = ""

        for song_begin in range(0, MAX_PLAYLIST_TRACKS, page_size):

            try:

                legacy = QQ_SESSION.get(

                    "https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg",

                    params={"type": 1, "json": 1, "utf8": 1, "onlysong": 0, "disstid": str(playlist_id), "song_begin": song_begin, "song_num": page_size, "format": "json", "g_tk": qq_get_gtk(cookie), "loginUin": 0, "hostUin": 0, "inCharset": "utf8", "outCharset": "utf-8", "notice": 0, "platform": "yqq.json", "needNewCode": 0},

                    headers=headers,

                    timeout=20,

                )

                detail = legacy.json() if legacy.status_code == 200 else {}

            except (requests.RequestException, ValueError) as exc:

                raise ValueError(f"QQ 音乐歌单第 {song_begin // page_size + 1} 页读取失败：{exc}") from exc

            if detail.get("subcode") != 0:

                raise ValueError("QQ 音乐歌单读取失败，请确认链接公开，或在管理后台填写 QQ 音乐 Cookie")

            cdlist = detail.get("cdlist") or []

            item = cdlist[0] if cdlist and isinstance(cdlist[0], dict) else detail

            records = item.get("songlist") or []

            title = title or str(item.get("dissname") or item.get("title") or "")

            added = 0

            for raw_song in records:

                song = _playlist_song(_player_qq_song(raw_song))

                key = (song.get("source"), song.get("id")) if song else None

                if not song or key in seen:

                    continue

                seen.add(key)

                songs.append(song)

                added += 1

            total = _playlist_total(item) or _playlist_total(detail)

            _ensure_playlist_total_within_limit(total)

            if len(songs) > MAX_PLAYLIST_TRACKS:

                _ensure_playlist_total_within_limit(len(songs))

            if not records or not added or (total and len(songs) >= total):

                break

    if not songs:

        raise ValueError("该 QQ 音乐歌单没有可导入的歌曲，可能为私密歌单或接口暂未返回歌曲")

    return title or "QQ 音乐歌单", songs





def _get_kugou_playlist(playlist_id):

    """Read a public Kugou playlist without requiring a user account."""

    headers = {

        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",

        "Referer": f"https://www.kugou.com/yy/special/single/{playlist_id}.html",

        "kg-RC": "1",

    }

    base_params = {

        "specialid": str(playlist_id),

        "appid": "1005",

        "clientver": "10026",

        "mid": "1",

        "uuid": "1",

        "dfid": "1",

    }

    try:

        detail_response = requests.get(

            "https://gateway.kugou.com/v3/get_special_info",

            params=base_params,

            headers=headers,

            timeout=20,

        )

        detail_result = detail_response.json() if detail_response.ok else {}

        detail = detail_result.get("data") if isinstance(detail_result, dict) else {}

        playlist_name = str((detail or {}).get("specialname") or (detail or {}).get("name") or "酷狗音乐歌单")



    except (requests.RequestException, ValueError) as exc:

        raise ValueError(f"酷狗音乐歌单读取失败：{exc}") from exc

    page_size = 300

    songs = []

    seen = set()

    result = {}

    for page in range(1, MAX_PLAYLIST_TRACKS // page_size + 2):

        try:

            list_response = requests.get("https://gateway.kugou.com/v2/get_other_list_file", params={**base_params, "page": page, "pagesize": page_size}, headers=headers, timeout=20)

            if not list_response.ok:

                raise ValueError(f"HTTP {list_response.status_code}")

            result = list_response.json()

        except (requests.RequestException, ValueError) as exc:

            raise ValueError(f"酷狗音乐歌单第 {page} 页读取失败：{exc}") from exc

        data = result.get("data") if isinstance(result, dict) else {}

        records = (data or {}).get("info") or (data or {}).get("list") or []

        added = 0

        for raw_song in records:

            song = _playlist_song(_player_kugou_song(raw_song)) if isinstance(raw_song, dict) else None

            key = (song.get("source"), song.get("id")) if song else None

            if not song or key in seen:

                continue

            seen.add(key)

            songs.append(song)

            added += 1

        total = _playlist_total(data)

        _ensure_playlist_total_within_limit(total)

        if len(songs) > MAX_PLAYLIST_TRACKS:

            _ensure_playlist_total_within_limit(len(songs))

        if not records or not added or len(records) < page_size or (total and len(songs) >= total) or len(songs) >= MAX_PLAYLIST_TRACKS:

            break

    if not songs:

        message = result.get("error") or result.get("msg") or "接口未返回歌曲"

        raise ValueError(f"酷狗音乐歌单读取失败：{message}。请确认歌单公开且链接有效")

    return playlist_name, songs







def _kugou_h5_signed_params(params):

    salt = "NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt"

    canonical = "".join(f"{key}={params[key]}" for key in sorted(params))

    return hashlib.md5(f"{salt}{canonical}{salt}".encode()).hexdigest()





def _get_kugou_collection_playlist(collection_id):

    """Read a public Kugou collection share URL (global_specialid=collection_*)."""

    if not re.fullmatch(r"collection_\d+_\d+_\d+_\d+", str(collection_id)):

        raise ValueError("?????? ID ??")



    now = str(int(time.time() * 1000))

    base_params = {

        "srcappid": "2919", "clientver": "20000", "clienttime": now,

        "mid": now, "uuid": now, "dfid": "-", "uid": "0", "appid": "1058",

        "token": "", "module": "playlist", "pagesize": 300,

        "global_collection_id": str(collection_id),

    }

    headers = {

        "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/124 Mobile Safari/537.36",

        "Referer": "https://activity.kugou.com/share/",

        "Origin": "https://activity.kugou.com",

        "clienttime": now,

        "mid": now,

        "dfid": "-",

    }

    songs = []

    seen = set()

    result = {}

    page_size = int(base_params["pagesize"])

    for page in range(1, MAX_PLAYLIST_TRACKS // page_size + 2):

        params = {**base_params, "page": page}

        params["signature"] = _kugou_h5_signed_params(params)

        try:

            response = requests.get("https://pubsongscdn.kugou.com/v2/get_other_list_file", params=params, headers=headers, timeout=25)

            if not response.ok:

                raise ValueError(f"HTTP {response.status_code}")

            result = response.json()

        except (requests.RequestException, ValueError) as exc:

            raise ValueError(f"酷狗音乐合集第 {page} 页读取失败：{exc}") from exc

        data = result.get("data") if isinstance(result, dict) else {}

        records = (data or {}).get("info") or []

        added = 0

        for raw_song in records:

            song = _playlist_song(_player_kugou_song(raw_song)) if isinstance(raw_song, dict) else None

            key = (song.get("source"), song.get("id")) if song else None

            if not song or key in seen:

                continue

            seen.add(key)

            songs.append(song)

            added += 1

        total = _playlist_total(data)

        _ensure_playlist_total_within_limit(total)

        if len(songs) > MAX_PLAYLIST_TRACKS:

            _ensure_playlist_total_within_limit(len(songs))

        if not records or not added or len(records) < page_size or (total and len(songs) >= total) or len(songs) >= MAX_PLAYLIST_TRACKS:

            break

    if not songs:

        message = result.get("errmsg") or result.get("error") or result.get("msg") or "???????"

        raise ValueError(f"?????????????{message}??????????????")



    playlist_name = "??????"

    detail_params = {

        "srcappid": "2919", "clientver": "20000", "clienttime": now,

        "mid": now, "uuid": now, "dfid": "-", "specialid": "0",

        "global_specialid": str(collection_id), "sign": "h5",

    }

    detail_params["signature"] = _kugou_h5_signed_params(detail_params)

    try:

        detail_response = requests.get(

            "https://mobiles.kugou.com/v5/special/info_v2",

            params=detail_params,

            headers=headers,

            timeout=20,

        )

        detail_result = detail_response.json() if detail_response.ok else {}

        detail = detail_result.get("data") if isinstance(detail_result, dict) else {}

        playlist_name = str((detail or {}).get("specialname") or playlist_name)

    except (requests.RequestException, ValueError):

        pass

    return playlist_name, songs





def _get_kuwo_playlist(playlist_id):

    """Read a public Kuwo playlist using its public web playlist endpoint."""

    session = requests.Session()

    headers = {

        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",

        "Referer": f"https://www.kuwo.cn/playlist_detail/{playlist_id}",

    }

    try:

        # Kuwo normally sets kw_token on the page response; anonymous access is enough for public playlists.

        session.get(headers["Referer"], headers=headers, timeout=15)

    except requests.RequestException as exc:

        raise ValueError(f"酷我音乐歌单读取失败：{exc}") from exc

    api_headers = {**headers, "csrf": session.cookies.get("kw_token", "")}

    page_size = 300

    songs = []

    seen = set()

    playlist_name = ""

    for page in range(1, MAX_PLAYLIST_TRACKS // page_size + 2):

        try:

            response = session.get("https://www.kuwo.cn/api/www/playlist/playListInfo", params={"pid": str(playlist_id), "pn": page, "rn": page_size, "httpsStatus": 1, "reqId": uuid.uuid4().hex}, headers=api_headers, timeout=20)

            result = response.json() if response.ok else {}

        except (requests.RequestException, ValueError) as exc:

            raise ValueError(f"酷我音乐歌单第 {page} 页读取失败：{exc}") from exc

        if not isinstance(result, dict) or result.get("code") not in (200, "200", 0, "0"):

            message = result.get("message") or result.get("msg") if isinstance(result, dict) else ""

            raise ValueError(f"酷我音乐歌单第 {page} 页读取失败：{message or '请确认歌单公开且链接有效'}")

        data = result.get("data") or {}

        records = data.get("musicList") or data.get("list") or []

        playlist_name = playlist_name or str(data.get("name") or data.get("playlistName") or "")

        added = 0

        for raw_song in records:

            song = _playlist_song(_player_kuwo_song(raw_song)) if isinstance(raw_song, dict) else None

            key = (song.get("source"), song.get("id")) if song else None

            if not song or key in seen:

                continue

            seen.add(key)

            songs.append(song)

            added += 1

        total = _playlist_total(data)

        _ensure_playlist_total_within_limit(total)

        if len(songs) > MAX_PLAYLIST_TRACKS:

            _ensure_playlist_total_within_limit(len(songs))

        if not records or not added or len(records) < page_size or (total and len(songs) >= total) or len(songs) >= MAX_PLAYLIST_TRACKS:

            break

    if not songs:

        raise ValueError("酷我音乐歌单没有可导入的歌曲，可能为私密歌单或平台临时限制访问")

    return playlist_name or "酷我音乐歌单", songs







def _account_cookie(account):

    return str(_decrypt_credential(account.get("credential_encrypted")).get("cookie") or "")





def _netease_account_profile(cookie):

    result = ncm_request("/api/nuser/account/get", cookie=cookie)

    profile = result.get("profile") if isinstance(result, dict) else None

    if result.get("code") != 200 or not isinstance(profile, dict) or not profile.get("userId"):

        raise ValueError(result.get("msg") or "网易云会话无效或已过期")

    return str(profile["userId"]), str(profile.get("nickname") or "网易云用户")





def _netease_account_playlists(account):

    cookie = _account_cookie(account)

    user_id = str(account.get("platform_user_id") or "")

    result = ncm_request("/api/user/playlist", params={"uid": user_id, "limit": 1000, "offset": 0}, cookie=cookie)

    items = result.get("playlist", []) if isinstance(result, dict) else []

    if result.get("code") != 200 or not isinstance(items, list):

        raise ValueError(result.get("msg") or "网易云歌单读取失败，请重新连接账户")

    return [(str(item.get("id")), str(item.get("name") or "网易云歌单")) for item in items if item.get("id")]





def _qq_account_profile(cookie):
    fields = extract_qq_cookie_fields(cookie)
    uin = str(fields.get("qq_uin") or "")
    if not uin:
        raise ValueError("QQ 音乐会话中缺少 uin，请粘贴完整的 QQ 音乐 Cookie")
    return uin, f"QQ 用户 {uin}"


def _qq_account_playlists(account):
    """Read playlists owned by the connected QQ Music account."""
    cookie = _account_cookie(account)
    user_id, _ = _qq_account_profile(cookie)
    params = {
        "format": "json",
        "inCharset": "utf8",
        "outCharset": "utf-8",
        "notice": 0,
        "platform": "yqq.json",
        "needNewCode": 0,
        "g_tk": qq_get_gtk(cookie),
        "uin": user_id,
        # QQ's endpoint expects this parameter in lowercase.
        "hostuin": user_id,
        "loginUin": user_id,
        "sin": 0,
        "size": 100,
    }
    try:
        response = QQ_SESSION.get(
            "https://c.y.qq.com/rsc/fcgi-bin/fcg_user_created_diss",
            params=params,
            headers={"Referer": "https://y.qq.com/", "Cookie": cookie},
            timeout=20,
        )
        result = json.loads(response.content.decode("utf-8")) if response.ok else {}
    except (requests.RequestException, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"QQ 音乐歌单列表读取失败：{exc}") from exc

    data = result.get("data") if isinstance(result, dict) else None
    if result.get("code") != 0 or not isinstance(data, dict):
        message = result.get("message") or result.get("msg") if isinstance(result, dict) else ""
        raise ValueError(f"QQ 音乐歌单列表读取失败：{message or '请重新连接 QQ 音乐账户'}")

    playlists = []
    seen_ids = set()
    for item in data.get("disslist") or []:
        if not isinstance(item, dict):
            continue
        playlist_id = str(item.get("tid") or item.get("dissid") or "").strip()
        if not playlist_id or playlist_id == "0" or playlist_id in seen_ids:
            continue
        seen_ids.add(playlist_id)
        name = str(item.get("diss_name") or item.get("dissname") or "QQ 音乐歌单").strip()
        playlists.append((playlist_id, name))
    return playlists


def _kugou_cookie_fields(cookie):

    """Read both Kugou app-session and browser-session cookie names."""

    return {

        "token": parse_cookie_kv(cookie, "token"),

        "userid": parse_cookie_kv(cookie, "userid"),

        "web_userid": parse_cookie_kv(cookie, "KugooID") or parse_cookie_kv(cookie, "kg_userid"),

        "dfid": parse_cookie_kv(cookie, "dfid") or parse_cookie_kv(cookie, "kg_dfid"),

        "mid": parse_cookie_kv(cookie, "KUGOU_API_MID") or parse_cookie_kv(cookie, "kg_mid") or parse_cookie_kv(cookie, "mid"),

        "web_token": parse_cookie_kv(cookie, "t"),

    }





def _kugou_cookie_mode(fields):

    if fields.get("token") and fields.get("userid"):

        return "app"

    if fields.get("web_userid") and (fields.get("web_token") or fields.get("mid")):

        return "web"

    return "unknown"







def _kugou_device():

    guid = uuid.uuid4().hex.upper()

    return {"guid": guid, "mid": hashlib.md5(guid.encode()).hexdigest().upper(), "dfid": "-", "mac": secrets.token_hex(6).upper(), "dev": secrets.token_hex(8).upper()}





def _kugou_login_signed_params(params):

    """Kugou QR-login endpoints use the web signature, not the app API one."""

    canonical = "".join(f"{key}={params[key]}" for key in sorted(params))

    return hashlib.md5(f"NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt{canonical}NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt".encode()).hexdigest()





def _kugou_signed_params(params, body=""):

    canonical = "".join(f"{key}={params[key]}" for key in sorted(params))

    return hashlib.md5(f"LnT6xpN3khm36zse0QzvmgTZ3waWdRSA{canonical}{body}LnT6xpN3khm36zse0QzvmgTZ3waWdRSA".encode()).hexdigest()





def _kugou_account_profile(cookie):

    fields = _kugou_cookie_fields(cookie)

    if not fields["token"] or not fields["userid"]:

        raise ValueError("酷狗音乐会话中缺少 token 或 userid，请重新扫码授权")

    return str(fields["userid"]), f"酷狗用户 {fields['userid']}"





def _kugou_account_playlists(account):

    cookie = _account_cookie(account)

    fields = _kugou_cookie_fields(cookie)

    user_id, _ = _kugou_account_profile(cookie)

    now = str(int(time.time()))

    params = {"dfid": fields["dfid"] or "-", "mid": fields["mid"] or hashlib.md5(user_id.encode()).hexdigest().upper(), "uuid": "-", "appid": "3116", "clientver": "11440", "clienttime": now, "token": fields["token"], "userid": user_id, "plat": "1"}

    payload = {"userid": user_id, "token": fields["token"], "total_ver": 979, "type": 2, "page": 1, "pagesize": 100}

    raw_body = json.dumps(payload, separators=(",", ":"))

    params["signature"] = _kugou_signed_params(params, raw_body)

    headers = {"User-Agent": "Android15-1070-11083-46-0-DiscoveryDRADProtocol-wifi", "Content-Type": "application/json", "x-router": "cloudlist.service.kugou.com", "dfid": params["dfid"], "clienttime": now, "mid": params["mid"], "kg-rc": "1", "kg-thash": "5d816a0", "kg-rec": "1", "kg-rf": "B9EDA08A64250DEFFBCADDEE00F8F25F", "Cookie": cookie}

    try:

        response = requests.post("https://gateway.kugou.com/v7/get_all_list", params=params, data=raw_body, headers=headers, timeout=20)

        result = response.json() if response.ok else {}

    except (requests.RequestException, ValueError) as exc:

        raise ValueError(f"酷狗音乐歌单读取失败：{exc}") from exc

    data = result.get("data") if isinstance(result, dict) else {}

    items = (data or {}).get("info") or (data or {}).get("list") or (data or {}).get("playlist") or []

    playlists = []

    seen_ids = set()

    for item in items:

        if not isinstance(item, dict):

            continue

        # User-created Kugou cloud playlists use listid; public playlists use specialid.

        list_id = str(item.get("listid") or "").strip()

        special_id = str(item.get("specialid") or "").strip()

        remote_id = f"cloudlist:{list_id}" if list_id and list_id != "0" else special_id

        if not remote_id or remote_id in seen_ids:

            continue

        seen_ids.add(remote_id)

        name = str(item.get("name") or item.get("specialname") or "酷狗音乐歌单").strip()

        playlists.append((remote_id, name))

    return playlists





def _get_kugou_account_playlist(playlist_id, cookie):

    """Read a connected Kugou account's private cloud playlist."""

    playlist_id = str(playlist_id or "").strip()

    if not playlist_id.startswith("cloudlist:"):

        return _get_kugou_playlist(playlist_id)



    list_id = playlist_id.split(":", 1)[1].strip()

    fields = _kugou_cookie_fields(cookie)

    user_id, _ = _kugou_account_profile(cookie)

    if not list_id:

        raise ValueError("酷狗音乐云歌单缺少 listid")

    if not fields["mid"]:

        raise ValueError("酷狗音乐会话中缺少 KUGOU_API_MID，请重新扫码授权")



    now = str(int(time.time()))

    params = {

        "dfid": fields["dfid"] or "-",

        "mid": fields["mid"],

        "uuid": "-",

        "appid": "3116",

        "clientver": "11440",

        "clienttime": now,

        "token": fields["token"],

        "userid": user_id,

    }

    payload = {

        "listid": list_id,

        "userid": user_id,

        "area_code": 1,

        "show_relate_goods": 1,

        "pagesize": 300,

        "allplatform": 1,

        "show_cover": 1,

        "type": 0,

        "token": fields["token"],

        "page": 1,

    }

    headers = {

        "User-Agent": "Android15-1070-11083-46-0-DiscoveryDRADProtocol-wifi",

        "Content-Type": "application/json",

        "x-router": "cloudlist.service.kugou.com",

        "dfid": params["dfid"],

        "clienttime": now,

        "mid": params["mid"],

        "kg-rc": "1",

        "kg-thash": "5d816a0",

        "kg-rec": "1",

        "kg-rf": "B9EDA08A64250DEFFBCADDEE00F8F25F",

        "Cookie": cookie,

    }

    page_size = int(payload["pagesize"])

    records = []

    seen_records = set()

    for page in range(1, MAX_PLAYLIST_TRACKS // page_size + 2):

        page_payload = {**payload, "page": page}

        raw_body = json.dumps(page_payload, separators=(",", ":"))

        page_params = dict(params)

        page_params["signature"] = _kugou_signed_params(page_params, raw_body)

        try:

            response = requests.post("https://gateway.kugou.com/v4/get_list_all_file", params=page_params, data=raw_body, headers=headers, timeout=25)

            result = response.json() if response.ok else {}

        except (requests.RequestException, ValueError) as exc:

            raise ValueError(f"酷狗音乐云歌单第 {page} 页读取失败：{exc}") from exc

        if result.get("status") != 1 or int(result.get("error_code") or 0) != 0:

            message = result.get("error") or result.get("msg") or "接口未返回歌曲"

            raise ValueError(f"酷狗音乐云歌单第 {page} 页读取失败：{message}")

        data = result.get("data") if isinstance(result, dict) else {}

        page_records = (data or {}).get("info") or []

        added = 0

        for item in page_records:

            if not isinstance(item, dict):

                continue

            record_key = str(item.get("hash") or item.get("filehash") or item.get("audio_id") or item.get("id") or "")

            if not record_key:

                record_key = json.dumps(item, sort_keys=True, ensure_ascii=False)

            if record_key in seen_records:

                continue

            seen_records.add(record_key)

            records.append(item)

            added += 1

        total = _playlist_total(data)

        _ensure_playlist_total_within_limit(total)

        if len(records) > MAX_PLAYLIST_TRACKS:

            _ensure_playlist_total_within_limit(len(records))

        if not page_records or not added or len(page_records) < page_size or (total and len(records) >= total) or len(records) >= MAX_PLAYLIST_TRACKS:

            break

    songs = []

    for item in records:

        if not isinstance(item, dict):

            continue

        singer_info = item.get("singerinfo") or []

        artists = "/".join(

            str(singer.get("name") or "").strip()

            for singer in singer_info

            if isinstance(singer, dict) and singer.get("name")

        )

        raw_name = str(item.get("name") or item.get("filename") or "").strip()

        display_name = raw_name.rsplit(".", 1)[0] if raw_name.lower().endswith((".mp3", ".flac", ".m4a", ".aac", ".wav")) else raw_name

        if " - " in display_name:

            possible_artist, possible_title = display_name.split(" - ", 1)

            if possible_title.strip():

                if not artists and possible_artist.strip():

                    artists = possible_artist.strip()

                display_name = possible_title.strip()

        normalized = _playlist_song(_player_kugou_song({

            "hash": item.get("hash") or item.get("filehash"),

            "filename": display_name,

            "singername": artists,

            "album_info": item.get("albuminfo") or {},

            "cover": item.get("cover") or "",

            "timelength": item.get("timelen") or item.get("duration") or 0,

        }))

        if normalized:

            songs.append(normalized)

    songs = _dedupe_playlist_songs(songs)

    if not songs:

        raise ValueError("酷狗音乐云歌单未返回可同步的歌曲")

    return f"酷狗音乐歌单 {list_id}", songs





def _qishui_account_profile(cookie):

    if not cookie:

        raise ValueError("汽水音乐会话中缺少 sessionid，请重新扫码授权")

    try:

        profile = qishui_me(cookie)

    except QishuiError as exc:

        raise ValueError(str(exc)) from exc

    sessionid = parse_cookie_kv(cookie, "sessionid")

    return str(profile.get("id") or sessionid[:12] or "qishui-user"), str(profile.get("nickname") or "汽水用户")





def _qishui_account_playlists(account):

    cookie = _account_cookie(account)

    try:

        return qishui_playlists(cookie)

    except QishuiError as exc:

        raise ValueError(str(exc)) from exc





def _get_qishui_account_playlist(playlist_id, cookie):

    try:

        return qishui_playlist_detail(cookie, playlist_id)

    except QishuiError as exc:

        raise ValueError(str(exc)) from exc





def _sync_account_playlists(user, account):

    platform = account.get("platform")

    if platform == "netease":

        remote_playlists = _netease_account_playlists(account)

        reader = lambda remote_id: _get_netease_playlist(remote_id, _account_cookie(account))

    elif platform == "qq":

        remote_playlists = _qq_account_playlists(account)

        reader = lambda remote_id: _get_qq_playlist(remote_id, _account_cookie(account))

    elif platform == "kugou":

        remote_playlists = _kugou_account_playlists(account)

        reader = lambda remote_id: _get_kugou_account_playlist(remote_id, _account_cookie(account))

    elif platform == "qishui":

        remote_playlists = _qishui_account_playlists(account)

        reader = lambda remote_id: _get_qishui_account_playlist(remote_id, _account_cookie(account))

    else:

        raise ValueError("该平台的私有歌单同步接口暂未验证，请等待平台连接器更新")

    if not remote_playlists:

        raise ValueError("未读取到歌单。请确认 Cookie 仍有效，并在第三方平台创建至少一个歌单")



    data = load_playlists(user["id"])

    now = datetime.now().isoformat()

    synced = 0

    total_songs = 0

    for remote_id, fallback_name in remote_playlists:

        try:

            remote_name, songs = reader(remote_id)

        except ValueError:

            continue

        if platform == "kugou" and str(remote_id).startswith("cloudlist:"):

            remote_name = fallback_name or remote_name

        playlist = next((item for item in data["playlists"] if item.get("account_id") == account["id"] and str(item.get("remote_id")) == str(remote_id)), None)

        if not playlist:

            playlist = {

                "id": uuid.uuid4().hex[:12], "name": _unique_playlist_name(data["playlists"], remote_name or fallback_name, platform),

                "user_id": user["id"], "created_at": now, "songs": [], "remote_id": str(remote_id),

                "remote_platform": platform, "account_id": account["id"], "account_platform": platform,

                "account_name": account.get("nickname") or fallback_name, "sync_enabled": True,

            }

            data["playlists"].append(playlist)

        normalized = []

        for song in songs:

            song = dict(song)

            song.update({"origin_platform": platform, "origin_account_id": account["id"], "from_synced_account": True})

            normalized.append(song)

        playlist.update({

            "name": remote_name or playlist["name"], "songs": normalized, "updated_at": now,

            "last_synced_at": now, "account_platform": platform, "account_name": account.get("nickname") or fallback_name,

        })

        synced += 1

        total_songs += len(normalized)

    if not synced:

        raise ValueError("已连接账户，但暂未读取到可同步的歌单。请确认歌单未被设为私密，或稍后重试")

    save_playlists(data)

    account["last_synced_at"] = now

    account["status"] = "active"

    account["status_message"] = f"已同步 {synced} 个歌单、{total_songs} 首歌曲"

    account["updated_at"] = now

    return synced, total_songs





def _unique_playlist_name(playlists, desired_name, platform):

    existing_names = {str(item.get("name") or "") for item in playlists}

    if desired_name not in existing_names:

        return desired_name

    source_names = {

        "netease": "网易云",

        "qq": "QQ",

        "kugou": "酷狗",

        "qishui": "汽水",

        "kuwo": "酷我",

    }

    suffix = f"（{source_names.get(platform, '音乐')}导入）"

    candidate = f"{desired_name[:60 - len(suffix)]}{suffix}"

    index = 2

    while candidate in existing_names:

        numbered_suffix = f"{suffix[:-1]} {index}）"

        candidate = f"{desired_name[:60 - len(numbered_suffix)]}{numbered_suffix}"

        index += 1

    return candidate





@app.get("/login", response_class=HTMLResponse)

@app.get("/register", response_class=HTMLResponse)

async def auth_page():

    return FileResponse(STATIC_DIR / "auth.html", media_type="text/html")





@app.get("/api/auth/me")

async def auth_me(request: Request):

    user = _session_user(request)

    if user:

        users_data = load_users()

        stored_user = next(

            (item for item in users_data.get("users", []) if str(item.get("id")) == str(user.get("id"))),

            None,

        )

        if stored_user:

            stored_user["last_login_at"] = datetime.now().isoformat()

            stored_user["last_login_ip"] = _client_ip(request)

            save_users(users_data)

    return JSONResponse({"code": 0, "user": _public_user(user) if user else None})



@app.post("/api/presence/heartbeat")

async def online_presence_heartbeat(request: Request):

    user = _require_user(request)

    try:

        body = await request.json()

    except ValueError:

        body = {}

    client_id = _online_presence_client_id(body.get("client_id"))

    now = time.monotonic()

    with ONLINE_PRESENCE_LOCK:

        _online_presence_cleanup(now)

        ONLINE_PRESENCE[client_id] = {"user_id": str(user["id"]), "last_seen": now}

        online = _online_presence_count()

    return JSONResponse({"code": 0, "online": online, "timeout_seconds": ONLINE_PRESENCE_TTL})



@app.post("/api/presence/leave")

async def online_presence_leave(request: Request, client_id: str = Query("")):

    user = _require_user(request)

    client_id = _online_presence_client_id(client_id)

    now = time.monotonic()

    with ONLINE_PRESENCE_LOCK:

        presence = ONLINE_PRESENCE.get(client_id)

        if presence and str(presence.get("user_id")) == str(user["id"]):

            ONLINE_PRESENCE.pop(client_id, None)

        _online_presence_cleanup(now)

        online = _online_presence_count()

    return JSONResponse({"code": 0, "online": online})





@app.post("/api/auth/register")

async def auth_register(request: Request):

    try:

        body = await request.json()

    except ValueError:

        return JSONResponse({"code": -1, "msg": "注册信息格式不正确"})

    username = str(body.get("username") or "")

    password = str(body.get("password") or "")

    display_name = str(body.get("display_name") or username).strip()[:40] or username

    if not re.fullmatch(r"[A-Za-z0-9]{8,32}", username):

        return JSONResponse({"code": -1, "msg": "用户名需为 8-32 位英文字母或数字，区分大小写"})

    if len(password) < 8 or len(password) > 128:

        return JSONResponse({"code": -1, "msg": "密码长度需为 8-128 位"})

    data = load_users()

    registration_config = load_config()
    if data["users"] and not registration_config.get("allow_registration", True):
        return JSONResponse({"code": -1, "msg": "当前已关闭新用户注册"}, status_code=403)

    if any(item.get("username") == username for item in data["users"]):

        return JSONResponse({"code": -1, "msg": "用户名已存在"})

    first_user = not data["users"]

    user = {

        "id": uuid.uuid4().hex[:16],

        "username": username,

        "display_name": display_name,

        "password_hash": _hash_password(password),

        "role": "admin" if first_user else "user",

        "created_at": datetime.now().isoformat(),

        "last_login_at": datetime.now().isoformat(),

        "last_login_ip": _client_ip(request),

    }

    data["users"].append(user)

    save_users(data)

    if first_user:

        _migrate_legacy_playlists(user["id"])

    token = secrets.token_urlsafe(32)

    SESSIONS[token] = {"user_id": user["id"], "expires_at": datetime.now() + SESSION_TTL}

    _save_sessions()

    response = JSONResponse({

        "code": 0,

        "msg": "注册成功，首个账号已成为管理员" if first_user else "注册成功",

        "user": _public_user(user),

    })

    response.set_cookie(SESSION_COOKIE, token, max_age=int(SESSION_TTL.total_seconds()), httponly=True, samesite="lax", secure=request.url.scheme == "https")

    return response





@app.post("/api/auth/login")

async def auth_login(request: Request):

    try:

        body = await request.json()

    except ValueError:

        return JSONResponse({"code": -1, "msg": "登录信息格式不正确"})

    username = str(body.get("username") or "")

    password = str(body.get("password") or "")

    users_data = load_users()

    user = next((item for item in users_data["users"] if item.get("username") == username), None)

    if not user or not _check_password(password, user.get("password_hash", "")):

        return JSONResponse({"code": -1, "msg": "用户名或密码错误"}, status_code=401)

    if user.get("banned"):

        return JSONResponse({"code": -1, "msg": "该账号已被管理员封禁"}, status_code=403)

    user["last_login_at"] = datetime.now().isoformat()

    user["last_login_ip"] = _client_ip(request)

    save_users(users_data)

    token = secrets.token_urlsafe(32)

    SESSIONS[token] = {"user_id": user["id"], "expires_at": datetime.now() + SESSION_TTL}

    _save_sessions()

    response = JSONResponse({"code": 0, "user": _public_user(user)})

    response.set_cookie(SESSION_COOKIE, token, max_age=int(SESSION_TTL.total_seconds()), httponly=True, samesite="lax", secure=request.url.scheme == "https")

    return response





@app.post("/api/auth/logout")

async def auth_logout(request: Request):

    token = request.cookies.get(SESSION_COOKIE)

    if token:

        SESSIONS.pop(token, None)

        _save_sessions()

    response = JSONResponse({"code": 0})

    response.delete_cookie(SESSION_COOKIE)

    return response







# ==================== 第三方账户与歌单同步 ====================



ACCOUNT_PLATFORM_NAMES = {"netease": "网易云音乐", "qq": "QQ 音乐", "kugou": "酷狗音乐", "qishui": "汽水音乐"}





def _qr_cleanup(session_store, session_lock):

    now = datetime.now()

    with session_lock:

        for token, session in list(session_store.items()):

            if session.get("expires_at") <= now:

                session_store.pop(token, None)





def _account_qr_cleanup():

    _qr_cleanup(ACCOUNT_QR_SESSIONS, ACCOUNT_QR_LOCK)





def _admin_qr_cleanup():

    _qr_cleanup(ADMIN_QR_SESSIONS, ADMIN_QR_LOCK)





def _cookie_from_response(response):

    return "; ".join(f"{name}={value}" for name, value in response.cookies.items())





def _upsert_connected_account(user, platform, platform_user_id, nickname, cookie, auth_type, status_message):

    data = load_connected_accounts(user["id"])

    now = datetime.now().isoformat()

    account = next(

        (item for item in data["accounts"] if item.get("platform") == platform and str(item.get("platform_user_id") or "") == str(platform_user_id)),

        None,

    )

    if not account:

        account = {"id": uuid.uuid4().hex[:12], "user_id": user["id"], "platform": platform, "created_at": now}

        data["accounts"].append(account)

    account.update({

        "nickname": nickname,

        "platform_user_id": str(platform_user_id),

        "auth_type": auth_type,

        "credential_encrypted": _encrypt_credential({"cookie": cookie}),

        "status": "active" if platform in ("netease", "qq", "kugou", "qishui") else "unsupported",

        "status_message": status_message,

        "updated_at": now,

    })

    save_connected_accounts(data, user["id"])

    return account





def _netease_qr_adapter_ready():

    """Start the maintained local QR adapter only when NetEase authorization is used."""

    global NCM_QR_ADAPTER_PROCESS

    try:

        requests.get(f"{NCM_QR_ADAPTER_URL}/", timeout=0.8)

        return

    except requests.RequestException:

        pass



    with NCM_QR_ADAPTER_LOCK:

        try:

            requests.get(f"{NCM_QR_ADAPTER_URL}/", timeout=0.8)

            return

        except requests.RequestException:

            pass

        if not NCM_QR_ADAPTER_APP.exists():

            raise ValueError("网易云二维码适配器未安装，请在项目目录执行 npm install --prefix .ncm-qr-adapter NeteaseCloudMusicApi@4.32.0")

        node = shutil.which("node") or shutil.which("node.exe")

        if not node:

            raise ValueError("未检测到 Node.js，无法启动网易云二维码登录适配器")

        env = os.environ.copy()

        env["PORT"] = str(NCM_QR_ADAPTER_PORT)

        env["HOST"] = "127.0.0.1"

        NCM_QR_ADAPTER_PROCESS = subprocess.Popen(

            [node, str(NCM_QR_ADAPTER_APP)],

            cwd=str(Path(__file__).parent),

            env=env,

            stdout=subprocess.DEVNULL,

            stderr=subprocess.DEVNULL,

        )

        deadline = time.time() + 20

        while time.time() < deadline:

            try:

                requests.get(f"{NCM_QR_ADAPTER_URL}/", timeout=0.8)

                return

            except requests.RequestException:

                time.sleep(0.25)

        raise ValueError("网易云二维码适配器启动超时")





def _netease_qr_adapter_request(path, params):

    _netease_qr_adapter_ready()

    response = requests.get(f"{NCM_QR_ADAPTER_URL}{path}", params=params, timeout=20)

    try:

        payload = response.json()

    except ValueError:

        payload = {}

    if response.status_code != 200:

        raise ValueError(payload.get("msg") or f"网易云二维码服务请求失败（HTTP {response.status_code}）")

    return payload





def _start_netease_qr_login(user, session_store=None, session_lock=None, owner_id=None, admin_mode=False):

    timestamp = int(time.time() * 1000)

    key_payload = _netease_qr_adapter_request("/login/qr/key", {"timestamp": timestamp, "noCookie": "true"})

    key = str((key_payload.get("data") or {}).get("unikey") or "")

    if key_payload.get("code") != 200 or not key:

        raise ValueError(key_payload.get("message") or key_payload.get("msg") or "无法创建网易云二维码")

    image_payload = _netease_qr_adapter_request("/login/qr/create", {

        "key": key,

        "platform": "web",

        "qrimg": "true",

        "timestamp": timestamp,

        "noCookie": "true",

    })

    qr_image = str(((image_payload.get("data") or {}).get("qrimg")) or "")

    if image_payload.get("code") != 200 or not qr_image.startswith("data:image/"):

        raise ValueError(image_payload.get("message") or image_payload.get("msg") or "无法生成网易云二维码")

    token = secrets.token_urlsafe(24)

    session_store = ACCOUNT_QR_SESSIONS if session_store is None else session_store

    session_lock = session_lock or ACCOUNT_QR_LOCK

    owner_id = user["id"] if owner_id is None else owner_id

    _qr_cleanup(session_store, session_lock)

    with session_lock:

        session_store[token] = {

            "user_id": owner_id,

            "platform": "netease",

            "key": key,

            "expires_at": datetime.now() + ACCOUNT_QR_TTL,

        }

    return {"token": token, "qr_image": qr_image, "expires_in": int(ACCOUNT_QR_TTL.total_seconds())}





def _poll_netease_qr_login(user, token, session_store=None, session_lock=None, owner_id=None, admin_mode=False):

    session_store = ACCOUNT_QR_SESSIONS if session_store is None else session_store

    session_lock = session_lock or ACCOUNT_QR_LOCK

    owner_id = user["id"] if owner_id is None else owner_id

    _qr_cleanup(session_store, session_lock)

    with session_lock:

        session = session_store.get(token)

    if not session or session.get("user_id") != owner_id or session.get("platform") != "netease":

        raise ValueError("二维码登录会话不存在或已过期")

    payload = _netease_qr_adapter_request("/login/qr/check", {

        "key": session["key"],

        "timestamp": int(time.time() * 1000),

        "noCookie": "true",

    })

    code = int(payload.get("code") or -1)

    if code in (801, 802):

        return {"status": "waiting", "message": payload.get("message") or "等待扫码确认"}

    if code == 803:

        cookie = str(payload.get("cookie") or "").strip()

        if not cookie:

            raise ValueError("网易云未返回授权会话，请重新扫码")

        platform_user_id, nickname = _netease_account_profile(cookie)

        with session_lock:

            session_store.pop(token, None)

        if admin_mode:

            return {"status": "authorized", "cookie": cookie, "platform_user_id": platform_user_id, "nickname": nickname}

        account = _upsert_connected_account(user, "netease", platform_user_id, nickname, cookie, "qr", "\u4e8c\u7ef4\u7801\u6388\u6743\u5b8c\u6210\uff0c\u53ef\u4ee5\u540c\u6b65\u6b4c\u5355")

        return {"status": "authorized", "account": _public_connected_account(account)}

    if code == 800:

        with session_lock:

            session_store.pop(token, None)

        return {"status": "expired", "message": payload.get("message") or "二维码已过期"}

    return {"status": "waiting", "message": payload.get("message") or "等待二维码授权"}





def _qq_qr_token(qrsig):

    token = 0

    for char in str(qrsig or ""):

        token += (token << 5) + ord(char)

    return token & 0x7FFFFFFF





def _qq_cookie_pairs(response):

    pairs = []

    try:

        raw_values = response.raw.headers.get_all("Set-Cookie") or []

    except (AttributeError, TypeError):

        raw_values = []

    for raw in raw_values:

        match = re.match(r"\s*([^=;]+)=([^;]*)", str(raw))

        if match and match.group(2):

            pairs.append(f"{match.group(1)}={match.group(2)}")

    if not pairs:

        pairs = [f"{name}={value}" for name, value in response.cookies.items() if value]

    return pairs





def _qq_merge_cookie_pairs(current, response):

    values = {}

    for pair in current or []:

        name, separator, value = str(pair).partition("=")

        if separator and value:

            values[name.strip()] = f"{name.strip()}={value.strip()}"

    for pair in _qq_cookie_pairs(response):

        name, _, _ = pair.partition("=")

        values[name] = pair

    return list(values.values())





def _qq_session_cookie(session):

    values = {}

    for pair in session.get("cookies") or []:

        name, separator, value = str(pair).partition("=")

        if separator and value:

            values[name.strip()] = f"{name.strip()}={value.strip()}"

    if session.get("qrsig"):

        values["qrsig"] = f"qrsig={session['qrsig']}"

    return "; ".join(values.values())





def _qq_login_callback_url(text):

    match = re.search(r'''ptuiCB\([^)]*?['"](https?://[^'"]+)['"]''', str(text or ""))

    return match.group(1) if match else ""





def _qq_login_headers(cookie):

    return {

        "User-Agent": QQ_SESSION.headers["User-Agent"],

        "Referer": "https://y.qq.com/",

        "Origin": "https://y.qq.com",

        "Cookie": cookie,

    }





def _start_qq_qr_login(user, session_store=None, session_lock=None, owner_id=None, admin_mode=False):

    response = requests.get(

        "https://ssl.ptlogin2.qq.com/ptqrshow",

        params={

            "appid": "716027609", "e": "2", "l": "M", "s": "3", "d": "72", "v": "4",

            "t": f"{time.time():.12f}", "daid": "383", "pt_3rd_aid": "100497308",

            "u1": "https://graph.qq.com/oauth2.0/login_jump",

        },

        headers={"User-Agent": QQ_SESSION.headers["User-Agent"], "Referer": "https://y.qq.com/"},

        timeout=20,

    )

    qrsig = response.cookies.get("qrsig") or parse_cookie_kv(response.headers.get("Set-Cookie", ""), "qrsig")

    if response.status_code != 200 or not qrsig or not response.content.startswith(b"\x89PNG"):

        raise ValueError("\u65e0\u6cd5\u751f\u6210 QQ \u97f3\u4e50\u4e8c\u7ef4\u7801\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5")

    token = secrets.token_urlsafe(24)

    session_store = ACCOUNT_QR_SESSIONS if session_store is None else session_store

    session_lock = session_lock or ACCOUNT_QR_LOCK

    owner_id = user["id"] if owner_id is None else owner_id

    _qr_cleanup(session_store, session_lock)

    with session_lock:

        session_store[token] = {

            "user_id": owner_id, "platform": "qq", "qrsig": qrsig,

            "cookies": _qq_cookie_pairs(response), "expires_at": datetime.now() + ACCOUNT_QR_TTL,

        }

    return {

        "token": token,

        "qr_image": "data:image/png;base64," + base64.b64encode(response.content).decode("ascii"),

        "expires_in": int(ACCOUNT_QR_TTL.total_seconds()),

    }





def _poll_qq_qr_login(user, token, session_store=None, session_lock=None, owner_id=None, admin_mode=False):

    session_store = ACCOUNT_QR_SESSIONS if session_store is None else session_store

    session_lock = session_lock or ACCOUNT_QR_LOCK

    owner_id = user["id"] if owner_id is None else owner_id

    _qr_cleanup(session_store, session_lock)

    with session_lock:

        session = session_store.get(token)

    if not session or session.get("user_id") != owner_id or session.get("platform") != "qq":

        raise ValueError("\u4e8c\u7ef4\u7801\u767b\u5f55\u4f1a\u8bdd\u4e0d\u5b58\u5728\u6216\u5df2\u8fc7\u671f")



    response = requests.get(

        "https://ssl.ptlogin2.qq.com/ptqrlogin",

        params={

            "u1": "https://graph.qq.com/oauth2.0/login_jump", "ptqrtoken": _qq_qr_token(session["qrsig"]),

            "ptredirect": "0", "h": "1", "t": "1", "g": "1", "from_ui": "1", "ptlang": "2052",

            "action": f"0-0-{int(time.time() * 1000)}", "js_ver": "23111510", "js_type": "1",

            "pt_uistyle": "40", "aid": "716027609", "daid": "383", "pt_3rd_aid": "100497308",

            "o1vId": "3674fc47871e9c407d8838690b355408", "pt_js_version": "v1.48.1",

        },

        headers=_qq_login_headers(_qq_session_cookie(session)),

        timeout=20,

    )

    text = response.text or ""

    session["cookies"] = _qq_merge_cookie_pairs(session.get("cookies"), response)

    if "\u4e8c\u7ef4\u7801\u5df2\u5931\u6548" in text or "\u4e8c\u7ef4\u7801\u5931\u6548" in text:

        with session_lock:

            session_store.pop(token, None)

        return {"status": "expired", "message": "\u4e8c\u7ef4\u7801\u5df2\u8fc7\u671f"}

    if "\u767b\u5f55\u6210\u529f" not in text:

        return {"status": "waiting", "message": "\u8bf7\u4f7f\u7528 QQ \u626b\u7801\u5e76\u5728\u624b\u673a\u4e0a\u786e\u8ba4\u6388\u6743"}



    callback_url = _qq_login_callback_url(text)

    if not callback_url:

        raise ValueError("QQ \u6388\u6743\u56de\u8c03\u5730\u5740\u7f3a\u5931\uff0c\u8bf7\u5237\u65b0\u4e8c\u7ef4\u7801\u540e\u91cd\u8bd5")

    callback = requests.get(callback_url, headers=_qq_login_headers(_qq_session_cookie(session)), timeout=20, allow_redirects=False)

    session["cookies"] = _qq_merge_cookie_pairs(session.get("cookies"), callback)

    cookie = _qq_session_cookie(session)

    p_skey = parse_cookie_kv(cookie, "p_skey")

    if not p_skey:

        raise ValueError("QQ \u6388\u6743\u4f1a\u8bdd\u4e0d\u5b8c\u6574\uff0c\u8bf7\u5237\u65b0\u4e8c\u7ef4\u7801\u540e\u91cd\u8bd5")



    g_tk = qq_get_gtk(cookie)

    authorize = requests.post(

        "https://graph.qq.com/oauth2.0/authorize",

        data={

            "response_type": "code", "client_id": "100497308",

            "redirect_uri": "https://y.qq.com/portal/wx_redirect.html?login_type=1&surl=https://y.qq.com/",

            "scope": "get_user_info,get_app_friends", "state": "state", "switch": "", "from_ptlogin": "1",

            "src": "1", "update_auth": "1", "openapi": "1010_1030", "g_tk": str(g_tk),

            "auth_time": datetime.now().ctime(), "ui": uuid.uuid4().hex.upper(),

        },

        headers=_qq_login_headers(cookie), timeout=20, allow_redirects=False,

    )

    session["cookies"] = _qq_merge_cookie_pairs(session.get("cookies"), authorize)

    location = authorize.headers.get("Location", "")

    code_match = re.search(r"[?&]code=([^&]+)", location)

    if authorize.status_code not in range(300, 400) or not code_match:

        raise ValueError("QQ \u6388\u6743\u4ee4\u724c\u83b7\u53d6\u5931\u8d25\uff0c\u8bf7\u91cd\u65b0\u626b\u7801")



    login_payload = {"comm": {"g_tk": g_tk, "platform": "yqq", "ct": 24, "cv": 0}, "req": {"module": "QQConnectLogin.LoginServer", "method": "QQLogin", "param": {"code": code_match.group(1)}}}

    music_login = requests.post(

        "https://u.y.qq.com/cgi-bin/musicu.fcg", data=json.dumps(login_payload, separators=(",", ":")),

        headers={**_qq_login_headers(_qq_session_cookie(session)), "Content-Type": "application/x-www-form-urlencoded"}, timeout=20,

    )

    session["cookies"] = _qq_merge_cookie_pairs(session.get("cookies"), music_login)

    cookie = _qq_session_cookie(session)

    fields = extract_qq_cookie_fields(cookie)

    if not fields.get("qq_uin"):

        raise ValueError("QQ \u5df2\u786e\u8ba4\u6388\u6743\uff0c\u4f46\u672a\u53d6\u5f97 QQ \u97f3\u4e50\u4f1a\u8bdd\uff0c\u8bf7\u91cd\u65b0\u626b\u7801")

    platform_user_id, nickname = _qq_account_profile(cookie)

    with session_lock:

        session_store.pop(token, None)

    if admin_mode:

        return {"status": "authorized", "cookie": cookie, "platform_user_id": platform_user_id, "nickname": nickname}

    account = _upsert_connected_account(user, "qq", platform_user_id, nickname, cookie, "qr", "\u4e8c\u7ef4\u7801\u6388\u6743\u5b8c\u6210\uff0c\u53ef\u4ee5\u540c\u6b65 QQ \u97f3\u4e50\u6b4c\u5355")

    return {"status": "authorized", "account": _public_connected_account(account)}







def _kugou_login_headers(device, clienttime):

    return {

        "User-Agent": "Android15-1070-11083-46-0-DiscoveryDRADProtocol-wifi",

        "dfid": device.get("dfid") or "-", "clienttime": clienttime, "mid": device["mid"],

        "kg-rc": "1", "kg-thash": "5d816a0", "kg-rec": "1", "kg-rf": "B9EDA08A64250DEFFBCADDEE00F8F25F",

        "Cookie": "; ".join([

            f"KUGOU_API_GUID={device['guid']}", f"KUGOU_API_MID={device['mid']}",

            f"KUGOU_API_MAC={device['mac']}", f"KUGOU_API_DEV={device['dev']}",

        ]),

    }





def _kugou_login_get(url, request_params, device):

    clienttime = str(int(time.time()))

    params = {"dfid": device.get("dfid") or "-", "mid": device["mid"], "uuid": "-", "appid": "3116", "clientver": "11440", "clienttime": clienttime}

    params.update(request_params)

    params["signature"] = _kugou_login_signed_params(params)

    response = requests.get(url, params=params, headers=_kugou_login_headers(device, clienttime), timeout=20)

    return response, (response.json() if response.ok else {})





def _start_kugou_qr_login(user, session_store=None, session_lock=None, owner_id=None, admin_mode=False):

    device = _kugou_device()

    response, result = _kugou_login_get(

        "https://login-user.kugou.com/v2/qrcode",

        {"appid": "1001", "type": "1", "plat": "4", "qrcode_txt": "https://h5.kugou.com/apps/loginQRCode/html/index.html?appid=3116&", "srcappid": "2919"},

        device,

    )

    data = result.get("data") if isinstance(result, dict) else {}

    qr_code = str((data or {}).get("qrcode") or "")

    qr_image = str((data or {}).get("qrcode_img") or "")

    if not qr_code or not qr_image.startswith("data:image/"):

        message = (result.get("msg") or result.get("error") or "Unable to create a Kugou Music QR code") if isinstance(result, dict) else "Unable to create a Kugou Music QR code"

        raise ValueError(message)

    token = secrets.token_urlsafe(24)

    session_store = ACCOUNT_QR_SESSIONS if session_store is None else session_store

    session_lock = session_lock or ACCOUNT_QR_LOCK

    owner_id = user["id"] if owner_id is None else owner_id

    _qr_cleanup(session_store, session_lock)

    with session_lock:

        session_store[token] = {"user_id": owner_id, "platform": "kugou", "qrcode": qr_code, "device": device, "expires_at": datetime.now() + ACCOUNT_QR_TTL}

    return {"token": token, "qr_image": qr_image, "expires_in": int(ACCOUNT_QR_TTL.total_seconds())}





def _poll_kugou_qr_login(user, token, session_store=None, session_lock=None, owner_id=None, admin_mode=False):

    session_store = ACCOUNT_QR_SESSIONS if session_store is None else session_store

    session_lock = session_lock or ACCOUNT_QR_LOCK

    owner_id = user["id"] if owner_id is None else owner_id

    _qr_cleanup(session_store, session_lock)

    with session_lock:

        session = session_store.get(token)

    if not session or session.get("user_id") != owner_id or session.get("platform") != "kugou":

        raise ValueError("QR login session does not exist or has expired")

    device = session["device"]

    response, result = _kugou_login_get(

        "https://login-user.kugou.com/v2/get_userinfo_qrcode",

        {"plat": "4", "appid": "3116", "srcappid": "2919", "qrcode": session["qrcode"]},

        device,

    )

    data = result.get("data") if isinstance(result, dict) else {}

    try:

        status = int((data or {}).get("status") if (data or {}).get("status") is not None else result.get("status", 0))

    except (TypeError, ValueError):

        status = 0

    if status in (-1, 5, 6):

        with session_lock:

            session_store.pop(token, None)

        return {"status": "expired", "message": "QR code has expired"}

    if status in (0, 1):

        return {"status": "waiting", "message": "Waiting for a Kugou Music QR scan"}

    if status in (2, 3):

        return {"status": "scanned", "message": "QR code scanned. Please confirm the authorization in Kugou Music."}

    if status != 4:

        detail = str((result or {}).get("error") or (result or {}).get("msg") or "") if isinstance(result, dict) else ""

        return {"status": "failed", "message": f"Kugou authorization could not be completed (status {status}){': ' + detail if detail else ''}"}

    user_data = (data or {}).get("userinfo") or data or {}

    user_id = str(user_data.get("userid") or user_data.get("user_id") or "")

    session_token = str(user_data.get("token") or "")

    if not user_id or not session_token:

        return {"status": "failed", "message": "Kugou confirmed the QR code but did not return a usable login session. Please refresh the QR code and try again."}

    cookie = "; ".join([f"token={session_token}", f"userid={user_id}", f"dfid={device['dfid']}", f"KUGOU_API_GUID={device['guid']}", f"KUGOU_API_MID={device['mid']}", f"KUGOU_API_MAC={device['mac']}", f"KUGOU_API_DEV={device['dev']}"])

    nickname = str(user_data.get("nickname") or user_data.get("username") or f"Kugou user {user_id}")

    with session_lock:

        session_store.pop(token, None)

    if admin_mode:

        return {"status": "authorized", "cookie": cookie, "platform_user_id": user_id, "nickname": nickname}

    account = _upsert_connected_account(user, "kugou", user_id, nickname, cookie, "qr", "QR authorization completed; playlists can now be synchronized")

    return {"status": "authorized", "account": _public_connected_account(account)}





def _start_kugou_concept_qr_login(user, session_store=None, session_lock=None, owner_id=None, admin_mode=False):

    """Create an isolated administrator QR session for Kugou Concept Edition."""

    device = _kugou_device()

    response, result = _kugou_login_get(

        "https://login-user.kugou.com/v2/qrcode",

        {"appid": "1001", "type": "1", "plat": "4", "qrcode_txt": "https://h5.kugou.com/apps/loginQRCode/html/index.html?appid=3116&", "srcappid": "2919"},

        device,

    )

    data = result.get("data") if isinstance(result, dict) else {}

    qr_code = str((data or {}).get("qrcode") or "")

    qr_image = str((data or {}).get("qrcode_img") or "")

    if not qr_code or not qr_image.startswith("data:image/"):

        message = (result.get("msg") or result.get("error") or "Unable to create a Kugou Concept Edition QR code") if isinstance(result, dict) else "Unable to create a Kugou Concept Edition QR code"

        raise ValueError(message)

    token = secrets.token_urlsafe(24)

    session_store = ACCOUNT_QR_SESSIONS if session_store is None else session_store

    session_lock = session_lock or ACCOUNT_QR_LOCK

    owner_id = user["id"] if owner_id is None else owner_id

    _qr_cleanup(session_store, session_lock)

    with session_lock:

        session_store[token] = {"user_id": owner_id, "platform": "kugou_concept", "qrcode": qr_code, "device": device, "expires_at": datetime.now() + ACCOUNT_QR_TTL}

    return {"token": token, "qr_image": qr_image, "expires_in": int(ACCOUNT_QR_TTL.total_seconds())}





def _poll_kugou_concept_qr_login(user, token, session_store=None, session_lock=None, owner_id=None, admin_mode=False):

    session_store = ACCOUNT_QR_SESSIONS if session_store is None else session_store

    session_lock = session_lock or ACCOUNT_QR_LOCK

    owner_id = user["id"] if owner_id is None else owner_id

    _qr_cleanup(session_store, session_lock)

    with session_lock:

        session = session_store.get(token)

    if not session or session.get("user_id") != owner_id or session.get("platform") != "kugou_concept":

        raise ValueError("QR login session does not exist or has expired")

    device = session["device"]

    _response, result = _kugou_login_get(

        "https://login-user.kugou.com/v2/get_userinfo_qrcode",

        {"plat": "4", "appid": "3116", "srcappid": "2919", "qrcode": session["qrcode"]},

        device,

    )

    data = result.get("data") if isinstance(result, dict) else {}

    try:

        status = int((data or {}).get("status") if (data or {}).get("status") is not None else result.get("status", 0))

    except (TypeError, ValueError):

        status = 0

    if status in (-1, 5, 6):

        with session_lock:

            session_store.pop(token, None)

        return {"status": "expired", "message": "QR code has expired"}

    if status in (0, 1):

        return {"status": "waiting", "message": "Waiting for a Kugou Concept Edition QR scan"}

    if status in (2, 3):

        return {"status": "scanned", "message": "QR code scanned. Please confirm the authorization in Kugou Concept Edition."}

    if status != 4:

        detail = str((result or {}).get("error") or (result or {}).get("msg") or "") if isinstance(result, dict) else ""

        return {"status": "failed", "message": f"Kugou Concept authorization could not be completed (status {status}){': ' + detail if detail else ''}"}

    user_data = (data or {}).get("userinfo") or data or {}

    user_id = str(user_data.get("userid") or user_data.get("user_id") or "")

    session_token = str(user_data.get("token") or "")

    if not user_id or not session_token:

        return {"status": "failed", "message": "Kugou Concept Edition confirmed the QR code but did not return a usable login session. Please refresh the QR code and try again."}

    cookie = "; ".join([f"token={session_token}", f"userid={user_id}", f"dfid={device['dfid']}", f"KUGOU_API_GUID={device['guid']}", f"KUGOU_API_MID={device['mid']}", f"KUGOU_API_MAC={device['mac']}", f"KUGOU_API_DEV={device['dev']}"])

    nickname = str(user_data.get("nickname") or user_data.get("username") or f"Kugou Concept user {user_id}")

    with session_lock:

        session_store.pop(token, None)

    return {"status": "authorized", "cookie": cookie, "platform_user_id": user_id, "nickname": nickname}





# ==================== Migu QR Login ====================



MIGU_QR_APPID = "8000"

MIGU_QR_SOURCE_ID = "220001"



def _start_migu_qr_login(user, session_store=None, session_lock=None, owner_id=None, admin_mode=False):

    """Generate a Migu Music QR code via the passport web gateway."""

    session_store = ACCOUNT_QR_SESSIONS if session_store is None else session_store

    session_lock = session_lock or ACCOUNT_QR_LOCK

    owner_id = user["id"] if owner_id is None else owner_id



    migu_session = requests.Session()

    migu_session.headers.update({

        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",

        "Referer": "https://passport.migu.cn/login",

    })

    # Visit login page to obtain session cookies

    migu_session.get("https://passport.migu.cn/login?callbackURL=https%3A%2F%2Fmusic.migu.cn%2Fv3&sourceID=100001", timeout=20)



    # Create QR code (note: parameter must be lowercase 'sourceid')

    response = migu_session.get(

        "https://passport.migu.cn/api/qrcWeb/qrcLogin",

        params={"sourceid": "100001"},

        timeout=20,

    )

    data = response.json()

    if int(data.get("status", 0)) != 2000:

        raise ValueError(data.get("message") or "咪咕二维码生成失败，请稍后重试")



    result = data.get("result") or {}

    qr_image = result.get("qrcUrl", "")

    qrc_session_id = result.get("qrc_sessionid", "")

    if not qr_image or not qr_image.startswith("data:image/"):

        raise ValueError("咪咕二维码图片未返回，请稍后重试")



    # Persist the actual requests.Session object so polling reuses the same

    # server-side session and TCP connection, which Migu requires.

    local_token = secrets.token_urlsafe(24)

    _qr_cleanup(session_store, session_lock)

    with session_lock:

        session_store[local_token] = {

            "user_id": owner_id,

            "platform": "migu",

            "migu_session": migu_session,

            "qrc_session_id": qrc_session_id,

            "created_at": datetime.now(),

            "expires_at": datetime.now() + ACCOUNT_QR_TTL,

        }

    return {"token": local_token, "qr_image": qr_image, "expires_in": int(ACCOUNT_QR_TTL.total_seconds()), "scan_app": "migu", "message": "请使用咪咕音乐 App 扫码确认"}





def _poll_migu_qr_login(user, token, session_store=None, session_lock=None, owner_id=None, admin_mode=False):

    """Poll Migu passport QR authorization status reusing the original session."""

    session_store = ACCOUNT_QR_SESSIONS if session_store is None else session_store

    session_lock = session_lock or ACCOUNT_QR_LOCK

    owner_id = user["id"] if owner_id is None else owner_id

    with session_lock:

        session = session_store.get(token)

    if not session or session.get("user_id") != owner_id or session.get("platform") != "migu":

        raise ValueError("二维码会话不存在或已过期，请刷新二维码")



    migu_session = session.get("migu_session")

    if not migu_session:

        raise ValueError("二维码会话已失效，请刷新二维码")



    response = migu_session.get("https://passport.migu.cn/api/qrcWeb/qrcquery", timeout=20)

    data = response.json()

    status_code = int(data.get("status", 0))



    # 4072 = Migu says "timeout" but this fires immediately and throughout

    # the waiting period. Only truly expire after our own TTL (5 minutes).

    if status_code == 4072:

        if datetime.now() > session.get("expires_at", datetime.now()):

            with session_lock:

                session_store.pop(token, None)

            return {"status": "expired", "message": "二维码已过期，请点击刷新二维码"}

        return {"status": "waiting", "message": "请使用咪咕音乐 App 扫码"}

    if status_code == 4071:

        return {"status": "waiting", "message": "请使用咪咕音乐 App 扫码"}

    if status_code == 4066:

        return {"status": "scanned", "message": "已扫码，请在手机上确认授权"}

    if status_code != 2000:

        return {"status": "waiting", "message": data.get("message") or "等待扫码授权"}



    # Success (status 2000)!

    logger.info("Migu QR authorized! result_keys=%s", list((data.get("result") or {}).keys()))

    result = data.get("result") or {}

    redirect_url = str(

        result.get("redirectURL") or result.get("redirect_url") or

        result.get("redirectUrl") or data.get("redirectURL") or ""

    ).strip()



    # Collect all cookies from the passport session

    cookie_str = "; ".join(f"{k}={v}" for k, v in migu_session.cookies.items())



    logger.info("Migu QR: has_redirect=%s, initial_cookie_count=%d", bool(redirect_url), len(cookie_str.split("; ")) if cookie_str else 0)



    # Follow the redirect chain to get music.migu.cn domain cookies

    if redirect_url:

        try:

            redirect_resp = migu_session.get(redirect_url, timeout=20, allow_redirects=True)

            all_cookies = {}

            for resp in ([redirect_resp] + list(getattr(redirect_resp, "history", []))):

                for k, v in resp.cookies.items():

                    all_cookies[k] = v

            for k, v in migu_session.cookies.items():

                all_cookies[k] = v

            cookie_str = "; ".join(f"{k}={v}" for k, v in all_cookies.items())

        except requests.RequestException as exc:

            logger.info("Migu QR redirect follow failed: %s", exc)



    with session_lock:

        session_store.pop(token, None)



    if not cookie_str:

        return {"status": "authorized", "cookie": "", "message": "扫码授权成功但未能自动提取 Cookie，请手动从浏览器复制"}



    if admin_mode:

        return {"status": "authorized", "cookie": cookie_str}

    account = _upsert_connected_account(user, "migu", "", "", cookie_str, "qr", "咪咕扫码授权完成")

    return {"status": "authorized", "account": _public_connected_account(account)}





def _qishui_bridge_headers():

    token = str(os.environ.get("QISHUI_BRIDGE_TOKEN", "") or "")

    return {"X-Qishui-Bridge-Token": token} if token else {}



def _qishui_bridge_request(method, path, payload=None, timeout=20):

    response = requests.request(

        method,

        f"{QISHUI_BRIDGE_URL}{path}",

        headers={**_qishui_bridge_headers(), "Content-Type": "application/json"},

        json=payload if payload is not None else None,

        timeout=timeout,

    )

    try:

        data = response.json()

    except ValueError as exc:

        raise ValueError(f"汽水登录桥返回了无效 JSON（HTTP {response.status_code}）") from exc

    if response.status_code != 200 or not isinstance(data, dict) or data.get("ok") is False:

        message = data.get("error") if isinstance(data, dict) else ""

        raise ValueError(str(message or f"汽水登录桥请求失败（HTTP {response.status_code}）"))

    return data



def _qishui_bridge_search(keyword, page=1, limit=20):

    """Search through the authenticated Electron bridge so ids match playback."""

    # Search and cross-platform fallback must use the authenticated bridge,
    # including after the main service has been restarted.
    _qishui_bridge_ready()

    try:

        page = max(1, int(page))

        limit = max(1, min(30, int(limit)))

    except (TypeError, ValueError):

        page, limit = 1, 20

    payload = _qishui_bridge_request(

        "POST",

        "/search",

        {"keyword": str(keyword or "").strip(), "offset": (page - 1) * limit, "limit": limit},

        timeout=20,

    )

    result = payload.get("result") if isinstance(payload, dict) else {}

    result = result if isinstance(result, dict) else {}

    raw_songs = result.get("songs") if isinstance(result.get("songs"), list) else []

    songs = [_player_qishui_song(item) for item in raw_songs]

    songs = [song for song in songs if song.get("id") and song.get("title")]

    if not songs and result.get("message"):

        raise ValueError(str(result.get("message")))

    return songs[:limit]




def _register_qishui_audio_url(raw_url):

    """Keep Qishui auth fragments server-side and expose only a short token to the browser."""

    raw_url = str(raw_url or "").strip()

    if not raw_url.startswith(("http://", "https://")):

        return ""

    token = secrets.token_urlsafe(32)

    with QISHUI_AUDIO_TOKEN_LOCK:

        now = time.time()

        for key, item in list(QISHUI_AUDIO_TOKENS.items()):

            if now - item["created_at"] > 3600:

                QISHUI_AUDIO_TOKENS.pop(key, None)

        QISHUI_AUDIO_TOKENS[token] = {"url": raw_url, "created_at": now}

    return f"/api/player/qishui-audio?token={token}"



def _qishui_audio_url(token):

    token = str(token or "").strip()

    with QISHUI_AUDIO_TOKEN_LOCK:

        item = QISHUI_AUDIO_TOKENS.get(token)

        if not item:

            return ""

        if time.time() - item["created_at"] > 3600:

            QISHUI_AUDIO_TOKENS.pop(token, None)

            return ""

        return item["url"]



def _qishui_bridge_available():

    """只检查已运行的汽水桥，不因后台状态查询自动启动 Electron。"""

    try:

        _qishui_bridge_request("GET", "/health", timeout=1.5)

        return True

    except (requests.RequestException, ValueError):

        return False


def _qishui_bridge_ready():

    """按需启动本机 Electron 汽水授权桥；桥只绑定 127.0.0.1。"""

    global QISHUI_BRIDGE_PROCESS

    try:

        _qishui_bridge_request("GET", "/health", timeout=1.5)

        return True

    except (requests.RequestException, ValueError):

        pass

    with QISHUI_BRIDGE_LOCK:

        try:

            _qishui_bridge_request("GET", "/health", timeout=1.5)

            return True

        except (requests.RequestException, ValueError):

            pass

        if not QISHUI_BRIDGE_APP.exists():

            raise ValueError("汽水音乐 Electron 授权桥未安装，请确认 .qishui-qr-bridge 文件完整")

        if not QISHUI_BRIDGE_ELECTRON.exists():

            raise ValueError("汽水音乐 Electron 运行时未安装，请先安装 .qishui-qr-bridge 的依赖")

        env = os.environ.copy()

        env["QISHUI_BRIDGE_PORT"] = str(QISHUI_BRIDGE_PORT)

        env["QISHUI_QR_CONFIG_FILE"] = str(QISHUI_BRIDGE_CONFIG_FILE)

        env["QISHUI_ELECTRON_USER_DATA"] = str(QISHUI_BRIDGE_USER_DATA)

        env["QISHUI_BRIDGE_USER_DATA"] = str(QISHUI_BRIDGE_USER_DATA)

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

        QISHUI_BRIDGE_PROCESS = subprocess.Popen(

            [str(QISHUI_BRIDGE_ELECTRON), str(QISHUI_BRIDGE_APP)],

            cwd=str(QISHUI_BRIDGE_DIR),

            env=env,

            stdin=subprocess.DEVNULL,

            stdout=subprocess.DEVNULL,

            stderr=subprocess.DEVNULL,

            creationflags=creationflags,

        )

        deadline = time.time() + 25

        while time.time() < deadline:

            if QISHUI_BRIDGE_PROCESS.poll() is not None:

                raise ValueError("汽水音乐 Electron 授权桥启动失败，请检查 Electron 运行时和桌面环境")

            try:

                _qishui_bridge_request("GET", "/health", timeout=1.0)

                return True

            except (requests.RequestException, ValueError):

                time.sleep(0.25)

        raise ValueError("汽水音乐 Electron 授权桥启动超时，请确认服务器桌面环境可用")



def _qishui_bridge_session():

    _qishui_bridge_ready()

    payload = _qishui_bridge_request("GET", "/session", timeout=10)

    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload

    cookie = str((result or {}).get("cookie") or "").strip()

    status = result.get("status") if isinstance(result, dict) else {}

    return cookie, status if isinstance(status, dict) else {}



def qishui_sync_persistent_session():

    """同步 Electron 持久化 Session 的 Cookie，并用汽水个人接口做健康检查。"""

    cfg = load_config()

    configured_cookie = _configured_qishui_cookie(cfg)

    has_bridge_config = QISHUI_BRIDGE_CONFIG_FILE.exists()

    if not configured_cookie and not has_bridge_config:

        cfg["qishui_status"] = "empty"

        cfg["qishui_last_error"] = None

        cfg["qishui_checked_at"] = datetime.now().isoformat()

        save_config(cfg)

        return False

    checked_at = datetime.now().isoformat()

    try:

        bridge_cookie, bridge_status = _qishui_bridge_session()

        effective_cookie = bridge_cookie or configured_cookie

        if not effective_cookie:

            cfg["qishui_status"] = "empty"

            cfg["qishui_last_error"] = None

            cfg["qishui_checked_at"] = checked_at

    
            save_config(cfg)

            return False

        profile = qishui_me(effective_cookie)

        if not profile.get("id"):

            raise ValueError("汽水登录会话无效，请重新扫码授权")

        previous_cookie = configured_cookie

        cfg["qishui_cookie"] = _encrypt_credential({

            "sessionid": parse_cookie_kv(effective_cookie, "sessionid"),

            "cookie": effective_cookie,

        })

        cfg["qishui_status"] = "ok"

        cfg["qishui_last_error"] = None

        cfg["qishui_checked_at"] = checked_at

        if bridge_cookie and bridge_cookie != previous_cookie:

            cfg["qishui_session_refreshed_at"] = checked_at


        save_config(cfg)

        return True

    except (QishuiError, requests.RequestException, ValueError) as exc:

        cfg = load_config()

        cfg["qishui_status"] = "invalid" if configured_cookie else "unknown"

        cfg["qishui_last_error"] = str(exc)[:500]

        cfg["qishui_checked_at"] = checked_at

        save_config(cfg)

        return False




def _start_qishui_qr_login(user, session_store=None, session_lock=None, owner_id=None, admin_mode=False):

    """Start an isolated Qishui QR session; the QR is confirmed in the logged-in Douyin app."""

    session_store = ACCOUNT_QR_SESSIONS if session_store is None else session_store

    session_lock = session_lock or ACCOUNT_QR_LOCK

    owner_id = user["id"] if owner_id is None else owner_id

    _qishui_bridge_ready()

    bridge_payload = _qishui_bridge_request("POST", "/qr/start", {}, timeout=45)

    bridge_result = bridge_payload.get("result") if isinstance(bridge_payload.get("result"), dict) else {}

    bridge_data = bridge_result.get("data") if isinstance(bridge_result.get("data"), dict) else {}

    bridge_token = str(bridge_data.get("token") or "").strip()

    qr_image = str(bridge_data.get("qr_image") or bridge_data.get("qrcode") or "").strip()

    if not bridge_token or not qr_image:

        raise ValueError("汽水登录桥未返回完整二维码，请稍后重试")

    local_token = secrets.token_urlsafe(24)

    _qr_cleanup(session_store, session_lock)

    with session_lock:

        session_store[local_token] = {

            "user_id": owner_id,

            "platform": "qishui",

            "bridge_token": bridge_token,

            "expires_at": datetime.now() + ACCOUNT_QR_TTL,

        }

    return {"token": local_token, "qr_image": qr_image, "expires_in": 300, "scan_app": "qishui", "message": "请使用已登录的汽水音乐或抖音 App 扫码确认"}





def _poll_qishui_qr_login(user, token, session_store=None, session_lock=None, owner_id=None, admin_mode=False):

    session_store = ACCOUNT_QR_SESSIONS if session_store is None else session_store

    session_lock = session_lock or ACCOUNT_QR_LOCK

    owner_id = user["id"] if owner_id is None else owner_id

    _qr_cleanup(session_store, session_lock)

    with session_lock:

        session = session_store.get(token)

    if not session or session.get("user_id") != owner_id or session.get("platform") != "qishui":

        raise ValueError("二维码会话不存在或已过期，请刷新二维码")

    bridge_payload = _qishui_bridge_request(

        "POST",

        "/qr/poll",

        {"token": session.get("bridge_token", "")},

        timeout=45,

    )

    bridge_result = bridge_payload.get("result") if isinstance(bridge_payload.get("result"), dict) else {}

    bridge_data = bridge_result.get("data") if isinstance(bridge_result.get("data"), dict) else {}

    bridge_status = str(bridge_result.get("status") or bridge_data.get("status") or "waiting").lower()

    status = "authorized" if bridge_status in ("authorized", "confirmed", "success") or bridge_data.get("confirmed") else bridge_status

    if status not in ("waiting", "scanned", "authorized", "expired", "failed"):

        status = "waiting"

    refreshed_cookie = str(bridge_result.get("cookie") or "").strip() if status == "authorized" else ""

    effective_cookie = refreshed_cookie

    result = {

        "status": status,

        "message": str(bridge_result.get("message") or bridge_data.get("description") or ""),

        "diagnostic": {

            "bridge_status": bridge_status,

            "bridge_cookie_ready": bool(refreshed_cookie),

        },

    }

    checked_at = datetime.now().isoformat()

    with session_lock:

        active_session = session_store.get(token)

        if active_session:

            if refreshed_cookie:

                active_session["qr_cookie"] = refreshed_cookie

            active_session["last_status"] = str(result.get("status") or "waiting")

            active_session["last_message"] = str(result.get("message") or "")

            active_session["last_checked_at"] = checked_at

            active_session["has_session_cookie"] = bool(effective_cookie)



    upstream_diagnostic = result.get("diagnostic") if isinstance(result.get("diagnostic"), dict) else {}

    diagnostic = {

        **upstream_diagnostic,

        "last_checked_at": checked_at,

        "has_session_cookie": bool(effective_cookie),

        "profile_valid": False,

    }

    diagnostic_scope = "admin" if admin_mode else "account"

    if result.get("status") != "authorized":

        result["diagnostic"] = diagnostic

        _save_qishui_qr_diagnostic(diagnostic_scope, result.get("status"), result.get("message"), diagnostic)

        if result.get("status") in ("expired", "failed"):

            with session_lock:

                session_store.pop(token, None)

        return result



    if not effective_cookie:

        message = "汽水音乐已确认，但未返回可用会话，请刷新二维码重试"

        _save_qishui_qr_diagnostic(diagnostic_scope, "failed", message, diagnostic)

        return {

            "status": "failed",

            "message": message,

            "diagnostic": diagnostic,

        }

    try:

        profile = qishui_me(effective_cookie)

        platform_user_id = str(profile.get("id") or "").strip()

        if not platform_user_id:

            message = "汽水音乐已确认，但上游未建立有效账号会话。请刷新二维码后重新扫码确认"

            _save_qishui_qr_diagnostic(diagnostic_scope, "failed", message, diagnostic)

            return {

                "status": "failed",

                "message": message,

                "diagnostic": diagnostic,

            }

        nickname = str(profile.get("nickname") or "汽水用户")

        diagnostic["profile_valid"] = True

        with session_lock:

            active_session = session_store.get(token)

            if active_session:

                active_session["profile_valid"] = True

    except (QishuiError, requests.RequestException, ValueError) as exc:

        message = f"汽水音乐授权校验失败：{exc}"

        _save_qishui_qr_diagnostic(diagnostic_scope, "failed", message, diagnostic)

        return {

            "status": "failed",

            "message": message,

            "diagnostic": diagnostic,

        }

    _save_qishui_qr_diagnostic(diagnostic_scope, "authorized", "授权成功，已取得有效汽水账号会话", diagnostic)

    with session_lock:

        session_store.pop(token, None)

    if admin_mode:

        return {"status": "authorized", "cookie": effective_cookie, "platform_user_id": platform_user_id, "nickname": nickname, "diagnostic": diagnostic}

    account = _upsert_connected_account(user, "qishui", platform_user_id, nickname, effective_cookie, "qr", "已通过汽水音乐 App 扫码授权")

    return {"status": "authorized", "account": _public_connected_account(account)}





@app.get("/api/accounts")

async def api_connected_accounts(request: Request):

    user = _require_user(request)

    data = load_connected_accounts(user["id"])

    supported_accounts = [item for item in data["accounts"] if item.get("platform") in ACCOUNT_PLATFORM_NAMES]

    if len(supported_accounts) != len(data["accounts"]):

        data["accounts"] = supported_accounts

        save_connected_accounts(data, user["id"])

    return JSONResponse({"code": 0, "accounts": [_public_connected_account(item) for item in supported_accounts]})



@app.post("/api/accounts/{platform}/connect")

async def api_connect_account(platform: str, request: Request):

    user = _require_user(request)

    platform = platform.lower().strip()

    if platform not in ACCOUNT_PLATFORM_NAMES:

        return JSONResponse({"code": -1, "msg": "不支持的音乐平台"})

    try:

        body = await request.json()

    except ValueError:

        return JSONResponse({"code": -1, "msg": "连接信息格式不正确"})

    method = str(body.get("method") or "cookie").lower()

    cookie = str(body.get("cookie") or "").strip()

    if method == "password":

        return JSONResponse({"code": -1, "msg": "为保护账户安全，当前版本不接收第三方明文密码。请使用平台扫码完成授权后导入会话 Cookie。"})

    if method != "cookie" or not cookie:

        return JSONResponse({"code": -1, "msg": "请粘贴已登录音乐平台网页导出的完整 Cookie 会话"})

    try:

        if platform == "netease":

            platform_user_id, nickname = _netease_account_profile(cookie)

            status_message = "会话已验证，可以同步网易云歌单"

        elif platform == "qq":

            platform_user_id, nickname = _qq_account_profile(cookie)

            status_message = "会话已验证，可以同步 QQ 音乐歌单"

        elif platform == "kugou":

            platform_user_id, nickname = _kugou_account_profile(cookie)

            status_message = "会话已验证，可以同步酷狗音乐歌单"

        else:

            platform_user_id, nickname = "", ACCOUNT_PLATFORM_NAMES[platform]

            status_message = "会话已加密保存；该平台的私有歌单接口仍在适配中"

    except ValueError as exc:

        return JSONResponse({"code": -1, "msg": str(exc)})

    data = load_connected_accounts(user["id"])

    now = datetime.now().isoformat()

    account = next((item for item in data["accounts"] if item.get("platform") == platform and str(item.get("platform_user_id") or "") == platform_user_id), None)

    if not account:

        account = {"id": uuid.uuid4().hex[:12], "user_id": user["id"], "platform": platform, "created_at": now}

        data["accounts"].append(account)

    account.update({

        "nickname": nickname, "platform_user_id": platform_user_id, "auth_type": "cookie", "credential_encrypted": _encrypt_credential({"cookie": cookie}),

        "status": "active" if platform in ("netease", "qq", "kugou", "qishui") else "unsupported", "status_message": status_message, "updated_at": now,

    })

    save_connected_accounts(data, user["id"])

    return JSONResponse({"code": 0, "msg": f"已连接{ACCOUNT_PLATFORM_NAMES[platform]}账户", "account": _public_connected_account(account)})





@app.post("/api/accounts/{platform}/qr/start")

async def api_start_account_qr(platform: str, request: Request):

    user = _require_user(request)

    platform = platform.lower().strip()

    if platform not in ACCOUNT_PLATFORM_NAMES:

        return JSONResponse({"code": -1, "msg": "\u4e0d\u652f\u6301\u7684\u97f3\u4e50\u5e73\u53f0"})

    try:

        if platform == "netease":

            result = _start_netease_qr_login(user)

        elif platform == "qq":

            result = _start_qq_qr_login(user)

        elif platform == "kugou":

            result = _start_kugou_qr_login(user)

        elif platform == "qishui":

            result = _start_qishui_qr_login(user)

        else:

            return JSONResponse({"code": -1, "msg": f"{ACCOUNT_PLATFORM_NAMES[platform]}\u6682\u65e0\u53ef\u9a8c\u8bc1\u7684\u7ad9\u5185\u626b\u7801\u6388\u6743\u56de\u8c03\uff0c\u8bf7\u4f7f\u7528\u4f1a\u8bdd\u5bfc\u5165\u8fde\u63a5\u3002"}, status_code=422)

        return JSONResponse({"code": 0, **result})

    except (requests.RequestException, ValueError) as exc:

        return JSONResponse({"code": -1, "msg": str(exc)}, status_code=502)





@app.get("/api/accounts/{platform}/qr/{token}")

async def api_poll_account_qr(platform: str, token: str, request: Request):

    user = _require_user(request)

    platform = platform.lower().strip()

    try:

        if platform == "netease":

            result = _poll_netease_qr_login(user, token)

        elif platform == "qq":

            result = _poll_qq_qr_login(user, token)

        elif platform == "kugou":

            result = _poll_kugou_qr_login(user, token)

        elif platform == "qishui":

            result = _poll_qishui_qr_login(user, token)

        else:

            return JSONResponse({"code": -1, "msg": "\u8be5\u5e73\u53f0\u6682\u4e0d\u652f\u6301\u626b\u7801\u6388\u6743"}, status_code=422)

        return JSONResponse({"code": 0, **result})

    except (requests.RequestException, ValueError) as exc:

        return JSONResponse({"code": -1, "msg": str(exc)}, status_code=422)





@app.post("/api/accounts/{account_id}/sync")

async def api_sync_account(account_id: str, request: Request):

    user = _require_user(request)

    data = load_connected_accounts(user["id"])

    account = next((item for item in data["accounts"] if item.get("id") == account_id), None)

    if not account:

        return JSONResponse({"code": -1, "msg": "未找到该账户"}, status_code=404)

    try:

        playlists, songs = _sync_account_playlists(user, account)

        save_connected_accounts(data, user["id"])

        return JSONResponse({"code": 0, "msg": f"已同步 {playlists} 个歌单、{songs} 首歌曲", "account": _public_connected_account(account)})

    except ValueError as exc:
        account.update({"status": "needs_reauth", "status_message": str(exc), "updated_at": datetime.now().isoformat()})
        save_connected_accounts(data, user["id"])
        return JSONResponse({"code": -1, "msg": str(exc)})
    except Exception:
        logger.exception("Unexpected playlist sync failure for account %s", account_id)
        account.update({
            "status": "error",
            "status_message": "歌单同步服务暂时异常，请稍后重试",
            "updated_at": datetime.now().isoformat(),
        })
        save_connected_accounts(data, user["id"])
        return JSONResponse({"code": -1, "msg": "歌单同步服务暂时异常，请稍后重试"}, status_code=502)


@app.delete("/api/accounts/{account_id}")

async def api_disconnect_account(account_id: str, request: Request):

    user = _require_user(request)

    data = load_connected_accounts(user["id"])

    original = len(data["accounts"])

    data["accounts"] = [item for item in data["accounts"] if item.get("id") != account_id]

    if len(data["accounts"]) == original:

        return JSONResponse({"code": -1, "msg": "未找到该账户"}, status_code=404)

    save_connected_accounts(data, user["id"])

    return JSONResponse({"code": 0, "msg": "已断开账户。已同步的本地歌单仍会保留。"})





# ==================== 本机歌单 API ====================



@app.get("/api/playlists")

async def api_playlists(request: Request):

    user = _require_user(request)

    owner_id = str(user["id"])

    data = load_playlists(user["id"])

    if not _find_playlist(data, "liked"):

        now = datetime.now().isoformat()

        data["playlists"].insert(0, {

            "id": "liked",

            "name": "我喜欢的音乐",

            "user_id": owner_id,

            "created_at": now,

            "updated_at": now,

            "songs": [],

        })

        save_playlists(data)

    return JSONResponse({"code": 0, **data})





@app.post("/api/playlists")

async def api_create_playlist(request: Request):

    body = await request.json()

    name = str(body.get("name") or "").strip()

    if not name or len(name) > 60:

        return JSONResponse({"code": -1, "msg": "歌单名称不能为空且不能超过 60 个字符"})

    user = _require_user(request)

    data = load_playlists(user["id"])

    if any(item.get("name") == name for item in data["playlists"]):

        return JSONResponse({"code": -1, "msg": "已经存在同名歌单"})

    now = datetime.now().isoformat()

    playlist = {

        "id": uuid.uuid4().hex[:12],

        "name": name,

        "user_id": user["id"],

        "created_at": now,

        "updated_at": now,

        "songs": [],

    }

    data["playlists"].append(playlist)

    save_playlists(data)

    return JSONResponse({"code": 0, "playlist": playlist})





@app.patch("/api/playlists/{playlist_id}")

async def api_rename_playlist(playlist_id: str, request: Request):

    body = await request.json()

    name = str(body.get("name") or "").strip()

    if not name or len(name) > 60:

        return JSONResponse({"code": -1, "msg": "歌单名称不能为空且不能超过 60 个字符"})

    user = _require_user(request)

    data = load_playlists(user["id"])

    playlist = _find_playlist(data, playlist_id)

    if not playlist:

        return JSONResponse({"code": -1, "msg": "歌单不存在"})

    if any(item.get("id") != playlist_id and item.get("name") == name for item in data["playlists"]):

        return JSONResponse({"code": -1, "msg": "已经存在同名歌单"})

    playlist["name"] = name

    playlist["updated_at"] = datetime.now().isoformat()

    save_playlists(data)

    return JSONResponse({"code": 0, "playlist": playlist})





@app.delete("/api/playlists/{playlist_id}")

async def api_delete_playlist(playlist_id: str, request: Request):

    if playlist_id == "liked":

        return JSONResponse({"code": -1, "msg": "默认喜欢歌单不能删除"})

    data = load_playlists(_require_user(request)["id"])

    before = len(data["playlists"])

    data["playlists"] = [item for item in data["playlists"] if str(item.get("id")) != playlist_id]

    if len(data["playlists"]) == before:

        return JSONResponse({"code": -1, "msg": "歌单不存在"})

    save_playlists(data)

    return JSONResponse({"code": 0, "msg": "歌单已删除"})





@app.post("/api/playlists/{playlist_id}/songs")

async def api_add_playlist_song(playlist_id: str, request: Request):

    body = await request.json()

    song = _playlist_song(body.get("song", body))

    if not song:

        return JSONResponse({"code": -1, "msg": "歌曲信息不完整"})

    data = load_playlists(_require_user(request)["id"])

    playlist = _find_playlist(data, playlist_id)

    if not playlist:

        return JSONResponse({"code": -1, "msg": "歌单不存在"})

    song_key = f"{song['source']}:{song['id']}"

    playlist.setdefault("songs", [])

    playlist["songs"] = [

        item for item in playlist["songs"]

        if f"{item.get('source', 'netease')}:{item.get('id', '')}" != song_key

    ]

    playlist["songs"].insert(0, song)

    playlist["updated_at"] = datetime.now().isoformat()

    save_playlists(data)

    return JSONResponse({"code": 0, "playlist": playlist})





@app.delete("/api/playlists/{playlist_id}/songs/{source}/{song_id}")

async def api_remove_playlist_song(playlist_id: str, source: str, song_id: str, request: Request):

    user = _require_user(request)

    data = load_playlists(user["id"])

    playlist = _find_playlist(data, playlist_id)

    if not playlist:

        return JSONResponse({"code": -1, "msg": "歌单不存在"})

    before = len(playlist.get("songs", []))

    playlist["songs"] = [

        item for item in playlist.get("songs", [])

        if not (str(item.get("source")) == source and str(item.get("id")) == song_id)

    ]

    if len(playlist["songs"]) == before:

        return JSONResponse({"code": -1, "msg": "歌曲不在该歌单中"})

    playlist["updated_at"] = datetime.now().isoformat()

    save_playlists(data)

    return JSONResponse({"code": 0, "playlist": playlist})





@app.post("/api/playlists/import")

async def api_import_playlists(request: Request):

    body = await request.json()

    incoming = body.get("data", body)

    if isinstance(incoming, dict):

        incoming = incoming.get("playlists") or incoming.get("playlist") or incoming.get("songs") or []

    if isinstance(incoming, dict):

        incoming = [incoming]

    if not isinstance(incoming, list):

        return JSONResponse({"code": -1, "msg": "无法识别歌单 JSON 格式"})



    user = _require_user(request)

    data = load_playlists(user["id"])

    imported_playlists = 0

    imported_songs = 0

    for item in incoming:

        if not isinstance(item, dict):

            continue

        name = str(item.get("name") or item.get("title") or "导入歌单").strip()[:60] or "导入歌单"

        songs_raw = item.get("songs") if isinstance(item.get("songs"), list) else [item]

        songs = []

        seen = set()

        for raw_song in songs_raw:

            song = _playlist_song(raw_song)

            if not song:

                continue

            key = f"{song['source']}:{song['id']}"

            if key not in seen:

                seen.add(key)

                songs.append(song)

        if not songs:

            continue

        target = next((p for p in data["playlists"] if p.get("name") == name), None)

        now = datetime.now().isoformat()

        if not target:

            target = {

                "id": "liked" if name == "我喜欢的音乐" else uuid.uuid4().hex[:12],

                "name": name,

                "user_id": user["id"],

                "created_at": now,

                "updated_at": now,

                "songs": [],

            }

            data["playlists"].append(target)

            imported_playlists += 1

        existing = {

            f"{s.get('source', 'netease')}:{s.get('id', '')}"

            for s in target.get("songs", [])

        }

        for song in songs:

            key = f"{song['source']}:{song['id']}"

            if key not in existing:

                target.setdefault("songs", []).append(song)

                existing.add(key)

                imported_songs += 1

        target["updated_at"] = now

    save_playlists(data)

    return JSONResponse({

        "code": 0,

        "msg": f"导入完成：{imported_playlists} 个歌单，{imported_songs} 首歌曲",

        "playlists": data["playlists"],

    })





@app.post("/api/playlists/import-link")

async def api_import_playlist_link(request: Request):

    try:

        body = await request.json()

        platform, remote_id = _resolve_playlist_share_url_and_parse(body.get("url"))

        cfg = load_config()

        if platform == "netease":

            remote_name, songs = _get_netease_playlist(remote_id, cfg.get("cookie", ""))

        elif platform == "qq":

            remote_name, songs = _get_qq_playlist(remote_id, qq_request_cookie(cfg))

        elif platform == "kugou":

            remote_name, songs = _get_kugou_playlist(remote_id)

        elif platform == "kugou_collection":

            remote_name, songs = _get_kugou_collection_playlist(remote_id)

            platform = "kugou"

        elif platform == "qishui":

            public_playlist = qishui_public_playlist(remote_id)

            remote_id = public_playlist["id"]

            remote_name, songs = public_playlist["name"], public_playlist["songs"]

        else:

            remote_name, songs = _get_kuwo_playlist(remote_id)

    except (QishuiError, ValueError, TypeError, AttributeError, KeyError, requests.RequestException) as exc:

        logger.info("Playlist share import failed: %s", exc)

        return JSONResponse({"code": -1, "msg": str(exc)})

    except Exception:

        logger.exception("Unexpected playlist share import failure")

        return JSONResponse({"code": -1, "msg": "歌单导入时发生异常，请稍后重试"})



    requested_name = str(body.get("name") or "").strip()

    if len(requested_name) > 60:

        return JSONResponse({"code": -1, "msg": "本地歌单名称不能超过 60 个字符"})

    user = _require_user(request)

    owner_id = str(user["id"])

    data = load_playlists(owner_id)

    playlist = next(

        (

            item for item in data["playlists"]

            if item.get("remote_platform") == platform and str(item.get("remote_id")) == remote_id

        ),

        None,

    )

    now = datetime.now().isoformat()

    if not playlist:

        name = requested_name or remote_name.strip() or "导入歌单"

        name = _unique_playlist_name(data["playlists"], name[:60], platform)

        playlist = {

            "id": uuid.uuid4().hex[:12],

            "name": name,

            "user_id": owner_id,

            "created_at": now,

            "updated_at": now,

            "remote_platform": platform,

            "remote_id": remote_id,

            "songs": [],

        }

        data["playlists"].append(playlist)



    existing = {

        f"{item.get('source', 'netease')}:{item.get('id', '')}"

        for item in playlist.get("songs", [])

    }

    added = 0

    for song in songs:

        key = f"{song['source']}:{song['id']}"

        if key not in existing:

            playlist.setdefault("songs", []).append(song)

            existing.add(key)

            added += 1

    playlist["updated_at"] = now

    save_playlists(data)

    return JSONResponse({

        "code": 0,

        "msg": f"已导入 {playlist['name']}：新增 {added} 首歌曲",

        "playlist": playlist,

        "platform": platform,

    })





ADMIN_HTML = """<!DOCTYPE html>

<html lang="zh-CN">

<head>

<meta charset="UTF-8">

<meta name="viewport" content="width=device-width, initial-scale=1.0">

<title>点歌API - 管理后台</title>

<style>

* { margin: 0; padding: 0; box-sizing: border-box; }

body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; color: #333; }

.container { max-width: 800px; margin: 0 auto; padding: 20px; }

.card { background: #fff; border-radius: 12px; padding: 24px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }

.card h2 { margin-bottom: 16px; font-size: 18px; color: #e60026; }

.form-group { margin-bottom: 16px; }

.form-group label { display: block; margin-bottom: 6px; font-weight: 600; font-size: 14px; }

.form-group input, .form-group textarea { width: 100%; padding: 10px 12px; border: 1px solid #ddd; border-radius: 8px; font-size: 14px; }

.form-group textarea { min-height: 80px; resize: vertical; }

.form-group .hint { font-size: 12px; color: #999; margin-top: 4px; }

.btn { display: inline-block; padding: 10px 24px; border: none; border-radius: 8px; font-size: 14px; cursor: pointer; font-weight: 600; transition: all 0.2s; }

.btn-primary { background: #e60026; color: #fff; }

.btn-primary:hover { background: #c4001f; }

.btn-secondary { background: #f0f0f0; color: #333; margin-left: 8px; }

.btn-secondary:hover { background: #e0e0e0; }

.btn-success { background: #2e7d32; color: #fff; }

.btn-success:hover { background: #1b5e20; }

.status-badge { display: inline-block; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; }

.status-ok { background: #e8f5e9; color: #2e7d32; }

.status-expired { background: #fce4ec; color: #c62828; }

.status-empty { background: #fff3e0; color: #e65100; }

.status-unknown { background: #f5f5f5; color: #666; }

.result-msg { margin-top: 12px; padding: 10px; border-radius: 8px; font-size: 14px; display: none; }

.result-success { background: #e8f5e9; color: #2e7d32; }

.result-error { background: #fce4ec; color: #c62828; }

.log-table { width: 100%; border-collapse: collapse; font-size: 13px; }

.log-table th, .log-table td { padding: 8px 12px; text-align: left; border-bottom: 1px solid #eee; }

.log-table th { font-weight: 600; color: #666; font-size: 12px; }

.status-bar { display: flex; align-items: center; gap: 12px; margin-top: 8px; }

.status-info { font-size: 13px; color: #666; margin-top: 4px; line-height: 1.6; }

.api-url { background: #f8f8f8; padding: 8px 12px; border-radius: 6px; font-family: monospace; font-size: 13px; word-break: break-all; margin-top: 8px; }

/* Tab 切换 */

.tab-nav { display: flex; gap: 0; margin-bottom: 20px; border-radius: 12px; overflow: hidden; }

.tab-btn { flex: 1; padding: 14px 24px; border: none; cursor: pointer; font-size: 16px; font-weight: 600; transition: all 0.2s; background: #eee; color: #888; }

.tab-btn.active { background: #e60026; color: #fff; }

.tab-btn:hover:not(.active) { background: #e0e0e0; }

.tab-panel { display: none; }

.tab-panel.active { display: block; }

.qq-field-table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 8px; }

.qq-field-table td { padding: 6px 10px; border-bottom: 1px solid #f0f0f0; }

.qq-field-table td:first-child { font-weight: 600; color: #888; white-space: nowrap; width: 130px; }

.qq-field-table td:last-child { word-break: break-all; font-family: monospace; font-size: 12px; }

.user-table { width: 100%; border-collapse: collapse; font-size: 13px; }

.user-table th, .user-table td { padding: 10px 8px; text-align: left; border-bottom: 1px solid #eee; vertical-align: middle; }

.user-table th { color: #666; font-size: 12px; }

.user-table .user-actions { display: flex; flex-wrap: wrap; gap: 6px; }

.user-table .btn { padding: 7px 10px; font-size: 12px; }

.btn-danger { background: #c62828; color: #fff; }

.btn-danger:hover { background: #8e1d1d; }

.user-muted { color: #999; }
.user-list-toolbar { display: flex; align-items: center; gap: 8px; margin: 14px 0; }
.user-list-toolbar input { flex: 1; min-width: 180px; margin: 0; }
.user-list-toolbar .btn { margin-left: 0; white-space: nowrap; }
.user-pagination { display: flex; align-items: center; justify-content: flex-end; gap: 8px; margin-top: 14px; }
.user-pagination .btn { margin-left: 0; }
.user-pagination-summary { margin-right: auto; color: #777; font-size: 13px; }
.registration-control { display:flex; align-items:center; justify-content:space-between; gap:16px; padding:14px 16px; margin-bottom:14px; border:1px solid #eee; border-radius:10px; background:#fafafa; }
.registration-control-actions { display:flex; align-items:center; gap:8px; flex-shrink:0; }
.registration-control-actions .btn { margin-left:0; white-space:nowrap; }
@media (max-width:600px) { .registration-control { align-items:flex-start; flex-direction:column; } .registration-control-actions { width:100%; justify-content:space-between; } }

.admin-qr-dialog { position: fixed; inset: 0; margin: auto; width: min(360px, calc(100vw - 32px)); max-height: calc(100vh - 32px); border: none; border-radius: 12px; padding: 0; box-shadow: 0 18px 54px rgba(0,0,0,.24); }

.admin-qr-dialog::backdrop { background: rgba(21, 26, 38, .52); }

.admin-qr-body { padding: 24px; text-align: center; }

.admin-qr-body h2 { margin-bottom: 10px; font-size: 18px; color: #e60026; }

.admin-qr-image { width: 240px; height: 240px; object-fit: contain; display: block; margin: 16px auto; border-radius: 8px; background: #f7f7f7; }

.admin-qr-status { min-height: 22px; color: #666; font-size: 13px; line-height: 1.5; }

.admin-qr-actions { display: flex; justify-content: center; gap: 8px; margin-top: 18px; }

.admin-password-dialog { position: fixed; inset: 0; margin: auto; width: min(420px, calc(100vw - 32px)); border: none; border-radius: 12px; padding: 0; box-shadow: 0 18px 54px rgba(0,0,0,.24); }

.admin-password-dialog::backdrop { background: rgba(21, 26, 38, .52); }

.admin-password-body { padding: 24px; }

.admin-password-body h2 { margin-bottom: 8px; font-size: 18px; color: #333; }

.admin-password-body .form-group { margin-top: 16px; }

.admin-password-body input { width: 100%; box-sizing: border-box; }

.admin-password-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 20px; }

.admin-password-actions .btn { margin-left: 0; }

@media (max-width: 640px) { .tab-nav { flex-wrap: wrap; border-radius: 8px; } .tab-btn { flex: 1 1 45%; padding: 12px 10px; } .user-list-toolbar { align-items: stretch; flex-wrap: wrap; } .user-list-toolbar input { flex-basis: 100%; } .user-pagination { flex-wrap: wrap; } .user-pagination-summary { flex-basis: 100%; } }

.third-source-item { padding: 8px 0; border-bottom: 1px solid #f0f0f0; }

.third-source-item:last-child { border-bottom: none; }

.switch-label { cursor: pointer; display: flex; align-items: center; gap: 8px; }

.btn-danger { background: linear-gradient(135deg, #e53935, #c62828); color: #fff; border: none; border-radius: 6px; cursor: pointer; }

.btn-danger:hover { opacity: 0.9; }

.btn-sm { padding: 4px 10px; font-size: 12px; }

.switch-label input[type="checkbox"] { width: 18px; height: 18px; cursor: pointer; }

</style>

</head>

<body>

<div class="container">

    <div style="display:flex;justify-content:flex-end;margin-top:16px;">

        <a href="/music" style="display:inline-block;padding:9px 14px;background:#e60026;color:#fff;border-radius:7px;text-decoration:none;font-size:14px;font-weight:600;">打开音乐平台</a>

    </div>

    <h1 style="margin: 24px 0; font-size: 24px;">🎵 点歌API 管理后台</h1>

    

    <!-- Tab 切换 -->

    <div class="tab-nav">

        <button class="tab-btn active" onclick="switchTab('netease')">🎵 网易云音乐</button>

        <button class="tab-btn" onclick="switchTab('qq')">🎵 QQ音乐</button>

        <button class="tab-btn" onclick="switchTab('kugou')">&#127925; &#37239;&#29399;&#38899;&#20048;</button>

         <button class="tab-btn" onclick="switchTab('migu')">🎵 咪咕音乐</button>

        <button class="tab-btn" onclick="switchTab('qishui')">🎵 汽水音乐</button>

        <button class="tab-btn" onclick="switchTab('third')">🔗 第三方音源</button>

        <button class="tab-btn" onclick="switchTab('users')">👥 用户管理</button>

        <button class="tab-btn" onclick="switchTab('source-settings')">⚙️ 音源设置</button>

    </div>

    

    <!-- 网易云面板 -->

    <div id="tab-netease" class="tab-panel active">

        <!-- Cookie 状态 -->

        <div class="card">

            <h2>🔑 Cookie 状态</h2>

            <div class="status-bar">

                <span id="ncmStatus" class="status-badge status-unknown">检测中...</span>

                <span id="ncmNickname" style="font-size:14px;"></span>

            </div>

            <div id="ncmCheckedAt" style="font-size:12px;color:#999;margin-top:4px;"></div>

            <div class="api-url" id="apiUrl" style="margin-top:8px;"></div>

            <div class="status-info">

                <div>📅 上次续期: <span id="ncmRefreshedAt">-</span></div>

                <div>⏰ 下次续期: <span id="ncmNextRefreshAt">-</span></div>

            </div>

            <div style="margin-top:8px;">

                <button class="btn btn-success" type="button" onclick="renewNcm()">🔄 手动续期</button>

                <button class="btn btn-secondary" onclick="checkNcmCookie()">🔍 重新检测</button>

            </div>

            <div id="ncmRenewResult" class="result-msg"></div>

        </div>

        

        <!-- 配置 -->

        <div class="card">

            <h2>⚙️ 网易云配置</h2>

            <div class="form-group">

                <label>Cookie (MUSIC_U)</label>

                <textarea id="cookie" placeholder="从浏览器 F12 → Application → Cookies → 复制 MUSIC_U 的值"></textarea>

                <div class="hint">获取方法：登录 music.163.com → F12 → Application → Cookies → 找到 MUSIC_U → 复制值</div>

            </div>

            <div class="form-group">

                <label>API Key（鉴权密钥）</label>

                <input type="text" id="apiKey" placeholder="设置一个密钥，调用 API 时需要带上">

                <div class="hint">留空则不验证，任何人都可以调用。建议设置一个复杂的 key。</div>

            </div>

            <button class="btn btn-primary" onclick="saveNcmSettings()">💾 保存配置</button>

            <button class="btn btn-secondary" type="button" onclick="startAdminQr('netease')">&#128247; &#25195;&#30721;&#25480;&#26435;</button>

            <div id="ncmSettingResult" class="result-msg"></div>

        </div>

        
        <!-- API URL -->

        <div class="card">

            <h2>📡 API 接口</h2>

            <div class="api-url" id="apiUrlDetail">加载中...</div>

        </div>

    </div>

    

    <!-- QQ音乐面板 -->

    <div id="tab-qq" class="tab-panel">

        <!-- QQ Cookie 状态 -->

        <div class="card">

            <h2>🔑 QQ音乐 Cookie 状态</h2>

            <div class="status-bar">

                <span id="qqStatus" class="status-badge status-unknown">检测中...</span>

            </div>

            <div id="qqCheckedAt" style="font-size:12px;color:#999;margin-top:4px;"></div>

            <div id="qqLastError" style="font-size:12px;color:#c62828;margin-top:4px;"></div>

            <div class="status-info">

                <div>📅 上次续期: <span id="qqRefreshedAt">-</span></div>

                <div>⏰ 下次续期: <span id="qqNextRefreshAt">-</span></div>

            </div>

            <div style="margin-top:8px;">

                <button class="btn btn-success" onclick="renewQQ()">🔄 手动续期</button>

                <button class="btn btn-secondary" onclick="checkQQ()">🔍 检测 Cookie</button>

            </div>

        </div>

        

        <!-- QQ配置 -->

        <div class="card">

            <h2>⚙️ QQ音乐配置</h2>

            <div class="form-group">

                <label>完整 Cookie（从 y.qq.com 复制）</label>

                <textarea id="qqCookie" placeholder="登录 y.qq.com → F12 → Network → 复制 Request Headers 里的 Cookie 完整值"></textarea>

                <div class="hint">获取方法：登录 y.qq.com → F12 → Network 随便点一个请求 → 复制 Request Headers 里的 Cookie 完整值</div>

            </div>

            <button class="btn btn-primary" onclick="saveQQCookie()">💾 解析并保存</button>

            <button class="btn btn-secondary" type="button" onclick="startAdminQr('qq')">&#128247; &#25195;&#30721;&#25480;&#26435;</button>

            <div id="qqSaveResult" class="result-msg"></div>

        </div>

        

        <!-- 解析字段展示 -->

        <div class="card" id="qqFieldsCard" style="display:none;">

            <h2>📋 已提取字段</h2>

            <table class="qq-field-table" id="qqFieldsTable">

                <tr><td>qqmusic_key</td><td id="field-musickey">-</td></tr>

                <tr><td>uin</td><td id="field-uin">-</td></tr>

                <tr><td>refresh_token</td><td id="field-refresh">-</td></tr>

                <tr><td>access_token</td><td id="field-access">-</td></tr>

                <tr><td>openid</td><td id="field-openid">-</td></tr>

                <tr><td>unionid</td><td id="field-unionid">-</td></tr>

                <tr><td>euin</td><td id="field-euin">-</td></tr>

            </table>

        </div>

        

        <!-- API URL -->

        <div class="card">

            <h2>📡 API 接口</h2>

            <div class="api-url" id="apiUrlQQDetail">加载中...</div>

        </div>

    </div>

    

    <!-- 咪咕音乐面板 -->

    <div id="tab-migu" class="tab-panel">

        <div class="card">

            <h2>🔑 Cookie 状态</h2>

            <div class="status-bar">

                <span id="miguStatus" class="status-badge status-unknown">检测中...</span>

            </div>

            <div id="miguCheckedAt" style="font-size:12px;color:#999;margin-top:4px;"></div>

            <div id="miguLastError" style="font-size:12px;color:#c62828;margin-top:4px;"></div>

            <div class="status-info">

                <div>📅 上次续期: <span id="miguRefreshedAt">-</span></div>

                <div>⏰ 下次续期: <span id="miguNextRefreshAt">-</span></div>

            </div>

            <div style="margin-top:8px;">

                <button class="btn btn-success" type="button" onclick="renewMigu()">🔄 手动续期</button>

                <button class="btn btn-secondary" onclick="checkMiguCookie()">🔍 重新检测</button>

            </div>

            <div id="miguRenewResult" class="result-msg"></div>

            <div class="hint" style="margin-top:8px;">咪咕会话由 pacmtoken 承载，后台会按设定间隔自动滚动续期（默认 3 小时），无需每天手动重填。</div>

        </div>

        <div class="card">

            <h2>⚙️ 咪咕音乐配置</h2>

            <div class="form-group">

                <label>完整 Cookie（从已登录的 music.migu.cn 复制）</label>

                <textarea id="miguCookie" placeholder="登录 music.migu.cn → F12 → Application → Cookies → 复制全部 Cookie 值"></textarea>

                <div class="hint">获取方法：登录 music.migu.cn → F12 → Network → 随意请求 → 复制 Request Headers 里的 Cookie 完整值</div>

            </div>

            <button class="btn btn-primary" onclick="saveMiguCookie()">💾 保存并检测</button>

            <button class="btn btn-secondary" type="button" onclick="startAdminQr('migu')">📷 扫码授权</button>

            <div id="miguSaveResult" class="result-msg"></div>

        </div>

        <div class="card">

            <h2>📡 API 接口</h2>

            <div class="api-url" id="apiUrlMiguDetail">加载中...</div>

            <p style="color:#666;font-size:13px;margin-top:8px;">填写 Cookie 后，搜索和插件点歌将使用你的会员账号获取播放地址。</p>

        </div>

        <div class="card">

            <h2>🧪 测试咪咕点歌</h2>

            <div class="form-group">

                <label>歌名</label>

                <input type="text" id="miguTestKeyword" value="七里香" placeholder="输入歌名测试">

            </div>

            <button class="btn btn-primary" onclick="testMiguSearch()">🔍 搜索测试</button>

            <div id="miguTestResult" class="result-msg"></div>

            <pre id="miguTestJson" style="background:#f8f8f8;padding:12px;border-radius:8px;font-size:12px;overflow-x:auto;margin-top:12px;display:none;"></pre>

        </div>

    </div>



    <!-- 汽水音乐面板 -->

    <div id="tab-qishui" class="tab-panel">

        <div class="card">

            <h2>🔑 汽水音乐会话状态</h2>

            <div class="status-bar">

                <span id="qishuiStatus" class="status-badge status-unknown">检测中...</span>

                <span id="qishuiBridgeStatus" style="font-size:14px;"></span>

            </div>

            <div id="qishuiCheckedAt" style="font-size:12px;color:#999;margin-top:4px;"></div>

            <div id="qishuiLastError" style="font-size:12px;color:#c62828;margin-top:4px;"></div>

            <div class="status-info">

                <div>📅 上次会话同步: <span id="qishuiRefreshedAt">-</span></div>

                <div>🔒 会话策略: <span>持久化 Session；汽水暂无可确认的官方自动续期接口</span></div>

            </div>

            <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;">

                <button class="btn btn-secondary" type="button" onclick="checkQishui()">🔍 重新检测</button>

                <button class="btn btn-primary" type="button" onclick="startAdminQr('qishui')">📷 扫码授权</button>

            </div>

            <div id="qishuiCheckResult" class="result-msg"></div>

            <div class="hint" style="margin-top:8px;">请使用已登录汽水音乐或抖音 App 的设备扫码并在 App 中确认。汽水授权依赖本机 Electron 桥，服务端不会直接暴露二维码授权接口。</div>

        </div>

        <div class="card">

            <h2>📡 汽水音乐接口</h2>

            <div class="api-url" id="apiUrlQishuiDetail">加载中...</div>

            <p style="color:#666;font-size:13px;margin-top:8px;">授权会话仅用于你自己的汽水账号搜索、播放和歌单同步。扫码成功后会以服务器加密形式保存会话。</p>

        </div>

    </div>



    <!-- 第三方音源面板 -->

    <!-- Kugou panel -->

    <div id="tab-kugou" class="tab-panel">

        <div class="card">

            <h2>&#128273; &#37239;&#29399;&#38899;&#20048; Cookie &#29366;&#24577;</h2>

            <div class="status-bar"><span id="kugouStatus" class="status-badge status-unknown">&#26816;&#27979;&#20013;...</span></div>

            <div id="kugouCheckedAt" style="font-size:12px;color:#999;margin-top:4px;"></div>

            <div id="kugouLastError" style="font-size:12px;color:#c62828;margin-top:4px;"></div>

            <div class="status-info"><div>&#29992;&#25143; ID: <span id="kugouUserId">-</span></div><div>&#20250;&#35805;&#31867;&#22411;: <span id="kugouSessionType">-</span></div></div>

            <div class="status-info" style="margin-top:6px;">

                <div>📅 上次续期: <span id="kugouRefreshedAt">-</span></div>

                <div>⏰ 下次续期: <span id="kugouNextRefreshAt">-</span></div>

            </div>

            <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;">

                <button class="btn btn-success" type="button" onclick="renewKugou()">🔄 手动续期</button>

                <button class="btn btn-secondary" onclick="checkKugou()">&#128269; &#37325;&#26032;&#26816;&#27979;</button>

            </div>

            <div id="kugouRenewHint" class="hint" style="margin-top:8px;"></div>

        </div>

        <div class="card">

            <h2>&#9881;&#65039; &#37239;&#29399;&#38899;&#20048;&#37197;&#32622;</h2>

            <div class="form-group">

                <label>&#23436;&#25972; Cookie&#65288;&#20174;&#24050;&#30331;&#24405;&#30340; kugou.com &#22797;&#21046;&#65289;</label>

                <textarea id="kugouCookie" placeholder="Web: KugooID=...; t=...; kg_mid=...; kg_dfid=...&#10;App: token=...; userid=...; KUGOU_API_MID=...; dfid=..."></textarea>

                <div class="hint">&#25903;&#25345;&#37239;&#29399;&#32593;&#39029; Cookie&#65288;KugooID / t / kg_mid / kg_dfid&#65289;&#21644; App &#20250;&#35805;&#65288;token / userid&#65289;&#12290;&#32593;&#39029; Cookie &#29992;&#20110;&#25628;&#32034;&#19982;&#23448;&#26041;&#25773;&#25918;&#22336;&#35299;&#26512;&#65307;App &#20250;&#35805;&#21478;&#22806;&#29992;&#20110;&#36134;&#25143;&#27468;&#21333;&#21516;&#27493;&#12290;</div>

            </div>

            <button class="btn btn-primary" onclick="saveKugouCookie()">&#128190; &#20445;&#23384;&#24182;&#26816;&#27979;</button>

            <button class="btn btn-secondary" type="button" onclick="startAdminQr('kugou')">&#128247; &#25195;&#30721;&#25480;&#26435;</button>

            <div id="kugouSaveResult" class="result-msg"></div>

        </div>

        <div class="card">

            <h2>&#127925; &#37239;&#29399;&#27010;&#24565;&#29256;&#20250;&#35805;</h2>

            <div class="status-bar"><span id="kugouConceptStatus" class="status-badge status-unknown">&#26816;&#27979;&#20013;...</span></div>

            <div id="kugouConceptCheckedAt" style="font-size:12px;color:#999;margin-top:4px;"></div>

            <div id="kugouConceptLastError" style="font-size:12px;color:#c62828;margin-top:4px;"></div>

            <div class="status-info"><div>&#29992;&#25143; ID: <span id="kugouConceptUserId">-</span></div><div>&#20250;&#35805;&#31867;&#22411;: <span>&#27010;&#24565;&#29256; App</span></div></div>

            <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;">

                <button class="btn btn-primary" type="button" onclick="startAdminQr('kugou_concept')">&#128247; &#27010;&#24565;&#29256;&#25195;&#30721;&#25480;&#26435;</button>

                <button class="btn btn-success" type="button" onclick="renewKugouConcept()">🔄 手动续期</button>

                <button class="btn btn-secondary" type="button" onclick="checkKugouConcept()">&#128269; &#37325;&#26032;&#26816;&#27979;</button>

            </div>

            <div id="kugouConceptResult" class="result-msg"></div>

            <div class="status-info" style="margin-top:10px;">

                <div>📅 上次续期: <span id="kugouConceptRefreshedAt">-</span></div>

                <div>🩺 上次自动执行: <span id="kugouConceptSchedulerAt">-</span></div>

                <div>⏰ 下次自动续期: <span id="kugouConceptNextCheckAt">-</span></div>

            </div>

            <div class="hint" style="margin-top:10px;">&#27010;&#24565;&#29256;&#20250;&#35805;&#19982;&#19978;&#26041;&#30340;&#37239;&#29399;&#32593;&#39029; Cookie &#29420;&#31435;&#20445;&#23384;&#12290;&#21482;&#29992;&#20110;&#31649;&#29702;&#21592;&#30340;&#25628;&#32034;&#19982;&#25773;&#25918;&#20248;&#20808;&#32423;&#65292;&#19981;&#24433;&#21709;&#26222;&#36890;&#29992;&#25143;&#30340;&#37239;&#29399;&#36134;&#25143;&#27468;&#21333;&#21516;&#27493;&#12290;</div>

            <div class="hint" style="margin-top:6px;">概念版会话已支持官方 /v5/login_by_token 自动续期：后台按设定间隔滚动 token 与 t1，续期失败会回退健康检查并在此提示。</div>

        </div>

        <div class="card">

            <h2>&#9654;&#65039; &#37239;&#29399;&#25773;&#25918;&#20248;&#20808;&#32423;</h2>

            <div class="form-group">

                <label for="kugouPlayerPriority">&#24403;&#38899;&#20048;&#24179;&#21488;&#20351;&#29992;&#8220;&#37239;&#29399;&#38899;&#20048;&#8221;&#25628;&#32034;&#25110;&#25773;&#25918;&#26102;</label>

                <select id="kugouPlayerPriority">

                    <option value="concept_first">&#37239;&#29399;&#27010;&#24565;&#29256;&#20248;&#20808;&#65288;&#25512;&#33616;&#65289;</option>

                    <option value="standard_first">&#26222;&#36890;&#37239;&#29399;&#20248;&#20808;</option>

                </select>

                <div class="hint">&#39318;&#36873;&#20250;&#35805;&#19981;&#21487;&#29992;&#25110;&#19981;&#36820;&#22238;&#32467;&#26524;&#26102;&#65292;&#20250;&#33258;&#21160;&#22238;&#36864;&#21040;&#21478;&#19968;&#22871;&#37239;&#29399;&#20250;&#35805;&#65307;&#20004;&#32773;&#37117;&#19981;&#21487;&#29992;&#26102;&#65292;&#20445;&#30041;&#21407;&#26469;&#30340;&#31532;&#19977;&#26041;&#22238;&#28304;&#19982;&#36328;&#24179;&#21488;&#34917;&#28304;&#27969;&#31243;&#12290;</div>

            </div>

            <button class="btn btn-primary" type="button" onclick="saveKugouPriority()">&#128190; &#20445;&#23384;&#25773;&#25918;&#20248;&#20808;&#32423;</button>

            <div id="kugouPriorityResult" class="result-msg"></div>

        </div>

        <div class="card"><h2>&#128225; API &#25509;&#21475;</h2><div class="api-url" id="apiUrlKugouDetail">&#21152;&#36733;&#20013;...</div></div>

    </div>



    <div id="tab-third" class="tab-panel">

        <div class="card">

            <h2>📡 API 接口</h2>

            <div class="api-url" id="apiUrlThirdDetail">加载中...</div>

        </div>

        <div class="card">

            <h2>🔗 第三方音源管理</h2>

            <p style="color:#666;font-size:13px;">在这里启用或停用第三方音源；全局音质、等待上限和官方补源顺序请到“音源设置”页面统一调整。</p>

            <div id="thirdSourcesList"></div>

            <button class="btn btn-primary" onclick="saveThirdSources()">💾 保存第三方音源</button>

            <div id="thirdSaveResult" class="result-msg"></div>

        </div>



        <div class="card">

            <h2>🔎 音源扫描器</h2>

            <p style="color:#666;font-size:13px;">从“音乐音源”文件夹选择脚本后，系统只静态识别公开接口，不会执行脚本；识别出的候选源会用真实歌曲回源验证，通过后才能一键添加并启用。</p>

            <div class="form-group">

                <label>音源文件</label>

                <select id="scannerFile">

                    <option value="">加载中...</option>

                </select>

            </div>

            <button class="btn btn-primary" onclick="scanSourceFile()">🔎 扫描并验证</button>

            <div id="scannerResult" class="result-msg" style="white-space:normal;"></div>

        </div>



        <div class="card">

            <h2>📤 上传并检测音源</h2>

            <p style="color:#666;font-size:13px;">选择本地 JS 音源文件后，系统会先进行同样的静态识别和真实回源验证。只有验证通过才会保存到“音乐音源”文件夹。</p>

            <div class="form-group">

                <label>本地 JS 音源文件</label>

                <input type="file" id="scannerUploadFile" accept=".js,application/javascript,text/javascript">

            </div>

            <button class="btn btn-primary" onclick="uploadAndScanSourceFile()">📤 检测并上传</button>

            <div id="scannerUploadResult" class="result-msg" style="white-space:normal;"></div>

        </div>

        

        <!-- 添加自定义音源 -->

        <div class="card">

            <h2>➕ 添加自定义音源</h2>

            <div class="form-group">

                <label>源标识（唯一 ID，字母数字下划线）</label>

                <input type="text" id="newSrcId" placeholder="如：mysource">

            </div>

            <div class="form-group">

                <label>名称</label>

                <input type="text" id="newSrcName" placeholder="如：我的音源">

            </div>

            <div class="form-group">

                <label>描述</label>

                <input type="text" id="newSrcDesc" placeholder="可选">

            </div>

            <div class="form-group">

                <label>API 地址</label>

                <input type="text" id="newSrcApiUrl" placeholder="如：https://lxmusicapi.onrender.com">

            </div>

            <div class="form-group">

                <label>API Key（可选）</label>

                <input type="text" id="newSrcApiKey" placeholder="如：share-v3，没有留空">

            </div>

            <div class="form-group">

                <label>音源类型</label>

                <select id="newSrcType" onchange="onSrcTypeChange()">

                    <option value="generic">通用（URL模板拼接）</option>

                    <option value="lx_script">LX Music 脚本源</option>

                </select>

                <span style="font-size:11px;color:#888;">LX脚本源只需填 API 地址，其他自动处理</span>

            </div>

            <div class="form-group" id="genericFields">

                <label>请求方法</label>

                <select id="newSrcMethod">

                    <option value="GET">GET</option>

                    <option value="POST">POST</option>

                </select>

            </div>

            <div class="form-group" id="genericPatternGroup">

                <label>URL 路径模板</label>

                <input type="text" id="newSrcPattern" value="/url/{source}/{song_id}/{quality}" placeholder="可用 {source} {song_id} {quality} 变量">

            </div>

            <div id="genericMoreFields">

                <div class="form-group">

                    <label>鉴权 Header 名（如 X-Request-Key）</label>

                    <input type="text" id="newSrcAuth" placeholder="留空则无鉴权头">

                </div>

                <div class="form-group">

                    <label>成功字段路径（code 字段）</label>

                    <input type="text" id="newSrcCodeField" value="code" placeholder="如 data.code">

                </div>

                <div class="form-group">

                    <label>成功 code 值</label>

                    <input type="number" id="newSrcCodeValue" value="0">

                </div>

                <div class="form-group">

                    <label>URL 字段路径</label>

                    <input type="text" id="newSrcUrlField" value="url" placeholder="如 data.url">

                </div>

                <div class="form-group">

                    <label>支持平台（逗号分隔）</label>

                    <input type="text" id="newSrcSources" placeholder="可选：tx, wy, kg, kw, mg，留空则全部尝试">

                </div>

            </div>

            <button class="btn btn-primary" onclick="addThirdSource()">✅ 添加并测试</button>

            <div id="thirdAddResult" class="result-msg"></div>

        </div>

        

        <div class="card">

            <h2>🧪 测试音源</h2>

            <div class="form-group">

                <label>选择音源测试</label>

                <select id="testSourceSelect"></select>

            </div>

            <button class="btn btn-secondary" onclick="testThirdSource()">🔍 测试</button>

            <div id="thirdTestResult" class="result-msg"></div>

        </div>

    </div>

    

    <!-- 音源设置面板 -->

    <div id="tab-source-settings" class="tab-panel">

        <div class="card">
            <h2>📣 网站公告</h2>
            <div class="form-group">
                <label>公告内容</label>
                <textarea id="siteAnnouncement" maxlength="500" placeholder="填写后会显示在音乐平台顶部，并持续滚动给所有用户查看"></textarea>
                <div class="hint">留空并保存即可隐藏公告栏。最多 500 个字符。</div>
            </div>
            <button class="btn btn-primary" type="button" onclick="saveAnnouncement()">📣 保存公告</button>
            <div id="sourceAnnouncementResult" class="result-msg"></div>
        </div>

        <div class="card">
            <h2>🔁 会话自动保活</h2>
            <div class="form-group">
                <label><input type="checkbox" id="autoRenewEnabled"> 启用自动保活（后台每 5 分钟检查一次到期任务）</label>
            </div>
            <div class="form-group">
                <label>咪咕续期间隔（小时）<span id="miguIntervalRange" class="hint" style="display:inline;"></span></label>
                <input type="number" id="miguRenewInterval" min="1" max="24" step="1">
            </div>
            <div class="form-group">
                <label>QQ音乐续期间隔（小时）<span id="qqIntervalRange" class="hint" style="display:inline;"></span></label>
                <input type="number" id="qqRenewInterval" min="1" max="60" step="1">
                <div class="hint">QQ 返回的 keyExpiresIn 实测约 3 天，间隔必须明显小于该值。</div>
            </div>
            <div class="form-group">
                <label>网易云续期间隔（小时）<span id="ncmIntervalRange" class="hint" style="display:inline;"></span></label>
                <input type="number" id="ncmRenewInterval" min="1" max="168" step="1">
            </div>
            <div class="form-group">
                <label>酷狗概念版续期间隔（小时）<span id="kugouConceptIntervalRange" class="hint" style="display:inline;"></span></label>
                <input type="number" id="kugouConceptCheckInterval" min="1" max="168" step="1">
                <div class="hint">走概念版官方 /v5/login_by_token 刷新，成功后自动滚动 token 与 t1。</div>
            </div>
            <div class="form-group">
                <label>酷狗音乐续期间隔（小时）<span id="kugouIntervalRange" class="hint" style="display:inline;"></span></label>
                <input type="number" id="kugouRenewInterval" min="1" max="168" step="1">
                <div class="hint">仅 App 会话（token + userid，扫码授权获得）可自动续期；网页 Cookie 只做健康检查。</div>
            </div>
            <button class="btn btn-primary" type="button" onclick="saveRenewSettings()">💾 保存保活设置</button>
            <div id="renewSettingsResult" class="result-msg"></div>
        </div>

        <div class="card">
            <h2>🎚️ 播放与回源</h2>
            <div class="form-group">
                <label>全局播放音质（官方源与第三方回源）</label>
                <select id="thirdQuality">
                    <option value="128k">128k 标准</option>
                    <option value="320k" selected>320k 高品质</option>
                    <option value="flac">FLAC 无损</option>
                    <option value="hires">Hi-Res（可用时）</option>
                </select>
            </div>
            <div class="form-group">
                <label>第三方回源等待上限（秒）</label>
                <input id="thirdSourceTimeout" type="number" min="3" max="60" step="1" value="10">
                <div class="hint">并行检测已启用的第三方回源，超时后停止等待。</div>
            </div>
            <div class="form-group">
                <label>自动播放总等待上限（秒）</label>
                <input id="playerResolveTimeout" type="number" min="5" max="75" step="1" value="12">
                <div class="hint">官方源与第三方回源同时解析；总时长会自动不小于第三方等待时长。</div>
            </div>
            <div class="form-group">
                <label>跨平台官方补源等待上限（秒）</label>
                <input id="crossPlatformTimeout" type="number" min="1" max="30" step="1" value="4">
                <div class="hint">仅在首轮没有可播放地址时，才并行补查其余官方平台的同一首歌。</div>
            </div>
            <div class="form-group">
                <label>跨平台官方补源优先级</label>
                <select id="crossPlatformPriority">
                    <option value="netease,qq,kugou,migu,qishui">网易云 → QQ → 酷狗 → 咪咕 → 汽水</option>
                    <option value="qq,netease,kugou,migu,qishui">QQ → 网易云 → 酷狗 → 咪咕 → 汽水</option>
                    <option value="kugou,qq,netease,migu,qishui">酷狗 → QQ → 网易云 → 咪咕 → 汽水</option>
                    <option value="migu,qq,kugou,netease,qishui">咪咕 → QQ → 酷狗 → 网易云 → 汽水</option>
                    <option value="qishui,qq,kugou,netease,migu">汽水 → QQ → 酷狗 → 网易云 → 咪咕</option>
                    <option value="qq,kugou,netease,migu,qishui">QQ → 酷狗 → 网易云 → 咪咕 → 汽水</option>
                    <option value="kugou,netease,qq,migu,qishui">酷狗 → 网易云 → QQ → 咪咕 → 汽水</option>
                </select>
                <div class="hint">当前平台和第三方回源都无可用地址时，按此顺序依次搜索其他官方平台。</div>
            </div>
            <button class="btn btn-primary" type="button" onclick="savePlaybackSettings()">💾 保存播放与回源设置</button>
            <div id="sourceSettingsResult" class="result-msg"></div>
        </div>

    </div>


    <!-- 用户管理面板 -->

    <div id="tab-users" class="tab-panel">

        <div class="card">

            <h2>👥 用户管理</h2>

            <p style="color:#666;font-size:13px;margin-bottom:14px;">封禁会立即使该用户退出登录；删除会永久清除该用户的本地歌单和第三方音乐账户授权。管理员账号不能在这里被操作；修改密码后，该用户需要用新密码重新登录。</p>

            <div class="registration-control">
                <div>
                    <strong>新用户注册</strong>
                    <div class="user-muted">关闭后不会影响已有账号登录、歌单和播放功能。</div>
                </div>
                <div class="registration-control-actions">
                    <span id="registrationStatus" class="status-badge status-unknown">加载中...</span>
                    <button id="registrationToggle" class="btn btn-secondary" type="button" onclick="toggleRegistration()" disabled>加载中...</button>
                </div>
            </div>

            <div id="userManageResult" class="result-msg"></div>

            <form class="user-list-toolbar" onsubmit="searchManagedUsers(event)">
                <input id="userSearchInput" type="search" maxlength="32" autocomplete="off" placeholder="按登录账号搜索（区分大小写）">
                <button class="btn btn-primary" type="submit">搜索</button>
                <button class="btn btn-secondary" type="button" onclick="clearManagedUserSearch()">清空</button>
            </form>

            <div style="overflow-x:auto;">

                <table class="user-table">

                    <thead><tr><th>用户</th><th>角色</th><th>注册时间</th><th>最后登录时间</th><th>最后登录 IP</th><th>状态</th><th>操作</th></tr></thead>

                    <tbody id="userManageBody"><tr><td colspan="7" class="user-muted">点击“用户管理”后加载</td></tr></tbody>

                </table>

            </div>

            <div class="user-pagination">
                <span id="userPaginationSummary" class="user-pagination-summary">第 1 页</span>
                <button id="userPrevPage" class="btn btn-secondary" type="button" onclick="changeManagedUserPage(-1)" disabled>上一页</button>
                <button id="userNextPage" class="btn btn-secondary" type="button" onclick="changeManagedUserPage(1)" disabled>下一页</button>
            </div>

        </div>

    </div>



    <!-- 测试点歌（三个 Tab 共用） -->

    <div class="card">

        <h2>🧪 测试点歌</h2>

        <div class="form-group">

            <label>歌名</label>

            <input type="text" id="testKeyword" placeholder="输入歌名测试，如：七里香">

        </div>

        <div class="form-group">

            <label>音乐源</label>

            <select id="testSource" style="padding:8px;border-radius:8px;border:1px solid #ddd;">

                <option value="netease">网易云音乐</option>

                <option value="qq">QQ音乐</option>

                <option value="kugou">酷狗音乐</option>

                <option value="migu">咪咕音乐</option>

                <option value="qishui">汽水音乐</option>

            </select>

        </div>

        <button class="btn btn-primary" onclick="testSearch()">🔍 搜索</button>

        <div id="testResult" class="result-msg"></div>

        <pre id="testXml" style="background:#f8f8f8;padding:12px;border-radius:8px;font-size:12px;overflow-x:auto;margin-top:12px;display:none;"></pre>

    </div>

    

    <!-- 统计 -->

    <div class="card">

        <h2>📊 服务统计</h2>

        <p id="statsText">加载中...</p>

    </div>

    

    <!-- 最近调用 -->

    <div class="card">

        <h2>📋 最近调用记录</h2>

        <div style="overflow-x:auto;">

            <table class="log-table" id="logTable">

                <thead><tr><th>时间</th><th>关键词</th><th>来源</th><th>结果</th><th>耗时</th></tr></thead>

                <tbody id="logBody"><tr><td colspan="5">加载中...</td></tr></tbody>

            </table>

        </div>

    </div>

</div>

<dialog id="adminQrDialog" class="admin-qr-dialog" onclose="stopAdminQr()">

  <div class="admin-qr-body">

    <h2 id="adminQrTitle">&#25195;&#30721;&#25480;&#26435;</h2>

    <div class="hint">&#25480;&#26435;&#25104;&#21151;&#21518;&#65292;&#20250;&#35805;&#21482;&#20445;&#23384;&#21040;&#20840;&#23616; API &#37197;&#32622;&#12290;</div>

    <img id="adminQrImage" class="admin-qr-image" alt="QR code">

    <div id="adminQrStatus" class="admin-qr-status"></div>

    <div class="admin-qr-actions"><button class="btn btn-primary" type="button" onclick="startAdminQr(adminQrState.platform)">&#21047;&#26032;&#20108;&#32500;&#30721;</button><button class="btn btn-secondary" type="button" onclick="closeAdminQr()">&#20851;&#38381;</button></div>

  </div>

</dialog>

<dialog id="adminPasswordDialog" class="admin-password-dialog" onclose="resetAdminPasswordDialog()">

  <form method="dialog" class="admin-password-body" onsubmit="submitAdminPassword(event); return false;">

    <h2>&#20462;&#25913;&#29992;&#25143;&#23494;&#30721;</h2>

    <div id="adminPasswordTarget" class="hint"></div>

    <div class="form-group">

      <label for="adminPasswordInput">&#26032;&#23494;&#30721;</label>

      <input id="adminPasswordInput" type="password" minlength="8" maxlength="128" autocomplete="new-password" placeholder="&#35831;&#36755;&#20837; 8-128 &#20301;&#26032;&#23494;&#30721;" required>

    </div>

    <div class="form-group">

      <label for="adminPasswordConfirm">&#30830;&#35748;&#26032;&#23494;&#30721;</label>

      <input id="adminPasswordConfirm" type="password" minlength="8" maxlength="128" autocomplete="new-password" placeholder="&#20877;&#27425;&#36755;&#20837;&#26032;&#23494;&#30721;" required>

    </div>

    <div id="adminPasswordResult" class="result-msg"></div>

    <div class="admin-password-actions">

      <button class="btn btn-secondary" type="button" onclick="closeAdminPasswordDialog()">&#21462;&#28040;</button>

      <button id="adminPasswordSubmit" class="btn btn-primary" type="submit">&#20445;&#23384;&#23494;&#30721;</button>

    </div>

  </form>

</dialog>



<script>

const BASE = window.location.origin;



// Tab 切换

function switchTab(tab) {

    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));

    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));

    document.querySelector('.tab-btn[onclick*="' + tab + '"]').classList.add('active');

    document.getElementById('tab-' + tab).classList.add('active');

    // 切换时刷新对应面板数据

    if (tab === 'netease') { loadNcmSettings(); loadNcmStatus(); }

    if (tab === 'qq') loadQQStatus();

    if (tab === 'kugou') { loadKugouStatus(); loadKugouConceptStatus(); }

    if (tab === 'migu') { loadMiguSettings(); loadMiguStatus(); }

    if (tab === 'qishui') loadQishuiStatus();

    if (tab === 'third') loadThirdSources();

    if (tab === 'source-settings') { loadNcmSettings(); loadRenewSettings(); loadThirdSources(); }

    if (tab === 'users') { loadManagedUsers(); loadRegistrationSettings(); }

}



async function loadAll() {

    await loadNcmSettings();

    await loadNcmStatus();

    await loadRenewSettings();

    await loadQQStatus();

    await loadKugouStatus();

    await loadKugouConceptStatus();

    await loadMiguSettings();

    await loadQishuiStatus();

    await loadThirdSources();

    await loadLogs();

}



// ========== 用户管理 ==========

function escUserText(value) {

    return String(value == null ? '' : value).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;', "'":'&#39;'}[ch]));

}



function showUserManageResult(message, success) {

    const el = document.getElementById('userManageResult');

    el.className = 'result-msg ' + (success ? 'result-success' : 'result-error');

    el.textContent = (success ? '✅ ' : '❌ ') + message;

    el.style.display = 'block';

}



async function loadRegistrationSettings() {

    const statusEl = document.getElementById('registrationStatus');

    const toggleEl = document.getElementById('registrationToggle');

    if (!statusEl || !toggleEl) return;

    try {

        const response = await fetch(BASE + '/admin/api/config', {cache: 'no-store'});

        const data = await response.json();

        if (!response.ok || data.code === -1) throw new Error(data.detail || data.msg || '注册状态读取失败');

        const enabled = data.allow_registration !== false;

        statusEl.textContent = enabled ? '✅ 已开启' : '⛔ 已关闭';

        statusEl.className = 'status-badge ' + (enabled ? 'status-ok' : 'status-expired');

        toggleEl.textContent = enabled ? '关闭注册' : '开启注册';

        toggleEl.className = 'btn ' + (enabled ? 'btn-secondary' : 'btn-success');

        toggleEl.disabled = false;

        toggleEl.dataset.registrationEnabled = enabled ? 'true' : 'false';

    } catch (error) {

        statusEl.textContent = '读取失败';

        statusEl.className = 'status-badge status-expired';

        toggleEl.textContent = '重试';

        toggleEl.className = 'btn btn-secondary';

        toggleEl.disabled = false;

        toggleEl.dataset.registrationEnabled = '';

        showUserManageResult(error.message || '注册状态读取失败', false);

    }

}



async function toggleRegistration() {

    const toggleEl = document.getElementById('registrationToggle');

    if (!toggleEl || toggleEl.disabled) return;

    const currentValue = toggleEl.dataset.registrationEnabled;

    if (!currentValue) {

        await loadRegistrationSettings();

        return;

    }

    const next = currentValue !== 'true';

    const action = next ? '开启' : '关闭';

    if (!window.confirm('确定' + action + '新用户注册吗？')) return;

    toggleEl.disabled = true;

    try {

        const response = await fetch(BASE + '/admin/api/config', {

            method: 'POST',

            headers: {'Content-Type': 'application/json'},

            body: JSON.stringify({allow_registration: next})

        });

        const data = await response.json();

        if (!response.ok || data.code !== 0) throw new Error(data.detail || data.msg || '注册开关保存失败');

        showUserManageResult(next ? '已开启新用户注册' : '已关闭新用户注册', true);

        await loadRegistrationSettings();

    } catch (error) {

        toggleEl.disabled = false;

        showUserManageResult(error.message || '注册开关保存失败', false);

    }

}



const managedUserState = {page: 1, pageSize: 20, query: '', totalPages: 1};

function searchManagedUsers(event) {

    if (event) event.preventDefault();

    managedUserState.query = document.getElementById('userSearchInput').value.trim();

    managedUserState.page = 1;

    loadManagedUsers();

}

function clearManagedUserSearch() {

    document.getElementById('userSearchInput').value = '';

    managedUserState.query = '';

    managedUserState.page = 1;

    loadManagedUsers();

}

function changeManagedUserPage(delta) {

    const nextPage = Math.min(managedUserState.totalPages, Math.max(1, managedUserState.page + delta));

    if (nextPage === managedUserState.page) return;

    managedUserState.page = nextPage;

    loadManagedUsers();

}

async function loadManagedUsers() {

    const tbody = document.getElementById('userManageBody');

    try {

        const params = new URLSearchParams({page: managedUserState.page, page_size: managedUserState.pageSize});

        if (managedUserState.query) params.set('q', managedUserState.query);

        const response = await fetch(BASE + '/admin/api/users?' + params.toString(), {cache: 'no-store'});

        const data = await response.json();

        if (!response.ok || data.code !== 0) throw new Error(data.detail || data.msg || '用户列表加载失败');

        const users = data.users || [];

        managedUserState.page = Number(data.page || 1);

        managedUserState.totalPages = Number(data.total_pages || 1);

        document.getElementById('userPaginationSummary').textContent = '第 ' + managedUserState.page + ' / ' + managedUserState.totalPages + ' 页，共 ' + Number(data.total || 0) + ' 个用户';

        document.getElementById('userPrevPage').disabled = managedUserState.page <= 1;

        document.getElementById('userNextPage').disabled = managedUserState.page >= managedUserState.totalPages;

        if (!users.length) {

            tbody.innerHTML = '<tr><td colspan="7" class="user-muted">' + (managedUserState.query ? '没有匹配的用户' : '暂无用户') + '</td></tr>';

            return;

        }

        tbody.innerHTML = users.map(user => {

            const isAdmin = user.role === 'admin';

            const name = escUserText(user.display_name || user.username);

            const username = escUserText(user.username || '');

            const joined = user.created_at ? escUserText(String(user.created_at).replace('T', ' ').slice(0, 19)) : '-';

            const lastLogin = user.last_login_at ? escUserText(String(user.last_login_at).replace('T', ' ').slice(0, 19)) : '暂无记录';

            const lastIp = user.last_login_ip ? escUserText(user.last_login_ip) : '暂无记录';

            const state = isAdmin ? '管理员' : (user.banned ? '已封禁' : '正常');

            const stateClass = user.banned ? 'status-expired' : (isAdmin ? 'status-ok' : 'status-unknown');

            const actions = isAdmin ? '<span class="user-muted">受保护</span>' : (user.banned

                ? '<button class="btn btn-success" type="button" data-user-action="unban" data-user-id="' + escUserText(user.id) + '">解禁</button>'

                : '<button class="btn btn-secondary" type="button" data-user-action="ban" data-user-id="' + escUserText(user.id) + '">封禁</button>')

                + '<button class="btn btn-secondary" type="button" data-user-action="reset-password" data-user-id="' + escUserText(user.id) + '" data-user-name="' + name + '">改密码</button>'

                + '<button class="btn btn-danger" type="button" data-user-action="delete" data-user-id="' + escUserText(user.id) + '" data-user-name="' + name + '">删除</button>';

            return '<tr><td><strong>' + name + '</strong><div class="user-muted">' + username + '</div></td><td>' + (isAdmin ? '管理员' : '普通用户') + '</td><td>' + joined + '</td><td>' + lastLogin + '</td><td>' + lastIp + '</td><td><span class="status-badge ' + stateClass + '">' + state + '</span></td><td><div class="user-actions">' + actions + '</div></td></tr>';

        }).join('');

        tbody.querySelectorAll('[data-user-action]').forEach(button => button.addEventListener('click', () => manageUser(button)));

    } catch (error) {

        tbody.innerHTML = '<tr><td colspan="7" class="user-muted">用户列表加载失败</td></tr>';

        showUserManageResult(error.message || '用户列表加载失败', false);

    }

}



async function manageUser(button) {

    const action = button.dataset.userAction;

    const userId = button.dataset.userId;

    const username = button.dataset.userName || '该用户';

    const labels = {ban: '封禁', unban: '解禁', 'reset-password': '改密码', delete: '删除'};

    const destructive = action === 'delete';

    if (action === 'reset-password') {
        openAdminPasswordDialog(userId, username);
        return;

    }

    const prompt = destructive

        ? '确定删除“' + username + '”吗？该用户的本地歌单和第三方账户授权会被永久删除。'

        : '确定' + labels[action] + '该用户吗？';

    if (!window.confirm(prompt)) return;

    button.disabled = true;

    try {

        const method = destructive ? 'DELETE' : 'POST';

        const response = await fetch(BASE + '/admin/api/users/' + encodeURIComponent(userId) + (action === 'delete' ? '' : '/' + action), {method});

        const data = await response.json();

        if (!response.ok || data.code !== 0) throw new Error(data.detail || data.msg || '操作失败');

        showUserManageResult(data.msg || '操作成功', true);

        await loadManagedUsers();

    } catch (error) {

        showUserManageResult(error.message || '操作失败', false);

        button.disabled = false;

    }

}



// ========== 咪咕音乐 ==========

async function loadMiguSettings() {

    const target = document.getElementById('apiUrlMiguDetail');

    if (!target) return;

    try {

        const r = await fetch(BASE + '/admin/api/config');

        const data = await r.json();

        target.textContent = BASE + '/api/migumusic?keyword=歌名' + (data.api_key ? '&key=' + data.api_key : '');

    } catch (e) { target.textContent = BASE + '/api/migumusic?keyword=歌名'; }

}



async function loadMiguStatus() {

    try {

        const r = await fetch(BASE + '/admin/api/migu-status');

        const data = await r.json();

        const badge = document.getElementById('miguStatus');

        const status = data.migu_status || 'empty';

        badge.textContent = status === 'ok' ? '✅ 有效' : status === 'invalid' ? '❌ 已失效' : status === 'empty' ? '⚠️ 未设置' : '❓ 未知';

        badge.className = 'status-badge status-' + status;

        const checked = document.getElementById('miguCheckedAt');

        if (checked) checked.textContent = data.migu_checked_at ? '检测时间：' + data.migu_checked_at : '';

        const error = document.getElementById('miguLastError');

        if (error) error.textContent = data.migu_last_error || '';

        const refreshed = document.getElementById('miguRefreshedAt');

        if (refreshed) refreshed.textContent = data.migu_refreshed_at || '-';

        const nextRefresh = document.getElementById('miguNextRefreshAt');

        if (nextRefresh) nextRefresh.textContent = data.migu_next_refresh_at || '-';

    } catch (e) { console.error(e); }

}



async function saveMiguCookie() {

    const cookie = document.getElementById('miguCookie').value.trim();

    const resultEl = document.getElementById('miguSaveResult');

    if (!cookie) { resultEl.className = 'result-msg result-error'; resultEl.textContent = '请输入 Cookie'; resultEl.style.display = 'block'; return; }

    resultEl.className = 'result-msg result-success'; resultEl.textContent = '正在保存并检测...'; resultEl.style.display = 'block';

    try {

        const r = await fetch(BASE + '/admin/api/save-migu-cookie', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({migu_cookie: cookie})});

        const data = await r.json();

        resultEl.className = 'result-msg ' + (data.code === 0 ? 'result-success' : 'result-error');

        resultEl.textContent = (data.code === 0 ? '✅ ' : '❌ ') + data.msg;

        resultEl.style.display = 'block';

        await loadMiguStatus();

    } catch (e) { resultEl.className = 'result-msg result-error'; resultEl.textContent = '❌ ' + e.message; resultEl.style.display = 'block'; }

}



async function checkMiguCookie() {

    try {

        const r = await fetch(BASE + '/admin/api/check-migu', {method: 'POST'});

        await loadMiguStatus();

    } catch (e) { console.error(e); }

}



async function testMiguSearch() {

    const keyword = document.getElementById('miguTestKeyword').value.trim();

    const resultEl = document.getElementById('miguTestResult');

    const jsonEl = document.getElementById('miguTestJson');

    if (!keyword) { resultEl.className = 'result-msg result-error'; resultEl.textContent = '请输入歌名'; resultEl.style.display = 'block'; return; }

    resultEl.className = 'result-msg'; resultEl.textContent = '⏳ 测试中...'; resultEl.style.display = 'block';

    try {

        const r = await fetch(BASE + '/api/migumusic?keyword=' + encodeURIComponent(keyword));

        const data = await r.json();

        resultEl.className = 'result-msg ' + (data.code === 200 ? 'result-success' : 'result-error');

        resultEl.textContent = data.code === 200 ? '✅ ' + data.title + ' - ' + data.singer + ' | 播放：' + (data.music_url ? '有' : '无') : '❌ ' + (data.msg || '未找到');

        jsonEl.textContent = JSON.stringify(data, null, 2); jsonEl.style.display = 'block';

    } catch (e) { resultEl.className = 'result-msg result-error'; resultEl.textContent = '❌ ' + e.message; resultEl.style.display = 'block'; }

}



// ========== 网易云 ==========

async function loadNcmSettings() {

    try {

        const r = await fetch(BASE + '/admin/api/config');

        const data = await r.json();

        document.getElementById('cookie').value = data.cookie || '';

        document.getElementById('apiKey').value = data.api_key || '';

        document.getElementById('siteAnnouncement').value = data.announcement || '';

        document.getElementById('apiUrlDetail').textContent = BASE + '/api/wxmusic?keyword=歌名' + (data.api_key ? '&key=' + data.api_key : '');

    } catch(e) { console.error(e); }

}



async function loadNcmStatus() {

    try {

        const r = await fetch(BASE + '/admin/api/status');

        const data = await r.json();

        const badge = document.getElementById('ncmStatus');

        badge.textContent = data.cookie_status === 'ok' ? '✅ 有效' : 

                          data.cookie_status === 'expired' ? '❌ 已失效' : 

                          data.cookie_status === 'empty' ? '⚠️ 未设置' : '❓ 未知';

        badge.className = 'status-badge status-' + data.cookie_status;

        document.getElementById('ncmNickname').textContent = data.cookie_nickname ? '👤 ' + data.cookie_nickname : '';

        document.getElementById('ncmCheckedAt').textContent = data.cookie_checked_at ? '检测时间：' + data.cookie_checked_at : '';

        const ncmRefreshed = document.getElementById('ncmRefreshedAt');

        if (ncmRefreshed) ncmRefreshed.textContent = data.ncm_refreshed_at || '-';

        const ncmNext = document.getElementById('ncmNextRefreshAt');

        if (ncmNext) ncmNext.textContent = data.ncm_next_refresh_at || '-';

        document.getElementById('statsText').textContent = '总调用次数：' + data.total_calls;

    } catch(e) { console.error(e); }

}



async function saveNcmSettings() {

    const cookie = document.getElementById('cookie').value.trim();

    const apiKey = document.getElementById('apiKey').value.trim();

    const resultEl = document.getElementById('ncmSettingResult');

    try {

        const r = await fetch(BASE + '/admin/api/config', {

            method: 'POST',

            headers: {'Content-Type': 'application/json'},

            body: JSON.stringify({cookie, api_key: apiKey})

        });

        const data = await r.json();

        if (data.code === 0) {

            resultEl.className = 'result-msg result-success';

            resultEl.textContent = '✅ 保存成功！正在检测 Cookie...';

            resultEl.style.display = 'block';

            setTimeout(() => { checkNcmCookie(); loadAll(); }, 500);

        } else {

            resultEl.className = 'result-msg result-error';

            resultEl.textContent = '❌ ' + (data.msg || '保存失败');

            resultEl.style.display = 'block';

        }

    } catch(e) {

        resultEl.className = 'result-msg result-error';

        resultEl.textContent = '❌ 请求失败: ' + e.message;

        resultEl.style.display = 'block';

    }

}



async function saveAnnouncement() {

    const announcementEl = document.getElementById('siteAnnouncement');
    const announcement = announcementEl ? announcementEl.value.trim() : '';

    const resultEl = document.getElementById('sourceAnnouncementResult') || document.getElementById('ncmSettingResult');

    try {

        const r = await fetch(BASE + '/admin/api/config', {

            method: 'POST',

            headers: {'Content-Type': 'application/json'},

            body: JSON.stringify({announcement})

        });

        const data = await r.json();

        if (data.code === 0) {

            resultEl.className = 'result-msg result-success';

            resultEl.textContent = '公告保存成功';

        } else {

            resultEl.className = 'result-msg result-error';

            resultEl.textContent = data.msg || '公告保存失败';

        }

    } catch (e) {

        resultEl.className = 'result-msg result-error';

        resultEl.textContent = '网络错误：' + e.message;

    }

}



async function checkNcmCookie() {

    const resultEl = document.getElementById('ncmSettingResult');

    try {

        const r = await fetch(BASE + '/admin/api/check-cookie', {method: 'POST'});

        const data = await r.json();

        resultEl.className = 'result-msg ' + (data.cookie_status === 'ok' ? 'result-success' : 'result-error');

        resultEl.textContent = data.cookie_status === 'ok' ? 

            '✅ Cookie 有效！欢迎 ' + data.nickname : 

            '❌ Cookie 无效: ' + (data.error || '');

        resultEl.style.display = 'block';

        await loadNcmStatus();

    } catch(e) {

        resultEl.className = 'result-msg result-error';

        resultEl.textContent = '❌ 请求失败: ' + e.message;

        resultEl.style.display = 'block';

    }

}



async function checkQishui() {

    const resultEl = document.getElementById('qishuiCheckResult');

    if (resultEl) {

        resultEl.className = 'result-msg result-success';

        resultEl.textContent = '正在重新检测汽水会话...';

        resultEl.style.display = 'block';

    }

    try {

        const response = await fetch(BASE + '/admin/api/check-qishui', {

            method: 'POST',

            cache: 'no-store',

        });

        let data = {};

        try { data = await response.json(); } catch (_) {}

        if (!response.ok || data.code !== 0) {

            throw new Error(data.msg || data.error || `检测失败（${response.status}）`);

        }

        if (resultEl) {

            resultEl.className = 'result-msg result-success';

            resultEl.textContent = '✅ ' + (data.msg || '汽水会话检测成功');

        }

    } catch (e) {

        if (resultEl) {

            resultEl.className = 'result-msg result-error';

            resultEl.textContent = '❌ ' + (e.message || '汽水会话检测失败');

        }

    } finally {

        await loadQishuiStatus();

    }

}


async function loadQishuiStatus() {

    try {

        const r = await fetch(BASE + '/admin/api/qishui-status', {cache: 'no-store'});

        const data = await r.json();

        const status = data.qishui_status || 'empty';

        const badge = document.getElementById('qishuiStatus');

        if (badge) { badge.textContent = status === 'ok' ? '有效' : status === 'empty' ? '未设置' : status === 'invalid' ? '已失效' : '待检测'; badge.className = 'status-badge status-' + status; }

        const checked = document.getElementById('qishuiCheckedAt'); if (checked) checked.textContent = data.qishui_checked_at ? '检测时间：' + data.qishui_checked_at : '';

        const error = document.getElementById('qishuiLastError'); if (error) error.textContent = data.qishui_last_error || '';

        const bridge = document.getElementById('qishuiBridgeStatus'); if (bridge) { bridge.textContent = data.qishui_bridge_available ? '授权桥已运行' : '授权桥待启动'; bridge.style.color = data.qishui_bridge_available ? '#2e7d32' : '#9e9e9e'; }

        const refreshed = document.getElementById('qishuiRefreshedAt'); if (refreshed) refreshed.textContent = data.qishui_session_refreshed_at || '-';

        const api = document.getElementById('apiUrlQishuiDetail'); if (api) api.textContent = BASE + '/api/wxmusic?source=qishui&keyword=歌名' + (data.api_key ? '&key=' + data.api_key : '');

    } catch (e) { console.error(e); }

}



// ========== Kugou ==========

async function loadKugouStatus() {

    try {

        const r = await fetch(BASE + '/admin/api/kugou-status');

        const data = await r.json();

        const badge = document.getElementById('kugouStatus');

        badge.textContent = data.kugou_status === 'ok' ? '✅ 有效' : data.kugou_status === 'invalid' ? '❌ 已失效' : data.kugou_status === 'empty' ? '⚠️ 未设置' : '❓ 未知';

        badge.className = 'status-badge status-' + (data.kugou_status || 'unknown');

        document.getElementById('kugouCheckedAt').textContent = data.kugou_checked_at ? '检测时间：' + data.kugou_checked_at : '';

        document.getElementById('kugouLastError').textContent = data.kugou_last_error || '';

        document.getElementById('kugouUserId').textContent = data.userid || '-';

        document.getElementById('kugouSessionType').textContent = data.session_type === 'web' ? 'Web Cookie' : data.session_type === 'app' ? 'App Token' : '-';

        const kgRefreshed = document.getElementById('kugouRefreshedAt');

        if (kgRefreshed) kgRefreshed.textContent = data.kugou_refreshed_at || '-';

        const kgNext = document.getElementById('kugouNextRefreshAt');

        if (kgNext) kgNext.textContent = data.auto_renew_supported ? (data.kugou_next_refresh_at || '-') : '不适用';

        const kgHint = document.getElementById('kugouRenewHint');

        if (kgHint) kgHint.textContent = data.kugou_status === 'empty' ? '尚未配置酷狗会话。' : (data.auto_renew_supported ? '当前是 App 会话，可走官方 /v5/login_by_token 自动续期。' : '当前是网页 Cookie，官方刷新接口不支持，仅做到点健康检查；如需自动续期请改用扫码授权。');

        document.getElementById('apiUrlKugouDetail').textContent = BASE + '/api/wxmusic?source=kugou&keyword=歌名' + (data.api_key ? '&key=' + data.api_key : '');

    } catch (error) { console.error(error); }

}



async function saveKugouCookie() {

    const cookie = document.getElementById('kugouCookie').value.trim();

    const resultEl = document.getElementById('kugouSaveResult');

    if (!cookie) { resultEl.className = 'result-msg result-error'; resultEl.textContent = '请输入 Cookie'; resultEl.style.display = 'block'; return; }

    resultEl.className = 'result-msg result-success'; resultEl.textContent = '正在保存并检测...'; resultEl.style.display = 'block';

    try {

        const r = await fetch(BASE + '/admin/api/save-kugou-cookie', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({kugou_cookie: cookie})});

        const data = await r.json();

        resultEl.className = 'result-msg ' + (data.code === 0 ? 'result-success' : 'result-error');

        resultEl.textContent = data.code === 0 ? '✅ ' + data.msg : '❌ ' + (data.msg || '保存失败');

        await loadKugouStatus();

    } catch (error) { resultEl.className = 'result-msg result-error'; resultEl.textContent = '❌ ' + error.message; }

}



async function checkKugou() {

    const resultEl = document.getElementById('kugouSaveResult');

    try {

        const r = await fetch(BASE + '/admin/api/check-kugou', {method: 'POST'}); const data = await r.json();

        resultEl.className = 'result-msg ' + (data.kugou_status === 'ok' ? 'result-success' : 'result-error');

        resultEl.textContent = data.kugou_status === 'ok' ? '✅ Cookie 有效' : '❌ ' + (data.error || 'Cookie 检测失败'); resultEl.style.display = 'block';

        await loadKugouStatus();

    } catch (error) { resultEl.className = 'result-msg result-error'; resultEl.textContent = '❌ ' + error.message; resultEl.style.display = 'block'; }

}



async function loadKugouConceptStatus() {

    try {

        const response = await fetch(BASE + '/admin/api/kugou-concept-status', {cache: 'no-store'});

        const data = await response.json();

        const status = data.kugou_concept_status || 'unknown';

        const badge = document.getElementById('kugouConceptStatus');

        if (badge) {

            badge.textContent = status === 'ok' ? '✅ 有效' : status === 'invalid' ? '❌ 已失效' : status === 'empty' ? '⚠️ 未授权' : '❓ 待检测';

            badge.className = 'status-badge status-' + status;

        }

        const checkedAt = document.getElementById('kugouConceptCheckedAt');

        if (checkedAt) checkedAt.textContent = data.kugou_concept_checked_at ? '检测时间：' + data.kugou_concept_checked_at : '';

        const lastError = document.getElementById('kugouConceptLastError');

        if (lastError) lastError.textContent = data.kugou_concept_last_error || '';

        const userId = document.getElementById('kugouConceptUserId');

        if (userId) userId.textContent = data.userid || '-';

        const priority = document.getElementById('kugouPlayerPriority');

        if (priority) priority.value = data.priority === 'standard_first' ? 'standard_first' : 'concept_first';

        const conceptRefreshed = document.getElementById('kugouConceptRefreshedAt');

        if (conceptRefreshed) conceptRefreshed.textContent = data.kugou_concept_refreshed_at || '-';

        const schedulerAt = document.getElementById('kugouConceptSchedulerAt');

        if (schedulerAt) schedulerAt.textContent = data.kugou_concept_checked_by_scheduler_at || '-';

        const nextCheckAt = document.getElementById('kugouConceptNextCheckAt');

        if (nextCheckAt) nextCheckAt.textContent = data.kugou_concept_next_check_at || '-';

    } catch (error) { console.error(error); }

}



async function checkKugouConcept() {

    const resultEl = document.getElementById('kugouConceptResult');

    resultEl.className = 'result-msg result-success'; resultEl.textContent = '正在检测概念版会话...'; resultEl.style.display = 'block';

    try {

        const response = await fetch(BASE + '/admin/api/check-kugou-concept', {method: 'POST', cache: 'no-store'});

        const data = await response.json();

        const valid = data.kugou_concept_status === 'ok';

        resultEl.className = 'result-msg ' + (valid ? 'result-success' : 'result-error');

        resultEl.textContent = valid ? '✅ 酷狗概念版会话有效' : '❌ ' + (data.error || '概念版会话检测失败');

        await loadKugouConceptStatus();

    } catch (error) { resultEl.className = 'result-msg result-error'; resultEl.textContent = '❌ ' + error.message; }

}



async function saveKugouPriority() {

    const resultEl = document.getElementById('kugouPriorityResult');

    const priority = document.getElementById('kugouPlayerPriority').value;

    try {

        const response = await fetch(BASE + '/admin/api/kugou-player-priority', {

            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({priority})

        });

        const data = await response.json();

        const saved = response.ok && data.code === 0;

        resultEl.className = 'result-msg ' + (saved ? 'result-success' : 'result-error');

        resultEl.textContent = saved ? '✅ 已保存：' + (priority === 'concept_first' ? '酷狗概念版优先' : '普通酷狗优先') : '❌ ' + (data.msg || '保存失败');

        resultEl.style.display = 'block';

        if (saved) await loadKugouConceptStatus();

    } catch (error) { resultEl.className = 'result-msg result-error'; resultEl.textContent = '❌ ' + error.message; resultEl.style.display = 'block'; }

}



// ========== QQ音乐 ==========

async function loadQQStatus() {

    try {

        const r = await fetch(BASE + '/admin/api/qq-status');

        const data = await r.json();

        

        // 状态徽章

        const badge = document.getElementById('qqStatus');

        badge.textContent = data.qq_status === 'ok' ? '✅ 有效' : 

                          data.qq_status === 'expired' ? '❌ 已失效' : 

                          data.qq_status === 'empty' ? '⚠️ 未设置' : '❓ 未知';

        badge.className = 'status-badge status-' + (data.qq_status || 'unknown');

        

        document.getElementById('qqCheckedAt').textContent = data.qq_checked_at ? '检测时间：' + data.qq_checked_at : '';

        document.getElementById('qqLastError').textContent = data.qq_last_error || '';

        document.getElementById('qqRefreshedAt').textContent = data.qq_refreshed_at || '-';

        document.getElementById('qqNextRefreshAt').textContent = data.qq_next_refresh_at || '-';

        

        // 展示已提取字段

        document.getElementById('field-musickey').textContent = data.qq_musickey ? data.qq_musickey.substring(0, 20) + '...' : '-';

        document.getElementById('field-uin').textContent = data.qq_uin || '-';

        document.getElementById('field-refresh').textContent = data.qq_refresh_token ? data.qq_refresh_token.substring(0, 20) + '...' : '-';

        document.getElementById('field-access').textContent = data.qq_access_token ? data.qq_access_token.substring(0, 20) + '...' : '-';

        document.getElementById('field-openid').textContent = data.qq_openid || '-';

        document.getElementById('field-unionid').textContent = data.qq_unionid || '-';

        document.getElementById('field-euin').textContent = data.euin || '-';

        

        // 如果有字段数据则显示字段卡片

        if (data.qq_musickey || data.qq_uin) {

            document.getElementById('qqFieldsCard').style.display = 'block';

        }

        

        // API URL

        document.getElementById('apiUrlQQDetail').textContent = BASE + '/api/wxmusic?source=qq&keyword=歌名' + (data.api_key ? '&key=' + data.api_key : '');

        if (document.getElementById('apiUrlThirdDetail')) {

            document.getElementById('apiUrlThirdDetail').textContent = BASE + '/api/wxmusic?source=third&keyword=歌名' + (data.api_key ? '&key=' + data.api_key : '');

        }

    } catch(e) { console.error(e); }

}



async function saveQQCookie() {

    const qqCookie = document.getElementById('qqCookie').value.trim();

    const resultEl = document.getElementById('qqSaveResult');

    if (!qqCookie) {

        resultEl.className = 'result-msg result-error';

        resultEl.textContent = '❌ 请输入 Cookie';

        resultEl.style.display = 'block';

        return;

    }

    try {

        const r = await fetch(BASE + '/admin/api/save-qq-cookie', {

            method: 'POST',

            headers: {'Content-Type': 'application/json'},

            body: JSON.stringify({qq_cookie: qqCookie})

        });

        const data = await r.json();

        if (data.code === 0) {

            resultEl.className = 'result-msg result-success';

            resultEl.textContent = '✅ ' + data.msg;

            resultEl.style.display = 'block';

            await loadQQStatus();

        } else {

            resultEl.className = 'result-msg result-error';

            resultEl.textContent = '❌ ' + (data.msg || '保存失败');

            resultEl.style.display = 'block';

        }

    } catch(e) {

        resultEl.className = 'result-msg result-error';

        resultEl.textContent = '❌ 请求失败: ' + e.message;

        resultEl.style.display = 'block';

    }

}



async function renewKugou() {

    const resultEl = document.getElementById('kugouSaveResult');

    resultEl.className = 'result-msg result-success';

    resultEl.textContent = '⏳ 正在续期酷狗会话...';

    resultEl.style.display = 'block';

    try {

        const r = await fetch(BASE + '/admin/api/renew-kugou', {method: 'POST'});

        const data = await r.json();

        resultEl.className = 'result-msg ' + (data.code === 0 ? 'result-success' : 'result-error');

        resultEl.textContent = (data.code === 0 ? '✅ ' : '❌ ') + (data.msg || '');

        resultEl.style.display = 'block';

        await loadKugouStatus();

    } catch (e) {

        resultEl.className = 'result-msg result-error';

        resultEl.textContent = '❌ 请求失败: ' + e.message;

        resultEl.style.display = 'block';

    }

}





async function renewKugouConcept() {

    const resultEl = document.getElementById('kugouConceptResult');

    resultEl.className = 'result-msg result-success';

    resultEl.textContent = '⏳ 正在续期酷狗概念版会话...';

    resultEl.style.display = 'block';

    try {

        const r = await fetch(BASE + '/admin/api/renew-kugou-concept', {method: 'POST'});

        const data = await r.json();

        resultEl.className = 'result-msg ' + (data.code === 0 ? 'result-success' : 'result-error');

        resultEl.textContent = (data.code === 0 ? '✅ ' : '❌ ') + (data.msg || '');

        resultEl.style.display = 'block';

        await loadKugouConceptStatus();

    } catch (e) {

        resultEl.className = 'result-msg result-error';

        resultEl.textContent = '❌ 请求失败: ' + e.message;

        resultEl.style.display = 'block';

    }

}





async function renewMigu() {

    const resultEl = document.getElementById('miguRenewResult');

    resultEl.className = 'result-msg result-success';

    resultEl.textContent = '⏳ 正在续期咪咕会话...';

    resultEl.style.display = 'block';

    try {

        const r = await fetch(BASE + '/admin/api/renew-migu', {method: 'POST'});

        const data = await r.json();

        resultEl.className = 'result-msg ' + (data.code === 0 ? 'result-success' : 'result-error');

        resultEl.textContent = (data.code === 0 ? '✅ ' : '❌ ') + (data.msg || '');

        resultEl.style.display = 'block';

        await loadMiguStatus();

    } catch (e) {

        resultEl.className = 'result-msg result-error';

        resultEl.textContent = '❌ 请求失败: ' + e.message;

        resultEl.style.display = 'block';

    }

}





async function renewNcm() {

    const resultEl = document.getElementById('ncmRenewResult');

    resultEl.className = 'result-msg result-success';

    resultEl.textContent = '⏳ 正在续期网易云会话...';

    resultEl.style.display = 'block';

    try {

        const r = await fetch(BASE + '/admin/api/renew-ncm', {method: 'POST'});

        const data = await r.json();

        resultEl.className = 'result-msg ' + (data.code === 0 ? 'result-success' : 'result-error');

        resultEl.textContent = (data.code === 0 ? '✅ ' : '❌ ') + (data.msg || '');

        resultEl.style.display = 'block';

        await loadNcmStatus();

    } catch (e) {

        resultEl.className = 'result-msg result-error';

        resultEl.textContent = '❌ 请求失败: ' + e.message;

        resultEl.style.display = 'block';

    }

}





const RENEW_INTERVAL_INPUTS = {

    migu_renew_interval_hours: {input: 'miguRenewInterval', range: 'miguIntervalRange'},

    qq_renew_interval_hours: {input: 'qqRenewInterval', range: 'qqIntervalRange'},

    ncm_renew_interval_hours: {input: 'ncmRenewInterval', range: 'ncmIntervalRange'},

    kugou_concept_check_interval_hours: {input: 'kugouConceptCheckInterval', range: 'kugouConceptIntervalRange'},

    kugou_renew_interval_hours: {input: 'kugouRenewInterval', range: 'kugouIntervalRange'},

};





async function loadRenewSettings() {

    try {

        const r = await fetch(BASE + '/admin/api/renew-settings', {cache: 'no-store'});

        const data = await r.json();

        const toggle = document.getElementById('autoRenewEnabled');

        if (toggle) toggle.checked = data.auto_renew_enabled !== false;

        Object.keys(RENEW_INTERVAL_INPUTS).forEach(key => {

            const meta = RENEW_INTERVAL_INPUTS[key];

            const input = document.getElementById(meta.input);

            const limit = (data.limits || {})[key] || {};

            if (input) {

                if (limit.min !== undefined) input.min = limit.min;

                if (limit.max !== undefined) input.max = limit.max;

                input.value = (data.intervals || {})[key] !== undefined ? data.intervals[key] : (limit.default || '');

            }

            const range = document.getElementById(meta.range);

            if (range && limit.min !== undefined) range.textContent = '（允许 ' + limit.min + '-' + limit.max + '，默认 ' + limit.default + '）';

        });

    } catch (e) { console.error(e); }

}





async function saveRenewSettings() {

    const resultEl = document.getElementById('renewSettingsResult');

    const toggle = document.getElementById('autoRenewEnabled');

    const body = {auto_renew_enabled: toggle ? toggle.checked : true};

    Object.keys(RENEW_INTERVAL_INPUTS).forEach(key => {

        const input = document.getElementById(RENEW_INTERVAL_INPUTS[key].input);

        if (input && input.value !== '') body[key] = Number(input.value);

    });

    resultEl.className = 'result-msg result-success';

    resultEl.textContent = '正在保存...';

    resultEl.style.display = 'block';

    try {

        const r = await fetch(BASE + '/admin/api/renew-settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});

        const data = await r.json();

        if (data.code === 0) {

            resultEl.className = 'result-msg result-success';

            resultEl.textContent = '✅ 已保存，下次续期时间已按新间隔重排';

            await loadRenewSettings();

            await loadNcmStatus();

            await loadKugouStatus();

            await loadQishuiStatus();

        } else {

            resultEl.className = 'result-msg result-error';

            resultEl.textContent = '❌ ' + (data.msg || '保存失败');

        }

        resultEl.style.display = 'block';

    } catch (e) {

        resultEl.className = 'result-msg result-error';

        resultEl.textContent = '❌ 请求失败: ' + e.message;

        resultEl.style.display = 'block';

    }

}





async function renewQQ() {

    const resultEl = document.getElementById('qqSaveResult');

    resultEl.className = 'result-msg result-success';

    resultEl.textContent = '⏳ 正在续期...';

    resultEl.style.display = 'block';

    try {

        const r = await fetch(BASE + '/admin/api/renew-qq', {method: 'POST'});

        const data = await r.json();

        if (data.code === 0) {

            resultEl.className = 'result-msg result-success';

            resultEl.textContent = '✅ ' + data.msg;

        } else {

            resultEl.className = 'result-msg result-error';

            resultEl.textContent = '❌ ' + (data.msg || '续期失败');

        }

        resultEl.style.display = 'block';

        await loadQQStatus();

    } catch(e) {

        resultEl.className = 'result-msg result-error';

        resultEl.textContent = '❌ 请求失败: ' + e.message;

        resultEl.style.display = 'block';

    }

}



async function checkQQ() {

    const resultEl = document.getElementById('qqSaveResult');

    resultEl.className = 'result-msg result-success';

    resultEl.textContent = '⏳ 正在检测...';

    resultEl.style.display = 'block';

    try {

        const r = await fetch(BASE + '/admin/api/check-qq', {method: 'POST'});

        const data = await r.json();

        if (data.qq_status === 'ok') {

            resultEl.className = 'result-msg result-success';

            resultEl.textContent = '✅ QQ音乐 Cookie 有效！' + (data.qq_nickname ? '（当前账号：' + data.qq_nickname + '）' : '');

        } else {

            resultEl.className = 'result-msg result-error';

            resultEl.textContent = '❌ ' + (data.error || '检测失败');

        }

        resultEl.style.display = 'block';

        await loadQQStatus();

    } catch(e) {

        resultEl.className = 'result-msg result-error';

        resultEl.textContent = '❌ 请求失败: ' + e.message;

        resultEl.style.display = 'block';

    }

}



// ========== 第三方音源 ==========

async function loadThirdSources() {

    try {

        const r = await fetch(BASE + '/admin/api/third-sources');

        const data = await r.json();

        renderThirdSources(

            data.sources || {},

            data.quality || '320k',

            data.third_timeout || 10,

            data.resolve_timeout || 12,

            data.cross_platform_timeout || 4,

            data.cross_platform_priority || ['netease','qq','kugou','migu','qishui'],

        );

    } catch(e) {

        console.error('load third sources error:', e);

    }

}



function scannerLine(text, color) {

    const line = document.createElement('div');

    line.textContent = text;

    if (color) line.style.color = color;

    line.style.marginTop = '6px';

    return line;

}



async function loadSourceScannerFiles() {

    const select = document.getElementById('scannerFile');

    try {

        const r = await fetch(BASE + '/admin/api/source-scanner/files');

        const data = await r.json();

        select.innerHTML = '';

        const files = data.files || [];

        if (!files.length) {

            const option = document.createElement('option');

            option.value = '';

            option.textContent = '音乐音源文件夹中没有 JS 文件';

            select.appendChild(option);

            return;

        }

        files.forEach(filename => {

            const option = document.createElement('option');

            option.value = filename;

            option.textContent = filename;

            select.appendChild(option);

        });

    } catch(e) {

        select.innerHTML = '<option value="">文件加载失败</option>';

    }

}



async function scanSourceFile() {

    const filename = document.getElementById('scannerFile').value;

    const resultEl = document.getElementById('scannerResult');

    if (!filename) return;

    resultEl.className = 'result-msg';

    resultEl.textContent = '正在静态识别候选源（实时回源仅作参考）...';

    resultEl.style.display = 'block';

    try {

        const r = await fetch(BASE + '/admin/api/source-scanner/inspect', {

            method: 'POST',

            headers: {'Content-Type': 'application/json'},

            body: JSON.stringify({filename}),

        });

        const data = await r.json();

        resultEl.innerHTML = '';

        resultEl.className = 'result-msg ' + (data.code === 0 ? 'result-success' : 'result-error');

        resultEl.style.display = 'block';

        if (data.code !== 0) {

            resultEl.textContent = '扫描失败：' + (data.msg || '未知错误');

            return;

        }



        resultEl.appendChild(scannerLine('已扫描：' + filename));

        if (data.is_lx_source) {

            resultEl.appendChild(scannerLine('LX Music 音源：已识别（' + (data.lx_signals || []).join('、') + '）', '#218838'));

        }

        (data.urls || []).forEach(url => resultEl.appendChild(scannerLine('公开地址：' + url)));

        (data.notes || []).forEach(note => resultEl.appendChild(scannerLine('提示：' + note, '#666')));

        const candidates = data.candidates || [];

        if (!candidates.length) {

            resultEl.appendChild(scannerLine('没有发现可自动接入的候选源。', '#b26a00'));

            return;

        }

        candidates.forEach(candidate => {

            const test = candidate.test || {};

            const row = document.createElement('div');

            row.style.marginTop = '10px';

            row.style.paddingTop = '8px';

            row.style.borderTop = '1px solid #eee';

            row.appendChild(scannerLine(candidate.name + '：' + (test.ok ? '实时验证通过' : '实时验证未通过（不影响静态添加）：' + (test.msg || '外部接口暂时未返回地址')), test.ok ? '#218838' : '#b26a00'));

            {

                const button = document.createElement('button');

                button.className = 'btn btn-primary';

                button.style.marginTop = '6px';

                button.textContent = '静态识别并添加';

                button.addEventListener('click', () => applyScannedSource(filename, candidate.id));

                row.appendChild(button);

            }

            resultEl.appendChild(row);

        });

    } catch(e) {

        resultEl.className = 'result-msg result-error';

        resultEl.textContent = '扫描失败：' + e.message;

    }

}



async function applyScannedSource(filename, candidateId) {

    const resultEl = document.getElementById('scannerResult');

    resultEl.className = 'result-msg';

    resultEl.textContent = '正在重新识别并写入配置...';

    resultEl.style.display = 'block';

    try {

        const r = await fetch(BASE + '/admin/api/source-scanner/apply', {

            method: 'POST',

            headers: {'Content-Type': 'application/json'},

            body: JSON.stringify({filename, candidate_id: candidateId}),

        });

        const data = await r.json();

        resultEl.className = 'result-msg ' + (data.code === 0 ? 'result-success' : 'result-error');

        resultEl.textContent = (data.code === 0 ? '已添加并启用：' : '添加失败：') + (data.msg || '');

        resultEl.style.display = 'block';

        if (data.code === 0) await loadThirdSources();

    } catch(e) {

        resultEl.className = 'result-msg result-error';

        resultEl.textContent = '添加失败：' + e.message;

        resultEl.style.display = 'block';

    }

}



async function uploadAndScanSourceFile() {

    const input = document.getElementById('scannerUploadFile');

    const resultEl = document.getElementById('scannerUploadResult');

    const file = input.files && input.files[0];

    if (!file) {

        resultEl.className = 'result-msg result-error';

        resultEl.textContent = '请选择一个 JS 音源文件。';

        resultEl.style.display = 'block';

        return;

    }

    if (!file.name.toLowerCase().endsWith('.js')) {

        resultEl.className = 'result-msg result-error';

        resultEl.textContent = '只允许上传 JS 音源文件。';

        resultEl.style.display = 'block';

        return;

    }

    if (file.size > 2 * 1024 * 1024) {

        resultEl.className = 'result-msg result-error';

        resultEl.textContent = '文件不能超过 2MB。';

        resultEl.style.display = 'block';

        return;

    }



    resultEl.className = 'result-msg';

    resultEl.textContent = '正在静态识别并保存兼容适配器...';

    resultEl.style.display = 'block';

    try {

        const dataUrl = await new Promise((resolve, reject) => {

            const reader = new FileReader();

            reader.onload = () => resolve(reader.result);

            reader.onerror = () => reject(new Error('无法读取文件'));

            reader.readAsDataURL(file);

        });

        const contentBase64 = String(dataUrl).split(',', 2)[1];

        const r = await fetch(BASE + '/admin/api/source-scanner/upload', {

            method: 'POST',

            headers: {'Content-Type': 'application/json'},

            body: JSON.stringify({filename: file.name, content_base64: contentBase64}),

        });

        const data = await r.json();

        resultEl.innerHTML = '';

        resultEl.className = 'result-msg ' + (data.code === 0 ? 'result-success' : 'result-error');

        resultEl.appendChild(scannerLine(data.code === 0 ? '上传完成：' + (data.msg || file.name) : '未上传：' + (data.msg || '验证未通过')));

        (data.candidates || []).forEach(candidate => {

            const test = candidate.test || {};

            resultEl.appendChild(scannerLine(candidate.name + '：' + (test.ok ? '实时验证通过' : '实时验证未通过（不影响保存）：' + (test.msg || '外部接口暂时未返回地址')), test.ok ? '#218838' : '#b26a00'));

        });

        (data.notes || []).forEach(note => resultEl.appendChild(scannerLine('提示：' + note, '#666')));

        resultEl.style.display = 'block';

        if (data.code === 0) {

            input.value = '';

            await loadSourceScannerFiles();

            await loadThirdSources();

        }

    } catch(e) {

        resultEl.className = 'result-msg result-error';

        resultEl.textContent = '上传失败：' + e.message;

        resultEl.style.display = 'block';

    }

}



function renderThirdSources(sources, quality, thirdTimeout, resolveTimeout, crossPlatformTimeout, crossPlatformPriority) {

    const container = document.getElementById('thirdSourcesList');

    const select = document.getElementById('testSourceSelect');

    container.innerHTML = '';

    select.innerHTML = '<option value="">-- 选择音源 --</option>';

    document.getElementById('thirdQuality').value = quality;

    document.getElementById('thirdSourceTimeout').value = thirdTimeout;

    document.getElementById('playerResolveTimeout').value = resolveTimeout;

    document.getElementById('crossPlatformTimeout').value = crossPlatformTimeout;

    // set priority dropdown
    if (crossPlatformPriority && crossPlatformPriority.length) {
        const key = crossPlatformPriority.join(',');
        const sel = document.getElementById('crossPlatformPriority');
        if (sel) {
            let found = false;
            for (const opt of sel.options) { if (opt.value === key) { sel.value = key; found = true; break; } }
            if (!found) {
                const names = {netease: '网易云', qq: 'QQ', kugou: '酷狗', migu: '咪咕', qishui: '汽水'};
                const option = document.createElement('option');
                option.value = key;
                option.textContent = crossPlatformPriority.map(item => names[item] || item).join(' → ');
                sel.appendChild(option);
                sel.value = key;
            }
        }
    }



    for (const [id, src] of Object.entries(sources)) {

        // 列表项

        const div = document.createElement('div');

        div.className = 'third-source-item';

        const builtin = src.builtin ? ' [内置]' : '';

        const sourceTypeName = src.source_type === 'lx_script' ? 'LX Music 音源' : '';

        const status = src.enabled ? '🟢 已启用' : '🔴 已禁用';

        const usageLimit = Number.isInteger(Number(src.usage_limit)) ? Number(src.usage_limit) : 0;

        const usageCount = Math.max(0, Number(src.usage_count) || 0);

        const usageRemaining = src.usage_unlimited ? '无限' : String(Math.max(0, Number(src.usage_remaining) || 0));

        const usageSummary = src.usage_unlimited ? '无限次' : (usageRemaining + ' / ' + usageLimit + ' 次');

        div.innerHTML = '<div style="display:flex;align-items:center;justify-content:space-between;gap:12px;">' +

            '<div style="min-width:0;flex:1;">' +

            '<label class="switch-label">' +

            '<input type="checkbox" class="source-toggle" data-id="' + id + '" ' + (src.enabled ? 'checked' : '') + '> ' +

            '<strong>' + (src.name || id) + '</strong><span style="color:#888;font-size:12px;">' + builtin + '</span>' +

            (sourceTypeName ? '<span style="color:#3867d6;font-size:12px;margin-left:8px;">' + sourceTypeName + '</span>' : '') +

            '<span style="color:#666;font-size:12px;margin-left:8px;">' + (src.desc || '') + '</span>' +

            '</label>' +

            '<div style="font-size:11px;color:#999;margin-top:2px;margin-left:28px;">' + status + ' | ' + (src.api_url || '') + '</div>' +

            '<div style="display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin:8px 0 0 28px;font-size:12px;color:#666;">' +

            '<label>调用上限：<input type="number" class="source-usage-limit" data-id="' + id + '" value="' + usageLimit + '" min="0" max="10000000" step="1" style="width:110px;"> <span>（0=无限）</span></label>' +

            '<span>已用：' + usageCount + ' 次</span><span>剩余：' + usageSummary + '</span>' +

            '<button type="button" class="btn btn-secondary btn-sm" data-reset-usage-id="' + id + '" style="padding:3px 8px;font-size:12px;">↺ 重置用量</button>' +

            '</div>' +

            '</div>' +

            (src.builtin ? '' : '<button class="btn btn-danger btn-sm" data-delete-id="' + id + '" style="padding:4px 10px;font-size:12px;">🗑️ 删除</button>') +

            '</div>';

        container.appendChild(div);



        // 测试下拉只保存源 ID，真实 API Key 留在后端配置中。

        const opt = document.createElement('option');

        opt.value = id;

        opt.dataset.sourceId = id;

        opt.textContent = (src.name || id) + builtin;

        select.appendChild(opt);

    }



    // 给删除按钮绑事件（用 data 属性避免内联 JS 引号问题）

    container.querySelectorAll('[data-delete-id]').forEach(btn => {

        btn.addEventListener('click', function() {

            deleteThirdSource(this.dataset.deleteId);

        });

    });

    container.querySelectorAll('[data-reset-usage-id]').forEach(btn => {

        btn.addEventListener('click', function() {

            resetThirdSourceUsage(this.dataset.resetUsageId);

        });

    });

}



async function saveThirdSources() {

    return saveSourceSettings('thirdSaveResult');

}



async function savePlaybackSettings() {

    return saveSourceSettings('sourceSettingsResult');

}



async function saveSourceSettings(resultId) {

    const resultEl = document.getElementById(resultId) || document.getElementById('sourceSettingsResult');

    const toggles = document.querySelectorAll('.source-toggle');

    const enabled = {};

    toggles.forEach(t => { enabled[t.dataset.id] = t.checked; });

    const quality = document.getElementById('thirdQuality').value;

    const thirdTimeout = Number.parseInt(document.getElementById('thirdSourceTimeout').value, 10);

    const resolveTimeout = Number.parseInt(document.getElementById('playerResolveTimeout').value, 10);

    const crossPlatformTimeout = Number.parseInt(document.getElementById('crossPlatformTimeout').value, 10);

    const usage_limits = {};

    let usageError = '';

    document.querySelectorAll('.source-usage-limit').forEach(input => {

        const value = Number.parseInt(input.value, 10);

        if (!Number.isInteger(value) || value < 0 || value > 10000000) {

            usageError = '调用次数上限必须是 0 到 10000000 的整数，0 表示无限。';

        } else {

            usage_limits[input.dataset.id] = value;

        }

    });

    if (usageError) {

        resultEl.className = 'result-msg result-error';

        resultEl.textContent = '❌ ' + usageError;

        resultEl.style.display = 'block';

        return;

    }

    if (!Number.isInteger(thirdTimeout) || thirdTimeout < 3 || thirdTimeout > 60) {

        resultEl.className = 'result-msg result-error';

        resultEl.textContent = '❌ 第三方回源等待上限需为 3 到 60 秒。';

        resultEl.style.display = 'block';

        return;

    }

    if (!Number.isInteger(resolveTimeout) || resolveTimeout < 5 || resolveTimeout > 75) {

        resultEl.className = 'result-msg result-error';

        resultEl.textContent = '❌ 自动播放总等待上限需为 5 到 75 秒。';

        resultEl.style.display = 'block';

        return;

    }

    if (!Number.isInteger(crossPlatformTimeout) || crossPlatformTimeout < 1 || crossPlatformTimeout > 30) {

        resultEl.className = 'result-msg result-error';

        resultEl.textContent = '❌ 跨平台官方补源等待上限需为 1 到 30 秒。';

        resultEl.style.display = 'block';

        return;

    }

    try {

        const r = await fetch(BASE + '/admin/api/third-sources/save', {

            method: 'POST',

            headers: {'Content-Type': 'application/json'},

            body: JSON.stringify({

                enabled,

                quality,

                third_timeout: thirdTimeout,

                resolve_timeout: resolveTimeout,

                cross_platform_timeout: crossPlatformTimeout,
                cross_platform_priority: document.getElementById('crossPlatformPriority').value.split(','),
                usage_limits,

            }),

        });

        const data = await r.json();

        if (r.ok && data.code === 0) {

            resultEl.className = 'result-msg result-success';

            resultEl.textContent = '✅ ' + (data.msg || '保存成功');

            await loadThirdSources();

        } else {

            resultEl.className = 'result-msg result-error';

            resultEl.textContent = '❌ ' + (data.msg || '保存失败');

        }

        resultEl.style.display = 'block';

    } catch(e) {

        resultEl.className = 'result-msg result-error';

        resultEl.textContent = '❌ ' + e.message;

        resultEl.style.display = 'block';

    }

}



function onSrcTypeChange() {

    const type = document.getElementById('newSrcType').value;

    const show = type === 'generic';

    document.getElementById('genericFields').style.display = show ? '' : 'none';

    document.getElementById('genericPatternGroup').style.display = show ? '' : 'none';

    document.getElementById('genericMoreFields').style.display = show ? '' : 'none';

}



async function addThirdSource() {

    const resultEl = document.getElementById('thirdAddResult');

    const sourceType = document.getElementById('newSrcType').value;

    const payload = {

        id: document.getElementById('newSrcId').value.trim(),

        name: document.getElementById('newSrcName').value.trim(),

        desc: document.getElementById('newSrcDesc').value.trim(),

        api_url: document.getElementById('newSrcApiUrl').value.trim(),

        source_type: sourceType,

    };

    

    if (sourceType === 'generic') {

        payload.api_key = document.getElementById('newSrcApiKey').value.trim();

        payload.method = document.getElementById('newSrcMethod').value;

        payload.url_pattern = document.getElementById('newSrcPattern').value.trim();

        payload.auth_header = document.getElementById('newSrcAuth').value.trim();

        payload.code_field = document.getElementById('newSrcCodeField').value.trim();

        payload.code_value = parseInt(document.getElementById('newSrcCodeValue').value) || 0;

        payload.url_field = document.getElementById('newSrcUrlField').value.trim();

        const sourcesInput = document.getElementById('newSrcSources').value.trim();

        if (sourcesInput) {

            payload.sources = sourcesInput.split(',').map(s => s.trim()).filter(Boolean);

        } else {

            payload.sources = [];

        }

    }



    if (!payload.id) { alert('请填写源标识'); return; }

    if (!payload.name) { alert('请填写名称'); return; }

    if (!payload.api_url) { alert('请填写 API 地址'); return; }

    if (sourceType === 'generic' && !payload.url_pattern) { alert('请填写 URL 路径模板'); return; }



    resultEl.className = 'result-msg';

    resultEl.textContent = '⏳ 添加并测试中...';

    resultEl.style.display = 'block';



    try {

        const addR = await fetch(BASE + '/admin/api/third-sources/add', {

            method: 'POST',

            headers: {'Content-Type': 'application/json'},

            body: JSON.stringify(payload),

        });

        const addData = await addR.json();

        if (addData.code !== 0) {

            throw new Error(addData.msg || '验证未通过，未添加');

        }

        resultEl.className = 'result-msg result-success';

        resultEl.textContent = '✅ ' + (addData.msg || '验证通过，已添加并启用');

        resultEl.style.display = 'block';

        await loadThirdSources();

        // 清空表单

        document.getElementById('newSrcId').value = '';

        document.getElementById('newSrcName').value = '';

        document.getElementById('newSrcDesc').value = '';

        document.getElementById('newSrcApiUrl').value = '';

        document.getElementById('newSrcApiKey').value = '';

    } catch(e) {

        resultEl.className = 'result-msg result-error';

        resultEl.textContent = '❌ ' + e.message;

        resultEl.style.display = 'block';

    }

}



async function resetThirdSourceUsage(id) {

    const resultEl = document.getElementById('thirdSaveResult');

    try {

        const r = await fetch(BASE + '/admin/api/third-sources/reset-usage', {

            method: 'POST',

            headers: {'Content-Type': 'application/json'},

            body: JSON.stringify({id}),

        });

        const data = await r.json();

        resultEl.className = 'result-msg ' + (r.ok && data.code === 0 ? 'result-success' : 'result-error');

        resultEl.textContent = (r.ok && data.code === 0 ? '✅ ' : '❌ ') + (data.msg || '操作失败');

        resultEl.style.display = 'block';

        if (r.ok && data.code === 0) await loadThirdSources();

    } catch(e) {

        resultEl.className = 'result-msg result-error';

        resultEl.textContent = '❌ ' + e.message;

        resultEl.style.display = 'block';

    }

}



async function deleteThirdSource(id) {

    if (!confirm('确定删除音源 "' + id + '" 吗？')) return;

    const resultEl = document.getElementById('thirdSaveResult');

    try {

        const r = await fetch(BASE + '/admin/api/third-sources/delete', {

            method: 'POST',

            headers: {'Content-Type': 'application/json'},

            body: JSON.stringify({id}),

        });

        const data = await r.json();

        resultEl.className = 'result-msg result-success';

        resultEl.textContent = '✅ ' + (data.msg || '已删除');

        resultEl.style.display = 'block';

        await loadThirdSources();

    } catch(e) {

        resultEl.className = 'result-msg result-error';

        resultEl.textContent = '❌ ' + e.message;

        resultEl.style.display = 'block';

    }

}



async function testThirdSource() {

    const resultEl = document.getElementById('thirdTestResult');

    const sel = document.getElementById('testSourceSelect');

    const opt = sel.options[sel.selectedIndex];

    if (!opt || !opt.value) return;

    const sourceId = opt.dataset.sourceId || opt.value;

    if (!sourceId) return;



    resultEl.className = 'result-msg';

    resultEl.textContent = '⏳ 测试中...';

    resultEl.style.display = 'block';

    try {

        const r = await fetch(BASE + '/admin/api/test-third-source', {

            method: 'POST',

            headers: {'Content-Type': 'application/json'},

            body: JSON.stringify({source_id: sourceId}),

        });

        const data = await r.json();

        resultEl.className = 'result-msg ' + (data.ok ? 'result-success' : 'result-error');

        resultEl.textContent = data.ok ? '✅ 音源可用（' + (data.msg || '') + '）' : '❌ 音源不可用（' + (data.msg || '') + '）';

    } catch(e) {

        resultEl.className = 'result-msg result-error';

        resultEl.textContent = '❌ ' + e.message;

        resultEl.style.display = 'block';

    }

}



// ========== 共用功能 ==========

async function loadLogs() {

    try {

        const r = await fetch(BASE + '/admin/api/logs');

        const logs = await r.json();

        const tbody = document.getElementById('logBody');

        if (!logs.length) {

            tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#999;">暂无记录</td></tr>';

            return;

        }

        tbody.innerHTML = logs.slice(0, 20).map(l => 

            '<tr>' +

            '<td>' + (l.time || '').replace('T', ' ').substring(0,19) + '</td>' +

            '<td>' + (l.keyword || '') + '</td>' +

            '<td>' + (l.source || 'netease') + '</td>' +

            '<td>' + (l.result === 'ok' ? '✅ ' + l.song + ' - ' + l.artist : '❌ ' + (l.error || '无结果')) + '</td>' +

            '<td>' + (l.elapsed_ms || 0) + 'ms</td>' +

            '</tr>'

        ).join('');

    } catch(e) { console.error(e); }

}



async function testSearch() {

    const kw = document.getElementById('testKeyword').value.trim();

    const keyEl = document.getElementById('apiKey');

    const key = keyEl.value.trim();

    const source = document.getElementById('testSource').value;

    const resultEl = document.getElementById('testResult');

    const xmlEl = document.getElementById('testXml');

    

    if (!kw) {

        resultEl.className = 'result-msg result-error';

        resultEl.textContent = '❌ 请输入歌名';

        resultEl.style.display = 'block';

        return;

    }

    

    try {

        const url = BASE + '/api/wxmusic?keyword=' + encodeURIComponent(kw) + '&source=' + source + '&key=' + key;

        const r = await fetch(url);

        const data = await r.json();

        if (data.code === 200) {

            resultEl.className = 'result-msg result-success';

            resultEl.textContent = '✅ ' + data.title + ' - ' + data.singer + ' | 封面: ' + (data.cover ? '有' : '无') + ' | 播放: ' + (data.music_url ? '有' : '无');

            resultEl.style.display = 'block';

        } else {

            resultEl.className = 'result-msg result-error';

            resultEl.textContent = '❌ ' + (data.msg || '未找到');

            resultEl.style.display = 'block';

        }

        

        // 显示返回 JSON

        xmlEl.textContent = JSON.stringify(data, null, 2);

        xmlEl.style.display = 'block';

        

        await loadLogs();

    } catch(e) {

        resultEl.className = 'result-msg result-error';

        resultEl.textContent = '❌ ' + e.message;

        resultEl.style.display = 'block';

    }

}



const adminPasswordState = {userId: '', username: ''};

function openAdminPasswordDialog(userId, username) {

    adminPasswordState.userId = userId;

    adminPasswordState.username = username;

    const dialog = document.getElementById('adminPasswordDialog');

    const target = document.getElementById('adminPasswordTarget');

    if (!dialog || !target) return;

    target.textContent = '正在修改：' + username;

    document.getElementById('adminPasswordResult').style.display = 'none';

    document.getElementById('adminPasswordInput').value = '';

    document.getElementById('adminPasswordConfirm').value = '';

    document.getElementById('adminPasswordSubmit').disabled = false;

    if (!dialog.open) dialog.showModal();

    window.setTimeout(() => document.getElementById('adminPasswordInput').focus(), 0);

}

function closeAdminPasswordDialog() {

    const dialog = document.getElementById('adminPasswordDialog');

    if (dialog && dialog.open) dialog.close();

}

function resetAdminPasswordDialog() {

    adminPasswordState.userId = '';

    adminPasswordState.username = '';

    const result = document.getElementById('adminPasswordResult');

    if (result) result.style.display = 'none';

}

async function submitAdminPassword(event) {

    event.preventDefault();

    const password = document.getElementById('adminPasswordInput').value;

    const repeatedPassword = document.getElementById('adminPasswordConfirm').value;

    const result = document.getElementById('adminPasswordResult');

    const submit = document.getElementById('adminPasswordSubmit');

    if (password.length < 8 || password.length > 128) {

        result.className = 'result-msg result-error';

        result.textContent = '密码长度需为 8-128 位';

        result.style.display = 'block';

        return;

    }

    if (password !== repeatedPassword) {

        result.className = 'result-msg result-error';

        result.textContent = '两次输入的密码不一致';

        result.style.display = 'block';

        return;

    }

    submit.disabled = true;

    result.className = 'result-msg';

    result.textContent = '正在保存...';

    result.style.display = 'block';

    try {

        const response = await fetch(BASE + '/admin/api/users/' + encodeURIComponent(adminPasswordState.userId) + '/password', {

            method: 'POST',

            headers: {'Content-Type': 'application/json'},

            body: JSON.stringify({password: password})

        });

        const data = await response.json();

        if (!response.ok || data.code !== 0) throw new Error(data.detail || data.msg || '密码修改失败');

        showUserManageResult(data.msg || '密码修改成功', true);

        await loadManagedUsers();

        closeAdminPasswordDialog();

    } catch (error) {

        result.className = 'result-msg result-error';

        result.textContent = error.message || '密码修改失败';

        result.style.display = 'block';

        submit.disabled = false;

    }

}



const adminQrState = { platform: '', token: '', timer: null };

const adminQrNames = { netease: '\u7f51\u6613\u4e91\u97f3\u4e50', qq: 'QQ\u97f3\u4e50', kugou: '\u9177\u72d7\u97f3\u4e50', kugou_concept: '\u9177\u72d7\u6982\u5ff5\u7248', qishui: '\u6c7d\u6c34\u97f3\u4e50' };

function stopAdminQr() { if (adminQrState.timer) window.clearTimeout(adminQrState.timer); adminQrState.timer = null; }

function closeAdminQr() { stopAdminQr(); const dialog = document.getElementById('adminQrDialog'); if (dialog && dialog.open) dialog.close(); }

function adminQrStatus(message, isError) { const element = document.getElementById('adminQrStatus'); element.textContent = message || ''; element.style.color = isError ? '#c62828' : '#666'; }

async function readAdminQrResponse(response, fallbackMessage) {

    const raw = await response.text();

    let data;

    try { data = JSON.parse(raw); }

    catch (_) { throw new Error(response.status >= 500 ? '\u670d\u52a1\u5668\u6682\u65f6\u5f02\u5e38\uff0c\u4e8c\u7ef4\u7801\u8bf7\u6c42\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5' : fallbackMessage); }

    if (!response.ok || data.code !== 0) throw new Error(data.msg || fallbackMessage);

    return data;

}

async function startAdminQr(platform) {

    stopAdminQr(); adminQrState.platform = platform; adminQrState.token = '';

    const dialog = document.getElementById('adminQrDialog');

    document.getElementById('adminQrTitle').textContent = (adminQrNames[platform] || platform) + '\u626b\u7801\u6388\u6743';

    document.getElementById('adminQrImage').removeAttribute('src'); adminQrStatus('\u6b63\u5728\u751f\u6210\u4e8c\u7ef4\u7801...'); if (!dialog.open) dialog.showModal();

    try {

        const response = await fetch(BASE + '/admin/api/qr/' + encodeURIComponent(platform) + '/start', { method: 'POST', cache: 'no-store' }); const data = await readAdminQrResponse(response, '\u65e0\u6cd5\u751f\u6210\u4e8c\u7ef4\u7801');

        if (data.code !== 0 || !data.qr_image || !data.token) throw new Error(data.msg || '\u65e0\u6cd5\u751f\u6210\u4e8c\u7ef4\u7801');

        adminQrState.token = data.token; document.getElementById('adminQrImage').src = data.qr_image;

        adminQrStatus(platform === 'qishui'

            ? '\u8bf7\u4f7f\u7528\u5df2\u767b\u5f55\u7684\u6c7d\u6c34\u97f3\u4e50 App \u626b\u7801\u5e76\u5728 App \u4e2d\u786e\u8ba4'

            : '\u8bf7\u4f7f\u7528' + (adminQrNames[platform] || platform) + '\u626b\u7801\u5e76\u5728\u624b\u673a\u786e\u8ba4'); pollAdminQr();

    } catch (error) { adminQrStatus(error.message || '\u4e8c\u7ef4\u7801\u521b\u5efa\u5931\u8d25', true); }

}

async function pollAdminQr() {

    if (!adminQrState.platform || !adminQrState.token) return;

    try {

        const response = await fetch(BASE + '/admin/api/qr/' + encodeURIComponent(adminQrState.platform) + '/' + encodeURIComponent(adminQrState.token), { cache: 'no-store' }); const data = await readAdminQrResponse(response, '\u6388\u6743\u72b6\u6001\u83b7\u53d6\u5931\u8d25');

        if (data.code !== 0) throw new Error(data.msg || '\u6388\u6743\u72b6\u6001\u83b7\u53d6\u5931\u8d25');

        const status = data.status || 'waiting'; adminQrStatus(data.message || (status === 'waiting' ? '\u7b49\u5f85\u626b\u7801\u6388\u6743' : status));

        if (status === 'authorized') { stopAdminQr(); adminQrStatus(data.message || '授权成功，已保存到全局配置'); document.getElementById('adminQrDialog').close(); if (adminQrState.platform === 'netease') await loadNcmStatus(); if (adminQrState.platform === 'qq') await loadQQStatus(); if (adminQrState.platform === 'kugou') await loadKugouStatus(); if (adminQrState.platform === 'kugou_concept') await loadKugouConceptStatus(); if (adminQrState.platform === 'qishui') await loadQishuiStatus(); if (adminQrState.platform === 'migu') await loadMiguStatus(); return; }

        if (status === 'expired' || status === 'failed') { stopAdminQr(); return; }

        adminQrState.timer = window.setTimeout(pollAdminQr, 1500);

    } catch (error) { adminQrStatus(error.message || '\u6388\u6743\u8bf7\u6c42\u5931\u8d25', true); stopAdminQr(); }

}

loadAll();

loadSourceScannerFiles();

</script>

</body>

</html>"""





@app.get("/admin", response_class=HTMLResponse)

@app.get("/admin/", response_class=HTMLResponse)

async def admin_page():

    return HTMLResponse(ADMIN_HTML, headers={"Cache-Control": "no-store, max-age=0"})





@app.get("/admin/api/users")

async def admin_list_users(

    request: Request,

    page: int = Query(default=1, ge=1),

    page_size: int = Query(default=20, ge=1, le=100),

    q: str = Query(default="", max_length=32),

):

    _require_admin(request)

    query = str(q or "").strip()

    users = []

    for stored_user in load_users().get("users", []):

        username = str(stored_user.get("username") or "")

        if query and query not in username:

            continue

        user = _public_user(stored_user)

        user["last_login_at"] = stored_user.get("last_login_at")

        user["last_login_ip"] = stored_user.get("last_login_ip")

        users.append(user)

    if query:

        users.sort(key=lambda user: user.get("created_at") or "")

    else:

        users.sort(key=lambda user: (user.get("role") != "admin", user.get("created_at") or ""))

    total = len(users)

    total_pages = max(1, (total + page_size - 1) // page_size)

    page = min(page, total_pages)

    start = (page - 1) * page_size

    return JSONResponse({

        "code": 0,

        "users": users[start:start + page_size],

        "total": total,

        "page": page,

        "page_size": page_size,

        "total_pages": total_pages,

    })





def _admin_target_user(request: Request, user_id: str):

    admin = _require_admin(request)

    user_id = str(user_id).strip()

    target = next((user for user in load_users().get("users", []) if str(user.get("id")) == user_id), None)

    if not target:

        raise HTTPException(status_code=404, detail="用户不存在")

    if str(target.get("id")) == str(admin.get("id")):

        raise HTTPException(status_code=400, detail="不能对当前管理员账号执行此操作")

    if target.get("role") == "admin":

        raise HTTPException(status_code=400, detail="不能操作其他管理员账号")

    return target





@app.post("/admin/api/users/{user_id}/password")

async def admin_reset_user_password(user_id: str, request: Request):

    target = _admin_target_user(request, user_id)

    try:

        body = await request.json()

    except (ValueError, TypeError):

        return JSONResponse({"code": -1, "msg": "密码信息格式不正确"}, status_code=400)

    password = str(body.get("password") or "")

    if len(password) < 8 or len(password) > 128:

        return JSONResponse({"code": -1, "msg": "密码长度需为 8-128 位"}, status_code=400)

    users_data = load_users()

    for user in users_data["users"]:

        if str(user.get("id")) == str(target.get("id")):

            user["password_hash"] = _hash_password(password)

            target = user

            break

    save_users(users_data)

    _revoke_user_sessions(target["id"])

    return JSONResponse({"code": 0, "msg": f"已修改用户 {target.get('username')} 的密码，旧会话已失效"})





@app.post("/admin/api/users/{user_id}/ban")

async def admin_ban_user(user_id: str, request: Request):

    target = _admin_target_user(request, user_id)

    users_data = load_users()

    for user in users_data["users"]:

        if str(user.get("id")) == str(target.get("id")):

            user["banned"] = True

            user["banned_at"] = datetime.now().isoformat()

            target = user

            break

    save_users(users_data)

    _revoke_user_sessions(target["id"])

    return JSONResponse({"code": 0, "msg": f"已封禁用户 {target.get('username')}", "user": _public_user(target)})





@app.post("/admin/api/users/{user_id}/unban")

async def admin_unban_user(user_id: str, request: Request):

    target = _admin_target_user(request, user_id)

    users_data = load_users()

    for user in users_data["users"]:

        if str(user.get("id")) == str(target.get("id")):

            user["banned"] = False

            user.pop("banned_at", None)

            target = user

            break

    save_users(users_data)

    return JSONResponse({"code": 0, "msg": f"已解禁用户 {target.get('username')}", "user": _public_user(target)})





@app.delete("/admin/api/users/{user_id}")

async def admin_delete_user(user_id: str, request: Request):

    target = _admin_target_user(request, user_id)

    users_data = load_users()

    users_data["users"] = [user for user in users_data["users"] if str(user.get("id")) != str(target.get("id"))]

    save_users(users_data)

    _revoke_user_sessions(target["id"])

    _delete_user_data(target["id"])

    return JSONResponse({"code": 0, "msg": f"已删除用户 {target.get('username')} 及其本地数据"})





@app.get("/admin/api/config")

async def admin_get_config(request: Request):

    _require_admin(request)

    cfg = load_config()

    cookie_masked = cfg.get("cookie", "")

    if len(cookie_masked) > 10:

        cookie_masked = cookie_masked[:4] + "****" + cookie_masked[-4:]

    qq_cookie_masked = cfg.get("qq_cookie", "")

    if len(qq_cookie_masked) > 10:

        qq_cookie_masked = qq_cookie_masked[:4] + "****" + qq_cookie_masked[-4:]

    return JSONResponse({

        "cookie": cookie_masked,

        "qq_cookie": qq_cookie_masked,

        "api_key": cfg.get("api_key", ""),

        "announcement": cfg.get("announcement", ""),

        "allow_registration": cfg.get("allow_registration", True),

    })





@app.post("/admin/api/config")

async def admin_save_config(request: Request):

    _require_admin(request)

    try:

        body = await request.json()

    except (ValueError, TypeError):

        return JSONResponse({"code": -1, "msg": "配置格式不正确"}, status_code=400)

    if not isinstance(body, dict):

        return JSONResponse({"code": -1, "msg": "配置格式不正确"}, status_code=400)

    cfg = load_config()

    

    cookie = body.get("cookie", "").strip()

    if cookie:

        cfg["cookie"] = cookie

        cfg["cookie_status"] = "unknown"

    

    qq_cookie = body.get("qq_cookie", "").strip()

    if "qq_cookie" in body:

        cfg["qq_cookie"] = qq_cookie

    

    api_key = body.get("api_key", "").strip()

    if "api_key" in body:

        cfg["api_key"] = api_key



    if "announcement" in body:

        cfg["announcement"] = str(body.get("announcement") or "").strip()[:500]

    if "allow_registration" in body:

        value = body.get("allow_registration")

        if not isinstance(value, bool):

            return JSONResponse({"code": -1, "msg": "注册开关参数不正确"}, status_code=400)

        cfg["allow_registration"] = value

    

    save_config(cfg)

    return JSONResponse({"code": 0, "msg": "保存成功"})





@app.get("/admin/api/status")

async def admin_status():

    cfg = load_config()

    logs = load_logs()

    return JSONResponse({

        "cookie_status": cfg.get("cookie_status", "unknown"),

        "cookie_nickname": cfg.get("nickname"),

        "cookie_checked_at": cfg.get("cookie_checked_at"),

        "last_error": cfg.get("last_error"),

        "ncm_refreshed_at": cfg.get("ncm_refreshed_at"),

        "ncm_next_refresh_at": cfg.get("ncm_next_refresh_at"),

        "total_calls": len(logs),

        "recent_calls": logs[:10],

    })





@app.get("/admin/api/logs")

async def admin_logs():

    return JSONResponse(load_logs())





@app.post("/admin/api/check-cookie")

async def admin_check_cookie():

    is_valid, nickname = check_ncm_cookie()

    cfg = load_config()

    return JSONResponse({

        "cookie_status": cfg.get("cookie_status"),

        "nickname": nickname,

        "error": cfg.get("last_error"),

    })





# ==================== QQ音乐管理后台 API ====================



@app.post("/admin/api/save-qq-cookie")

async def admin_save_qq_cookie(request: Request):

    """接收完整 QQ cookie，自动提取关键字段并保存"""

    body = await request.json()

    cookie_str = body.get("qq_cookie", "").strip()

    

    if not cookie_str:

        return JSONResponse({"code": -1, "msg": "Cookie 为空"})

    

    cfg = load_config()

    cfg["qq_cookie"] = cookie_str

    

    # 自动提取关键字段

    fields = extract_qq_cookie_fields(cookie_str)

    cfg["qq_musickey"] = fields.get("qq_musickey", "")

    cfg["qq_uin"] = fields.get("qq_uin", "")

    cfg["euin"] = fields.get("euin", "")

    cfg["qq_refresh_token"] = fields.get("qq_refresh_token", "")

    cfg["qq_access_token"] = fields.get("qq_access_token", "")

    cfg["qq_openid"] = fields.get("qq_openid", "")

    cfg["qq_unionid"] = fields.get("qq_unionid", "")

    cfg["qq_status"] = "unknown"

    

    # 如果有 musickey，设为首次续期时间

    if cfg["qq_musickey"]:

        now = datetime.now()

        cfg["qq_refreshed_at"] = now.isoformat()

        cfg["qq_next_refresh_at"] = (now + timedelta(hours=24)).isoformat()

    

    save_config(cfg)

    

    # 返回提取结果摘要

    extracted_info = {

        "musickey": (fields["qq_musickey"][:10] + "...") if fields["qq_musickey"] else "未提取到",

        "uin": fields["qq_uin"] or "未提取到",

        "refresh_token": ("已提取" if fields["qq_refresh_token"] else "未提取到"),

        "access_token": ("已提取" if fields["qq_access_token"] else "未提取到"),

        "openid": ("已提取" if fields["qq_openid"] else "未提取到"),

    }

    return JSONResponse({"code": 0, "msg": f"解析成功：{json.dumps(extracted_info, ensure_ascii=False)}", "fields": extracted_info})





@app.get("/admin/api/qq-status")

async def admin_qq_status():

    """QQ音乐状态"""

    cfg = load_config()

    return JSONResponse({

        "qq_status": cfg.get("qq_status", "unknown"),

        "qq_nickname": cfg.get("qq_nickname", ""),

        "qq_checked_at": cfg.get("qq_checked_at"),

        "qq_last_error": cfg.get("qq_last_error"),

        "qq_refreshed_at": cfg.get("qq_refreshed_at"),

        "qq_next_refresh_at": cfg.get("qq_next_refresh_at"),

        "qq_musickey": cfg.get("qq_musickey", ""),

        "qq_uin": cfg.get("qq_uin", ""),

        "qq_refresh_token": cfg.get("qq_refresh_token", ""),

        "qq_access_token": cfg.get("qq_access_token", ""),

        "qq_openid": cfg.get("qq_openid", ""),

        "qq_unionid": cfg.get("qq_unionid", ""),

        "euin": cfg.get("euin", ""),

        "api_key": cfg.get("api_key", ""),

    })





@app.post("/admin/api/renew-qq")

async def admin_renew_qq():

    """手动触发 QQ音乐 musickey 续期"""

    success = qq_renew_musickey()

    cfg = load_config()

    if success:

        return JSONResponse({"code": 0, "msg": f"续期成功，下次续期: {cfg.get('qq_next_refresh_at', '')}"})

    else:

        return JSONResponse({"code": -1, "msg": cfg.get("qq_last_error", "续期失败")})





@app.post("/admin/api/renew-migu")

async def admin_renew_migu():

    """手动触发咪咕会话续期（滚动 pacmtoken）"""

    success = migu_renew_cookie()

    cfg = load_config()

    if success:

        return JSONResponse({"code": 0, "msg": f"续期成功，下次续期: {cfg.get('migu_next_refresh_at', '')}"})

    return JSONResponse({"code": -1, "msg": cfg.get("migu_last_error") or "续期失败"})





@app.post("/admin/api/renew-ncm")

async def admin_renew_ncm():

    """手动触发网易云会话续期（/login/refresh）"""

    success = ncm_renew_cookie()

    cfg = load_config()

    if success:

        return JSONResponse({"code": 0, "msg": f"续期成功，下次续期: {cfg.get('ncm_next_refresh_at', '')}"})

    return JSONResponse({"code": -1, "msg": cfg.get("last_error") or "续期失败"})





@app.post("/admin/api/renew-kugou-concept")

async def admin_renew_kugou_concept(request: Request):

    """手动触发酷狗概念版会话续期（官方 /v5/login_by_token）"""

    _require_admin(request)

    cfg = load_config()

    if not str(cfg.get("kugou_concept_cookie", "") or "").strip():

        return JSONResponse({"code": -1, "msg": "请先扫码授权酷狗概念版"})

    success = kugou_concept_renew_cookie()

    cfg = load_config()

    if success:

        return JSONResponse({"code": 0, "msg": f"续期成功，下次续期: {cfg.get('kugou_concept_next_refresh_at', '')}"})

    return JSONResponse({"code": -1, "msg": cfg.get("kugou_concept_last_error") or "续期失败"})





@app.post("/admin/api/renew-kugou")

async def admin_renew_kugou(request: Request):

    """手动触发普通酷狗会话续期（官方 /v5/login_by_token，仅 App 会话可用）"""

    _require_admin(request)

    cfg = load_config()

    cookie = str(cfg.get("kugou_cookie", "") or "").strip()

    if not cookie:

        return JSONResponse({"code": -1, "msg": "请先扫码授权或填写酷狗 Cookie"})

    if _kugou_cookie_mode(_kugou_cookie_fields(cookie)) != "app":

        return JSONResponse({"code": -1, "msg": "当前是网页 Cookie，官方刷新接口只支持 App 会话（token + userid），请改用扫码授权"})

    success = kugou_renew_cookie()

    cfg = load_config()

    if success:

        return JSONResponse({"code": 0, "msg": f"续期成功，下次续期: {cfg.get('kugou_next_refresh_at', '')}"})

    return JSONResponse({"code": -1, "msg": cfg.get("kugou_last_error") or "续期失败"})





@app.get("/admin/api/renew-settings")

async def admin_get_renew_settings(request: Request):

    """返回自动保活开关、各平台间隔与允许范围。"""

    _require_admin(request)

    cfg = load_config()

    limits = {key: {"default": item[0], "min": item[1], "max": item[2]} for key, item in RENEW_INTERVAL_LIMITS.items()}

    return JSONResponse({

        "auto_renew_enabled": bool(cfg.get("auto_renew_enabled", True)),

        "intervals": {key: renew_interval_hours(cfg, key) for key in RENEW_INTERVAL_LIMITS},

        "limits": limits,

        "supported": {"netease": True, "qq": True, "migu": True, "kugou_concept": True, "kugou": True},

        "next_refresh_at": {

            "netease": cfg.get("ncm_next_refresh_at"),

            "qq": cfg.get("qq_next_refresh_at"),

            "migu": cfg.get("migu_next_refresh_at"),

            "kugou_concept": cfg.get("kugou_concept_next_refresh_at"),

            "kugou": cfg.get("kugou_next_refresh_at"),

        },

    })





@app.post("/admin/api/renew-settings")

async def admin_save_renew_settings(request: Request):

    """保存自动保活设置；间隔值会被夹紧到平台允许范围。"""

    _require_admin(request)

    body = await request.json()

    cfg = load_config()

    if "auto_renew_enabled" in body:

        cfg["auto_renew_enabled"] = bool(body.get("auto_renew_enabled"))

    applied = {}

    for key, (default, low, high) in RENEW_INTERVAL_LIMITS.items():

        if key not in body:

            applied[key] = renew_interval_hours(cfg, key)

            continue

        try:

            value = int(float(body.get(key)))

        except (TypeError, ValueError):

            return JSONResponse({"code": -1, "msg": f"{key} 必须是数字"}, status_code=422)

        cfg[key] = max(low, min(high, value))

        applied[key] = cfg[key]

    # 间隔变化后立即重排下次续期时间，避免沿用旧计划。

    _schedule_next(cfg, "ncm", applied["ncm_renew_interval_hours"])

    _schedule_next(cfg, "qq", applied["qq_renew_interval_hours"])

    _schedule_next(cfg, "migu", applied["migu_renew_interval_hours"])

    _schedule_next(cfg, "kugou_concept", applied["kugou_concept_check_interval_hours"])

    _schedule_next(cfg, "kugou", applied["kugou_renew_interval_hours"])

    save_config(cfg)

    return JSONResponse({"code": 0, "auto_renew_enabled": bool(cfg.get("auto_renew_enabled", True)), "intervals": applied})




@app.post("/admin/api/check-qq")

async def admin_check_qq():

    """检测 QQ音乐 cookie 是否有效"""

    is_valid = check_qq_cookie()

    cfg = load_config()

    return JSONResponse({

        "qq_status": cfg.get("qq_status"),

        "qq_nickname": cfg.get("qq_nickname", ""),

        "error": cfg.get("qq_last_error"),

        "qq_checked_at": cfg.get("qq_checked_at"),

    })





# ==================== 第三方音源 API ====================



def check_kugou_cookie():

    cfg = load_config()

    cookie = str(cfg.get("kugou_cookie") or "").strip()

    now = datetime.now().isoformat()

    fields = _kugou_cookie_fields(cookie)

    mode = _kugou_cookie_mode(fields)

    if not cookie:

        cfg.update({"kugou_status": "empty", "kugou_checked_at": now, "kugou_last_error": None})

        save_config(cfg)

        return False

    if mode == "unknown":

        cfg.update({"kugou_status": "invalid", "kugou_checked_at": now, "kugou_last_error": "Cookie needs either token + userid, or KugooID + t/kg_mid"})

        save_config(cfg)

        return False

    if mode == "web":

        # Web sessions cannot access the signed cloud-playlist endpoint, but can be used for playback.

        probe = kugou_search_song("test", page=1, limit=1, cookie=cookie)

        if probe.get("status") == 1:

            cfg.update({"kugou_status": "ok", "kugou_checked_at": now, "kugou_last_error": None})

            save_config(cfg)

            return True

        cfg.update({"kugou_status": "invalid", "kugou_checked_at": now, "kugou_last_error": probe.get("msg") or "Kugou web session check failed"})

        save_config(cfg)

        return False

    try:

        _kugou_account_playlists({"credential_encrypted": _encrypt_credential({"cookie": cookie})})

        cfg.update({"kugou_status": "ok", "kugou_checked_at": now, "kugou_last_error": None})

        save_config(cfg)

        return True

    except ValueError as exc:

        cfg.update({"kugou_status": "invalid", "kugou_checked_at": now, "kugou_last_error": str(exc)})

        save_config(cfg)

        return False





@app.post("/admin/api/save-kugou-cookie")

async def admin_save_kugou_cookie(request: Request):

    _require_admin(request)

    body = await request.json()

    cookie = str(body.get("kugou_cookie") or "").strip()

    fields = _kugou_cookie_fields(cookie)

    if not cookie:

        return JSONResponse({"code": -1, "msg": "Cookie is required"})

    mode = _kugou_cookie_mode(fields)

    if mode == "unknown":

        return JSONResponse({"code": -1, "msg": "Cookie needs either token + userid, or KugooID + t/kg_mid"})

    cfg = load_config()

    cfg["kugou_cookie"] = cookie

    cfg["kugou_status"] = "unknown"

    _schedule_next(cfg, "kugou", renew_interval_hours(cfg, "kugou_renew_interval_hours"))

    save_config(cfg)

    check_kugou_cookie()

    cfg = load_config()

    return JSONResponse({"code": 0, "msg": "Kugou session saved and checked", "fields": {"userid": fields.get("userid") or fields.get("web_userid") or "", "token_configured": bool(fields.get("token")), "session_type": mode}, "status": cfg.get("kugou_status")})





@app.get("/admin/api/kugou-status")

async def admin_kugou_status(request: Request):

    _require_admin(request)

    cfg = load_config()

    fields = _kugou_cookie_fields(cfg.get("kugou_cookie", ""))

    mode = _kugou_cookie_mode(fields)

    return JSONResponse({"kugou_status": cfg.get("kugou_status", "empty"), "kugou_checked_at": cfg.get("kugou_checked_at"), "kugou_last_error": cfg.get("kugou_last_error"), "userid": fields.get("userid") or fields.get("web_userid") or "", "token_configured": bool(fields.get("token")), "session_type": mode, "kugou_refreshed_at": cfg.get("kugou_refreshed_at"), "kugou_next_refresh_at": cfg.get("kugou_next_refresh_at"), "kugou_checked_by_scheduler_at": cfg.get("kugou_checked_by_scheduler_at"), "auto_renew_supported": mode == "app", "api_key": cfg.get("api_key", "")})





@app.post("/admin/api/check-kugou")

async def admin_check_kugou(request: Request):

    _require_admin(request)

    check_kugou_cookie()

    cfg = load_config()

    return JSONResponse({"kugou_status": cfg.get("kugou_status"), "kugou_checked_at": cfg.get("kugou_checked_at"), "error": cfg.get("kugou_last_error")})





def check_kugou_concept_cookie():

    """Validate the independent Concept Edition administrator session with a signed search."""

    cfg = load_config()

    cookie = str(cfg.get("kugou_concept_cookie") or "").strip()

    now = datetime.now().isoformat()

    fields = _kugou_cookie_fields(cookie)

    if not cookie:

        cfg.update({"kugou_concept_status": "empty", "kugou_concept_checked_at": now, "kugou_concept_last_error": None})

        save_config(cfg)

        return False

    if not fields.get("token") or not fields.get("userid"):

        cfg.update({"kugou_concept_status": "invalid", "kugou_concept_checked_at": now, "kugou_concept_last_error": "Concept Edition session needs token + userid"})

        save_config(cfg)

        return False

    result = kugou_concept_search_song("音乐", page=1, limit=1, cookie=cookie, device=cfg.get("kugou_concept_device") or {})

    if result.get("status") == 1:

        cfg.update({"kugou_concept_status": "ok", "kugou_concept_checked_at": now, "kugou_concept_last_error": None})

        save_config(cfg)

        return True

    cfg.update({"kugou_concept_status": "invalid", "kugou_concept_checked_at": now, "kugou_concept_last_error": result.get("msg") or "Kugou Concept Edition session check failed"})

    save_config(cfg)

    return False





@app.get("/admin/api/kugou-concept-status")

async def admin_kugou_concept_status(request: Request):

    _require_admin(request)

    cfg = load_config()

    fields = _kugou_cookie_fields(cfg.get("kugou_concept_cookie", ""))

    return JSONResponse({

        "kugou_concept_status": cfg.get("kugou_concept_status", "empty"),

        "kugou_concept_checked_at": cfg.get("kugou_concept_checked_at"),

        "kugou_concept_last_error": cfg.get("kugou_concept_last_error"),

        "userid": fields.get("userid") or "",

        "token_configured": bool(fields.get("token")),

        "priority": cfg.get("kugou_player_priority", "concept_first"),

        "kugou_concept_next_check_at": cfg.get("kugou_concept_next_refresh_at"),

        "kugou_concept_checked_by_scheduler_at": cfg.get("kugou_concept_checked_by_scheduler_at"),

        "kugou_concept_refreshed_at": cfg.get("kugou_concept_refreshed_at"),

        "auto_renew_supported": True,

    })





@app.post("/admin/api/check-kugou-concept")

async def admin_check_kugou_concept(request: Request):

    _require_admin(request)

    check_kugou_concept_cookie()

    cfg = load_config()

    return JSONResponse({"kugou_concept_status": cfg.get("kugou_concept_status"), "kugou_concept_checked_at": cfg.get("kugou_concept_checked_at"), "error": cfg.get("kugou_concept_last_error")})





@app.post("/admin/api/kugou-player-priority")

async def admin_save_kugou_player_priority(request: Request):

    _require_admin(request)

    body = await request.json()

    priority = str(body.get("priority") or "").strip()

    if priority not in ("concept_first", "standard_first"):

        return JSONResponse({"code": -1, "msg": "Unsupported Kugou playback priority"}, status_code=422)

    cfg = load_config()

    cfg["kugou_player_priority"] = priority

    save_config(cfg)

    return JSONResponse({"code": 0, "priority": priority})





# ==================== Administrator global Cookie QR authorization ====================



def _save_admin_platform_cookie(platform, cookie):

    """Persist an administrator-authorized global API session without exposing its value."""

    cookie = str(cookie or "").strip()

    if not cookie:

        raise ValueError("\u6388\u6743\u672a\u8fd4\u56de\u53ef\u7528\u4f1a\u8bdd\uff0c\u8bf7\u5237\u65b0\u4e8c\u7ef4\u7801\u540e\u91cd\u8bd5")



    cfg = load_config()

    if platform == "netease":

        cfg.update({"cookie": cookie, "cookie_status": "unknown", "last_error": None, "ncm_refreshed_at": datetime.now().isoformat()})

        _schedule_next(cfg, "ncm", renew_interval_hours(cfg, "ncm_renew_interval_hours"))

        save_config(cfg)

        valid, nickname = check_ncm_cookie()

        status = load_config().get("cookie_status", "unknown")

        return {"status": status, "nickname": nickname, "valid": valid}



    if platform == "qq":

        fields = extract_qq_cookie_fields(cookie)

        if not fields.get("qq_uin"):

            raise ValueError("\u672a\u53d6\u5f97 QQ \u97f3\u4e50\u4f1a\u8bdd\uff0c\u8bf7\u5237\u65b0\u4e8c\u7ef4\u7801\u540e\u91cd\u8bd5")

        cfg.update({

            "qq_cookie": cookie,

            "qq_musickey": fields.get("qq_musickey", ""),

            "qq_uin": fields.get("qq_uin", ""),

            "euin": fields.get("euin", ""),

            "qq_refresh_token": fields.get("qq_refresh_token", ""),

            "qq_access_token": fields.get("qq_access_token", ""),

            "qq_openid": fields.get("qq_openid", ""),

            "qq_unionid": fields.get("qq_unionid", ""),

            "qq_status": "unknown",

            "qq_last_error": None,

        })

        if fields.get("qq_musickey"):

            now = datetime.now()

            cfg["qq_refreshed_at"] = now.isoformat()

            _schedule_next(cfg, "qq", renew_interval_hours(cfg, "qq_renew_interval_hours"))

        save_config(cfg)

        valid = check_qq_cookie()

        status = load_config().get("qq_status", "unknown")

        return {"status": status, "nickname": None, "valid": valid}



    if platform == "qishui":

        sessionid = parse_cookie_kv(cookie, "sessionid")

        if not cookie:

            raise ValueError("汽水授权未返回有效会话，请刷新二维码后重试")

        cfg["qishui_cookie"] = _encrypt_credential({"sessionid": sessionid, "cookie": cookie})

        cfg["qishui_status"] = "unknown"

        cfg["qishui_last_error"] = None

        cfg["qishui_checked_at"] = datetime.now().isoformat()

        cfg["qishui_session_refreshed_at"] = datetime.now().isoformat()


        save_config(cfg)

        try:

            profile = qishui_me(cookie)

            if not profile.get("id"):

                raise ValueError("汽水授权会话尚未生效，请刷新二维码后重新扫码确认")

            cfg = load_config(); cfg["qishui_status"] = "ok"; cfg["qishui_last_error"] = None; cfg["qishui_checked_at"] = datetime.now().isoformat(); save_config(cfg)

            return {"status": "ok", "nickname": profile.get("nickname"), "valid": True}

        except (QishuiError, requests.RequestException, ValueError) as exc:

            cfg = load_config(); cfg["qishui_status"] = "invalid"; cfg["qishui_last_error"] = str(exc); cfg["qishui_checked_at"] = datetime.now().isoformat(); save_config(cfg)

            return {"status": "invalid", "nickname": None, "valid": False}



    if platform == "migu":

        cfg.update({"migu_cookie": cookie, "migu_status": "unknown", "migu_last_error": None, "migu_refreshed_at": datetime.now().isoformat()})

        _schedule_next(cfg, "migu", renew_interval_hours(cfg, "migu_renew_interval_hours"))

        save_config(cfg)

        valid = check_migu_cookie()

        status = load_config().get("migu_status", "unknown")

        return {"status": status, "nickname": None, "valid": valid}



    if platform == "kugou":

        fields = _kugou_cookie_fields(cookie)

        mode = _kugou_cookie_mode(fields)

        if mode == "unknown":

            raise ValueError("\u9177\u72d7\u6388\u6743\u672a\u8fd4\u56de\u53ef\u7528\u4f1a\u8bdd\uff0c\u8bf7\u5237\u65b0\u4e8c\u7ef4\u7801\u540e\u91cd\u8bd5")

        cfg.update({"kugou_cookie": cookie, "kugou_status": "unknown", "kugou_last_error": None})

        _schedule_next(cfg, "kugou", renew_interval_hours(cfg, "kugou_renew_interval_hours"))

        save_config(cfg)

        valid = check_kugou_cookie()

        status = load_config().get("kugou_status", "unknown")

        return {"status": status, "nickname": None, "valid": valid, "session_type": mode}





    if platform == "kugou_concept":

        fields = _kugou_cookie_fields(cookie)

        if not fields.get("token") or not fields.get("userid"):

            raise ValueError("酷狗概念版授权未返回 token 或 userid，请刷新二维码后重试")

        cfg.update({

            "kugou_concept_cookie": cookie,

            "kugou_concept_device": _kugou_concept_device_from_cookie(cookie, cfg.get("kugou_concept_device") or {}),

            "kugou_concept_status": "unknown",

            "kugou_concept_last_error": None,

        })

        _schedule_next(cfg, "kugou_concept", renew_interval_hours(cfg, "kugou_concept_check_interval_hours"))

        save_config(cfg)

        valid = check_kugou_concept_cookie()

        status = load_config().get("kugou_concept_status", "unknown")

        return {"status": status, "nickname": None, "valid": valid, "session_type": "concept_app"}



    raise ValueError("\u4e0d\u652f\u6301\u7684\u97f3\u4e50\u5e73\u53f0")





@app.post("/admin/api/qr/{platform}/start")

async def admin_start_qr_authorization(platform: str, request: Request):

    admin = _require_admin(request)

    platform = platform.lower().strip()

    try:

        if platform == "netease":

            result = _start_netease_qr_login(admin, ADMIN_QR_SESSIONS, ADMIN_QR_LOCK, admin["id"], True)

        elif platform == "qq":

            result = _start_qq_qr_login(admin, ADMIN_QR_SESSIONS, ADMIN_QR_LOCK, admin["id"], True)

        elif platform == "kugou":

            result = _start_kugou_qr_login(admin, ADMIN_QR_SESSIONS, ADMIN_QR_LOCK, admin["id"], True)

        elif platform == "kugou_concept":

            result = _start_kugou_concept_qr_login(admin, ADMIN_QR_SESSIONS, ADMIN_QR_LOCK, admin["id"], True)

        elif platform == "migu":

            result = _start_migu_qr_login(admin, ADMIN_QR_SESSIONS, ADMIN_QR_LOCK, admin["id"], True)

        elif platform == "qishui":

            result = _start_qishui_qr_login(admin, ADMIN_QR_SESSIONS, ADMIN_QR_LOCK, admin["id"], True)

        else:

            return JSONResponse({"code": -1, "msg": "\u4e0d\u652f\u6301\u7684\u97f3\u4e50\u5e73\u53f0"}, status_code=422)

        return JSONResponse({"code": 0, "platform": platform, **result})

    except (ValueError, requests.RequestException) as exc:

        return JSONResponse({"code": -1, "msg": str(exc)}, status_code=502)

    except Exception:

        logger.exception("Unexpected admin QR start failure for %s", platform)

        return JSONResponse({"code": -1, "msg": "二维码服务暂时异常，请稍后重试"}, status_code=500)





@app.get("/admin/api/qr/{platform}/{token}")

async def admin_poll_qr_authorization(platform: str, token: str, request: Request):

    admin = _require_admin(request)

    platform = platform.lower().strip()

    try:

        if platform == "netease":

            result = _poll_netease_qr_login(admin, token, ADMIN_QR_SESSIONS, ADMIN_QR_LOCK, admin["id"], True)

        elif platform == "qq":

            result = _poll_qq_qr_login(admin, token, ADMIN_QR_SESSIONS, ADMIN_QR_LOCK, admin["id"], True)

        elif platform == "kugou":

            result = _poll_kugou_qr_login(admin, token, ADMIN_QR_SESSIONS, ADMIN_QR_LOCK, admin["id"], True)

        elif platform == "kugou_concept":

            result = _poll_kugou_concept_qr_login(admin, token, ADMIN_QR_SESSIONS, ADMIN_QR_LOCK, admin["id"], True)

        elif platform == "migu":

            result = _poll_migu_qr_login(admin, token, ADMIN_QR_SESSIONS, ADMIN_QR_LOCK, admin["id"], True)

        elif platform == "qishui":

            result = _poll_qishui_qr_login(admin, token, ADMIN_QR_SESSIONS, ADMIN_QR_LOCK, admin["id"], True)

        else:

            return JSONResponse({"code": -1, "msg": "\u4e0d\u652f\u6301\u7684\u97f3\u4e50\u5e73\u53f0"}, status_code=422)

        if result.get("status") == "authorized":

            saved = _save_admin_platform_cookie(platform, result.pop("cookie", ""))

            result["config_status"] = saved.get("status")

            result["message"] = "\u6388\u6743\u6210\u529f\uff0c\u5df2\u4fdd\u5b58\u5230\u5168\u5c40 API \u914d\u7f6e" if saved.get("valid") else "\u6388\u6743\u5df2\u4fdd\u5b58\uff0c\u4f46\u6709\u6548\u6027\u68c0\u6d4b\u672a\u901a\u8fc7\uff0c\u8bf7\u7a0d\u540e\u91cd\u65b0\u68c0\u6d4b"

        return JSONResponse({"code": 0, "platform": platform, **result})

    except (ValueError, requests.RequestException) as exc:

        return JSONResponse({"code": -1, "msg": str(exc)}, status_code=502)

    except Exception:

        logger.exception("Unexpected admin QR poll failure for %s", platform)

        return JSONResponse({"code": -1, "msg": "二维码授权状态暂时异常，请稍后重试"}, status_code=500)









# ==================== Migu cookie management ====================



def check_migu_cookie():

    """Verify the global Migu cookie and update config."""

    cfg = load_config()

    cookie = cfg.get("migu_cookie", "")

    now = datetime.now().isoformat()

    if not cookie:

        cfg.update({"migu_status": "empty", "migu_checked_at": now, "migu_last_error": None})

        save_config(cfg)

        return False

    valid, message = migu_check_cookie(cookie)

    if valid:

        cfg.update({"migu_status": "ok", "migu_checked_at": now, "migu_last_error": None})

    else:

        cfg.update({"migu_status": "invalid", "migu_checked_at": now, "migu_last_error": message})

    save_config(cfg)

    return valid





@app.get("/admin/api/migu-status")

async def admin_migu_status(request: Request):

    _require_admin(request)

    cfg = load_config()

    return JSONResponse({

        "migu_status": cfg.get("migu_status", "empty"),

        "migu_checked_at": cfg.get("migu_checked_at"),

        "migu_last_error": cfg.get("migu_last_error"),

        "migu_refreshed_at": cfg.get("migu_refreshed_at"),

        "migu_next_refresh_at": cfg.get("migu_next_refresh_at"),

        "api_key": cfg.get("api_key", ""),

    })





@app.post("/admin/api/save-migu-cookie")

async def admin_save_migu_cookie(request: Request):

    _require_admin(request)

    body = await request.json()

    cookie = str(body.get("migu_cookie") or "").strip()

    if not cookie:

        return JSONResponse({"code": -1, "msg": "Cookie 为空"})

    cfg = load_config()

    cfg["migu_cookie"] = cookie

    cfg["migu_status"] = "unknown"

    cfg["migu_refreshed_at"] = datetime.now().isoformat()

    _schedule_next(cfg, "migu", renew_interval_hours(cfg, "migu_renew_interval_hours"))

    save_config(cfg)

    valid = check_migu_cookie()

    cfg = load_config()

    return JSONResponse({

        "code": 0 if valid else -1,

        "msg": "Cookie 保存并验证成功" if valid else f"Cookie 已保存但验证失败：{cfg.get('migu_last_error', '')}",

        "status": cfg.get("migu_status"),

    })





@app.post("/admin/api/check-migu")

async def admin_check_migu(request: Request):

    _require_admin(request)

    valid = check_migu_cookie()

    cfg = load_config()

    return JSONResponse({

        "migu_status": cfg.get("migu_status"),

        "migu_checked_at": cfg.get("migu_checked_at"),

        "error": cfg.get("migu_last_error"),

    })





@app.get("/admin/api/qishui-status")

async def admin_qishui_status(request: Request):

    _require_admin(request)

    cfg = load_config()

    return JSONResponse({

        "qishui_status": cfg.get("qishui_status", "empty"),

        "qishui_checked_at": cfg.get("qishui_checked_at"),

        "qishui_last_error": cfg.get("qishui_last_error"),

        "qishui_session_refreshed_at": cfg.get("qishui_session_refreshed_at"),

        "qishui_bridge_available": _qishui_bridge_available(),

        "qishui_auto_check_supported": False,

        "qishui_auto_renew_supported": False,

        "qishui_persistent_session_supported": True,

        "api_key": cfg.get("api_key", ""),

    })



@app.post("/admin/api/check-qishui")

async def admin_check_qishui(request: Request):

    _require_admin(request)

    valid = qishui_sync_persistent_session()

    cfg = load_config()

    status = cfg.get("qishui_status", "empty")

    return JSONResponse({

        "code": 0 if valid and status == "ok" else -1,

        "msg": "汽水会话检测成功" if valid and status == "ok" else (

            cfg.get("qishui_last_error") or "未检测到有效汽水会话，请先扫码授权"

        ),

        "status": status,

        "qishui_checked_at": cfg.get("qishui_checked_at"),

        "error": cfg.get("qishui_last_error"),

    })



@app.get("/admin/api/third-sources")

async def admin_get_third_sources():

    """获取所有第三方音源配置"""

    cfg = load_config()

    quality = configured_player_quality(cfg)

    all_sources = _get_all_source_configs(cfg)

    third_timeout, resolve_timeout, cross_platform_timeout = _player_timeout_settings(cfg)

    return JSONResponse({

        "sources": {sid: _public_source_config(source) for sid, source in all_sources.items()},

        "quality": quality,
        "player_quality": cfg.get("player_quality", quality),

        "third_timeout": third_timeout,

        "resolve_timeout": resolve_timeout,

        "cross_platform_timeout": cross_platform_timeout,

        "cross_platform_priority": configured_cross_platform_priority(cfg),

    })





@app.post("/admin/api/third-sources/save")

async def admin_save_third_sources(request: Request):

    """保存第三方音源启用状态"""

    body = await request.json()

    cfg = load_config()

    enabled_map = body.get("enabled", {})  # {源标识: True/False}

    quality = body.get("quality", "320k")

    try:

        third_timeout = int(body.get("third_timeout", cfg.get("player_third_source_timeout", 10)))

        resolve_timeout = int(body.get("resolve_timeout", cfg.get("player_resolve_timeout", 12)))

        cross_platform_timeout = int(body.get("cross_platform_timeout", cfg.get("player_cross_platform_timeout", 4)))

    except (TypeError, ValueError):

        return JSONResponse({"code": -1, "msg": "等待时间必须是整数秒"}, status_code=422)

    if not 3 <= third_timeout <= 60:

        return JSONResponse({"code": -1, "msg": "第三方回源等待上限需为 3 到 60 秒"}, status_code=422)

    if not 5 <= resolve_timeout <= 75:

        return JSONResponse({"code": -1, "msg": "自动播放总等待上限需为 5 到 75 秒"}, status_code=422)

    if not 1 <= cross_platform_timeout <= 30:

        return JSONResponse({"code": -1, "msg": "跨平台官方补源等待上限需为 1 到 30 秒"}, status_code=422)

    resolve_timeout = max(third_timeout, resolve_timeout)

    user_sources = cfg.get("third_sources", {}) or {}

    for sid, enabled in enabled_map.items():

        if sid in _BUILTIN_SOURCES:

            # 内置源只存启用状态

            if sid not in user_sources:

                user_sources[sid] = {}

            user_sources[sid]["enabled"] = enabled

        elif sid in user_sources:

            user_sources[sid]["enabled"] = enabled

    usage_limits = body.get("usage_limits", {}) or {}

    if not isinstance(usage_limits, dict):

        return JSONResponse({"code": -1, "msg": "调用次数配置格式无效"}, status_code=422)

    for sid, raw_limit in usage_limits.items():

        if sid not in user_sources:

            continue

        try:

            limit = int(raw_limit)

        except (TypeError, ValueError):

            return JSONResponse({"code": -1, "msg": f"音源 {sid} 的调用次数必须是整数"}, status_code=422)

        if limit < 0 or limit > 10000000:

            return JSONResponse({"code": -1, "msg": "调用次数上限需为 0 到 10000000，0 表示无限"}, status_code=422)

        user_sources[sid]["usage_limit"] = limit

        current = _source_usage_snapshot(user_sources[sid])["usage_count"]

        user_sources[sid]["usage_count"] = min(current, limit) if limit > 0 else current

    cfg["third_sources"] = user_sources

    quality = normalize_player_quality(quality)
    cfg["third_source_quality"] = quality
    cfg["player_quality"] = quality

    cfg["player_third_source_timeout"] = third_timeout

    cfg["player_resolve_timeout"] = resolve_timeout

    cfg["player_cross_platform_timeout"] = cross_platform_timeout

    # 跨平台补源优先级
    raw_priority = body.get("cross_platform_priority")
    if raw_priority is not None:
        if isinstance(raw_priority, str):
            raw_priority = [x.strip() for x in raw_priority.split(",") if x.strip()]
        if isinstance(raw_priority, (list, tuple)):
            valid = []
            for p in raw_priority:
                p = str(p or "").strip().lower()
                if p in OFFICIAL_PLAYER_PLATFORMS and p not in valid:
                    valid.append(p)
            for p in OFFICIAL_PLAYER_PLATFORMS:
                if p not in valid:
                    valid.append(p)
            cfg["player_cross_platform_priority"] = valid

    save_config(cfg)

    return JSONResponse({"code": 0, "msg": "已保存"})





@app.post("/admin/api/third-sources/add")

async def admin_add_third_source(request: Request):

    """验证后添加自定义第三方音源。"""

    body = await request.json()

    sid = body.get("id", "").strip()

    if not sid or not sid.isidentifier():

        return JSONResponse({"code": -1, "msg": "源标识必须为字母数字下划线"})

    if sid in _BUILTIN_SOURCES:

        return JSONResponse({"code": -1, "msg": "该标识已被内置源占用"})

    cfg = load_config()

    user_sources = cfg.get("third_sources", {}) or {}

    if sid in user_sources:

        return JSONResponse({"code": -1, "msg": "该标识已存在"})

    source_type = body.get("source_type", "generic")

    new_source = {

        "name": body.get("name", sid),

        "desc": body.get("desc", ""),

        "api_url": body.get("api_url", "").rstrip("/"),

        "api_key": body.get("api_key", ""),

        "method": body.get("method", "GET"),

        "url_pattern": body.get("url_pattern", "/url/{source}/{song_id}/{quality}"),

        "auth_header": body.get("auth_header", ""),

        "response_type": body.get("response_type", "json"),

        "code_field": body.get("code_field", "code"),

        "code_value": body.get("code_value", 0),

        "url_field": body.get("url_field", "url"),

        "sources": body.get("sources", []),

        "source_type": source_type,

        "usage_limit": 0,

        "usage_count": 0,

        "support_url": True,

        "enabled": True,

        "builtin": False,

    }

    if source_type == "lx_script":

        # Manual LX entries use the same narrow adapter as uploaded scripts.
        # API URL and optional key are the only provider-specific inputs.
        new_source.update({

            "url_pattern": "/music/url?source={source}&songId={song_id}&quality={quality}",

            "auth_header": body.get("auth_header") or ("X-API-Key" if body.get("api_key") else ""),

            "source_aliases": {

                "wy": "netease", "netease": "netease",

                "tx": "qq", "qq": "qq",

                "kg": "kugou", "git": "kugou", "kugou": "kugou",

                "kw": "kuwo", "kuwo": "kuwo",

                "mg": "migu", "migu": "migu",

            },

            "usage_limit": 750,

        })

    test_result = _test_source_detailed(new_source)

    if not test_result.get("ok"):

        return JSONResponse({

            "code": -1,

            "msg": f"验证未通过，未添加：{test_result.get('msg', '无法获取播放地址')}",

        })

    user_sources[sid] = new_source

    cfg["third_sources"] = user_sources

    save_config(cfg)

    return JSONResponse({"code": 0, "msg": f"已验证并启用 {new_source['name']}"})





@app.post("/admin/api/third-sources/delete")

async def admin_delete_third_source(request: Request):

    """删除自定义第三方音源"""

    body = await request.json()

    sid = body.get("id", "")

    if sid in _BUILTIN_SOURCES:

        return JSONResponse({"code": -1, "msg": "内置源不能删除"})

    cfg = load_config()

    user_sources = cfg.get("third_sources", {}) or {}

    if sid not in user_sources:

        return JSONResponse({"code": -1, "msg": "源不存在"})

    del user_sources[sid]

    cfg["third_sources"] = user_sources

    save_config(cfg)

    return JSONResponse({"code": 0, "msg": "已删除"})





@app.post("/admin/api/third-sources/edit")

async def admin_edit_third_source(request: Request):

    """编辑自定义音源"""

    body = await request.json()

    sid = body.get("id", "")

    if sid in _BUILTIN_SOURCES:

        return JSONResponse({"code": -1, "msg": "内置源不能编辑，请到源码修改"})

    cfg = load_config()

    user_sources = cfg.get("third_sources", {}) or {}

    if sid not in user_sources:

        return JSONResponse({"code": -1, "msg": "源不存在"})

    for key in ["name", "desc", "api_url", "method", "url_pattern",

                "auth_header", "response_type", "code_field", "url_field", "sources"]:

        if key in body:

            user_sources[sid][key] = body[key]

    if "api_key" in body and str(body.get("api_key") or "").strip():

        user_sources[sid]["api_key"] = str(body["api_key"]).strip()

    if "code_value" in body:

        user_sources[sid]["code_value"] = body["code_value"]

    if "usage_limit" in body:

        try:

            limit = int(body["usage_limit"])

        except (TypeError, ValueError):

            return JSONResponse({"code": -1, "msg": "调用次数上限必须是整数"}, status_code=422)

        if limit < 0 or limit > 10000000:

            return JSONResponse({"code": -1, "msg": "调用次数上限需为 0 到 10000000，0 表示无限"}, status_code=422)

        user_sources[sid]["usage_limit"] = limit

    cfg["third_sources"] = user_sources

    save_config(cfg)

    return JSONResponse({"code": 0, "msg": "已更新"})





@app.post("/admin/api/third-sources/reset-usage")

async def admin_reset_third_source_usage(request: Request):

    body = await request.json()

    sid = str(body.get("id") or "").strip()

    cfg = load_config()

    sources = cfg.get("third_sources", {}) or {}

    if sid not in sources:

        return JSONResponse({"code": -1, "msg": "源不存在"})

    sources[sid]["usage_count"] = 0

    cfg["third_sources"] = sources

    save_config(cfg)

    return JSONResponse({"code": 0, "msg": "用量已重置"})



@app.post("/admin/api/test-third-source")

async def admin_test_third_source(request: Request):

    """测试第三方音源是否可用"""

    body = await request.json()

    source_id = str(body.get("source_id") or "").strip()
    if source_id:
        cfg = load_config()
        source_def = (cfg.get("third_sources", {}) or {}).get(source_id, {})
        if not source_def:
            return JSONResponse({"code": -1, "msg": "源不存在"})
    else:
        # Compatibility with older admin pages. New pages send only source_id
        # so redacted browser data can never replace a server-side API key.
        source_def = body.get("source_def", {})

    if not source_def:

        return JSONResponse({"code": -1, "msg": "缺少源配置"})

    try:

        result = _test_source_detailed(source_def)

        return JSONResponse({"code": 0, **result})

    except Exception as e:

        return JSONResponse({"code": -1, "msg": str(e)})





def _read_scanner_source(filename):

    """Return a JavaScript source file from the local source library safely."""

    if not isinstance(filename, str) or Path(filename).name != filename:

        return None, "文件名无效"

    if not filename.lower().endswith(".js"):

        return None, "只允许扫描 JS 音源文件"

    path = SOURCE_LIBRARY_DIR / filename

    if not path.is_file():

        return None, "音源文件不存在"

    try:

        return path.read_text(encoding="utf-8", errors="replace"), None

    except OSError as exc:

        return None, str(exc)





@app.get("/admin/api/source-scanner/files")

async def admin_source_scanner_files():

    """List local JavaScript source files without running them."""

    if not SOURCE_LIBRARY_DIR.exists():

        return JSONResponse({"code": 0, "files": []})

    files = sorted(

        path.name for path in SOURCE_LIBRARY_DIR.glob("*.js")

        if path.is_file()

    )

    return JSONResponse({"code": 0, "files": files})





@app.post("/admin/api/source-scanner/inspect")

async def admin_source_scanner_inspect(request: Request):

    """Statically inspect a local source, then test only recognized adapters."""

    body = await request.json()

    filename = body.get("filename", "")

    script_text, error = _read_scanner_source(filename)

    if error:

        return JSONResponse({"code": -1, "msg": error})



    inspection = _detect_source_candidates(script_text, filename)

    candidates = []

    for candidate in inspection.get("candidates", []):

        candidate_copy = dict(candidate)

        candidate_copy["test"] = _test_source_detailed(candidate_copy)

        candidates.append(candidate_copy)

    inspection["candidates"] = candidates

    return JSONResponse({"code": 0, **_public_inspection(inspection)})





@app.post("/admin/api/source-scanner/upload")

async def admin_source_scanner_upload(request: Request):

    """Statically validate and save an uploaded source; live probing is informational only."""

    body = await request.json()

    filename = body.get("filename", "")

    content_base64 = body.get("content_base64", "")

    if not isinstance(filename, str) or Path(filename).name != filename:

        return JSONResponse({"code": -1, "msg": "文件名无效"})

    if not filename.lower().endswith(".js"):

        return JSONResponse({"code": -1, "msg": "只允许上传 JS 音源文件"})

    if not isinstance(content_base64, str) or not content_base64:

        return JSONResponse({"code": -1, "msg": "缺少文件内容"})



    try:

        content = base64.b64decode(content_base64, validate=True)

    except (ValueError, TypeError):

        return JSONResponse({"code": -1, "msg": "文件内容编码无效"})

    if len(content) > 2 * 1024 * 1024:

        return JSONResponse({"code": -1, "msg": "文件不能超过 2MB"})

    try:

        script_text = content.decode("utf-8-sig")

    except UnicodeDecodeError:

        return JSONResponse({"code": -1, "msg": "只支持 UTF-8 编码的 JS 音源文件"})



    inspection = _detect_source_candidates(script_text, filename)

    candidates = []

    for candidate in inspection.get("candidates", []):

        candidate_copy = dict(candidate)

        candidate_copy["test"] = _test_source_detailed(candidate_copy)

        candidates.append(candidate_copy)

    inspection["candidates"] = candidates

    if not candidates:

        return JSONResponse({

            "code": -1,

            "msg": "没有检测到可兼容的受支持回源接口，文件未保存",

            **_public_inspection(inspection),

        })



    SOURCE_LIBRARY_DIR.mkdir(parents=True, exist_ok=True)

    target_path = SOURCE_LIBRARY_DIR / filename

    if target_path.exists():

        return JSONResponse({

            "code": -1,

            "msg": "同名文件已存在，为避免覆盖没有保存",

            **_public_inspection(inspection),

        })

    try:

        target_path.write_bytes(content)

    except OSError as exc:

        return JSONResponse({"code": -1, "msg": f"保存文件失败：{exc}", **_public_inspection(inspection)})



    # The uploaded JavaScript is never executed. The server uses the statically

    # extracted adapter immediately; a live probe is only diagnostic because

    # public resolver services can fail temporarily or reject the sample song.

    enabled_candidates = []

    for candidate in candidates:

        if candidate.get("source_type") not in {"public_resolver_aggregate", "lx_script"}:

            continue

        sid = candidate["id"]

        test_result = candidate.get("test", {}) or {}

        stored_candidate = {key: value for key, value in candidate.items() if key != "test"}

        try:

            cfg = load_config()

            user_sources = cfg.get("third_sources", {}) or {}

            user_sources[sid] = {

                **stored_candidate,

                "enabled": True,

                "builtin": False,

                "support_url": True,

                "usage_limit": int(candidate.get("usage_limit", 750 if candidate.get("source_type") == "lx_script" else 0) or 0),

                "usage_count": int(candidate.get("usage_count", 0) or 0),

                "verification_status": "passed" if test_result.get("ok") else "pending",

                "verification_message": test_result.get("msg", "未完成实时回源验证"),

            }

            cfg["third_sources"] = user_sources

            save_config(cfg)

            enabled_candidates.append(candidate.get("name", sid))

        except OSError as exc:

            logger.exception("Unable to enable uploaded public resolver source")

            return JSONResponse({

                "code": -1,

                "msg": f"文件已保存，但启用音源失败：{exc}",

                **_public_inspection(inspection),

            })



    status = "已静态识别并保存到音乐音源文件夹"

    if enabled_candidates:

        status += "，并已启用：" + "、".join(enabled_candidates) + "（实时验证仅供参考）"

    return JSONResponse({

        "code": 0,

        "msg": f"{filename} {status}",

        "enabled_candidates": enabled_candidates,

        **_public_inspection(inspection),

    })





@app.post("/admin/api/source-scanner/apply")

async def admin_source_scanner_apply(request: Request):

    """Re-inspect a recognized adapter and enable it; live probing is informational only."""

    body = await request.json()

    filename = body.get("filename", "")

    candidate_id = body.get("candidate_id", "")

    script_text, error = _read_scanner_source(filename)

    if error:

        return JSONResponse({"code": -1, "msg": error})



    inspection = _detect_source_candidates(script_text, filename)

    candidate = next(

        (item for item in inspection.get("candidates", []) if item.get("id") == candidate_id),

        None,

    )

    if not candidate:

        return JSONResponse({"code": -1, "msg": "该文件中没有可添加的已识别候选源"})



    test_result = _test_source_detailed(candidate)

    # 与上传流程保持一致：静态适配成功即可写入，实时回源只作为诊断信息。
    # 公共接口临时异常不应阻止确定兼容的 LX 音源接入。



    sid = candidate["id"]

    cfg = load_config()

    user_sources = cfg.get("third_sources", {}) or {}

    if sid in _BUILTIN_SOURCES:

        user_sources.setdefault(sid, {})["enabled"] = True

    else:

        user_sources[sid] = {

            **candidate,

            "enabled": True,

            "builtin": False,

            "support_url": True,

            "usage_limit": int(candidate.get("usage_limit", 750 if candidate.get("source_type") == "lx_script" else 0) or 0),

            "usage_count": int(candidate.get("usage_count", 0) or 0),

            "verification_status": "passed" if test_result.get("ok") else "pending",

            "verification_message": test_result.get("msg", "未完成实时回源验证"),

        }

    cfg["third_sources"] = user_sources

    save_config(cfg)

    verification_note = "实时验证通过" if test_result.get("ok") else "已保存，实时验证未通过（不影响静态添加）"

    return JSONResponse({"code": 0, "msg": f"{candidate.get('name', sid)} 已添加并启用，{verification_note}"})





# ==================== 启动 ====================



def main():

    # Windows 控制台可能仍是 GBK，避免日志里的中文或图标导致服务启动失败。

    if hasattr(sys.stdout, "reconfigure"):

        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if hasattr(sys.stderr, "reconfigure"):

        sys.stderr.reconfigure(encoding="utf-8", errors="replace")



    cfg = load_config()

    port = cfg.get("port", 8100)

    host = cfg.get("host", "0.0.0.0")

    

    # 首次启动检测网易云 cookie

    if cfg.get("cookie") and cfg.get("cookie_status") in ("unknown", None):

        print("🔍 检测网易云 Cookie 有效性...")

        try:

            check_ncm_cookie()

            print(f"   网易云 Cookie 状态: {load_config().get('cookie_status')}")

        except Exception as e:

            print(f"   ⚠️ 网易云 Cookie 检测失败: {e}")

    

    # 首次启动检测 QQ音乐 cookie

    if cfg.get("qq_musickey") and cfg.get("qq_status") in ("unknown", None):

        print("🔍 检测 QQ音乐 Cookie 有效性...")

        try:

            check_qq_cookie()

            print(f"   QQ音乐 Cookie 状态: {load_config().get('qq_status')}")

        except Exception as e:

            print(f"   ⚠️ QQ音乐 Cookie 检测失败: {e}")

    

    # 启动会话自动保活后台调度线程

    renew_thread = threading.Thread(target=auto_renew_scheduler, daemon=True)

    renew_thread.start()

    

    print(f"""

╔══════════════════════════════════════╗

║     🎵 微信点歌 API v1.1.0        ║

╠══════════════════════════════════════╣

║  API:  http://{host}:{port}/api/search              ║

║  管理: http://{host}:{port}/admin                  ║

║  文档: http://{host}:{port}/docs                   ║

╚══════════════════════════════════════╝

""")

    

    uvicorn.run(app, host=host, port=port, log_level="info")





if __name__ == "__main__":

    main()
