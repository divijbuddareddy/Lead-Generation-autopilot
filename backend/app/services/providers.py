"""Provider architecture (PRD 23-24, 156, 222-223).

Business logic never calls a vendor directly: it calls ProviderRouter, which
picks enabled providers by priority, records health, and fails over.
"""
import abc
import hashlib
import re
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from . import core
from .demo_data import companies_by_ref, dataset
from .nlp import expand_titles, normalize_title


# ------------------------------------------------------------------ interfaces
class CompanySearchProvider(abc.ABC):
    @abc.abstractmethod
    def search(self, criteria: dict, limit: int, ctx: dict) -> list[dict]: ...


class PeopleSearchProvider(abc.ABC):
    @abc.abstractmethod
    def search(self, criteria: dict, company_refs: list[str] | None, limit: int, ctx: dict) -> list[dict]: ...


class EmailEnrichmentProvider(abc.ABC):
    @abc.abstractmethod
    def find_email(self, person: dict, ctx: dict) -> dict: ...


class PhoneEnrichmentProvider(abc.ABC):
    @abc.abstractmethod
    def find_phone(self, person: dict, ctx: dict) -> dict: ...


class EmailVerificationProvider(abc.ABC):
    @abc.abstractmethod
    def verify(self, email: str, ctx: dict) -> dict: ...


class ResearchProvider(abc.ABC):
    @abc.abstractmethod
    def research(self, entity: dict, depth: str, ctx: dict) -> dict: ...


# ------------------------------------------------------------------ demo provider (synthetic data)
def _match_company(c: dict, k: dict) -> bool:
    if k.get("industries") and c["industry"] not in k["industries"]:
        return False
    if k.get("exclude_industries") and c["industry"] in k["exclude_industries"]:
        return False
    if k.get("countries") and c["country"] not in k["countries"]:
        return False
    if k.get("states") and c["state"] not in k["states"] and not (k.get("cities") and c["city"] in k["cities"]):
        return False
    if k.get("cities") and c["city"] not in k["cities"]:
        return False
    if k.get("exclude_cities") and c["city"] in k["exclude_cities"]:
        return False
    if k.get("employee_min") and c["employee_count"] < k["employee_min"]:
        return False
    if k.get("employee_max") and c["employee_count"] > k["employee_max"]:
        return False
    if k.get("revenue_min") and c["revenue_estimate"] < k["revenue_min"]:
        return False
    if k.get("business_model") and c["business_model"] not in k["business_model"]:
        return False
    if k.get("technologies") and not set(k["technologies"]) & set(c["technologies"]):
        return False
    sig_types = {s["type"] for s in c["signals"]}
    sig_text = " ".join(s["text"].lower() for s in c["signals"])
    for s in k.get("signals", []):
        if s == "hiring_sales" and "sales roles" not in sig_text:
            return False
        if s != "hiring_sales" and s not in sig_types:
            return False
    for kw in k.get("keywords", []):
        if kw.lower() not in (c["name"] + c["description"]).lower():
            return False
    return True


class DemoCompanyProvider(CompanySearchProvider):
    def search(self, criteria, limit, ctx):
        # Local synthetic data is free: return every match and let the platform rank it.
        rows = [dict(c) for c in dataset()[0] if _match_company(c, criteria)]
        if ctx.get("meta") is not None:
            ctx["meta"]["total"] = len(rows)
        return rows


class DemoPeopleProvider(PeopleSearchProvider):
    def search(self, criteria, company_refs, limit, ctx):
        fams = expand_titles(criteria.get("job_titles") or [])
        comp = companies_by_ref()
        allowed = set(company_refs) if company_refs is not None else None
        out = []
        for p in dataset()[1]:
            if allowed is not None and p["company_ref"] not in allowed:
                continue
            if allowed is None and not _match_company(comp[p["company_ref"]], criteria):
                continue
            if fams and p["normalized_title"] not in fams:
                continue
            q = dict(p)
            q["has_email"], q["has_phone"] = bool(p["email"]), bool(p["phone"])
            q["email"], q["phone"] = "", ""   # contact data costs an enrichment call
            out.append(q)
            if limit and len(out) >= limit:
                break
        return out


class DemoEmailProvider(EmailEnrichmentProvider):
    def find_email(self, person, ctx):
        src = next((p for p in dataset()[1] if p["provider_ref"] == person.get("provider_ref")), None)
        if src and src["email"]:
            return {"email": src["email"], "confidence": 0.9, "source": "demo_provider"}
        return {"email": "", "confidence": 0, "source": "demo_provider"}


class PatternEmailProvider(EmailEnrichmentProvider):
    """Cheap first-pass: guesses first.last@domain (low confidence, must be verified)."""
    def find_email(self, person, ctx):
        dom = person.get("domain") or ""
        if not dom or not person.get("first_name"):
            return {"email": "", "confidence": 0}
        local = f"{person['first_name']}.{person.get('last_name', '')}".lower().strip(".").replace(" ", "")
        return {"email": f"{re.sub(r'[^a-z.]', '', local)}@{dom}", "confidence": 0.45, "source": "pattern"}


class DemoPhoneProvider(PhoneEnrichmentProvider):
    def find_phone(self, person, ctx):
        src = next((p for p in dataset()[1] if p["provider_ref"] == person.get("provider_ref")), None)
        return {"phone": src["phone"] if src else "", "confidence": 0.8 if src and src["phone"] else 0}


ROLE_LOCALS = {"info", "sales", "admin", "support", "contact", "hello", "office", "hr", "careers", "team"}
DISPOSABLE = {"mailinator.com", "10minutemail.com", "guerrillamail.com", "tempmail.com", "yopmail.com"}


class SyntaxVerificationProvider(EmailVerificationProvider):
    """Statuses per PRD 151: VALID, INVALID, RISKY, CATCH_ALL, UNKNOWN, DISPOSABLE, ROLE_ACCOUNT."""
    def verify(self, email, ctx):
        email = (email or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9._%+'\-]+@[a-z0-9.\-]+\.[a-z]{2,}", email):
            return {"status": "INVALID"}
        local, dom = email.split("@", 1)
        if dom in DISPOSABLE:
            return {"status": "DISPOSABLE"}
        if local in ROLE_LOCALS:
            return {"status": "ROLE_ACCOUNT"}
        if dom.endswith(".demo"):
            return {"status": "VALID", "note": "synthetic demo record"}
        if ctx.get("source") == "pattern":
            return {"status": "RISKY"}
        return {"status": "VALID" if ctx.get("confidence", 0.8) >= 0.7 else "UNKNOWN"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text, self.links, self.title, self._skip, self._in_title = [], [], "", 0, False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip and data.strip():
            self.text.append(data.strip())


KEY_PAGES = ["about", "product", "service", "pricing", "case", "customer", "industr", "solution", "career", "contact"]
_page_cache: dict[str, tuple[float, dict]] = {}


def crawl_website(url: str, max_pages: int = 5) -> dict:
    """Website Research Engine (PRD 30, 162). Content is untrusted data."""
    if not url.startswith("http"):
        url = "https://" + url
    host = urlparse(url).netloc
    if host.endswith(".demo") or not host:
        return {"pages": [], "error": "synthetic domain - no live website"}
    pages, seen, queue = [], set(), [url]
    with httpx.Client(timeout=10, follow_redirects=True,
                      headers={"User-Agent": "GTM-Research-Bot/1.0 (+business research)"}) as client:
        while queue and len(pages) < max_pages:
            u = queue.pop(0)
            if u in seen:
                continue
            seen.add(u)
            cached = _page_cache.get(u)
            if cached and time.time() - cached[0] < 86400:
                pages.append(cached[1])
                continue
            try:
                r = client.get(u)
                if r.status_code >= 400 or "text/html" not in r.headers.get("content-type", ""):
                    continue
            except httpx.HTTPError:
                continue
            ex = _TextExtractor()
            ex.feed(r.text[:400_000])
            text = re.sub(r"\s+", " ", " ".join(ex.text))[:8000]
            page = {"url": u, "title": ex.title.strip()[:200], "text": text,
                    "content_hash": hashlib.sha256(text.encode()).hexdigest()[:16]}
            _page_cache[u] = (time.time(), page)
            pages.append(page)
            if len(pages) == 1:
                for href in ex.links:
                    full = urljoin(u, href).split("#")[0]
                    if urlparse(full).netloc == urlparse(u).netloc and any(k in full.lower() for k in KEY_PAGES):
                        if full not in queue and full not in seen:
                            queue.append(full)
    return {"pages": pages}


class WebsiteResearchProvider(ResearchProvider):
    def research(self, entity, depth, ctx):
        pages = {"quick": 1, "standard": 3, "deep": 6}.get(depth, 3)
        return crawl_website(entity.get("website") or entity.get("domain", ""), pages)


class GenericRESTCompanyProvider(CompanySearchProvider):
    """Plug-in adapter for any licensed provider exposing POST {endpoint} -> [canonical companies].
    Endpoint URL lives in provider.config; the API key in the admin vault (DATA_PROVIDER_API_KEY)."""
    def search(self, criteria, limit, ctx):
        endpoint, key = ctx.get("config", {}).get("endpoint"), ctx.get("api_key")
        if not endpoint or not key:
            raise RuntimeError("provider not configured")
        r = httpx.post(endpoint, json={"criteria": criteria, "limit": limit},
                       headers={"Authorization": f"Bearer {key}"}, timeout=20)
        r.raise_for_status()
        return r.json().get("companies", [])


from .leadocean import LeadOceanCompanySearch, LeadOceanEmail, LeadOceanPeopleSearch, LeadOceanPhone  # noqa: E402

IMPLEMENTATIONS = {
    "leadocean_companies": LeadOceanCompanySearch, "leadocean_people": LeadOceanPeopleSearch,
    "leadocean_email": LeadOceanEmail, "leadocean_phone": LeadOceanPhone,
    "demo_companies": DemoCompanyProvider, "demo_people": DemoPeopleProvider, "demo_email": DemoEmailProvider,
    "pattern_email": PatternEmailProvider, "demo_phone": DemoPhoneProvider, "syntax_verifier": SyntaxVerificationProvider,
    "website_research": WebsiteResearchProvider, "external_rest_companies": GenericRESTCompanyProvider,
}

LO_LICENSE = {"persistent_storage_allowed": True, "export_allowed": True, "cache_allowed": True}
REGISTRY = [
    # id, name, kind, priority, cost, secret, description, license, enabled
    ("leadocean_companies", "LeadOcean - Companies", "company_search", 5, 0.0, "LEADOCEAN_API_KEY",
     "Real B2B company data (~63M companies). Each company returned uses 1 LeadOcean record.", LO_LICENSE, True),
    ("leadocean_people", "LeadOcean - People", "people_search", 5, 0.0, "LEADOCEAN_API_KEY",
     "Real decision makers (~690M profiles), deliverable-email people only. 1 record per person.", LO_LICENSE, True),
    ("leadocean_email", "LeadOcean - Work email", "email_enrichment", 5, 0.0, "LEADOCEAN_API_KEY",
     "Verified work email at the current employer. 1 record per lookup.", LO_LICENSE, True),
    ("leadocean_phone", "LeadOcean - Phone", "phone_enrichment", 5, 0.0, "LEADOCEAN_API_KEY",
     "Phone numbers for a person. 1 record per lookup.", LO_LICENSE, True),
    ("demo_companies", "Demo Data Provider - Companies (synthetic)", "company_search", 50, 0.0, "",
     "720 fictional companies across India and global hubs, for demos and testing.",
     {"persistent_storage_allowed": True, "export_allowed": True, "retention_days": 0}, True),
    ("external_rest_companies", "External licensed provider (REST adapter)", "company_search", 10, 0.002,
     "DATA_PROVIDER_API_KEY", "Connect any licensed company-data API. Set endpoint in config and key in vault.",
     {"persistent_storage_allowed": True, "export_allowed": False, "retention_days": 30}, False),
    ("demo_people", "Demo Data Provider - People (synthetic)", "people_search", 50, 0.0, "",
     "Fictional decision makers mapped to demo companies.",
     {"persistent_storage_allowed": True, "export_allowed": True}, True),
    ("pattern_email", "Email pattern guesser", "email_enrichment", 30, 0.0, "",
     "Low-confidence fallback (first.last@domain); result is marked RISKY until verified.", {"export_allowed": True}, True),
    ("demo_email", "Demo email finder", "email_enrichment", 10, 0.01, "", "Returns demo emails.",
     {"export_allowed": True}, True),
    ("demo_phone", "Demo phone finder", "phone_enrichment", 20, 0.04, "", "Returns placeholder phones.",
     {"export_allowed": False}, True),
    ("syntax_verifier", "Syntax / role / disposable verifier", "email_verification", 10, 0.0, "",
     "Offline verification heuristics.", {}, True),
    ("website_research", "Website crawler", "research", 10, 0.0, "", "Fetches public company websites.",
     {"cache_allowed": True}, True),
]


def ensure_registry(db: Session):
    from ..config import DEMO_DATA
    for pid, name, kind, prio, cost, sec, desc, lic, enabled in REGISTRY:
        row = db.get(m.Provider, pid)
        if not row:
            row = m.Provider(id=pid, name=name, kind=kind, priority=prio, cost_usd=cost, requires_secret=sec,
                             description=desc, license=lic, enabled=enabled)
            db.add(row)
        if pid.startswith("demo"):
            row.enabled = DEMO_DATA   # synthetic data is never used outside demo/test mode


class ProviderRouter:
    """Cost/priority ordered routing with failover and health tracking."""

    def __init__(self, db: Session, org_id: str):
        self.db, self.org_id = db, org_id

    def _providers(self, kind: str) -> list[m.Provider]:
        return list(self.db.scalars(select(m.Provider).where(m.Provider.kind == kind, m.Provider.enabled.is_(True))
                                    .order_by(m.Provider.priority)))

    def call(self, kind: str, method: str, *args, accept=lambda r: True, **ctx):
        errors = []
        self.last_errors = errors
        self.attempted = 0
        real_attempted = False
        for prov in self._providers(kind):
            impl = IMPLEMENTATIONS.get(prov.id)
            if not impl:
                continue
            # Once a real (licensed) provider has been tried, never silently fall back to synthetic demo data.
            if real_attempted and prov.id.startswith("demo"):
                continue
            pctx = dict(ctx, config=prov.config or {})
            if prov.requires_secret:
                key = core.get_secret(self.db, self.org_id, prov.requires_secret)
                if not key:
                    continue
                pctx["api_key"] = key
                real_attempted = True
            t0 = time.time()
            self.attempted += 1
            prov.calls += 1
            try:
                res = getattr(impl(), method)(*args, pctx)
                prov.avg_latency_ms = round(prov.avg_latency_ms * 0.8 + (time.time() - t0) * 1000 * 0.2, 1)
                if accept(res):
                    return res, prov
                errors.append(f"{prov.id}: no result")
            except Exception as exc:  # noqa: BLE001 - failover
                prov.failures += 1
                errors.append(f"{prov.id}: {exc}")
        return None, None

    def search_companies(self, criteria, limit, meta: dict | None = None):
        res, prov = self.call("company_search", "search", criteria, limit, accept=lambda r: r is not None, meta=meta)
        return res or [], prov

    def search_people(self, criteria, refs, limit, domains: list | None = None, meta: dict | None = None):
        res, prov = self.call("people_search", "search", criteria, refs, limit, accept=lambda r: r is not None,
                              domains=domains, meta=meta)
        return res or [], prov

    def find_email(self, person: dict):
        res, prov = self.call("email_enrichment", "find_email", person, accept=lambda r: bool(r.get("email")))
        return res or {"email": ""}, prov

    def find_phone(self, person: dict):
        res, prov = self.call("phone_enrichment", "find_phone", person, accept=lambda r: bool(r.get("phone")))
        return res or {"phone": ""}, prov

    def verify(self, email: str, **ctx):
        res, prov = self.call("email_verification", "verify", email, **ctx)
        return res or {"status": "UNKNOWN"}, prov

    def research(self, entity: dict, depth: str):
        res, prov = self.call("research", "research", entity, depth)
        return res or {"pages": []}, prov


def normalize_person_title(p: dict) -> dict:
    p.setdefault("normalized_title", normalize_title(p.get("title", "")))
    return p
