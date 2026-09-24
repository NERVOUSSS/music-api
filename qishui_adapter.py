"""Small Qishui Music adapter for account, playlist, search, and lyrics.

Encrypted audio, download URLs, playback authorization, and decryption are
deliberately excluded. Playback continues through the project's existing
cross-platform resolver.
"""
import logging
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import requests


QISHUI_PC_API = "https://api.qishui.com"
QISHUI_LUNA_API = "https://beta-luna.douyin.com"
QISHUI_PUBLIC_API = "https://api-vehicle.volcengine.com"
QISHUI_TIMEOUT = 20
QISHUI_PUBLIC_TIMEOUT = 10
QISHUI_PC_VERSION_NAME = str(os.environ.get("QISHUI_PC_VERSION_NAME", "3.8.0")).strip() or "3.8.0"
QISHUI_PC_VERSION_CODE = str(os.environ.get("QISHUI_PC_VERSION_CODE", "30080000")).strip() or "30080000"
QISHUI_PC_APP_UA = str(os.environ.get("QISHUI_PC_APP_UA", f"LunaPC/{QISHUI_PC_VERSION_NAME}(467160162)")).strip()
QISHUI_PC_CONTEXT_FILE = Path(os.environ.get("QISHUI_PC_CONTEXT_FILE", Path(__file__).parent / "data" / ".qishui-pc-context.json"))
QISHUI_PC_DEVICE_FILE = Path(os.environ.get("QISHUI_PC_DEVICE_FILE", Path(__file__).parent / "data" / ".qishui-pc-device.json"))
QISHUI_PC_CHANNEL = str(os.environ.get("QISHUI_PC_CHANNEL", "official")).strip() or "official"
QISHUI_PC_OS_VERSION = str(os.environ.get("QISHUI_PC_OS_VERSION", "Windows 10 Pro")).strip() or "Windows 10 Pro"
QISHUI_PUBLIC_HEADERS = {
    "Accept": "application/json,text/plain,*/*",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/136.0 Safari/537.36",
    "Referer": "https://www.qishui.com/",
}
LOGGER = logging.getLogger("qishui_adapter")
AUTH_COOKIE_NAMES = (
    "sessionid",
    "sessionid_ss",
    "sid_tt",
    "sid_guard",
    "uid_tt",
    "uid_tt_ss",
)

PASSPORT = {
    "passport_jssdk_version": "2.4.13",
    "passport_jssdk_type": "normal",
    "is_from_ttaccountsdk": "1",
    "aid": "386088",
    "next": QISHUI_PC_API,
    "need_logo": "false",
    "need_short_url": "false",
    "is_new_login": "1",
    "iid": "",
    "version_code": QISHUI_PC_VERSION_CODE,
}


def _read_json_file(path):
    try:
        if not path.exists():
            return {}
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _random_device_id():
    # The PC client uses numeric identifiers. Persisting them prevents a new
    # device identity on every request, without copying a user's real ID.
    import secrets

    return str(secrets.randbelow(9000000000000000) + 1000000000000000)


def _pc_device_context():
    stored = _read_json_file(QISHUI_PC_DEVICE_FILE)

    def value(env_names, file_names):
        for name in env_names:
            candidate = str(os.environ.get(name, "")).strip()
            if candidate:
                return candidate
        for name in file_names:
            candidate = str(stored.get(name, "")).strip()
            if candidate:
                return candidate
        return ""

    device_id = value(("QISHUI_DEVICE_ID", "QISHUI_PC_DEVICE_ID"), ("device_id", "deviceId"))
    iid = value(("QISHUI_IID", "QISHUI_PC_IID"), ("iid", "install_id", "installId"))
    device_id = device_id if re.fullmatch(r"\d{16}", device_id) else _random_device_id()
    iid = iid if re.fullmatch(r"\d{16}", iid) else _random_device_id()
    if stored.get("device_id") != device_id or stored.get("iid") != iid:
        try:
            QISHUI_PC_DEVICE_FILE.parent.mkdir(parents=True, exist_ok=True)
            QISHUI_PC_DEVICE_FILE.write_text(
                json.dumps({"device_id": device_id, "iid": iid}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            LOGGER.debug("Unable to persist Qishui PC device context", exc_info=True)
    return {"device_id": device_id, "iid": iid}


def _pc_context():
    stored = _read_json_file(QISHUI_PC_CONTEXT_FILE)
    device = _pc_device_context()

    def value(env_names, file_names, default=""):
        for name in env_names:
            candidate = str(os.environ.get(name, "")).strip()
            if candidate:
                return candidate
        for name in file_names:
            candidate = str(stored.get(name, "")).strip()
            if candidate:
                return candidate
        return default

    return {
        "device_id": device["device_id"],
        "iid": device["iid"],
        "version_name": value(("QISHUI_PC_VERSION_NAME",), ("QISHUI_PC_VERSION_NAME",), QISHUI_PC_VERSION_NAME),
        "version_code": value(("QISHUI_PC_VERSION_CODE",), ("QISHUI_PC_VERSION_CODE",), QISHUI_PC_VERSION_CODE),
        "user_agent": value(("QISHUI_PC_APP_UA",), ("QISHUI_PC_APP_UA", "userAgent"), QISHUI_PC_APP_UA),
        "channel": value(("QISHUI_PC_CHANNEL",), ("QISHUI_PC_CHANNEL", "channel"), QISHUI_PC_CHANNEL),
        "os_version": value(("QISHUI_PC_OS_VERSION",), ("QISHUI_PC_OS_VERSION", "osVersion"), QISHUI_PC_OS_VERSION),
        "x-helios": value(("QISHUI_X_HELIOS", "X_HELIOS"), ("QISHUI_X_HELIOS", "x-helios")),
        "x-medusa": value(("QISHUI_X_MEDUSA", "X_MEDUSA"), ("QISHUI_X_MEDUSA", "x-medusa")),
        "x-ss-stub": value(("QISHUI_X_SS_STUB", "X_SS_STUB"), ("QISHUI_X_SS_STUB", "x-ss-stub")),
    }


def qishui_pc_context_diagnostic():
    context = _pc_context()
    return {
        "version_name": context["version_name"],
        "version_code": context["version_code"],
        "device_id_ready": bool(re.fullmatch(r"\d{16}", context["device_id"])),
        "iid_ready": bool(re.fullmatch(r"\d{16}", context["iid"])),
        "dynamic_headers": {
            "helios": len(context["x-helios"]),
            "medusa": len(context["x-medusa"]),
            "ss_stub": len(context["x-ss-stub"]),
        },
    }


PASSPORT["iid"] = _pc_context()["iid"]
QR_CREATE_PARAMS = {
    key: PASSPORT[key]
    for key in (
        "passport_jssdk_version",
        "passport_jssdk_type",
        "is_from_ttaccountsdk",
        "aid",
        "next",
        "need_logo",
        "need_short_url",
        "is_new_login",
    )
}
QR_STATUS_PARAMS = {
    key: PASSPORT[key]
    for key in (
        "passport_jssdk_version",
        "passport_jssdk_type",
        "is_from_ttaccountsdk",
        "aid",
        "iid",
    )
}


class QishuiError(ValueError):
    pass


def _json(response):
    if not response.ok:
        try:
            body = response.json()
        except ValueError:
            body = {}
        message = (body.get("message") or body.get("msg")) if isinstance(body, dict) else ""
        raise QishuiError(message or f"Qishui request failed ({response.status_code})")
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError as exc:
        raise QishuiError("Qishui returned invalid JSON") from exc


def _data(body):
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        return body["data"]
    return body or {}


def _cookie_from_headers(headers):
    values = headers.get("set-cookie") or headers.get("Set-Cookie") or []
    if isinstance(values, str):
        values = [values]
    return "; ".join(str(value).split(";", 1)[0].strip() for value in values if str(value).strip())


def _merge_cookies(*cookies):
    values = {}
    for cookie_header in cookies:
        for item in str(cookie_header or "").split(";"):
            name, separator, value = item.strip().partition("=")
            if separator and name:
                values[name] = value
    return "; ".join(f"{name}={value}" for name, value in values.items())


def _response_cookies(response):
    cookies = []
    # Passport can set the useful session cookie on an intermediate redirect.
    # Keep the whole redirect chain server-side; only the final status leaves
    # this adapter.
    for current in [*(getattr(response, "history", []) or []), response]:
        try:
            cookies.extend(f"{cookie.name}={cookie.value}" for cookie in current.cookies)
        except (AttributeError, TypeError):
            pass
        cookies.append(_cookie_from_headers(current.headers))
    return _merge_cookies(_cookie_from_headers(response.headers), "; ".join(cookies))


def _sessionid_from_cookie(cookie_header):
    cookie_text = str(cookie_header or "")
    for cookie_name in AUTH_COOKIE_NAMES:
        match = re.search(rf"(?:^|;\s*){re.escape(cookie_name)}=([^;]+)", cookie_text, re.I)
        if match:
            return match.group(1).strip()
    return ""


def _has_authorization_cookie(cookie_header):
    names = {name.lower() for name in _cookie_names(cookie_header)}
    return any(name in names for name in AUTH_COOKIE_NAMES)


def _sessionid_from_body(value):
    """Read only explicit session fields returned by Passport, if any."""
    if isinstance(value, dict):
        for key in ("sessionid", "session_id"):
            candidate = str(value.get(key) or "").strip()
            if candidate and ";" not in candidate and not any(char.isspace() for char in candidate):
                return candidate
        for item in value.values():
            candidate = _sessionid_from_body(item)
            if candidate:
                return candidate
    elif isinstance(value, list):
        for item in value:
            candidate = _sessionid_from_body(item)
            if candidate:
                return candidate
    return ""


def _cookie_names(cookie_header):
    return sorted({item.strip().partition("=")[0] for item in str(cookie_header or "").split(";") if "=" in item})


def _safe_body_keys(value):
    if not isinstance(value, dict):
        return []
    return sorted(str(key) for key in value.keys())[:30]


def _authorization_redirect(value):
    """Find Passport's post-confirmation URL without accepting arbitrary URLs."""
    if isinstance(value, dict):
        for key in ("redirect_url", "redirect", "login_url", "next_url", "url"):
            candidate = str(value.get(key) or "").strip()
            parsed = urlparse(candidate)
            host = parsed.hostname or ""
            if parsed.scheme == "https" and host.endswith(("qishui.com", "douyin.com", "bytedance.com")):
                return candidate
        for item in value.values():
            candidate = _authorization_redirect(item)
            if candidate:
                return candidate
    elif isinstance(value, list):
        for item in value:
            candidate = _authorization_redirect(item)
            if candidate:
                return candidate
    return ""


def _follow_authorization_redirect(url, cookie):
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html,application/json,*/*", "Cookie": cookie},
            timeout=QISHUI_TIMEOUT,
        )
    except requests.RequestException as exc:
        LOGGER.info("Qishui QR authorization redirect failed: %s", type(exc).__name__)
        return cookie
    LOGGER.info(
        "Qishui QR authorization redirect host=%s status=%s redirects=%s cookie_names=%s",
        urlparse(url).hostname or "",
        response.status_code,
        [item.status_code for item in (getattr(response, "history", []) or [])],
        _cookie_names(_response_cookies(response)),
    )
    return _merge_cookies(cookie, _response_cookies(response))


def _session_cookie(session_or_cookie):
    """Accept a complete server-side cookie context or the legacy sessionid."""
    value = str(session_or_cookie or "").strip()
    if not value:
        raise QishuiError("Qishui session is invalid. Scan the QR code again.")
    if "=" in value:
        return _merge_cookies(value)
    if ";" in value or any(char.isspace() for char in value):
        raise QishuiError("Qishui session is invalid. Scan the QR code again.")
    return f"sessionid={value};"


def _pc_headers(session_or_cookie=""):
    context = _pc_context()
    headers = {
        "User-Agent": context["user_agent"],
        "Accept": "application/json,text/plain,*/*",
        "Content-Type": "application/json; charset=utf-8",
        "x-luna-background-type": "foreground",
        "x-luna-is-background-req": "0",
        "x-luna-is-local-user": "1",
        "Referer": "https://www.qishui.com/",
    }
    for name in ("x-helios", "x-medusa", "x-ss-stub"):
        if context[name]:
            headers[name] = context[name]
    if session_or_cookie:
        headers["Cookie"] = _session_cookie(session_or_cookie)
    return headers


def _get(host, path, session_or_cookie="", params=None):
    # Match the PC Web client: omit empty optional parameters.
    query = {
        key: value
        for key, value in (params or {}).items()
        if value is not None and value != ""
    }
    response = requests.get(
        f"{host}{path}",
        params=query,
        headers=_pc_headers(session_or_cookie),
        timeout=QISHUI_TIMEOUT,
    )
    return _json(response)


def _search_params(keyword, page=1, limit=20):
    context = _pc_context()
    try:
        cursor = max(0, (int(page) - 1) * int(limit))
    except (TypeError, ValueError):
        cursor = 0
    return {
        "aid": "386088",
        "app_name": "luna_pc",
        "region": "cn",
        "geo_region": "cn",
        "os_region": "cn",
        "sim_region": "",
        "device_id": context["device_id"],
        "cdid": "",
        "iid": context["iid"],
        "version_name": context["version_name"],
        "version_code": context["version_code"],
        "channel": context["channel"],
        "build_mode": "master",
        "network_carrier": "",
        "ac": "wifi",
        "tz_name": "Asia/Shanghai",
        "resolution": "",
        "device_platform": "windows",
        "device_type": "Windows",
        "os_version": context["os_version"],
        "fp": context["device_id"],
        "q": str(keyword).strip(),
        "cursor": str(cursor),
        "count": int(limit),
        "search_id": "",
        "search_method": "input",
        "debug_params": "",
        "from_search_id": "",
        "search_scene": "",
    }


def _url(value):
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return value
    if isinstance(value, dict):
        direct = value.get("url")
        if isinstance(direct, str) and direct.startswith(("http://", "https://")):
            return direct
        uri = str(value.get("uri") or "").strip("/")
        urls = value.get("urls")
        if uri and isinstance(urls, list):
            base = next((item for item in urls if isinstance(item, str) and item.startswith(("http://", "https://"))), "")
            if base:
                prefix = str(value.get("template_prefix") or "").strip()
                suffix = f"~{prefix}-crop-center:400:400.jpg" if prefix else ""
                return f"{base.rstrip('/')}/{uri}{suffix}"
        if isinstance(urls, list) and urls:
            return _url(urls[0])
        return _url(direct or value.get("uri"))
    if isinstance(value, list):
        return _url(value[0]) if value else ""
    return ""


def _track(resource):
    resource = resource if isinstance(resource, dict) else {}
    entity = resource.get("entity") or {}
    wrapper = entity.get("track_wrapper") if isinstance(entity, dict) else {}
    track = (
        (wrapper or {}).get("track")
        or entity.get("track")
        or resource.get("track")
        or resource
    )
    if not isinstance(track, dict) or not track.get("id"):
        return None
    artists = track.get("artists") or []
    artist = "/".join(str(item.get("name") or "") for item in artists if isinstance(item, dict)).strip()
    album = track.get("album") or {}
    cover = _url(album.get("url_cover") or track.get("url_cover") or track.get("cover_url"))
    return {
        "id": str(track.get("id")),
        "title": str(track.get("name") or ""),
        "artist": artist,
        "album": str(album.get("name") or ""),
        "cover": cover.replace("http://", "https://", 1),
        "duration": int(track.get("duration") or track.get("duration_ms") or 0),
        "extra": "",
        "source": "qishui",
        "vip": bool((track.get("label_info") or {}).get("only_vip_playable")),
    }


def krc_to_lrc(text):
    lines = []
    for raw in str(text or "").replace("\r", "").split("\n"):
        match = re.match(r"\[(\d+),(\d+)\](.*)", raw.strip())
        if not match:
            if raw.strip() and not raw.lstrip().startswith("[id:"):
                lines.append(raw.strip())
            continue
        start = int(match.group(1))
        words = re.findall(r"<\d+,\d+(?:,\d+)?>((?:[^<>]|<(?!\d+,))*)", match.group(3))
        content = "".join(words).strip() or re.sub(r"<\d+,\d+(?:,\d+)?>", "", match.group(3)).strip()
        if content:
            minutes, rest = divmod(start, 60000)
            seconds, millis = divmod(rest, 1000)
            lines.append(f"[{minutes:02d}:{seconds:02d}.{millis:03d}]{content}")
    return "\n".join(lines)


def start_qr():
    response = requests.get(
        f"{QISHUI_PC_API}/passport/web/get_qrcode/",
        params=QR_CREATE_PARAMS,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json, text/plain, */*"},
        timeout=QISHUI_TIMEOUT,
    )
    data = _data(_json(response))
    image = str(data.get("qrcode") or "")
    token = str(data.get("token") or "")
    if not token or not image.startswith("data:image/"):
        raise QishuiError("Could not generate a Qishui Music QR code")
    return {
        "token": token,
        "qr_image": image,
        "expires_in": 300,
        "cookie": _response_cookies(response),
        "scan_app": "qishui",
        "message": "Use the logged-in Qishui Music app to scan and confirm.",
    }


def poll_qr(token, qr_cookie):
    if not token or not qr_cookie:
        raise QishuiError("QR session expired. Refresh the code and try again.")
    form = {
        "need_logo": "false",
        "need_short_url": "false",
        "is_frontier": "true",
        "token": token,
        "is_new_login": "1",
        "next": QISHUI_PC_API,
    }
    response = requests.post(
        f"{QISHUI_PC_API}/passport/web/check_qrconnect/",
        params=QR_STATUS_PARAMS,
        data=form,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0",
            "Cookie": qr_cookie,
        },
        timeout=QISHUI_TIMEOUT,
    )
    body = _json(response)
    data = _data(body)
    status = str(data.get("status", body.get("status", ""))).lower()
    updated_cookie = _merge_cookies(qr_cookie, _response_cookies(response))
    redirect_url = _authorization_redirect(body)
    if redirect_url and status in ("2", "3", "success", "authorized", "confirm", "confirmed"):
        updated_cookie = _follow_authorization_redirect(redirect_url, updated_cookie)
    sessionid = _sessionid_from_cookie(updated_cookie) or _sessionid_from_body(body)
    diagnostic = {
        "upstream_status": status or "unknown",
        "error_code": str(data.get("error_code", body.get("error_code", ""))) if isinstance(body, dict) else "",
        "body_keys": _safe_body_keys(body),
        "data_keys": _safe_body_keys(data),
        "cookie_names": _cookie_names(updated_cookie),
        "redirect_statuses": [item.status_code for item in (getattr(response, "history", []) or [])],
        "has_authorization_cookie": _has_authorization_cookie(updated_cookie),
        "has_sessionid": bool(sessionid),
        "has_authorization_redirect": bool(redirect_url),
    }
    LOGGER.info("Qishui QR poll diagnostic=%s", diagnostic)
    error_code = str(diagnostic["error_code"] or "")
    if error_code not in ("", "0"):
        if error_code == "7":
            message = "汽水音乐要求在官方客户端完成额外安全验证，当前授权未下发网页会话。"
        else:
            message = f"汽水音乐授权未通过（上游错误码 {error_code}）。"
        return {"status": "failed", "message": message, "cookie": updated_cookie, "diagnostic": diagnostic}
    if sessionid:
        if not _sessionid_from_cookie(updated_cookie):
            updated_cookie = _merge_cookies(updated_cookie, f"sessionid={sessionid}")
        return {"status": "authorized", "sessionid": sessionid, "cookie": updated_cookie, "diagnostic": diagnostic}
    if status in ("2", "3", "success", "authorized", "confirm", "confirmed") and _has_authorization_cookie(updated_cookie):
        return {"status": "authorized", "sessionid": "", "cookie": updated_cookie, "diagnostic": diagnostic}
    if status in ("0", "new", "waiting", "wait"):
        return {"status": "waiting", "message": "Waiting for Qishui Music app scan.", "cookie": updated_cookie, "diagnostic": diagnostic}
    if status in ("1", "scan", "scanned"):
        return {"status": "scanned", "message": "Scanned. Confirm authorization in Qishui Music.", "cookie": updated_cookie, "diagnostic": diagnostic}
    if status in ("2", "3", "success", "authorized", "confirm", "confirmed"):
        return {"status": "scanned", "message": "Confirmed. Waiting for the Qishui session.", "cookie": updated_cookie, "diagnostic": diagnostic}
    if status in ("-1", "4", "5", "expired"):
        return {"status": "expired", "message": "QR code expired. Refresh and try again.", "cookie": updated_cookie, "diagnostic": diagnostic}
    return {"status": "waiting", "message": "Waiting for Qishui Music authorization.", "cookie": updated_cookie, "diagnostic": diagnostic}


def me(sessionid):
    body = _data(_get(QISHUI_PC_API, "/luna/pc/me", sessionid, {"aid": PASSPORT["aid"]}))
    info = body.get("my_info") or body.get("user") or body
    return {"id": str(info.get("id") or ""), "nickname": str(info.get("nickname") or info.get("name") or "Qishui user")}


def playlists(sessionid):
    body = _data(_get(QISHUI_PC_API, "/luna/pc/me/playlist", sessionid, {
        "aid": PASSPORT["aid"], "iid": PASSPORT["iid"], "version_code": PASSPORT["version_code"],
    }))
    items = body.get("playlists") or []
    return [
        (str(item.get("id")), str(item.get("title") or item.get("name") or "Qishui playlist"))
        for item in items
        if isinstance(item, dict) and item.get("id")
    ]


def playlist_detail(sessionid, playlist_id):

    playlist_id = str(playlist_id or "").strip()

    if not playlist_id:

        raise QishuiError("Qishui playlist id is missing")

    songs = []

    seen_ids = set()

    cursor = ""

    playlist = {}

    max_pages = 100

    for _ in range(max_pages):

        body = _data(_get(QISHUI_PC_API, "/luna/pc/playlist/detail", sessionid, {

            "aid": PASSPORT["aid"],

            "iid": PASSPORT["iid"],

            "version_code": PASSPORT["version_code"],

            "playlist_id": playlist_id,

            "cursor": cursor,

            "count": 100,

        }))

        if not playlist:

            playlist = body.get("playlist") or {}

        page_items = body.get("media_resources") or []

        page_songs = [_track(item) for item in page_items]

        page_songs = [song for song in page_songs if song]

        added = 0

        for song in page_songs:

            if song["id"] not in seen_ids:

                seen_ids.add(song["id"])

                songs.append(song)

                added += 1

        next_cursor = str(body.get("next_cursor") or "").strip()

        if not added or not next_cursor or next_cursor == cursor:

            break

        cursor = next_cursor

    if not songs:

        raise QishuiError("The Qishui playlist did not return synchronizable tracks")

    return str(playlist.get("title") or playlist.get("name") or "Qishui playlist"), songs


def _search_candidates(body):
    candidates = []
    for key in ("result_groups", "groups", "resources", "tracks", "songs", "data"):
        value = body.get(key) if isinstance(body, dict) else None
        if isinstance(value, list):
            candidates.extend(value)
    result = body.get("result") if isinstance(body, dict) else None
    if isinstance(result, dict):
        for key in ("tracks", "songs", "data", "resources"):
            value = result.get(key)
            if isinstance(value, list):
                candidates.extend(value)
    return candidates


def _public_search_text(value):
    return re.sub(r"[\W_]+", "", str(value or "").casefold())


def _public_search_score(song, keyword):
    query = _public_search_text(keyword)
    if not query:
        return 0
    title = _public_search_text(song.get("title"))
    artist = _public_search_text(song.get("artist"))
    album = _public_search_text(song.get("album"))
    score = 0
    if title == query:
        score += 180
    elif query in title:
        score += 120
    elif title and title in query and len(title) >= 2:
        score += 70
    if artist == query:
        score += 150
    elif query in artist:
        score += 105
    if album == query:
        score += 80
    elif query in album:
        score += 45
    return score


def _public_track(resource, index, keyword):
    resource = resource if isinstance(resource, dict) else {}
    author = resource.get("author_info") or resource.get("author") or resource.get("artist") or {}
    album = resource.get("album_info") or resource.get("album") or {}
    if not isinstance(author, dict):
        author = {"name": author}
    if not isinstance(album, dict):
        album = {"name": album}
    song_id = str(
        resource.get("item_id")
        or resource.get("id")
        or resource.get("song_id")
        or resource.get("music_id")
        or f"qishui-public-{index}-{keyword}"
    ).strip()
    title = str(resource.get("title") or resource.get("name") or resource.get("song_name") or "").strip()
    if not song_id or not title:
        return None
    try:
        raw_duration = float(resource.get("duration") or resource.get("duration_ms") or 0)
    except (TypeError, ValueError):
        raw_duration = 0
    duration = int(raw_duration * 1000) if 0 < raw_duration < 10000 else int(raw_duration)
    label_info = resource.get("qishui_label_info") or {}
    if not isinstance(label_info, dict):
        label_info = {}
    cover = _url(resource.get("cover_url") or resource.get("cover") or resource.get("artwork") or album.get("cover_url"))
    return {
        "id": song_id,
        "title": title,
        "artist": str(author.get("name") or resource.get("author_name") or resource.get("artist_name") or resource.get("singer") or "").strip(),
        "album": str(album.get("name") or resource.get("album_name") or "").strip(),
        "cover": cover.replace("http://", "https://", 1),
        "duration": duration,
        "extra": "",
        "source": "qishui",
        "vip": bool(label_info.get("only_vip_playable")),
        # The public catalogue is metadata only. Playback must continue through
        # the existing cross-platform fallback resolver.
        "playable": False,
        "playback_mode": "recommend-match",
    }


def _public_search(keyword, page=1, limit=20):
    try:
        page = max(1, int(page))
        limit = max(1, min(30, int(limit)))
    except (TypeError, ValueError):
        page, limit = 1, 20
    offset = (page - 1) * limit
    request_limit = min(100, max(offset + limit * 3, 36))
    response = requests.get(
        f"{QISHUI_PUBLIC_API}/v2/search/type",
        params={
            "keyword": str(keyword).strip(),
            "search_type": "music",
            "limit": request_limit,
            # The endpoint may repeat the first page for non-zero offsets;
            # fetch a bounded window and paginate the ranked local results.
            "real_offset": 0,
            "search_source": "qishui",
        },
        headers=QISHUI_PUBLIC_HEADERS,
        timeout=QISHUI_PUBLIC_TIMEOUT,
    )
    body = _data(_json(response))
    raw_items = body.get("list") if isinstance(body, dict) else []
    if not isinstance(raw_items, list):
        raw_items = []
    mapped = [
        song
        for index, item in enumerate(raw_items)
        for song in [_public_track(item, index, keyword)]
        if song
    ]
    ranked = sorted(
        enumerate(mapped),
        key=lambda item: (-_public_search_score(item[1], keyword), item[0]),
    )
    return [song for _, song in ranked[offset:offset + limit]]


def search(keyword, sessionid="", page=1, limit=20):
    official_error = ""
    if sessionid:
        try:
            body = _data(_get(QISHUI_PC_API, "/luna/pc/search/track", sessionid, _search_params(keyword, page, limit)))
            songs, seen = [], set()
            for item in _search_candidates(body):
                nested = item.get("data") if isinstance(item, dict) and isinstance(item.get("data"), list) else [item]
                for resource in nested:
                    song = _track(resource)
                    if song and song["id"] not in seen:
                        songs.append(song)
                        seen.add(song["id"])
            if songs:
                return songs[:int(limit)]
        except (QishuiError, requests.RequestException) as exc:
            # An empty/failed PC response is not proof that the account session
            # expired. Try the same public catalogue used by the reference
            # client before reporting a search failure.
            official_error = str(exc)
    try:
        return _public_search(keyword, page=page, limit=limit)
    except (QishuiError, requests.RequestException) as exc:
        if official_error:
            raise QishuiError(f"汽水官方搜索不可用，公开目录搜索也失败：{exc}") from exc
        raise QishuiError(f"汽水公开搜索失败：{exc}") from exc


def public_playlist(playlist_url):
    """Read a public Qishui playlist share page without requiring an account session.

    The endpoint deliberately consumes only server-rendered metadata and track
    information. It never reads encrypted audio URLs or playback credentials.
    """
    value = str(playlist_url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not (parsed.hostname or "").lower().endswith("douyin.com"):
        raise QishuiError("请粘贴汽水音乐（抖音域名）的歌单分享链接")
    try:
        response = requests.get(
            value,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html,application/xhtml+xml"},
            timeout=QISHUI_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise QishuiError(f"汽水音乐歌单分享页请求失败：{exc}") from exc
    if not response.ok:
        raise QishuiError(f"汽水音乐歌单分享页返回异常状态码 {response.status_code}")

    final_url = urlparse(response.url)
    query = dict(item.split("=", 1) if "=" in item else (item, "") for item in final_url.query.split("&") if item)
    playlist_id = str(query.get("playlist_id") or "").strip()
    if not playlist_id.isdigit() or not re.search(r"/qishui/share/playlist/?$", final_url.path):
        raise QishuiError("链接不是有效的汽水音乐歌单分享地址")

    router_match = re.search(r"<script[^>]*>\s*_ROUTER_DATA\s*=\s*(\{[\s\S]*?)</script>", response.text, re.I)
    if not router_match:
        raise QishuiError("未能从汽水音乐分享页解析出歌单数据")
    try:
        raw_data = router_match.group(1).strip()
        router_data, _ = json.JSONDecoder().raw_decode(raw_data)
        page = ((router_data.get("loaderData") or {}).get("playlist_page") or {})
    except (ValueError, TypeError, AttributeError) as exc:
        raise QishuiError("汽水音乐分享页数据格式无法解析") from exc

    info = page.get("playlistInfo") or {}
    songs = [_track(item) for item in (page.get("medias") or [])]
    songs = [song for song in songs if song]
    if not songs:
        raise QishuiError("该汽水音乐歌单没有可导入的歌曲")
    return {
        "id": playlist_id,
        "name": str(info.get("title") or info.get("public_title") or "汽水音乐歌单"),
        "cover": _url(info.get("url_cover")).replace("http://", "https://", 1),
        "songs": songs,
    }


def lyric(track_id, sessionid=""):
    headers = {"User-Agent": "Mozilla/5.0"}
    if sessionid:
        headers["Cookie"] = _session_cookie(sessionid)
    response = requests.get(
        f"{QISHUI_LUNA_API}/luna/h5/seo_track",
        params={"track_id": str(track_id), "device_platform": "web"},
        headers=headers,
        timeout=QISHUI_TIMEOUT,
    )
    body = _data(_json(response))
    lyric_body = body.get("lyric") or {}
    raw = lyric_body.get("content") if isinstance(lyric_body, dict) else lyric_body
    return krc_to_lrc(raw or "")
