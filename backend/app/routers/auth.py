import re

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models as m
from ..config import DEMO_DATA
from ..database import get_db
from ..deps import APIError, Ctx, get_ctx, rate_limit
from ..rbac import P, ROLE_LABELS, ROLE_PERMISSIONS, ROLES
from ..security import create_token, hash_password, verify_password
from ..services import core

router = APIRouter(prefix="/app", tags=["auth"])


class LoginIn(BaseModel):
    email: str
    password: str


class SignupIn(BaseModel):
    name: str = Field(min_length=2)
    email: EmailStr
    password: str = Field(min_length=8)
    company_name: str = Field(min_length=2)
    website: str = ""
    industry: str = ""
    country: str = "India"
    timezone: str = "Asia/Kolkata"
    objective: str = "Book qualified meetings"


def _membership(db, user_id):
    return db.scalars(select(m.Membership).where(m.Membership.user_id == user_id).order_by(m.Membership.created_at)).first()


@router.post("/auth/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    rate_limit(f"login:{request.client.host if request.client else ''}", 20)
    user = db.scalars(select(m.User).where(func.lower(m.User.email) == body.email.lower().strip())).first()
    if not user or not verify_password(body.password, user.password_hash):
        core.audit(db, None, "auth.login_failed", details={"email": body.email[:100]},
                   ip=request.client.host if request.client else "")
        db.commit()
        raise APIError(401, "INVALID_CREDENTIALS", "Email or password is incorrect.")
    if not user.is_active:
        raise APIError(403, "USER_INACTIVE", "This account has been deactivated.")
    mem = _membership(db, user.id)
    user.last_login_at = m.utcnow()
    core.audit(db, mem.organization_id if mem else None, "auth.login", actor_id=user.id,
               ip=request.client.host if request.client else "")
    db.commit()
    return {"token": create_token(user.id, mem.organization_id if mem else None)}


@router.post("/auth/signup")
def signup(body: SignupIn, request: Request, db: Session = Depends(get_db)):
    """SaaS self-service signup (PRD 12, 192, 294): creates org + workspace + owner on the Starter plan."""
    rate_limit(f"signup:{request.client.host if request.client else ''}", 5)
    if db.scalars(select(m.User).where(func.lower(m.User.email) == body.email.lower())).first():
        raise APIError(409, "EMAIL_TAKEN", "An account with this email already exists.")
    slug = re.sub(r"[^a-z0-9]+", "-", body.company_name.lower()).strip("-")[:60] or "org"
    base, i = slug, 1
    while db.scalars(select(m.Organization).where(m.Organization.slug == slug)).first():
        i += 1
        slug = f"{base}-{i}"
    plan = db.scalars(select(m.Plan).where(m.Plan.code == "starter")).first()
    org = m.Organization(name=body.company_name, slug=slug, plan_code="starter", website=body.website,
                         industry=body.industry, country=body.country, timezone=body.timezone,
                         settings={"objective": body.objective, "sender_name": body.name.split()[0]})
    db.add(org)
    db.flush()
    db.add(m.Workspace(organization_id=org.id, name="Main", timezone=body.timezone))
    user = m.User(email=body.email.lower(), name=body.name, password_hash=hash_password(body.password))
    db.add(user)
    db.flush()
    db.add(m.Membership(user_id=user.id, organization_id=org.id, role="owner"))
    acc = core.credit_account(db, org.id)
    acc.monthly_allowance = plan.monthly_gtu if plan else 3000
    core.grant(db, org.id, acc.monthly_allowance, "Starter plan monthly allowance")
    if DEMO_DATA:
        db.add(m.Mailbox(organization_id=org.id, address=f"outreach@{slug}.sandbox", provider="sandbox", daily_limit=50))
    core.audit(db, org.id, "org.signup", actor_id=user.id, details={"plan": "starter"})
    db.commit()
    return {"token": create_token(user.id, org.id)}


DEMO_ROLE_ORDER = ["owner", "admin", "gtm_manager", "sales_user", "viewer", "platform_admin"]
DEMO_DESCRIPTIONS = {
    "owner": "Everything, including billing and API keys",
    "admin": "Users, API keys, policies, audit log",
    "gtm_manager": "Search, missions, campaigns",
    "sales_user": "Inbox, meetings, notes",
    "viewer": "Read-only dashboards",
    "platform_admin": "Platform console: tenants, providers, pricing",
}


def _demo_user(db, role: str):
    if role == "platform_admin":
        return db.scalars(select(m.User).where(m.User.is_platform_admin.is_(True), m.User.is_active.is_(True))
                          .order_by(m.User.created_at)).first(), None
    org = db.scalars(select(m.Organization).where(m.Organization.slug == "ai-automation-labs")).first()
    if not org:
        return None, None
    row = db.execute(select(m.User, m.Membership).join(m.Membership, m.Membership.user_id == m.User.id)
                     .where(m.Membership.organization_id == org.id, m.Membership.role == role,
                            m.User.is_active.is_(True)).order_by(m.User.created_at)).first()
    return (row[0], org.id) if row else (None, None)


@router.get("/auth/demo-accounts")
def demo_accounts(db: Session = Depends(get_db)):
    """Accounts shown as one-click buttons on the login page. Empty unless GTM_DEMO_LOGIN=1."""
    from ..config import DEMO_LOGIN, DEMO_LOGIN_ROLES
    if not DEMO_LOGIN:
        return []
    out = []
    for role in DEMO_ROLE_ORDER:
        if role not in DEMO_LOGIN_ROLES:
            continue
        user, _ = _demo_user(db, role)
        if user:
            out.append({"role": role, "label": ROLE_LABELS.get(role, role), "name": user.name, "email": user.email,
                        "description": DEMO_DESCRIPTIONS[role]})
    return out


class DemoLoginIn(BaseModel):
    role: str


@router.post("/auth/demo-login")
def demo_login(body: DemoLoginIn, request: Request, db: Session = Depends(get_db)):
    from ..config import DEMO_LOGIN, DEMO_LOGIN_ROLES
    if not DEMO_LOGIN or body.role not in DEMO_LOGIN_ROLES:
        raise APIError(403, "DEMO_DISABLED", "Demo sign-in is not enabled.")
    rate_limit(f"demo:{request.client.host if request.client else ''}", 30)
    user, org_id = _demo_user(db, body.role)
    if not user:
        raise APIError(404, "NO_DEMO_ACCOUNT", "No demo account exists for that role.")
    if body.role == "platform_admin":
        mem = _membership(db, user.id)
        org_id = mem.organization_id if mem else None
    user.last_login_at = m.utcnow()
    core.audit(db, org_id, "auth.demo_login", actor_id=user.id, details={"role": body.role},
               ip=request.client.host if request.client else "")
    db.commit()
    return {"token": create_token(user.id, org_id, demo=True)}


class PasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


@router.post("/auth/password")
def change_password(body: PasswordIn, ctx: Ctx = Depends(get_ctx)):
    if not ctx.user or not verify_password(body.current_password, ctx.user.password_hash):
        raise APIError(400, "INVALID_PASSWORD", "Current password is incorrect.")
    if body.new_password == body.current_password:
        raise APIError(400, "PASSWORD_UNCHANGED", "Choose a password different from the current one.")
    ctx.user.password_hash = hash_password(body.new_password)
    ctx.user.must_change_password = False
    core.audit_ctx(ctx, "auth.password_changed", "user", ctx.user.id)
    ctx.db.commit()
    return {"ok": True}


@router.get("/me")
def me(ctx: Ctx = Depends(get_ctx)):
    db = ctx.db
    if not ctx.user:
        raise APIError(403, "USER_ONLY", "This endpoint requires a user session.")
    orgs = [{"id": o.id, "name": o.name, "role": mem.role}
            for mem, o in db.execute(select(m.Membership, m.Organization).join(m.Organization,
                                     m.Organization.id == m.Membership.organization_id)
                                     .where(m.Membership.user_id == ctx.user.id))]
    plan = db.scalars(select(m.Plan).where(m.Plan.code == ctx.org.plan_code)).first() if ctx.org else None
    acc = core.credit_account(db, ctx.org_id) if ctx.org else None
    unread = db.scalar(select(func.count(m.Notification.id)).where(m.Notification.organization_id == ctx.org_id,
                                                                   m.Notification.read.is_(False))) if ctx.org else 0
    db.commit()
    return {
        "user": {"id": ctx.user.id, "name": ctx.user.name, "email": ctx.user.email,
                 "is_platform_admin": ctx.user.is_platform_admin,
                 "must_change_password": ctx.user.must_change_password and not getattr(ctx, "demo_session", False)},
        "demo_session": getattr(ctx, "demo_session", False),
        "organization": {"id": ctx.org.id, "name": ctx.org.name, "plan": ctx.org.plan_code, "website": ctx.org.website,
                         "internal_mode": (ctx.org.settings or {}).get("internal_mode", False),
                         "onboarded": bool(ctx.org.profile)} if ctx.org else None,
        "workspace": {"id": ctx.workspace.id, "name": ctx.workspace.name, "timezone": ctx.workspace.timezone}
        if ctx.workspace else None,
        "workspaces": [{"id": w.id, "name": w.name} for w in db.scalars(
            select(m.Workspace).where(m.Workspace.organization_id == ctx.org_id))] if ctx.org else [],
        "role": ctx.role, "role_label": ROLE_LABELS.get(ctx.role, ""),
        "permissions": sorted(ctx.perms),
        "organizations": orgs,
        "plan": {"code": plan.code, "name": plan.name, "entitlements": plan.entitlements, "limits": plan.limits} if plan else None,
        "credits": {"balance": round(acc.balance, 2), "allowance": acc.monthly_allowance} if acc else None,
        "unread_notifications": unread or 0,
        "demo_mode": DEMO_DATA,
    }


@router.get("/roles")
def roles(ctx: Ctx = Depends(get_ctx)):
    return {"roles": [{"code": r, "label": ROLE_LABELS[r], "permissions": sorted(ROLE_PERMISSIONS[r])} for r in ROLES],
            "permission_catalog": P}
