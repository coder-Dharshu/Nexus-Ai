"""
Nexus AI — Auth unit tests.
"""
from __future__ import annotations

import time

import pytest
from fastapi import HTTPException

from src.security.auth import (
    create_token_pair,
    decode_token,
    hash_password,
    verify_password,
    csrf_manager,
    TokenPair,
)


class TestPasswordHashing:

    def test_hash_is_not_plaintext(self):
        pw = "SecurePassword123!"
        h = hash_password(pw)
        assert h != pw

    def test_hash_is_bcrypt(self):
        h = hash_password("test")
        assert h.startswith("$2b$")

    def test_verify_correct_password(self):
        pw = "MyP@ssw0rd!"
        h = hash_password(pw)
        assert verify_password(pw, h) is True

    def test_verify_wrong_password(self):
        h = hash_password("correct")
        assert verify_password("incorrect", h) is False

    def test_different_hashes_for_same_password(self):
        # bcrypt uses salt — same password → different hash each time
        pw = "SamePassword"
        h1 = hash_password(pw)
        h2 = hash_password(pw)
        assert h1 != h2
        # But both should verify correctly
        assert verify_password(pw, h1)
        assert verify_password(pw, h2)


class TestJWTTokens:

    def test_create_returns_token_pair(self):
        pair = create_token_pair("user_001")
        assert isinstance(pair, TokenPair)
        assert pair.access_token
        assert pair.refresh_token

    def test_access_token_decodes(self):
        pair = create_token_pair("user_002")
        data = decode_token(pair.access_token)
        assert data.sub == "user_002"
        assert data.scope == "access"

    def test_refresh_token_decodes(self):
        pair = create_token_pair("user_003")
        data = decode_token(pair.refresh_token, expected_scope="refresh")
        assert data.sub == "user_003"
        assert data.scope == "refresh"

    def test_token_has_unique_jti(self):
        p1 = create_token_pair("user_004")
        p2 = create_token_pair("user_004")
        d1 = decode_token(p1.access_token)
        d2 = decode_token(p2.access_token)
        assert d1.jti != d2.jti

    def test_token_has_iat(self):
        pair = create_token_pair("user_005")
        data = decode_token(pair.access_token)
        assert data.iat > 0
        assert data.iat <= int(time.time()) + 1

    def test_token_has_exp(self):
        pair = create_token_pair("user_006")
        data = decode_token(pair.access_token)
        assert data.exp > data.iat

    def test_wrong_scope_raises_401(self):
        pair = create_token_pair("user_007")
        with pytest.raises(HTTPException) as exc:
            decode_token(pair.access_token, expected_scope="refresh")
        assert exc.value.status_code == 401

    def test_refresh_as_access_raises_401(self):
        pair = create_token_pair("user_008")
        with pytest.raises(HTTPException) as exc:
            decode_token(pair.refresh_token, expected_scope="access")
        assert exc.value.status_code == 401

    def test_garbage_token_raises_401(self):
        with pytest.raises(HTTPException) as exc:
            decode_token("garbage.not.a.token")
        assert exc.value.status_code == 401

    def test_truncated_token_raises_401(self):
        pair = create_token_pair("user_009")
        truncated = pair.access_token[:20]
        with pytest.raises(HTTPException):
            decode_token(truncated)

    def test_modified_payload_raises_401(self):
        pair = create_token_pair("user_010")
        parts = pair.access_token.split(".")
        # Modify the payload section
        modified = parts[0] + ".TAMPERED" + parts[2]
        with pytest.raises(HTTPException):
            decode_token(modified)

    def test_expires_in_positive(self):
        pair = create_token_pair("user_011")
        assert pair.expires_in > 0

    def test_different_users_different_subs(self):
        p1 = create_token_pair("alice")
        p2 = create_token_pair("bob")
        d1 = decode_token(p1.access_token)
        d2 = decode_token(p2.access_token)
        assert d1.sub == "alice"
        assert d2.sub == "bob"
        assert d1.sub != d2.sub


class TestCSRF:

    def test_generate_token(self):
        token = csrf_manager.generate_token("session_abc")
        assert token
        assert ":" in token

    def test_validate_own_token(self):
        token = csrf_manager.generate_token("session_xyz")
        assert csrf_manager.validate_token("session_xyz", token) is True

    def test_wrong_session_rejected(self):
        token = csrf_manager.generate_token("session_A")
        assert csrf_manager.validate_token("session_B", token) is False

    def test_tampered_token_rejected(self):
        token = csrf_manager.generate_token("session_C")
        tampered = token[:-10] + "tampered!!"
        assert csrf_manager.validate_token("session_C", tampered) is False

    def test_garbage_token_rejected(self):
        assert csrf_manager.validate_token("session_D", "not:a:valid:token") is False

    def test_empty_token_rejected(self):
        assert csrf_manager.validate_token("session_E", "") is False
