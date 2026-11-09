import hashlib
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Iterable, Optional

DEFAULT_APP_VERSION = "6.194.0"
DEFAULT_USER_AGENT_PREFIX = "OKEx-APPEX10754"
DEFAULT_DEVICE_MODEL = "sdk_gphone64_x86_64"
DEFAULT_ANDROID_VERSION = "12"
DEFAULT_LOCALE = "vi-VN"
DEFAULT_X_CDN = "https://static.okx.com"
DEFAULT_RISK_PARAMS = "fp-status=-2"
DEFAULT_APP_WEB_MODE = "pro"
DEFAULT_TOKEN_TYPE = 3
DEFAULT_TMX_SESSION_STATUS = "3"


def headers_order_string_hash(header_names: Iterable[str]) -> str:
    """
    MD5 of header names sorted case-insensitively and joined with no separator.

    App: C5840Jei.EZpvd -> HashUtils.copydefault(joinToString(sorted names, ""))
    computed from chain.request().headers().names() inside DeviceInfoInterceptor,
    i.e. before User-Agent/devId/platform/real-app-version/x-utc/lua-version are added.
    """
    joined = "".join(sorted(header_names, key=str.lower))
    return hashlib.md5(joined.encode("utf-8")).hexdigest()


def current_utc_offset() -> str:
    offset = datetime.now().astimezone().utcoffset()
    total_minutes = int(offset.total_seconds() // 60) if offset else 0
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    return f"{sign}{total_minutes // 60:02d}:{total_minutes % 60:02d}"


@dataclass(frozen=True)
class ClientProfile:
    app_version: str = DEFAULT_APP_VERSION
    real_app_version: str = DEFAULT_APP_VERSION
    user_agent_prefix: str = DEFAULT_USER_AGENT_PREFIX
    device_model: str = DEFAULT_DEVICE_MODEL
    android_version: str = DEFAULT_ANDROID_VERSION
    locale: str = DEFAULT_LOCALE
    app_web_mode: str = DEFAULT_APP_WEB_MODE
    lua_version: Optional[str] = None
    x_cdn: str = DEFAULT_X_CDN
    risk_params: str = DEFAULT_RISK_PARAMS
    fingerprint_id: Optional[str] = None
    token_type: int = DEFAULT_TOKEN_TYPE
    tmx_session_status: str = DEFAULT_TMX_SESSION_STATUS

    @classmethod
    def from_account(cls, account_data: dict) -> "ClientProfile":
        overrides = {}
        if account_data.get("app_version"):
            overrides["app_version"] = str(account_data["app_version"])
        if account_data.get("real_app_version"):
            overrides["real_app_version"] = str(account_data["real_app_version"])
        if account_data.get("user_agent_prefix"):
            overrides["user_agent_prefix"] = str(account_data["user_agent_prefix"])
        if account_data.get("device_model"):
            overrides["device_model"] = str(account_data["device_model"])
        if account_data.get("android_version"):
            overrides["android_version"] = str(account_data["android_version"])
        if account_data.get("locale"):
            overrides["locale"] = str(account_data["locale"])
        if account_data.get("app_web_mode"):
            overrides["app_web_mode"] = str(account_data["app_web_mode"])
        if account_data.get("lua_version"):
            overrides["lua_version"] = str(account_data["lua_version"])
        if account_data.get("x_cdn"):
            overrides["x_cdn"] = str(account_data["x_cdn"])
        if account_data.get("risk_params"):
            overrides["risk_params"] = str(account_data["risk_params"])
        if account_data.get("fingerprint_id"):
            overrides["fingerprint_id"] = str(account_data["fingerprint_id"])
        if account_data.get("token_type") is not None:
            overrides["token_type"] = int(account_data["token_type"])
        if account_data.get("tmx_session_status") is not None:
            overrides["tmx_session_status"] = str(account_data["tmx_session_status"])
        return replace(cls(), **overrides)

    def user_agent(self) -> str:
        return (
            f"{self.user_agent_prefix}/{self.app_version} "
            f"({self.device_model}; U; Android {self.android_version}; {self.locale};) "
            f"locale={self.locale}"
        )
