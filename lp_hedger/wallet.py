"""Local hot wallet stored as an encrypted (web3 secret-storage v3) keystore.

The private key is only ever decrypted into memory of the running process.
It is never written anywhere in plaintext and never sent to the browser.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from eth_account import Account
from eth_account.signers.local import LocalAccount

from .config import data_dir


class WalletLockedError(RuntimeError):
    pass


class HotWallet:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or (data_dir() / "hotwallet.keystore.json")
        self._account: Optional[LocalAccount] = None

    # ---- state -------------------------------------------------------------
    @property
    def exists(self) -> bool:
        return self.path.exists()

    @property
    def unlocked(self) -> bool:
        return self._account is not None

    @property
    def address(self) -> Optional[str]:
        if self._account is not None:
            return self._account.address
        if self.exists:
            with self.path.open() as f:
                raw = json.load(f)
            addr = raw.get("address")
            return "0x" + addr if addr and not addr.startswith("0x") else addr
        return None

    @property
    def account(self) -> LocalAccount:
        if self._account is None:
            raise WalletLockedError("Hot wallet is locked. Unlock it first.")
        return self._account

    @property
    def private_key_hex(self) -> str:
        return self.account.key.hex()

    # ---- lifecycle ---------------------------------------------------------
    def create(self, password: str, private_key: Optional[str] = None) -> str:
        """Create (or import) a wallet, encrypt it to disk and unlock it."""
        if self.exists:
            raise FileExistsError(f"A wallet already exists at {self.path}")
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters")
        acct = Account.from_key(private_key) if private_key else Account.create(os.urandom(32).hex())
        encrypted = Account.encrypt(acct.key, password)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w") as f:
            json.dump(encrypted, f)
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        self._account = acct
        return acct.address

    def unlock(self, password: str) -> str:
        if not self.exists:
            raise FileNotFoundError("No wallet found. Create one first.")
        with self.path.open() as f:
            encrypted = json.load(f)
        try:
            key = Account.decrypt(encrypted, password)
        except ValueError as e:  # wrong password
            raise ValueError("Wrong password") from e
        self._account = Account.from_key(key)
        return self._account.address

    def lock(self) -> None:
        self._account = None

    def sign_message_text(self, text: str) -> str:
        from eth_account.messages import encode_defunct

        hx = self.account.sign_message(encode_defunct(text=text)).signature.hex()
        return hx if hx.startswith("0x") else "0x" + hx

    def sign_hash(self, digest: bytes) -> str:
        acct = self.account
        if hasattr(acct, "unsafe_sign_hash"):
            sig = acct.unsafe_sign_hash(digest)
        else:  # eth-account < 0.13
            sig = acct._sign_hash(digest)  # type: ignore[attr-defined]
        hx = sig.signature.hex()
        return hx if hx.startswith("0x") else "0x" + hx
