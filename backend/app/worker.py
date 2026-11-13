"""In-process durable worker (PRD 69, 125-128, 169, 200).

Jobs live in the `jobs` table (survive restarts), are picked by priority
(P0 inbound reply ... P6 refresh) and retried only on temporary errors.
Swap for Temporal / a Redis queue when scale requires it.
"""
import logging
import threading
import time
from datetime import datetime

import httpx
from sqlalchemy import select

from . import models as m
from .config import WORKER_TICK_SECONDS
from .database import SessionLocal
from .services import core, engine
from .services.intelligence import enrich_person, research_company

log = logging.getLogger("gtm.worker")
TEMPORARY = (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError, TimeoutError, ConnectionError)


def _job_research(db, job):
    org = db.get(m.Organization, job.organization_id)
    ids = job.payload.get("company_ids", [])
    job.total = len(ids)
    for i, cid in enumerate(ids[job.progress:], start=job.progress):
        c = db.get(m.Company, cid)
        if c and c.organization_id == org.id:
            research_company(db, org, job.workspace_id, c, job.payload.get("depth", "standard"))
        job.progress = i + 1
        db.commit()
    return {"researched": job.progress}


def _job_enrich(db, job):
    org = db.get(m.Organization, job.organization_id)
    ids = job.payload.get("person_ids", [])
    job.total, found = len(ids), 0
    for i, pid in enumerate(ids[job.progress:], start=job.progress):
        p = db.get(m.Person, pid)
        if p and p.organization_id == org.id:
            r = enrich_person(db, org, job.workspace_id, p, phone=job.payload.get("phone", False))
            found += 1 if r.get("email") or p.email else 0
        job.progress = i + 1
        db.commit()
    return {"enriched": job.progress, "with_email": found}


def _job_simulated_reply(db, job):
    run_after = datetime.fromisoformat(job.payload["run_after"])
    if run_after > m.utcnow():
        return "DEFER"
    p = db.get(m.Person, job.payload["person_id"])
    org = db.get(m.Organization, job.organization_id)
    if not p or not org:
        return {"skipped": True}
    # Don't reply if the lead was stopped (e.g. suppressed) in the meantime.
    return engine.handle_inbound(db, org, p, job.payload["text"], job.payload.get("channel", "email"), simulated=True)


def _job_sync_crm(db, job):
    return engine.sync_crm(db, job.organization_id, job.payload["person_id"], job.payload.get("stage", "Hot lead"))


HANDLERS = {"research_company": _job_research, "enrich": _job_enrich, "simulated_reply": _job_simulated_reply,
            "sync_crm": _job_sync_crm}


def run_jobs(db, limit=10):
    jobs = list(db.scalars(select(m.Job).where(m.Job.status.in_(["QUEUED", "RUNNING"]))
                           .order_by(m.Job.priority, m.Job.created_at).limit(limit)))
    for job in jobs:
        handler = HANDLERS.get(job.type)
        if not handler:
            continue
        job.status, job.attempts = "RUNNING", job.attempts + 1
        db.commit()
        try:
            res = handler(db, job)
            if res == "DEFER":
                job.status, job.attempts = "QUEUED", job.attempts - 1
            else:
                job.status, job.result = "COMPLETED", res if isinstance(res, dict) else {"result": str(res)}
                if job.total and job.progress < job.total:
                    job.status = "PARTIAL"
        except core.InsufficientCredits as exc:
            job.status, job.error = "PAUSED", exc.message
        except TEMPORARY as exc:
            job.status = "QUEUED" if job.attempts < 4 else "FAILED"
            job.error = f"temporary: {exc}"
        except Exception as exc:  # noqa: BLE001 - permanent error, stop
            log.exception("job %s failed", job.id)
            db.rollback()
            job = db.get(m.Job, job.id)
            job.status, job.error = "FAILED", f"permanent: {exc}"[:500]
        db.commit()


_last_notification = {"ts": m.utcnow()}


def forward_notifications(db):
    rows = list(db.scalars(select(m.Notification).where(m.Notification.created_at > _last_notification["ts"],
                                                        m.Notification.kind.in_(["hot_lead", "meeting_booked", "ai_escalation"]))))
    for n in rows:
        engine.post_slack(db, n.organization_id, f"*{n.title}*\n{n.body}")
        _last_notification["ts"] = max(_last_notification["ts"], n.created_at)


def tick(counter: int):
    db = SessionLocal()
    try:
        run_jobs(db)
        for camp in db.scalars(select(m.Campaign).where(m.Campaign.status == "RUNNING")):
            try:
                engine.process_campaign(db, camp)
            except Exception:  # noqa: BLE001
                log.exception("campaign %s tick failed", camp.id)
                db.rollback()
        engine.deliver_webhooks(db)
        forward_notifications(db)
        if counter % 12 == 0:
            for org in db.scalars(select(m.Organization).where(m.Organization.status == "active")):
                engine.optimize_org(db, org)
    finally:
        db.close()


class Worker:
    def __init__(self):
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="gtm-worker", daemon=True)
        self.last_tick = None

    def start(self):
        self.thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        i = 0
        while not self._stop.is_set():
            try:
                tick(i)
                self.last_tick = m.utcnow()
            except Exception:  # noqa: BLE001
                log.exception("worker tick failed")
            i += 1
            self._stop.wait(WORKER_TICK_SECONDS)


worker = Worker()
