"""Server-trusted request identity boundary; no client-supplied production identity."""

from dataclasses import dataclass
from pathlib import Path
import tempfile
from typing import TypedDict

from fastapi import HTTPException, status
from sqlalchemy.engine import make_url


ROLE_ORDER = {"viewer": 0, "operator": 1, "analyst": 2, "reviewer": 3, "company_admin": 4, "super_admin": 5}


@dataclass(frozen=True)
class VerifiedPrincipal:
    """Only a server-side authentication dependency may construct this value."""

    user_id: str
    active_company_id: str


class TrustedTenantContext(TypedDict):
    company_id: str
    user_id: str
    role: str
    auth_mode: str


async def get_verified_principal() -> VerifiedPrincipal | None:
    """Authentication integration point. No verifier installed means fail closed."""
    return None


def resolve_tenant_context(
    *, app_env: str, demo_identity_enabled: bool,
    principal: VerifiedPrincipal | None,
    verified_role: str | None = None,
    demo_company_id: str, demo_user_id: str, demo_role: str,
) -> TrustedTenantContext:
    if principal is not None:
        if not isinstance(principal, VerifiedPrincipal) or not principal.user_id or not principal.active_company_id or verified_role not in ROLE_ORDER:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "TENANT_FORBIDDEN"})
        return {"company_id": principal.active_company_id, "user_id": principal.user_id,
                "role": verified_role, "auth_mode": "verified"}
    if app_env in {"development", "test"} and demo_identity_enabled:
        if not demo_company_id or not demo_user_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "INVALID_DEMO_IDENTITY"})
        if demo_role not in ROLE_ORDER:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "FORBIDDEN"})
        return {"company_id": demo_company_id, "user_id": demo_user_id,
                "role": demo_role, "auth_mode": "demo"}
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "AUTHENTICATION_REQUIRED"})


def require_local_demo_database(database_url: str, *, workspace: Path, test_mode: bool = False) -> None:
    """Refuse a Demo identity against a remote or non-project database."""
    try:
        url = make_url(database_url)
    except Exception as exc:
        raise RuntimeError("Demo database URL is invalid") from exc
    if url.drivername != "sqlite+aiosqlite":
        raise RuntimeError("Demo identity requires an isolated local SQLite database")
    if url.database == ":memory:":
        return
    if not url.database:
        raise RuntimeError("Demo database path is missing")
    actual = Path(url.database).resolve()
    permitted = ((Path(tempfile.gettempdir()) / "ozon-agent-tests") if test_mode else (workspace / "data")).resolve()
    if not actual.is_relative_to(permitted):
        raise RuntimeError("Demo identity requires a database under this backend's isolated data directory")
