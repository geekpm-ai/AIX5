import base64
import datetime as dt
import os
import time
import xml.etree.ElementTree as ET

import pytest
from argon2 import PasswordHasher
from lxml import etree
from zeep.wsse.username import UsernameToken

from aix5_ai.onvif_auth import (
    MAX_CLOCK_SKEW,
    OnvifAuthError,
    SOAP,
    WSSE,
    WSU,
    authenticate,
)
from aix5_ai.store import Store


@pytest.fixture
def account(tmp_path):
    store = Store(tmp_path / "app.db")
    store.change_account(
        "owner", PasswordHasher().hash("actual-password"), "actual-password"
    )
    yield store
    store.close()


def token(password="actual-password", username="owner", created=None, digest=True):
    envelope = etree.fromstring(
        f'<s:Envelope xmlns:s="{SOAP}"><s:Body>'
        '<GetCapabilities xmlns="http://www.onvif.org/ver10/device/wsdl"/>'
        "</s:Body></s:Envelope>"
    )
    UsernameToken(username, password, use_digest=digest, created=created).apply(
        envelope, {}
    )
    return etree.tostring(envelope).decode()


def test_real_zeep_digest_and_password_text_authenticate(account):
    authenticate(account, "", token())
    authenticate(account, "", token(digest=False))
    basic = base64.b64encode(b"owner:actual-password").decode()
    authenticate(account, f"Basic {basic}")


@pytest.mark.parametrize("mode", ["digest", "text", "basic"])
def test_wrong_password_fails_for_every_mode(account, mode):
    if mode == "basic":
        authorization = "Basic " + base64.b64encode(b"owner:wrong-password").decode()
        body = ""
    else:
        authorization = ""
        body = token(password="wrong-password", digest=mode == "digest")
    with pytest.raises(OnvifAuthError, match="用户名或密码错误"):
        authenticate(account, authorization, body)


def test_digest_nonce_cannot_be_reused_even_after_restart(account):
    body = token()
    authenticate(account, "", body)
    with pytest.raises(OnvifAuthError, match="已使用"):
        authenticate(account, "", body)
    account.change_account("owner", password="actual-password")
    with pytest.raises(OnvifAuthError, match="已使用"):
        authenticate(account, "", body)
    reopened = Store(account.root / "app.db")
    try:
        with pytest.raises(OnvifAuthError, match="已使用"):
            authenticate(reopened, "", body)
        authenticate(reopened, "", token())
    finally:
        reopened.close()


@pytest.mark.parametrize("seconds", [-MAX_CLOCK_SKEW - 10, MAX_CLOCK_SKEW + 10])
def test_digest_rejects_stale_and_future_timestamp(account, seconds):
    created = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)
    with pytest.raises(OnvifAuthError, match="时间"):
        authenticate(account, "", token(created=created))


@pytest.mark.parametrize(
    "mutation", ["nonce", "created", "digest", "body", "duplicate", "type"]
)
def test_digest_rejects_malformed_or_misplaced_token(account, mutation):
    root = ET.fromstring(token())
    security = root.find(f"{{{SOAP}}}Header/{{{WSSE}}}Security")
    user_token = security.find(f"{{{WSSE}}}UsernameToken")
    if mutation == "nonce":
        user_token.find(f"{{{WSSE}}}Nonce").text = "not-base64"
    elif mutation == "created":
        user_token.remove(user_token.find(f"{{{WSU}}}Created"))
    elif mutation == "digest":
        user_token.find(f"{{{WSSE}}}Password").text = "arbitrary-digest"
    elif mutation == "body":
        root.find(f"{{{SOAP}}}Header").remove(security)
        root.find(f"{{{SOAP}}}Body").append(security)
    elif mutation == "duplicate":
        user_token.append(ET.Element(f"{{{WSSE}}}Username"))
    else:
        user_token.find(f"{{{WSSE}}}Password").set("Type", "unknown")
    with pytest.raises(OnvifAuthError):
        authenticate(account, "", ET.tostring(root).decode())


def test_legacy_account_requires_verified_login_and_secret_stays_encrypted(account):
    account.set("onvif_credential", None)
    with pytest.raises(OnvifAuthError, match="尚未初始化"):
        authenticate(account, "", token())
    with pytest.raises(OnvifAuthError):
        authenticate(account, "Basic " + base64.b64encode(b"owner:wrong").decode())
    assert account.onvif_password() is None
    authenticate(account, "", token(digest=False))
    authenticate(account, "", token())
    assert account.onvif_password() == "actual-password"
    assert "actual-password" not in account.get("onvif_credential")
    assert os.stat(account.root / ".onvif-secret.key").st_mode & 0o777 == 0o600


def test_credential_rotation_invalidates_old_digest_and_survives_restart(account):
    account.change_account(
        "renamed", PasswordHasher().hash("new-password"), "new-password"
    )
    for username, password in [
        ("owner", "actual-password"),
        ("renamed", "actual-password"),
    ]:
        with pytest.raises(OnvifAuthError):
            authenticate(account, "", token(username=username, password=password))
    reopened = Store(account.root / "app.db")
    try:
        authenticate(reopened, "", token(username="renamed", password="new-password"))
    finally:
        reopened.close()


def test_missing_key_and_external_hash_change_fail_closed(account):
    key = account.root / ".onvif-secret.key"
    key.unlink()
    assert account.onvif_password() is None
    authenticate(account, "", token(digest=False))
    assert account.onvif_password() == "actual-password"
    account.set("password_hash", PasswordHasher().hash("new-password"))
    assert account.onvif_password() is None
    with pytest.raises(OnvifAuthError, match="尚未初始化"):
        authenticate(account, "", token())


@pytest.mark.parametrize("damaged", [b"", b"invalid-key"])
def test_damaged_key_recovers_only_after_verified_password(account, damaged):
    key = account.root / ".onvif-secret.key"
    key.write_bytes(damaged)
    with pytest.raises(OnvifAuthError, match="尚未初始化"):
        authenticate(account, "", token())
    with pytest.raises(OnvifAuthError, match="用户名或密码错误"):
        authenticate(account, "", token(password="wrong", digest=False))
    assert key.read_bytes() == damaged
    authenticate(account, "", token(digest=False))
    authenticate(account, "", token())
    assert account.onvif_password() == "actual-password"
    assert key.stat().st_mode & 0o777 == 0o600
    assert not list(account.root.glob(".onvif-key-*"))


def test_expired_nonce_cleanup_keeps_valid_replay_entries(account):
    now = time.time()
    assert account.consume_onvif_nonce("old", now - 1, now - 2)
    assert account.consume_onvif_nonce("valid", now + 100, now)
    rows = account.db.execute("SELECT nonce_hash FROM onvif_nonces").fetchall()
    assert [row[0] for row in rows] == ["valid"]
