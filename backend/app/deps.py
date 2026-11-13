"""Request context: authentication (JWT or API key), tenant resolution, RBAC guards."""
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m
from .database import get_db
from .rbac import PLATFORM_PERMISSIONS, permissions_for
from .security import decode_token, hash_api_key


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict | None = None):
        self.status, self.code, self.message, self.headers = status, code, message, headers or {}


@dataclass
class Ctx:
    db: Session
    request_id: str
    ip: str
    user: m.User | None = None
    org: m.Organization | None = None
    workspace: m.Workspace | None = None
    role: str = ""
    perms: set = field(default_factory=set)
    actor_type: str = "user"
    actor_id: str = ""
    api_key: m.ApiKey | None = None

    @property
    def org_id(self) -> str:
        return self.org.id if self.org else ""

    @property
    def ws_id(self) -> str | None:
        return self.workspace.id if self.workspace else None

    @property
    def is_platform_admin(self) -> bool:
        return bool(self.user and self.user.is_platform_admin)

    def has(self, perm: str) -> bool:
        if perm.startswith("platform."):
            return self.is_platform_admin
        return perm in self.perms


# ---- simple in-memory rate limiter (per API key / user) -- PRD 224 ----
_rl_lock = threading.Lock()
_rl_hits: dict[str, deque] = defaultdict(deque)


def rate_limit(bucket: str, limit_per_minute: int):
    now = time.time()
    with _rl_lock:
        q = _rl_hits[bucket]
        while q and q[0] < now - 60:
            q.popleft()
        if len(q) >= limit_per_minute:
            retry = int(60 - (now - q[0])) + 1
            raise APIError(429, "RATE_LIMITED", "Too many requests.", {"Retry-After": str(retry)})
        q.append(now)


def _resolve_workspace(db: Session, org_id: str, ws_header: str | None) -> m.Workspace | None:
    if ws_header:
        ws = db.get(m.Workspace, ws_header)
        if not ws or ws.organization_id != org_id:
            raise APIError(403, "WORKSPACE_FORBIDDEN", "Workspace not accessible in this organization.")
        return ws
    return db.scalars(select(m.Workspace).where(m.Workspace.organization_id == org_id)
                      .order_by(m.Workspace.created_at)).first()


def get_ctx(request: Request, db: Session = Depends(get_db),
            authorization: str | None = Header(default=None),
            x_api_key: str | None = Header(default=None),
            x_workspace_id: str | None = Header(default=None),
            x_org_id: str | None = Header(default=None)) -> Ctx:
    ctx = Ctx(db=db, request_id=getattr(request.state, "request_id", ""),
              ip=request.client.host if request.client else "")
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    raw_key = x_api_key or (token if token and token.startswith("gtm_live_") else None)

    if raw_key:
        key = db.scalars(select(m.ApiKey).where(m.ApiKey.key_hash == hash_api_key(raw_key))).first()
        if not key or key.revoked:
            raise APIError(401, "INVALID_API_KEY", "API key is invalid or revoked.")
        org = db.get(m.Organization, key.organization_id)
        if not org or org.status != "active":
            raise APIError(403, "ORG_SUSPENDED", "Organization is not active.")
        plan = db.scalars(select(m.Plan).where(m.Plan.code == org.plan_code)).first()
        rate_limit(f"key:{key.id}", int((plan.limits or {}).get("api_rpm", 60)) if plan else 60)
        key.last_used_at = m.utcnow()
        db.commit()
        ctx.api_key, ctx.org, ctx.role = key, org, key.role
        ctx.workspace = db.get(m.Workspace, key.workspace_id) if key.workspace_id else _resolve_workspace(db, org.id, None)
        ctx.perms = permissions_for(key.role)
        ctx.actor_type, ctx.actor_id = "api_key", key.id
        return ctx

    if not token:
        raise APIError(401, "UNAUTHENTICATED", "Missing credentials.")
    payload = decode_token(token)
    if not payload:
        raise APIError(401, "INVALID_TOKEN", "Session expired or invalid. Please log in again.")
    user = db.get(m.User, payload["sub"])
    if not user or not user.is_active:
        raise APIError(401, "USER_INACTIVE", "User is not active.")
    ctx.user, ctx.actor_id = user, user.id
    rate_limit(f"user:{user.id}", 600)
    # One-time passwords must be replaced before anything else can be done (enforced server-side).
    if user.must_change_password and request.url.path not in ("/app/me", "/app/auth/password"):
        raise APIError(403, "PASSWORD_CHANGE_REQUIRED", "Please set a new password before continuing.")

    org_id = x_org_id or payload.get("org")
    if org_id:
        mem = db.scalars(select(m.Membership).where(m.Membership.user_id == user.id,
                                                    m.Membership.organization_id == org_id)).first()
        if mem:
            org = db.get(m.Organization, org_id)
            if org and org.status != "active" and not user.is_platform_admin:
                raise APIError(403, "ORG_SUSPENDED", "Organization is suspended. Contact support.")
            ctx.org, ctx.role = org, mem.role
            ctx.perms = permissions_for(mem.role)
            ctx.workspace = _resolve_workspace(db, org_id, x_workspace_id)
    if user.is_platform_admin:
        ctx.perms = set(ctx.perms) | PLATFORM_PERMISSIONS
    return ctx


def require(*perms: str):
    """Dependency factory: all listed permissions are required."""
    def dep(ctx: Ctx = Depends(get_ctx)) -> Ctx:
        for p in perms:
            if not p.startswith("platform.") and not ctx.org:
                raise APIError(403, "NO_TENANT", "No organization context for this account.")
            if not ctx.has(p):
                raise APIError(403, "FORBIDDEN", f"Your role does not allow: {p}")
        return ctx
    return dep


def tenant_get(ctx: Ctx, model, obj_id: str):
    """Fetch a tenant-owned row, enforcing organization isolation (PRD 230, 239)."""
    obj = ctx.db.get(model, obj_id)
    if not obj or getattr(obj, "organization_id", None) != ctx.org_id:
        raise APIError(404, "NOT_FOUND", f"{model.__name__} not found.")
    return obj
