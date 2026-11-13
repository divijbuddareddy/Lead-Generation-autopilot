"""Inbound provider webhooks (PRD 278-279).

Resend sends delivery/bounce/complaint events and inbound replies (`email.received`) here.
Every request is: signature-verified (Svix scheme) -> stored raw -> de-duplicated -> processed -> acknowledged.
The signing secret is the per-organization RESEND_WEBHOOK_SECRET held in the admin vault.
"""
import base64
import hashlib
import hmac
import json
import re
import time

import httpx
from fastapi import APIRouter, Request
from sqlalchemy import select

from .. import models as m
from ..database import SessionLocal
from ..deps import APIError
from ..services import core, engine

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
TOLERANCE_SECONDS = 5 * 60


def verify_svix(secret: str, msg_id: str, timestamp: str, signature_header: str, body: bytes) -> bool:
    """Svix verification: HMAC-SHA256(base64decode(secret without whsec_), f"{id}.{ts}.{body}")."""
    if not (secret and msg_id and timestamp and signature_header):
        return False
    try:
        if abs(time.time() - int(timestamp)) > TOLERANCE_SECONDS:
            return False   # replay protection
        key = base64.b64decode(secret.removeprefix("whsec_"))
    except (ValueError, TypeError):
        return False
    signed = f"{msg_id}.{timestamp}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    for part in signature_header.split():
        version, _, sig = part.partition(",")
        if version == "v1" and hmac.compare_digest(sig, expected):
            return True
    return False


_QUOTE_MARKERS = [r"^On .+ wrote:\s*$", r"^-{2,}\s*Original Message\s*-{2,}", r"^From: .+", r"^>"]


def strip_quoted(text: str) -> str:
    """Keep only the new part of a reply (drop quoted history and signatures delimiter)."""
    out = []
    for line in (text or "").splitlines():
        if any(re.match(p, line.strip(), re.I) for p in _QUOTE_MARKERS):
            break
        out.append(line)
    cleaned = "\n".join(out).strip()
    return cleaned or (text or "").strip()


def _html_to_text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"(?i)<br\s*/?>|</p>", "\n", html or ""))).strip()


def _address(value) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    found = re.search(r"[\w.+'-]+@[\w-]+\.[\w.-]+", value or "")
    return found.group(0).lower() if found else ""


@router.post("/resend/{org_id}")
async def resend_webhook(org_id: str, request: Request):
    body = await request.body()
    db = SessionLocal()
    try:
        org = db.get(m.Organization, org_id)
        secret = core.get_secret(db, org_id, "RESEND_WEBHOOK_SECRET") if org else None
        h = request.headers
        if not org or not verify_svix(secret or "", h.get("svix-id", ""), h.get("svix-timestamp", ""),
                                      h.get("svix-signature", ""), body):
            raise APIError(401, "INVALID_SIGNATURE", "Webhook signature verification failed.")
        event_id = h["svix-id"]
        dedupe_key = f"resend:{org_id}:{event_id}"[:200]
        if db.get(m.IdempotencyRecord, dedupe_key):
            return {"ok": True, "duplicate": True}
        event = json.loads(body)
        etype, data = event.get("type", ""), event.get("data", {}) or {}
        # immutable raw-event archive + dedupe marker
        db.add(m.IdempotencyRecord(key=dedupe_key, organization_id=org_id, endpoint="webhooks/resend", response={}))
        db.add(m.DomainEvent(organization_id=org_id, type=f"provider.resend.{etype}", entity_id=str(data.get("email_id", ""))[:32],
                             data=event))
        result = _process(db, org, etype, data)
        db.commit()
        return {"ok": True, **result}
    finally:
        db.close()


def _process(db, org: m.Organization, etype: str, data: dict) -> dict:
    email_id = data.get("email_id", "")
    msg = db.scalars(select(m.Message).where(m.Message.organization_id == org.id,
                                             m.Message.provider_message_id == email_id)).first() if email_id else None

    if etype in ("email.sent", "email.delivered"):
        if msg and etype == "email.delivered":
            msg.status = "delivered"
            core.emit(db, org.id, "message.delivered", msg.id, {"provider": "resend"}, campaign_id=msg.campaign_id)
        return {"handled": etype}

    if etype in ("email.bounced", "email.complained"):
        conv = db.get(m.Conversation, msg.conversation_id) if msg else None
        person = db.get(m.Person, conv.person_id) if conv else None
        if not person:
            addr = _address(data.get("to"))
            person = db.scalars(select(m.Person).where(m.Person.organization_id == org.id, m.Person.email == addr)).first() if addr else None
        box = db.scalars(select(m.Mailbox).where(m.Mailbox.organization_id == org.id,
                                                 m.Mailbox.address == (msg.sender if msg else ""))).first()
        if etype == "email.bounced":
            if msg:
                msg.status, msg.error = "failed", "permanent: bounced"
            if box:
                box.bounces += 1
            if person:
                person.email_status = "INVALID"
                engine.suppress_person(db, org.id, person, "email", "hard bounce")
            core.emit(db, org.id, "message.failed", msg.id if msg else "", {"reason": "bounced"},
                      campaign_id=msg.campaign_id if msg else None)
        else:
            if box:
                box.complaints += 1
            if person:
                engine.suppress_person(db, org.id, person, "all", "spam complaint")
            core.notify(db, org.id, "compliance", "Spam complaint received",
                        f"{person.full_name if person else 'A recipient'} marked an email as spam; contact suppressed.", "#/settings?tab=suppression")
        if box:
            engine.update_mailbox_health(box)   # degraded mailboxes stop receiving work automatically
        return {"handled": etype}

    if etype == "email.received":
        key = core.get_secret(db, org.id, "RESEND_API_KEY")
        sender = _address(data.get("from"))
        text = ""
        if key and email_id:
            # Webhooks carry metadata only; fetch the body from the Received Emails API.
            r = httpx.get(f"{engine.RESEND_API}/emails/receiving/{email_id}",
                          headers={"Authorization": f"Bearer {key}"}, timeout=15)
            if r.status_code == 200:
                full = r.json()
                text = full.get("text") or _html_to_text(full.get("html") or "")
                sender = _address(full.get("from")) or sender
        text = strip_quoted(text) or (data.get("subject") or "")
        person = db.scalars(select(m.Person).where(m.Person.organization_id == org.id, m.Person.email == sender)).first() if sender else None
        if not person:
            core.notify(db, org.id, "inbound_unknown", f"Email from unknown sender {sender or '(unknown)'}",
                        (data.get("subject") or "")[:200], "#/inbox")
            return {"handled": etype, "matched": False}
        db.commit()
        res = engine.handle_inbound(db, org, person, text[:5000], "email")   # reply agent (PRD 70-77)
        return {"handled": etype, "matched": True, "intent": res["classification"]["intent"]}

    return {"handled": "ignored", "type": etype}
