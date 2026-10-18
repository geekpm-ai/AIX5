"""ONVIF Basic / WS-Security authentication for the single local account."""

import base64
import binascii
import datetime as dt
import hashlib
import hmac
import time
import xml.etree.ElementTree as ET

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

SOAP = "http://www.w3.org/2003/05/soap-envelope"
SOAP11 = "http://schemas.xmlsoap.org/soap/envelope/"
WSSE = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
)
WSU = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
)
TOKEN_PROFILE = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0"
)
PASSWORD_DIGEST = TOKEN_PROFILE + "#PasswordDigest"
PASSWORD_TEXT = TOKEN_PROFILE + "#PasswordText"
MAX_CLOCK_SKEW = 300


class OnvifAuthError(ValueError):
    pass


def _one(parent, tag):
    matches = parent.findall(tag)
    if len(matches) != 1:
        raise OnvifAuthError("ONVIF 认证信息无效")
    return matches[0]


def authenticate(store, authorization: str, body: str = "", *, now=None):
    """Reject bad credentials, stale timestamps and replayed digest nonces.

    Caller serializes this operation with account updates. UsernameToken values
    are read only from the SOAP security header, never from the request body.
    """
    now = time.time() if now is None else now
    if authorization.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(authorization.split(None, 1)[1], validate=True)
            username, password = decoded.decode("utf-8").split(":", 1)
        except (ValueError, UnicodeDecodeError, binascii.Error, IndexError):
            raise OnvifAuthError("ONVIF 认证信息无效") from None
        _verify_password(store, username, password)
        return
    if not body:
        raise OnvifAuthError("ONVIF 需要用户名和密码")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise OnvifAuthError("ONVIF 认证信息无效") from None
    namespace = root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""
    if namespace not in (SOAP, SOAP11) or root.tag != f"{{{namespace}}}Envelope":
        raise OnvifAuthError("ONVIF 认证信息无效")
    header = _one(root, f"{{{namespace}}}Header")
    security = _one(header, f"{{{WSSE}}}Security")
    token = _one(security, f"{{{WSSE}}}UsernameToken")
    username = _one(token, f"{{{WSSE}}}Username").text or ""
    password_element = _one(token, f"{{{WSSE}}}Password")
    password = password_element.text or ""
    password_type = password_element.get("Type", PASSWORD_TEXT)
    if password_type == PASSWORD_TEXT:
        _verify_password(store, username, password)
        return
    if password_type != PASSWORD_DIGEST or username != store.get("username"):
        raise OnvifAuthError("ONVIF 用户名或密码错误")
    nonce_text = _one(token, f"{{{WSSE}}}Nonce").text or ""
    created = _one(token, f"{{{WSU}}}Created").text or ""
    try:
        nonce = base64.b64decode(nonce_text, validate=True)
        digest = base64.b64decode(password, validate=True)
        timestamp = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
        if timestamp.tzinfo is None or not 8 <= len(nonce) <= 256 or len(digest) != 20:
            raise ValueError
        created_at = timestamp.timestamp()
    except (ValueError, OverflowError, binascii.Error):
        raise OnvifAuthError("ONVIF 认证信息无效") from None
    if abs(created_at - now) > MAX_CLOCK_SKEW:
        raise OnvifAuthError("ONVIF 认证时间已过期，请检查设备与客户端时间")
    secret = store.onvif_password()
    if secret is None:
        raise OnvifAuthError("ONVIF 摘要认证尚未初始化，请先使用当前账号登录网页")
    expected = hashlib.sha1(
        nonce + created.encode("utf-8") + secret.encode("utf-8")
    ).digest()
    if not hmac.compare_digest(digest, expected):
        raise OnvifAuthError("ONVIF 用户名或密码错误")
    nonce_key = hashlib.sha256(username.encode() + b"\0" + nonce).hexdigest()
    if not store.consume_onvif_nonce(nonce_key, created_at + MAX_CLOCK_SKEW, now):
        raise OnvifAuthError("ONVIF 认证请求已使用，请重新发送")


def _verify_password(store, username, password):
    if username != store.get("username"):
        raise OnvifAuthError("ONVIF 用户名或密码错误")
    try:
        PasswordHasher().verify(store.get("password_hash"), password)
    except (VerificationError, InvalidHashError):
        raise OnvifAuthError("ONVIF 用户名或密码错误") from None
    # Existing installations gain digest support after the first successful
    # Basic/PasswordText authentication, or after a normal web login.
    store.set_onvif_password(password)
