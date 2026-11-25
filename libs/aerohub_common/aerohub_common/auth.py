"""Автентифікація та рольова авторизація.

У робочій архітектурі токени видає Keycloak (OIDC). У прототипі токени видає
службовий ендпоінт /api/v1/auth/token з демонстраційними користувачами
(:data:`DEMO_USERS`); формат токена (JWT з полями sub, roles, exp) той самий,
тому перехід на Keycloak зводиться до зміни перевірки підпису.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Iterable

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from .config import get_settings
from .events import utcnow


class Roles:
    """Ролі користувачів (підрозділ 4 звіту, таблиця 4.1)."""

    AOC = "aoc_dispatcher"
    PLANNER = "planner"
    AGENT = "agent"
    HANDLER = "handler"
    BILLING = "billing"
    ADMIN = "admin"


# Демонстраційні користувачі: логін -> (пароль, ролі). Лише для прототипу.
DEMO_USERS: dict[str, tuple[str, list[str]]] = {
    "dispatcher": ("dispatcher", [Roles.AOC]),
    "planner": ("planner", [Roles.PLANNER]),
    "agent": ("agent", [Roles.AGENT]),
    "handler": ("handler", [Roles.HANDLER]),
    "admin": ("admin", [Roles.ADMIN, Roles.PLANNER, Roles.AOC]),
}


class Principal(BaseModel):
    username: str
    roles: list[str]

    def has_any(self, roles: Iterable[str]) -> bool:
        return Roles.ADMIN in self.roles or any(r in self.roles for r in roles)


def issue_token(username: str, roles: list[str]) -> str:
    s = get_settings()
    now = utcnow()
    claims = {"sub": username, "roles": roles, "iat": now, "exp": now + timedelta(minutes=s.jwt_ttl_minutes)}
    return jwt.encode(claims, s.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> Principal:
    try:
        claims = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Недійсний або прострочений токен") from exc
    return Principal(username=claims["sub"], roles=claims.get("roles", []))


_bearer = HTTPBearer(auto_error=False)


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Principal:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Потрібна автентифікація")
    return decode_token(creds.credentials)


def require_roles(*roles: str):
    """Залежність FastAPI: пропускає лише користувачів з однією з ролей."""

    def checker(user: Principal = Depends(current_user)) -> Principal:
        if not user.has_any(roles):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Недостатньо прав для операції")
        return user

    return checker
