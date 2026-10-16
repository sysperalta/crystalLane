"""One-time codes: 6 digits from `secrets`, stored only as a keyed hash, compared in constant time."""

import hashlib
import hmac
import secrets

OTP_TTL_SECONDS = 300
MAX_SENDS_PER_CALL = 2  # initial + 1 resend
MAX_ATTEMPTS_PER_CALL = 3


class OtpService:
    def __init__(self) -> None:
        # Per-process key: a dump of call state can't be brute-forced offline.
        self._key = secrets.token_bytes(32)

    @staticmethod
    def generate() -> str:
        return f"{secrets.randbelow(1_000_000):06d}"

    def digest(self, code: str) -> bytes:
        return hmac.new(self._key, code.encode("ascii"), hashlib.sha256).digest()

    def matches(self, code: str, stored: bytes) -> bool:
        return hmac.compare_digest(self.digest(code), stored)


def sms_body(code: str) -> str:
    return (
        f"Crystal Lane Freight: your verification code is {code}. "
        "It expires in 5 minutes. Only read it to the agent on your current call."
    )
