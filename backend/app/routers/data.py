"""Search, companies, people/leads, research, enrichment, lists, import/export, jobs."""
import csv
import io
import re

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select

from .. import models as m
from ..deps import APIError, Ctx, require, tenant_get
from ..services import core, llm
from ..services.intelligence import (company_dict, enrich_person, person_dict, research_company, score_company,
                                     search_companies, search_people, upsert_company, upsert_person, workspace_icp)
from ..services.nlp import describe_criteria, parse_search
from ..services.providers import ProviderRouter

router = APIRouter(prefix="/app", tags=["data"])


# ------------------------------------------------------------------ AI search chat (PRD 15-16, 100)
class SearchIn(BaseModel):
    query: str = ""
    criteria: dict | None = None
    base: dict | None = None
    limit: int = Field(50, ge=1, le=500)
    mode: str = "companies"   # companies | people
    company_ids: list[str] | None = None


@router.post("/search/parse")
def parse(body: SearchIn, ctx: Ctx = Depends(require("search.run"))):
    c = parse_search(body.query, body.base)
    return {"criteria": c, "summary": describe_criteria(c)}


@router.post("/search")
def search(body: SearchIn, ctx: Ctx = Depends(require("search.run"))):
    q = body.query.lower()
    # Chat refinement: "only show owners and CEOs" transitions to people search (PRD 100).
    to_people = body.mode == "people" or bool(re.search(r"\b(only show|show me|find)\b.*\b(owners?|ceos?|founders?|ctos?|coos?|heads?|directors?|vps?|people|decision makers?)\b", q))
    if to_people:
        res = search_people(ctx, query=body.query, criteria=body.criteria, company_ids=body.company_ids,
                            limit=body.limit, base=body.base)
        res["mode"] = "people"
    else:
        res = search_companies(ctx, query=body.query, criteria=body.criteria, limit=body.limit, base=body.base)
        res["mode"] = "companies"
    core.audit_ctx(ctx, "search.run", "search", "", {"query": body.query[:200], "mode": res["mode"]})
    ctx.db.commit()
    return res


# ------------------------------------------------------------------ companies
@router.get("/companies")
def companies(q: str = "", industry: str = "", min_icp: int = 0, limit: int = 100, offset: int = 0,
              ctx: Ctx = Depends(require("leads.view"))):
    stmt = select(m.Company).where(m.Company.organization_id == ctx.org_id, m.Company.workspace_id == ctx.ws_id)
    if q:
        stmt = stmt.where(or_(m.Company.name.ilike(f"%{q}%"), m.Company.domain.ilike(f"%{q}%"), m.Company.city.ilike(f"%{q}%")))
    if industry:
        stmt = stmt.where(m.Company.industry == industry)
    rows = [company_dict(c) for c in ctx.db.scalars(stmt.order_by(m.Company.created_at.desc()))]
    rows = [r for r in rows if (r["scores"].get("icp") or 0) >= min_icp]
    rows.sort(key=lambda r: r["scores"].get("opportunity", 0), reverse=True)
    return {"total": len(rows), "results": rows[offset:offset + limit]}


@router.get("/companies/{cid}")
def company_detail(cid: str, ctx: Ctx = Depends(require("leads.view"))):
    db = ctx.db
    c = tenant_get(ctx, m.Company, cid)
    people = [person_dict(db, p, c, ctx.has("leads.view")) for p in db.scalars(select(m.Person).where(m.Person.company_id == c.id))]
    obs = [{"field": o.field_name, "value": o.field_value, "provider": o.provider, "confidence": o.confidence,
            "observed_at": o.observed_at.isoformat(), "source_url": o.source_url}
           for o in db.scalars(select(m.FieldObservation).where(m.FieldObservation.entity_id == c.id)
                               .order_by(m.FieldObservation.observed_at.desc()))]
    timeline = [{"type": e.type, "data": e.data, "at": e.created_at.isoformat()}
                for e in db.scalars(select(m.DomainEvent).where(m.DomainEvent.organization_id == ctx.org_id,
                                                                m.DomainEvent.entity_id.in_([c.id] + [p["id"] for p in people]))
                                    .order_by(m.DomainEvent.created_at.desc()).limit(50))]
    core.emit(db, ctx.org_id, "company_viewed", c.id, {})
    db.commit()
    return {**company_dict(c), "people": people, "provenance": obs, "timeline": timeline}


class ResearchIn(BaseModel):
    company_ids: list[str]
    depth: str = Field("standard", pattern="^(quick|standard|deep)$")


@router.post("/research")
def research(body: ResearchIn, ctx: Ctx = Depends(require("research.run"))):
    if body.depth == "deep":
        core.require_entitlement(ctx.db, ctx.org, "deep_research")
    ids = [tenant_get(ctx, m.Company, i).id for i in body.company_ids[:200]]
    if len(ids) == 1:
        res = research_company(ctx.db, ctx.org, ctx.ws_id, ctx.db.get(m.Company, ids[0]), body.depth)
        ctx.db.commit()
        return {"research": res}
    job = m.Job(organization_id=ctx.org_id, workspace_id=ctx.ws_id, type="research_company", priority=4,
                payload={"company_ids": ids, "depth": body.depth}, total=len(ids), created_by=ctx.actor_id,
                request_id=ctx.request_id)
    ctx.db.add(job)
    ctx.db.commit()
    return {"job_id": job.id, "status": job.status, "total": len(ids)}


class CriterionIn(BaseModel):
    criterion: str
    company_ids: list[str] | None = None


@router.post("/companies/criteria")
def custom_criterion(body: CriterionIn, ctx: Ctx = Depends(require("research.run"))):
    """Custom AI criteria (PRD 18): score companies 0-100 against a user-defined criterion, persisted."""
    db = ctx.db
    stmt = select(m.Company).where(m.Company.organization_id == ctx.org_id, m.Company.workspace_id == ctx.ws_id)
    if body.company_ids:
        stmt = stmt.where(m.Company.id.in_(body.company_ids))
    comps = list(db.scalars(stmt.limit(100)))
    words = [w for w in re.findall(r"[a-z]{4,}", body.criterion.lower()) if w not in
             {"score", "companies", "based", "whether", "they", "likely", "have", "need", "with", "that"}]
    out = []
    for c in comps:
        facts = f"{c.description} {' '.join(s['text'] for s in c.signals or [])} {' '.join(c.technologies or [])} " \
                f"{c.industry} {c.sub_industry} employees:{c.employee_count}"
        data, model = llm.complete_json(db, ctx.org, "classify", "custom_criterion",
                                        "Score the company 0-100 on the criterion using only the facts. JSON {score, reason}.",
                                        f"Criterion: {body.criterion}\nFacts: {llm.untrusted(facts, 3000)}", 300, ctx.ws_id)
        if not data:
            hits = [w for w in words if w[:5] in facts.lower()]
            score = min(100, 30 + 20 * len(hits) + (15 if c.employee_count > 100 else 0) +
                        (10 if any(s["type"] in ("hiring", "process") for s in c.signals or []) else 0))
            data = {"score": score, "reason": ("Evidence: " + ", ".join(hits)) if hits else
                    "Weak direct evidence; inferred from size and signals."}
            model = "rules"
        cc = dict(c.custom_criteria or {})
        cc[body.criterion[:80]] = {"score": int(data.get("score", 0)), "reason": data.get("reason", ""), "model": model}
        c.custom_criteria = cc
        out.append({"company_id": c.id, "name": c.name, **cc[body.criterion[:80]]})
    core.meter(db, ctx.org_id, "ai_classification", max(1, len(out)), ws_id=ctx.ws_id, ref="criterion", enforce=False)
    db.commit()
    return {"criterion": body.criterion, "results": sorted(out, key=lambda x: -x["score"])}


class LookalikeIn(BaseModel):
    company_ids: list[str]
    limit: int = 50


@router.post("/companies/lookalike")
def lookalike(body: LookalikeIn, ctx: Ctx = Depends(require("search.run"))):
    """Lookalike engine (PRD 112-113): derive a profile from seed companies, search, rank by similarity."""
    seeds = [tenant_get(ctx, m.Company, i) for i in body.company_ids[:20]]
    if not seeds:
        raise APIError(400, "NO_SEEDS", "Select at least one company.")
    emps = sorted(s.employee_count for s in seeds)
    crit = {"industries": sorted({s.industry for s in seeds}), "countries": sorted({s.country for s in seeds}),
            "employee_min": int(emps[0] * 0.5), "employee_max": int(emps[-1] * 1.8)}
    res = search_companies(ctx, criteria=crit, limit=body.limit * 2)
    seed_ids = {s.id for s in seeds}
    techs = {t for s in seeds for t in s.technologies or []}
    sig_types = {x["type"] for s in seeds for x in s.signals or []}
    for r in res["results"]:
        firmo = 40 if r["industry"] in crit["industries"] else 0
        size = 20 if crit["employee_min"] <= r["employee_count"] <= crit["employee_max"] else 5
        tech = 15 * len(techs & set(r["technologies"])) / max(1, len(techs))
        model = 10 if r["business_model"] in {s.business_model for s in seeds} else 0
        sig = 15 * len(sig_types & {x["type"] for x in r["signals"]}) / max(1, len(sig_types))
        r["similarity"] = round(firmo + size + tech + model + sig)
    res["results"] = sorted([r for r in res["results"] if r["id"] not in seed_ids], key=lambda r: -r["similarity"])[:body.limit]
    res["profile"] = crit
    return res


# ------------------------------------------------------------------ people / leads (PRD 101-105)
def _lead_query(ctx: Ctx, q: str, stage: str, temperature: str, owner: str, list_id: str):
    stmt = select(m.Person).where(m.Person.organization_id == ctx.org_id, m.Person.workspace_id == ctx.ws_id)
    if q:
        stmt = stmt.where(or_(m.Person.full_name.ilike(f"%{q}%"), m.Person.email.ilike(f"%{q}%"), m.Person.title.ilike(f"%{q}%")))
    if stage:
        stmt = stmt.where(m.Person.stage == stage)
    if temperature:
        stmt = stmt.where(m.Person.temperature == temperature)
    if owner == "me":
        stmt = stmt.where(m.Person.owner_id == ctx.actor_id)
    if list_id:
        stmt = stmt.where(m.Person.id.in_(select(m.ListMember.person_id).where(m.ListMember.list_id == list_id)))
    return stmt


@router.get("/leads")
def leads(q: str = "", stage: str = "", temperature: str = "", owner: str = "", list_id: str = "",
          sort: str = "score", limit: int = 100, offset: int = 0, ctx: Ctx = Depends(require("leads.view"))):
    stmt = _lead_query(ctx, q, stage, temperature, owner, list_id)
    total = ctx.db.scalar(select(func.count()).select_from(stmt.subquery()))
    order = {"score": (m.Person.icp_score + m.Person.intent_score).desc(), "recent": m.Person.created_at.desc(),
             "name": m.Person.full_name}.get(sort, m.Person.created_at.desc())
    rows = list(ctx.db.scalars(stmt.order_by(order).offset(offset).limit(min(limit, 500))))
    comp_ids = {p.company_id for p in rows if p.company_id}
    comps = {c.id: c for c in ctx.db.scalars(select(m.Company).where(m.Company.id.in_(comp_ids)))} if comp_ids else {}
    users = {u.id: u.name for u in ctx.db.scalars(select(m.User))}
    out = []
    for p in rows:
        d = person_dict(ctx.db, p, comps.get(p.company_id))
        d["owner"] = users.get(p.owner_id, "")
        out.append(d)
    stages = dict(ctx.db.execute(select(m.Person.stage, func.count(m.Person.id)).where(
        m.Person.organization_id == ctx.org_id, m.Person.workspace_id == ctx.ws_id).group_by(m.Person.stage)).all())
    return {"total": total, "results": out, "stages": stages}


@router.get("/leads/{pid}")
def lead_detail(pid: str, ctx: Ctx = Depends(require("leads.view"))):
    db = ctx.db
    p = tenant_get(ctx, m.Person, pid)
    comp = db.get(m.Company, p.company_id) if p.company_id else None
    convs = list(db.scalars(select(m.Conversation).where(m.Conversation.person_id == p.id)))
    msgs = list(db.scalars(select(m.Message).where(m.Message.conversation_id.in_([c.id for c in convs]))
                           .order_by(m.Message.created_at))) if convs else []
    notes = [{"id": n.id, "body": n.body, "at": n.created_at.isoformat(),
              "by": (db.get(m.User, n.user_id).name if db.get(m.User, n.user_id) else "")}
             for n in db.scalars(select(m.Note).where(m.Note.person_id == p.id).order_by(m.Note.created_at.desc()))]
    obs = [{"field": o.field_name, "value": o.field_value, "provider": o.provider, "confidence": o.confidence,
            "observed_at": o.observed_at.isoformat()}
           for o in db.scalars(select(m.FieldObservation).where(m.FieldObservation.entity_id == p.id))]
    camps = [{"campaign": (db.get(m.Campaign, cl.campaign_id).name if db.get(m.Campaign, cl.campaign_id) else ""),
              "status": cl.status, "step": cl.current_step, "stop_reason": cl.stop_reason}
             for cl in db.scalars(select(m.CampaignLead).where(m.CampaignLead.person_id == p.id))]
    meetings = [{"id": x.id, "title": x.title, "start_at": x.start_at.isoformat(), "status": x.status}
                for x in db.scalars(select(m.Meeting).where(m.Meeting.person_id == p.id))]
    crm = db.scalars(select(m.CrmRecord).where(m.CrmRecord.person_id == p.id)).first()
    why_now = [s["text"] for s in (comp.signals or [])][:3] if comp else []
    rec = ("Book a discovery call" if p.temperature == "hot" else "Continue sequence" if p.stage in ("Contacted", "Engaged")
           else "Enrich and add to a campaign" if p.stage == "New" else "Nurture")
    return {**person_dict(db, p, comp), "company_detail": company_dict(comp) if comp else None,
            "why_this_person": f"{p.title} ({p.normalized_title}) at a company scoring ICP {p.icp_score}.",
            "why_now": why_now, "conversation_ids": [c.id for c in convs],
            "messages": [{"id": x.id, "direction": x.direction, "channel": x.channel, "subject": x.subject,
                          "content": x.content, "status": x.status, "at": x.created_at.isoformat(), "error": x.error}
                         for x in msgs],
            "notes": notes, "provenance": obs, "campaigns": camps, "meetings": meetings,
            "crm": {"stage": crm.deal_stage, "system": crm.external_system, "status": crm.sync_status} if crm else None,
            "whatsapp_opt_in": bool((p.custom_fields or {}).get("whatsapp_opt_in")),
            "recommended_action": rec}


class LeadUpdateIn(BaseModel):
    stage: str | None = None
    owner_id: str | None = None
    feedback: str | None = None   # good | bad (PRD 183)
    whatsapp_opt_in: bool | None = None
    sms_consent: bool | None = None


PIPELINE = ["New", "Contacted", "Engaged", "Warm", "Hot", "Meeting Booked", "Meeting Completed", "Opportunity",
            "Won", "Lost", "Nurture", "Suppressed"]


@router.patch("/leads/{pid}")
def update_lead(pid: str, body: LeadUpdateIn, ctx: Ctx = Depends(require("notes.add"))):
    p = tenant_get(ctx, m.Person, pid)
    if body.stage:
        if body.stage not in PIPELINE:
            raise APIError(400, "INVALID_STAGE", f"Stage must be one of {PIPELINE}")
        p.stage = body.stage
    if body.owner_id is not None:
        if not ctx.has("campaigns.manage") and body.owner_id != ctx.actor_id:
            raise APIError(403, "FORBIDDEN", "Sales users can only assign leads to themselves.")
        p.owner_id = body.owner_id or None
    if body.feedback in ("good", "bad", ""):
        p.feedback = body.feedback
        core.decide(ctx.db, ctx.org_id, "feedback", f"lead_marked_{body.feedback or 'cleared'}",
                    "Salesperson feedback recorded as training example", entity_type="person", entity_id=p.id,
                    model="human")
    cf = dict(p.custom_fields or {})
    if body.whatsapp_opt_in is not None:
        cf["whatsapp_opt_in"] = body.whatsapp_opt_in
    if body.sms_consent is not None:
        cf["sms_consent"] = body.sms_consent
    p.custom_fields = cf
    core.audit_ctx(ctx, "lead.updated", "person", p.id, body.model_dump(exclude_none=True))
    core.emit(ctx.db, ctx.org_id, "lead.updated", p.id, body.model_dump(exclude_none=True))
    ctx.db.commit()
    return {"ok": True}


class NoteIn(BaseModel):
    body: str = Field(min_length=1)


@router.post("/leads/{pid}/notes")
def add_note(pid: str, body: NoteIn, ctx: Ctx = Depends(require("notes.add"))):
    p = tenant_get(ctx, m.Person, pid)
    ctx.db.add(m.Note(organization_id=ctx.org_id, person_id=p.id, user_id=ctx.actor_id, body=body.body))
    ctx.db.commit()
    return {"ok": True}


class BulkIn(BaseModel):
    person_ids: list[str]
    action: str   # enrich | verify | add_to_list | suppress | assign | delete | add_to_campaign
    list_id: str | None = None
    campaign_id: str | None = None
    owner_id: str | None = None
    phone: bool = False


@router.post("/leads/bulk")
def bulk(body: BulkIn, ctx: Ctx = Depends(require("leads.view"))):
    """Bulk actions (PRD 102). Expensive actions become jobs."""
    perm = {"enrich": "enrich.run", "verify": "enrich.run", "add_to_list": "lists.manage", "suppress": "suppression.manage",
            "assign": "campaigns.manage", "delete": "data.delete", "add_to_campaign": "campaigns.manage"}.get(body.action)
    if not perm or not ctx.has(perm):
        raise APIError(403, "FORBIDDEN", f"Your role cannot perform '{body.action}'.")
    db = ctx.db
    people = list(db.scalars(select(m.Person).where(m.Person.id.in_(body.person_ids), m.Person.organization_id == ctx.org_id)))
    ids = [p.id for p in people]
    if body.action in ("enrich", "verify"):
        if len(ids) <= 5:
            out = [{"id": p.id, **enrich_person(db, ctx.org, ctx.ws_id, p, body.phone)} for p in people]
            db.commit()
            return {"results": out}
        job = m.Job(organization_id=ctx.org_id, workspace_id=ctx.ws_id, type="enrich", priority=5,
                    payload={"person_ids": ids, "phone": body.phone}, total=len(ids), created_by=ctx.actor_id)
        db.add(job)
        db.commit()
        return {"job_id": job.id}
    if body.action == "add_to_list":
        lst = tenant_get(ctx, m.LeadList, body.list_id or "")
        existing = set(db.scalars(select(m.ListMember.person_id).where(m.ListMember.list_id == lst.id)))
        for i in ids:
            if i not in existing:
                db.add(m.ListMember(list_id=lst.id, person_id=i))
    elif body.action == "add_to_campaign":
        camp = tenant_get(ctx, m.Campaign, body.campaign_id or "")
        existing = set(db.scalars(select(m.CampaignLead.person_id).where(m.CampaignLead.campaign_id == camp.id)))
        for n, i in enumerate(ids):
            if i not in existing:
                db.add(m.CampaignLead(organization_id=ctx.org_id, campaign_id=camp.id, person_id=i,
                                      status="active" if camp.status == "RUNNING" else "pending",
                                      next_action_at=m.utcnow() if camp.status == "RUNNING" else None,
                                      variant="A" if n % 2 == 0 else "B"))
    elif body.action == "suppress":
        from ..services.engine import suppress_person
        for p in people:
            suppress_person(db, ctx.org_id, p, "all", "manual bulk suppression", ctx.actor_id)
    elif body.action == "assign":
        for p in people:
            p.owner_id = body.owner_id
    elif body.action == "delete":
        db.execute(delete(m.Person).where(m.Person.id.in_(ids)))
    core.audit_ctx(ctx, f"leads.bulk_{body.action}", "person", "", {"count": len(ids)})
    db.commit()
    return {"ok": True, "count": len(ids)}


@router.get("/leads-export")
def export(q: str = "", stage: str = "", list_id: str = "", ctx: Ctx = Depends(require("data.export"))):
    """Export respects provider licence (PRD 226-227): fields from non-exportable providers are withheld."""
    db = ctx.db
    providers = {p.id: p for p in db.scalars(select(m.Provider))}
    rows = list(db.scalars(_lead_query(ctx, q, stage, "", "", list_id).limit(10000)))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["full_name", "title", "company", "email", "email_status", "phone", "linkedin_url", "city", "country",
                "stage", "icp_score", "intent_score", "source"])
    withheld = 0
    for p in rows:
        comp = db.get(m.Company, p.company_id) if p.company_id else None
        phone_obs = db.scalars(select(m.FieldObservation).where(m.FieldObservation.entity_id == p.id,
                                                                m.FieldObservation.field_name == "phone")).first()
        phone = p.phone
        if phone_obs and phone_obs.provider in providers and not (providers[phone_obs.provider].license or {}).get("export_allowed", True):
            phone, withheld = "[licence: not exportable]", withheld + 1
        w.writerow([p.full_name, p.title, comp.name if comp else "", p.email, p.email_status, phone, p.linkedin_url,
                    p.city, p.country, p.stage, p.icp_score, p.intent_score, p.source])
    core.audit_ctx(ctx, "data.exported", "person", "", {"rows": len(rows), "withheld_fields": withheld})
    core.emit(db, ctx.org_id, "data_exported", "", {"rows": len(rows)})
    db.commit()
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=leads.csv"})


# ------------------------------------------------------------------ lists & saved searches (PRD 67-68)
class ListIn(BaseModel):
    name: str
    type: str = "static"
    definition: dict = {}


@router.get("/lists")
def lists(ctx: Ctx = Depends(require("leads.view"))):
    out = []
    for lst in ctx.db.scalars(select(m.LeadList).where(m.LeadList.organization_id == ctx.org_id)
                              .order_by(m.LeadList.created_at.desc())):
        n = ctx.db.scalar(select(func.count()).where(m.ListMember.list_id == lst.id))
        out.append({"id": lst.id, "name": lst.name, "type": lst.type, "definition": lst.definition, "members": n,
                    "summary": describe_criteria(lst.definition.get("criteria", {})) if lst.type == "saved_search" else ""})
    return out


@router.post("/lists")
def create_list(body: ListIn, ctx: Ctx = Depends(require("lists.manage"))):
    lst = m.LeadList(organization_id=ctx.org_id, workspace_id=ctx.ws_id, name=body.name, type=body.type,
                     definition=body.definition)
    ctx.db.add(lst)
    ctx.db.commit()
    return {"id": lst.id}


@router.post("/lists/{lid}/refresh")
def refresh_list(lid: str, ctx: Ctx = Depends(require("search.run"))):
    """Saved search stores the definition, not results; refreshing re-runs it."""
    lst = tenant_get(ctx, m.LeadList, lid)
    if lst.type != "saved_search":
        raise APIError(400, "NOT_SAVED_SEARCH", "Only saved searches can be refreshed.")
    res = search_companies(ctx, criteria=lst.definition.get("criteria", {}), limit=lst.definition.get("limit", 50))
    return {"returned": res["returned"], "total_matches": res["total_matches"]}


@router.delete("/lists/{lid}")
def delete_list(lid: str, ctx: Ctx = Depends(require("lists.manage"))):
    ctx.db.delete(tenant_get(ctx, m.LeadList, lid))
    ctx.db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ import (PRD 65-66)
FIELD_ALIASES = {
    "first_name": ["first_name", "firstname", "first name", "given name"], "last_name": ["last_name", "lastname", "last name", "surname"],
    "full_name": ["full_name", "name", "full name", "contact name"], "email": ["email", "email address", "e-mail", "work email"],
    "phone": ["phone", "mobile", "phone number", "whatsapp"], "linkedin_url": ["linkedin_url", "linkedin", "linkedin url"],
    "company": ["company", "company name", "organization", "account"], "company_domain": ["company_domain", "domain", "website"],
    "title": ["job_title", "title", "designation", "role"], "country": ["country"], "city": ["city", "location"],
}


def _read_upload(raw: bytes, filename: str) -> list[dict]:
    if filename.lower().endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        ws = wb.active
        it = ws.iter_rows(values_only=True)
        headers = [str(h or "").strip() for h in next(it, [])]
        return [{headers[i]: ("" if v is None else str(v)) for i, v in enumerate(r) if i < len(headers)} for r in it][:50000]
    text = raw.decode("utf-8-sig", errors="replace")
    return list(csv.DictReader(io.StringIO(text)))[:50000]


def _auto_map(headers: list[str]) -> dict:
    mapping = {}
    for h in headers:
        hl = h.lower().strip()
        for field, aliases in FIELD_ALIASES.items():
            if hl in aliases and field not in mapping.values():
                mapping[h] = field
                break
    return mapping


def _sanitize(v: str) -> str:
    v = (v or "").strip()[:500]
    return "'" + v if v[:1] in ("=", "+", "@") and not re.fullmatch(r"\+?[\d\s\-()]+", v) else v  # CSV-injection guard


@router.post("/import")
async def import_leads(file: UploadFile = File(...), mapping: str = Form(""), commit: bool = Form(False),
                       list_name: str = Form(""), ctx: Ctx = Depends(require("data.import"))):
    import json
    raw = await file.read()
    if len(raw) > 15 * 1024 * 1024:
        raise APIError(413, "FILE_TOO_LARGE", "Max 15 MB.")
    rows = _read_upload(raw, file.filename or "")
    headers = list(rows[0].keys()) if rows else []
    mp = json.loads(mapping) if mapping else _auto_map(headers)
    if not commit:
        return {"headers": headers, "mapping": mp, "rows": len(rows), "preview": rows[:5],
                "fields": list(FIELD_ALIASES.keys())}
    from ..services.engine import is_suppressed
    from ..services.providers import SyntaxVerificationProvider
    db = ctx.db
    stats = {"imported": 0, "duplicates": 0, "invalid_email": 0, "missing_identity": 0, "suppressed": 0,
             "needs_enrichment": 0, "errors": 0}
    lst = None
    if list_name:
        lst = m.LeadList(organization_id=ctx.org_id, workspace_id=ctx.ws_id, name=list_name, type="imported")
        db.add(lst)
        db.flush()
    verifier = SyntaxVerificationProvider()
    for row in rows:
        try:
            rec = {f: _sanitize(row.get(h, "")) for h, f in mp.items() if f}
            if not rec.get("first_name") and rec.get("full_name"):
                parts = rec["full_name"].split()
                rec["first_name"], rec["last_name"] = parts[0], " ".join(parts[1:])
            email = rec.get("email", "").lower()
            if email:
                st = verifier.verify(email, {})["status"]
                if st == "INVALID":
                    stats["invalid_email"] += 1
                    email = ""
                rec["email"] = email
            if not (email or rec.get("phone") or rec.get("linkedin_url") or
                    (rec.get("first_name") and (rec.get("company") or rec.get("company_domain")))):
                stats["missing_identity"] += 1
                continue
            comp = None
            dom = (rec.get("company_domain") or (email.split("@")[1] if email else "")).lower()
            dom = dom.removeprefix("https://").removeprefix("http://").removeprefix("www.").split("/")[0]
            if rec.get("company") or dom:
                comp, _ = upsert_company(db, ctx.org_id, ctx.ws_id, {"name": rec.get("company") or dom, "domain": dom,
                                                                     "website": f"https://{dom}" if dom else "",
                                                                     "country": rec.get("country", ""), "city": rec.get("city", "")},
                                         "customer_import")
                if not comp.scores:
                    comp.scores, comp.score_explanation = score_company(company_dict(comp), workspace_icp(db, ctx.workspace))
            from ..services.intelligence import find_duplicate_person
            payload = {**rec, "company_id": comp.id if comp else None,
                       "custom_fields": {k: _sanitize(v) for k, v in row.items() if k not in mp and v}}
            if find_duplicate_person(db, ctx.ws_id, payload):
                stats["duplicates"] += 1
                continue
            p, _ = upsert_person(db, ctx.org_id, ctx.ws_id, payload, "customer_import")
            p.email_status = verifier.verify(p.email, {"confidence": 0.8})["status"] if p.email else "UNKNOWN"
            if comp:
                p.icp_score = int((comp.scores or {}).get("icp", 0))
            if is_suppressed(db, ctx.org_id, ctx.ws_id, p):
                stats["suppressed"] += 1
                p.stage = "Suppressed"
            if not p.email:
                stats["needs_enrichment"] += 1
            if lst:
                db.add(m.ListMember(list_id=lst.id, person_id=p.id))
            stats["imported"] += 1
            core.emit(db, ctx.org_id, "lead.created", p.id, {"source": "import"})
        except Exception:  # noqa: BLE001 - never fail the whole file for bad rows
            stats["errors"] += 1
    core.audit_ctx(ctx, "data.imported", "list", lst.id if lst else "", stats)
    db.commit()
    return {"stats": stats, "list_id": lst.id if lst else None}


# ------------------------------------------------------------------ jobs (PRD 69)
@router.get("/jobs")
def jobs(ctx: Ctx = Depends(require("leads.view"))):
    return [_job(j) for j in ctx.db.scalars(select(m.Job).where(m.Job.organization_id == ctx.org_id,
                                                                m.Job.type != "simulated_reply")
                                            .order_by(m.Job.created_at.desc()).limit(50))]


@router.get("/jobs/{jid}")
def job(jid: str, ctx: Ctx = Depends(require("leads.view"))):
    return _job(tenant_get(ctx, m.Job, jid))


@router.post("/jobs/{jid}/cancel")
def cancel_job(jid: str, ctx: Ctx = Depends(require("campaigns.manage"))):
    j = tenant_get(ctx, m.Job, jid)
    if j.status in ("QUEUED", "RUNNING", "PAUSED"):
        j.status = "CANCELLED"
    ctx.db.commit()
    return _job(j)


def _job(j: m.Job) -> dict:
    pct = round(100 * j.progress / j.total) if j.total else (100 if j.status == "COMPLETED" else 0)
    return {"id": j.id, "type": j.type, "status": j.status, "progress": j.progress, "total": j.total, "percent": pct,
            "result": j.result, "error": j.error, "created_at": j.created_at.isoformat()}


@router.get("/providers/leadocean")
def leadocean_status(ctx: Ctx = Depends(require("integrations.manage"))):
    """LeadOcean account: plan and records left this month (free call). Key stays server-side."""
    from ..services.leadocean import LeadOceanError, get_account
    key = core.get_secret(ctx.db, ctx.org_id, "LEADOCEAN_API_KEY")
    if not key:
        return {"configured": False}
    try:
        acc = get_account(key)
    except (LeadOceanError, Exception) as exc:  # noqa: BLE001
        return {"configured": True, "ok": False, "error": str(exc)[:200]}
    rec = acc.get("records") or {}
    return {"configured": True, "ok": True, "plan": (acc.get("account") or {}).get("plan"),
            "records_used": rec.get("used"), "records_remaining": rec.get("remaining"),
            "records_ceiling": rec.get("ceiling"), "resets_at": rec.get("resetsAt"), "when_over": rec.get("whenOver")}


@router.get("/providers/status")
def provider_status(ctx: Ctx = Depends(require("leads.view"))):
    """Data Sources page (tenant view): which providers are active, with licence rules - no secrets."""
    return [{"id": p.id, "name": p.name, "kind": p.kind, "enabled": p.enabled, "priority": p.priority,
             "license": p.license, "description": p.description,
             "needs_credential": p.requires_secret or None,
             "credential_configured": bool(core.get_secret(ctx.db, ctx.org_id, p.requires_secret)) if p.requires_secret else None}
            for p in ctx.db.scalars(select(m.Provider).order_by(m.Provider.kind, m.Provider.priority))
            if p.enabled or not p.id.startswith("demo")]


_ = ProviderRouter
