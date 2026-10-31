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
from eth_utils import to_checksum_address
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


ORDER_EIP712_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Order": [
        {"name": "salt", "type": "uint256"},
        {"name": "maker", "type": "address"},
        {"name": "signer", "type": "address"},
        {"name": "tokenId", "type": "uint256"},
        {"name": "makerAmount", "type": "uint256"},
        {"name": "takerAmount", "type": "uint256"},
        {"name": "side", "type": "uint8"},
        {"name": "signatureType", "type": "uint8"},
        {"name": "timestamp", "type": "uint256"},
        {"name": "metadata", "type": "bytes32"},
        {"name": "builder", "type": "bytes32"},
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
        self._checksum_address = to_checksum_address(self._address)
        self._domain_cache = {}

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

    def sign_order(self, order_data: dict, verifying_contract: str = "0xE111180000d2663C0091e4f400237545B87B996B") -> str:
        """
        Sign a V2 order with EIP-712 for CLOB submission.

        Args:
            order_data: Order fields to sign (matching V2 types)
            verifying_contract: The verifying contract address (defaults to standard V2 exchange)

        Returns:
            Hex signature string
        """
        # Ensure verifying_contract is checksummed (cached per contract)
        if verifying_contract not in self._domain_cache:
            contract_addr = to_checksum_address(verifying_contract)
            self._domain_cache[verifying_contract] = {
                "name": "Polymarket CTF Exchange",
                "version": "2",
                "chainId": self._chain_id,
                "verifyingContract": contract_addr,
            }
        
        domain = self._domain_cache[verifying_contract]
        
        typed_data = {
            "types": ORDER_EIP712_TYPES,
            "primaryType": "Order",
            "domain": domain,
            "message": {
                "salt": int(order_data["salt"]),
                "maker": to_checksum_address(order_data["maker"]),
                "signer": to_checksum_address(order_data["signer"]),
                "tokenId": int(order_data["tokenId"]),
                "makerAmount": int(order_data["makerAmount"]),
                "takerAmount": int(order_data["takerAmount"]),
                "side": int(order_data["side"]),
                "signatureType": int(order_data["signatureType"]),
                "timestamp": int(order_data["timestamp"]),
                "metadata": order_data["metadata"],
                "builder": order_data["builder"],
            },
        }

        sig = self.sign_eip712(typed_data)
        if not sig.startswith("0x"):
            sig = "0x" + sig
        return sig
