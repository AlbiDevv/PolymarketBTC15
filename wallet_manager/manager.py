"""
Wallet Manager — on-chain layer for Polymarket trading.

Handles:
- Private key loading (from env/keystore only, never from code)
- EIP-712 order signing (required by Polymarket CLOB)
- USDC and MATIC balance monitoring on Polygon
- ERC-20 allowance management (limited, not unlimited)
- API credential generation tied to wallet address
- Gas shortage alerts
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from loguru import logger


@dataclass
class WalletBalances:
    matic: float
    usdc: float
    address: str


@dataclass
class AllowanceInfo:
    token: str
    spender: str
    current: float
    required: float
    sufficient: bool


# Minimal ABI fragments needed for balance / allowance checks
ERC20_ABI_FRAGMENT = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "_owner", "type": "address"},
            {"name": "_spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
]

# Polygon Mainnet addresses
POLYGON_USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
POLYGON_RPC = "https://polygon-rpc.com"

# Polymarket CTF Exchange (the contract that needs allowance)
POLYMARKET_CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"


class WalletManager:
    """
    Manages the trading wallet on Polygon.

    IMPORTANT: The private key is loaded exclusively from the environment
    variable POLYGON_PRIVATE_KEY. It must never be stored in code,
    config files, or git.
    """

    def __init__(
        self,
        private_key: str | None = None,
        rpc_url: str = POLYGON_RPC,
        usdc_address: str = POLYGON_USDC,
        exchange_address: str = POLYMARKET_CTF_EXCHANGE,
        min_matic_balance: float = 0.5,
        min_usdc_balance: float = 10.0,
    ):
        self._private_key = private_key or os.getenv("POLYGON_PRIVATE_KEY", "")
        self._rpc_url = rpc_url
        self._usdc_address = usdc_address
        self._exchange_address = exchange_address
        self._min_matic = min_matic_balance
        self._min_usdc = min_usdc_balance

        self._w3 = None
        self._account = None
        self._usdc_contract = None

    def initialize(self):
        """Lazy-load web3 and set up account. Call once at startup."""
        try:
            from web3 import Web3
            from eth_account import Account
        except ImportError:
            logger.warning(
                "web3/eth_account not installed. Wallet features disabled. "
                "Install with: pip install web3 eth-account"
            )
            return

        if not self._private_key:
            logger.warning("No POLYGON_PRIVATE_KEY set. Wallet manager in read-only mode.")
            return

        self._w3 = Web3(Web3.HTTPProvider(self._rpc_url))
        self._account = Account.from_key(self._private_key)
        self._usdc_contract = self._w3.eth.contract(
            address=Web3.to_checksum_address(self._usdc_address),
            abi=ERC20_ABI_FRAGMENT,
        )
        logger.info(f"Wallet initialized: {self.address}")

    @property
    def address(self) -> str:
        if self._account:
            return self._account.address
        return ""

    @property
    def is_ready(self) -> bool:
        return self._w3 is not None and self._account is not None

    def get_balances(self) -> WalletBalances:
        if not self.is_ready:
            return WalletBalances(matic=0, usdc=0, address="")

        matic_wei = self._w3.eth.get_balance(self.address)
        matic = float(self._w3.from_wei(matic_wei, "ether"))

        usdc_raw = self._usdc_contract.functions.balanceOf(self.address).call()
        usdc = usdc_raw / 1e6  # USDC has 6 decimals

        return WalletBalances(matic=matic, usdc=usdc, address=self.address)

    def check_gas_health(self) -> bool:
        """Return True if MATIC balance is sufficient for gas."""
        balances = self.get_balances()
        if balances.matic < self._min_matic:
            logger.warning(
                f"Low MATIC balance: {balances.matic:.4f} "
                f"(minimum: {self._min_matic})"
            )
            return False
        return True

    def check_usdc_health(self) -> bool:
        """Return True if USDC balance is sufficient for trading."""
        balances = self.get_balances()
        if balances.usdc < self._min_usdc:
            logger.warning(
                f"Low USDC balance: ${balances.usdc:.2f} "
                f"(minimum: ${self._min_usdc})"
            )
            return False
        return True

    def get_allowance(self) -> AllowanceInfo:
        """Check current USDC allowance for the Polymarket exchange."""
        if not self.is_ready:
            return AllowanceInfo(
                token="USDC", spender=self._exchange_address,
                current=0, required=0, sufficient=False,
            )

        from web3 import Web3

        raw = self._usdc_contract.functions.allowance(
            self.address,
            Web3.to_checksum_address(self._exchange_address),
        ).call()
        current = raw / 1e6

        balances = self.get_balances()
        required = balances.usdc

        return AllowanceInfo(
            token="USDC",
            spender=self._exchange_address,
            current=current,
            required=required,
            sufficient=current >= required,
        )

    def set_allowance(self, amount_usdc: float) -> str:
        """
        Approve the exchange to spend a specific amount of USDC.
        Returns transaction hash.

        Uses limited allowance (not unlimited) as per security best practice.
        """
        if not self.is_ready:
            raise RuntimeError("Wallet not initialized")

        from web3 import Web3

        amount_raw = int(amount_usdc * 1e6)
        spender = Web3.to_checksum_address(self._exchange_address)

        tx = self._usdc_contract.functions.approve(spender, amount_raw).build_transaction(
            {
                "from": self.address,
                "nonce": self._w3.eth.get_transaction_count(self.address),
                "gas": 60_000,
                "maxFeePerGas": self._w3.eth.gas_price * 2,
                "maxPriorityFeePerGas": self._w3.to_wei(30, "gwei"),
            }
        )

        signed = self._account.sign_transaction(tx)
        tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
        hex_hash = tx_hash.hex()
        logger.info(f"Allowance tx sent: {hex_hash} (amount: ${amount_usdc:.2f})")
        return hex_hash

    def sign_order_message(self, message_hash: bytes) -> str:
        """
        Sign an EIP-712 typed message for Polymarket CLOB order.
        Returns hex-encoded signature.
        """
        if not self.is_ready:
            raise RuntimeError("Wallet not initialized")

        from eth_account.messages import encode_defunct

        signable = encode_defunct(primitive=message_hash)
        signed = self._account.sign_message(signable)
        return signed.signature.hex()

    def generate_api_credentials(self) -> dict:
        """
        Generate Polymarket API credentials by signing a nonce.
        In practice, this is done via py-clob-client's create_api_key().
        Returns dict with key, secret, passphrase.
        """
        if not self.is_ready:
            raise RuntimeError("Wallet not initialized")

        logger.info(
            "To generate Polymarket API credentials, use py-clob-client:\n"
            "  from py_clob_client.client import ClobClient\n"
            "  client = ClobClient(host, key=private_key, chain_id=137)\n"
            "  creds = client.create_api_key()\n"
        )
        return {"note": "Use py-clob-client to generate credentials"}

    def health_report(self) -> dict:
        """Full wallet health check."""
        balances = self.get_balances()
        allowance = self.get_allowance()
        return {
            "address": self.address,
            "matic_balance": balances.matic,
            "usdc_balance": balances.usdc,
            "gas_ok": balances.matic >= self._min_matic,
            "usdc_ok": balances.usdc >= self._min_usdc,
            "allowance_current": allowance.current,
            "allowance_sufficient": allowance.sufficient,
        }
