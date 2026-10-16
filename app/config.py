"""Runtime configuration, read from environment variables only.

Secrets (API_KEY, TMS_TOKEN, DEMO_CONSOLE_KEY) are never hard-coded, logged or echoed.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

VERSION = "1.2.1"

_MC_RE = re.compile(r"^[0-9]{1,8}$")
_PHONE_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(RuntimeError):
    """The environment is incomplete or unsafe; the service refuses to start."""


@dataclass(frozen=True)
class Settings:
    env: str = "demo"
    api_keys: tuple[str, ...] = field(default=(), repr=False)
    tms_host: str = "127.0.0.1"
    tms_port: int = 9100
    tms_token: str = field(default="", repr=False)
    # Run the TMS simulator inside this process (single-container cloud deploys).
    tms_embedded_sim: bool = True
    tms_fault_rate: float = 0.0
    # MC number -> phone number on file (E.164). Demo stand-in for the TMS carrier master.
    demo_carriers: Mapping[str, str] = field(default_factory=dict, repr=False)
    # Separate secret for the demo console (simulated handset). Never the agent's API key.
    demo_console_key: str | None = field(default=None, repr=False)
    # QA automation only: MC number -> fixed 6-digit OTP. These MCs are active test carriers
    # whose code is known in advance; every other rule (attempts, expiry, resends) still applies.
    qa_test_carriers: Mapping[str, str] = field(default_factory=dict, repr=False)
    fmcsa_latency: tuple[float, float] = (0.05, 0.2)

    @property
    def demo_mode(self) -> bool:
        return self.env != "production"

    def validate(self) -> Settings:
        if not self.demo_mode:
            raise ConfigError(
                "ENV=production is not supported by this POC build: FMCSA, SMS and the TMS "
                "are simulated. Run with ENV=demo."
            )
        if not self.api_keys:
            raise ConfigError("API_KEY is required (comma-separate two keys to rotate)")
        if any(len(key) < 16 for key in self.api_keys):
            raise ConfigError("Each API_KEY must be at least 16 characters")
        if not self.tms_token:
            raise ConfigError("TMS_TOKEN is required")
        if not 0.0 <= self.tms_fault_rate <= 1.0:
            raise ConfigError("TMS_FAULT_RATE must be between 0 and 1")
        if self.demo_console_key is not None:
            if len(self.demo_console_key) < 16:
                raise ConfigError("DEMO_CONSOLE_KEY must be at least 16 characters")
            if self.demo_console_key in self.api_keys:
                raise ConfigError("DEMO_CONSOLE_KEY must differ from API_KEY")
        if self.qa_test_carriers and not self.demo_mode:
            raise ConfigError("QA_TEST_CARRIERS (fixed OTP codes) is only allowed when ENV is not production")
        return self


def load_settings(environ: Mapping[str, str] = os.environ) -> Settings:
    env = (environ.get("ENV") or "demo").strip().lower()
    tms_host = (environ.get("TMS_HOST") or "").strip()
    try:
        return Settings(
            env=env,
            api_keys=tuple(k.strip() for k in (environ.get("API_KEY") or "").split(",") if k.strip()),
            tms_host=tms_host or "127.0.0.1",
            tms_port=int(environ.get("TMS_PORT") or 9100),
            tms_token=(environ.get("TMS_TOKEN") or "").strip(),
            tms_embedded_sim=_parse_bool("TMS_EMBEDDED_SIM", environ.get("TMS_EMBEDDED_SIM"), default=not tms_host),
            tms_fault_rate=float(environ.get("TMS_FAULT_RATE") or 0),
            demo_carriers=_parse_demo_carriers(environ.get("DEMO_CARRIERS") or ""),
            demo_console_key=(environ.get("DEMO_CONSOLE_KEY") or "").strip() or None,
            qa_test_carriers=_parse_qa_test_carriers(environ.get("QA_TEST_CARRIERS") or ""),
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from None


def _parse_bool(name: str, value: str | None, *, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    lowered = value.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false")


def _parse_demo_carriers(raw: str) -> dict[str, str]:
    """'123456:+15551234567,654321:+15559876543' -> {mc: phone}. Errors never echo phone numbers."""
    carriers: dict[str, str] = {}
    for index, entry in enumerate(filter(None, (e.strip() for e in raw.split(","))), start=1):
        mc, sep, phone = entry.partition(":")
        if not sep or not _MC_RE.match(mc.strip()) or not _PHONE_RE.match(phone.strip()):
            raise ConfigError(f"DEMO_CARRIERS entry #{index} must look like 123456:+15551234567")
        carriers[mc.strip()] = phone.strip()
    return carriers


def _parse_qa_test_carriers(raw: str) -> dict[str, str]:
    """'900001:112233' -> {mc: fixed_otp}. Errors never echo the codes."""
    carriers: dict[str, str] = {}
    for index, entry in enumerate(filter(None, (e.strip() for e in raw.split(","))), start=1):
        mc, sep, code = entry.partition(":")
        if not sep or not _MC_RE.match(mc.strip()) or not re.fullmatch(r"[0-9]{6}", code.strip()):
            raise ConfigError(f"QA_TEST_CARRIERS entry #{index} must look like 900001:112233 (MC:6-digit code)")
        carriers[mc.strip()] = code.strip()
    return carriers
