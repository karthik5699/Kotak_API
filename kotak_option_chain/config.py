"""
Configuration and persistence layer for Kotak Neo Option Chain & Greeks Engine.

Manages:
1. Permanent credentials (`data/credentials.json`):
   - `consumer_key`: Kotak Neo Trade API token
   - `mobile_number`: Registered mobile number (normalized to +91XXXXXXXXXX)
   - `ucc`: Unique Client Code (User ID)
   - `mpin`: 6-digit Trading MPIN
   These are requested ONCE from the user and saved permanently on disk (chmod 600).

2. Daily TOTP session state (`data/session.json`):
   - `trading_date`: YYYY-MM-DD (rolls over daily at 06:00 AM IST)
   - `view_token`, `sid`, `edit_token`, `edit_sid`, `edit_rid`, `base_url`, `data_center`, `feed_url`, `rt_url`
   Expires automatically every morning so the WhatsApp LLM agent knows to ask for
   a fresh 6-digit TOTP once per day.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

# Project root & data directory
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"

# Indian Standard Time (UTC+05:30)
IST = timezone(timedelta(hours=5, minutes=30))

# Kotak Neo daily session reset cutoff (06:00 AM IST)
DAILY_RESET_TIME_IST = time(6, 0, 0)

REQUIRED_PERMANENT_FIELDS = ("consumer_key", "mobile_number", "ucc", "mpin")


def normalize_mobile_number(mobile: str) -> str:
    """
    Normalize an Indian mobile number to the '+91XXXXXXXXXX' format required by Kotak Neo API.
    """
    cleaned = re.sub(r"[\s\-\(\)]", "", str(mobile).strip())
    if not cleaned:
        return ""
    if cleaned.startswith("+"):
        return cleaned
    if cleaned.startswith("91") and len(cleaned) == 12 and cleaned.isdigit():
        return f"+{cleaned}"
    if cleaned.startswith("0") and len(cleaned) == 11 and cleaned[1:].isdigit():
        return f"+91{cleaned[1:]}"
    if len(cleaned) == 10 and cleaned.isdigit():
        return f"+91{cleaned}"
    return cleaned


def get_current_trading_date_ist(now: datetime | None = None) -> str:
    """
    Return the active trading session date (YYYY-MM-DD) in IST.
    Kotak sessions expire every morning around 6:00 AM IST. Any login before 06:00 AM IST
    belongs to the previous trading day's window and will expire at 06:00 AM IST.
    """
    now_ist = (now or datetime.now(IST)).astimezone(IST)
    if now_ist.time() < DAILY_RESET_TIME_IST:
        effective_date = (now_ist - timedelta(days=1)).date()
    else:
        effective_date = now_ist.date()
    return effective_date.isoformat()


@dataclass
class PermanentCredentials:
    consumer_key: str = ""
    mobile_number: str = ""
    ucc: str = ""
    mpin: str = ""
    environment: str = "prod"

    def missing_fields(self) -> list[str]:
        missing = []
        for f in REQUIRED_PERMANENT_FIELDS:
            val = getattr(self, f, "")
            if not val or not str(val).strip():
                missing.append(f)
        return missing

    @property
    def is_complete(self) -> bool:
        return len(self.missing_fields()) == 0

    def to_safe_dict(self) -> dict[str, str]:
        """Return a masked summary safe for logging or WhatsApp display."""
        def _mask(val: str, visible: int = 4) -> str:
            if not val:
                return "<not set>"
            if visible <= 0 or len(val) <= visible:
                return "*" * len(val)
            return "*" * (len(val) - visible) + val[-visible:]

        return {
            "consumer_key": _mask(self.consumer_key, 4),
            "mobile_number": self.mobile_number or "<not set>",
            "ucc": self.ucc or "<not set>",
            "mpin": _mask(self.mpin, 0),
            "environment": self.environment,
        }


@dataclass
class DailySession:
    trading_date: str = ""
    authenticated_at_ist: str = ""
    ucc: str = ""
    greeting_name: str = ""
    view_token: str = ""
    sid: str = ""
    edit_token: str = ""
    edit_sid: str = ""
    edit_rid: str = ""
    base_url: str = ""
    data_center: str = ""
    feed_url: str | None = None
    rt_url: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def is_valid_for_today(self, now: datetime | None = None) -> bool:
        """
        Check whether this session has an edit_token and matches the current IST trading date.
        """
        if not self.edit_token or not self.trading_date:
            return False
        expected_date = get_current_trading_date_ist(now)
        return self.trading_date == expected_date


class ConfigStore:
    """
    Manages reading and writing permanent credentials and daily TOTP sessions on disk.
    """

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
        self.credentials_path = self.data_dir / "credentials.json"
        self.session_path = self.data_dir / "session.json"

    def _ensure_dir(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _write_secure_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.chmod(tmp_path, 0o600)
        tmp_path.replace(path)

    def load_credentials(self) -> PermanentCredentials:
        """
        Load permanent credentials from disk, with fallback/override support from environment variables.
        """
        data: dict[str, Any] = {}
        if self.credentials_path.exists():
            try:
                with open(self.credentials_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                data = {}

        consumer_key = os.environ.get("NEO_CONSUMER_KEY") or data.get("consumer_key", "")
        mobile_number = os.environ.get("NEO_MOBILE_NUMBER") or data.get("mobile_number", "")
        ucc = os.environ.get("NEO_UCC") or data.get("ucc", "")
        mpin = os.environ.get("NEO_MPIN") or data.get("mpin", "")
        environment = os.environ.get("NEO_ENVIRONMENT") or data.get("environment", "prod")

        return PermanentCredentials(
            consumer_key=str(consumer_key).strip(),
            mobile_number=normalize_mobile_number(mobile_number) if mobile_number else "",
            ucc=str(ucc).strip(),
            mpin=str(mpin).strip(),
            environment=str(environment).strip() or "prod",
        )

    def save_credentials(
        self,
        consumer_key: str | None = None,
        mobile_number: str | None = None,
        ucc: str | None = None,
        mpin: str | None = None,
        environment: str | None = None,
    ) -> PermanentCredentials:
        """
        Update and permanently save credentials to `data/credentials.json` (mode 0600).
        Only non-None arguments overwrite existing values, allowing partial updates.
        """
        current = self.load_credentials()
        if consumer_key is not None and str(consumer_key).strip():
            current.consumer_key = str(consumer_key).strip()
        if mobile_number is not None and str(mobile_number).strip():
            current.mobile_number = normalize_mobile_number(mobile_number)
        if ucc is not None and str(ucc).strip():
            current.ucc = str(ucc).strip()
        if mpin is not None and str(mpin).strip():
            current.mpin = str(mpin).strip()
        if environment is not None and str(environment).strip():
            current.environment = str(environment).strip()

        self._ensure_dir()
        self._write_secure_json(self.credentials_path, asdict(current))
        return current

    def load_session(self) -> DailySession | None:
        """
        Load the cached daily session from `data/session.json`, or return None if missing.
        """
        if not self.session_path.exists():
            return None
        try:
            with open(self.session_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            return DailySession(
                trading_date=raw.get("trading_date", ""),
                authenticated_at_ist=raw.get("authenticated_at_ist", ""),
                ucc=raw.get("ucc", ""),
                greeting_name=raw.get("greeting_name", ""),
                view_token=raw.get("view_token", ""),
                sid=raw.get("sid", ""),
                edit_token=raw.get("edit_token", ""),
                edit_sid=raw.get("edit_sid", ""),
                edit_rid=raw.get("edit_rid", ""),
                base_url=raw.get("base_url", ""),
                data_center=raw.get("data_center", ""),
                feed_url=raw.get("feed_url"),
                rt_url=raw.get("rt_url"),
                extra=raw.get("extra", {}),
            )
        except (json.JSONDecodeError, OSError):
            return None

    def save_session(self, session: DailySession) -> DailySession:
        """
        Persist the daily session to `data/session.json` (mode 0600).
        """
        self._ensure_dir()
        self._write_secure_json(self.session_path, asdict(session))
        return session

    def clear_session(self) -> None:
        """
        Delete `data/session.json` when a session expires or returns 401/403.
        """
        if self.session_path.exists():
            try:
                self.session_path.unlink()
            except OSError:
                pass
