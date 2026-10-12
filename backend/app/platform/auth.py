"""Single-admin authentication (NFR-08, ADR-13).

Sessions are server-side rows; the cookie holds a random token whose SHA-256 is the key.
CSRF: cookies are SameSite=Strict and every mutating request must carry the custom header
`X-PM-Request: 1`, which a cross-site form cannot set without a CORS preflight.
"""

import time
from collections import defaultdict, deque
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.platform.audit import audit
from app.platform.config import get_settings
from app.platform.db import get_db, utcnow
from app.platform.models import AppUser, AuthSession
from app.platform.security import (
    decrypt_secret,
    encrypt_secret,
    hash_password,
    new_token,
    new_totp_secret,
    token_hash,
    totp_uri,
    verify_password,
    verify_totp,
)

COOKIE = "pm_session"
CSRF_HEADER = "x-pm-request"
router = APIRouter(prefix="/api/auth", tags=["auth"])

_attempts: dict[str, deque[float]] = defaultdict(deque)


def _rate_limited(ip: str) -> bool:
    window = _attempts[ip]
    now = time.monotonic()
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= get_settings().login_rate_per_minute:
        return True
    window.append(now)
    return False


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _is_https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto.split(",")[0].strip() == "https"


def _set_cookie(response: Response, request: Request, token: str) -> None:
    mode = get_settings().cookie_secure
    secure = _is_https(request) if mode == "auto" else mode == "true"
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        samesite="strict",
        secure=secure,
        max_age=get_settings().session_ttl_hours * 3600,
        path="/",
    )


class UserOut(BaseModel):
    id: int
    username: str
    totp_enabled: bool


def network_trusted(request: Request) -> bool:
    """True when the connecting address and every forwarded hop are inside the trusted networks. A public address
    anywhere in the chain (e.g. an internet client behind a proxy) means the request must sign in."""
    import ipaddress

    s = get_settings()
    if s.auth_mode != "network":
        return False
    hops = [request.client.host if request.client else ""]
    hops += [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    nets = [ipaddress.ip_network(n, strict=False) for n in s.trusted_networks]
    for h in hops:
        try:
            ip = ipaddress.ip_address(h)
        except ValueError:
            return False
        if not any(ip in n for n in nets):
            return False
    return True


def owner(db: Session) -> AppUser:
    """The single admin; created on first use with an unusable random password (set one later if ever needed)."""
    import secrets

    u = db.scalar(select(AppUser).order_by(AppUser.id).limit(1))
    if u is None:
        u = AppUser(username="owner", password_hash=hash_password(secrets.token_urlsafe(32)))
        db.add(u)
        db.flush()
    return u


def current_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> AppUser:
    token = request.cookies.get(COOKIE)
    if not token and network_trusted(request):
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != "1":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing request header")
        return owner(db)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in to continue")
    sess = db.get(AuthSession, token_hash(token))
    now = utcnow()
    idle = timedelta(hours=get_settings().session_idle_hours)
    if sess is None or sess.expires_at < now or sess.last_seen_at + idle < now:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired, sign in again")
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != "1":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing request header")
    if (now - sess.last_seen_at) > timedelta(minutes=5):
        sess.last_seen_at = now
    user = db.get(AppUser, sess.user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in to continue")
    return user


CurrentUser = Annotated[AppUser, Depends(current_user)]
Db = Annotated[Session, Depends(get_db)]


class SetupIn(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=12, max_length=256)


class LoginIn(BaseModel):
    username: str
    password: str
    totp: str | None = None


@router.get("/status")
def auth_status(request: Request, db: Db) -> dict[str, object]:
    """Tells the UI whether to show first-run setup, the sign-in form, or the app."""
    if network_trusted(request):
        u = owner(db)
        return {
            "setup_required": False,
            "signed_in": True,
            "open": True,
            "user": UserOut(id=u.id, username=u.username, totp_enabled=u.totp_enabled),
        }
    has_user = db.scalar(select(func.count()).select_from(AppUser)) or 0
    signed_in = False
    user: UserOut | None = None
    token = request.cookies.get(COOKIE)
    if token:
        sess = db.get(AuthSession, token_hash(token))
        if sess and sess.expires_at > utcnow():
            su = db.get(AppUser, sess.user_id)
            if su:
                signed_in = True
                user = UserOut(id=su.id, username=su.username, totp_enabled=su.totp_enabled)
    return {"setup_required": has_user == 0, "signed_in": signed_in, "open": False, "user": user}


def _start_session(db: Session, request: Request, response: Response, user: AppUser) -> None:
    token = new_token()
    now = utcnow()
    db.add(
        AuthSession(
            token_hash=token_hash(token),
            user_id=user.id,
            expires_at=now + timedelta(hours=get_settings().session_ttl_hours),
            last_seen_at=now,
            user_agent=(request.headers.get("user-agent") or "")[:255],
            ip=_client_ip(request),
        )
    )
    _set_cookie(response, request, token)


@router.post("/setup", status_code=201)
def setup(body: SetupIn, request: Request, response: Response, db: Db) -> UserOut:
    if request.headers.get(CSRF_HEADER) != "1":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing request header")
    if (db.scalar(select(func.count()).select_from(AppUser)) or 0) > 0:
        raise HTTPException(status.HTTP_409_CONFLICT, "Setup is already complete")
    user = AppUser(username=body.username, password_hash=hash_password(body.password))
    db.add(user)
    db.flush()
    audit(db, "system", "user.create", "app_user", user.id, after={"username": user.username})
    _start_session(db, request, response, user)
    return UserOut(id=user.id, username=user.username, totp_enabled=False)


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, db: Db) -> UserOut:
    if request.headers.get(CSRF_HEADER) != "1":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing request header")
    if _rate_limited(_client_ip(request)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts, wait a minute")
    user = db.scalar(select(AppUser).where(AppUser.username == body.username))
    if user is None or not verify_password(user.password_hash, body.password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong username or password")
    if user.totp_enabled:
        if not body.totp:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Enter the code from your authenticator app")
        if not user.totp_secret_enc or not verify_totp(decrypt_secret(user.totp_secret_enc), body.totp):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "That code is not valid")
    _start_session(db, request, response, user)
    return UserOut(id=user.id, username=user.username, totp_enabled=user.totp_enabled)


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, db: Db) -> None:
    token = request.cookies.get(COOKIE)
    if token:
        db.execute(delete(AuthSession).where(AuthSession.token_hash == token_hash(token)))
    response.delete_cookie(COOKIE, path="/")


@router.get("/me")
def me(user: CurrentUser) -> UserOut:
    return UserOut(id=user.id, username=user.username, totp_enabled=user.totp_enabled)


class PasswordIn(BaseModel):
    current: str
    new: str = Field(min_length=12, max_length=256)


@router.post("/password", status_code=204)
def change_password(body: PasswordIn, user: CurrentUser, db: Db) -> None:
    if not verify_password(user.password_hash, body.current):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is wrong")
    user.password_hash = hash_password(body.new)
    audit(db, user.username, "user.password", "app_user", user.id)


@router.post("/totp/setup")
def totp_setup(user: CurrentUser) -> dict[str, str]:
    """Returns a new secret; it is stored only after /totp/enable verifies a code."""
    secret = new_totp_secret()
    return {"secret": secret, "uri": totp_uri(secret, user.username)}


class TotpEnableIn(BaseModel):
    secret: str
    code: str


@router.post("/totp/enable", status_code=204)
def totp_enable(body: TotpEnableIn, user: CurrentUser, db: Db) -> None:
    if not verify_totp(body.secret, body.code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That code is not valid")
    user.totp_secret_enc = encrypt_secret(body.secret)
    user.totp_enabled = True
    audit(db, user.username, "user.totp_enable", "app_user", user.id)
