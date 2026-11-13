"""Intelligence layer: scoring, provenance, search materialisation, research,
business understanding, ICP, mission + campaign generation, message writing."""
import re
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import models as m
from ..deps import APIError
from . import core, llm
from .nlp import describe_criteria, normalize_title, parse_search
from .providers import ProviderRouter, crawl_website

DEFAULT_WEIGHTS = {"icp": 0.35, "intent": 0.30, "reachability": 0.15, "confidence": 0.10, "freshness": 0.10}


# ------------------------------------------------------------------ ICP & scoring (PRD 17, 105, 111)
def workspace_icp(db: Session, ws: m.Workspace | None) -> dict:
    if ws and (ws.settings or {}).get("icp"):
        return ws.settings["icp"]
    if ws:
        mission = db.scalars(select(m.Mission).where(m.Mission.workspace_id == ws.id, m.Mission.status == "active")
                             .order_by(m.Mission.created_at.desc())).first()
        if mission and mission.icp:
            return mission.icp
    return {}


def _freshness(days: int) -> tuple[int, str]:
    if days <= 30:
        return 95, "Fresh"
    if days <= 90:
        return 75, "Recent"
    if days <= 180:
        return 50, "Ageing"
    return 25, "Stale"


def score_company(c: dict, icp: dict, weights: dict | None = None, people_reach: dict | None = None) -> tuple[dict, dict]:
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    reasons, fit = [], 0
    inds = icp.get("industries") or []
    if not inds:
        fit += 12
    elif c.get("industry") in inds:
        fit += 22
        reasons.append(("+22", "Industry match", c.get("industry")))
    emp = c.get("employee_count") or 0
    lo, hi = icp.get("employee_min"), icp.get("employee_max")
    if lo is None and hi is None:
        fit += 10
    elif (lo is None or emp >= lo) and (hi is None or emp <= hi):
        fit += 18
        reasons.append(("+18", "Employee range", f"{emp} employees"))
    elif emp and ((lo and emp >= lo * 0.7) or (hi and emp <= hi * 1.3)):
        fit += 8
        reasons.append(("+8", "Near employee range", f"{emp} employees"))
    geo = (icp.get("cities") and c.get("city") in icp["cities"]) or \
          (icp.get("states") and c.get("state") in icp["states"]) or \
          (icp.get("countries") and c.get("country") in icp["countries"])
    if geo:
        fit += 16
        reasons.append(("+16", "Geography", c.get("city") or c.get("country")))
    elif not (icp.get("cities") or icp.get("states") or icp.get("countries")):
        fit += 8
    sigs = c.get("signals") or []
    sig_types = {s["type"] for s in sigs}
    wanted = set(icp.get("signals") or [])
    if sig_types & (wanted | {"hiring"}) or ("hiring_sales" in wanted and any("sales" in s["text"].lower() for s in sigs)):
        fit += 15
        reasons.append(("+15", "Hiring / growth signal", next((s["text"] for s in sigs if s["type"] in ("hiring", "employee_growth")), "")))
    if icp.get("technologies") and set(icp["technologies"]) & set(c.get("technologies") or []):
        fit += 10
        reasons.append(("+10", "Technology fit", ", ".join(set(icp["technologies"]) & set(c["technologies"]))))
    elif any(t in (c.get("technologies") or []) for t in ("Excel", "WhatsApp Business", "Tally ERP")):
        fit += 7
        reasons.append(("+7", "Automation opportunity in tech stack", ", ".join(c["technologies"][:3])))
    if c.get("revenue_estimate"):
        fit += 6
        reasons.append(("+6", "Revenue estimate available", f"${c['revenue_estimate']:,.0f}"))
    if icp.get("business_model") and c.get("business_model") in icp["business_model"]:
        fit += 5
        reasons.append(("+5", "Business model", c.get("business_model")))
    fit = min(100, fit)

    intent, intent_reasons = 20, []
    weights_sig = {"hiring": 18, "funding": 20, "expansion": 15, "employee_growth": 14, "process": 16,
                   "technology": 10, "launch": 8, "website_change": 6}
    min_days = 365
    for s in sigs:
        days = s.get("observed_days_ago", 90)
        min_days = min(min_days, days)
        decay = 1.0 if days <= 30 else 0.7 if days <= 90 else 0.4
        pts = round(weights_sig.get(s["type"], 5) * decay)
        intent += pts
        intent_reasons.append((f"+{pts}", s["type"].replace("_", " ").title(), s["text"]))
    intent = min(100, intent)
    reach = people_reach or {}
    reachability = min(100, 30 + 25 * min(2, reach.get("emails", 1 if c.get("domain") else 0))
                       + 10 * min(2, reach.get("phones", 0)) + (10 if c.get("linkedin_url") else 0))
    confidence = int(round((c.get("data_confidence") or 0.75) * 100))
    freshness, fresh_label = _freshness(min_days if sigs else 120)
    opp = round(fit * w["icp"] + intent * w["intent"] + reachability * w["reachability"]
                + confidence * w["confidence"] + freshness * w["freshness"])
    scores = {"icp": fit, "intent": intent, "reachability": reachability, "confidence": confidence,
              "freshness": freshness, "freshness_label": fresh_label, "opportunity": opp,
              "priority": "A" if opp >= 70 else "B" if opp >= 55 else "C"}
    explanation = {"icp": [{"points": p, "label": l, "evidence": e} for p, l, e in reasons],
                   "intent": [{"points": p, "label": l, "evidence": e} for p, l, e in intent_reasons],
                   "weights": w}
    return scores, explanation


def why_company(c: m.Company) -> str:
    bits = [f"{c.name} is a {c.employee_count}-person {c.sub_industry or c.industry} company in {c.city or c.country}."]
    for r in (c.score_explanation or {}).get("icp", [])[:3]:
        bits.append(f"{r['label']}: {r['evidence']}.")
    sig = [s["text"] for s in (c.signals or [])][:2]
    if sig:
        bits.append("Signals: " + "; ".join(sig) + ".")
    return " ".join(bits)


# ------------------------------------------------------------------ provenance + materialisation (PRD 25-26, 62, 153)
def observe(db: Session, org_id: str, entity_type: str, entity_id: str, fields: dict, provider: str,
            confidence: float = 0.75, source_url: str = "", license_: str = "", ttl_days: int = 180):
    now = m.utcnow()
    for k, v in fields.items():
        if v in (None, "", [], {}):
            continue
        db.add(m.FieldObservation(organization_id=org_id, entity_type=entity_type, entity_id=entity_id,
                                  field_name=k, field_value=str(v)[:500], provider=provider, confidence=confidence,
                                  source_url=source_url, license=license_, observed_at=now,
                                  expires_at=now + timedelta(days=ttl_days)))


COMPANY_FIELDS = ["name", "domain", "website", "description", "industry", "sub_industry", "country", "state", "city",
                  "employee_count", "revenue_estimate", "founded_year", "business_model", "linkedin_url",
                  "technologies", "signals"]


def upsert_company(db: Session, org_id: str, ws_id: str, data: dict, provider: str) -> tuple[m.Company, bool]:
    dom = (data.get("domain") or "").lower().removeprefix("www.")
    q = select(m.Company).where(m.Company.workspace_id == ws_id)
    conds = []
    if dom:
        conds.append(m.Company.domain == dom)
    if data.get("provider_ref"):
        conds.append(m.Company.provider_ref == data["provider_ref"])
    existing = db.scalars(q.where(or_(*conds))).first() if conds else \
        db.scalars(q.where(m.Company.name == data.get("name", ""))).first()
    created = existing is None
    c = existing or m.Company(organization_id=org_id, workspace_id=ws_id, name=data.get("name", dom or "Unknown"))
    for f in COMPANY_FIELDS:
        v = data.get(f)
        if v not in (None, "", [], 0) and (created or not getattr(c, f)):
            setattr(c, f, v)
    if dom:
        c.domain = dom
    c.provider_ref = c.provider_ref or data.get("provider_ref", "")
    c.source = c.source or provider
    if created:
        db.add(c)
        db.flush()
        observe(db, org_id, "company", c.id, {f: data.get(f) for f in ["employee_count", "industry", "city",
                                                                        "revenue_estimate", "technologies"]},
                provider, 0.8 if provider.startswith("demo") else 0.7)
        for s in data.get("signals") or []:
            observe(db, org_id, "company", c.id, {"signal": s["text"]}, s.get("source", provider), 0.75)
    return c, created


def find_duplicate_person(db: Session, ws_id: str, data: dict) -> m.Person | None:
    q = select(m.Person).where(m.Person.workspace_id == ws_id)
    email = (data.get("email") or "").strip().lower()
    if email:
        p = db.scalars(q.where(m.Person.email == email)).first()
        if p:
            return p
    if data.get("linkedin_url"):
        p = db.scalars(q.where(m.Person.linkedin_url == data["linkedin_url"])).first()
        if p:
            return p
    if data.get("provider_ref"):
        p = db.scalars(q.where(m.Person.provider_ref == data["provider_ref"])).first()
        if p:
            return p
    if data.get("phone"):
        p = db.scalars(q.where(m.Person.phone == data["phone"])).first()
        if p:
            return p
    if data.get("full_name") and data.get("company_id"):
        return db.scalars(q.where(m.Person.full_name == data["full_name"],
                                  m.Person.company_id == data["company_id"])).first()
    return None


def upsert_person(db: Session, org_id: str, ws_id: str, data: dict, provider: str) -> tuple[m.Person, bool]:
    data = dict(data)
    data["email"] = (data.get("email") or "").strip().lower()
    if not data.get("full_name"):
        data["full_name"] = f"{data.get('first_name', '')} {data.get('last_name', '')}".strip()
    existing = find_duplicate_person(db, ws_id, data)
    created = existing is None
    p = existing or m.Person(organization_id=org_id, workspace_id=ws_id)
    for f in ["company_id", "first_name", "last_name", "full_name", "title", "seniority", "department", "email",
              "phone", "linkedin_url", "country", "city", "timezone", "provider_ref"]:
        v = data.get(f)
        if v and (created or not getattr(p, f)):
            setattr(p, f, v)
    p.normalized_title = data.get("normalized_title") or normalize_title(p.title or "")
    p.source = p.source or provider
    if data.get("custom_fields"):
        p.custom_fields = {**(p.custom_fields or {}), **data["custom_fields"]}
    if created:
        db.add(p)
        db.flush()
        observe(db, org_id, "person", p.id, {"title": p.title, "email": p.email, "phone": p.phone}, provider,
                0.85 if provider.startswith("demo") else 0.7)
    return p, created


def rescore_people_for_company(db: Session, c: m.Company):
    for p in db.scalars(select(m.Person).where(m.Person.company_id == c.id)):
        p.icp_score = int((c.scores or {}).get("icp", 0))
        p.intent_score = max(p.intent_score or 0, int((c.scores or {}).get("intent", 0)))


def _llm_parse(db, org, query: str, base: dict | None, ws_id) -> tuple[dict, str]:
    rules = parse_search(query, base)
    data, model = llm.complete_json(
        db, org, "parse", "search_parser",
        "Convert the user's B2B targeting request into JSON with keys: industries (choose from: "
        "Software & SaaS, IT Services, Manufacturing, Healthcare, Education, Real Estate, Professional Services, "
        "Logistics, Retail & E-commerce, Financial Services, Construction, Marketing Agency, Hospitality), countries, "
        "states, cities, exclude_cities, exclude_industries, employee_min, employee_max, signals (from: hiring, "
        "hiring_sales, funding, expansion, employee_growth, process, technology, launch), technologies, "
        "business_model, job_titles, ai_criteria. If a previous criteria object is given, return the refined full object.",
        f"Previous criteria: {base or {}}\nRequest: {llm.untrusted(query, 2000)}", 800, ws_id)
    if data and isinstance(data, dict):
        merged = {**rules, **{k: v for k, v in data.items() if k in rules and v not in (None, "")}}
        return merged, model
    return rules, "rules"


def search_companies(ctx, query: str = "", criteria: dict | None = None, limit: int = 50, base: dict | None = None,
                     save: bool = True, prefer: dict | None = None) -> dict:
    """`criteria` are hard filters; `prefer` (e.g. buying signals to prioritise) only affects ranking."""
    db, org, ws = ctx.db, ctx.org, ctx.workspace
    model = "structured"
    if query and not criteria:
        criteria, model = _llm_parse(db, org, query, base, ctx.ws_id)
    criteria = criteria or {}
    router = ProviderRouter(db, org.id)
    pmeta: dict = {}
    raw, prov = router.search_companies(criteria, limit, meta=pmeta)
    if not raw and not prov and not getattr(router, "attempted", 0):
        raise APIError(400, "NO_DATA_PROVIDER", "No lead data source is connected. Add your LeadOcean API key in "
                       "Integrations > Credential vault (LEADOCEAN_API_KEY), or import leads from a CSV.")
    if not raw and getattr(router, "last_errors", None) and not prov:
        raise APIError(502, "DATA_PROVIDER_ERROR", "; ".join(router.last_errors)[:300])
    total = pmeta.get("total") or len(raw)
    icp = {**workspace_icp(db, ws), **{k: v for k, v in criteria.items() if v}, **(prefer or {})}
    weights = (ws.settings or {}).get("score_weights") if ws else None
    if criteria.get("exclude_contacted"):
        contacted = {c.provider_ref for c in db.scalars(
            select(m.Company).join(m.Conversation, m.Conversation.company_id == m.Company.id)
            .where(m.Company.workspace_id == ctx.ws_id))}
        raw = [r for r in raw if r.get("provider_ref") not in contacted]
    # suppressed domains never appear (PRD 64)
    sup_domains = {s.value for s in db.scalars(select(m.SuppressionEntry).where(
        m.SuppressionEntry.organization_id == org.id, m.SuppressionEntry.kind == "domain"))}
    raw = [r for r in raw if r.get("domain") not in sup_domains]
    for r in raw:
        r["scores"], r["score_explanation"] = score_company(r, icp, weights)
    raw.sort(key=lambda r: r["scores"]["opportunity"], reverse=True)
    top = raw[:limit]
    results = []
    if top:
        core.meter(db, org.id, "company_search", max(1, len(top) / 25), ws_id=ctx.ws_id, ref="search")
    for r in top:
        if save:
            c, _ = upsert_company(db, org.id, ctx.ws_id, r, prov.id if prov else "unknown")
            c.scores, c.score_explanation = r["scores"], r["score_explanation"]
            results.append(company_dict(c))
        else:
            results.append(r)
    core.emit(db, org.id, "search_created", "", {"query": query, "results": len(results)}, ws_id=ctx.ws_id)
    db.commit()
    return {"criteria": criteria, "summary": describe_criteria(criteria), "total_matches": max(total, len(raw)),
            "provider_total": total, "provider_notes": pmeta.get("unsupported") or [], "provider_notice": pmeta.get("notice"), "returned": len(results), "results": results, "parser": model,
            "provider": prov.name if prov else None}


def search_people(ctx, query: str = "", criteria: dict | None = None, company_ids: list[str] | None = None,
                  limit: int = 100, base: dict | None = None) -> dict:
    db, org = ctx.db, ctx.org
    model = "structured"
    if query and not criteria:
        criteria, model = _llm_parse(db, org, query, base, ctx.ws_id)
    criteria = criteria or {}
    if not criteria.get("job_titles"):
        criteria["job_titles"] = ["Founder", "CEO", "COO", "Head of Sales"]
    router = ProviderRouter(db, org.id)
    refs, domains = None, None
    if company_ids:
        comps = [c for c in db.scalars(select(m.Company).where(m.Company.id.in_(company_ids),
                                                               m.Company.organization_id == org.id))]
        refs = [c.provider_ref for c in comps if c.provider_ref]
        domains = [c.domain for c in comps if c.domain]
    raw, prov = router.search_people(criteria, refs, limit, domains=domains, meta={})
    if not raw and not prov and not getattr(router, "attempted", 0):
        raise APIError(400, "NO_DATA_PROVIDER", "No lead data source is connected. Add your LeadOcean API key in "
                       "Integrations > Credential vault (LEADOCEAN_API_KEY), or import leads from a CSV.")
    if not raw and getattr(router, "last_errors", None) and not prov:
        raise APIError(502, "DATA_PROVIDER_ERROR", "; ".join(router.last_errors)[:300])
    if raw:
        core.meter(db, org.id, "people_search", max(1, len(raw) / 25), ws_id=ctx.ws_id, ref="people_search")
    from .demo_data import companies_by_ref
    cref = companies_by_ref()
    icp = {**workspace_icp(db, ctx.workspace), **{k: v for k, v in criteria.items() if v}}
    out = []
    for r in raw:
        comp_data = r.get("company") or cref.get(r.get("company_ref"))
        comp = None
        if comp_data:
            comp, created = upsert_company(db, org.id, ctx.ws_id, comp_data, prov.id if prov else "unknown")
            if created or not comp.scores:
                comp.scores, comp.score_explanation = score_company(comp_data, icp)
        person, _ = upsert_person(db, org.id, ctx.ws_id, {**r, "company_id": comp.id if comp else None},
                                  prov.id if prov else "unknown")
        person.custom_fields = {**(person.custom_fields or {}), "has_email": r.get("has_email"),
                                "has_phone": r.get("has_phone")}
        if comp:
            person.icp_score = int(comp.scores.get("icp", 0))
            person.intent_score = int(comp.scores.get("intent", 0))
        out.append(person_dict(db, person, comp))
    out.sort(key=lambda p: (p["icp_score"] + p["intent_score"]), reverse=True)
    db.commit()
    return {"criteria": criteria, "summary": describe_criteria(criteria), "returned": len(out), "results": out,
            "parser": model, "provider": prov.name if prov else None}


# ------------------------------------------------------------------ enrichment (PRD 69, 151, 156)
SENDABLE_DEFAULT = ["VALID", "CATCH_ALL"]


def enrich_person(db: Session, org: m.Organization, ws_id: str | None, p: m.Person, phone: bool = False) -> dict:
    router = ProviderRouter(db, org.id)
    comp = db.get(m.Company, p.company_id) if p.company_id else None
    payload = {"provider_ref": p.provider_ref, "first_name": p.first_name, "last_name": p.last_name,
               "domain": comp.domain if comp else ""}
    out = {}
    if not p.email:
        core.meter(db, org.id, "email_enrichment", 1, ws_id=ws_id, ref=p.id)
        res, prov = router.find_email(payload)
        if res.get("email"):
            p.email = res["email"].lower()
            observe(db, org.id, "person", p.id, {"email": p.email}, prov.id if prov else "unknown", res.get("confidence", 0.6))
            if res.get("status"):
                p.email_status = res["status"]   # provider already verified it (e.g. LeadOcean)
            else:
                ver, _ = router.verify(p.email, source=res.get("source"), confidence=res.get("confidence", 0.6))
                core.meter(db, org.id, "email_verification", 1, ws_id=ws_id, ref=p.id)
                p.email_status = ver["status"]
            p.last_verified_at = m.utcnow()
        out["email"] = p.email or None
    elif p.email_status in ("UNKNOWN", ""):
        ver, _ = router.verify(p.email, confidence=0.8)
        core.meter(db, org.id, "email_verification", 1, ws_id=ws_id, ref=p.id)
        p.email_status, p.last_verified_at = ver["status"], m.utcnow()
    if phone and not p.phone:
        core.require_entitlement(db, org, "phone_enrichment")
        core.meter(db, org.id, "phone_enrichment", 1, ws_id=ws_id, ref=p.id)
        res, prov = router.find_phone(payload)
        if res.get("phone"):
            p.phone, p.phone_status = res["phone"], "FOUND"
            observe(db, org.id, "person", p.id, {"phone": p.phone}, prov.id if prov else "unknown", res.get("confidence", 0.6))
        out["phone"] = p.phone or None
    out["email_status"] = p.email_status
    return out


# ------------------------------------------------------------------ research (PRD 30, 107-108)
def research_company(db: Session, org: m.Organization, ws_id: str | None, c: m.Company, depth: str = "standard") -> dict:
    core.meter(db, org.id, f"research_{depth}", 1, ws_id=ws_id, ref=c.id)
    router = ProviderRouter(db, org.id)
    crawl, _ = router.research({"website": c.website, "domain": c.domain}, depth)
    pages = crawl.get("pages", [])
    page_text = "\n\n".join(f"[{p['url']}] {p['title']}\n{p['text'][:3000]}" for p in pages)
    known = {k: getattr(c, k) for k in ["name", "industry", "sub_industry", "city", "country", "employee_count",
                                         "business_model", "technologies", "description"]}
    known["signals"] = [s["text"] for s in c.signals or []]
    offer = db.scalars(select(m.Offer).where(m.Offer.organization_id == org.id)).first()
    data, model = llm.complete_json(
        db, org, "research", "company_research",
        "Produce a company research brief as JSON with keys: company_overview, business_model, products (list), "
        "target_market, growth_indicators (list), technology (list), hiring_signals (list), buying_signals (list), "
        "likely_pain_points (list), relevant_decision_makers (list of roles), suggested_approach, evidence (list of "
        "'fact - source'), confidence (0-1). Use ONLY the known facts and website text; say 'unknown' otherwise.",
        f"Seller offer: {offer.name + ': ' + offer.description if offer else 'n/a'}\nKnown facts: {known}\n"
        f"Website text:\n{llm.untrusted(page_text, 14000)}", 2500, ws_id)
    if not data:
        sig = c.signals or []
        pains = []
        if any(s["type"] in ("hiring", "employee_growth") for s in sig):
            pains.append("Scaling sales/support headcount creates lead-routing and follow-up gaps")
        if any(s["type"] == "process" for s in sig) or any(t in (c.technologies or []) for t in ("Excel", "WhatsApp Business")):
            pains.append("Manual lead handling across WhatsApp and spreadsheets")
        if any(s["type"] == "expansion" for s in sig):
            pains.append("Multi-location operations need standardised processes")
        pains = pains or ["Repetitive operational processes that could be automated"]
        data = {
            "company_overview": c.description or f"{c.name} operates in {c.industry}.",
            "business_model": c.business_model or "unknown",
            "products": [c.sub_industry] if c.sub_industry else [],
            "target_market": "Businesses" if c.business_model == "B2B" else "Consumers" if c.business_model == "B2C" else "unknown",
            "growth_indicators": [s["text"] for s in sig if s["type"] in ("employee_growth", "funding", "expansion")],
            "technology": c.technologies or [], "hiring_signals": [s["text"] for s in sig if s["type"] == "hiring"],
            "buying_signals": [s["text"] for s in sig], "likely_pain_points": pains,
            "relevant_decision_makers": ["Founder/CEO", "COO / Operations Leadership", "Sales Leadership"],
            "suggested_approach": f"Lead with {pains[0].lower()} and reference their most recent signal.",
            "evidence": [f"{s['text']} - {s['source']}" for s in sig] + [f"{p['title']} - {p['url']}" for p in pages[:3]],
            "confidence": 0.6 if sig else 0.4,
        }
    data["depth"], data["model"], data["pages_crawled"] = depth, model, [p["url"] for p in pages]
    data["researched_at"] = m.utcnow().isoformat()
    c.research = data
    core.decide(db, org.id, "research", "research_company", f"{depth} research completed",
                entity_type="company", entity_id=c.id, confidence=float(data.get("confidence", 0.6) or 0.6), model=model)
    return data


def understand_business(db: Session, org: m.Organization, website: str, description: str = "") -> dict:
    """Onboarding: website -> company profile (PRD 13, 211-212)."""
    crawl = crawl_website(website, 5) if website else {"pages": []}
    pages = crawl.get("pages", [])
    text = "\n\n".join(f"[{p['url']}] {p['title']}\n{p['text'][:3500]}" for p in pages)
    data, model = llm.complete_json(
        db, org, "research", "business_understanding",
        "Analyse this company's website and produce JSON: company_description, primary_offers (list of "
        "{name, description, problems, benefits}), industries_served (list), target_customer, geography (list), "
        "pricing_model, buying_motion, likely_buyer (list of titles), sales_cycle, differentiators (list), "
        "competitors (list of {name, why_relevant}), proof_points (list), objections (list), "
        "icp {industries, countries, employee_min, employee_max, signals, job_titles}.",
        f"Company: {org.name}\nUser description: {llm.untrusted(description, 2000)}\nWebsite:\n{llm.untrusted(text, 16000)}",
        3000)
    if not data:
        blob = (description + " " + text).lower()
        parsed = parse_search(blob)
        offers = []
        for kw, name in [("crm", "CRM automation"), ("erp", "ERP automation"), ("ai", "AI process automation"),
                         ("whatsapp", "WhatsApp automation"), ("app", "Software & app development"),
                         ("consult", "Consulting"), ("market", "Marketing services")]:
            if re.search(rf"\b{kw}", blob):
                offers.append({"name": name, "description": f"{name} for growing businesses",
                               "problems": ["manual, repetitive work", "slow lead follow-up"],
                               "benefits": ["save team hours", "faster response to customers"]})
        data = {
            "company_description": description or (pages[0]["title"] if pages else org.name),
            "primary_offers": offers[:4] or [{"name": "Core offer", "description": description or "Our services",
                                              "problems": [], "benefits": []}],
            "industries_served": parsed["industries"] or ["Professional Services", "Software & SaaS", "Manufacturing"],
            "target_customer": "SMBs and mid-market companies", "geography": parsed["countries"] or [org.country or "India"],
            "pricing_model": "unknown", "buying_motion": "Sales-led", "likely_buyer": ["Founder", "CEO", "COO", "Head of Sales"],
            "sales_cycle": "2-8 weeks", "differentiators": [], "competitors": [], "proof_points": [], "objections":
                ["We already have a vendor", "Budget not allocated", "Not a priority right now"],
            "icp": {"industries": parsed["industries"] or ["Professional Services", "Software & SaaS", "Manufacturing"],
                    "countries": parsed["countries"] or [org.country or "India"], "employee_min": 20, "employee_max": 500,
                    "signals": ["hiring"], "job_titles": ["Founder", "CEO", "COO", "Head of Sales"]},
        }
    data["model"], data["pages_analyzed"] = model, [p["url"] for p in pages]
    return data


# ------------------------------------------------------------------ missions & campaigns (PRD 9, 37, 44, 268)
CHANNEL_WORDS = {"email": "email", "whatsapp": "whatsapp", "linkedin": "linkedin", "sms": "sms", "voice": "voice",
                 "call": "voice"}
PERSONA_GROUPS = [("Founders", ["Founder", "CEO", "Managing Director"]),
                  ("Operations leaders", ["COO", "Head of Operations"]),
                  ("Sales leaders", ["Head of Sales", "VP Sales", "Sales Director"]),
                  ("Technology leaders", ["CTO", "Head of IT"]),
                  ("Marketing leaders", ["CMO", "Head of Marketing"])]


def parse_budget(text: str) -> float | None:
    mt = re.search(r"(\d[\d,\.]*)\s*(k|lakh|lac)?\s*(?:/\s*mo|/\s*month|monthly|per month|a month|budget)", text.lower())
    mt = mt or re.search(r"budget (?:of )?(?:₹|rs\.?|inr|\$)?\s*(\d[\d,\.]*)\s*(k|lakh|lac)?", text.lower())
    if not mt:
        return None
    v = float(mt.group(1).replace(",", ""))
    unit = mt.group(2)
    if unit == "k":
        v *= 1000
    elif unit in ("lakh", "lac"):
        v *= 100000
    return v


def build_mission_from_instruction(ctx, instruction: str) -> dict:
    crit = parse_search(instruction)
    titles = crit.get("job_titles") or []
    personas = titles or ["Founder", "CEO", "COO", "Head of Sales"]
    channels = sorted({v for k, v in CHANNEL_WORDS.items() if re.search(rf"\b{k}", instruction.lower())}) or ["email"]
    budget = parse_budget(instruction) or 5000
    offer = None
    mo = re.search(r"(?:for|selling|sell|need|needing) (?:our )?([a-z \-/]+?)(?: service| services| implementation|,|\.|$)",
                   instruction.lower())
    if mo:
        offer = mo.group(1).strip()
    pretty = " ".join(w.upper() if len(w) <= 3 else w.title() for w in (offer or "GTM").split())
    name_bits = [crit["industries"][0] if crit.get("industries") else pretty]
    if crit.get("cities") or crit.get("countries"):
        name_bits.append((crit.get("cities") or crit.get("countries"))[0])
    icp = {k: crit[k] for k in ["industries", "countries", "states", "cities", "exclude_cities", "employee_min",
                                "employee_max", "signals", "technologies", "business_model"] if crit.get(k)}
    icp["job_titles"] = personas
    return {"name": " - ".join(name_bits), "objective": "Generate qualified meetings", "instruction": instruction,
            "icp": icp, "personas": personas, "signals": crit.get("signals", []), "channels": channels,
            "budget_monthly": budget, "offer_hint": offer, "summary": describe_criteria(crit)}


DEFAULT_SEQUENCE = {
    "email": [{"day": 0, "channel": "email", "type": "initial"}, {"day": 3, "channel": "email", "type": "follow_up"},
              {"day": 7, "channel": "email", "type": "follow_up"}, {"day": 14, "channel": "email", "type": "breakup"}],
}


def omnichannel_sequence(channels: list[str]) -> list[dict]:
    """PRD 263: Email D0 -> LinkedIn D2 -> WhatsApp D4 -> Email D7 -> Voice D10 -> Final email D14."""
    seq = [{"day": 0, "channel": "email", "type": "initial"}]
    if "linkedin" in channels:
        seq.append({"day": 2, "channel": "linkedin", "type": "connect"})
    if "whatsapp" in channels:
        seq.append({"day": 4, "channel": "whatsapp", "type": "follow_up"})
    seq.append({"day": 7, "channel": "email", "type": "follow_up"})
    if "sms" in channels:
        seq.append({"day": 9, "channel": "sms", "type": "follow_up"})
    if "voice" in channels:
        seq.append({"day": 10, "channel": "voice", "type": "qualification_call"})
    seq.append({"day": 14, "channel": "email", "type": "breakup"})
    if "email" not in channels:
        seq = [s for s in seq if s["channel"] != "email"] or [{"day": 0, "channel": channels[0], "type": "initial"}]
    return seq


def generate_campaigns_for_mission(db: Session, ctx, mission: m.Mission) -> list[m.Campaign]:
    personas = mission.personas or ["Founder", "CEO"]
    groups = [(label, [t for t in titles if t.lower() in " ".join(personas).lower() or
                       normalize_title(t) in {normalize_title(p) for p in personas}])
              for label, titles in PERSONA_GROUPS]
    groups = [(l, t) for l, t in groups if t][:3] or [("Decision makers", personas)]
    seg = (mission.icp.get("industries") or ["SMBs"])
    out = []
    share = round((mission.budget_monthly or 0) / max(1, len(groups)), 2)
    for label, titles in groups:
        camp = m.Campaign(organization_id=ctx.org_id, workspace_id=ctx.ws_id, mission_id=mission.id,
                          name=f"{label} - {', '.join(seg[:2])}", objective=mission.objective or "Book qualified meetings",
                          offer_id=mission.offer_id, audience={**mission.icp, "job_titles": titles},
                          target_roles=titles, channels=mission.channels or ["email"],
                          sequence=omnichannel_sequence(mission.channels or ["email"]),
                          messaging_strategy={"tone": "consultative", "angle": "pain-based", "personalization": "auto",
                                              "cta": "short discovery call"},
                          budget_total=share, budget_daily=round(share / 30, 2), autonomy_mode=mission.autonomy_mode,
                          status="DRAFT", created_by=ctx.actor_id)
        db.add(camp)
        out.append(camp)
    db.flush()
    core.decide(db, ctx.org_id, "gtm_orchestrator", "create_campaigns",
                f"Split mission into {len(out)} persona campaigns: {', '.join(c.name for c in out)}",
                entity_type="mission", entity_id=mission.id, confidence=0.8)
    return out


def build_audience(ctx, camp: m.Campaign, limit: int = 40) -> dict:
    """Find companies + people for a campaign and attach them (with preview stats, PRD 45)."""
    crit = dict(camp.audience or {})
    # Mission signals mean "prioritise", not "require": rank by them instead of filtering (PRD 2.1 example).
    prefer = {"signals": crit.pop("signals", [])}
    crit.pop("job_titles", None)
    comp_res = search_companies(ctx, criteria=crit, limit=max(10, limit // 2), prefer=prefer)
    ids = [c["id"] for c in comp_res["results"]]
    ppl = search_people(ctx, criteria={"job_titles": camp.target_roles or crit.get("job_titles") or []},
                        company_ids=ids, limit=limit)
    added = skipped = 0
    db = ctx.db
    # Deduplicate across the portfolio: never queue someone who is already in another live
    # campaign or was contacted recently (PRD 100 "exclude already contacted", 152).
    recent = m.utcnow() - timedelta(days=14)
    busy = set(db.scalars(select(m.CampaignLead.person_id).where(
        m.CampaignLead.organization_id == ctx.org_id, m.CampaignLead.campaign_id != camp.id,
        m.CampaignLead.status.in_(["pending", "active", "replied"]))))
    busy |= set(db.scalars(select(m.Conversation.person_id).join(m.Message, m.Message.conversation_id == m.Conversation.id)
                           .where(m.Conversation.organization_id == ctx.org_id, m.Message.direction == "outbound",
                                  m.Message.campaign_id != camp.id, m.Message.created_at >= recent)))
    for p in ppl["results"]:
        if p["id"] in busy:
            skipped += 1
            continue
        if not db.scalars(select(m.CampaignLead).where(m.CampaignLead.campaign_id == camp.id,
                                                       m.CampaignLead.person_id == p["id"])).first():
            db.add(m.CampaignLead(organization_id=ctx.org_id, campaign_id=camp.id, person_id=p["id"],
                                  variant="A" if added % 2 == 0 else "B"))
            added += 1
    camp.status = "READY" if camp.status in ("DRAFT", "RESEARCHING") else camp.status
    db.commit()
    return {"companies": len(ids), "people": len(ppl["results"]), "added": added, "skipped_already_in_play": skipped,
            "top_segments": _top_segments(comp_res["results"]), "criteria": comp_res["summary"]}


def _top_segments(companies: list[dict]) -> list[dict]:
    seg = {}
    for c in companies:
        seg[c["industry"]] = seg.get(c["industry"], 0) + 1
    return [{"segment": k, "companies": v} for k, v in sorted(seg.items(), key=lambda x: -x[1])[:5]]


# ------------------------------------------------------------------ messaging (PRD 46-48, 258-261)
def route_offer(db: Session, org_id: str, camp: m.Campaign, company: m.Company | None) -> m.Offer | None:
    offers = list(db.scalars(select(m.Offer).where(m.Offer.organization_id == org_id)))
    if company:
        for o in offers:
            if company.industry in (o.industries or []):
                return o
    if camp.offer_id:
        o = db.get(m.Offer, camp.offer_id)
        if o:
            return o
    return offers[0] if offers else None


def generate_message(db: Session, org: m.Organization, camp: m.Campaign, p: m.Person, comp: m.Company | None,
                     step: dict, step_index: int, history: list[m.Message] | None = None) -> dict:
    offer = route_offer(db, org.id, camp, comp)
    proof = [x.claim for x in db.scalars(select(m.ProofItem).where(m.ProofItem.organization_id == org.id,
                                                                   m.ProofItem.approved.is_(True)))][:3]
    facts, evidence = [], []
    if comp:
        facts.append(f"Company: {comp.name} ({comp.industry}, {comp.city or comp.country})")
        evidence.append({"fact": f"{comp.name} in {comp.city}", "source": comp.source})
        for s in (comp.signals or [])[:2]:
            facts.append(f"Signal: {s['text']} (source: {s['source']})")
            evidence.append({"fact": s["text"], "source": s["source"]})
        if (comp.research or {}).get("likely_pain_points"):
            facts.append("Likely pain: " + comp.research["likely_pain_points"][0])
    level = 0 + (1 if comp else 0) + (1 if p.title else 0) + (1 if comp and comp.research else 0) + \
        (1 if comp and comp.signals else 0) + (1 if history else 0)
    channel = step.get("channel", "email")
    prev = "\n".join(f"{h.direction}: {h.content[:300]}" for h in (history or [])[-4:])
    data, model = llm.complete_json(
        db, org, "message", "message_writer",
        f"Write a {channel} {step.get('type', 'initial')} message for B2B outreach as JSON {{subject, body}}. "
        f"Body under {'90' if channel in ('whatsapp', 'sms', 'linkedin') else '130'} words, plain text, no links, "
        "one clear CTA, sign as the sender first name only. Personalise only with the facts listed; never invent "
        "numbers, customers, funding or events. Approved proof points may be quoted verbatim.",
        f"Sender company: {org.name}\nOffer: {offer.name + ' - ' + offer.description if offer else 'our services'}\n"
        f"Problems we solve: {offer.problems if offer else []}\nApproved proof: {proof}\nCTA: "
        f"{offer.cta if offer else 'Open to a short call?'}\nRecipient: {p.first_name}, {p.title}\nFacts:\n"
        + "\n".join(facts) + f"\nPrevious messages:\n{llm.untrusted(prev, 2000)}", 900, camp.workspace_id)
    if data and data.get("body"):
        return {"subject": data.get("subject", "")[:200], "body": data["body"], "evidence": evidence,
                "personalization_level": min(level, 5), "model": model, "offer": offer.name if offer else ""}
    return _template_message(org, camp, p, comp, step, step_index, offer, proof, evidence, level)


def signal_phrase(text: str) -> str:
    """Turn a stored signal ('Hiring 12 sales roles…') into a verb phrase that follows the company name."""
    t = text.rstrip(".")
    low = t[0].lower() + t[1:] if t else t
    for prefix, repl in (("Hiring", "is hiring"), ("Opening", "is opening"), ("Expanding", "is expanding"),
                         ("Headcount", "has grown headcount"), ("Handles", "handles"), ("Multiple", "has multiple"),
                         ("Pricing page updated", "recently updated its pricing page")):
        if t.startswith(prefix):
            rest = t[len(prefix):]
            if prefix == "Headcount":
                rest = rest.replace(" grew", "", 1)
            if prefix.startswith("Pricing"):
                rest = ""
            return (repl + rest).strip()
    return low


def _template_message(org, camp, p, comp, step, idx, offer, proof, evidence, level):
    fn = p.first_name or "there"
    sender = (org.settings or {}).get("sender_name") or "Team " + org.name.split()[0]
    offer_name = offer.name if offer else "our automation services"
    problem = (offer.problems[0] if offer and offer.problems else "manual, repetitive work slowing the team down")
    benefit = (offer.benefits[0] if offer and offer.benefits else "free up hours every week")
    cta = offer.cta if offer and offer.cta else "Open to a 20-minute call next week?"
    if step.get("variant") == "B":  # controlled CTA experiment (PRD 86-87): only the CTA differs
        cta = (camp.messaging_strategy or {}).get("variant_b_cta") or "Worth a quick look? Happy to send a 2-minute overview."
    hook = ""
    if comp and comp.signals:
        hook = f"I noticed {comp.name} {signal_phrase(comp.signals[0]['text'])}."
    elif comp:
        hook = f"I came across {comp.name} while looking at {comp.industry.lower()} companies in {comp.city or comp.country}."
    role_line = {
        "Founder/CEO": "As a founder, you're probably the one feeling it when follow-ups slip.",
        "Sales Leadership": "For sales teams, that usually shows up as slow lead response and patchy follow-up.",
        "COO / Operations Leadership": "On the operations side it often means hours lost to repetitive manual steps.",
    }.get(p.normalized_title, "")
    proof_line = f" {proof[0]}" if proof else ""
    t = step.get("type", "initial")
    ch = step.get("channel", "email")
    if t == "initial":
        subject = f"{comp.name if comp else fn}: {offer_name.lower()}"
        body = (f"Hi {fn},\n\n{hook} Teams at that stage often struggle with {problem}. {role_line}\n\n"
                f"We help companies like yours with {offer_name.lower()} to {benefit}.{proof_line}\n\n{cta}\n\n{sender}")
    elif t == "breakup":
        subject = f"Closing the loop, {fn}"
        body = (f"Hi {fn},\n\nI haven't heard back, so I'll assume {offer_name.lower()} isn't a priority right now. "
                f"If that changes, just reply here and I'll share how others in {comp.industry.lower() if comp else 'your space'} "
                f"approached it.\n\n{sender}")
    elif t == "connect":
        subject = ""
        body = f"Hi {fn}, I work with {comp.industry.lower() if comp else 'B2B'} teams on {offer_name.lower()}. Would be glad to connect."
    elif t == "qualification_call":
        subject = "Call script"
        body = (f"Goal: qualify interest in {offer_name}. Open: reference {hook or 'their business'}. Ask: how do you "
                f"handle {problem} today? Who owns it? Timeline? If interested -> offer a 30-minute discovery slot.")
    else:
        subject = f"Re: {comp.name if comp else fn}: {offer_name.lower()}"
        nudges = [f"Quick follow-up on my note. Is {problem} on your radar this quarter?",
                  f"One more thought: most teams we speak with start small, automating a single workflow first. {cta}",
                  f"Would it help if I sent a 2-minute overview of how this works for {comp.industry.lower() if comp else 'teams like yours'}?"]
        body = f"Hi {fn},\n\n{nudges[min(idx - 1, 2) if idx else 0]}\n\n{sender}"
    if ch in ("whatsapp", "sms"):
        body = (f"Hi {fn}, this is {sender} from {org.name}. {hook} We help with {offer_name.lower()} to {benefit}. "
                f"Worth a quick chat? Reply STOP to opt out.")
    return {"subject": subject, "body": body, "evidence": evidence, "personalization_level": min(level, 5),
            "model": "template", "offer": offer.name if offer else ""}


# ------------------------------------------------------------------ serialisers
def company_dict(c: m.Company) -> dict:
    return {"id": c.id, "name": c.name, "domain": c.domain, "website": c.website, "description": c.description,
            "industry": c.industry, "sub_industry": c.sub_industry, "country": c.country, "state": c.state,
            "city": c.city, "employee_count": c.employee_count, "revenue_estimate": c.revenue_estimate,
            "founded_year": c.founded_year, "business_model": c.business_model, "linkedin_url": c.linkedin_url,
            "technologies": c.technologies or [], "signals": c.signals or [], "scores": c.scores or {},
            "score_explanation": c.score_explanation or {}, "research": c.research or {},
            "custom_criteria": c.custom_criteria or {}, "source": c.source, "why": why_company(c)}


def person_dict(db: Session, p: m.Person, comp: m.Company | None = None, reveal: bool = True) -> dict:
    comp = comp or (db.get(m.Company, p.company_id) if p.company_id else None)
    cf = p.custom_fields or {}
    return {"id": p.id, "full_name": p.full_name, "first_name": p.first_name, "last_name": p.last_name,
            "title": p.title, "normalized_title": p.normalized_title, "seniority": p.seniority,
            "department": p.department, "email": p.email if reveal else ("•••" if p.email else ""),
            "email_status": p.email_status, "phone": p.phone if reveal else ("•••" if p.phone else ""),
            "linkedin_url": p.linkedin_url, "city": p.city, "country": p.country, "stage": p.stage,
            "temperature": p.temperature, "icp_score": p.icp_score, "intent_score": p.intent_score,
            "owner_id": p.owner_id, "source": p.source, "feedback": p.feedback,
            "has_email": bool(p.email) or bool(cf.get("has_email")), "has_phone": bool(p.phone) or bool(cf.get("has_phone")),
            "company": {"id": comp.id, "name": comp.name, "industry": comp.industry, "domain": comp.domain,
                        "employee_count": comp.employee_count, "city": comp.city,
                        "signal": (comp.signals or [{}])[0].get("text", "") if comp.signals else ""} if comp else None,
            "created_at": p.created_at.isoformat() if p.created_at else None}
