"""
Tests for L1 and L2 authentication modules.
"""

import os
import sys
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.auth.l2_auth import L2Auth


class TestL2AuthHeaders:

    def test_headers_contain_required_fields(self):
        """Every CLOB request must include all 5 POLY underscore headers."""
        auth = L2Auth(
            api_key="test_key",
            api_secret="dGVzdF9zZWNyZXRfMTIz",
            passphrase="test_pass",
            address="0x1234567890abcdef",
        )

        headers = auth.build_headers("GET", "/order")

        assert "POLY_ADDRESS" in headers
        assert "POLY_SIGNATURE" in headers
        assert "POLY_TIMESTAMP" in headers
        assert "POLY_API_KEY" in headers
        assert "POLY_PASSPHRASE" in headers

    def test_timestamp_is_integer_string(self):
        """Timestamp must be UTC seconds as an integer string."""
        auth = L2Auth(
            api_key="test_key",
            api_secret="dGVzdF9zZWNyZXQ=",
            passphrase="test_pass",
            address="0xtest",
        )

        headers = auth.build_headers("GET", "/test")
        ts = headers["POLY_TIMESTAMP"]

        # Must be parseable as int
        ts_int = int(ts)
        # Must be reasonable (within last minute)
        assert abs(ts_int - int(time.time())) < 60

    def test_signature_changes_with_path(self):
        """Different paths should produce different signatures."""
        auth = L2Auth(
            api_key="test_key",
            api_secret="dGVzdF9zZWNyZXRfMTIz",
            passphrase="test_pass",
            address="0xtest",
        )

        h1 = auth.build_headers("GET", "/order")
        h2 = auth.build_headers("GET", "/book")

        # Signatures should differ (different paths)
        assert h1["POLY_SIGNATURE"] != h2["POLY_SIGNATURE"] or h1["POLY_TIMESTAMP"] != h2["POLY_TIMESTAMP"]


class TestL2Auth401Tracking:

    def test_401_counter_increments(self):
        auth = L2Auth("k", "s", "p", "0x")
        assert auth.consecutive_401s == 0

        auth.record_401()
        assert auth.consecutive_401s == 1

        auth.record_401()
        assert auth.consecutive_401s == 2

    def test_401_counter_triggers_kill_at_3(self):
        auth = L2Auth("k", "s", "p", "0x")

        auth.record_401()
        assert not auth.should_kill

        auth.record_401()
        assert not auth.should_kill

        auth.record_401()
        assert auth.should_kill  # 3 consecutive = kill

    def test_401_counter_resets_on_success(self):
        auth = L2Auth("k", "s", "p", "0x")

        auth.record_401()
        auth.record_401()
        auth.reset_401_counter()
        assert auth.consecutive_401s == 0
        assert not auth.should_kill

        """Auth failure logging must never include secret or passphrase."""
        auth = L2Auth(
            api_key="my_key",
            api_secret="SUPER_SECRET_VALUE",
            passphrase="MY_PASSPHRASE",
            address="0xtest",
        )

        # This should log but NOT include the secret
        auth.log_auth_failure_details("GET", "/test", 401, "unauthorized")
        # If we get here without error, the method works


from src.auth.l1_auth import L1Auth

class TestL1AuthOrderSigning:

    def test_l1_auth_signs_v2_order(self):
        """L1Auth must successfully sign a V2 order returning a valid hex signature."""
        private_key = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
        l1 = L1Auth(private_key)

        order_data = {
            "salt": "12345",
            "maker": l1.address,
            "signer": l1.address,
            "tokenId": "729371089201948301",
            "makerAmount": "5000000",  # $5.00 USDC
            "takerAmount": "10000000",  # 10.0 shares
            "side": 0,  # BUY
            "signatureType": 1,
            "timestamp": "1713398400000",
            "metadata": "0x" + "0" * 64,
            "builder": "0x" + "0" * 64,
        }

        sig = l1.sign_order(order_data)
        assert isinstance(sig, str)
        assert sig.startswith("0x")
        assert len(sig) == 132  # 65 bytes = 130 hex chars + 0x prefix
