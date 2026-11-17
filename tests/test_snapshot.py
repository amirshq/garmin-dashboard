"""Encrypted GitHub Pages snapshot: page builds from the DB, encryption round-trips, no plaintext leaks."""

import base64
import gzip
import json
import re

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from dashboard import snapshot
from tests.test_sync import TODAY, _seed, conn  # noqa: F401  (fixture)


def _decrypt(lock_html: str, password: str) -> str:
    p = json.loads(re.search(r"const P = (\{.*?\});", lock_html).group(1))
    d = lambda s: base64.b64decode(s)  # noqa: E731
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=d(p["salt"]), iterations=p["iter"]).derive(password.encode())
    return gzip.decompress(AESGCM(key).decrypt(d(p["iv"]), d(p["ct"]), None)).decode()


def test_build_and_encrypt_round_trip(conn):  # noqa: F811
    _seed(conn, loads=[50] * 30)
    page = snapshot.build_html(conn, TODAY)
    assert "Health snapshot" in page and "c-hrv" in page and "Recent nights" in page
    locked = snapshot.encrypt_page(page, "correct horse battery staple", salt=b"0" * 16, iterations=1000)
    assert "Recent nights" not in locked and TODAY.isoformat() not in locked  # nothing readable in the public file
    assert _decrypt(locked, "correct horse battery staple") == page
    with pytest.raises(Exception):
        _decrypt(locked, "wrong password")


def test_fresh_nonce_per_build():
    a = snapshot.encrypt_page("x", "pw-long-enough-123", salt=b"1" * 16, iterations=1000)
    b = snapshot.encrypt_page("x", "pw-long-enough-123", salt=b"1" * 16, iterations=1000)
    assert json.loads(re.search(r"const P = (\{.*?\});", a).group(1))["iv"] != \
           json.loads(re.search(r"const P = (\{.*?\});", b).group(1))["iv"]


@pytest.mark.parametrize("pw", ["", "short"])
def test_weak_password_refused(monkeypatch, pw):
    monkeypatch.setenv("SNAPSHOT_PASSWORD", pw)
    with pytest.raises(SystemExit):
        snapshot.password()
