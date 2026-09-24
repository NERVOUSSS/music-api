"""Minimal Migu Music catalogue adapter.

The adapter only uses fields returned by Migu's public catalogue response.  It
never derives protected media URLs or attempts to bypass account, membership,
region, or copyright controls.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any
from urllib.parse import urlparse

import requests

LOGGER = logging.getLogger("music-sources")
MIGU_SEARCH_URL = "https://pd.musicapp.migu.cn/MIGUM3.0/v1.0/content/search_all.do"
MIGU_TIMEOUT = 15
_CACHE_TTL_SECONDS = 20 * 60
_TRACK_CACHE: dict[str, dict[str, Any]] = {}

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
    "Referer": "https://music.migu.cn/",
    "Accept": "application/json, text/plain, */*",
}


def _clean_url(value: Any) -> str:
    value = str(value or "").strip()
    parsed = urlparse(value)
    return value if parsed.scheme == "https" and parsed.netloc else ""


def _value(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = record.get(key)
        if value not in (None, "", [], {}):
            return value
    return ""


def _artist(value: Any) -> str:
    if isinstance(value, list):
        return "/".join(
            str(item.get("name") or item.get("singerName") or item.get("artistName") or "").strip()
            if isinstance(item, dict) else str(item).strip()
            for item in value
            if item
        )
    if isinstance(value, dict):
        return str(value.get("name") or value.get("singerName") or value.get("artistName") or "").strip()
    return str(value or "").strip()


def _cover(value: Any) -> str:
    if isinstance(value, list):
        for item in value:
            url = _cover(item)
            if url:
                return url
        return ""
    if isinstance(value, dict):
        return _clean_url(_value(value, "img", "imgUrl", "url", "imgItem"))
    return _clean_url(value)


def _duration_ms(value: Any) -> int:
    try:
        raw = float(value or 0)
    except (TypeError, ValueError):
        return 0
    return int(raw * 1000) if 0 < raw < 10000 else int(raw)


def _song_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Read only documented-ish song-list shapes seen across Migu web releases."""
    candidates: list[Any] = [
        ((payload.get("songResultData") or {}).get("result")),
        ((payload.get("data") or {}).get("songs")),
        ((payload.get("data") or {}).get("songList")),
        payload.get("songs"),
        payload.get("result"),
    ]
    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
        if isinstance(candidate, dict):
            for key in ("result", "songs", "songList", "items", "list"):
                nested = candidate.get(key)
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]
    return []


def _track(record: dict[str, Any]) -> dict[str, Any]:
    song_id = str(_value(record, "songId", "copyrightId", "contentId", "id", "musicId"))
    title = str(_value(record, "songName", "name", "title"))
    artist = _artist(_value(record, "singers", "singer", "artists", "artist", "artistName"))
    album = _value(record, "album")
    album_name = (
        str(_value(album, "name", "albumName", "title")) if isinstance(album, dict)
        else str(_value(record, "albumName", "album", "albumTitle"))
    )
    cover = _cover(_value(record, "imgItems", "cover", "coverUrl", "albumPic", "albumPicUrl"))
    play_url = _clean_url(_value(record, "listenUrl", "playUrl", "audioUrl", "mp3Url", "url"))
    lyric_url = _clean_url(_value(record, "lyricUrl", "lrcUrl", "lyricsUrl"))
    return {
        "id": song_id,
        "source": "migu",
        "title": title,
        "artist": artist,
        "album": album_name,
        "cover": cover,
        "duration": _duration_ms(_value(record, "duration", "durationMs", "length", "playTime")),
        # `copyrightId` identifies the licensed work; the playback API also
        # requires the *distinct* `contentId`.  Keep the latter in `extra` so
        # the player can forward it even after a cache miss or a page refresh.
        "extra": str(_value(record, "contentId", "resourceId", "id", "copyrightId")),
        "_play_url": play_url,
        "_lyric_url": lyric_url,
    }


def _cache_track(track: dict[str, Any]) -> None:
    song_id = str(track.get("id") or "")
    if not song_id:
        return
    _TRACK_CACHE[song_id] = {"expires_at": time.monotonic() + _CACHE_TTL_SECONDS, **track}
    if len(_TRACK_CACHE) > 500:
        now = time.monotonic()
        for key in list(_TRACK_CACHE):
            if _TRACK_CACHE[key].get("expires_at", 0) <= now:
                _TRACK_CACHE.pop(key, None)


def _cached(song_id: str) -> dict[str, Any]:
    item = _TRACK_CACHE.get(str(song_id)) or {}
    if item.get("expires_at", 0) <= time.monotonic():
        _TRACK_CACHE.pop(str(song_id), None)
        return {}
    return item


def search(keyword: str, page: int = 1, limit: int = 20) -> tuple[list[dict[str, Any]], str]:
    """Search Migu's public catalogue and retain only its supplied metadata."""
    params = {
        "text": keyword,
        "pageNo": max(1, int(page)),
        "pageSize": min(30, max(1, int(limit))),
        "searchSwitch": json.dumps({"song": 1}, separators=(",", ":")),
    }
    try:
        response = requests.get(MIGU_SEARCH_URL, params=params, headers=_HEADERS, timeout=MIGU_TIMEOUT)
        if not response.ok:
            return [], f"咪咕音乐目录请求失败（HTTP {response.status_code}）"
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        LOGGER.info("Migu search failed: %s", type(exc).__name__)
        return [], "咪咕音乐目录暂时不可用，请稍后重试"

    tracks = []
    for record in _song_records(payload if isinstance(payload, dict) else {}):
        track = _track(record)
        if not track["id"] or not track["title"]:
            continue
        _cache_track(track)
        tracks.append({key: value for key, value in track.items() if not key.startswith("_")})
    return tracks, ""


def playback_url(song_id: str) -> str:
    """Return an HTTPS stream URL only when the official search response supplied it."""
    return _clean_url(_cached(song_id).get("_play_url"))


# ==================== Cookie-aware playback (member account) ====================

MIGU_PC_LISTEN_URL = "https://app.c.nf.migu.cn/strategy/pc/listen/v2.0"
MIGU_TONE_FLAG = "HQ"  # SQ=lossless, HQ=high, PQ=standard
_MIGU_ENCRYPT_KEY = "Jk8qzuePiJ1qE3mDYhLQ3T73DtDoAhLP"
_MIGU_MAGIC = bytes([171, 205, 1])


def _migu_decrypt(data: bytes, key: str = _MIGU_ENCRYPT_KEY) -> str | None:
    """Decrypt the encrypted response from the Migu v5 PC listen API."""
    if len(data) < 4 or data[0:3] != _MIGU_MAGIC:
        return None
    rand_byte = data[3]
    key_bytes = key.encode("utf-8")
    key_len = len(key_bytes)
    result = bytearray(len(data) - 4)
    for i in range(4, len(data)):
        result[i - 4] = (data[i] + rand_byte - key_bytes[(i - 4) % key_len]) & 0xFF
    return bytes(result).decode("utf-8", errors="replace")


def playback_url_with_cookie(song_id: str, cookie: str = "", content_id: str = "", quality: str = "320k") -> str:
    """Resolve a playback URL using the user's Migu session cookie.

    Uses the v5 PC listen API with encrypted responses. The endpoint requires
    a valid web session cookie and returns the stream URL for member content.
    Falls back to the catalogue-supplied URL on failure.
    """
    if not cookie or not cookie.strip():
        return playback_url(song_id)

    cached = _cached(song_id)
    content_id = str(content_id or cached.get("extra") or song_id)
    copyright_id = str(cached.get("id") or song_id)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://music.migu.cn/",
        "Origin": "https://music.migu.cn",
        "Cookie": cookie.strip(),
        "birth": "h5page",
        "signature": "1",
        "channel": "014000D",
    }
    requested = str(quality or "320k").strip().lower()
    tone_order = {
        "hires": ["SQ", "HQ", "PQ"],
        "flac": ["SQ", "HQ", "PQ"],
        "320k": ["HQ", "PQ"],
        "128k": ["PQ"],
    }.get(requested, [MIGU_TONE_FLAG, "PQ"])

    for tone_flag in tone_order:
        params = {
            "copyrightId": copyright_id,
            "contentId": content_id,
            "toneFlag": tone_flag,
            "resourceType": "E",
            "netType": "01",
            "scene": "",
        }
        try:
            response = requests.get(MIGU_PC_LISTEN_URL, params=params, headers=headers, timeout=MIGU_TIMEOUT)
            if response is None:
                continue
            if response.ok and len(response.content) > 4:
                decrypted = _migu_decrypt(response.content)
                if decrypted:
                    import json as _json
                    data = _json.loads(decrypted)
                    if isinstance(data, dict) and str(data.get("code", "")) == "000000":
                        url = _clean_url((data.get("data") or {}).get("url") or "")
                        if url:
                            LOGGER.info("Migu PC listen resolved for %s at %s", song_id, tone_flag)
                            return url
                    else:
                        LOGGER.info("Migu PC listen non-success for %s at %s: %s", song_id, tone_flag, data.get("code"))
        except (requests.RequestException, ValueError) as exc:
            LOGGER.info("Migu PC listen failed for %s at %s: %s", song_id, tone_flag, type(exc).__name__)

    # Fallback to catalogue-supplied URL
    return _clean_url(cached.get("_play_url"))


MIGU_USER_INFO_URL = "https://c.musicapp.migu.cn/MIGUM2.0/v1.0/user/queryUserInfo.do"

# The member session is carried by `pacmtoken`; the other cookie entries are
# device/analytics values that Migu does not authenticate against.
MIGU_SESSION_FIELD = "pacmtoken"

_MIGU_AUTH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://music.migu.cn/",
    "Origin": "https://music.migu.cn",
    "birth": "h5page",
    "signature": "1",
    "channel": "014000D",
}


def _cookie_pairs(cookie: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for item in str(cookie or "").split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        key, _, value = item.partition("=")
        pairs.append((key.strip(), value.strip()))
    return pairs


def merge_cookie(cookie: str, updates: dict[str, str]) -> str:
    """Return the cookie string with `updates` applied, preserving field order."""
    updates = {str(k): str(v) for k, v in (updates or {}).items() if k and v}
    if not updates:
        return str(cookie or "").strip()
    pairs = _cookie_pairs(cookie)
    seen: set[str] = set()
    merged: list[str] = []
    for key, value in pairs:
        if key in updates:
            if key in seen:
                continue
            merged.append(f"{key}={updates[key]}")
        else:
            merged.append(f"{key}={value}")
        seen.add(key)
    for key, value in updates.items():
        if key not in seen:
            merged.append(f"{key}={value}")
    return "; ".join(merged)


def _rolled_session_token(response: requests.Response) -> str:
    """Read the refreshed `pacmtoken` that Migu rolls out via Set-Cookie."""
    try:
        value = response.cookies.get(MIGU_SESSION_FIELD) or ""
    except Exception:
        value = ""
    if value:
        return str(value).strip()
    raw = response.headers.get("Set-Cookie", "") or ""
    match = re.search(rf"{MIGU_SESSION_FIELD}=([^;,\s]+)", raw)
    return match.group(1).strip() if match else ""


def user_info(cookie: str) -> tuple[bool, str, str, str]:
    """Query the signed-in Migu member profile.

    Returns (ok, nickname, message, rolled_session_token). The response body is
    encrypted with the same scheme as the PC listen API.
    """
    cookie = str(cookie or "").strip()
    if not cookie:
        return False, "", "Cookie 为空", ""
    headers = {**_MIGU_AUTH_HEADERS, "Cookie": cookie}
    try:
        response = requests.get(MIGU_USER_INFO_URL, headers=headers, timeout=MIGU_TIMEOUT)
    except requests.RequestException as exc:
        return False, "", f"咪咕账号接口请求失败（{type(exc).__name__}）", ""
    if not response.ok:
        return False, "", f"咪咕账号接口返回 HTTP {response.status_code}", ""
    rolled = _rolled_session_token(response)
    decrypted = _migu_decrypt(response.content)
    if not decrypted:
        return False, "", "咪咕账号接口响应无法解析", rolled
    try:
        payload = json.loads(decrypted)
    except ValueError:
        return False, "", "咪咕账号接口响应不是有效 JSON", rolled
    if not isinstance(payload, dict):
        return False, "", "咪咕账号接口响应结构异常", rolled
    code = str(payload.get("code") or "")
    if code != "000000":
        message = str(payload.get("info") or payload.get("msg") or payload.get("message") or "")
        return False, "", f"会话无效（code={code}{'：' + message if message else ''}）", rolled
    item = payload.get("userInfoItem")
    nickname = ""
    if isinstance(item, dict):
        nickname = str(item.get("nickName") or item.get("nickname") or "").strip()
    return True, nickname, "会员 Cookie 有效", rolled


def renew_cookie(cookie: str) -> tuple[bool, str, str]:
    """Roll the Migu session forward.

    Each authenticated profile request makes Migu hand back a fresh
    `pacmtoken`; merging it back into the stored cookie keeps the session
    alive without another QR scan. Returns (ok, cookie, message).
    """
    cookie = str(cookie or "").strip()
    ok, nickname, message, rolled = user_info(cookie)
    if not ok:
        return False, cookie, message
    if not rolled or rolled == dict(_cookie_pairs(cookie)).get(MIGU_SESSION_FIELD, ""):
        return True, cookie, f"会话有效，咪咕未下发新 {MIGU_SESSION_FIELD}"
    return True, merge_cookie(cookie, {MIGU_SESSION_FIELD: rolled}), (
        f"会话已续期{'（' + nickname + '）' if nickname else ''}"
    )


def check_cookie(cookie: str) -> tuple[bool, str]:
    """Verify a Migu cookie against the authenticated member profile endpoint.

    Returns (valid, message).
    """
    if not cookie or not cookie.strip():
        return False, "Cookie 为空"

    if not dict(_cookie_pairs(cookie)).get(MIGU_SESSION_FIELD):
        return False, f"Cookie 缺少必要的咪咕会话字段（{MIGU_SESSION_FIELD}）"

    ok, nickname, message, _ = user_info(cookie)
    if ok and nickname:
        return True, f"会员 Cookie 有效（{nickname}）"
    return ok, message


def lyric(song_id: str) -> str:
    """Fetch lyrics only from an HTTPS lyric URL supplied by the catalogue response."""
    url = _clean_url(_cached(song_id).get("_lyric_url"))
    if not url:
        return ""
    try:
        response = requests.get(url, headers=_HEADERS, timeout=MIGU_TIMEOUT)
        if not response.ok:
            return ""
        content_type = response.headers.get("Content-Type", "")
        if "json" in content_type:
            payload = response.json()
            if isinstance(payload, dict):
                return str(_value(payload, "lyric", "lrc", "content", "data"))
        return response.text if len(response.text) <= 1_000_000 else ""
    except (requests.RequestException, ValueError):
        return ""
