"""LeadOcean data provider (https://api.leadocean.io, OpenAPI at /openapi.json).

Auth: `x-api-key` header. The key lives only in the admin credential vault as
LEADOCEAN_API_KEY (organization-level, or a platform default set by the Super Admin).

Used for: company search, people search, work-email and phone enrichment.
Every record LeadOcean returns counts against the account's monthly records, so
searches request only the page size actually needed.
"""
from datetime import datetime

import httpx

from .nlp import TITLE_ONTOLOGY, normalize_title

BASE = "https://api.leadocean.io"

# App industry categories -> LeadOcean's normalised industry labels (all exact enum values).
INDUSTRY_MAP = {
    "Software & SaaS": ["Software Development", "Computer Software", "Technology; Information and Internet", "Internet"],
    "IT Services": ["IT Services and IT Consulting", "Information Technology and Services", "IT System Custom Software Development"],
    "Manufacturing": ["Manufacturing", "Industrial Machinery Manufacturing", "Machinery Manufacturing",
                      "Motor Vehicle Parts Manufacturing", "Textile Manufacturing", "Packaging and Containers Manufacturing",
                      "Electrical Equipment Manufacturing", "Chemical Manufacturing"],
    "Healthcare": ["Hospitals and Health Care", "Hospital and Health Care", "Hospitals", "Medical Practices",
                   "Medical and Diagnostic Laboratories", "Pharmaceutical Manufacturing"],
    "Education": ["Education", "Higher Education", "Primary and Secondary Education", "E-Learning Providers",
                  "Education Administration Programs", "Professional Training and Coaching"],
    "Real Estate": ["Real Estate", "Commercial Real Estate", "Real Estate Agents and Brokers", "Leasing Residential Real Estate"],
    "Professional Services": ["Professional Services", "Accounting", "Legal Services", "Law Practice", "Management Consulting",
                              "Business Consulting and Services", "Staffing and Recruiting", "Architecture and Planning"],
    "Logistics": ["Transportation; Logistics; Supply Chain and Storage", "Logistics and Supply Chain",
                  "Freight and Package Transportation", "Warehousing and Storage", "Truck Transportation"],
    "Retail & E-commerce": ["Retail", "Online and Mail Order Retail", "Retail Apparel and Fashion", "Consumer Goods",
                            "Food and Beverage Retail"],
    "Financial Services": ["Financial Services", "Banking", "Insurance", "Investment Management", "Credit Intermediation",
                           "Loan Brokers"],
    "Construction": ["Construction", "Building Construction", "Civil Engineering", "Residential Building Construction",
                     "Interior Design", "Specialty Trade Contractors"],
    "Marketing Agency": ["Advertising Services", "Marketing Services", "Marketing and Advertising",
                         "Public Relations and Communications Services"],
    "Hospitality": ["Hospitality", "Hotels and Motels", "Restaurants", "Travel Arrangements", "Events Services",
                    "Leisure; Travel and Tourism"],
}
REVERSE_INDUSTRY = {lo.lower(): app for app, los in INDUSTRY_MAP.items() for lo in los}

COUNTRY_CODES = {"India": "IN", "United States": "US", "United Kingdom": "GB", "Singapore": "SG",
                 "United Arab Emirates": "AE", "Germany": "DE", "Australia": "AU", "Canada": "CA", "France": "FR",
                 "Netherlands": "NL", "Saudi Arabia": "SA", "Malaysia": "MY", "Indonesia": "ID", "South Africa": "ZA"}
COUNTRY_NAMES = {v: k for k, v in COUNTRY_CODES.items()}

# LeadOcean email_status -> platform verification status (PRD 151).
EMAIL_STATUS = {"verified": "VALID", "catch_all_valid": "VALID", "catch_all": "CATCH_ALL", "risky": "RISKY",
                "unknown": "UNKNOWN", "untested": "UNKNOWN", "derived": "RISKY", "invalid": "INVALID",
                "role": "ROLE_ACCOUNT", "disposable": "DISPOSABLE", "spam_trap": "INVALID", "abuse": "INVALID",
                "none": "INVALID"}
SENDABLE = ["verified", "catch_all_valid", "catch_all"]


class LeadOceanError(RuntimeError):
    pass


def _post(key: str, path: str, body: dict, timeout: float = 30) -> dict:
    try:
        r = httpx.post(f"{BASE}{path}", json=body, headers={"x-api-key": key, "Accept": "application/json"}, timeout=timeout)
    except httpx.HTTPError as exc:
        raise LeadOceanError(f"LeadOcean unreachable: {exc}") from exc
    if r.status_code == 401:
        raise LeadOceanError("LeadOcean rejected the API key (401). Check LEADOCEAN_API_KEY in the vault.")
    if r.status_code == 402:
        raise LeadOceanError("LeadOcean monthly records are used up (402). Upgrade the LeadOcean plan or wait for the reset.")
    if r.status_code == 429:
        raise LeadOceanError("LeadOcean rate limit reached (429). Try again shortly.")
    if r.status_code >= 400:
        try:
            msg = r.json().get("error") or r.json().get("message") or r.text
        except ValueError:
            msg = r.text
        raise LeadOceanError(f"LeadOcean {r.status_code}: {str(msg)[:200]}")
    return r.json()


def get_account(key: str) -> dict:
    r = httpx.get(f"{BASE}/v1/account", headers={"x-api-key": key}, timeout=15)
    if r.status_code >= 400:
        raise LeadOceanError(f"LeadOcean {r.status_code}: {r.text[:200]}")
    return r.json().get("data", {})


def _rows(resp: dict) -> list:
    return resp.get("data") or resp.get("items") or []


def _clean(body: dict) -> dict:
    return {k: v for k, v in body.items() if v not in (None, [], "", {})}


# ------------------------------------------------------------------ criteria -> LeadOcean filters
def company_filters(c: dict) -> dict:
    industries = [lo for app in c.get("industries") or [] for lo in INDUSTRY_MAP.get(app, [])]
    excl = [lo for app in c.get("exclude_industries") or [] for lo in INDUSTRY_MAP.get(app, [])]
    body = {
        "industry": industries, "excludeIndustry": excl,
        "hqCountry": [COUNTRY_CODES[x] for x in c.get("countries") or [] if x in COUNTRY_CODES],
        "hqState": c.get("states") or [], "hqCity": c.get("cities") or [],
        "minEmployees": c.get("employee_min"), "maxEmployees": c.get("employee_max"),
        "minRevenue": c.get("revenue_min"), "maxRevenue": c.get("revenue_max"),
        "technologies": c.get("technologies") or [], "keywords": c.get("keywords") or [],
    }
    if "funding" in (c.get("signals") or []):
        body["minLastFundingYear"] = datetime.now().year - 2
    return _clean(body)


def title_keywords(titles: list[str]) -> list[str]:
    """Expand 'Head of Sales' into its ontology family so LeadOcean's keyword match finds the variants."""
    out = []
    for t in titles:
        fam = normalize_title(t)
        variants = TITLE_ONTOLOGY.get(fam, [])
        out.append(t)
        out.extend(v for v in variants if v != "md")   # "md" alone is too ambiguous
    seen, uniq = set(), []
    for t in out:
        k = t.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(t)
    return uniq[:25]


# ------------------------------------------------------------------ record -> canonical
def _year_from(date_str) -> int:
    try:
        return int(str(date_str)[:4])
    except (TypeError, ValueError):
        return 0


def company_from_record(rec: dict) -> dict:
    cd = rec.get("company_data") or {}
    addr = cd.get("company_primary_address") or {}
    emp = cd.get("company_employees") or {}
    inds = cd.get("company_industries") or []
    raw_ind = (inds[0].get("name3") or inds[0].get("name") or "") if inds else ""
    funding = cd.get("company_funding") or {}
    techs = rec.get("company_detected_technologies") or []
    if isinstance(techs, dict):
        techs = list(techs.keys())
    techs = [t.get("name") if isinstance(t, dict) else str(t) for t in techs][:12]
    signals = []
    year = funding.get("last_round_year") or 0
    if year and year >= datetime.now().year - 2:
        rtype = funding.get("last_round_type") or "funding"
        signals.append({"type": "funding", "text": f"Raised a {rtype} round in {year}", "source": "LeadOcean",
                        "observed_days_ago": max(1, (datetime.now().year - year) * 365)})
    dm = cd.get("company_number_of_decision_makers")
    domain = (cd.get("company_domain") or "").lower()
    web = (cd.get("company_web_address") or {}).get("url") or (f"https://{domain}" if domain else "")
    completion = (rec.get("company_metrics") or {}).get("completion_score")
    country = addr.get("country_code") or addr.get("country") or ""
    return {
        "provider_ref": f"lo_c_{cd.get('company_id')}", "name": cd.get("company_name") or domain,
        "domain": domain, "website": web, "description": (cd.get("company_description") or "")[:1500],
        "industry": REVERSE_INDUSTRY.get(raw_ind.lower(), raw_ind), "sub_industry": raw_ind,
        "country": COUNTRY_NAMES.get(country, country), "state": addr.get("state") or "", "city": addr.get("city") or "",
        "employee_count": emp.get("number_of_employees") or emp.get("number_of_employees_min") or 0,
        "revenue_estimate": cd.get("company_revenue_in_usd") or 0,
        "founded_year": _year_from((cd.get("company_profile") or {}).get("date_founded")),
        "business_model": "", "technologies": techs, "signals": signals,
        "linkedin_url": ((cd.get("company_social_links") or {}).get("linkedin") or {}).get("url", ""),
        "data_confidence": float(completion) if completion else 0.75,
        "decision_makers": dm,
    }


def person_from_record(rec: dict) -> dict:
    pd = rec.get("profile_data") or {}
    cd = rec.get("contact_data") or {}
    meta = rec.get("meta") or {}
    ids = meta.get("source_ids") or {}
    exps = cd.get("contact_current_experiences") or []
    cur = next((e for e in exps if e.get("current")), exps[0] if exps else {})
    addr = pd.get("profile_address") or {}
    country = addr.get("country_code") or ""
    title = cur.get("job_title") or pd.get("profile_headline") or ""
    emp = cur.get("company_employees") or {}
    raw_ind = cur.get("company_industry") or ""
    company = None
    if cur.get("company_name") or cur.get("company_domain"):
        company = {"provider_ref": f"lo_c_{ids.get('company_id')}" if ids.get("company_id") else "",
                   "name": cur.get("company_name") or cur.get("company_domain"),
                   "domain": (cur.get("company_domain") or "").lower(),
                   "website": f"https://{cur['company_domain']}" if cur.get("company_domain") else "",
                   "industry": REVERSE_INDUSTRY.get(raw_ind.lower(), raw_ind), "sub_industry": raw_ind,
                   "employee_count": emp.get("number_of_employees") or emp.get("number_of_employees_min") or 0,
                   "linkedin_url": ((cur.get("company_social_links") or {}).get("linkedin") or {}).get("url", ""),
                   "country": COUNTRY_NAMES.get(country, country), "technologies": [], "signals": []}
    status = cd.get("email_status")
    return {
        "provider_ref": f"lo_p_{ids.get('person_id')}", "company_ref": company["provider_ref"] if company else "",
        "company": company, "first_name": pd.get("profile_first_name") or "", "last_name": pd.get("profile_last_name") or "",
        "full_name": pd.get("profile_full_name") or "", "title": title, "normalized_title": normalize_title(title),
        "seniority": (cur.get("job_seniority") or {}).get("job_level") or "", "department": "",
        "email": "", "phone": "", "linkedin_url": pd.get("profile_url") or "",
        "country": COUNTRY_NAMES.get(country, country), "city": addr.get("city") or "",
        "has_email": bool(cd.get("has_email")), "has_phone": bool(cd.get("has_phone")),
        "email_status_hint": EMAIL_STATUS.get(status or "", "UNKNOWN"),
    }


# ------------------------------------------------------------------ provider classes (plugged into ProviderRouter)
class LeadOceanCompanySearch:
    def search(self, criteria, limit, ctx):
        body = {**company_filters(criteria), "limit": max(1, min(int(limit or 25), 100)), "slim": True}
        resp = _post(ctx["api_key"], "/v1/companies/search", body)
        meta = resp.get("meta") or {}
        if ctx.get("meta") is not None:
            ctx["meta"].update({"total": meta.get("total"), "unsupported": meta.get("unsupportedFilters") or [],
                                "notice": (meta.get("notice") or {}).get("message")})
        rows = [company_from_record(r) for r in _rows(resp)]
        excl = set(criteria.get("exclude_cities") or [])
        return [r for r in rows if r["city"] not in excl]


class LeadOceanPeopleSearch:
    def search(self, criteria, company_refs, limit, ctx):
        body = {"title": title_keywords(criteria.get("job_titles") or []),
                "limit": max(1, min(int(limit or 25), 100)), "slim": True, "emailStatus": SENDABLE}
        domains = [d for d in (ctx.get("domains") or []) if d and not d.endswith(".demo")]
        if domains:
            body["domain"] = domains[:100]
        else:
            cf = company_filters(criteria)
            body.update({k: v for k, v in cf.items() if k in ("industry", "excludeIndustry", "hqCountry", "hqState",
                                                              "hqCity", "minEmployees", "maxEmployees", "technologies")})
            if criteria.get("cities"):
                body["city"] = criteria["cities"]
            if criteria.get("exclude_cities"):
                body["excludeCity"] = criteria["exclude_cities"]
        resp = _post(ctx["api_key"], "/v1/people/search", _clean(body))
        if ctx.get("meta") is not None:
            ctx["meta"].update({"unsupported": (resp.get("meta") or {}).get("unsupportedFilters") or []})
        return [person_from_record(r) for r in _rows(resp)]


def _person_id(person: dict) -> str | None:
    ref = person.get("provider_ref") or ""
    return ref[5:] if ref.startswith("lo_p_") else None


class LeadOceanEmail:
    """Work email at the CURRENT employer, only addresses LeadOcean actually checked."""
    def find_email(self, person, ctx):
        pid = _person_id(person)
        body = {"person_id": pid} if pid else ({"linkedin_url": person["linkedin_url"]}
                                              if (person.get("linkedin_url") or "").startswith("http") else None)
        if not body:
            return {"email": ""}
        data = _post(ctx["api_key"], "/v2/people/email/work", body).get("data")
        if not data or not data.get("email"):
            return {"email": ""}
        return {"email": data["email"].lower(), "confidence": 0.9 if data.get("status") == "verified" else 0.75,
                "source": "leadocean", "status": EMAIL_STATUS.get(data.get("status") or "", "UNKNOWN")}


class LeadOceanPhone:
    def find_phone(self, person, ctx):
        pid = _person_id(person)
        body = {"person_id": pid} if pid else ({"linkedin_url": person["linkedin_url"]}
                                              if (person.get("linkedin_url") or "").startswith("http") else None)
        if not body:
            return {"phone": ""}
        data = _post(ctx["api_key"], "/v2/people/phone", body).get("data") or {}
        best = data.get("best") or {}
        return {"phone": best.get("phone") or "", "confidence": 0.8, "type": best.get("type")}
