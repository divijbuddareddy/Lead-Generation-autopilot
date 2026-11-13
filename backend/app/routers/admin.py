"""Tenant administration: users & roles, credential vault, API keys, webhooks,
workspace policy, billing, mailboxes, offers/proof, suppression, audit, notifications."""
import secrets

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select

from .. import models as m
from ..deps import APIError, Ctx, require, tenant_get
from ..rbac import ROLE_LABELS, ROLES, can_assign
from ..security import encrypt, generate_api_key, hash_password
from ..services import core
from ..services.engine import DEFAULT_POLICY, ws_policy
from ..services.intelligence import DEFAULT_WEIGHTS

router = APIRouter(prefix="/app", tags=["admin"])


# ------------------------------------------------------------------ users & roles
class InviteIn(BaseModel):
    email: EmailStr
    name: str
    role: str
    password: str | None = None


@router.get("/users")
def list_users(ctx: Ctx = Depends(require("users.manage"))):
    rows = ctx.db.execute(select(m.Membership, m.User).join(m.User, m.User.id == m.Membership.user_id)
                          .where(m.Membership.organization_id == ctx.org_id)).all()
    return [{"id": u.id, "name": u.name, "email": u.email, "role": mem.role, "role_label": ROLE_LABELS[mem.role],
             "active": u.is_active, "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None}
            for mem, u in rows]


@router.post("/users")
def invite_user(body: InviteIn, ctx: Ctx = Depends(require("users.manage"))):
    db = ctx.db
    if body.role not in ROLES or not can_assign(ctx.role, body.role):
        raise APIError(403, "ROLE_NOT_ASSIGNABLE", f"A {ctx.role} cannot grant the {body.role} role.")
    plan = db.scalars(select(m.Plan).where(m.Plan.code == ctx.org.plan_code)).first()
    count = db.scalar(select(func.count(m.Membership.id)).where(m.Membership.organization_id == ctx.org_id))
    if plan and count >= (plan.limits or {}).get("users", 2):
        raise APIError(402, "USER_LIMIT", f"Your plan allows {plan.limits.get('users')} users. Upgrade to add more.")
    user = db.scalars(select(m.User).where(func.lower(m.User.email) == body.email.lower())).first()
    temp = body.password or secrets.token_urlsafe(9)
    if not user:
        user = m.User(email=body.email.lower(), name=body.name, password_hash=hash_password(temp),
                      must_change_password=True)
        db.add(user)
        db.flush()
    elif db.scalars(select(m.Membership).where(m.Membership.user_id == user.id,
                                               m.Membership.organization_id == ctx.org_id)).first():
        raise APIError(409, "ALREADY_MEMBER", "User is already a member.")
    db.add(m.Membership(user_id=user.id, organization_id=ctx.org_id, role=body.role))
    core.audit_ctx(ctx, "user.invited", "user", user.id, {"role": body.role, "email": user.email})
    db.commit()
    return {"id": user.id, "temporary_password": None if body.password else temp}


class RoleIn(BaseModel):
    role: str | None = None
    active: bool | None = None


@router.patch("/users/{user_id}")
def update_user(user_id: str, body: RoleIn, ctx: Ctx = Depends(require("users.manage"))):
    db = ctx.db
    mem = db.scalars(select(m.Membership).where(m.Membership.user_id == user_id,
                                                m.Membership.organization_id == ctx.org_id)).first()
    if not mem:
        raise APIError(404, "NOT_FOUND", "User not found in this organization.")
    if user_id == ctx.actor_id:
        raise APIError(400, "SELF_CHANGE", "You cannot change your own role or status.")
    if not can_assign(ctx.role, mem.role):
        raise APIError(403, "FORBIDDEN", f"A {ctx.role} cannot modify a {mem.role}.")
    if body.role:
        if body.role not in ROLES or not can_assign(ctx.role, body.role):
            raise APIError(403, "ROLE_NOT_ASSIGNABLE", f"A {ctx.role} cannot grant {body.role}.")
        if mem.role == "owner" and body.role != "owner":
            owners = db.scalar(select(func.count(m.Membership.id)).where(m.Membership.organization_id == ctx.org_id,
                                                                         m.Membership.role == "owner"))
            if owners <= 1:
                raise APIError(400, "LAST_OWNER", "The organization must keep at least one owner.")
        core.audit_ctx(ctx, "user.role_changed", "user", user_id, {"from": mem.role, "to": body.role})
        mem.role = body.role
    if body.active is not None:
        u = db.get(m.User, user_id)
        u.is_active = body.active
        core.audit_ctx(ctx, "user.status_changed", "user", user_id, {"active": body.active})
    db.commit()
    return {"ok": True}


@router.delete("/users/{user_id}")
def remove_user(user_id: str, ctx: Ctx = Depends(require("users.manage"))):
    mem = ctx.db.scalars(select(m.Membership).where(m.Membership.user_id == user_id,
                                                    m.Membership.organization_id == ctx.org_id)).first()
    if not mem or user_id == ctx.actor_id or not can_assign(ctx.role, mem.role):
        raise APIError(403, "FORBIDDEN", "Cannot remove this user.")
    ctx.db.delete(mem)
    core.audit_ctx(ctx, "user.removed", "user", user_id)
    ctx.db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ credential vault (admin-held API keys)
class SecretIn(BaseModel):
    key: str
    value: str = ""


@router.get("/integrations/secrets")
def get_secrets(ctx: Ctx = Depends(require("integrations.manage"))):
    """Only admins/owners reach this; values are never returned - only a mask."""
    return core.list_secrets(ctx.db, "org", ctx.org_id)


@router.put("/integrations/secrets")
def put_secret(body: SecretIn, ctx: Ctx = Depends(require("integrations.manage"))):
    core.set_secret(ctx.db, "org", ctx.org_id, body.key, body.value.strip(), ctx.actor_id)
    core.audit_ctx(ctx, "secret.updated" if body.value else "secret.deleted", "secret", body.key)
    ctx.db.commit()
    return {"ok": True}


@router.get("/integrations/status")
def integration_status(ctx: Ctx = Depends(require("dashboard.view"))):
    """Non-secret view for everyone: which capabilities are connected (no values)."""
    has = lambda k: bool(core.get_secret(ctx.db, ctx.org_id, k))  # noqa: E731
    from ..services.engine import email_mode
    return {"ai": "anthropic" if has("ANTHROPIC_API_KEY") else "openai" if has("OPENAI_API_KEY") else "rules (no AI key)",
            "email": email_mode(ctx.db, ctx.org_id), "whatsapp": "cloud_api" if has("WHATSAPP_TOKEN") else "sandbox",
            "sms": "twilio" if has("TWILIO_AUTH_TOKEN") else "sandbox", "crm": "hubspot" if has("HUBSPOT_TOKEN") else "internal",
            "slack": "connected" if has("SLACK_WEBHOOK_URL") else "not connected", "calendar": "native booking"}


RESEND_EVENTS = ["email.sent", "email.delivered", "email.bounced", "email.complained", "email.received"]


def _resend_domains(key: str) -> tuple[list[dict], str]:
    import httpx
    try:
        r = httpx.get("https://api.resend.com/domains", headers={"Authorization": f"Bearer {key}"}, timeout=10)
    except httpx.HTTPError as exc:
        return [], f"Could not reach Resend: {exc}"
    if r.status_code != 200:
        try:
            msg = r.json().get("message", r.text)
        except ValueError:
            msg = r.text
        return [], f"Resend {r.status_code}: {msg}"[:200]
    return [{"name": d.get("name"), "status": d.get("status"), "region": d.get("region")}
            for d in r.json().get("data", [])], ""


@router.get("/integrations/resend")
def resend_status(request: Request, ctx: Ctx = Depends(require("integrations.manage"))):
    """Setup checklist: API key, verified sending domains (SPF/DKIM), webhook, reply-to."""
    key = core.get_secret(ctx.db, ctx.org_id, "RESEND_API_KEY")
    domains, error = _resend_domains(key) if key else ([], "")
    boxes = list(ctx.db.scalars(select(m.Mailbox).where(m.Mailbox.organization_id == ctx.org_id,
                                                        m.Mailbox.provider == "resend")))
    return {"api_key": bool(key), "domains": domains, "error": error,
            "webhook_secret": bool(core.get_secret(ctx.db, ctx.org_id, "RESEND_WEBHOOK_SECRET")),
            "reply_to": core.get_secret(ctx.db, ctx.org_id, "RESEND_REPLY_TO") or "",
            "webhook_url": str(request.base_url).rstrip("/") + f"/webhooks/resend/{ctx.org_id}",
            "webhook_events": RESEND_EVENTS, "mailboxes": [b.address for b in boxes]}


class TestEmailIn(BaseModel):
    to: EmailStr
    mailbox_id: str | None = None


@router.post("/integrations/resend/test")
def resend_test(body: TestEmailIn, ctx: Ctx = Depends(require("integrations.manage"))):
    from ..services.engine import PermanentSendError, send_resend
    q = select(m.Mailbox).where(m.Mailbox.organization_id == ctx.org_id, m.Mailbox.provider == "resend")
    if body.mailbox_id:
        q = q.where(m.Mailbox.id == body.mailbox_id)
    box = ctx.db.scalars(q).first()
    if not box:
        raise APIError(400, "NO_RESEND_MAILBOX", "Add a Resend sending mailbox first (address on a verified domain).")
    probe = m.Person(email=body.to, first_name="", source="admin_test")
    try:
        res = send_resend(ctx.db, ctx.org, box, probe, "GTM Autopilot: Resend test",
                          "This is a test email from GTM Autopilot. Your Resend integration is working.")
    except PermanentSendError as exc:
        raise APIError(400, "RESEND_REJECTED", str(exc))
    except Exception as exc:  # noqa: BLE001
        raise APIError(502, "RESEND_UNAVAILABLE", f"Resend is not reachable right now: {exc}"[:200])
    core.audit_ctx(ctx, "email.test_sent", "mailbox", box.id, {"to": body.to, "provider": "resend"})
    ctx.db.commit()
    return {"ok": True, "id": res["provider_message_id"], "from": box.address}


class AISettingsIn(BaseModel):
    vendor: str | None = None
    model: str | None = None


@router.put("/integrations/ai")
def set_ai(body: AISettingsIn, ctx: Ctx = Depends(require("integrations.manage"))):
    s = dict(ctx.org.settings or {})
    s["ai"] = {k: v for k, v in body.model_dump().items() if v}
    ctx.org.settings = s
    core.audit_ctx(ctx, "ai.settings_changed", "organization", ctx.org_id, s["ai"])
    ctx.db.commit()
    return s["ai"]


# ------------------------------------------------------------------ public API keys (PRD 116, 145)
class ApiKeyIn(BaseModel):
    name: str
    role: str = "gtm_manager"


@router.get("/api-keys")
def list_keys(ctx: Ctx = Depends(require("apikeys.manage"))):
    return [{"id": k.id, "name": k.name, "prefix": k.prefix + "…", "role": k.role, "revoked": k.revoked,
             "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None, "created_at": k.created_at.isoformat()}
            for k in ctx.db.scalars(select(m.ApiKey).where(m.ApiKey.organization_id == ctx.org_id)
                                    .order_by(m.ApiKey.created_at.desc()))]


@router.post("/api-keys")
def create_key(body: ApiKeyIn, ctx: Ctx = Depends(require("apikeys.manage"))):
    core.require_entitlement(ctx.db, ctx.org, "api_access")
    if body.role not in ROLES or not can_assign(ctx.role, body.role) or body.role == "owner":
        raise APIError(403, "ROLE_NOT_ASSIGNABLE", "API keys can act as admin, gtm_manager, sales_user or viewer.")
    raw, prefix, h = generate_api_key()
    key = m.ApiKey(organization_id=ctx.org_id, workspace_id=ctx.ws_id, name=body.name, prefix=prefix, key_hash=h,
                   role=body.role, created_by=ctx.actor_id)
    ctx.db.add(key)
    core.audit_ctx(ctx, "api_key.created", "api_key", key.id, {"name": body.name, "role": body.role})
    ctx.db.commit()
    return {"id": key.id, "key": raw, "note": "Store this key now - it will not be shown again."}


@router.delete("/api-keys/{key_id}")
def revoke_key(key_id: str, ctx: Ctx = Depends(require("apikeys.manage"))):
    k = tenant_get(ctx, m.ApiKey, key_id)
    k.revoked = True
    core.audit_ctx(ctx, "api_key.revoked", "api_key", key_id)
    ctx.db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ webhooks
class WebhookIn(BaseModel):
    url: str = Field(pattern=r"^https?://")
    events: list[str] = []


@router.get("/webhooks")
def list_webhooks(ctx: Ctx = Depends(require("webhooks.manage"))):
    eps = list(ctx.db.scalars(select(m.WebhookEndpoint).where(m.WebhookEndpoint.organization_id == ctx.org_id)))
    deliveries = list(ctx.db.scalars(select(m.WebhookDelivery).where(m.WebhookDelivery.organization_id == ctx.org_id)
                                     .order_by(m.WebhookDelivery.created_at.desc()).limit(30)))
    return {"events": core.WEBHOOK_EVENTS,
            "endpoints": [{"id": e.id, "url": e.url, "events": e.events, "active": e.active} for e in eps],
            "deliveries": [{"id": d.id, "event": d.event, "status": d.status, "code": d.response_code,
                            "attempts": d.attempts, "created_at": d.created_at.isoformat()} for d in deliveries]}


@router.post("/webhooks")
def create_webhook(body: WebhookIn, ctx: Ctx = Depends(require("webhooks.manage"))):
    secret = "whsec_" + secrets.token_urlsafe(24)
    ep = m.WebhookEndpoint(organization_id=ctx.org_id, url=body.url, events=body.events, secret_enc=encrypt(secret))
    ctx.db.add(ep)
    core.audit_ctx(ctx, "webhook.created", "webhook", ep.id, {"url": body.url})
    ctx.db.commit()
    return {"id": ep.id, "signing_secret": secret, "note": "Verify X-GTM-Signature (HMAC-SHA256). Shown once."}


@router.delete("/webhooks/{wid}")
def delete_webhook(wid: str, ctx: Ctx = Depends(require("webhooks.manage"))):
    ctx.db.delete(tenant_get(ctx, m.WebhookEndpoint, wid))
    core.audit_ctx(ctx, "webhook.deleted", "webhook", wid)
    ctx.db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ workspace settings / policy (PRD 38-39, 88, 190, 215)
@router.get("/settings")
def get_settings(ctx: Ctx = Depends(require("dashboard.view"))):
    ws = ctx.workspace
    return {"organization": {"name": ctx.org.name, "website": ctx.org.website, "industry": ctx.org.industry,
                             "country": ctx.org.country, "timezone": ctx.org.timezone,
                             "sender_name": (ctx.org.settings or {}).get("sender_name", ""),
                             "profile": ctx.org.profile or {}},
            "workspace": {"name": ws.name, "timezone": ws.timezone} if ws else {},
            "policy": ws_policy(ws), "score_weights": {**DEFAULT_WEIGHTS, **((ws.settings or {}).get("score_weights") or {})},
            "icp": (ws.settings or {}).get("icp", {}), "memory": (ws.settings or {}).get("memory", []),
            "default_policy": DEFAULT_POLICY, "can_edit": ctx.has("workspace.manage")}


class SettingsIn(BaseModel):
    organization: dict | None = None
    workspace: dict | None = None
    policy: dict | None = None
    score_weights: dict | None = None
    icp: dict | None = None
    memory: list[str] | None = None


@router.put("/settings")
def put_settings(body: SettingsIn, ctx: Ctx = Depends(require("workspace.manage"))):
    ws = ctx.workspace
    s = dict(ws.settings or {})
    if body.policy is not None:
        if "autopilot" in body.policy and not ctx.has("autonomy.configure"):
            raise APIError(403, "FORBIDDEN", "Changing autopilot requires autonomy.configure.")
        s["policy"] = {**s.get("policy", {}), **body.policy}
    if body.score_weights is not None:
        total = sum(float(v) for v in body.score_weights.values())
        if not 0.99 <= total <= 1.01:
            raise APIError(400, "INVALID_WEIGHTS", "Score weights must sum to 1.0.")
        s["score_weights"] = body.score_weights
    if body.icp is not None:
        s["icp"] = body.icp
    if body.memory is not None:   # workspace memory e.g. "We do not target hospitals" (PRD 36)
        s["memory"] = body.memory
    ws.settings = s
    if body.workspace:
        ws.name = body.workspace.get("name", ws.name)
        ws.timezone = body.workspace.get("timezone", ws.timezone)
    if body.organization:
        o = ctx.org
        for k in ("name", "website", "industry", "country", "timezone"):
            if k in body.organization:
                setattr(o, k, body.organization[k])
        if "sender_name" in body.organization:
            o.settings = {**(o.settings or {}), "sender_name": body.organization["sender_name"]}
    core.audit_ctx(ctx, "settings.updated", "workspace", ws.id, {k: v for k, v in body.model_dump().items() if v is not None})
    ctx.db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ billing & usage (PRD 89-93, 248-250)
@router.get("/billing")
def billing(ctx: Ctx = Depends(require("billing.view"))):
    db = ctx.db
    acc = core.credit_account(db, ctx.org_id)
    plans = [{"code": p.code, "name": p.name, "price_usd": p.price_usd, "monthly_gtu": p.monthly_gtu,
              "entitlements": p.entitlements, "limits": p.limits}
             for p in db.scalars(select(m.Plan).where(m.Plan.code != "internal"))]
    tx = [{"amount": t.amount, "reason": t.reason, "at": t.created_at.isoformat()}
          for t in db.scalars(select(m.CreditTransaction).where(m.CreditTransaction.organization_id == ctx.org_id)
                              .order_by(m.CreditTransaction.created_at.desc()).limit(40))]
    usage = core.usage_summary(db, ctx.org_id)
    used = sum(v["gtu"] for v in usage.values())
    days = max(1, (m.utcnow() - acc.period_start).days or 1)
    prices = [{"meter": p.meter_type, "gtu": p.gtu_cost, "category": p.category}
              for p in db.scalars(select(m.MeterPrice))]
    db.commit()
    return {"plan": ctx.org.plan_code, "balance": round(acc.balance, 2), "allowance": acc.monthly_allowance,
            "used_30d": round(used, 2), "estimated_month_end": round(used / days * 30, 2),
            "pct_used": round(100 * (1 - acc.balance / acc.monthly_allowance), 1) if acc.monthly_allowance else 0,
            "usage": usage, "transactions": tx, "plans": plans, "prices": prices,
            "can_manage": ctx.has("billing.manage"),
            "internal_mode": (ctx.org.settings or {}).get("internal_mode", False)}


class PlanIn(BaseModel):
    plan: str


@router.post("/billing/plan")
def change_plan(body: PlanIn, ctx: Ctx = Depends(require("billing.manage"))):
    plan = ctx.db.scalars(select(m.Plan).where(m.Plan.code == body.plan, m.Plan.code != "internal")).first()
    if not plan:
        raise APIError(404, "NOT_FOUND", "Plan not found.")
    old = ctx.org.plan_code
    ctx.org.plan_code = plan.code
    acc = core.credit_account(ctx.db, ctx.org_id)
    if plan.monthly_gtu > acc.monthly_allowance:
        core.grant(ctx.db, ctx.org_id, plan.monthly_gtu - acc.monthly_allowance, f"Upgrade to {plan.name} (prorated)")
    acc.monthly_allowance = plan.monthly_gtu
    core.audit_ctx(ctx, "billing.plan_changed", "organization", ctx.org_id, {"from": old, "to": plan.code})
    ctx.db.commit()
    return {"ok": True, "note": "Payment collection is handled by your payment provider integration (Stripe/Razorpay)."}


class TopUpIn(BaseModel):
    gtu: int = Field(gt=0, le=1_000_000)


@router.post("/billing/topup")
def topup(body: TopUpIn, ctx: Ctx = Depends(require("billing.manage"))):
    core.grant(ctx.db, ctx.org_id, body.gtu, "Additional usage purchased")
    core.audit_ctx(ctx, "billing.topup", "organization", ctx.org_id, {"gtu": body.gtu})
    ctx.db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ mailboxes (PRD 50-52, 167-168)
class MailboxIn(BaseModel):
    address: EmailStr
    provider: str = "sandbox"
    daily_limit: int = 40


@router.get("/mailboxes")
def mailboxes(ctx: Ctx = Depends(require("dashboard.view"))):
    return [{"id": b.id, "address": b.address, "provider": b.provider, "status": b.status, "health": b.health_score,
             "daily_limit": b.daily_limit, "sent_today": b.sent_today, "sent_total": b.sent_total,
             "replies": b.replies, "bounces": b.bounces}
            for b in ctx.db.scalars(select(m.Mailbox).where(m.Mailbox.organization_id == ctx.org_id))]


@router.post("/mailboxes")
def add_mailbox(body: MailboxIn, ctx: Ctx = Depends(require("integrations.manage"))):
    if body.provider not in ("resend", "smtp", "sandbox"):
        raise APIError(400, "INVALID_PROVIDER", "Provider must be resend, smtp or sandbox.")
    if body.provider == "smtp" and not core.get_secret(ctx.db, ctx.org_id, "SMTP_HOST"):
        raise APIError(400, "SMTP_NOT_CONFIGURED", "Add SMTP credentials in Integrations first.")
    if body.provider == "resend":
        key = core.get_secret(ctx.db, ctx.org_id, "RESEND_API_KEY")
        if not key:
            raise APIError(400, "RESEND_NOT_CONFIGURED", "Add your Resend API key in the credential vault first.")
        domains, err = _resend_domains(key)
        dom = body.address.split("@")[-1].lower()
        match = next((d for d in domains if (d["name"] or "").lower() == dom), None)
        if not err and not match:
            raise APIError(400, "DOMAIN_NOT_IN_RESEND", f"{dom} is not a domain in your Resend account. Add and verify it in Resend first.")
        if match and match["status"] != "verified":
            raise APIError(400, "DOMAIN_NOT_VERIFIED", f"{dom} is '{match['status']}' in Resend. Finish DNS verification (SPF/DKIM) first.")
    # New real mailboxes start WARMING with a conservative cap (PRD 168); volume grows with health.
    b = m.Mailbox(organization_id=ctx.org_id, address=body.address.lower(), provider=body.provider,
                  daily_limit=body.daily_limit, status="HEALTHY" if body.provider == "sandbox" else "WARMING")
    ctx.db.add(b)
    core.audit_ctx(ctx, "mailbox.added", "mailbox", b.id, {"address": body.address})
    ctx.db.commit()
    return {"id": b.id}


@router.patch("/mailboxes/{bid}")
def update_mailbox(bid: str, body: dict, ctx: Ctx = Depends(require("integrations.manage"))):
    b = tenant_get(ctx, m.Mailbox, bid)
    if body.get("status") in ("HEALTHY", "WARMING", "PAUSED", "RETIRED"):
        b.status = body["status"]
    if body.get("daily_limit"):
        b.daily_limit = int(body["daily_limit"])
    ctx.db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ offers & proof (PRD 257-261)
class OfferIn(BaseModel):
    name: str
    description: str = ""
    problems: list[str] = []
    benefits: list[str] = []
    cta: str = "Open to a 20-minute call next week?"
    pricing: str = ""
    industries: list[str] = []


@router.get("/offers")
def offers(ctx: Ctx = Depends(require("leads.view"))):
    o = [{"id": x.id, "name": x.name, "description": x.description, "problems": x.problems, "benefits": x.benefits,
          "cta": x.cta, "pricing": x.pricing, "industries": x.industries}
         for x in ctx.db.scalars(select(m.Offer).where(m.Offer.organization_id == ctx.org_id))]
    p = [{"id": x.id, "claim": x.claim, "source": x.source, "approved": x.approved}
         for x in ctx.db.scalars(select(m.ProofItem).where(m.ProofItem.organization_id == ctx.org_id))]
    return {"offers": o, "proof": p}


@router.post("/offers")
def create_offer(body: OfferIn, ctx: Ctx = Depends(require("offers.manage"))):
    o = m.Offer(organization_id=ctx.org_id, workspace_id=ctx.ws_id, **body.model_dump())
    ctx.db.add(o)
    core.audit_ctx(ctx, "offer.created", "offer", o.id)
    ctx.db.commit()
    return {"id": o.id}


@router.put("/offers/{oid}")
def update_offer(oid: str, body: OfferIn, ctx: Ctx = Depends(require("offers.manage"))):
    o = tenant_get(ctx, m.Offer, oid)
    for k, v in body.model_dump().items():
        setattr(o, k, v)
    ctx.db.commit()
    return {"ok": True}


@router.delete("/offers/{oid}")
def delete_offer(oid: str, ctx: Ctx = Depends(require("offers.manage"))):
    ctx.db.delete(tenant_get(ctx, m.Offer, oid))
    ctx.db.commit()
    return {"ok": True}


class ProofIn(BaseModel):
    claim: str
    source: str = ""
    approved: bool = False


@router.post("/proof")
def add_proof(body: ProofIn, ctx: Ctx = Depends(require("offers.manage"))):
    if body.approved and not ctx.has("workspace.manage"):
        raise APIError(403, "FORBIDDEN", "Only admins can approve claims.")
    pr = m.ProofItem(organization_id=ctx.org_id, **body.model_dump())
    ctx.db.add(pr)
    core.audit_ctx(ctx, "proof.created", "proof", pr.id, {"approved": body.approved})
    ctx.db.commit()
    return {"id": pr.id}


@router.patch("/proof/{pid}")
def approve_proof(pid: str, body: dict, ctx: Ctx = Depends(require("workspace.manage"))):
    pr = tenant_get(ctx, m.ProofItem, pid)
    pr.approved = bool(body.get("approved"))
    core.audit_ctx(ctx, "proof.approval_changed", "proof", pid, {"approved": pr.approved})
    ctx.db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ suppression (PRD 63-64)
class SuppressIn(BaseModel):
    kind: str = Field(pattern="^(email|phone|domain|person|linkedin)$")
    value: str
    channel: str = "all"
    scope: str = "organization"
    reason: str = "manual"


@router.get("/suppression")
def list_suppression(ctx: Ctx = Depends(require("leads.view"))):
    return [{"id": s.id, "kind": s.kind, "value": s.value, "channel": s.channel, "reason": s.reason,
             "scope": "workspace" if s.workspace_id else "organization", "created_at": s.created_at.isoformat()}
            for s in ctx.db.scalars(select(m.SuppressionEntry).where(m.SuppressionEntry.organization_id == ctx.org_id)
                                    .order_by(m.SuppressionEntry.created_at.desc()).limit(500))]


@router.post("/suppression")
def add_suppression(body: SuppressIn, ctx: Ctx = Depends(require("suppression.manage"))):
    val = body.value.strip().lower() if body.kind in ("email", "domain") else body.value.strip()
    if body.kind == "domain":
        val = val.removeprefix("http://").removeprefix("https://").removeprefix("www.").split("/")[0]
    s = m.SuppressionEntry(organization_id=ctx.org_id, workspace_id=ctx.ws_id if body.scope == "workspace" else None,
                           kind=body.kind, value=val, channel=body.channel, reason=body.reason, created_by=ctx.actor_id)
    ctx.db.add(s)
    # stop any in-flight sequences that now match
    from ..services.engine import is_suppressed, stop_followups
    ctx.db.flush()
    affected = 0
    q = select(m.Person).join(m.CampaignLead, m.CampaignLead.person_id == m.Person.id).where(
        m.Person.organization_id == ctx.org_id, m.CampaignLead.status.in_(["pending", "active"]))
    for p in ctx.db.scalars(q):
        if is_suppressed(ctx.db, ctx.org_id, ctx.ws_id, p):
            stop_followups(ctx.db, p.id, "suppressed")
            affected += 1
    core.audit_ctx(ctx, "suppression.added", "suppression", s.id, {"kind": body.kind, "value": val})
    core.emit(ctx.db, ctx.org_id, "lead.suppressed", "", {"kind": body.kind, "value": val})
    ctx.db.commit()
    return {"id": s.id, "stopped_sequences": affected}


@router.delete("/suppression/{sid}")
def remove_suppression(sid: str, ctx: Ctx = Depends(require("suppression.manage"))):
    s = tenant_get(ctx, m.SuppressionEntry, sid)
    core.audit_ctx(ctx, "suppression.removed", "suppression", sid, {"kind": s.kind, "value": s.value})
    ctx.db.delete(s)
    ctx.db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ audit & notifications
@router.get("/audit")
def audit_log(action: str = "", limit: int = 200, ctx: Ctx = Depends(require("audit.view"))):
    q = select(m.AuditLog).where(m.AuditLog.organization_id == ctx.org_id)
    if action:
        q = q.where(m.AuditLog.action.like(f"{action}%"))
    users = {u.id: u.name for u in ctx.db.scalars(select(m.User))}
    return [{"id": a.id, "action": a.action, "actor": users.get(a.actor_id, a.actor_id or a.actor_type),
             "actor_type": a.actor_type, "entity": f"{a.entity_type}:{a.entity_id}" if a.entity_type else "",
             "details": a.details, "ip": a.ip, "request_id": a.request_id, "at": a.created_at.isoformat()}
            for a in ctx.db.scalars(q.order_by(m.AuditLog.created_at.desc()).limit(min(limit, 1000)))]


@router.get("/notifications")
def notifications(ctx: Ctx = Depends(require("dashboard.view"))):
    return [{"id": n.id, "kind": n.kind, "title": n.title, "body": n.body, "link": n.link, "read": n.read,
             "at": n.created_at.isoformat()}
            for n in ctx.db.scalars(select(m.Notification).where(m.Notification.organization_id == ctx.org_id)
                                    .order_by(m.Notification.created_at.desc()).limit(50))]


@router.post("/notifications/read")
def mark_read(ctx: Ctx = Depends(require("dashboard.view"))):
    for n in ctx.db.scalars(select(m.Notification).where(m.Notification.organization_id == ctx.org_id,
                                                         m.Notification.read.is_(False))):
        n.read = True
    ctx.db.commit()
    return {"ok": True}


@router.get("/role-labels")
def role_labels():
    return ROLE_LABELS
