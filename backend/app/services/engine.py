"""Execution engine: compliance, channel layer, delivery scheduler, reply agent,
booking agent, CRM sync, optimisation agent, daily brief."""
import hashlib
import hmac
import json
import logging
import random
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models as m
from ..config import SANDBOX_TIME_COMPRESSION
from ..security import decrypt
from . import core, llm
from .intelligence import generate_message, upsert_person
from .nlp import (HOT, NEGATIVE, SAFE_AUTO, classify_reply, escalation_reasons, extract_day,
                  extract_referral, extract_return_date)

log = logging.getLogger("gtm.engine")

DEFAULT_POLICY = {
    "auto_reply_confidence": 0.7, "sendable_email_statuses": ["VALID", "CATCH_ALL"],
    "recontact_days": 14, "blocked_countries": [], "review_countries": [],
    "send_window": {"days": [0, 1, 2, 3, 4], "start_hour": 9, "end_hour": 19},
    "autopilot": {"find_audiences": True, "create_campaigns": True, "reallocate_budget": True,
                  "change_messaging": True, "handle_replies": True, "book_meetings": True, "update_crm": True,
                  "create_offer": False, "change_pricing": False},
    "budget_change_autonomous_limit": 100.0,   # GTU/day; above -> human approval (PRD 190)
    "booking": {"duration": 30, "start_hour": 10, "end_hour": 18, "days": [0, 1, 2, 3, 4], "buffer": 15,
                "location": "Google Meet (link sent on confirmation)"},
    "sandbox_auto_replies": False,
}


def ws_policy(ws: m.Workspace | None) -> dict:
    s = (ws.settings or {}).get("policy", {}) if ws else {}
    out = json.loads(json.dumps(DEFAULT_POLICY))
    for k, v in s.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def local_now(tz: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(tz)).replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        return datetime.now()


def step_delay(days: float) -> timedelta:
    # Sandbox time compression: 1 sequence day == 1 minute, so the full loop can be demoed.
    return timedelta(minutes=days) if SANDBOX_TIME_COMPRESSION else timedelta(days=days)


# ------------------------------------------------------------------ suppression (PRD 63-64)
def is_suppressed(db: Session, org_id: str, ws_id: str | None, p: m.Person, channel: str = "all",
                  campaign_id: str | None = None) -> str | None:
    comp = db.get(m.Company, p.company_id) if p.company_id else None
    values = {("person", p.id)}
    if p.email:
        values.add(("email", p.email.lower()))
        values.add(("domain", p.email.split("@")[-1].lower()))
    if comp and comp.domain:
        values.add(("domain", comp.domain.lower()))
    if p.phone:
        values.add(("phone", p.phone))
    if p.linkedin_url:
        values.add(("linkedin", p.linkedin_url))
    rows = db.scalars(select(m.SuppressionEntry).where(m.SuppressionEntry.organization_id == org_id,
                                                       m.SuppressionEntry.value.in_([v for _, v in values])))
    for r in rows:
        if (r.kind, r.value) not in values:
            continue
        if r.workspace_id and r.workspace_id != ws_id:
            continue
        if r.campaign_id and r.campaign_id != campaign_id:
            continue
        if r.channel not in ("all", channel) and channel != "all":
            continue
        return f"{r.kind} {r.value} suppressed ({r.reason or 'policy'})"
    return None


def suppress_person(db: Session, org_id: str, p: m.Person, channel: str, reason: str, by: str = "agent"):
    if p.email and channel in ("email", "all"):
        db.add(m.SuppressionEntry(organization_id=org_id, kind="email", value=p.email.lower(), channel="all"
                                  if channel == "all" else "email", reason=reason, created_by=by))
    if p.phone and channel in ("whatsapp", "sms", "voice", "all"):
        db.add(m.SuppressionEntry(organization_id=org_id, kind="phone", value=p.phone, channel=channel, reason=reason,
                                  created_by=by))
    db.add(m.SuppressionEntry(organization_id=org_id, kind="person", value=p.id, channel=channel, reason=reason,
                              created_by=by))
    p.stage = "Suppressed"
    stop_followups(db, p.id, f"suppressed: {reason}")
    core.emit(db, org_id, "lead.suppressed", p.id, {"reason": reason, "channel": channel})


def stop_followups(db: Session, person_id: str, reason: str, status: str = "stopped"):
    for cl in db.scalars(select(m.CampaignLead).where(m.CampaignLead.person_id == person_id,
                                                      m.CampaignLead.status.in_(["pending", "active"]))):
        cl.status, cl.stop_reason, cl.next_action_at = status, reason[:80], None


# ------------------------------------------------------------------ compliance / delivery safety (PRD 146, 152)
CHANNEL_METER = {"email": "email_send", "whatsapp": "whatsapp_message", "sms": "sms_message", "voice": "voice_minute",
                 "linkedin": None}
CHANNEL_FEATURE = {"whatsapp": "whatsapp_outreach", "sms": "sms_outreach", "voice": "voice_outreach",
                   "linkedin": "linkedin_assistant"}


def check_send(db: Session, org: m.Organization, ws: m.Workspace | None, camp: m.Campaign | None, p: m.Person,
               channel: str) -> tuple[str, str]:
    pol = ws_policy(ws)
    reason = is_suppressed(db, org.id, ws.id if ws else None, p, channel, camp.id if camp else None)
    if reason:
        return "BLOCK", reason
    if channel in CHANNEL_FEATURE and not core.entitled(db, org, CHANNEL_FEATURE[channel]):
        return "BLOCK", f"plan does not include {channel}"
    if channel == "email":
        if not p.email:
            return "BLOCK", "no email address"
        if p.email_status not in pol["sendable_email_statuses"]:
            return "BLOCK", f"email status {p.email_status} not sendable by policy"
    if channel in ("whatsapp", "sms", "voice") and not p.phone:
        return "BLOCK", "no phone number"
    if channel == "whatsapp" and not (p.custom_fields or {}).get("whatsapp_opt_in") and p.source != "demo_people":
        return "BLOCK", "WhatsApp requires recipient opt-in for business-initiated messages"
    if channel == "sms" and not (p.custom_fields or {}).get("sms_consent") and p.source != "demo_people":
        return "BLOCK", "SMS requires recorded consent / registered template"
    if p.country and p.country in pol["blocked_countries"]:
        return "BLOCK", f"outreach to {p.country} blocked by workspace policy"
    if p.country and p.country in pol["review_countries"]:
        return "REVIEW", f"{p.country} requires human review (jurisdiction policy)"
    if camp:
        recent = db.scalars(select(m.Message).join(m.Conversation).where(
            m.Conversation.person_id == p.id, m.Message.direction == "outbound",
            m.Message.campaign_id != camp.id, m.Message.status.in_(["sent", "delivered"]),
            m.Message.created_at >= m.utcnow() - timedelta(days=pol["recontact_days"]))).first()
        if recent:
            return "BLOCK", f"contacted by another campaign within {pol['recontact_days']} days"
        meter = CHANNEL_METER.get(channel)
        cost = core.cost_of(db, meter) if meter else 0
        if camp.budget_total and camp.spent + cost > camp.budget_total:
            return "BUDGET", "campaign budget exhausted"
        if meter and not core.can_afford(db, org.id, meter):
            return "BUDGET", "organization credits exhausted"
    return "ALLOW", "all checks passed"


# ------------------------------------------------------------------ channel layer (PRD 49-60)
class SendResult(dict):
    pass


def _is_demo(p: m.Person) -> bool:
    return (p.source or "").startswith("demo") or (p.email or "").endswith(".demo") or "DEMO" in (p.phone or "")


def _sandbox(channel: str) -> SendResult:
    return SendResult(status="delivered", provider_message_id=f"sbx_{channel}_{random.getrandbits(40):x}", mode="sandbox")


class PermanentSendError(Exception):
    """Provider rejected the message for a reason a retry won't fix (bad address, unverified domain...)."""


RESEND_API = "https://api.resend.com"


def email_mode(db: Session, org_id: str) -> str:
    """Which real email provider is configured for this org: resend (preferred) | smtp | sandbox."""
    if core.get_secret(db, org_id, "RESEND_API_KEY"):
        return "resend"
    if core.get_secret(db, org_id, "SMTP_HOST"):
        return "smtp"
    return "sandbox"


def _sender(org: m.Organization, address: str) -> str:
    name = ((org.settings or {}).get("sender_name") or "").replace('"', "").strip()
    return f"{name} <{address}>" if name else address


def send_resend(db, org, mailbox: m.Mailbox, p: m.Person, subject: str, body: str,
                idempotency_key: str | None = None, tags: dict | None = None) -> SendResult:
    """Resend Emails API. The platform's per-message idempotency key is passed through, so a
    retry after a crash can never produce a second email (PRD 124, 277)."""
    key = core.get_secret(db, org.id, "RESEND_API_KEY")
    if not key:
        raise PermanentSendError("Resend API key is not configured (Integrations > Credential vault)")
    reply_to = core.get_secret(db, org.id, "RESEND_REPLY_TO") or mailbox.address
    payload = {
        "from": _sender(org, mailbox.address), "to": [p.email], "subject": subject or "(no subject)",
        "text": body + "\n\n--\nIf you'd prefer not to hear from us, just reply STOP.",
        "reply_to": reply_to,
        "headers": {"List-Unsubscribe": f"<mailto:{reply_to}?subject=unsubscribe>"},
        "tags": [{"name": k, "value": "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in str(v))[:256]}
                 for k, v in (tags or {}).items() if v],
    }
    headers = {"Authorization": f"Bearer {key}"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key[:256]
    r = httpx.post(f"{RESEND_API}/emails", json=payload, headers=headers, timeout=20)
    if r.status_code == 429 or r.status_code >= 500:
        r.raise_for_status()   # temporary -> retried later with the same idempotency key
    if r.status_code >= 400:
        try:
            detail = r.json().get("message") or r.text
        except ValueError:
            detail = r.text
        raise PermanentSendError(f"Resend {r.status_code}: {detail}"[:240])
    return SendResult(status="sent", provider_message_id=r.json().get("id", ""), mode="resend")


def send_email(db, org, mailbox: m.Mailbox, p: m.Person, subject: str, body: str,
               idempotency_key: str | None = None, tags: dict | None = None) -> SendResult:
    # Demo/synthetic records and sandbox mailboxes never leave the platform.
    if _is_demo(p) or mailbox.provider == "sandbox":
        return _sandbox("email")
    if mailbox.provider == "resend":
        return send_resend(db, org, mailbox, p, subject, body, idempotency_key, tags)
    host = core.get_secret(db, org.id, "SMTP_HOST")
    if not host:
        raise PermanentSendError("SMTP is not configured")
    msg = EmailMessage()
    msg["From"] = core.get_secret(db, org.id, "SMTP_FROM") or mailbox.address
    msg["To"], msg["Subject"] = p.email, subject
    msg.set_content(body + "\n\n--\nIf you'd prefer not to hear from us, reply STOP.")
    with smtplib.SMTP(host, int(core.get_secret(db, org.id, "SMTP_PORT") or 587), timeout=20) as s:
        s.starttls()
        user = core.get_secret(db, org.id, "SMTP_USER")
        if user:
            s.login(user, core.get_secret(db, org.id, "SMTP_PASSWORD") or "")
        s.send_message(msg)
    return SendResult(status="sent", provider_message_id=msg.get("Message-ID", ""), mode="smtp")


def send_whatsapp(db, org, p: m.Person, body: str) -> SendResult:
    token, phone_id = core.get_secret(db, org.id, "WHATSAPP_TOKEN"), core.get_secret(db, org.id, "WHATSAPP_PHONE_NUMBER_ID")
    if _is_demo(p):
        return _sandbox("whatsapp")
    if not token or not phone_id:
        raise PermanentSendError("WhatsApp is not connected (add WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID)")
    r = httpx.post(f"https://graph.facebook.com/v20.0/{phone_id}/messages", timeout=20,
                   headers={"Authorization": f"Bearer {token}"},
                   json={"messaging_product": "whatsapp", "to": p.phone.replace("+", "").replace("-", ""),
                         "type": "text", "text": {"body": body}})
    r.raise_for_status()
    return SendResult(status="sent", provider_message_id=r.json().get("messages", [{}])[0].get("id", ""), mode="whatsapp_cloud")


def send_sms(db, org, p: m.Person, body: str) -> SendResult:
    sid, tok, frm = (core.get_secret(db, org.id, k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM"))
    if _is_demo(p):
        return _sandbox("sms")
    if not (sid and tok and frm):
        raise PermanentSendError("SMS is not connected (add the Twilio credentials)")
    r = httpx.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", auth=(sid, tok), timeout=20,
                   data={"To": p.phone, "From": frm, "Body": body})
    r.raise_for_status()
    return SendResult(status="sent", provider_message_id=r.json().get("sid", ""), mode="twilio")


def send_voice(db, org, p: m.Person, script: str) -> SendResult:
    # Real telephony is not built yet: never pretend a real person was called.
    if _is_demo(p):
        return _sandbox("voice")
    raise PermanentSendError("Voice calling is not available yet; remove the voice step or call manually")


def pick_mailbox(db: Session, org_id: str, real_recipient: bool = True) -> m.Mailbox | None:
    """Real (Resend/SMTP) mailboxes serve real recipients first; sandbox boxes serve demo records."""
    today = m.utcnow().strftime("%Y-%m-%d")
    boxes = list(db.scalars(select(m.Mailbox).where(m.Mailbox.organization_id == org_id,
                                                    m.Mailbox.status.in_(["HEALTHY", "WARMING"]))))
    for b in boxes:
        if b.counter_date != today:
            b.counter_date, b.sent_today = today, 0
    boxes = [b for b in boxes if b.sent_today < b.daily_limit]
    if real_recipient:
        # Never "fake-send" to a real person: only real (Resend/SMTP) mailboxes may serve them.
        boxes = [b for b in boxes if b.provider != "sandbox"]
    else:
        boxes = [b for b in boxes if b.provider == "sandbox"] or boxes
    return min(boxes, key=lambda b: b.sent_today, default=None)


def update_mailbox_health(b: m.Mailbox):
    """PRD 52: 100 - bounce - complaint penalties + reply bonus; degraded boxes stop receiving work."""
    sent = max(20, b.sent_total)   # rates over a minimum sample so one early bounce can't kill a mailbox
    score = 100 - (b.bounces / sent) * 400 - (b.complaints / sent) * 2000 + min(10, (b.replies / sent) * 100)
    b.health_score = round(max(0, min(100, score)), 1)
    if b.status != "PAUSED":
        b.status = "HEALTHY" if b.health_score >= 70 else "DEGRADED"


def get_conversation(db: Session, org_id: str, ws_id: str | None, p: m.Person, channel: str,
                     campaign_id: str | None) -> m.Conversation:
    conv = db.scalars(select(m.Conversation).where(m.Conversation.person_id == p.id,
                                                   m.Conversation.organization_id == org_id)
                      .order_by(m.Conversation.created_at)).first()
    if not conv:   # one omnichannel conversation per person (PRD 61)
        conv = m.Conversation(organization_id=org_id, workspace_id=ws_id, person_id=p.id, company_id=p.company_id,
                              campaign_id=campaign_id, channel=channel)
        db.add(conv)
        db.flush()
    return conv


def deliver(db: Session, org: m.Organization, ws: m.Workspace | None, p: m.Person, channel: str, subject: str,
            body: str, *, campaign: m.Campaign | None = None, idempotency_key: str | None = None, step: int = 0,
            ai_generated: bool = False, model: str = "", confidence: float = 0, evidence: list | None = None,
            conv: m.Conversation | None = None, enforce_policy: bool = True) -> m.Message:
    """All outbound communication originates here, server-side (PRD 311, 277)."""
    retry = None
    if idempotency_key:
        dup = db.scalars(select(m.Message).where(m.Message.idempotency_key == idempotency_key)).first()
        if dup and not (dup.status in ("failed", "deferred") and (dup.error or "").startswith(("temporary", "no healthy"))
                        and (dup.attempts or 0) < 5):
            return dup   # already sent / permanently failed: never send twice
        retry = dup      # temporary failure: retry the SAME row with the SAME idempotency key
    conv = conv or get_conversation(db, org.id, ws.id if ws else None, p, channel, campaign.id if campaign else None)
    msg = retry or m.Message(organization_id=org.id, conversation_id=conv.id, campaign_id=campaign.id if campaign else None,
                    channel=channel, direction="outbound", recipient=p.email if channel == "email" else p.phone,
                    subject=subject, content=body, idempotency_key=idempotency_key, step=step, ai_generated=ai_generated,
                    ai_model=model, ai_confidence=confidence, evidence=evidence or [], status="queued")
    if not retry:
        db.add(msg)
    msg.attempts = (msg.attempts or 0) + 1
    msg.status, msg.error = "queued", ""
    if enforce_policy:
        verdict, reason = check_send(db, org, ws, campaign, p, channel)
        if verdict != "ALLOW":
            msg.status, msg.error = "blocked", f"{verdict}: {reason}"[:255]
            core.decide(db, org.id, "compliance", f"block_{channel}", reason, entity_type="person", entity_id=p.id,
                        policy_result="BLOCK" if verdict == "BLOCK" else verdict, decision="blocked")
            db.commit()
            return msg
    db.commit()   # persist intent + idempotency key BEFORE calling the provider
    try:
        if channel == "email":
            box = pick_mailbox(db, org.id, real_recipient=not _is_demo(p))
            if not box:
                msg.status, msg.error = "deferred", ("no healthy mailbox with capacity - add a Resend sending mailbox "
                                                     "in Integrations > Email (Resend)")
                db.commit()
                return msg
            msg.sender = box.address
            res = send_email(db, org, box, p, subject, body, idempotency_key=idempotency_key or msg.id,
                             tags={"campaign": campaign.id if campaign else "", "org": org.id})
            box.sent_today += 1
            box.sent_total += 1
            update_mailbox_health(box)
        elif channel == "whatsapp":
            res = send_whatsapp(db, org, p, body)
        elif channel == "sms":
            res = send_sms(db, org, p, body)
        elif channel == "voice":
            res = send_voice(db, org, p, body)
        elif channel == "linkedin":
            db.add(m.LinkedInTask(organization_id=org.id, workspace_id=ws.id if ws else None, person_id=p.id,
                                  campaign_id=campaign.id if campaign else None, action="connect",
                                  reason=f"ICP {p.icp_score} / intent {p.intent_score}", suggested_message=body))
            res = SendResult(status="task_created", provider_message_id="", mode="task")
        else:
            raise ValueError(f"unknown channel {channel}")
        msg.status, msg.provider_message_id = res["status"], res.get("provider_message_id", "")
        msg.sent_at = m.utcnow()
        meter = CHANNEL_METER.get(channel)
        if meter and res["status"] != "task_created":
            cost = core.meter(db, org.id, meter, 1, ws_id=ws.id if ws else None, ref=msg.id, enforce=False)
            if campaign:
                campaign.spent = round((campaign.spent or 0) + cost, 4)
        conv.last_message_at = m.utcnow()
        if p.stage in ("New", ""):
            p.stage = "Contacted"
        core.emit(db, org.id, "message.sent", msg.id, {"channel": channel, "person_id": p.id, "mode": res.get("mode")},
                  ws_id=ws.id if ws else None, campaign_id=campaign.id if campaign else None)
    except PermanentSendError as exc:
        msg.status, msg.error = "failed", f"permanent: {exc}"[:255]
        core.emit(db, org.id, "message.failed", msg.id, {"error": str(exc)[:200]},
                  campaign_id=campaign.id if campaign else None)
    except Exception as exc:  # noqa: BLE001 - network/429/5xx: temporary, retried with the same key
        msg.status, msg.error = "failed", f"temporary: {exc}"[:255]
        core.emit(db, org.id, "message.failed", msg.id, {"error": str(exc)[:200]},
                  campaign_id=campaign.id if campaign else None)
    db.commit()
    return msg


# ------------------------------------------------------------------ campaign delivery scheduler (PRD 169-173)
def in_send_window(pol: dict, tz: str) -> bool:
    if SANDBOX_TIME_COMPRESSION:
        return True
    now = local_now(tz)
    w = pol["send_window"]
    return now.weekday() in w["days"] and w["start_hour"] <= now.hour < w["end_hour"]


def start_campaign(db: Session, camp: m.Campaign):
    camp.status, camp.started_at = "RUNNING", camp.started_at or m.utcnow()
    for cl in db.scalars(select(m.CampaignLead).where(m.CampaignLead.campaign_id == camp.id,
                                                      m.CampaignLead.status == "pending")):
        cl.status, cl.next_action_at = "active", m.utcnow()
    core.emit(db, camp.organization_id, "campaign.started", camp.id, {"name": camp.name}, campaign_id=camp.id)


def process_campaign(db: Session, camp: m.Campaign, max_sends: int = 25) -> int:
    org = db.get(m.Organization, camp.organization_id)
    ws = db.get(m.Workspace, camp.workspace_id) if camp.workspace_id else None
    if not org or org.status != "active":
        return 0
    pol = ws_policy(ws)
    if not in_send_window(pol, ws.timezone if ws else "Asia/Kolkata"):
        return 0
    today = m.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    sent_today = db.scalar(select(func.count(m.Message.id)).where(
        m.Message.campaign_id == camp.id, m.Message.direction == "outbound", m.Message.created_at >= today,
        m.Message.status.in_(["sent", "delivered"]))) or 0
    room = max(0, (camp.daily_send_limit or 50) - sent_today)
    if camp.budget_daily and not SANDBOX_TIME_COMPRESSION:
        spent_today = db.scalar(select(func.sum(m.UsageEvent.customer_cost)).join(
            m.Message, m.Message.id == m.UsageEvent.ref).where(m.Message.campaign_id == camp.id,
                                                              m.UsageEvent.created_at >= today)) or 0
        if spent_today >= camp.budget_daily:
            return 0
    due = list(db.scalars(select(m.CampaignLead).where(
        m.CampaignLead.campaign_id == camp.id, m.CampaignLead.status == "active",
        m.CampaignLead.next_action_at <= m.utcnow()).order_by(m.CampaignLead.next_action_at).limit(min(room, max_sends))))
    sent = 0
    for cl in due:
        if cl.current_step >= len(camp.sequence or []):
            cl.status, cl.next_action_at = "completed", None
            continue
        step = dict(camp.sequence[cl.current_step])
        step["variant"] = cl.variant
        p = db.get(m.Person, cl.person_id)
        comp = db.get(m.Company, p.company_id) if p and p.company_id else None
        if not p:
            cl.status = "stopped"
            continue
        if step["channel"] == "email" and p.email_status in ("UNKNOWN", "") :
            from .intelligence import enrich_person
            try:
                enrich_person(db, org, ws.id if ws else None, p)
            except core.InsufficientCredits:
                camp.status = "BUDGET_EXHAUSTED"
                core.notify(db, org.id, "campaign_paused", f"{camp.name} paused", "Credits exhausted", f"#/campaigns/{camp.id}")
                break
        history = []
        conv = db.scalars(select(m.Conversation).where(m.Conversation.person_id == p.id)).first()
        if conv:
            history = list(db.scalars(select(m.Message).where(m.Message.conversation_id == conv.id)
                                      .order_by(m.Message.created_at)))
        content = generate_message(db, org, camp, p, comp, step, cl.current_step, history)
        msg = deliver(db, org, ws, p, step["channel"], content["subject"], content["body"], campaign=camp,
                      idempotency_key=f"{cl.id}:{cl.current_step}", step=cl.current_step, ai_generated=True,
                      model=content["model"], confidence=0.85, evidence=content["evidence"])
        if msg.status == "blocked" and msg.error.startswith("BUDGET"):
            camp.status = "BUDGET_EXHAUSTED"
            core.notify(db, org.id, "campaign_paused", f"{camp.name}: budget exhausted", msg.error, f"#/campaigns/{camp.id}")
            core.emit(db, org.id, "campaign.paused", camp.id, {"reason": "budget"}, campaign_id=camp.id)
            break
        if msg.status == "blocked":
            cl.status, cl.stop_reason, cl.next_action_at = "blocked", msg.error[:80], None
            continue
        if msg.status == "failed" and msg.error.startswith("permanent"):
            cl.status, cl.stop_reason, cl.next_action_at = "stopped", msg.error[:80], None
            continue
        if msg.status in ("deferred", "failed"):
            if (msg.attempts or 0) >= 5:
                cl.status, cl.stop_reason, cl.next_action_at = "stopped", "send failed after 5 attempts", None
            else:
                cl.next_action_at = m.utcnow() + timedelta(minutes=10 * (msg.attempts or 1))   # backoff
            continue
        sent += 1
        cl.current_step += 1
        if cl.current_step < len(camp.sequence):
            gap = camp.sequence[cl.current_step]["day"] - step["day"]
            cl.next_action_at = m.utcnow() + step_delay(max(gap, 0.5))
        else:
            cl.status, cl.next_action_at = "completed", None
        if pol.get("sandbox_auto_replies") and _is_demo(p) and step["channel"] != "linkedin":
            _maybe_schedule_simulated_reply(db, org, p, msg, cl)
        db.commit()
    remaining = db.scalar(select(func.count(m.CampaignLead.id)).where(
        m.CampaignLead.campaign_id == camp.id, m.CampaignLead.status.in_(["pending", "active"])))
    total = db.scalar(select(func.count(m.CampaignLead.id)).where(m.CampaignLead.campaign_id == camp.id))
    if camp.status == "RUNNING" and total and not remaining:
        blocked = db.scalar(select(func.count(m.CampaignLead.id)).where(
            m.CampaignLead.campaign_id == camp.id, m.CampaignLead.status == "blocked")) or 0
        if blocked == total:
            # Nothing could legally/safely be sent - surface it instead of silently "completing".
            camp.status = "COMPLIANCE_PAUSED"
            core.notify(db, org.id, "campaign_paused", f"{camp.name} needs attention",
                        "Every lead was blocked by policy (suppression, duplicates or missing contact data).",
                        f"#/campaigns/{camp.id}")
        else:
            camp.status = "COMPLETED"
            core.emit(db, org.id, "campaign.completed", camp.id, {}, campaign_id=camp.id)
    db.commit()
    return sent


# Sandbox prospects reply with a realistic mix so the full loop can be observed end to end.
SIMULATED_REPLIES = [
    (0.20, "This sounds interesting. Can we talk Friday afternoon?"),
    (0.10, "We're currently evaluating CRM solutions. What would the implementation timeline look like?"),
    (0.10, "Please send pricing."),
    (0.12, "Not interested, thanks."),
    (0.10, "I'm out of office until the 2nd of next month with limited access to email."),
    (0.08, "Speak to Priya Nair in our operations team, she handles this."),
    (0.10, "Not right now - maybe next quarter."),
    (0.05, "STOP"),
    (0.05, "Interesting. Tell me more - how does this work for a team our size?"),
    (0.10, "Sure, let's set up a call. Tuesday 11am works for me."),
]


def _maybe_schedule_simulated_reply(db, org, p, msg, cl):
    rnd = random.Random(f"{p.id}:{cl.current_step}")
    if rnd.random() > 0.30:
        return
    r, acc = rnd.random(), 0
    text = SIMULATED_REPLIES[-1][1]
    for w, t in SIMULATED_REPLIES:
        acc += w
        if r <= acc:
            text = t
            break
    db.add(m.Job(organization_id=org.id, workspace_id=p.workspace_id, type="simulated_reply", priority=0,
                 payload={"person_id": p.id, "text": text, "channel": msg.channel,
                          "run_after": (m.utcnow() + timedelta(seconds=rnd.randint(20, 90))).isoformat()}))


# ------------------------------------------------------------------ booking agent (PRD 78-81)
def busy_slots(db: Session, ws_id: str | None) -> list[tuple[datetime, datetime]]:
    return [(x.start_at, x.end_at) for x in db.scalars(select(m.Meeting).where(
        m.Meeting.workspace_id == ws_id, m.Meeting.status.in_(["booked", "proposed"]),
        m.Meeting.start_at >= m.utcnow() - timedelta(days=1)))]


def available_slots(db: Session, ws: m.Workspace | None, day: datetime | None = None, part: str = "",
                    n: int = 3) -> list[datetime]:
    pol = ws_policy(ws)["booking"]
    tz = ws.timezone if ws else "Asia/Kolkata"
    now = local_now(tz)
    busy = busy_slots(db, ws.id if ws else None)
    dur, buf = timedelta(minutes=pol["duration"]), timedelta(minutes=pol["buffer"])
    start_day = (day or now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    lo, hi = pol["start_hour"], pol["end_hour"]
    if part == "morning":
        hi = min(hi, 12)
    elif part == "afternoon":
        lo = max(lo, 13)
    elif part == "evening":
        lo = max(lo, 16)
    out = []
    for d in range(0, 14):
        date = start_day + timedelta(days=d)
        if date.weekday() not in pol["days"]:
            continue
        t = date.replace(hour=lo)
        while t + dur <= date.replace(hour=hi) and len(out) < n:
            if t > now + timedelta(hours=2) and all(t + dur + buf <= b0 or t >= b1 + buf for b0, b1 in busy):
                out.append(t)
                t += timedelta(hours=2)   # spread suggestions
            else:
                t += timedelta(minutes=30)
        if len(out) >= n or (day and d == 0 and out):
            break
    return out


def book_meeting(db: Session, org: m.Organization, ws: m.Workspace | None, p: m.Person, start: datetime,
                 conv: m.Conversation | None = None, booked_by: str = "agent") -> m.Meeting:
    pol = ws_policy(ws)["booking"]
    camp = db.get(m.Campaign, conv.campaign_id) if conv and conv.campaign_id else None
    comp = db.get(m.Company, p.company_id) if p.company_id else None
    mtg = m.Meeting(organization_id=org.id, workspace_id=ws.id if ws else None, person_id=p.id,
                    conversation_id=conv.id if conv else None, campaign_id=camp.id if camp else None,
                    mission_id=camp.mission_id if camp else None,
                    title=f"Discovery call: {p.full_name}" + (f" ({comp.name})" if comp else ""),
                    start_at=start, end_at=start + timedelta(minutes=pol["duration"]),
                    timezone=ws.timezone if ws else "Asia/Kolkata", location=pol["location"], booked_by=booked_by)
    db.add(mtg)
    p.stage, p.temperature = "Meeting Booked", "hot"
    stop_followups(db, p.id, "meeting booked", "completed")
    db.flush()
    core.emit(db, org.id, "meeting.booked", mtg.id, {"person_id": p.id, "start_at": start.isoformat()},
              ws_id=mtg.workspace_id, campaign_id=mtg.campaign_id)
    core.notify(db, org.id, "meeting_booked", f"Meeting booked: {p.full_name}",
                f"{start.strftime('%a %d %b, %I:%M %p')} ({mtg.timezone})", "#/meetings")
    core.decide(db, org.id, "booking", "book_meeting", f"Prospect agreed to {start.strftime('%a %d %b %I:%M %p')}",
                entity_type="person", entity_id=p.id, confidence=0.9)
    queue_crm_sync(db, org.id, p.id, "Meeting booked")
    return mtg


# ------------------------------------------------------------------ CRM (PRD 82-83)
def queue_crm_sync(db: Session, org_id: str, person_id: str, stage: str):
    db.add(m.Job(organization_id=org_id, type="sync_crm", priority=2, payload={"person_id": person_id, "stage": stage}))


def sync_crm(db: Session, org_id: str, person_id: str, stage: str) -> dict:
    p = db.get(m.Person, person_id)
    if not p:
        return {"skipped": "person missing"}
    rec = db.scalars(select(m.CrmRecord).where(m.CrmRecord.organization_id == org_id,
                                               m.CrmRecord.person_id == person_id)).first()
    if not rec:
        rec = m.CrmRecord(organization_id=org_id, person_id=person_id)
        db.add(rec)
    rec.deal_stage = stage
    token = core.get_secret(db, org_id, "HUBSPOT_TOKEN")
    if token and p.email and not _is_demo(p):
        comp = db.get(m.Company, p.company_id) if p.company_id else None
        headers = {"Authorization": f"Bearer {token}"}
        props = {"email": p.email, "firstname": p.first_name, "lastname": p.last_name, "jobtitle": p.title,
                 "company": comp.name if comp else "", "lifecyclestage": "salesqualifiedlead"}
        try:
            s = httpx.post("https://api.hubapi.com/crm/v3/objects/contacts/search", headers=headers, timeout=15,
                           json={"filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": p.email}]}]})
            found = s.json().get("results", []) if s.status_code == 200 else []
            if found:
                r = httpx.patch(f"https://api.hubapi.com/crm/v3/objects/contacts/{found[0]['id']}", headers=headers,
                                json={"properties": props}, timeout=15)
                rec.external_id = found[0]["id"]
            else:
                r = httpx.post("https://api.hubapi.com/crm/v3/objects/contacts", headers=headers,
                               json={"properties": props}, timeout=15)
                rec.external_id = r.json().get("id", "")
            r.raise_for_status()
            rec.external_system, rec.sync_status, rec.last_error = "hubspot", "synced", ""
        except Exception as exc:  # noqa: BLE001
            rec.sync_status, rec.last_error = "failed", str(exc)[:250]
            raise
    else:
        rec.external_system, rec.sync_status = "internal", "synced"
    return {"stage": stage, "system": rec.external_system}


# ------------------------------------------------------------------ reply agent (PRD 70-77, 172-177)
def _reply_text(org, p, intent, slots, offer, ret_date=None, referral=None) -> str:
    fn = p.first_name or "there"
    sender = (org.settings or {}).get("sender_name") or "Team " + org.name.split()[0]
    fmt = lambda d: d.strftime("%A %d %b, %I:%M %p")  # noqa: E731
    if intent in ("MEETING_REQUEST", "VERY_INTERESTED", "INTERESTED") and slots:
        opts = "\n".join(f"- {fmt(s)}" for s in slots)
        return f"Hi {fn},\n\nGreat to hear from you. Here are a few times that work on our side:\n{opts}\n\nReply with the one that suits you and I'll send the invite.\n\n{sender}"
    if intent == "PRICING":
        price = offer.pricing if offer and offer.pricing else ""
        base = f"Pricing depends on scope; {price}." if price else "Pricing depends on the workflows in scope, so we usually scope it on a short call."
        tail = ("\n\nWould one of these times work for a 30-minute scoping call?\n" + "\n".join(f"- {fmt(s)}" for s in slots)) if slots else ""
        return f"Hi {fn},\n\nThanks for asking. {base}{tail}\n\n{sender}"
    if intent == "QUESTION":
        return (f"Hi {fn},\n\nGood question. The short answer is that we start with one high-impact workflow and expand "
                f"from there. The easiest way to answer properly is a quick call - would one of these work?\n"
                + "\n".join(f"- {fmt(s)}" for s in slots) + f"\n\n{sender}")
    if intent == "NOT_INTERESTED":
        return f"Hi {fn},\n\nUnderstood, thanks for letting me know. I won't follow up further.\n\n{sender}"
    if intent == "NOT_NOW":
        return f"Hi {fn},\n\nNo problem at all. I'll check back in a couple of months.\n\n{sender}"
    if intent == "WRONG_PERSON":
        return f"Hi {fn},\n\nThanks, and apologies for the misdirect. Who would be the best person to speak with about this?\n\n{sender}"
    if intent == "REFERRAL" and referral:
        return f"Hi {fn},\n\nThank you - I'll reach out to {referral.get('first_name')} directly.\n\n{sender}"
    if intent == "MEETING_CONFIRMED":
        return f"Hi {fn},\n\nConfirmed - calendar invite on its way. Looking forward to it.\n\n{sender}"
    return ""


def handle_inbound(db: Session, org: m.Organization, p: m.Person, text: str, channel: str = "email",
                   simulated: bool = False) -> dict:
    ws = db.get(m.Workspace, p.workspace_id) if p.workspace_id else None
    pol = ws_policy(ws)
    conv = get_conversation(db, org.id, ws.id if ws else None, p, channel, None)
    if not conv.campaign_id:
        cl = db.scalars(select(m.CampaignLead).where(m.CampaignLead.person_id == p.id)).first()
        conv.campaign_id = cl.campaign_id if cl else None
    inbound = m.Message(organization_id=org.id, conversation_id=conv.id, campaign_id=conv.campaign_id, channel=channel,
                        direction="inbound", sender=p.email or p.phone, content=text, status="received",
                        sent_at=m.utcnow(), evidence=[{"simulated": True}] if simulated else [])
    db.add(inbound)
    conv.last_message_at, conv.status = m.utcnow(), "open"
    core.emit(db, org.id, "message.replied", conv.id, {"person_id": p.id, "channel": channel},
              ws_id=conv.workspace_id, campaign_id=conv.campaign_id)
    for box in db.scalars(select(m.Mailbox).where(m.Mailbox.organization_id == org.id)):
        if channel == "email":
            box.replies += 1
            break

    # Classification: LLM when available, rules otherwise (PRD 74, 142).
    history = list(db.scalars(select(m.Message).where(m.Message.conversation_id == conv.id).order_by(m.Message.created_at)))
    cls = classify_reply(text)
    data, model = llm.complete_json(
        db, org, "classify", "reply_classifier",
        "Classify the prospect's latest reply. JSON keys: intent (one of INTERESTED, VERY_INTERESTED, QUESTION, "
        "PRICING, OBJECTION, NOT_NOW, NOT_INTERESTED, WRONG_PERSON, REFERRAL, MEETING_REQUEST, MEETING_CONFIRMED, "
        "OOO, UNSUBSCRIBE, ANGRY, SPAM, UNKNOWN), sentiment, temperature (hot|warm|cold), lead_score 0-100, "
        "confidence 0-1, evidence (list of short quotes), summary.",
        "Thread:\n" + llm.untrusted("\n".join(f"{h.direction}: {h.content[:600]}" for h in history[-8:]), 6000),
        600, conv.workspace_id)
    if data and data.get("intent"):
        cls.update({k: data[k] for k in ("intent", "sentiment", "temperature", "lead_score", "confidence", "evidence")
                    if k in data})
    cls["model"] = model
    intent = cls["intent"]
    escalate = escalation_reasons(text)
    conv.intent, conv.sentiment, conv.temperature = intent, cls["sentiment"], cls["temperature"]
    conv.lead_score, conv.urgency = int(cls["lead_score"]), cls["urgency"] if "urgency" in cls else "normal"
    conv.summary = (data or {}).get("summary") or f"Latest reply classified {intent} ({cls['sentiment']})."
    mem = dict(conv.memory or {})
    mem.setdefault("asked", []).append(text[:200])
    p.temperature = cls["temperature"]
    p.intent_score = max(p.intent_score or 0, int(cls["lead_score"]))
    if p.stage in ("New", "Contacted"):
        p.stage = "Engaged"
    stop_followups(db, p.id, f"reply received ({intent})", "replied")   # PRD 172
    actions = []

    if intent == "UNSUBSCRIBE":
        suppress_person(db, org.id, p, "all" if channel == "email" else channel, "prospect opted out")
        conv.status, conv.next_action = "closed", "Suppressed - do not contact"
        actions.append("suppressed")
    elif intent == "ANGRY":   # PRD 177
        suppress_person(db, org.id, p, "all", "hostile reply")
        conv.needs_review, conv.review_reason, conv.ai_mode = True, "Hostile reply - human review", "human"
        core.notify(db, org.id, "ai_escalation", f"Escalation: angry reply from {p.full_name}", text[:200], f"#/inbox/{conv.id}")
        core.emit(db, org.id, "agent.escalation", conv.id, {"reason": "angry"})
        actions.append("escalated")
    elif intent == "OOO":   # PRD 174
        ret = extract_return_date(text, m.utcnow()) or (m.utcnow() + timedelta(days=7))
        for cl in db.scalars(select(m.CampaignLead).where(m.CampaignLead.person_id == p.id,
                                                          m.CampaignLead.status == "replied")):
            cl.status = "active"
            cl.next_action_at = (m.utcnow() + timedelta(minutes=5)) if SANDBOX_TIME_COMPRESSION else ret + timedelta(days=1)
        conv.next_action, conv.next_action_at = f"Resume after {ret.strftime('%d %b')}", ret
        p.stage = "Contacted"
        actions.append(f"paused until {ret.date()}")
    elif intent == "REFERRAL":   # PRD 175
        ref = extract_referral(text)
        if ref:
            comp = db.get(m.Company, p.company_id) if p.company_id else None
            dom = comp.domain if comp else ""
            email = ref.get("email") or (f"{ref['first_name'].lower()}.{ref.get('last_name', '').lower()}@{dom}".replace(".@", "@") if dom else "")
            newp, created = upsert_person(db, org.id, p.workspace_id, {
                "first_name": ref["first_name"], "last_name": ref.get("last_name", ""), "company_id": p.company_id,
                "title": (ref.get("department") or "").title() + " (referred)", "email": email,
                "country": p.country, "city": p.city, "custom_fields": {"referred_by": p.id}}, "referral")
            newp.email_status = "VALID" if dom.endswith(".demo") else "RISKY"
            newp.source = "demo_referral" if _is_demo(p) else "referral"
            actions.append(f"created referred lead {newp.full_name}")
            mem["referral"] = {"person_id": newp.id, "name": newp.full_name}
            core.decide(db, org.id, "reply", "create_referral_lead", f"{p.full_name} referred {newp.full_name}",
                        entity_type="person", entity_id=newp.id, confidence=0.8)
    elif intent == "NOT_INTERESTED":
        p.stage = "Lost"
    elif intent == "NOT_NOW":
        p.stage = "Nurture"
        conv.next_action_at = m.utcnow() + timedelta(days=60)
        conv.next_action = "Re-engage in ~60 days"

    # Booking intent
    slots = []
    tz = ws.timezone if ws else "Asia/Kolkata"
    if intent in ("MEETING_REQUEST", "VERY_INTERESTED", "INTERESTED", "PRICING", "QUESTION", "MEETING_CONFIRMED"):
        day, part = extract_day(text, local_now(tz))
        if intent == "MEETING_CONFIRMED" and mem.get("proposed_slots"):
            chosen = datetime.fromisoformat(mem["proposed_slots"][0])
            for s in mem["proposed_slots"]:
                d = datetime.fromisoformat(s)
                if d.strftime("%A").lower() in text.lower() or d.strftime("%I").lstrip("0") + (" pm" if d.hour >= 12 else " am") in text.lower():
                    chosen = d
            if pol["autopilot"]["book_meetings"] and conv.ai_mode == "ai":
                book_meeting(db, org, ws, p, chosen, conv)
                actions.append(f"booked {chosen}")
        elif day and part == "exact":
            free = available_slots(db, ws, day.replace(hour=0, minute=0), "", 12)
            match = next((s for s in free if s == day), None)
            if match and pol["autopilot"]["book_meetings"] and conv.ai_mode == "ai":
                book_meeting(db, org, ws, p, match, conv)
                actions.append(f"booked {match}")
                intent = "MEETING_CONFIRMED"
            else:
                slots = available_slots(db, ws, day, "", 3)
        else:
            slots = available_slots(db, ws, day, part, 3)
        if slots:
            mem["proposed_slots"] = [s.isoformat() for s in slots]

    if cls["temperature"] == "hot":
        if p.stage not in ("Meeting Booked",):
            p.stage = "Hot"
        core.emit(db, org.id, "lead.hot", p.id, {"intent": intent, "score": conv.lead_score}, campaign_id=conv.campaign_id)
        core.notify(db, org.id, "hot_lead", f"Hot lead: {p.full_name}", f"{intent}: {text[:160]}", f"#/inbox/{conv.id}")
        if pol["autopilot"]["update_crm"]:
            queue_crm_sync(db, org.id, p.id, "Hot lead")
    elif cls["temperature"] == "warm" and p.stage in ("Engaged", "Contacted"):
        p.stage = "Warm"

    # Draft + auto-send decision (PRD 75)
    offer = db.scalars(select(m.Offer).where(m.Offer.organization_id == org.id)).first()
    draft = "" if "booked" in " ".join(actions) and intent != "MEETING_CONFIRMED" else \
        _reply_text(org, p, intent, slots, offer, referral=mem.get("referral") and extract_referral(text))
    if "booked" in " ".join(actions):
        draft = _reply_text(org, p, "MEETING_CONFIRMED", [], offer)
    if draft and intent not in ("UNSUBSCRIBE", "ANGRY", "OOO"):
        gen, gmodel = llm.complete_json(
            db, org, "reply", "reply_writer",
            "Rewrite the draft reply to the prospect so it answers their latest message naturally. Keep every proposed "
            "time slot exactly as given, never promise discounts, custom terms, or facts not provided. JSON: {body}.",
            f"Draft:\n{draft}\nThread:\n" + llm.untrusted("\n".join(f"{h.direction}: {h.content[:500]}" for h in history[-6:]), 5000),
            700, conv.workspace_id)
        if gen and gen.get("body"):
            draft = gen["body"]
    reasons = []
    if cls["confidence"] < pol["auto_reply_confidence"]:
        reasons.append(f"low confidence {cls['confidence']}")
    if intent not in SAFE_AUTO:
        reasons.append(f"intent {intent} requires review")
    if escalate:
        reasons.append("escalation: " + ", ".join(escalate))
    if conv.ai_mode != "ai":
        reasons.append("human owns conversation")
    if not pol["autopilot"]["handle_replies"]:
        reasons.append("autopilot reply handling disabled")
    if draft:
        if reasons:
            conv.needs_review, conv.review_reason, conv.draft_reply = True, "; ".join(reasons)[:200], draft
            if escalate:
                core.notify(db, org.id, "ai_escalation", f"Needs you: {p.full_name}", "; ".join(reasons), f"#/inbox/{conv.id}")
                core.emit(db, org.id, "agent.escalation", conv.id, {"reasons": reasons})
            core.decide(db, org.id, "reply", "draft_for_review", "; ".join(reasons), entity_type="conversation",
                        entity_id=conv.id, confidence=cls["confidence"], policy_result="REVIEW", decision="needs_approval",
                        evidence=cls.get("evidence", []), model=model)
        else:
            subj = "Re: " + (next((h.subject for h in history if h.subject), "our conversation") or "").removeprefix("Re: ")
            deliver(db, org, ws, p, channel if channel in ("email", "whatsapp", "sms") else "email", subj, draft,
                    ai_generated=True, model=model, confidence=cls["confidence"], conv=conv,
                    idempotency_key=f"reply:{inbound.id}", enforce_policy=intent != "NOT_INTERESTED")
            conv.needs_review, conv.draft_reply = False, ""
            core.decide(db, org.id, "reply", "auto_reply", f"Intent {intent} with confidence {cls['confidence']}",
                        entity_type="conversation", entity_id=conv.id, confidence=cls["confidence"],
                        evidence=cls.get("evidence", []), model=model)
            actions.append("auto-replied")
    conv.memory = mem
    conv.next_action = conv.next_action or {"MEETING_REQUEST": "Confirm slot", "PRICING": "Scoping call",
                                            "INTERESTED": "Book discovery call", "VERY_INTERESTED": "Book discovery call"}.get(intent, "")
    db.commit()
    return {"conversation_id": conv.id, "classification": cls, "actions": actions, "draft": conv.draft_reply,
            "needs_review": conv.needs_review}


# ------------------------------------------------------------------ analytics
def campaign_stats(db: Session, camp_id: str) -> dict:
    def cnt(q):
        return db.scalar(q) or 0
    sent = cnt(select(func.count(m.Message.id)).where(m.Message.campaign_id == camp_id, m.Message.direction == "outbound",
                                                      m.Message.status.in_(["sent", "delivered"])))
    blocked = cnt(select(func.count(m.Message.id)).where(m.Message.campaign_id == camp_id, m.Message.status == "blocked"))
    convs = select(m.Conversation.id).where(m.Conversation.campaign_id == camp_id)
    replies = cnt(select(func.count(func.distinct(m.Message.conversation_id))).where(
        m.Message.conversation_id.in_(convs), m.Message.direction == "inbound"))
    positive = cnt(select(func.count(m.Conversation.id)).where(m.Conversation.campaign_id == camp_id,
                                                               m.Conversation.intent.in_(list(HOT | {"INTERESTED", "PRICING", "QUESTION", "REFERRAL"}))))
    hot = cnt(select(func.count(m.Conversation.id)).where(m.Conversation.campaign_id == camp_id,
                                                          m.Conversation.temperature == "hot"))
    meetings = cnt(select(func.count(m.Meeting.id)).where(m.Meeting.campaign_id == camp_id, m.Meeting.status != "cancelled"))
    leads = cnt(select(func.count(m.CampaignLead.id)).where(m.CampaignLead.campaign_id == camp_id))
    camp = db.get(m.Campaign, camp_id)
    spend = round(camp.spent or 0, 2) if camp else 0
    return {"leads": leads, "sent": sent, "blocked": blocked, "replies": replies, "positive": positive, "hot": hot,
            "meetings": meetings, "spend": spend, "reply_rate": round(100 * replies / sent, 1) if sent else 0,
            "hot_rate": round(100 * hot / sent, 1) if sent else 0,
            "cost_per_lead": round(spend / hot, 2) if hot else None,
            "cost_per_meeting": round(spend / meetings, 2) if meetings else None}


def variant_stats(db: Session, camp_id: str) -> dict:
    out = {}
    for v in ("A", "B"):
        pids = select(m.CampaignLead.person_id).where(m.CampaignLead.campaign_id == camp_id, m.CampaignLead.variant == v)
        n = db.scalar(select(func.count()).select_from(pids.subquery())) or 0
        replied = db.scalar(select(func.count(m.Conversation.id)).where(
            m.Conversation.person_id.in_(pids), m.Conversation.campaign_id == camp_id, m.Conversation.intent != "")) or 0
        out[v] = {"leads": n, "replied": replied, "reply_rate": round(100 * replied / n, 1) if n else 0}
    return out


# ------------------------------------------------------------------ optimisation agent (PRD 84-88, 190, 269)
def optimize_org(db: Session, org: m.Organization) -> list[str]:
    actions = []
    for ws in db.scalars(select(m.Workspace).where(m.Workspace.organization_id == org.id)):
        pol = ws_policy(ws)
        camps = list(db.scalars(select(m.Campaign).where(m.Campaign.workspace_id == ws.id,
                                                         m.Campaign.status.in_(["RUNNING", "OPTIMIZING"]))))
        min_sample = 8 if SANDBOX_TIME_COMPRESSION else 50
        scored = [(c, campaign_stats(db, c.id)) for c in camps]
        eligible = [(c, s) for c, s in scored if s["sent"] >= min_sample]
        if len(eligible) < 2:
            continue
        best = max(eligible, key=lambda x: (x[1]["hot_rate"], x[1]["reply_rate"]))
        for c, s in eligible:
            if c.id == best[0].id:
                continue
            if s["reply_rate"] < best[1]["reply_rate"] / 3 and s["hot"] == 0 and pol["autopilot"]["reallocate_budget"]:
                if c.autonomy_mode in ("autonomous", "full_autonomous"):
                    c.status = "PAUSED"
                    c.agent_state = {**(c.agent_state or {}), "paused_reason": "underperforming"}
                    core.decide(db, org.id, "optimization", "pause_campaign",
                                f"Reply rate {s['reply_rate']}% vs best {best[1]['reply_rate']}% with 0 hot leads after {s['sent']} sends",
                                entity_type="campaign", entity_id=c.id, confidence=0.82)
                    core.emit(db, org.id, "campaign.paused", c.id, {"reason": "optimization"}, campaign_id=c.id)
                    actions.append(f"Paused {c.name}")
                else:
                    core.decide(db, org.id, "optimization", "pause_campaign", "Underperforming (proposal)",
                                entity_type="campaign", entity_id=c.id, decision="proposed")
        bc, bs = best
        if bs["hot"] > 0 and bc.budget_daily:
            mission = db.get(m.Mission, bc.mission_id) if bc.mission_id else None
            increase = round(bc.budget_daily * 0.15, 2)
            cap = (mission.budget_monthly / 30) if mission and mission.budget_monthly else bc.budget_daily * 2
            if bc.budget_daily + increase <= cap and not (bc.agent_state or {}).get("scaled_today"):
                if increase <= pol["budget_change_autonomous_limit"]:
                    bc.budget_daily = round(bc.budget_daily + increase, 2)
                    bc.agent_state = {**(bc.agent_state or {}), "scaled_today": m.utcnow().date().isoformat(), "label": "Scaling"}
                    core.decide(db, org.id, "optimization", "increase_campaign_budget",
                                f"Hot-lead rate {bs['hot_rate']}% is the best in the portfolio; +15% daily allocation",
                                entity_type="campaign", entity_id=bc.id, confidence=0.86)
                    actions.append(f"Scaled {bc.name} +15%")
                else:
                    core.decide(db, org.id, "optimization", "increase_campaign_budget", "Above autonomous limit",
                                entity_type="campaign", entity_id=bc.id, decision="needs_approval")
    db.commit()
    return actions


def daily_brief(db: Session, org_id: str, ws_id: str | None) -> dict:
    since = m.utcnow() - timedelta(days=1)

    def ev(t):
        return db.scalar(select(func.count(m.DomainEvent.id)).where(m.DomainEvent.organization_id == org_id,
                                                                    m.DomainEvent.type == t, m.DomainEvent.created_at >= since)) or 0
    new_people = db.scalar(select(func.count(m.Person.id)).where(m.Person.organization_id == org_id, m.Person.created_at >= since)) or 0
    hot, meetings, replies, sent = ev("lead.hot"), ev("meeting.booked"), ev("message.replied"), ev("message.sent")
    decisions = list(db.scalars(select(m.AIDecision).where(m.AIDecision.organization_id == org_id,
                                                           m.AIDecision.created_at >= since,
                                                           m.AIDecision.agent.in_(["optimization", "gtm_orchestrator"]))
                                .order_by(m.AIDecision.created_at.desc()).limit(5)))
    review = db.scalar(select(func.count(m.Conversation.id)).where(m.Conversation.organization_id == org_id,
                                                                    m.Conversation.needs_review.is_(True))) or 0
    camps = list(db.scalars(select(m.Campaign).where(m.Campaign.organization_id == org_id,
                                                     m.Campaign.status.in_(["RUNNING", "OPTIMIZING"]))))
    best = max(((c, campaign_stats(db, c.id)) for c in camps), key=lambda x: x[1]["hot"], default=None)
    happened = (f"In the last 24 hours I added {new_people} new prospects, sent {sent} messages, received {replies} "
                f"replies, found {hot} hot leads and booked {meetings} meetings.")
    if best and best[1]["hot"]:
        happened += f" Best campaign: {best[0].name} ({best[1]['hot']} hot leads, {best[1]['reply_rate']}% reply rate)."
    return {"what_happened": happened,
            "what_i_changed": [f"{d.action.replace('_', ' ')}: {d.reason}" for d in decisions] or ["No strategy changes."],
            "what_im_testing": [f"CTA A/B test in {c.name}" for c in camps[:3]] or ["Nothing yet - start a campaign."],
            "what_requires_you": f"{review} conversation(s) need your review." if review else "Nothing right now.",
            "metrics": {"new_prospects": new_people, "sent": sent, "replies": replies, "hot": hot, "meetings": meetings}}


# ------------------------------------------------------------------ webhooks (PRD 123, 278)
def deliver_webhooks(db: Session, limit: int = 20):
    for d in db.scalars(select(m.WebhookDelivery).where(m.WebhookDelivery.status.in_(["pending", "retrying"]))
                        .order_by(m.WebhookDelivery.created_at).limit(limit)):
        ep = db.get(m.WebhookEndpoint, d.endpoint_id)
        if not ep or not ep.active:
            d.status = "cancelled"
            continue
        body = json.dumps(d.payload, default=str).encode()
        secret = decrypt(ep.secret_enc) or ""
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        d.attempts += 1
        try:
            r = httpx.post(ep.url, content=body, timeout=8, headers={
                "Content-Type": "application/json", "X-GTM-Event": d.event, "X-GTM-Delivery": d.id,
                "X-GTM-Signature": f"sha256={sig}"})
            d.response_code = r.status_code
            d.status = "delivered" if r.status_code < 300 else ("failed" if d.attempts >= 5 else "retrying")
        except httpx.HTTPError:
            d.status = "failed" if d.attempts >= 5 else "retrying"
    db.commit()


def post_slack(db: Session, org_id: str, text: str):
    url = core.get_secret(db, org_id, "SLACK_WEBHOOK_URL")
    if url:
        try:
            httpx.post(url, json={"text": text}, timeout=5)
        except httpx.HTTPError:
            pass
