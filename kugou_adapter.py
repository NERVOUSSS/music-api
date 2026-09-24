"""Kugou session refresh adapter.

Kugou's Android clients keep a login alive through `/v5/login_by_token`, which
expects an AES-encrypted `p3` payload plus an RSA-wrapped session key.  This
module implements only that official refresh call so an administrator session
authorized by QR code does not have to be re-scanned; it never bypasses
account, membership, region, or copyright controls.

Two client flavours share the same endpoint and differ only in identifiers,
salts and crypto material:

* `lite=True`  -> Kugou Concept Edition (appid 3116, clientver 11440)
* `lite=False` -> standard Kugou Music   (appid 1005, clientver 20489)
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import time
from typing import Any

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from migu_adapter import merge_cookie

LOGGER = logging.getLogger("music-sources")

KUGOU_RENEW_URL = "http://login.user.kugou.com/v5/login_by_token"
KUGOU_RENEW_TIMEOUT = 20
KUGOU_USER_AGENT = "Android15-1070-11083-46-0-DiscoveryDRADProtocol-wifi"
# Cookie fields the refresh response may roll; everything else is device state.
KUGOU_RENEW_COOKIE_FIELDS = ("token", "userid", "t1", "vip_type", "vip_token")

_LITE_P3_KEY = "c24f74ca2820225badc01946dba4fdf7"
_LITE_P3_IV = "adc01946dba4fdf7"
_STD_P3_KEY = "90b8382a1bb4ccdcf063102053fd75b8"
_STD_P3_IV = "f063102053fd75b8"
_LITE_T1_KEY = "5e4ef500e9597fe004bd09a46d8add98"
_LITE_T1_IV = "04bd09a46d8add98"
_LITE_T2_KEY = "fd14b35e3f81af3817a20ae7adae7020"
_LITE_T2_IV = "17a20ae7adae7020"
_LITE_T2_CONST = "0f607264fc6318a92b9e13c65db7cd3c"
_T3_CONST = "MCwwLDAsMCwwLDAsMCwwLDA="

_STD_PUBLIC_KEY = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDIAG7QOELSYoIJvTFJhMpe1s/gbjDJX51HBNnEl5HXqTW6"
    "lQ7LC8jr9fWZTwusknp+sVGzwd40MwP6U5yDE27M/X1+UR4tvOGOqp94TJtQ1EPnWGWXngpeIW5GxoQGao1r"
    "mYWAu6oi1z9XkChrsUdC6DJE5E221wf/4WLFxwAtRQIDAQAB\n"
    "-----END PUBLIC KEY-----"
)
_LITE_PUBLIC_KEY = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDECi0Np2UR87scwrvTr72L6oO01rBbbBPriSDFPxr3Z5sy"
    "ug0O24QyQO8bg27+0+4kBzTBTBOZ/WWU0WryL1JSXRTXLgFVxtzIY41Pe7lPOgsfTCn5kZcvKhYKJesKnnJD"
    "Nr5/abvTGf+rHG3YRwsCHcQ08/q6ifSioBszvb3QiwIDAQAB\n"
    "-----END PUBLIC KEY-----"
)

_LITE_SIGN_SALT = "LnT6xpN3khm36zse0QzvmgTZ3waWdRSA"
_STD_SIGN_SALT = "OIlwieks28dk2k092lksi2UIkp"
_LITE_APPID = "3116"
_LITE_CLIENTVER = "11440"
_STD_APPID = "1005"
_STD_CLIENTVER = "20489"
_RANDOM_KEY_POOL = "1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ"


class KugouRenewError(RuntimeError):
    """Raised when the refresh call cannot be completed."""


def _md5(value: Any) -> str:
    if not isinstance(value, str):
        value = json.dumps(value, separators=(",", ":"))
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def _pkcs7_pad(raw: bytes) -> bytes:
    padding = 16 - len(raw) % 16
    return raw + bytes([padding]) * padding


def _pkcs7_unpad(raw: bytes) -> bytes:
    return raw[: -raw[-1]] if raw else raw


def _aes_encrypt_hex(payload: Any, key: str, iv: str) -> str:
    if not isinstance(payload, str):
        payload = json.dumps(payload, separators=(",", ":"))
    encryptor = Cipher(algorithms.AES(key.encode()), modes.CBC(iv.encode())).encryptor()
    return (encryptor.update(_pkcs7_pad(payload.encode("utf-8"))) + encryptor.finalize()).hex()


def _aes_decrypt_hex(payload: str, seed: str) -> Any:
    """Undo the response encryption, whose key/iv derive from the request seed."""
    key = _md5(seed)[:32]
    decryptor = Cipher(algorithms.AES(key.encode()), modes.CBC(key[-16:].encode())).decryptor()
    raw = _pkcs7_unpad(decryptor.update(bytes.fromhex(payload)) + decryptor.finalize())
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def _random_seed(length: int = 16) -> str:
    return "".join(random.choice(_RANDOM_KEY_POOL) for _ in range(length)).lower()


def _aes_encrypt_with_seed(payload: Any) -> tuple[str, str]:
    """Encrypt with a throwaway seed; the seed itself travels inside the RSA blob."""
    seed = _random_seed()
    key = _md5(seed)[:32]
    return _aes_encrypt_hex(payload, key, key[-16:]), seed


def _rsa_encrypt_hex(payload: Any, pem: str) -> str:
    """Kugou uses raw (zero-padded, textbook) RSA rather than a padded scheme."""
    if not isinstance(payload, str):
        payload = json.dumps(payload, separators=(",", ":"))
    numbers = serialization.load_pem_public_key(pem.encode()).public_numbers()
    key_length = (numbers.n.bit_length() + 7) // 8
    raw = payload.encode("utf-8")
    if len(raw) > key_length:
        raise KugouRenewError("RSA payload exceeds the key size")
    raw = raw + b"\x00" * (key_length - len(raw))
    cipher = pow(int.from_bytes(raw, "big"), numbers.e, numbers.n)
    return format(cipher, "x").rjust(key_length * 2, "0")


def _signature(params: dict[str, Any], body: str, salt: str) -> str:
    canonical = "".join(f"{key}={params[key]}" for key in sorted(params))
    return _md5(f"{salt}{canonical}{body}{salt}")


def _cookie_value(cookie: str, key: str) -> str:
    match = re.search(r"(?:^|;\s*)" + re.escape(key) + r"=([^;]*)", str(cookie or ""))
    return match.group(1).strip() if match else ""


def _build_request(cookie: str, lite: bool) -> tuple[dict[str, str], str, dict[str, str], str]:
    """Assemble the signed params, JSON body, headers and the AES seed."""
    now_ms = int(time.time() * 1000)
    now_seconds = now_ms // 1000
    token = _cookie_value(cookie, "token")
    userid = _cookie_value(cookie, "userid") or "0"
    if not token or userid == "0":
        raise KugouRenewError("Cookie 缺少 token 或 userid，无法调用官方刷新接口")

    p3_key, p3_iv = (_LITE_P3_KEY, _LITE_P3_IV) if lite else (_STD_P3_KEY, _STD_P3_IV)
    p3 = _aes_encrypt_hex({"clienttime": now_seconds, "token": token}, p3_key, p3_iv)
    encrypted_params, seed = _aes_encrypt_with_seed({})
    pk = _rsa_encrypt_hex({"clienttime_ms": now_ms, "key": seed}, _LITE_PUBLIC_KEY if lite else _STD_PUBLIC_KEY)

    guid = _cookie_value(cookie, "KUGOU_API_GUID")
    mac = _cookie_value(cookie, "KUGOU_API_MAC")
    dev = _cookie_value(cookie, "KUGOU_API_DEV")
    mid = _cookie_value(cookie, "KUGOU_API_MID")
    dfid = _cookie_value(cookie, "dfid") or "-"
    previous_t1 = _cookie_value(cookie, "t1")

    payload: dict[str, Any] = {
        "dfid": dfid,
        "p3": p3,
        "plat": 1,
        "t1": _aes_encrypt_hex(f"{previous_t1}|{now_ms}" if previous_t1 else f"|{now_ms}", _LITE_T1_KEY, _LITE_T1_IV) if lite else 0,
        "t2": _aes_encrypt_hex(f"{guid}|{_LITE_T2_CONST}|{mac}|{dev}|{now_ms}", _LITE_T2_KEY, _LITE_T2_IV) if lite else 0,
        "t3": _T3_CONST,
        "pk": pk,
        "params": encrypted_params,
        "userid": userid,
        "clienttime_ms": now_ms,
    }
    if lite:
        payload["dev"] = dev
    body = json.dumps(payload, separators=(",", ":"))

    params = {
        "dfid": dfid,
        "mid": mid,
        "uuid": "-",
        "appid": _LITE_APPID if lite else _STD_APPID,
        "clientver": _LITE_CLIENTVER if lite else _STD_CLIENTVER,
        "clienttime": str(now_seconds),
        "token": token,
        "userid": userid,
    }
    params["signature"] = _signature(params, body, _LITE_SIGN_SALT if lite else _STD_SIGN_SALT)

    headers = {
        "User-Agent": KUGOU_USER_AGENT,
        "Content-Type": "application/json",
        "dfid": dfid,
        "clienttime": str(now_seconds),
        "mid": mid,
        "kg-rc": "1",
        "kg-thash": "5d816a0",
        "kg-rec": "1",
        "kg-rf": "B9EDA08A64250DEFFBCADDEE00F8F25F",
        "Cookie": str(cookie or ""),
    }
    return params, body, headers, seed


def _renew_updates(data: dict[str, Any], seed: str) -> dict[str, str]:
    """Collect the cookie fields Kugou rolls, mirroring the Android client."""
    updates: dict[str, str] = {}
    secu = data.get("secu_params")
    if secu:
        decoded = _aes_decrypt_hex(str(secu), seed)
        if isinstance(decoded, dict):
            data = {**data, **decoded}
        elif decoded:
            data["token"] = decoded
    for field in KUGOU_RENEW_COOKIE_FIELDS:
        value = data.get(field)
        if value in (None, ""):
            continue
        updates[field] = str(value)
    if not updates.get("token"):
        raise KugouRenewError("刷新接口未返回新的 token")
    return updates


def renew_session(cookie: str, lite: bool = True) -> tuple[bool, str, str]:
    """Refresh a Kugou app session in place.

    Returns `(ok, cookie, message)`.  On success `cookie` is the merged session
    with the rolled fields applied; on failure the original cookie is returned
    untouched so a transient error never destroys a working session.
    """
    cookie = str(cookie or "").strip()
    label = "酷狗概念版" if lite else "酷狗音乐"
    if not cookie:
        return False, cookie, f"{label} Cookie 为空"
    try:
        params, body, headers, seed = _build_request(cookie, lite)
    except KugouRenewError as exc:
        return False, cookie, str(exc)

    try:
        response = requests.post(
            KUGOU_RENEW_URL,
            params=params,
            data=body.encode("utf-8"),
            headers=headers,
            timeout=KUGOU_RENEW_TIMEOUT,
        )
    except requests.RequestException as exc:
        return False, cookie, f"{label} 刷新请求失败: {type(exc).__name__}"

    try:
        result = response.json()
    except ValueError:
        return False, cookie, f"{label} 刷新接口返回了非 JSON 响应 (HTTP {response.status_code})"
    if not isinstance(result, dict):
        return False, cookie, f"{label} 刷新接口响应格式异常"

    if int(result.get("status") or 0) != 1:
        detail = str(result.get("error_msg") or result.get("msg") or result.get("error_code") or "未知错误")
        return False, cookie, f"{label} 会话已失效或被拒绝: {detail}"

    data = result.get("data")
    if not isinstance(data, dict):
        return False, cookie, f"{label} 刷新成功但未返回会话数据"
    try:
        updates = _renew_updates(data, seed)
    except KugouRenewError as exc:
        return False, cookie, f"{label} {exc}"

    LOGGER.info("Kugou %s session refreshed (fields: %s)", "lite" if lite else "standard", ",".join(sorted(updates)))
    return True, merge_cookie(cookie, updates), f"{label} 会话已续期 (更新字段: {','.join(sorted(updates))})"

