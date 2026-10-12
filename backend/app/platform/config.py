"""Application settings, loaded from environment variables (prefix PM_)."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PM_", env_file=None)

    database_url: str = "postgresql+psycopg://pm:pm@db:5432/pm"
    # Fernet key for encrypting stored secrets (API tokens). Generated on first deploy.
    master_key: str = ""
    session_ttl_hours: int = 24 * 7
    session_idle_hours: int = 24
    # "auto": Secure cookie when the request arrived over HTTPS (X-Forwarded-Proto from the proxy).
    cookie_secure: str = "auto"
    rules_dir: str = "/srv/rules"
    imports_dir: str = "/srv/imports"
    # Outbound HTTP allowlist (NFR-02). Hostnames only; exact match or subdomain match.
    outbound_allowlist: list[str] = Field(
        default_factory=lambda: [
            "data-api.ecb.europa.eu",
            "query1.finance.yahoo.com",
            "query2.finance.yahoo.com",
            "lt.morningstar.com",
            "api.ynab.com",
            "api.kraken.com",
            "api.binance.com",
            "api.coingecko.com",
            "pxdata.stat.fi",
            "mba.tuck.dartmouth.edu",
        ]
    )
    # LAN services reached through the egress proxy over plain HTTP (Gotify, Paperless, local LLM).
    lan_allowlist: list[str] = Field(default_factory=list)
    # Local LLM default (an OpenAI-compatible gateway on the LAN); a connection saved in Settings overrides these.
    llm_url: str = ""
    llm_model: str = "local-general"
    llm_vision_model: str = "local-vision"
    llm_max_tokens: int = 4096  # answer budget per call; the gateway grants up to this for portfolio-manager
    login_rate_per_minute: int = 5
    # "login": every request needs a session. "network": requests whose whole path (the connecting address and every
    # X-Forwarded-For hop) is inside trusted_networks are signed in as the owner without a password; anything else
    # still has to sign in. Only safe when the app is reachable from the LAN/VPN only (it is: see deploy/firewall).
    auth_mode: str = "login"
    trusted_networks: list[str] = Field(
        default_factory=lambda: ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128", "fc00::/7"]
    )
    timezone: str = "Europe/Helsinki"


@lru_cache
def get_settings() -> Settings:
    return Settings()
