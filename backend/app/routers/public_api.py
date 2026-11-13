"""Public API /api/v1 (PRD 116-124, 224, 284).

Auth: `Authorization: Bearer gtm_live_...` or `X-API-Key`. Keys are created by
workspace admins and act with the role chosen at creation. Every write accepts
an `Idempotency-Key` header; replays return the stored response.
"""
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from .. import models as m
from ..deps import APIError, Ctx, get_ctx, require, tenant_get
from ..services import core
from ..services.intelligence import enrich_person, person_dict, search_companies, search_people
from . import admin, data, gtm

router = APIRouter(prefix="/api/v1", tags=["public-api"])


def api_ctx(*perms):
    base = require(*perms)

    def dep(ctx: Ctx = Depends(get_ctx)) -> Ctx:
        ctx = base(ctx)
        if ctx.org:
            core.require_entitlement(ctx.db, ctx.org, "api_access")
            core.meter(ctx.db, ctx.org_id, "api_call", 1, ws_id=ctx.ws_id, enforce=False)
        return ctx
    return dep


def idempotent(request: Request, ctx: Ctx, key: str | None, fn):
    if not key:
        return fn()
    rec_key = f"{ctx.org_id}:{request.url.path}:{key}"[:200]
    rec = ctx.db.get(m.IdempotencyRecord, rec_key)
    if rec:
        return JSONResponse(rec.response, status_code=rec.status_code, headers={"Idempotent-Replay": "true"})
    result = fn()
    ctx.db.add(m.IdempotencyRecord(key=rec_key, organization_id=ctx.org_id, endpoint=request.url.path,
                                   response=result if isinstance(result, (dict, list)) else {"result": str(result)}))
    ctx.db.commit()
    return result


class CompanySearchIn(BaseModel):
    query: str = ""
    filters: dict | None = None
    criteria: list[str] = []
    limit: int = Field(100, ge=1, le=500)


@router.post("/search/companies")
def api_search_companies(body: CompanySearchIn, ctx: Ctx = Depends(api_ctx("search.run"))):
    res = search_companies(ctx, query=body.query, criteria=body.filters, limit=body.limit)
    if body.criteria:
        crit = data.custom_criterion(data.CriterionIn(criterion=body.criteria[0],
                                                      company_ids=[r["id"] for r in res["results"]]), ctx)
        res["criteria_scores"] = crit["results"]
    return res


class PeopleSearchIn(BaseModel):
    company_query: str = ""
    job_titles: list[str] = []
    company_ids: list[str] | None = None
    limit: int = Field(100, ge=1, le=500)


@router.post("/search/people")
def api_search_people(body: PeopleSearchIn, ctx: Ctx = Depends(api_ctx("search.run"))):
    from ..services.nlp import parse_search
    crit = parse_search(body.company_query) if body.company_query else {}
    crit["job_titles"] = body.job_titles or crit.get("job_titles", [])
    return search_people(ctx, criteria=crit, company_ids=body.company_ids, limit=body.limit)


class EnrichIn(BaseModel):
    person_id: str


@router.post("/enrich/email")
def api_enrich_email(body: EnrichIn, request: Request, ctx: Ctx = Depends(api_ctx("enrich.run")),
                     idempotency_key: str | None = Header(default=None)):
    def run():
        p = tenant_get(ctx, m.Person, body.person_id)
        r = enrich_person(ctx.db, ctx.org, ctx.ws_id, p)
        ctx.db.commit()
        return {"person_id": p.id, **r}
    return idempotent(request, ctx, idempotency_key, run)


@router.post("/enrich/phone")
def api_enrich_phone(body: EnrichIn, request: Request, ctx: Ctx = Depends(api_ctx("enrich.run")),
                     idempotency_key: str | None = Header(default=None)):
    def run():
        p = tenant_get(ctx, m.Person, body.person_id)
        r = enrich_person(ctx.db, ctx.org, ctx.ws_id, p, phone=True)
        ctx.db.commit()
        return {"person_id": p.id, **r}
    return idempotent(request, ctx, idempotency_key, run)


class ResearchIn(BaseModel):
    company_id: str
    depth: str = Field("standard", pattern="^(quick|standard|deep)$")


@router.post("/research/company")
def api_research(body: ResearchIn, request: Request, ctx: Ctx = Depends(api_ctx("research.run")),
                 idempotency_key: str | None = Header(default=None)):
    """Returns an async task (PRD 120)."""
    def run():
        c = tenant_get(ctx, m.Company, body.company_id)
        job = m.Job(organization_id=ctx.org_id, workspace_id=ctx.ws_id, type="research_company",
                    payload={"company_ids": [c.id], "depth": body.depth}, total=1, created_by=ctx.actor_id,
                    request_id=ctx.request_id)
        ctx.db.add(job)
        ctx.db.commit()
        return {"task_id": job.id, "status": job.status}
    return idempotent(request, ctx, idempotency_key, run)


@router.get("/tasks/{tid}")
def api_task(tid: str, ctx: Ctx = Depends(api_ctx("leads.view"))):
    j = tenant_get(ctx, m.Job, tid)
    res = data._job(j)
    if j.type == "research_company" and j.status == "COMPLETED":
        res["research"] = {cid: ctx.db.get(m.Company, cid).research for cid in j.payload.get("company_ids", [])
                           if ctx.db.get(m.Company, cid)}
    return res


@router.get("/leads")
def api_leads(q: str = "", stage: str = "", limit: int = 100, offset: int = 0, ctx: Ctx = Depends(api_ctx("leads.view"))):
    return data.leads(q=q, stage=stage, temperature="", owner="", list_id="", sort="score", limit=limit, offset=offset, ctx=ctx)


@router.get("/leads/{pid}")
def api_lead(pid: str, ctx: Ctx = Depends(api_ctx("leads.view"))):
    return person_dict(ctx.db, tenant_get(ctx, m.Person, pid))


class LeadIn(BaseModel):
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    linkedin_url: str = ""
    title: str = ""
    company: str = ""
    company_domain: str = ""
    country: str = ""


@router.post("/leads")
def api_create_lead(body: LeadIn, request: Request, ctx: Ctx = Depends(api_ctx("data.import")),
                    idempotency_key: str | None = Header(default=None)):
    def run():
        from ..services.intelligence import upsert_company, upsert_person
        comp = None
        if body.company or body.company_domain:
            comp, _ = upsert_company(ctx.db, ctx.org_id, ctx.ws_id, {"name": body.company or body.company_domain,
                                                                     "domain": body.company_domain}, "api")
        p, created = upsert_person(ctx.db, ctx.org_id, ctx.ws_id, {**body.model_dump(), "company_id": comp.id if comp else None}, "api")
        if created:
            core.emit(ctx.db, ctx.org_id, "lead.created", p.id, {"source": "api"})
        ctx.db.commit()
        return {"id": p.id, "created": created}
    return idempotent(request, ctx, idempotency_key, run)


@router.get("/lists")
def api_lists(ctx: Ctx = Depends(api_ctx("leads.view"))):
    return data.lists(ctx)


@router.get("/missions")
def api_missions(ctx: Ctx = Depends(api_ctx("leads.view"))):
    return gtm.missions(ctx)


@router.post("/missions")
def api_create_mission(body: gtm.MissionIn, request: Request, ctx: Ctx = Depends(api_ctx("missions.manage")),
                       idempotency_key: str | None = Header(default=None)):
    return idempotent(request, ctx, idempotency_key, lambda: gtm.create_mission(body, ctx))


@router.get("/campaigns")
def api_campaigns(ctx: Ctx = Depends(api_ctx("leads.view"))):
    return gtm.campaigns(ctx)


@router.post("/campaigns")
def api_create_campaign(body: gtm.CampaignIn, request: Request, ctx: Ctx = Depends(api_ctx("campaigns.manage")),
                        idempotency_key: str | None = Header(default=None)):
    return idempotent(request, ctx, idempotency_key, lambda: gtm.create_campaign(body, ctx))


@router.get("/campaigns/{cid}")
def api_campaign(cid: str, ctx: Ctx = Depends(api_ctx("leads.view"))):
    return gtm.campaign_detail(cid, ctx)


@router.patch("/campaigns/{cid}")
def api_update_campaign(cid: str, body: dict, ctx: Ctx = Depends(api_ctx("campaigns.manage"))):
    return gtm.update_campaign(cid, body, ctx)


@router.post("/campaigns/{cid}/{action}")
def api_campaign_action(cid: str, action: str, request: Request, ctx: Ctx = Depends(api_ctx("campaigns.manage")),
                        idempotency_key: str | None = Header(default=None)):
    if action not in ("start", "pause", "archive", "resume"):
        raise APIError(404, "UNKNOWN_ACTION", action)
    return idempotent(request, ctx, idempotency_key, lambda: gtm.campaign_action(cid, action, ctx))


@router.get("/inbox")
def api_inbox(filter: str = "all", ctx: Ctx = Depends(api_ctx("conversations.view"))):
    return gtm.inbox(filter, ctx)


@router.get("/inbox/{cid}")
def api_conversation(cid: str, ctx: Ctx = Depends(api_ctx("conversations.view"))):
    return gtm.conversation(cid, ctx)


@router.post("/inbox/{cid}/reply")
def api_reply(cid: str, body: gtm.ReplyIn, request: Request, ctx: Ctx = Depends(api_ctx("conversations.reply")),
              idempotency_key: str | None = Header(default=None)):
    return idempotent(request, ctx, idempotency_key, lambda: gtm.reply(cid, body, ctx))


@router.post("/inbox/{cid}/takeover")
def api_takeover(cid: str, ctx: Ctx = Depends(api_ctx("conversations.takeover"))):
    return gtm.takeover(cid, ctx)


@router.post("/inbox/{cid}/return-to-ai")
def api_return(cid: str, ctx: Ctx = Depends(api_ctx("conversations.takeover"))):
    return gtm.return_to_ai(cid, ctx)


@router.get("/meetings")
def api_meetings(ctx: Ctx = Depends(api_ctx("meetings.view"))):
    return gtm.meetings(ctx)


@router.get("/analytics")
def api_analytics(ctx: Ctx = Depends(api_ctx("dashboard.view"))):
    d = gtm.dashboard(ctx)
    return {"kpis": d["kpis"], "funnel": d["funnel"],
            "campaigns": [{"id": c["id"], "name": c["name"], "status": c["status"], **c["stats"]} for c in d["campaigns"]]}


@router.get("/usage")
def api_usage(ctx: Ctx = Depends(api_ctx("billing.view"))):
    acc = core.credit_account(ctx.db, ctx.org_id)
    return {"balance_gtu": round(acc.balance, 2), "allowance_gtu": acc.monthly_allowance,
            "last_30_days": core.usage_summary(ctx.db, ctx.org_id)}


@router.get("/suppression")
def api_suppression(ctx: Ctx = Depends(api_ctx("leads.view"))):
    return admin.list_suppression(ctx)


@router.post("/suppression")
def api_add_suppression(body: admin.SuppressIn, ctx: Ctx = Depends(api_ctx("suppression.manage"))):
    return admin.add_suppression(body, ctx)


@router.post("/inbound")
def api_inbound(body: dict, ctx: Ctx = Depends(api_ctx("conversations.reply"))):
    """Provider webhook bridge: {email|phone, text, channel}. Routes to the reply agent."""
    from ..services import engine
    q = select(m.Person).where(m.Person.organization_id == ctx.org_id)
    if body.get("email"):
        q = q.where(m.Person.email == body["email"].lower())
    elif body.get("phone"):
        q = q.where(m.Person.phone == body["phone"])
    else:
        raise APIError(400, "MISSING_IDENTITY", "Provide email or phone.")
    p = ctx.db.scalars(q).first()
    if not p:
        raise APIError(404, "NOT_FOUND", "No lead with that identity.")
    return engine.handle_inbound(ctx.db, ctx.org, p, str(body.get("text", ""))[:5000], body.get("channel", "email"))
