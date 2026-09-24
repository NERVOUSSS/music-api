"""Third-party music source adapters.

Uploaded LX Music files are treated as untrusted text.  The adapter only
extracts the endpoint and source metadata; it never executes JavaScript.
"""

import json
import re
import hashlib
import ast
import requests as _requests
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger("music-sources")

# ==================== 预定义音源模板 ====================
BUILTIN_SOURCES = {}


PUBLIC_RESOLVER_BACKENDS = {
    "liuyun_qq": {
        "name": "柳云 QQ",
        "api_url": "https://api.liuyunidc.cn/baimusic/musicurl.php",
    },
    "aacab_qq": {
        "name": "aa.cab QQ",
        "api_url": "https://a.aa.cab/qq.music",
    },
    "oiapi_kuwo": {
        "name": "oiapi 酷我",
        "api_url": "https://oiapi.net/api/Kuwo",
    },
}


def _public_resolver_candidate(backends):
    backend_ids = [backend for backend in backends if backend in PUBLIC_RESOLVER_BACKENDS]
    names = [PUBLIC_RESOLVER_BACKENDS[backend]["name"] for backend in backend_ids]
    return {
        "id": "public_resolver_aggregate",
        "name": "公开聚合回源",
        "desc": "、".join(names) + "，按歌曲信息自动切换播放地址",
        "api_url": "多个公开回源接口",
        "source_type": "public_resolver_aggregate",
        "backends": backend_ids,
        "support_url": True,
        "sources": [],
    }


def _is_public_http_url(value):
    try:
        parsed = urlparse(value)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc) and parsed.hostname not in {
            "localhost", "127.0.0.1", "::1",
        }
    except Exception:
        return False



def _decode_js_string(value):
    """Decode a quoted JS literal without executing the uploaded script."""
    try:
        return str(ast.literal_eval(value)).strip()
    except (SyntaxError, ValueError):
        return value[1:-1].strip() if len(value) >= 2 else ""


def _extract_js_string(text, name):
    """Extract a string constant, including backticks and JSON.parse literals."""
    assignment = re.search(
        rf"(?:const|let|var)\s+{re.escape(name)}\s*=\s*",
        text or "", flags=re.IGNORECASE,
    )
    if not assignment:
        return ""
    tail = (text or "")[assignment.end():]
    parsed = re.match(r"JSON\.parse\s*\(\s*([\"'`])((?:\\.|(?!\1).)*)\1\s*\)", tail, re.IGNORECASE | re.DOTALL)
    if parsed:
        raw = _decode_js_string(parsed.group(1) + parsed.group(2) + parsed.group(1))
        try:
            value = json.loads(raw)
            return str(value).strip() if isinstance(value, str) else ""
        except (TypeError, json.JSONDecodeError):
            return raw
    literal = re.match(r"([\"'`])((?:\\.|(?!\1).)*)\1", tail, re.DOTALL)
    if not literal:
        return ""
    return _decode_js_string(literal.group(1) + literal.group(2) + literal.group(1))


def _extract_lx_quality_sources(text):
    """Read MUSIC_QUALITY in literal or JSON.parse form, never by executing JS."""
    source_text = text or ""
    assignment = re.search(r"MUSIC_QUALITY\s*=\s*", source_text, flags=re.IGNORECASE)
    if not assignment:
        return {}
    tail = source_text[assignment.end():]
    json_match = re.match(r"JSON\.parse\s*\(\s*([\"'`])((?:\\.|(?!\1).)*)\1\s*\)", tail, re.IGNORECASE | re.DOTALL)
    if json_match:
        raw = _decode_js_string(json_match.group(1) + json_match.group(2) + json_match.group(1))
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return {
                    str(source).lower(): [str(item).strip() for item in values[:20] if str(item).strip()]
                    for source, values in parsed.items()
                    if isinstance(values, list) and values
                }
        except (TypeError, json.JSONDecodeError):
            pass
    object_match = re.match(r"\{(.*?)\}", tail, re.DOTALL)
    if not object_match:
        return {}
    result = {}
    for source, values in re.findall(
        r"[\"']([a-z0-9_-]+)[\"']\s*:\s*\[([^\]]*)\]", object_match.group(1), flags=re.IGNORECASE
    ):
        qualities = [item.strip() for item in re.findall(r"[\"']([^\"']+)[\"']", values)]
        if qualities:
            result[source.lower()] = qualities[:20]
    return result


def _extract_lx_url_pattern(text):
    """Extract a static API_URL template used by common LX source versions."""
    source_text = text or ""
    candidates = re.findall(r"\$\{API_URL\}([ `/][^`\"']*)", source_text, flags=re.IGNORECASE | re.DOTALL)
    candidates += [
        _decode_js_string(match.group(1) + match.group(2) + match.group(1))
        for match in re.finditer(
            r"API_URL\s*\+\s*([\"'`])((?:\\.|(?!\1).)*)\1",
            source_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
    ]
    for value in candidates:
        if not value or not all(token in value for token in ("source", "songId", "quality")):
            continue
        return (
            value
            .replace("${source}", "{source}")
            .replace("${songId}", "{song_id}")
            .replace("${quality}", "{quality}")
            .replace("${encodeURIComponent(source)}", "{source}")
            .replace("${encodeURIComponent(songId)}", "{song_id}")
            .replace("${encodeURIComponent(quality)}", "{quality}")
        )
    return "/music/url?source={source}&songId={song_id}&quality={quality}"


def _extract_lx_auth_header(text):
    match = re.search(r"[\"']([^\"']+)[\"']\s*:\s*API_KEY", text or "", flags=re.IGNORECASE)
    return match.group(1).strip() if match else "X-API-Key"


def _lx_candidate(text, filename=""):
    api_url = _extract_js_string(text, "API_URL")
    api_key = _extract_js_string(text, "API_KEY")
    quality_sources = _extract_lx_quality_sources(text)
    if not _is_public_http_url(api_url) or not quality_sources:
        return None

    source_aliases = {
        "wy": "netease", "netease": "netease",
        "tx": "qq", "qq": "qq",
        "kg": "kugou", "git": "kugou", "kugou": "kugou",
        "kw": "kuwo", "kuwo": "kuwo",
        "mg": "migu", "migu": "migu",
    }
    sources = []
    source_keys = {}
    for source in quality_sources:
        normalized = source_aliases.get(source, source)
        if normalized not in sources:
            sources.append(normalized)
        source_keys.setdefault(normalized, source)
    if not sources:
        return None

    digest = hashlib.sha256((api_url + "\n" + "\n".join(sources)).encode("utf-8")).hexdigest()[:12]
    filename_stem = re.sub(r"\.[^.]+$", "", filename or "")
    name = filename_stem or "LX Music 音源"
    return {
        "id": f"lx_{digest}",
        "name": name,
        "desc": "从 LX Music 音源脚本静态提取的播放适配器",
        "api_url": api_url.rstrip("/"),
        "api_key": api_key,
        "method": "GET",
        "url_pattern": _extract_lx_url_pattern(text),
        "auth_header": _extract_lx_auth_header(text) if api_key else "",
        "code_field": "code",
        "code_value": 0,
        "url_field": "url",
        "response_type": "json",
        "quality_map": {source: values for source, values in quality_sources.items()},
        "sources": sources,
        "source_aliases": source_aliases,
        "source_keys": source_keys,
        "source_type": "lx_script",
        "support_search": False,
        "support_url": True,
        "usage_limit": 750,
        "usage_count": 0,
        "builtin": False,
    }

def detect_source_candidates(script_text, filename=""):
    """Statically detect source adapters without executing untrusted JavaScript."""
    text = script_text or ""
    urls = []
    for raw_url in re.findall(r"https?://[^\s\"'`<>\\]+", text):
        url = raw_url.rstrip(").,;}")
        if _is_public_http_url(url) and url not in urls:
            urls.append(url)

    candidates = []
    notes = []
    normalized = text.lower()
    lx_signals = []
    lx_signal_patterns = [
        ("globalThis.lx", r"\bglobalThis\s*\.\s*lx\b"),
        ("EVENT_NAMES.request", r"\bEVENT_NAMES\s*\.\s*request\b"),
        ("request event handler", r"\bon\s*\(\s*EVENT_NAMES\s*\.\s*request"),
        ("musicInfo", r"\bmusicInfo\b"),
        ("musicUrl action", r"\b(?:action|type)\s*={1,3}\s*['\"](?:musicUrl|url)['\"]"),
        ("request()", r"\brequest\s*\("),
    ]
    for signal, pattern in lx_signal_patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            lx_signals.append(signal)
    is_lx_source = len(lx_signals) >= 2 or (
        "event_names.request" in normalized and "musicinfo" in normalized
    )

    xinghai_detected = (
        any("yy.zddyr.top/lx/api" in url for url in urls)
        or ("yy.zddyr.top" in normalized and "/lx/api/" in normalized)
    )
    if xinghai_detected:
        xinghai_url = "https://yy.zddyr.top/lx/api"
        if xinghai_url not in urls:
            urls.append(xinghai_url)
        candidates.append({
            "id": "xinghai",
            "name": "Xinghai",
            "desc": "Detected public QQ playback resolver",
            "api_url": "https://yy.zddyr.top/lx/api",
            "source_type": "xinghai",
            "support_url": True,
            "sources": ["qq"],
        })

    public_backends = []
    if any("api.liuyunidc.cn/baimusic/musicurl.php" in url for url in urls):
        public_backends.append("liuyun_qq")
    if any("a.aa.cab/qq.music" in url for url in urls):
        public_backends.append("aacab_qq")
    if any("oiapi.net/api/kuwo" in url.lower() for url in urls):
        public_backends.append("oiapi_kuwo")
    if public_backends:
        candidates.append(_public_resolver_candidate(public_backends))
        notes.append(
            "识别到可由本项目后端调用的公开播放回源接口："
            + "、".join(PUBLIC_RESOLVER_BACKENDS[item]["name"] for item in public_backends)
            + "。"
        )

    native_markers = {
        "u.y.qq.com/cgi-bin/musicu.fcg": "QQ Music",
        "interface3.music.163.com/eapi": "NetEase Cloud Music",
        "www.kuwo.cn/api/": "Kuwo Music",
        "wwwapi.kugou.com/": "Kugou Music",
        "music.migu.cn/": "Migu Music",
    }
    detected_native = [name for marker, name in native_markers.items() if marker in normalized]
    if detected_native:
        notes.append(
            "This script calls platform-specific private endpoints directly ("
            + ", ".join(detected_native)
            + "). It cannot be added as a generic resolver without a dedicated adapter."
        )

    if is_lx_source:
        adapter = _lx_candidate(text, filename)
        if adapter:
            candidates.append(adapter)
            notes.append("已从 LX Music 脚本静态提取播放接口、平台和音质配置；不会执行脚本。")
        notes.append(
            "识别为 LX Music 音源（静态特征：" + "、".join(lx_signals) + "）。"
        )
        if not adapter:
            notes.append("未能从脚本提取明确的 API_URL/MUSIC_QUALITY，不能自动接入。")

    if not candidates and not notes:
        notes.append(
            "No supported public resolver was found. This may be an encrypted script, a stale endpoint, "
            "or a source that needs a dedicated adapter."
        )

    return {
        "filename": filename,
        "urls": urls[:20],
        "is_lx_source": is_lx_source,
        "lx_signals": lx_signals,
        "candidates": candidates,
        "notes": notes,
    }


def build_music_source_id_mapping(song_source, song):
    """从歌曲信息中提取各平台的 ID"""
    ids = {}
    if song_source == "netease":
        ids["wy"] = str(song.get("id", ""))
    elif song_source == "qq":
        songmid = song.get("songmid") or song.get("id", "")
        ids["tx"] = songmid
        ids["qq"] = songmid
    elif song_source == "kugou":
        song_hash = str(song.get("hash") or song.get("filehash") or song.get("id") or "")
        ids["kg"] = song_hash
    elif song_source == "kuwo":
        song_id = str(song.get("rid") or song.get("id") or "").replace("MUSIC_", "")
        ids["kw"] = song_id
    elif song_source == "migu":
        ids["mg"] = str(song.get("copyrightId") or song.get("id") or "")
    return ids


def get_url_from_source_def(source_def, song_source_name, song_id, quality="320k"):
    """根据源定义通用获取播放链接"""
    api_url = source_def.get("api_url", "")
    url_pattern = source_def.get("url_pattern", "")
    method = source_def.get("method", "GET")
    quality_map = source_def.get("quality_map", {})

    if quality_map and quality in quality_map:
        q = quality_map[quality]
    else:
        q = quality

    try:
        final_path = url_pattern.replace("{source}", song_source_name).replace("{song_id}", song_id).replace("{quality}", q)
    except:
        return None

    full_url = api_url.rstrip("/") + "/" + final_path.lstrip("/")

    headers = {"User-Agent": "lx-music-desktop/2.10.0"}
    auth_header = source_def.get("auth_header", "")
    api_key = source_def.get("api_key", "")
    if auth_header and api_key:
        headers[auth_header] = api_key

    try:
        if method == "POST":
            r = _requests.post(full_url, json=source_def.get("post_body", {}), headers=headers, timeout=10)
        else:
            r = _requests.get(full_url, headers=headers, timeout=10)

        data = r.json()

        code_field = source_def.get("code_field", "code")
        url_field = source_def.get("url_field", "url")

        def get_field(obj, field_path):
            if not field_path or field_path == "_no_check":
                return None
            parts = field_path.split(".")
            val = obj
            for p in parts:
                if isinstance(val, dict):
                    val = val.get(p)
                else:
                    return None
            return val

        # 特殊处理：如果 code_field == "_no_check"，直接取 url_field
        if code_field == "_no_check":
            actual_url = get_field(data, url_field)
            if actual_url and isinstance(actual_url, str) and actual_url.startswith("http"):
                return actual_url
        else:
            code_value = source_def.get("code_value", 0)
            actual_code = get_field(data, code_field)
            actual_url = get_field(data, url_field)
            if actual_code == code_value and actual_url:
                return actual_url

        logger.debug("third source url fail: %s", data)
    except Exception as e:
        logger.debug("third source error: %s", e)

    return None


def get_url_from_lx_script_source(source_def, song_source, song, quality="320k"):
    """Call the endpoint statically extracted from an LX Music source."""
    api_url = source_def.get("api_url", "").rstrip("/")
    source_keys = source_def.get("source_keys", {}) or {}
    platform_to_lx = {
        "netease": "wy", "qq": "tx", "kugou": "kg", "kuwo": "kw", "migu": "mg",
    }
    canonical_source = str(song_source or "").lower()
    lx_source = source_keys.get(canonical_source) or platform_to_lx.get(canonical_source) or canonical_source
    if source_keys and canonical_source not in source_keys:
        return None

    if canonical_source == "netease":
        song_id = str(song.get("id") or song.get("songmid") or "")
    elif canonical_source == "qq":
        song_id = str(song.get("songmid") or song.get("id") or "")
    elif canonical_source == "kugou":
        song_id = str(song.get("hash") or song.get("filehash") or song.get("id") or "")
    elif canonical_source == "kuwo":
        song_id = str(song.get("rid") or song.get("id") or "").replace("MUSIC_", "")
    elif canonical_source == "migu":
        song_id = str(song.get("copyrightId") or song.get("id") or "")
    else:
        song_id = str(song.get("songmid") or song.get("id") or song.get("hash") or "")
    if not api_url or not song_id:
        return None

    qualities = source_def.get("quality_map", {}).get(lx_source) or source_def.get("quality_map", {}).get(song_source) or []
    requested_quality = str(quality or "320k")
    if requested_quality in qualities:
        selected_quality = requested_quality
    elif qualities:
        rank = {"128k": 0, "320k": 1, "flac": 2, "flac24bit": 3, "hires": 4, "atmos": 5, "master": 6}
        selected_quality = max(qualities, key=lambda item: rank.get(item, 0))
    else:
        selected_quality = requested_quality

    url_pattern = source_def.get("url_pattern") or "/music/url?source={source}&songId={song_id}&quality={quality}"
    full_url = api_url + "/" + url_pattern.lstrip("/")
    full_url = (
        full_url.replace("{source}", lx_source)
        .replace("{song_id}", song_id)
        .replace("{quality}", selected_quality)
    )
    headers = {"User-Agent": "music-api/lx-adapter"}
    if source_def.get("api_key") and source_def.get("auth_header"):
        headers[source_def["auth_header"]] = source_def["api_key"]
    try:
        response = _requests.get(full_url, headers=headers, timeout=12)
        if response.status_code >= 400:
            logger.debug("lx script source [%s] returned HTTP %s", source_def.get("name", ""), response.status_code)
            return None
        data = response.json()
        code = data.get("code") if isinstance(data, dict) else None
        url = data.get("url") if isinstance(data, dict) else None
        if not url and isinstance(data, dict):
            payload = data.get("data")
            if isinstance(payload, dict):
                url = payload.get("url") or payload.get("urlHigh") or payload.get("playUrl")
            elif isinstance(payload, str):
                url = payload
        if code in (None, 0, 200) and isinstance(url, str) and url.startswith(("http://", "https://")):
            return url
        logger.debug("lx script source [%s] response: %s", source_def.get("name", ""), data)
    except Exception as exc:
        logger.debug("lx script source [%s] error: %s", source_def.get("name", ""), exc)
    return None

def get_url_from_xinghai_source(source_def, song_source, song, quality="320k"):
    """适配星海音乐源公开的 QQ/KG/KW/MG 回源接口。"""
    source_map = {
        "qq": "qq",
        "kugou": "kg",
        "kuwo": "kw",
        "migu": "migu",
    }
    upstream_source = source_map.get(song_source)
    if not upstream_source:
        return None

    song_id = str(song.get("songmid") or song.get("id") or "")
    if not song_id:
        return None

    params = {
        "source": upstream_source,
        "name": song.get("songname") or song.get("name") or song.get("title") or "",
        "singer": song.get("singer") or song.get("artist") or "",
        "songmid": song_id,
        "interval": song.get("interval") or int((song.get("duration") or 0) / 1000),
        "albumName": song.get("albumname") or song.get("album") or "",
        "quality": quality,
    }
    try:
        response = _requests.get(
            source_def.get("api_url", "").rstrip("/") + "/",
            params=params,
            headers={"User-Agent": "LX-Music-Mobile"},
            timeout=12,
        )
        data = response.json()
        url = data.get("url", "")
        if data.get("code") == 200 and isinstance(url, str) and url.startswith("http"):
            return url
        logger.debug("xinghai source response: %s", data)
    except Exception as e:
        logger.debug("xinghai source error: %s", e)
    return None


def _song_value(song, *keys):
    for key in keys:
        value = song.get(key)
        if value:
            return str(value).strip()
    return ""


def _normalize_song_text(value):
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", str(value or "").lower())


def _is_probable_song_match(expected_title, expected_artist, actual_title, actual_artist):
    expected_title = _normalize_song_text(expected_title)
    actual_title = _normalize_song_text(actual_title)
    if expected_title and actual_title and expected_title not in actual_title and actual_title not in expected_title:
        return False

    expected_artist = _normalize_song_text(expected_artist)
    actual_artist = _normalize_song_text(actual_artist)
    if expected_artist and actual_artist:
        return expected_artist in actual_artist or actual_artist in expected_artist
    return True


def _public_resolver_url(backend_id, song_source, song, quality="320k"):
    title = _song_value(song, "songname", "name", "title")
    artist = _song_value(song, "singer", "artist", "artistName")
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

    try:
        if backend_id == "liuyun_qq":
            if song_source != "qq":
                return None
            songmid = _song_value(song, "songmid", "mid", "id")
            if not songmid:
                return None
            quality_map = {
                "128k": "128k", "320k": "320k", "flac": "flac",
                "flac24bit": "master", "hires": "atmos", "atmos": "atmos", "master": "master",
            }
            response = _requests.get(
                PUBLIC_RESOLVER_BACKENDS[backend_id]["api_url"],
                params={"source": "tx", "musicId": songmid, "quality": quality_map.get(quality, "320k")},
                headers={**headers, "Referer": "http://api.liuyunidc.cn/baimusic/"},
                timeout=10,
            )
            data = response.json()
            url = data.get("url") or (data.get("data") or {}).get("url")
        elif backend_id == "aacab_qq":
            if not title:
                return None
            quality_map = {"128k": "0", "320k": "1", "flac": "4", "master": "5"}
            response = _requests.get(
                PUBLIC_RESOLVER_BACKENDS[backend_id]["api_url"],
                params={"msg": title, "n": "1", "type": quality_map.get(quality, "1")},
                headers=headers,
                timeout=10,
            )
            data = response.json()
            result = data.get("data") or {}
            if not _is_probable_song_match(title, artist, result.get("song"), result.get("singer")):
                logger.debug("aa.cab song mismatch for %s - %s", title, artist)
                return None
            url = result.get("music") or result.get("url") or data.get("playUrl") or data.get("url")
        elif backend_id == "oiapi_kuwo":
            if not title:
                return None
            quality_map = {"flac": "1", "320k": "2", "128k": "3"}
            response = _requests.get(
                PUBLIC_RESOLVER_BACKENDS[backend_id]["api_url"],
                params={"msg": " ".join(item for item in (title, artist) if item), "n": "1", "br": quality_map.get(quality, "3")},
                headers=headers,
                timeout=10,
            )
            data = response.json()
            result = data.get("data") or {}
            actual_title = result.get("name") or result.get("song") or data.get("song")
            actual_artist = result.get("artist") or result.get("singer") or data.get("singer")
            if not _is_probable_song_match(title, artist, actual_title, actual_artist):
                logger.debug("oiapi song mismatch for %s - %s", title, artist)
                return None
            url = result.get("url")
        else:
            return None
    except (ValueError, _requests.RequestException) as exc:
        logger.debug("public resolver [%s] error: %s", backend_id, exc)
        return None

    return url if isinstance(url, str) and url.startswith(("http://", "https://")) else None


def get_url_from_public_resolver_source(source_def, song_source, song, quality="320k"):
    """Resolve an uploaded public LX-style script through vetted backend adapters."""
    for backend_id in source_def.get("backends") or []:
        url = _public_resolver_url(backend_id, song_source, song, quality)
        if url:
            return url
    return None


def try_third_sources(song_source, song, enabled_source_configs, quality="320k"):
    """尝试多个启用的第三方音源获取播放链接"""
    ids = build_music_source_id_mapping(song_source, song)
    
    for sname, sdef in enabled_source_configs.items():
        if not sdef or not sdef.get("support_url"):
            continue
        
        if sdef.get("source_type") == "xinghai":
            url = get_url_from_xinghai_source(sdef, song_source, song, quality)
            if url:
                logger.info("third source [%s/xinghai] got url", sname)
                return url, sname
            continue

        if sdef.get("source_type") == "public_resolver_aggregate":
            url = get_url_from_public_resolver_source(sdef, song_source, song, quality)
            if url:
                logger.info("third source [%s/public aggregate] got url", sname)
                return url, sname
            continue

        # LX 脚本格式源
        if sdef.get("source_type") == "lx_script":
            try:
                url = get_url_from_lx_script_source(sdef, song_source, song, quality)
                if url:
                    logger.info("third source [%s/lx_script] got url", sname)
                    return url, sname
            except Exception as e:
                logger.debug("third source [%s/lx_script] error: %s", sname, e)
            continue
        
        # 通用 URL 模板格式源
        if not ids:
            continue
        for platform, sid in ids.items():
            if not sid:
                continue
            supported = sdef.get("sources", [])
            if supported and platform not in supported:
                continue
            try:
                url = get_url_from_source_def(sdef, platform, sid, quality)
                if url:
                    logger.info("third source [%s/%s] got url", sname, platform)
                    return url, sname
            except Exception as e:
                logger.debug("third source [%s/%s] error: %s", sname, platform, e)
                continue

    return None, None


def try_third_sources_parallel(song_source, song, enabled_source_configs, quality="320k", excluded_sources=None, timeout=9):
    """Probe enabled resolvers concurrently and return the first URL returned."""
    excluded = {str(item).strip() for item in (excluded_sources or []) if str(item).strip()}
    configs = {
        sid: source for sid, source in (enabled_source_configs or {}).items()
        if sid not in excluded and source and source.get("support_url")
    }
    if not configs:
        return None, None

    def probe(item):
        sid, source = item
        return (*try_third_sources(song_source, song, {sid: source}, quality), sid)

    executor = ThreadPoolExecutor(max_workers=min(8, len(configs)))
    try:
        futures = [executor.submit(probe, item) for item in configs.items()]
        for future in as_completed(futures, timeout=timeout):
            try:
                url, resolved_by, _ = future.result()
            except Exception as exc:
                logger.debug("parallel third source failed: %s", exc)
                continue
            if url:
                return url, resolved_by
    except TimeoutError:
        logger.debug("parallel third source probe timed out after %ss", timeout)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return None, None


def test_source(source_def):
    return test_source_detailed(source_def).get("ok", False)


def test_source_detailed(source_def):
    """Test a supported resolver with a platform-compatible sample song."""
    if not source_def:
        return {"ok": False, "msg": "Missing source configuration"}

    test_song = {
        "id": "004Z8Ihr0JIu5s",
        "songmid": "004Z8Ihr0JIu5s",
        "title": "\u4e03\u91cc\u9999",
        "artist": "\u5468\u6770\u4f26",
        "album": "\u4e03\u91cc\u9999",
        "duration": 299000,
    }
    try:
        source_type = source_def.get("source_type", "")
        for attempt in range(2):
            if source_type == "xinghai":
                xinghai_test_song = {
                    "id": "60054701923",
                    "title": "晴天",
                    "artist": "周杰伦",
                    "album": "叶惠美",
                    "duration": 269000,
                }
                url = get_url_from_xinghai_source(source_def, "migu", xinghai_test_song)
            elif source_type == "public_resolver_aggregate":
                url = get_url_from_public_resolver_source(source_def, "qq", test_song)
            elif source_type == "lx_script":
                test_song_by_source = {
                    "netease": {"id": "186016", "title": "晴天", "artist": "周杰伦"},
                    "kugou": {"hash": "3A1A0A7A8D2D0B7A1F6D5E5F6A0B2C3D", "title": "晴天", "artist": "周杰伦"},
                    "kuwo": {"rid": "581", "title": "晴天", "artist": "周杰伦"},
                    "migu": {"copyrightId": "60054701923", "title": "晴天", "artist": "周杰伦"},
                    "qq": test_song,
                }
                url = None
                for test_source in source_def.get("sources", []) or []:
                    url = get_url_from_lx_script_source(
                        source_def, test_source, test_song_by_source.get(test_source, test_song)
                    )
                    if url:
                        break
            else:
                url = get_url_from_source_def(source_def, "tx", test_song["songmid"], "320k")
            if url and url.startswith("http"):
                suffix = "" if attempt == 0 else " after retry"
                return {"ok": True, "msg": f"Playback URL resolved{suffix}"}
        return {"ok": False, "msg": "No playback URL returned after two attempts"}
    except Exception as exc:
        return {"ok": False, "msg": str(exc)}
