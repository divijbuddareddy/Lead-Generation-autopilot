"""Cross-cutting services: audit, secrets vault, credits/metering, events, notifications."""
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models as m
from ..deps import APIError
from ..security import decrypt, encrypt

# --------------------------------------------------------------------------- audit

def audit(db: Session, org_id: str | None, action: str, *, actor_id: str = "", actor_type: str = "user",
          entity_type: str = "", entity_id: str = "", details: dict | None = None, ip: str = "",
          request_id: str = ""):
    db.add(m.AuditLog(organization_id=org_id, actor_id=actor_id, actor_type=actor_type, action=action,
                      entity_type=entity_type, entity_id=entity_id, details=details or {}, ip=ip,
                      request_id=request_id))


def audit_ctx(ctx, action: str, entity_type: str = "", entity_id: str = "", details: dict | None = None):
    audit(ctx.db, ctx.org_id or None, action, actor_id=ctx.actor_id, actor_type=ctx.actor_type,
          entity_type=entity_type, entity_id=entity_id, details=details, ip=ctx.ip, request_id=ctx.request_id)


# --------------------------------------------------------------------------- secrets vault
# Credentials are entered by admins only, encrypted at rest, never returned in
# plain text and never placed into an LLM prompt (PRD 143-144, 243).

SECRET_CATALOG = [
    {"key": "ANTHROPIC_API_KEY", "label": "Anthropic API key", "group": "AI models", "secret": True},
    {"key": "OPENAI_API_KEY", "label": "OpenAI API key", "group": "AI models", "secret": True},
    {"key": "RESEND_API_KEY", "label": "Resend API key (re_...)", "group": "Email - Resend (recommended)", "secret": True},
    {"key": "RESEND_WEBHOOK_SECRET", "label": "Resend webhook signing secret (whsec_...)", "group": "Email - Resend (recommended)", "secret": True},
    {"key": "RESEND_REPLY_TO", "label": "Reply-to address (your Resend receiving address, so replies flow back)", "group": "Email - Resend (recommended)", "secret": False},
    {"key": "SMTP_HOST", "label": "SMTP host", "group": "Email - SMTP (alternative)", "secret": False},
    {"key": "SMTP_PORT", "label": "SMTP port (587)", "group": "Email - SMTP (alternative)", "secret": False},
    {"key": "SMTP_USER", "label": "SMTP username", "group": "Email - SMTP (alternative)", "secret": False},
    {"key": "SMTP_PASSWORD", "label": "SMTP password / app password", "group": "Email - SMTP (alternative)", "secret": True},
    {"key": "SMTP_FROM", "label": "From address", "group": "Email - SMTP (alternative)", "secret": False},
    {"key": "WHATSAPP_TOKEN", "label": "WhatsApp Cloud API access token", "group": "WhatsApp", "secret": True},
    {"key": "WHATSAPP_PHONE_NUMBER_ID", "label": "WhatsApp phone number ID", "group": "WhatsApp", "secret": False},
    {"key": "TWILIO_ACCOUNT_SID", "label": "Twilio account SID (SMS/Voice)", "group": "SMS & Voice", "secret": False},
    {"key": "TWILIO_AUTH_TOKEN", "label": "Twilio auth token", "group": "SMS & Voice", "secret": True},
    {"key": "TWILIO_FROM", "label": "Twilio from number", "group": "SMS & Voice", "secret": False},
    {"key": "HUBSPOT_TOKEN", "label": "HubSpot private app token", "group": "CRM", "secret": True},
    {"key": "SLACK_WEBHOOK_URL", "label": "Slack incoming webhook URL", "group": "Notifications", "secret": True},
    {"key": "LEADOCEAN_API_KEY", "label": "LeadOcean API key (real B2B companies & people)", "group": "Data providers", "secret": True},
    {"key": "DATA_PROVIDER_API_KEY", "label": "External data provider API key", "group": "Data providers", "secret": True},
]
SECRET_KEYS = {s["key"] for s in SECRET_CATALOG}


def set_secret(db: Session, scope: str, org_id: str | None, key: str, value: str, user_id: str):
    if key not in SECRET_KEYS:
        raise APIError(400, "UNKNOWN_SECRET", f"Unknown credential {key}.")
    row = db.scalars(select(m.Secret).where(m.Secret.scope == scope, m.Secret.organization_id == org_id,
                                            m.Secret.key_name == key)).first()
    if not value:
        if row:
            db.delete(row)
        return
    if not row:
        row = m.Secret(scope=scope, organization_id=org_id, key_name=key, ciphertext="")
        db.add(row)
    row.ciphertext = encrypt(value)
    row.last4 = value[-4:] if len(value) > 6 else ""
    row.updated_by, row.updated_at = user_id, m.utcnow()


def get_secret(db: Session, org_id: str | None, key: str) -> str | None:
    """Resolution order: organization credential -> platform credential. Both are entered by
    admins in the encrypted vault; there is deliberately no code or env-var fallback."""
    if org_id:
        row = db.scalars(select(m.Secret).where(m.Secret.scope == "org", m.Secret.organization_id == org_id,
                                                m.Secret.key_name == key)).first()
        if row:
            return decrypt(row.ciphertext)
    row = db.scalars(select(m.Secret).where(m.Secret.scope == "platform", m.Secret.key_name == key)).first()
    if row:
        return decrypt(row.ciphertext)
    return None


def list_secrets(db: Session, scope: str, org_id: str | None) -> list[dict]:
    rows = {r.key_name: r for r in db.scalars(select(m.Secret).where(m.Secret.scope == scope,
                                                                     m.Secret.organization_id == org_id))}
    platform = set() if scope == "platform" else {
        r.key_name for r in db.scalars(select(m.Secret).where(m.Secret.scope == "platform"))}
    out = []
    for s in SECRET_CATALOG:
        r = rows.get(s["key"])
        out.append({**s, "configured": bool(r), "masked": ("••••••••" + r.last4) if r else "",
                    "updated_at": r.updated_at.isoformat() if r else None,
                    "inherited_from_platform": (not r) and s["key"] in platform})
    return out


# --------------------------------------------------------------------------- credits (GTU)

DEFAULT_PRICES = {
    # meter: (gtu, provider_cost_usd, category)
    "company_search": (1, 0.002, "data"), "people_search": (1, 0.003, "data"),
    "email_enrichment": (2, 0.01, "data"), "phone_enrichment": (5, 0.04, "data"),
    "email_verification": (0.5, 0.002, "data"), "research_quick": (1, 0.005, "research"),
    "research_standard": (3, 0.02, "research"), "research_deep": (10, 0.08, "research"),
    "ai_generation": (0.5, 0.004, "ai"), "ai_classification": (0.2, 0.001, "ai"),
    "email_send": (1, 0.008, "email"), "whatsapp_message": (3, 0.02, "whatsapp"),
    "sms_message": (3, 0.02, "sms"), "voice_minute": (10, 0.08, "voice"),
    "api_call": (0, 0, "api"),
}


class InsufficientCredits(APIError):
    def __init__(self, msg="Not enough GTM Units. Upgrade or buy additional usage."):
        super().__init__(402, "INSUFFICIENT_CREDITS", msg)


def price(db: Session, meter: str) -> m.MeterPrice:
    p = db.get(m.MeterPrice, meter)
    if not p:
        g, c, cat = DEFAULT_PRICES.get(meter, (1, 0, "data"))
        p = m.MeterPrice(meter_type=meter, gtu_cost=g, provider_cost_usd=c, category=cat)
        db.add(p)
        db.flush()
    return p


def credit_account(db: Session, org_id: str) -> m.CreditAccount:
    acc = db.get(m.CreditAccount, org_id)
    if not acc:
        acc = m.CreditAccount(organization_id=org_id, balance=0, monthly_allowance=0)
        db.add(acc)
        db.flush()
    return acc


def cost_of(db: Session, meter: str, qty: float = 1) -> float:
    return price(db, meter).gtu_cost * qty


def can_afford(db: Session, org_id: str, meter: str, qty: float = 1) -> bool:
    return credit_account(db, org_id).balance >= cost_of(db, meter, qty)


def meter(db: Session, org_id: str, meter_type: str, qty: float = 1, ws_id: str | None = None,
          ref: str = "", enforce: bool = True) -> float:
    """Spend control (PRD 90-93): check, deduct and record usage. No negative balance."""
    p = price(db, meter_type)
    cost = p.gtu_cost * qty
    acc = credit_account(db, org_id)
    if enforce and cost > 0 and acc.balance < cost:
        notify(db, org_id, "account_limit", "Credits exhausted",
               "Your GTM engine paused an action because GTM Units ran out. Upgrade or buy usage.", "#/billing")
        raise InsufficientCredits()
    before = acc.balance
    acc.balance = round(acc.balance - cost, 4)
    db.add(m.UsageEvent(organization_id=org_id, workspace_id=ws_id, meter_type=meter_type, category=p.category,
                        quantity=qty, provider_cost=p.provider_cost_usd * qty, customer_cost=cost, ref=ref))
    if cost:
        db.add(m.CreditTransaction(organization_id=org_id, amount=-cost, reason=f"{meter_type} x{qty:g}",
                                   meter_type=meter_type))
    allowance = acc.monthly_allowance or 1
    for pct, msg in ((0.8, "You're approaching your monthly limit."),
                     (0.95, "Your GTM engine may slow down soon."), (1.0, "Upgrade or buy additional usage.")):
        used_before = 1 - before / allowance
        used_after = 1 - acc.balance / allowance
        if used_before < pct <= used_after:
            notify(db, org_id, "account_limit", f"{int(pct * 100)}% of monthly GTU used", msg, "#/billing")
            emit(db, org_id, "usage.threshold_reached", "", {"percent": int(pct * 100)})
    return cost


def grant(db: Session, org_id: str, amount: float, reason: str):
    acc = credit_account(db, org_id)
    acc.balance = round(acc.balance + amount, 4)
    db.add(m.CreditTransaction(organization_id=org_id, amount=amount, reason=reason))


def entitled(db: Session, org: m.Organization, feature: str) -> bool:
    """Entitlement-driven features (PRD 252) - no hard-coded plan logic."""
    plan = db.scalars(select(m.Plan).where(m.Plan.code == org.plan_code)).first()
    return bool(plan and (plan.entitlements or {}).get(feature, False))


def require_entitlement(db: Session, org: m.Organization, feature: str):
    if not entitled(db, org, feature):
        raise APIError(402, "NOT_ENTITLED", f"Your plan does not include '{feature}'. Upgrade to enable it.")


def usage_summary(db: Session, org_id: str, days: int = 30) -> dict:
    since = m.utcnow() - timedelta(days=days)
    rows = db.execute(select(m.UsageEvent.category, func.sum(m.UsageEvent.customer_cost),
                             func.sum(m.UsageEvent.quantity), func.sum(m.UsageEvent.provider_cost))
                      .where(m.UsageEvent.organization_id == org_id, m.UsageEvent.created_at >= since)
                      .group_by(m.UsageEvent.category)).all()
    return {r[0]: {"gtu": round(r[1] or 0, 2), "quantity": round(r[2] or 0, 2), "provider_cost_usd": round(r[3] or 0, 4)}
            for r in rows}


# --------------------------------------------------------------------------- events / webhooks / notifications

WEBHOOK_EVENTS = ["lead.created", "lead.updated", "lead.hot", "lead.suppressed", "message.sent",
                  "message.delivered", "message.failed", "message.replied", "campaign.started",
                  "campaign.paused", "campaign.completed", "meeting.booked", "meeting.cancelled",
                  "usage.threshold_reached", "agent.escalation"]


def emit(db: Session, org_id: str, type_: str, entity_id: str = "", data: dict | None = None,
         ws_id: str | None = None, campaign_id: str | None = None):
    data = data or {}
    db.add(m.DomainEvent(organization_id=org_id, workspace_id=ws_id, type=type_, entity_id=entity_id,
                         campaign_id=campaign_id, data=data))
    if type_ in WEBHOOK_EVENTS:
        for ep in db.scalars(select(m.WebhookEndpoint).where(m.WebhookEndpoint.organization_id == org_id,
                                                             m.WebhookEndpoint.active.is_(True))):
            if not ep.events or type_ in ep.events:
                db.add(m.WebhookDelivery(organization_id=org_id, endpoint_id=ep.id, event=type_,
                                         payload={"event": type_, "entity_id": entity_id, "data": data,
                                                  "created_at": m.utcnow().isoformat()}))


def notify(db: Session, org_id: str, kind: str, title: str, body: str = "", link: str = ""):
    db.add(m.Notification(organization_id=org_id, kind=kind, title=title, body=body, link=link))


def decide(db: Session, org_id: str, agent: str, action: str, reason: str, *, entity_type: str = "",
           entity_id: str = "", confidence: float = 0.8, policy_result: str = "ALLOW",
           decision: str = "executed", evidence: list | None = None, model: str = "rules"):
    """Every autonomous action has an auditable reason (PRD 136, 317)."""
    db.add(m.AIDecision(organization_id=org_id, agent=agent, action=action, reason=reason,
                        entity_type=entity_type, entity_id=entity_id, confidence=confidence,
                        policy_result=policy_result, decision=decision, evidence=evidence or [], model=model))
