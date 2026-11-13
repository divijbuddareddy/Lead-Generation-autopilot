"""Platform Super Admin console (PRD 10.6, 164-166, 220, 231, 286).

Super admins manage tenants, providers, pricing, credits, platform-level
credentials and health. They do NOT get tenant conversation content (PRD 231).
"""
from datetime import timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select

from .. import models as m
from ..deps import APIError, Ctx, require
from ..services import core

router = APIRouter(prefix="/app/platform", tags=["platform"])


@router.get("/tenants")
def tenants(ctx: Ctx = Depends(require("platform.tenants"))):
    db = ctx.db
    since = m.utcnow() - timedelta(days=30)
    out = []
    for o in db.scalars(select(m.Organization).order_by(m.Organization.created_at)):
        acc = core.credit_account(db, o.id)
        usage = db.execute(select(func.sum(m.UsageEvent.customer_cost), func.sum(m.UsageEvent.provider_cost))
                           .where(m.UsageEvent.organization_id == o.id, m.UsageEvent.created_at >= since)).first()
        ai = db.execute(select(func.count(m.UsageEvent.id), func.sum(m.UsageEvent.quantity), func.sum(m.UsageEvent.provider_cost))
                        .where(m.UsageEvent.organization_id == o.id, m.UsageEvent.category == "ai",
                               m.UsageEvent.created_at >= since)).first()
        out.append({"id": o.id, "name": o.name, "plan": o.plan_code, "status": o.status,
                    "users": db.scalar(select(func.count(m.Membership.id)).where(m.Membership.organization_id == o.id)),
                    "campaigns": db.scalar(select(func.count(m.Campaign.id)).where(m.Campaign.organization_id == o.id)),
                    "messages_30d": db.scalar(select(func.count(m.Message.id)).where(m.Message.organization_id == o.id,
                                                                                     m.Message.created_at >= since)),
                    "balance": round(acc.balance, 1), "gtu_30d": round(usage[0] or 0, 1),
                    "provider_cost_30d": round(usage[1] or 0, 4), "ai_calls_30d": ai[0] or 0,
                    "ai_ktokens_30d": round(ai[1] or 0, 1), "ai_cost_30d": round(ai[2] or 0, 4),
                    "created_at": o.created_at.isoformat()})
    db.commit()
    return out


class TenantUpdate(BaseModel):
    status: str | None = None
    plan: str | None = None
    grant_gtu: float | None = None
    reason: str = "platform adjustment"


@router.patch("/tenants/{oid}")
def update_tenant(oid: str, body: TenantUpdate, ctx: Ctx = Depends(require("platform.tenants"))):
    db = ctx.db
    o = db.get(m.Organization, oid)
    if not o:
        raise APIError(404, "NOT_FOUND", "Tenant not found.")
    if body.status in ("active", "suspended"):
        o.status = body.status
    if body.plan:
        if not db.scalars(select(m.Plan).where(m.Plan.code == body.plan)).first():
            raise APIError(400, "INVALID_PLAN", "Unknown plan.")
        o.plan_code = body.plan
    if body.grant_gtu:
        if not ctx.has("platform.credits"):
            raise APIError(403, "FORBIDDEN", "platform.credits required")
        core.grant(db, oid, body.grant_gtu, f"Platform: {body.reason}")
    core.audit(db, oid, "platform.tenant_updated", actor_id=ctx.actor_id, details=body.model_dump(exclude_none=True),
               ip=ctx.ip, request_id=ctx.request_id)
    db.commit()
    return {"ok": True}


@router.get("/providers")
def providers(ctx: Ctx = Depends(require("platform.providers"))):
    return [{"id": p.id, "name": p.name, "kind": p.kind, "enabled": p.enabled, "priority": p.priority,
             "cost_usd": p.cost_usd, "region": p.region, "calls": p.calls, "failures": p.failures,
             "success_rate": round(100 * (1 - p.failures / p.calls), 1) if p.calls else None,
             "avg_latency_ms": p.avg_latency_ms, "license": p.license, "config": p.config,
             "requires_secret": p.requires_secret, "description": p.description}
            for p in ctx.db.scalars(select(m.Provider).order_by(m.Provider.kind, m.Provider.priority))]


@router.patch("/providers/{pid}")
def update_provider(pid: str, body: dict, ctx: Ctx = Depends(require("platform.providers"))):
    p = ctx.db.get(m.Provider, pid)
    if not p:
        raise APIError(404, "NOT_FOUND", "Provider not found.")
    for k in ("enabled", "priority", "cost_usd", "region", "license", "config"):
        if k in body:
            setattr(p, k, body[k])
    core.audit(ctx.db, None, "platform.provider_updated", actor_id=ctx.actor_id, entity_type="provider", entity_id=pid,
               details=body, ip=ctx.ip)
    ctx.db.commit()
    return {"ok": True}


@router.get("/pricing")
def pricing(ctx: Ctx = Depends(require("platform.pricing"))):
    return {"meters": [{"meter": p.meter_type, "gtu": p.gtu_cost, "provider_cost_usd": p.provider_cost_usd,
                        "category": p.category} for p in ctx.db.scalars(select(m.MeterPrice))],
            "plans": [{"code": p.code, "name": p.name, "price_usd": p.price_usd, "monthly_gtu": p.monthly_gtu,
                       "entitlements": p.entitlements, "limits": p.limits} for p in ctx.db.scalars(select(m.Plan))]}


@router.patch("/pricing/meters/{meter}")
def update_meter(meter: str, body: dict, ctx: Ctx = Depends(require("platform.pricing"))):
    p = core.price(ctx.db, meter)
    if "gtu" in body:
        p.gtu_cost = float(body["gtu"])
    if "provider_cost_usd" in body:
        p.provider_cost_usd = float(body["provider_cost_usd"])
    core.audit(ctx.db, None, "platform.meter_price_updated", actor_id=ctx.actor_id, details={"meter": meter, **body})
    ctx.db.commit()
    return {"ok": True}


@router.patch("/pricing/plans/{code}")
def update_plan(code: str, body: dict, ctx: Ctx = Depends(require("platform.pricing"))):
    p = ctx.db.scalars(select(m.Plan).where(m.Plan.code == code)).first()
    if not p:
        raise APIError(404, "NOT_FOUND", "Plan not found.")
    for k in ("name", "price_usd", "monthly_gtu", "entitlements", "limits"):
        if k in body:
            setattr(p, k, body[k])
    core.audit(ctx.db, None, "platform.plan_updated", actor_id=ctx.actor_id, details={"plan": code, **body})
    ctx.db.commit()
    return {"ok": True}


# Platform-level credentials: shared keys used by every tenant that has not set its own.
class SecretIn(BaseModel):
    key: str
    value: str = ""


@router.get("/secrets")
def platform_secrets(ctx: Ctx = Depends(require("platform.secrets"))):
    return core.list_secrets(ctx.db, "platform", None)


@router.put("/secrets")
def put_platform_secret(body: SecretIn, ctx: Ctx = Depends(require("platform.secrets"))):
    core.set_secret(ctx.db, "platform", None, body.key, body.value.strip(), ctx.actor_id)
    core.audit(ctx.db, None, "platform.secret_updated" if body.value else "platform.secret_deleted",
               actor_id=ctx.actor_id, entity_type="secret", entity_id=body.key, ip=ctx.ip)
    ctx.db.commit()
    return {"ok": True}


@router.get("/health")
def health(ctx: Ctx = Depends(require("platform.health"))):
    from ..worker import worker
    db = ctx.db
    q = dict(db.execute(select(m.Job.status, func.count(m.Job.id)).group_by(m.Job.status)).all())
    hour = m.utcnow() - timedelta(hours=1)
    by_channel = db.execute(select(m.Message.channel, m.Message.status, func.count(m.Message.id))
                            .where(m.Message.created_at >= hour, m.Message.direction == "outbound")
                            .group_by(m.Message.channel, m.Message.status)).all()
    channels = {}
    for ch, st, n in by_channel:
        c = channels.setdefault(ch, {"sent": 0, "failed": 0, "blocked": 0})
        if st in ("sent", "delivered", "task_created"):
            c["sent"] += n
        elif st in c:
            c[st] += n
    boxes = db.execute(select(m.Mailbox.status, func.count(m.Mailbox.id)).group_by(m.Mailbox.status)).all()
    webhooks = dict(db.execute(select(m.WebhookDelivery.status, func.count(m.WebhookDelivery.id))
                               .group_by(m.WebhookDelivery.status)).all())
    spam_spikes = [{"org": oid, "unsubscribes_24h": n} for oid, n in db.execute(
        select(m.SuppressionEntry.organization_id, func.count(m.SuppressionEntry.id))
        .where(m.SuppressionEntry.created_at >= m.utcnow() - timedelta(days=1), m.SuppressionEntry.kind == "person")
        .group_by(m.SuppressionEntry.organization_id)).all() if n >= 10]
    return {"worker_last_tick": worker.last_tick.isoformat() if worker.last_tick else None,
            "queue": q, "channels_last_hour": channels, "mailboxes": dict(boxes), "webhooks": webhooks,
            "abuse_signals": spam_spikes,
            "tenants": db.scalar(select(func.count(m.Organization.id))),
            "users": db.scalar(select(func.count(m.User.id)))}


@router.get("/jobs")
def jobs(ctx: Ctx = Depends(require("platform.jobs"))):
    return [{"id": j.id, "org": j.organization_id, "type": j.type, "status": j.status, "attempts": j.attempts,
             "progress": j.progress, "total": j.total, "error": j.error, "request_id": j.request_id,
             "created_at": j.created_at.isoformat()}
            for j in ctx.db.scalars(select(m.Job).order_by(m.Job.created_at.desc()).limit(100))]


@router.get("/audit")
def platform_audit(request_id: str = "", ctx: Ctx = Depends(require("platform.audit"))):
    """Execution trace by request_id (PRD 285-286). Metadata only - no message content."""
    q = select(m.AuditLog)
    if request_id:
        q = q.where(m.AuditLog.request_id == request_id)
    rows = [{"action": a.action, "org": a.organization_id, "actor": a.actor_id, "actor_type": a.actor_type,
             "entity": f"{a.entity_type}:{a.entity_id}", "request_id": a.request_id, "ip": a.ip,
             "at": a.created_at.isoformat()} for a in ctx.db.scalars(q.order_by(m.AuditLog.created_at.desc()).limit(200))]
    jobs_ = [{"id": j.id, "type": j.type, "status": j.status, "error": j.error}
             for j in ctx.db.scalars(select(m.Job).where(m.Job.request_id == request_id))] if request_id else []
    return {"audit": rows, "jobs": jobs_}
