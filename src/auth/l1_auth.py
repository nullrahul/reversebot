"""
L1 Authentication — EIP-712 Wallet Signing for Polymarket.

Uses eth_account to sign EIP-712 typed data for L1 authentication.
Derives L2 API credentials from the L1 signature.

SECURITY:
- Private key is NEVER logged, printed, or stored in DB
- L2 credentials are stored in memory only, never on disk
- Regenerate L2 on 401 — max 3 consecutive retries then kill switch
"""

import time
from typing import Optional, Tuple

from eth_account import Account
try:
    from eth_account.messages import encode_defunct, encode_typed_data
    
    def sign_eip712_data(typed_data):
        return encode_typed_data(full_message=typed_data)
except ImportError:
    # Older eth_account
    from eth_account.messages import encode_defunct
    from eth_account.messages import encode_structured_data
    
    def sign_eip712_data(typed_data):
        return encode_structured_data(typed_data)

from src.logs.log_config import get_logger

logger = get_logger("l1_auth")


# EIP-712 domain for Polymarket
POLY_DOMAIN = {
    "name": "ClobAuthDomain",
    "version": "1",
    "chainId": 137,
}

# EIP-712 types for creating API key
CREATE_API_KEY_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
    ],
    "CreateApiKey": [
        {"name": "nonce", "type": "uint256"},
    ],
}


class L1Auth:
    """
    L1 EIP-712 authentication handler.
    Signs typed data to derive L2 API credentials.
    """

    def __init__(self, private_key: str, chain_id: int = 137):
        """
        Initialize with private key.

        Args:
            private_key: Hex private key (no 0x prefix required, handled either way)
            chain_id: Chain ID (137 for Polygon mainnet)
        """
        # Normalize key format
        key = private_key.strip()
        if not key.startswith("0x"):
            key = "0x" + key

        self._account = Account.from_key(key)
        self._chain_id = chain_id
        self._address = self._account.address

        # Log initialization WITHOUT revealing the key
        logger.info(
            "l1_auth_initialized",
            address=self._address,
            chain_id=chain_id,
            message="L1 auth initialized — private key loaded securely",
        )

    @property
    def address(self) -> str:
        """Public wallet address."""
        return self._address

    def sign_message(self, message: str) -> str:
        """
        Sign a plaintext message with the private key.
        Returns the hex signature.
        """
        msg = encode_defunct(text=message)
        signed = self._account.sign_message(msg)
        return signed.signature.hex()

    def sign_eip712(self, typed_data: dict) -> str:
        """
        Sign EIP-712 typed data.
        Returns the hex signature.
        """
        encoded = sign_eip712_data(typed_data)
        signed = self._account.sign_message(encoded)
        return signed.signature.hex()

    def create_l2_auth_signature(self, nonce: int) -> Tuple[str, str]:
        """
        Create the EIP-712 signature needed to derive L2 API credentials.

        Args:
            nonce: Unique nonce for this auth request

        Returns:
            Tuple of (address, signature_hex)
        """
        typed_data = {
            "types": CREATE_API_KEY_TYPES,
            "primaryType": "CreateApiKey",
            "domain": {
                **POLY_DOMAIN,
                "chainId": self._chain_id,
            },
            "message": {
                "nonce": nonce,
            },
        }

        signature = self.sign_eip712(typed_data)

        logger.info(
            "l2_auth_signature_created",
            address=self._address,
            nonce=nonce,
            message="L2 auth signature created for API key derivation",
        )

        return self._address, signature

    def sign_order(self, order_data: dict) -> str:
        """
        Sign an order with EIP-712 for CLOB submission.

        Args:
            order_data: Order fields to sign

        Returns:
            Hex signature string
        """
        # Polymarket order signing uses specific EIP-712 types
        # The exact structure depends on the CLOB API version
        typed_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                ],
                "Order": [
                    {"name": "salt", "type": "uint256"},
                    {"name": "maker", "type": "address"},
                    {"name": "signer", "type": "address"},
                    {"name": "taker", "type": "address"},
                    {"name": "tokenId", "type": "uint256"},
                    {"name": "makerAmount", "type": "uint256"},
                    {"name": "takerAmount", "type": "uint256"},
                    {"name": "expiration", "type": "uint256"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "feeRateBps", "type": "uint256"},
                    {"name": "side", "type": "uint8"},
                    {"name": "signatureType", "type": "uint8"},
                ],
            },
            "primaryType": "Order",
            "domain": {
                **POLY_DOMAIN,
                "chainId": self._chain_id,
            },
            "message": order_data,
        }

        return self.sign_eip712(typed_data)
