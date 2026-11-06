"""
Unit tests for Permanent Credential storage and Daily TOTP Session Lifecycle.
"""

from __future__ import annotations

import stat
from datetime import datetime
from pathlib import Path
from typing import Any

from kotak_option_chain.auth import AuthManager
from kotak_option_chain.config import (
    IST,
    ConfigStore,
    get_current_trading_date_ist,
    normalize_mobile_number,
)


class FakeNeoUtility:
    def __init__(self, consumer_key: str | None = None, host: str = "prod") -> None:
        self.consumer_key = consumer_key
        self.host = host
        self.view_token: str | None = None
        self.sid: str | None = None
        self.edit_token: str | None = None
        self.edit_sid: str | None = None
        self.edit_rid: str | None = None
        self.base_url: str | None = None
        self.data_center: str | None = None
        self.ucc: str | None = None
        self.feed_url: str | None = None
        self.rt_url: str | None = None
        self.sfeed_websocket_url: str | None = None
        self.order_feed_url: str | None = None


class FakeNeoAPI:
    def __init__(self, consumer_key: str | None = None, environment: str = "prod") -> None:
        self.configuration = FakeNeoUtility(consumer_key=consumer_key, host=environment)

    def totp_login(self, mobile_number: str, ucc: str, totp: str) -> dict[str, Any]:
        if totp == "000000":
            return {"error": [{"code": "400", "message": "Invalid TOTP"}]}
        self.configuration.view_token = "view-jwt-token"
        self.configuration.sid = "view-sid-123"
        self.configuration.ucc = ucc
        return {
            "data": {
                "token": "view-jwt-token",
                "sid": "view-sid-123",
                "ucc": ucc,
                "greetingName": "KARTHIK",
                "status": "success",
            }
        }

    def totp_validate(self, mpin: str) -> dict[str, Any]:
        self.configuration.edit_token = "trade-jwt-token"
        self.configuration.edit_sid = "trade-sid-456"
        self.configuration.edit_rid = "trade-rid-789"
        self.configuration.base_url = "https://e43.kotaksecurities.com"
        self.configuration.data_center = "E43"
        return {
            "data": {
                "token": "trade-jwt-token",
                "sid": "trade-sid-456",
                "rid": "trade-rid-789",
                "baseUrl": "https://e43.kotaksecurities.com",
                "dataCenter": "E43",
                "ucc": self.configuration.ucc,
                "greetingName": "KARTHIK",
                "kType": "Trade",
                "status": "success",
            }
        }


def test_mobile_number_normalization() -> None:
    assert normalize_mobile_number("9876543210") == "+919876543210"
    assert normalize_mobile_number("09876543210") == "+919876543210"
    assert normalize_mobile_number("919876543210") == "+919876543210"
    assert normalize_mobile_number("+91 98765-43210") == "+919876543210"


def test_permanent_credentials_and_daily_totp_rollover(tmp_path: Path, monkeypatch: Any) -> None:
    for env_var in ("NEO_CONSUMER_KEY", "NEO_MOBILE_NUMBER", "NEO_UCC", "NEO_MPIN"):
        monkeypatch.delenv(env_var, raising=False)

    store = ConfigStore(data_dir=tmp_path)
    auth = AuthManager(store=store, client_factory=FakeNeoAPI)

    day1_morning = datetime(2026, 11, 6, 9, 15, 0, tzinfo=IST)

    # 1. Initially, permanent credentials are missing -> SETUP_REQUIRED
    status0 = auth.get_auth_status(now=day1_morning)
    assert status0["status"] == "SETUP_REQUIRED"
    assert set(status0["missing_fields"]) == {"consumer_key", "mobile_number", "ucc", "mpin"}

    # 2. Save permanent credentials once -> transitions to TOTP_REQUIRED
    status1 = auth.save_permanent_credentials(
        consumer_key="ck-secret-token",
        mobile_number="9876543210",
        ucc="KOTAK123",
        mpin="112233",
        now=day1_morning,
    )
    assert status1["status"] == "TOTP_REQUIRED"
    assert store.credentials_path.exists()
    mode = stat.S_IMODE(store.credentials_path.stat().st_mode)
    assert mode == 0o600
    assert store.load_credentials().mobile_number == "+919876543210"

    # 3. Login with daily 6-digit TOTP -> transitions to AUTHENTICATED
    login_res = auth.login_with_daily_totp("654321", now=day1_morning)
    assert login_res["status"] == "AUTHENTICATED"
    assert login_res["trading_date_ist"] == "2026-11-06"
    assert login_res["greeting_name"] == "KARTHIK"

    # 4. Same day afternoon (14:30 IST) -> still AUTHENTICATED without needing TOTP again
    day1_afternoon = datetime(2026, 11, 6, 14, 30, 0, tzinfo=IST)
    status_afternoon = auth.get_auth_status(now=day1_afternoon)
    assert status_afternoon["status"] == "AUTHENTICATED"
    client, _, session = auth.require_authenticated_client(now=day1_afternoon)
    assert client.configuration.edit_token == "trade-jwt-token"
    assert client.configuration.base_url == "https://e43.kotaksecurities.com"
    assert session.ucc == "KOTAK123"

    # 5. Next morning before 06:00 AM IST (05:30 AM IST) -> still belongs to 2026-11-06 window
    day2_pre_reset = datetime(2026, 11, 7, 5, 30, 0, tzinfo=IST)
    assert get_current_trading_date_ist(day2_pre_reset) == "2026-11-06"
    assert auth.get_auth_status(now=day2_pre_reset)["status"] == "AUTHENTICATED"

    # 6. Next morning after 06:00 AM IST (09:00 AM IST) -> session expired! Must ask for TOTP again,
    #    while permanent credentials remain saved!
    day2_morning = datetime(2026, 11, 7, 9, 0, 0, tzinfo=IST)
    status_day2 = auth.get_auth_status(now=day2_morning)
    assert status_day2["status"] == "TOTP_REQUIRED"
    assert status_day2["previous_session_date"] == "2026-11-06"
    assert status_day2["saved_credentials"]["mobile_number"] == "+919876543210"
    assert status_day2["saved_credentials"]["ucc"] == "KOTAK123"
