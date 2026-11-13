"""Onboarding, missions, campaigns, inbox, meetings, LinkedIn tasks, analytics, command bar, agent trace."""
import re
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from .. import models as m
from ..deps import APIError, Ctx, require, tenant_get
from ..services import core, engine, llm
from ..services.intelligence import (build_audience, build_mission_from_instruction, company_dict,
                                     generate_campaigns_for_mission, generate_message, omnichannel_sequence,
                                     person_dict, search_companies, search_people, understand_business)
from ..services.nlp import HOT

router = APIRouter(prefix="/app", tags=["gtm"])


# ------------------------------------------------------------------ onboarding (PRD 12-13, 192, 211-212)
class OnboardIn(BaseModel):
    website: str = ""
    description: str = ""
    apply: bool = True


@router.post("/onboarding/analyze")
def onboard(body: OnboardIn, ctx: Ctx = Depends(require("workspace.manage"))):
    db, org = ctx.db, ctx.org
    profile = understand_business(db, org, body.website or org.website, body.description)
    org.profile = profile
    if body.website:
        org.website = body.website
    created = []
    if body.apply:
        existing = {o.name for o in db.scalars(select(m.Offer).where(m.Offer.organization_id == org.id))}
        for o in profile.get("primary_offers", [])[:5]:
            if o.get("name") and o["name"] not in existing:
                db.add(m.Offer(organization_id=org.id, workspace_id=ctx.ws_id, name=o["name"],
                               description=o.get("description", ""), problems=o.get("problems", []),
                               benefits=o.get("benefits", [])))
                created.append(o["name"])
        if profile.get("icp"):
            ws = ctx.workspace
            ws.settings = {**(ws.settings or {}), "icp": profile["icp"]}
    core.audit_ctx(ctx, "onboarding.analyzed", "organization", org.id, {"website": org.website})
    db.commit()
    return {"profile": profile, "offers_created": created}


# ------------------------------------------------------------------ missions (PRD 9, 37)
class MissionIn(BaseModel):
    instruction: str = ""
    name: str | None = None
    objective: str | None = None
    icp: dict | None = None
    personas: list[str] | None = None
    channels: list[str] | None = None
    budget_monthly: float | None = None
    meeting_goal: int = 10
    autonomy_mode: str = Field("full_autonomous", pattern="^(assistant|copilot|autonomous|full_autonomous)$")
    offer_id: str | None = None
    auto_generate_campaigns: bool = True
    auto_build_audience: bool = False


def mission_dict(db, x: m.Mission) -> dict:
    camps = list(db.scalars(select(m.Campaign).where(m.Campaign.mission_id == x.id)))
    stats = [engine.campaign_stats(db, c.id) for c in camps]
    return {"id": x.id, "name": x.name, "objective": x.objective, "instruction": x.instruction, "icp": x.icp,
            "personas": x.personas, "signals": x.signals, "channels": x.channels, "budget_monthly": x.budget_monthly,
            "meeting_goal": x.meeting_goal, "autonomy_mode": x.autonomy_mode, "status": x.status, "memory": x.memory,
            "offer_id": x.offer_id, "campaigns": len(camps),
            "meetings": sum(s["meetings"] for s in stats), "hot": sum(s["hot"] for s in stats),
            "spent": round(sum(s["spend"] for s in stats), 2), "created_at": x.created_at.isoformat()}


@router.get("/missions")
def missions(ctx: Ctx = Depends(require("leads.view"))):
    return [mission_dict(ctx.db, x) for x in ctx.db.scalars(select(m.Mission).where(
        m.Mission.organization_id == ctx.org_id, m.Mission.workspace_id == ctx.ws_id).order_by(m.Mission.created_at.desc()))]


@router.post("/missions/plan")
def plan_mission(body: MissionIn, ctx: Ctx = Depends(require("missions.manage"))):
    """Preview what the agent would create from a natural-language instruction."""
    return build_mission_from_instruction(ctx, body.instruction)


@router.post("/missions")
def create_mission(body: MissionIn, ctx: Ctx = Depends(require("missions.manage"))):
    db = ctx.db
    plan = build_mission_from_instruction(ctx, body.instruction) if body.instruction else {}
    if body.channels:
        for ch in body.channels:
            feat = engine.CHANNEL_FEATURE.get(ch)
            if feat and not core.entitled(db, ctx.org, feat):
                raise APIError(402, "NOT_ENTITLED", f"Your plan does not include the {ch} channel.")
    ws_memory = (ctx.workspace.settings or {}).get("memory", [])
    icp = body.icp or plan.get("icp", {})
    for note in ws_memory:   # workspace memory influences all missions (PRD 36)
        mm = re.search(r"(?:do not|don't|never) target ([a-z &]+)", note.lower())
        if mm:
            from ..services.nlp import INDUSTRY_KEYWORDS
            for ind, kws in INDUSTRY_KEYWORDS.items():
                if any(k in mm.group(1) for k in kws):
                    icp["exclude_industries"] = sorted(set(icp.get("exclude_industries", [])) | {ind})
                    icp["industries"] = [i for i in icp.get("industries", []) if i != ind]
    offer_id = body.offer_id
    if not offer_id:
        offers = list(db.scalars(select(m.Offer).where(m.Offer.organization_id == ctx.org_id)))
        hint = (plan.get("offer_hint") or "").lower()
        offer_id = next((o.id for o in offers if hint and any(w in o.name.lower() for w in hint.split())), None) or \
            (offers[0].id if offers else None)
    x = m.Mission(organization_id=ctx.org_id, workspace_id=ctx.ws_id, name=body.name or plan.get("name") or "New mission",
                  objective=body.objective or plan.get("objective", "Generate qualified meetings"),
                  instruction=body.instruction, icp=icp, personas=body.personas or plan.get("personas", []),
                  signals=plan.get("signals", []), channels=body.channels or plan.get("channels", ["email"]),
                  budget_monthly=body.budget_monthly or plan.get("budget_monthly", 5000), meeting_goal=body.meeting_goal,
                  autonomy_mode=body.autonomy_mode, offer_id=offer_id, created_by=ctx.actor_id,
                  memory=[f"Workspace memory: {n}" for n in ws_memory])
    db.add(x)
    db.flush()
    camps = generate_campaigns_for_mission(db, ctx, x) if body.auto_generate_campaigns else []
    core.audit_ctx(ctx, "mission.created", "mission", x.id, {"name": x.name})
    db.commit()
    audience = []
    if body.auto_build_audience:
        for c in camps:
            audience.append({"campaign_id": c.id, **build_audience(ctx, c)})
    return {"mission": mission_dict(db, x), "campaign_ids": [c.id for c in camps], "audience": audience}


@router.get("/missions/{mid}")
def mission_detail(mid: str, ctx: Ctx = Depends(require("leads.view"))):
    x = tenant_get(ctx, m.Mission, mid)
    camps = [campaign_dict(ctx.db, c) for c in ctx.db.scalars(select(m.Campaign).where(m.Campaign.mission_id == x.id))]
    decisions = [_decision(d) for d in ctx.db.scalars(select(m.AIDecision).where(
        m.AIDecision.organization_id == ctx.org_id, m.AIDecision.entity_id.in_([x.id] + [c["id"] for c in camps]))
        .order_by(m.AIDecision.created_at.desc()).limit(30))]
    return {**mission_dict(ctx.db, x), "campaign_list": camps, "decisions": decisions}


@router.patch("/missions/{mid}")
def update_mission(mid: str, body: dict, ctx: Ctx = Depends(require("missions.manage"))):
    x = tenant_get(ctx, m.Mission, mid)
    if "budget_monthly" in body and float(body["budget_monthly"]) > x.budget_monthly and not ctx.has("autonomy.configure"):
        raise APIError(403, "FORBIDDEN", "Raising a hard budget requires an admin (autonomy.configure).")
    for k in ("name", "objective", "icp", "personas", "channels", "budget_monthly", "meeting_goal", "autonomy_mode", "status"):
        if k in body:
            setattr(x, k, body[k])
    core.audit_ctx(ctx, "mission.updated", "mission", mid, body)
    ctx.db.commit()
    return mission_dict(ctx.db, x)


@router.post("/missions/{mid}/campaigns")
def mission_generate(mid: str, ctx: Ctx = Depends(require("missions.manage"))):
    x = tenant_get(ctx, m.Mission, mid)
    camps = generate_campaigns_for_mission(ctx.db, ctx, x)
    ctx.db.commit()
    return {"campaign_ids": [c.id for c in camps]}


# ------------------------------------------------------------------ campaigns (PRD 41-45, 187-188)
def campaign_dict(db, c: m.Campaign) -> dict:
    s = engine.campaign_stats(db, c.id)
    label = {"RUNNING": (c.agent_state or {}).get("label", "Running"), "PAUSED": "Paused",
             "BUDGET_EXHAUSTED": "Needs attention", "COMPLIANCE_PAUSED": "Needs attention"}.get(c.status, c.status.title())
    return {"id": c.id, "name": c.name, "mission_id": c.mission_id, "objective": c.objective, "status": c.status,
            "status_label": label, "audience": c.audience, "target_roles": c.target_roles, "channels": c.channels,
            "sequence": c.sequence, "messaging_strategy": c.messaging_strategy, "budget_daily": c.budget_daily,
            "budget_total": c.budget_total, "spent": round(c.spent or 0, 2), "autonomy_mode": c.autonomy_mode,
            "daily_send_limit": c.daily_send_limit, "offer_id": c.offer_id, "negative_criteria": c.negative_criteria,
            "agent_state": c.agent_state, "stats": s, "created_at": c.created_at.isoformat()}


class CampaignIn(BaseModel):
    name: str = ""
    instruction: str = ""   # "Create a campaign for startup founders in Tamil Nadu" (PRD 44)
    mission_id: str | None = None
    objective: str = "Book qualified meetings"
    audience: dict | None = None
    target_roles: list[str] | None = None
    channels: list[str] = ["email"]
    sequence: list[dict] | None = None
    budget_total: float = 1000
    budget_daily: float = 0
    daily_send_limit: int = 50
    autonomy_mode: str = "full_autonomous"
    offer_id: str | None = None
    negative_criteria: list[str] = []
    messaging_strategy: dict = {}


@router.get("/campaigns")
def campaigns(ctx: Ctx = Depends(require("leads.view"))):
    return [campaign_dict(ctx.db, c) for c in ctx.db.scalars(select(m.Campaign).where(
        m.Campaign.organization_id == ctx.org_id, m.Campaign.workspace_id == ctx.ws_id,
        m.Campaign.status != "ARCHIVED").order_by(m.Campaign.created_at.desc()))]


@router.post("/campaigns")
def create_campaign(body: CampaignIn, ctx: Ctx = Depends(require("campaigns.manage"))):
    db = ctx.db
    for ch in body.channels:
        feat = engine.CHANNEL_FEATURE.get(ch)
        if feat and not core.entitled(db, ctx.org, feat):
            raise APIError(402, "NOT_ENTITLED", f"Your plan does not include the {ch} channel.")
    audience, roles, name = body.audience, body.target_roles, body.name
    if body.instruction:
        plan = build_mission_from_instruction(ctx, body.instruction)
        audience = audience or plan["icp"]
        roles = roles or plan["personas"]
        name = name or plan["name"]
    if body.mission_id:
        tenant_get(ctx, m.Mission, body.mission_id)
    c = m.Campaign(organization_id=ctx.org_id, workspace_id=ctx.ws_id, mission_id=body.mission_id,
                   name=name or "New campaign", objective=body.objective, audience=audience or {},
                   target_roles=roles or ["Founder", "CEO"], channels=body.channels,
                   sequence=body.sequence or omnichannel_sequence(body.channels), budget_total=body.budget_total,
                   budget_daily=body.budget_daily or round(body.budget_total / 30, 2), daily_send_limit=body.daily_send_limit,
                   autonomy_mode=body.autonomy_mode, offer_id=body.offer_id, negative_criteria=body.negative_criteria,
                   messaging_strategy=body.messaging_strategy or {"tone": "consultative", "angle": "pain-based"},
                   created_by=ctx.actor_id)
    db.add(c)
    core.audit_ctx(ctx, "campaign.created", "campaign", c.id, {"name": c.name})
    core.emit(db, ctx.org_id, "campaign_created", c.id, {"name": c.name})
    db.commit()
    return campaign_dict(db, c)


@router.get("/campaigns/{cid}")
def campaign_detail(cid: str, ctx: Ctx = Depends(require("leads.view"))):
    db = ctx.db
    c = tenant_get(ctx, m.Campaign, cid)
    leads = []
    for cl, p in db.execute(select(m.CampaignLead, m.Person).join(m.Person, m.Person.id == m.CampaignLead.person_id)
                            .where(m.CampaignLead.campaign_id == c.id).limit(300)):
        leads.append({"id": p.id, "name": p.full_name, "title": p.title, "status": cl.status, "step": cl.current_step,
                      "variant": cl.variant, "stop_reason": cl.stop_reason, "temperature": p.temperature,
                      "next_action_at": cl.next_action_at.isoformat() if cl.next_action_at else None,
                      "company": db.get(m.Company, p.company_id).name if p.company_id and db.get(m.Company, p.company_id) else ""})
    decisions = [_decision(d) for d in db.scalars(select(m.AIDecision).where(m.AIDecision.entity_id == c.id)
                                                   .order_by(m.AIDecision.created_at.desc()).limit(20))]
    return {**campaign_dict(db, c), "leads": leads, "decisions": decisions, "variants": engine.variant_stats(db, c.id),
            "insight": _insight(db, c)}


def _insight(db, c: m.Campaign) -> dict:
    """Why is this campaign working? (PRD 188)"""
    rows = db.execute(select(m.Person.normalized_title, m.Company.industry, m.Conversation.temperature)
                      .join(m.Conversation, m.Conversation.person_id == m.Person.id)
                      .join(m.Company, m.Company.id == m.Person.company_id, isouter=True)
                      .where(m.Conversation.campaign_id == c.id)).all()
    if not rows:
        return {"summary": "Not enough replies yet to explain performance."}
    def top(idx):
        cnt = {}
        for r in rows:
            if r[2] in ("hot", "warm"):
                cnt[r[idx]] = cnt.get(r[idx], 0) + 1
        return max(cnt, key=cnt.get) if cnt else "n/a"
    if top(1) == "n/a":
        return {"summary": f"{len(rows)} conversation(s) so far, but no positive replies yet - too early to say what works."}
    v = engine.variant_stats(db, c.id)
    best_cta = "A (offer CTA)" if v["A"]["reply_rate"] >= v["B"]["reply_rate"] else "B (soft CTA)"
    return {"top_segment": top(1), "best_title": top(0), "best_channel": (c.channels or ["email"])[0],
            "best_opening": (c.messaging_strategy or {}).get("angle", "pain-based"), "best_cta": best_cta,
            "summary": f"Positive replies cluster in {top(1)} with {top(0)} personas; CTA variant {best_cta} leads."}


@router.patch("/campaigns/{cid}")
def update_campaign(cid: str, body: dict, ctx: Ctx = Depends(require("campaigns.manage"))):
    c = tenant_get(ctx, m.Campaign, cid)
    if "budget_total" in body and float(body["budget_total"]) > (c.budget_total or 0):
        mission = ctx.db.get(m.Mission, c.mission_id) if c.mission_id else None
        if mission and float(body["budget_total"]) > mission.budget_monthly:
            raise APIError(400, "BUDGET_EXCEEDS_MISSION", "Campaign budget cannot exceed the mission's hard budget.")
    for k in ("name", "objective", "audience", "target_roles", "channels", "sequence", "budget_total", "budget_daily",
              "daily_send_limit", "autonomy_mode", "offer_id", "negative_criteria", "messaging_strategy"):
        if k in body:
            setattr(c, k, body[k])
    core.audit_ctx(ctx, "campaign.updated", "campaign", cid, {k: body[k] for k in body if k != "sequence"})
    ctx.db.commit()
    return campaign_dict(ctx.db, c)


@router.post("/campaigns/{cid}/audience")
def campaign_audience(cid: str, body: dict | None = None, ctx: Ctx = Depends(require("campaigns.manage"))):
    c = tenant_get(ctx, m.Campaign, cid)
    res = build_audience(ctx, c, int((body or {}).get("limit", 40)))
    core.audit_ctx(ctx, "campaign.audience_built", "campaign", cid, {"added": res["added"]})
    ctx.db.commit()
    return res


@router.get("/campaigns/{cid}/preview")
def campaign_preview(cid: str, ctx: Ctx = Depends(require("leads.view"))):
    """Lead review before execution (PRD 45): counts, examples, sample messages, estimated spend, risks."""
    db = ctx.db
    c = tenant_get(ctx, m.Campaign, cid)
    pairs = list(db.execute(select(m.CampaignLead, m.Person).join(m.Person, m.Person.id == m.CampaignLead.person_id)
                            .where(m.CampaignLead.campaign_id == c.id)))
    people = [p for _, p in pairs]
    comp_ids = {p.company_id for p in people if p.company_id}
    examples = []
    for cl, p in pairs[:3]:
        comp = db.get(m.Company, p.company_id) if p.company_id else None
        step = dict(c.sequence[0]) if c.sequence else {"channel": "email", "type": "initial"}
        step["variant"] = cl.variant
        msg = generate_message(db, ctx.org, c, p, comp, step, 0)
        examples.append({"to": p.full_name, "title": p.title, "company": comp.name if comp else "", **msg})
    per_lead = sum(core.cost_of(db, engine.CHANNEL_METER[s["channel"]]) for s in c.sequence or []
                   if engine.CHANNEL_METER.get(s["channel"]))
    est = round(per_lead * len(people) + core.cost_of(db, "email_enrichment") * sum(1 for p in people if not p.email), 1)
    risks = []
    no_email = sum(1 for p in people if not p.email)
    if no_email:
        risks.append(f"{no_email} leads need email enrichment before sending.")
    suppressed = sum(1 for p in people if engine.is_suppressed(db, ctx.org_id, ctx.ws_id, p, "all", c.id))
    if suppressed:
        risks.append(f"{suppressed} leads are suppressed and will be skipped.")
    if c.budget_total and est > c.budget_total:
        risks.append(f"Estimated spend {est} GTU exceeds campaign budget {c.budget_total} GTU - sends stop at the cap.")
    if not core.can_afford(db, ctx.org_id, "email_send", len(people)):
        risks.append("Organization credit balance may not cover this campaign.")
    segs = {}
    for cid_ in comp_ids:
        comp = db.get(m.Company, cid_)
        segs[comp.industry] = segs.get(comp.industry, 0) + 1
    db.commit()
    return {"companies": len(comp_ids), "people": len(people), "examples": examples, "estimated_spend_gtu": est,
            "expected_capacity_per_day": c.daily_send_limit, "risks": risks or ["No blocking risks detected."],
            "top_segments": [{"segment": k, "companies": v} for k, v in sorted(segs.items(), key=lambda x: -x[1])]}


@router.post("/campaigns/{cid}/{action}")
def campaign_action(cid: str, action: str, ctx: Ctx = Depends(require("campaigns.manage"))):
    db = ctx.db
    c = tenant_get(ctx, m.Campaign, cid)
    if action == "start":
        if not db.scalar(select(func.count(m.CampaignLead.id)).where(m.CampaignLead.campaign_id == c.id)):
            raise APIError(400, "NO_AUDIENCE", "Build or add an audience before starting.")
        if c.status in ("BUDGET_EXHAUSTED",) and c.budget_total and c.spent >= c.budget_total:
            raise APIError(400, "BUDGET_EXHAUSTED", "Raise the campaign budget before restarting.")
        engine.start_campaign(db, c)
    elif action in ("pause", "resume"):
        if action == "pause":
            c.status = "PAUSED"
            core.emit(db, ctx.org_id, "campaign.paused", c.id, {"by": ctx.actor_id}, campaign_id=c.id)
        else:
            engine.start_campaign(db, c)
    elif action == "archive":
        c.status = "ARCHIVED"
    elif action == "run-now":
        if c.status != "RUNNING":
            raise APIError(400, "NOT_RUNNING", "Campaign is not running.")
        sent = engine.process_campaign(db, c)
        return {"sent": sent, **campaign_dict(db, c)}
    else:
        raise APIError(404, "UNKNOWN_ACTION", action)
    core.audit_ctx(ctx, f"campaign.{action}", "campaign", cid)
    db.commit()
    return campaign_dict(db, c)


# ------------------------------------------------------------------ inbox (PRD 70-77, 122)
def conv_dict(db, c: m.Conversation, with_messages=False) -> dict:
    p = db.get(m.Person, c.person_id)
    comp = db.get(m.Company, c.company_id) if c.company_id else None
    last = db.scalars(select(m.Message).where(m.Message.conversation_id == c.id).order_by(m.Message.created_at.desc())).first()
    d = {"id": c.id, "person": {"id": p.id, "name": p.full_name, "title": p.title, "email": p.email, "phone": p.phone,
                                "stage": p.stage} if p else None,
         "company": {"id": comp.id, "name": comp.name, "icp": (comp.scores or {}).get("icp"), "industry": comp.industry}
         if comp else None, "channel": c.channel, "status": c.status, "intent": c.intent, "sentiment": c.sentiment,
         "urgency": c.urgency, "temperature": c.temperature, "lead_score": c.lead_score, "ai_mode": c.ai_mode,
         "assigned_to": c.assigned_to, "needs_review": c.needs_review, "review_reason": c.review_reason,
         "draft_reply": c.draft_reply, "summary": c.summary, "next_action": c.next_action,
         "last_message": (last.content[:140] if last else ""), "last_direction": last.direction if last else "",
         "last_message_at": c.last_message_at.isoformat()}
    if with_messages:
        d["messages"] = [{"id": x.id, "direction": x.direction, "channel": x.channel, "subject": x.subject,
                          "content": x.content, "status": x.status, "error": x.error, "ai_generated": x.ai_generated,
                          "ai_model": x.ai_model, "evidence": x.evidence, "at": x.created_at.isoformat()}
                         for x in db.scalars(select(m.Message).where(m.Message.conversation_id == c.id).order_by(m.Message.created_at))]
        d["memory"] = c.memory
        d["why_hot"] = c.summary if c.temperature == "hot" else ""
        d["meetings"] = [{"id": x.id, "start_at": x.start_at.isoformat(), "status": x.status}
                         for x in db.scalars(select(m.Meeting).where(m.Meeting.conversation_id == c.id))]
    return d


@router.get("/inbox")
def inbox(filter: str = "all", ctx: Ctx = Depends(require("conversations.view"))):
    q = select(m.Conversation).where(m.Conversation.organization_id == ctx.org_id, m.Conversation.workspace_id == ctx.ws_id)
    has_inbound = select(m.Message.conversation_id).where(m.Message.direction == "inbound")
    if filter == "need_reply":
        q = q.where(m.Conversation.id.in_(has_inbound), m.Conversation.status == "open",
                    (m.Conversation.needs_review.is_(True)) | (m.Conversation.ai_mode == "human"))
    elif filter == "hot":
        q = q.where(m.Conversation.temperature == "hot")
    elif filter == "meetings":
        q = q.where(m.Conversation.id.in_(select(m.Meeting.conversation_id)))
    elif filter == "unsubscribe":
        q = q.where(m.Conversation.intent == "UNSUBSCRIBE")
    elif filter == "human_review":
        q = q.where(m.Conversation.needs_review.is_(True))
    elif filter == "ai_handled":
        q = q.where(m.Conversation.id.in_(has_inbound), m.Conversation.needs_review.is_(False), m.Conversation.ai_mode == "ai")
    elif filter == "replied":
        q = q.where(m.Conversation.id.in_(has_inbound))
    rows = list(ctx.db.scalars(q.order_by(m.Conversation.last_message_at.desc()).limit(200)))
    counts = {"human_review": ctx.db.scalar(select(func.count(m.Conversation.id)).where(
        m.Conversation.organization_id == ctx.org_id, m.Conversation.needs_review.is_(True))) or 0}
    return {"results": [conv_dict(ctx.db, c) for c in rows], "counts": counts}


@router.get("/inbox/{cid}")
def conversation(cid: str, ctx: Ctx = Depends(require("conversations.view"))):
    return conv_dict(ctx.db, tenant_get(ctx, m.Conversation, cid), True)


class ReplyIn(BaseModel):
    body: str = Field(min_length=1)
    subject: str = ""
    channel: str | None = None


@router.post("/inbox/{cid}/reply")
def reply(cid: str, body: ReplyIn, ctx: Ctx = Depends(require("conversations.reply"))):
    db = ctx.db
    c = tenant_get(ctx, m.Conversation, cid)
    p = db.get(m.Person, c.person_id)
    msg = engine.deliver(db, ctx.org, ctx.workspace, p, body.channel or (c.channel if c.channel != "linkedin" else "email"),
                         body.subject or "Re: our conversation", body.body, conv=c,
                         idempotency_key=f"manual:{cid}:{ctx.request_id}")
    c.needs_review, c.draft_reply = False, ""
    core.audit_ctx(ctx, "conversation.replied", "conversation", cid, {"status": msg.status})
    db.commit()
    if msg.status == "blocked":
        raise APIError(422, "CONTACT_SUPPRESSED" if "suppressed" in msg.error else "SEND_BLOCKED", msg.error)
    return conv_dict(db, c, True)


@router.post("/inbox/{cid}/approve")
def approve_draft(cid: str, body: dict | None = None, ctx: Ctx = Depends(require("exceptions.approve"))):
    c = tenant_get(ctx, m.Conversation, cid)
    if not (body or {}).get("body") and not c.draft_reply:
        raise APIError(400, "NO_DRAFT", "Nothing to approve.")
    text = (body or {}).get("body") or c.draft_reply
    core.decide(ctx.db, ctx.org_id, "reply", "human_approved_draft", "Draft approved by user", entity_type="conversation",
                entity_id=cid, model="human")
    return reply(cid, ReplyIn(body=text), ctx)


@router.post("/inbox/{cid}/takeover")
def takeover(cid: str, ctx: Ctx = Depends(require("conversations.takeover"))):
    """PRD 77: AI auto-send stops, user owns the conversation, agent stays available for suggestions."""
    c = tenant_get(ctx, m.Conversation, cid)
    c.ai_mode, c.assigned_to = "human", ctx.actor_id
    core.audit_ctx(ctx, "conversation.takeover", "conversation", cid)
    ctx.db.commit()
    return conv_dict(ctx.db, c, True)


@router.post("/inbox/{cid}/return-to-ai")
def return_to_ai(cid: str, ctx: Ctx = Depends(require("conversations.takeover"))):
    c = tenant_get(ctx, m.Conversation, cid)
    c.ai_mode, c.needs_review = "ai", False
    core.audit_ctx(ctx, "conversation.return_to_ai", "conversation", cid)
    ctx.db.commit()
    return conv_dict(ctx.db, c, True)


class InboundIn(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    channel: str = "email"


@router.post("/inbox/{cid}/simulate-reply")
def simulate_reply(cid: str, body: InboundIn, ctx: Ctx = Depends(require("conversations.reply"))):
    """Sandbox only: inject a prospect reply to exercise the reply agent end-to-end."""
    c = tenant_get(ctx, m.Conversation, cid)
    p = ctx.db.get(m.Person, c.person_id)
    if not engine._is_demo(p):
        raise APIError(400, "SANDBOX_ONLY", "Simulated replies are only allowed for demo/test conversations.")
    res = engine.handle_inbound(ctx.db, ctx.org, p, body.text, body.channel, simulated=True)
    return {**res, "conversation": conv_dict(ctx.db, c, True)}


@router.post("/inbound/{person_id}")
def inbound_for_person(person_id: str, body: InboundIn, ctx: Ctx = Depends(require("conversations.reply"))):
    """Record an inbound reply for a lead (e.g. forwarded from a mailbox / provider webhook)."""
    p = tenant_get(ctx, m.Person, person_id)
    return engine.handle_inbound(ctx.db, ctx.org, p, body.text, body.channel)


# ------------------------------------------------------------------ meetings (PRD 78-81)
@router.get("/meetings")
def meetings(ctx: Ctx = Depends(require("meetings.view"))):
    out = []
    for x in ctx.db.scalars(select(m.Meeting).where(m.Meeting.organization_id == ctx.org_id,
                                                    m.Meeting.workspace_id == ctx.ws_id).order_by(m.Meeting.start_at)):
        p = ctx.db.get(m.Person, x.person_id) if x.person_id else None
        out.append({"id": x.id, "title": x.title, "start_at": x.start_at.isoformat(), "end_at": x.end_at.isoformat(),
                    "timezone": x.timezone, "status": x.status, "location": x.location, "booked_by": x.booked_by,
                    "person": {"id": p.id, "name": p.full_name, "title": p.title} if p else None,
                    "conversation_id": x.conversation_id, "notes": x.notes})
    return out


@router.get("/meetings/slots")
def slots(day: str = "", ctx: Ctx = Depends(require("meetings.view"))):
    d = datetime.fromisoformat(day) if day else None
    return [s.isoformat() for s in engine.available_slots(ctx.db, ctx.workspace, d, "", 8)]


class BookIn(BaseModel):
    person_id: str
    start_at: str


@router.post("/meetings")
def book(body: BookIn, ctx: Ctx = Depends(require("meetings.book"))):
    p = tenant_get(ctx, m.Person, body.person_id)
    start = datetime.fromisoformat(body.start_at)
    conv = ctx.db.scalars(select(m.Conversation).where(m.Conversation.person_id == p.id)).first()
    mtg = engine.book_meeting(ctx.db, ctx.org, ctx.workspace, p, start, conv, booked_by=ctx.actor_id)
    core.audit_ctx(ctx, "meeting.booked", "meeting", mtg.id)
    ctx.db.commit()
    return {"id": mtg.id}


@router.patch("/meetings/{mid}")
def update_meeting(mid: str, body: dict, ctx: Ctx = Depends(require("meetings.book"))):
    x = tenant_get(ctx, m.Meeting, mid)
    st = body.get("status")
    if st in ("completed", "no_show", "cancelled", "booked"):
        x.status = st
        p = ctx.db.get(m.Person, x.person_id) if x.person_id else None
        if p and st == "completed":
            p.stage = "Meeting Completed"
            engine.queue_crm_sync(ctx.db, ctx.org_id, p.id, "Meeting completed")
        if st == "cancelled":
            core.emit(ctx.db, ctx.org_id, "meeting.cancelled", x.id, {})
        if st == "no_show" and p:   # PRD 179: trigger configured follow-up
            core.notify(ctx.db, ctx.org_id, "no_show", f"No-show: {p.full_name}", "Agent will propose new times.", f"#/leads/{p.id}")
    if "notes" in body:   # PRD 180 post-meeting notes
        x.notes = body["notes"]
    if body.get("start_at"):
        x.start_at = datetime.fromisoformat(body["start_at"])
        x.end_at = x.start_at + timedelta(minutes=30)
    core.audit_ctx(ctx, "meeting.updated", "meeting", mid, body)
    ctx.db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ LinkedIn task assistant (PRD 56-57)
@router.get("/linkedin-tasks")
def li_tasks(ctx: Ctx = Depends(require("leads.view"))):
    out = []
    for t in ctx.db.scalars(select(m.LinkedInTask).where(m.LinkedInTask.organization_id == ctx.org_id)
                            .order_by(m.LinkedInTask.created_at.desc()).limit(100)):
        p = ctx.db.get(m.Person, t.person_id)
        out.append({"id": t.id, "action": t.action, "reason": t.reason, "message": t.suggested_message, "status": t.status,
                    "person": {"id": p.id, "name": p.full_name, "title": p.title, "linkedin_url": p.linkedin_url} if p else None})
    return out


@router.patch("/linkedin-tasks/{tid}")
def li_task_update(tid: str, body: dict, ctx: Ctx = Depends(require("conversations.reply"))):
    t = tenant_get(ctx, m.LinkedInTask, tid)
    if body.get("status") in ("completed", "skipped", "open"):
        t.status = body["status"]
        core.emit(ctx.db, ctx.org_id, f"linkedin_task_{t.status}", t.person_id, {})
    ctx.db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ analytics (PRD 94-98, 187, 207-210)
@router.get("/analytics/dashboard")
def dashboard(ctx: Ctx = Depends(require("dashboard.view"))):
    db = ctx.db
    oid = ctx.org_id

    def count(model, *conds):
        return db.scalar(select(func.count(model.id)).where(model.organization_id == oid, *conds)) or 0
    sent = count(m.Message, m.Message.direction == "outbound", m.Message.status.in_(["sent", "delivered"]))
    replied_convs = db.scalar(select(func.count(func.distinct(m.Message.conversation_id))).where(
        m.Message.organization_id == oid, m.Message.direction == "inbound")) or 0
    positive = count(m.Conversation, m.Conversation.intent.in_(list(HOT | {"INTERESTED", "PRICING", "QUESTION"})))
    hot = count(m.Person, m.Person.temperature == "hot")
    meetings = count(m.Meeting, m.Meeting.status != "cancelled")
    spend = db.scalar(select(func.sum(m.UsageEvent.customer_cost)).where(m.UsageEvent.organization_id == oid)) or 0
    today = m.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    spend_today = db.scalar(select(func.sum(m.UsageEvent.customer_cost)).where(
        m.UsageEvent.organization_id == oid, m.UsageEvent.created_at >= today)) or 0
    companies = count(m.Company)
    people = count(m.Person)
    reachable = count(m.Person, m.Person.email != "")
    contacted = db.scalar(select(func.count(func.distinct(m.Conversation.person_id))).where(m.Conversation.organization_id == oid)) or 0
    qualified = count(m.Person, m.Person.stage.in_(["Hot", "Meeting Booked", "Meeting Completed", "Opportunity", "Won"]))
    opp = count(m.Person, m.Person.stage.in_(["Opportunity", "Won"]))
    won = count(m.Person, m.Person.stage == "Won")
    camps = [campaign_dict(db, c) for c in db.scalars(select(m.Campaign).where(
        m.Campaign.organization_id == oid, m.Campaign.status.notin_(["ARCHIVED"])).order_by(m.Campaign.created_at.desc()).limit(12))]
    review = count(m.Conversation, m.Conversation.needs_review.is_(True))
    running = sum(1 for c in camps if c["status"] == "RUNNING")
    recs = []
    for c in camps:
        s = c["stats"]
        if c["status"] == "RUNNING" and s["sent"] >= 8 and s["reply_rate"] < 2:
            recs.append(f"Pause or rework low-performing '{c['name']}' ({s['reply_rate']}% replies)")
        if s["hot"] >= 2:
            recs.append(f"Increase allocation for '{c['name']}' ({s['hot']} hot leads)")
    if review:
        recs.append(f"{review} conversation(s) need your attention")
    if not camps:
        recs.append("Create your first mission to start the GTM engine")
    daily = []
    for i in range(13, -1, -1):
        d0 = today - timedelta(days=i)
        d1 = d0 + timedelta(days=1)
        def ev(t):
            return db.scalar(select(func.count(m.DomainEvent.id)).where(m.DomainEvent.organization_id == oid,
                                                                        m.DomainEvent.type == t,
                                                                        m.DomainEvent.created_at >= d0, m.DomainEvent.created_at < d1)) or 0
        daily.append({"date": d0.strftime("%d %b"), "sent": ev("message.sent"), "replies": ev("message.replied"),
                      "hot": ev("lead.hot"), "meetings": ev("meeting.booked")})
    return {
        "kpis": {"meetings": meetings, "hot_leads": hot, "positive_replies": positive, "outreach": sent,
                 "reply_rate": round(100 * replied_convs / sent, 1) if sent else 0,
                 "meeting_rate": round(100 * meetings / sent, 2) if sent else 0, "spend_gtu": round(spend, 1),
                 "spend_today_gtu": round(spend_today, 1),
                 "cost_per_meeting": round(spend / meetings, 1) if meetings else None},
        "funnel": [{"stage": "Companies", "value": companies}, {"stage": "People", "value": people},
                   {"stage": "Reachable", "value": reachable}, {"stage": "Contacted", "value": contacted},
                   {"stage": "Delivered", "value": sent}, {"stage": "Replied", "value": replied_convs},
                   {"stage": "Positive", "value": positive}, {"stage": "Qualified", "value": qualified},
                   {"stage": "Meeting", "value": meetings}, {"stage": "Opportunity", "value": opp},
                   {"stage": "Customer", "value": won}],
        "campaigns": camps, "running_campaigns": running, "needs_attention": review, "recommendations": recs[:6],
        "brief": engine.daily_brief(db, oid, ctx.ws_id), "daily": daily,
        "agents_active": running > 0,
    }


@router.get("/analytics/decisions")
def decisions(agent: str = "", ctx: Ctx = Depends(require("agents.monitor"))):
    q = select(m.AIDecision).where(m.AIDecision.organization_id == ctx.org_id)
    if agent:
        q = q.where(m.AIDecision.agent == agent)
    return [_decision(d) for d in ctx.db.scalars(q.order_by(m.AIDecision.created_at.desc()).limit(200))]


def _decision(d: m.AIDecision) -> dict:
    return {"id": d.id, "agent": d.agent, "action": d.action, "reason": d.reason, "confidence": d.confidence,
            "policy_result": d.policy_result, "decision": d.decision, "entity": f"{d.entity_type}:{d.entity_id}",
            "evidence": d.evidence, "model": d.model, "at": d.created_at.isoformat()}


@router.post("/agents/optimize")
def run_optimizer(ctx: Ctx = Depends(require("agents.monitor"))):
    return {"actions": engine.optimize_org(ctx.db, ctx.org)}


# ------------------------------------------------------------------ command bar (PRD 99, 189, 330)
class CommandIn(BaseModel):
    text: str = Field(min_length=2, max_length=1000)


@router.post("/command")
def command(body: CommandIn, ctx: Ctx = Depends(require("command.run"))):
    """Natural-language command bar. Every tool call is permission- and policy-checked (PRD 216-218)."""
    t = body.text.strip()
    tl = t.lower()
    db = ctx.db

    def need(perm):
        if not ctx.has(perm):
            raise APIError(403, "FORBIDDEN", f"Your role cannot do that ({perm}).")

    tool = None
    routed, model = llm.complete_json(
        db, ctx.org, "command", "command_router",
        "Map the user's command to one tool. JSON {tool, args}. Tools: search_companies{query}, search_people{query}, "
        "pause_campaigns{max_cost_per_meeting|name_contains}, show_hot_leads{}, explain_performance{}, create_mission{instruction}, "
        "book_interested{}, reallocate_budget{segment}, research_top{n}, daily_brief{}, unknown{}.",
        llm.untrusted(t, 1000), 300, ctx.ws_id)
    if routed and routed.get("tool") and routed["tool"] != "unknown":
        tool, args = routed["tool"], routed.get("args") or {}
    else:
        args = {}
        if re.search(r"\bpause\b", tl):
            tool = "pause_campaigns"
            mm = re.search(r"above \$?(\d+)", tl)
            args = {"max_cost_per_meeting": float(mm.group(1))} if mm else {"name_contains": re.sub(r".*pause\s+(the\s+)?", "", tl).replace("campaign", "").strip()}
        elif re.search(r"(create|start|launch).*(mission|campaign|targeting)|generate clients|start targeting", tl):
            tool, args = "create_mission", {"instruction": t}
        elif re.search(r"\bwhy\b|underperform|declin|not replying|aren't replying", tl):
            tool = "explain_performance"
        elif re.search(r"hot leads|high (buying )?intent|show me prospects", tl):
            tool = "show_hot_leads"
        elif re.search(r"book meetings", tl):
            tool = "book_interested"
        elif re.search(r"(move|shift|reallocate).*budget", tl):
            tool, args = "reallocate_budget", {"segment": re.sub(r".*budget (in)?to\s*", "", tl)}
        elif re.search(r"research the top (\d+)", tl):
            tool, args = "research_top", {"n": int(re.search(r"top (\d+)", tl).group(1))}
        elif re.search(r"brief|what happened|summary", tl):
            tool = "daily_brief"
        elif re.search(r"\b(ctos?|ceos?|founders?|coos?|owners?|heads? of|vps?|directors?|people|decision makers)\b", tl):
            tool, args = "search_people", {"query": t}
        elif re.search(r"\b(find|search|show|companies|like our)\b", tl):
            tool, args = "search_companies", {"query": t}
        model = "rules"
    core.audit_ctx(ctx, "command.run", "command", "", {"text": t[:200], "tool": tool})
    core.decide(db, ctx.org_id, "command_bar", tool or "unknown", f"User command: {t[:150]}", model=model,
                decision="executed" if tool else "blocked")

    if tool == "search_companies":
        need("search.run")
        res = search_companies(ctx, query=args.get("query", t), limit=25)
        return {"tool": tool, "message": f"I found {res['total_matches']} companies matching: {res['summary']}. "
                                         f"Showing the top {res['returned']} by opportunity score.", "data": res}
    if tool == "search_people":
        need("search.run")
        res = search_people(ctx, query=args.get("query", t), limit=50)
        return {"tool": tool, "message": f"Found {res['returned']} people ({res['summary']}).", "data": res}
    if tool == "show_hot_leads":
        rows = list(db.scalars(select(m.Person).where(m.Person.organization_id == ctx.org_id,
                                                      (m.Person.temperature == "hot") | (m.Person.intent_score >= 70))
                               .order_by(m.Person.intent_score.desc()).limit(25)))
        return {"tool": tool, "message": f"{len(rows)} prospects with high buying intent.",
                "data": {"mode": "people", "results": [person_dict(db, p) for p in rows]}}
    if tool == "pause_campaigns":
        need("campaigns.manage")
        paused = []
        for c in db.scalars(select(m.Campaign).where(m.Campaign.organization_id == ctx.org_id, m.Campaign.status == "RUNNING")):
            s = engine.campaign_stats(db, c.id)
            if "max_cost_per_meeting" in args:
                cpm = s["cost_per_meeting"]
                hit = (cpm is not None and cpm > args["max_cost_per_meeting"]) or (cpm is None and s["spend"] > args["max_cost_per_meeting"])
            else:
                hit = bool(args.get("name_contains")) and args["name_contains"].lower() in c.name.lower()
            if hit:
                c.status = "PAUSED"
                paused.append(c.name)
                core.decide(db, ctx.org_id, "command_bar", "pause_campaign", f"Command: {t[:120]}", entity_type="campaign", entity_id=c.id)
        db.commit()
        return {"tool": tool, "message": f"Paused {len(paused)} campaign(s): {', '.join(paused) or 'none matched'}."}
    if tool == "create_mission":
        need("missions.manage")
        res = create_mission(MissionIn(instruction=args.get("instruction", t)), ctx)
        return {"tool": tool, "message": f"Created mission '{res['mission']['name']}' with {len(res['campaign_ids'])} campaigns. "
                                         "Open it to build the audience and review before launch.", "data": res}
    if tool == "explain_performance":
        camps = [(c, engine.campaign_stats(db, c.id)) for c in db.scalars(select(m.Campaign).where(
            m.Campaign.organization_id == ctx.org_id, m.Campaign.status.in_(["RUNNING", "PAUSED", "COMPLETED"])))]
        if not camps:
            return {"tool": tool, "message": "No campaigns have run yet, so there's no performance to explain."}
        camps.sort(key=lambda x: -x[1]["hot_rate"])
        best, worst = camps[0], camps[-1]
        msg = (f"'{best[0].name}' leads with a {best[1]['hot_rate']}% hot-lead rate and {best[1]['reply_rate']}% replies. ")
        if worst[0].id != best[0].id:
            msg += (f"'{worst[0].name}' trails at {worst[1]['reply_rate']}% replies. I recommend shifting ~20% of its "
                    f"budget to '{best[0].name}' and testing a softer CTA in the weaker segment.")
        return {"tool": tool, "message": msg}
    if tool == "book_interested":
        need("meetings.book")
        n = 0
        for conv in db.scalars(select(m.Conversation).where(m.Conversation.organization_id == ctx.org_id,
                                                            m.Conversation.intent.in_(["INTERESTED", "VERY_INTERESTED", "MEETING_REQUEST"]))):
            if not db.scalars(select(m.Meeting).where(m.Meeting.conversation_id == conv.id)).first():
                p = db.get(m.Person, conv.person_id)
                slot = engine.available_slots(db, ctx.workspace, None, "", 1)
                if slot and p:
                    text = engine._reply_text(ctx.org, p, "INTERESTED", engine.available_slots(db, ctx.workspace, None, "", 3), None)
                    engine.deliver(db, ctx.org, ctx.workspace, p, "email", "Finding a time", text, conv=conv,
                                   idempotency_key=f"bookall:{conv.id}:{m.utcnow().date()}")
                    n += 1
        db.commit()
        return {"tool": tool, "message": f"Sent meeting time proposals to {n} interested lead(s)."}
    if tool == "reallocate_budget":
        need("campaigns.manage")
        seg = args.get("segment", "").strip()
        target = [c for c in db.scalars(select(m.Campaign).where(m.Campaign.organization_id == ctx.org_id,
                                                                 m.Campaign.status == "RUNNING"))
                  if seg and seg.split()[0] in (c.name + " " + str(c.audience)).lower()]
        if not target:
            return {"tool": tool, "message": f"No running campaign targets '{seg}'. Create one with: 'Start targeting {seg} companies'."}
        for c in target:
            inc = round((c.budget_daily or 10) * 0.2, 2)
            c.budget_daily = round((c.budget_daily or 10) + inc, 2)
            core.decide(db, ctx.org_id, "command_bar", "increase_campaign_budget", f"User asked to move budget into {seg}",
                        entity_type="campaign", entity_id=c.id)
        db.commit()
        return {"tool": tool, "message": f"Increased daily allocation by 20% for {', '.join(c.name for c in target)} (within hard limits)."}
    if tool == "research_top":
        need("research.run")
        ids = [c.id for c in sorted(db.scalars(select(m.Company).where(m.Company.organization_id == ctx.org_id,
                                                                       m.Company.workspace_id == ctx.ws_id)),
                                    key=lambda c: -(c.scores or {}).get("opportunity", 0))[: int(args.get("n", 20))]]
        job = m.Job(organization_id=ctx.org_id, workspace_id=ctx.ws_id, type="research_company", payload={"company_ids": ids,
                    "depth": "standard"}, total=len(ids), created_by=ctx.actor_id)
        db.add(job)
        db.commit()
        return {"tool": tool, "message": f"Researching the top {len(ids)} companies in the background (job {job.id})."}
    if tool == "daily_brief":
        b = engine.daily_brief(db, ctx.org_id, ctx.ws_id)
        return {"tool": tool, "message": b["what_happened"] + " " + b["what_requires_you"], "data": b}
    return {"tool": None, "message": "I couldn't map that to an action yet. Try: 'Find SaaS companies in Bangalore with "
                                     "50-200 employees', 'Show me prospects with high buying intent', or 'Pause campaigns "
                                     "with cost per meeting above $50'."}


_ = company_dict
