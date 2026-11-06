"""
Authentication & Session Lifecycle Manager for Kotak Neo API.

Designed specifically for a WhatsApp LLM Agent:
1. Permanent credentials (`consumer_key`, `mobile_number`, `ucc`, `mpin`) are requested ONCE
   and stored permanently in `data/credentials.json`.
2. Daily TOTP is requested ONCE every morning (after the 06:00 AM IST reset) and the resulting
   session tokens (`edit_token`, `edit_sid`, `base_url`, `data_center`, etc.) are cached in
   `data/session.json` for the rest of the trading day.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from neo_api_client import NeoAPI

from kotak_option_chain.config import (
     IST,
    ConfigStore,
    DailySession,
    PermanentCredentials,
    get_current_trading_date_ist,
)


class AuthStateError(Exception):
    """Base exception when an API call cannot proceed due to missing setup or expired daily TOTP."""

    def __init__(self, status: str, message: str, payload: dict[str, Any]) -> None:
        super().__init__(message)
        self.status = status
        self.payload = payload


class SetupRequiredError(AuthStateError):
    """Raised when permanent credentials (mobile_number, ucc, mpin, consumer_key) are missing."""


class TotpRequiredError(AuthStateError):
    """Raised when today's TOTP login has not been completed or has expired."""


class AuthManager:
    """
    Coordinates permanent credential setup, daily TOTP 2FA authentication, and NeoAPI session rehydration.
    """

    def __init__(
        self,
        store: ConfigStore | None = None,
        client_factory: Any = NeoAPI,
    ) -> None:
        self.store = store or ConfigStore()
        self.client_factory = client_factory

    def get_auth_status(self, now: datetime | None = None) -> dict[str, Any]:
        """
        Inspect permanent credentials and today's session state without making network calls.
        Returns a structured dictionary suitable for an LLM agent on WhatsApp.
        """
        creds = self.store.load_credentials()
        missing = creds.missing_fields()

        if missing:
            field_descriptions = {
                "consumer_key": "Consumer Key token from Kotak Neo App (More -> Trade API -> Generate application)",
                "mobile_number": "Registered mobile number (e.g. +919876543210 or 10-digit number)",
                "ucc": "Unique Client Code (UCC / User ID from Kotak Neo Profile)",
                "mpin": "6-digit Trading Account MPIN",
            }
            missing_prompts = [f"- {k}: {field_descriptions[k]}" for k in missing]
            return {
                "status": "SETUP_REQUIRED",
                "ready": False,
                "missing_fields": missing,
                "saved_credentials": creds.to_safe_dict(),
                "message": "Permanent Kotak Neo credentials are incomplete.",
                "agent_instruction": (
                    "Ask the user on WhatsApp for the following permanent details (saved once permanently):\n"
                    + "\n".join(missing_prompts)
                    + "\nThen call `save_permanent_credentials`."
                ),
            }

        session = self.store.load_session()
        today_ist = get_current_trading_date_ist(now)

        if session is None or not session.is_valid_for_today(now):
            expired_date = session.trading_date if session else None
            return {
                "status": "TOTP_REQUIRED",
                "ready": False,
                "trading_date_ist": today_ist,
                "previous_session_date": expired_date,
                "saved_credentials": creds.to_safe_dict(),
                "message": f"Daily Kotak Neo session for {today_ist} is not active (TOTP required).",
                "agent_instruction": (
                    "Ask the user on WhatsApp: 'Please share your current 6-digit Kotak TOTP code "
                    "from your Authenticator app to start today's session.' "
                    "Once they reply with the 6-digit code, call `login_with_daily_totp(totp=...)`."
                ),
            }

        return {
            "status": "AUTHENTICATED",
            "ready": True,
            "trading_date_ist": session.trading_date,
            "authenticated_at_ist": session.authenticated_at_ist,
            "ucc": session.ucc,
            "greeting_name": session.greeting_name,
            "data_center": session.data_center,
            "base_url": session.base_url,
            "saved_credentials": creds.to_safe_dict(),
            "message": f"Authenticated for trading day {session.trading_date} as {session.greeting_name or session.ucc}.",
        }

    def save_permanent_credentials(
        self,
        consumer_key: str | None = None,
        mobile_number: str | None = None,
        ucc: str | None = None,
        mpin: str | None = None,
        environment: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """
        Save permanent credentials (`consumer_key`, `mobile_number`, `ucc`, `mpin`) to disk
        and return the updated authentication status.
        """
        creds = self.store.save_credentials(
            consumer_key=consumer_key,
            mobile_number=mobile_number,
            ucc=ucc,
            mpin=mpin,
            environment=environment,
        )
        status = self.get_auth_status(now=now)
        status["updated_credentials"] = creds.to_safe_dict()
        return status

    def login_with_daily_totp(
        self,
        totp: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """
        Perform the 2-step Kotak Neo login (`totp_login` -> `totp_validate`) using the
        permanently stored credentials and the user's fresh 6-digit TOTP code.
        Saves the resulting daily session tokens to `data/session.json`.
        """
        creds = self.store.load_credentials()
        missing = creds.missing_fields()
        if missing:
            return self.get_auth_status(now=now)

        cleaned_totp = re.sub(r"\s+", "", str(totp).strip())
        if not re.fullmatch(r"\d{6}", cleaned_totp):
            return {
                "status": "INVALID_TOTP_FORMAT",
                "ready": False,
                "message": f"TOTP must be a 6-digit numeric code, received: {totp!r}",
                "agent_instruction": "Ask the user for a valid 6-digit numeric TOTP code from their Authenticator app.",
            }

        client = self.client_factory(
            consumer_key=creds.consumer_key,
            environment=creds.environment,
        )

        # Step 1: totp_login (generates view_token and sid)
        try:
            login_resp = client.totp_login(
                mobile_number=creds.mobile_number,
                ucc=creds.ucc,
                totp=cleaned_totp,
            )
        except Exception as exc:
            return {
                "status": "AUTH_ERROR",
                "step": "totp_login",
                "ready": False,
                "error": str(exc),
                "agent_instruction": (
                    "TOTP login failed. Check if the 6-digit TOTP code expired (codes rotate every 30 seconds) "
                    "and ask the user for a fresh 6-digit TOTP."
                ),
            }

        login_data = login_resp.get("data") if isinstance(login_resp, dict) else None
        if not isinstance(login_data, dict) or not login_data.get("token"):
            return {
                "status": "AUTH_ERROR",
                "step": "totp_login",
                "ready": False,
                "response": login_resp,
                "agent_instruction": (
                    "Kotak rejected the TOTP login. If the TOTP code expired, ask the user for a fresh "
                    "6-digit TOTP code. If mobile_number, UCC, or consumer_key is wrong, update it with "
                    "`save_permanent_credentials`."
                ),
            }

        # Step 2: totp_validate (validates MPIN and generates edit_token, edit_sid, baseUrl, dataCenter)
        try:
            validate_resp = client.totp_validate(mpin=creds.mpin)
        except Exception as exc:
            return {
                "status": "AUTH_ERROR",
                "step": "totp_validate",
                "ready": False,
                "error": str(exc),
                "agent_instruction": "MPIN validation failed. Verify the permanent MPIN with the user.",
            }

        val_data = validate_resp.get("data") if isinstance(validate_resp, dict) else None
        if not isinstance(val_data, dict) or not val_data.get("token"):
            return {
                "status": "AUTH_ERROR",
                "step": "totp_validate",
                "ready": False,
                "response": validate_resp,
                "agent_instruction": (
                    "Kotak rejected the MPIN during `totp_validate`. Ask the user to confirm their 6-digit "
                    "trading MPIN and update it via `save_permanent_credentials(mpin=...)`."
                ),
            }

        now_ist = (now or datetime.now(IST)).astimezone(IST)
        trading_date = get_current_trading_date_ist(now_ist)
        cfg = client.configuration

        session = DailySession(
            trading_date=trading_date,
            authenticated_at_ist=now_ist.isoformat(timespec="seconds"),
            ucc=str(val_data.get("ucc") or cfg.ucc or creds.ucc),
            greeting_name=str(val_data.get("greetingName") or login_data.get("greetingName") or ""),
            view_token=str(cfg.view_token or login_data.get("token") or ""),
            sid=str(cfg.sid or login_data.get("sid") or ""),
            edit_token=str(cfg.edit_token or val_data.get("token") or ""),
            edit_sid=str(cfg.edit_sid or val_data.get("sid") or ""),
            edit_rid=str(cfg.edit_rid or val_data.get("rid") or ""),
            base_url=str(cfg.base_url or val_data.get("baseUrl") or ""),
            data_center=str(cfg.data_center or val_data.get("dataCenter") or ""),
            feed_url=getattr(cfg, "feed_url", None) or val_data.get("feedUrl"),
            rt_url=getattr(cfg, "rt_url", None) or val_data.get("rtUrl"),
            extra={
                "sfeed_websocket_url": getattr(cfg, "sfeed_websocket_url", None),
                "order_feed_url": getattr(cfg, "order_feed_url", None),
                "kType": val_data.get("kType", "Trade"),
                "clientType": val_data.get("clientType", ""),
            },
        )
        self.store.save_session(session)

        return {
            "status": "AUTHENTICATED",
            "ready": True,
            "trading_date_ist": session.trading_date,
            "authenticated_at_ist": session.authenticated_at_ist,
            "ucc": session.ucc,
            "greeting_name": session.greeting_name,
            "data_center": session.data_center,
            "base_url": session.base_url,
            "message": (
                f"Daily TOTP login successful! Session active for {session.trading_date} "
                f"(User: {session.greeting_name or session.ucc})."
            ),
        }

    def require_authenticated_client(
        self,
        now: datetime | None = None,
    ) -> tuple[NeoAPI, PermanentCredentials, DailySession]:
        """
        Return a ready-to-use `NeoAPI` instance hydrated with today's session tokens.
        Raises `SetupRequiredError` or `TotpRequiredError` if not authenticated for today.
        """
        status = self.get_auth_status(now=now)
        if status["status"] == "SETUP_REQUIRED":
            raise SetupRequiredError(status["status"], status["message"], status)
        if status["status"] == "TOTP_REQUIRED":
            raise TotpRequiredError(status["status"], status["message"], status)

        creds = self.store.load_credentials()
        session = self.store.load_session()
        assert session is not None

        client = self.client_factory(
            consumer_key=creds.consumer_key,
            environment=creds.environment,
        )
        cfg = client.configuration
        cfg.consumer_key = creds.consumer_key
        cfg.view_token = session.view_token
        cfg.sid = session.sid
        cfg.edit_token = session.edit_token
        cfg.edit_sid = session.edit_sid
        cfg.edit_rid = session.edit_rid
        cfg.base_url = session.base_url or cfg.base_url
        cfg.data_center = session.data_center
        cfg.ucc = session.ucc or creds.ucc
        cfg.feed_url = session.feed_url
        cfg.rt_url = session.rt_url
        if session.extra:
            cfg.sfeed_websocket_url = session.extra.get("sfeed_websocket_url")
            cfg.order_feed_url = session.extra.get("order_feed_url")

        return client, creds, session

    def invalidate_daily_session(self) -> None:
        """
        Clear cached daily session (used when the API returns 401/403 mid-day).
        """
        self.store.clear_session()
