"""Deterministic language understanding used directly, or as fallback when no
LLM key is configured: search parsing (PRD 15-16), title ontology (21), reply
intent taxonomy (71-72), OOO/referral/date extraction (174-175, 81)."""
import re
from datetime import datetime, timedelta

from .demo_data import INDUSTRIES, LOCATIONS

# ---------------------------------------------------------------- title ontology (PRD 21)
TITLE_ONTOLOGY = {
    "Founder/CEO": ["founder", "co-founder", "cofounder", "ceo", "chief executive", "managing director", "md",
                    "owner", "proprietor", "president"],
    "COO / Operations Leadership": ["coo", "chief operating", "head of operations", "operations head", "vp operations",
                                    "operations director", "director of operations", "admissions director"],
    "Sales Leadership": ["head of sales", "sales head", "vp sales", "vice president sales", "sales director",
                         "director of sales", "cro", "chief revenue", "head of revenue", "revenue director",
                         "commercial director"],
    "Sales Management": ["sales manager", "business development", "bdm", "account executive"],
    "Technology Leadership": ["cto", "chief technology", "head of it", "it head", "vp engineering", "it director",
                              "head of engineering"],
    "Marketing Leadership": ["cmo", "chief marketing", "head of marketing", "marketing director", "vp marketing"],
    "Customer Support Leadership": ["head of customer support", "support head", "customer success head",
                                    "head of customer success"],
    "Finance Leadership": ["cfo", "chief financial", "finance director", "head of finance"],
    "HR Leadership": ["hr director", "chro", "head of hr", "head of people"],
}


def normalize_title(title: str) -> str:
    t = f" {title.lower()} "
    for family, variants in TITLE_ONTOLOGY.items():
        for v in variants:
            if re.search(rf"(?<![a-z]){re.escape(v)}(?![a-z])", t):
                return family
    return "Other"


def expand_titles(titles: list[str]) -> set[str]:
    """'Head of Sales' -> the whole Sales Leadership family."""
    fams = set()
    for t in titles:
        f = normalize_title(t)
        if f != "Other":
            fams.add(f)
        for family in TITLE_ONTOLOGY:
            if t.lower() in family.lower():
                fams.add(family)
    return fams


# ---------------------------------------------------------------- search parser (PRD 15-16, 100)
INDUSTRY_KEYWORDS = {
    "Software & SaaS": ["saas", "software", "tech compan", "startup", "ai compan", "fintech", "hr tech", "martech"],
    "IT Services": ["it services", "it service", "it consulting", "software services", "outsourcing", "staff augmentation"],
    "Manufacturing": ["manufactur", "factory", "factories", "industrial", "auto component", "textile", "packaging"],
    "Healthcare": ["healthcare", "hospital", "clinic", "diagnostic", "health", "pharma", "medical"],
    "Education": ["education", "edtech", "school", "college", "universit", "institute", "coaching", "admission"],
    "Real Estate": ["real estate", "realty", "property", "properties", "developer", "co-working"],
    "Professional Services": ["professional services", "consulting firm", "accounting", "legal", "law firm",
                              "recruitment", "ca firm", "architect"],
    "Logistics": ["logistic", "freight", "warehous", "delivery", "supply chain", "cold chain"],
    "Retail & E-commerce": ["retail", "e-commerce", "ecommerce", "d2c", "store", "brand"],
    "Financial Services": ["financial services", "nbfc", "insurance", "wealth", "lending", "finance compan"],
    "Construction": ["construction", "contractor", "builder", "interior", "infrastructure"],
    "Marketing Agency": ["agency", "agencies", "marketing agenc", "digital marketing", "creative agenc"],
    "Hospitality": ["hospitality", "hotel", "restaurant", "travel", "event management"],
}
SIGNAL_KEYWORDS = {
    "hiring": ["hiring", "recruiting", "job opening"], "hiring_sales": ["hiring sales", "hiring salespeople",
                                                                         "sales team", "sales hiring", "hiring sales"],
    "funding": ["funded", "funding", "raised", "series a", "series b", "seed"],
    "expansion": ["expanding", "expansion", "new branch", "international"],
    "employee_growth": ["growing rapidly", "fast-growing", "fast growing", "high growth", "high-growth", "growing"],
    "process": ["manual", "whatsapp-heavy", "spreadsheet", "repetitive"],
    "technology": ["using", "uses", "adopted"],
}
COUNTRY_ALIASES = {"india": "India", "indian": "India", "usa": "United States", "us ": "United States",
                   "united states": "United States", "america": "United States", "uk": "United Kingdom",
                   "united kingdom": "United Kingdom", "britain": "United Kingdom", "singapore": "Singapore",
                   "uae": "United Arab Emirates", "dubai": "United Arab Emirates", "germany": "Germany",
                   "australia": "Australia"}
CITY_ALIASES = {"bengaluru": "Bangalore", "bombay": "Mumbai", "delhi": "New Delhi", "gurugram": "Gurgaon",
                "madras": "Chennai", "cochin": "Kochi", "calcutta": "Kolkata"}
STATES = {l[1].lower(): l[1] for l in LOCATIONS}
CITIES = {l[2].lower(): l[2] for l in LOCATIONS}
TECHS = sorted({t for ind in INDUSTRIES.values() for t in ind["tech"]}, key=len, reverse=True)
REGIONS = {"south india": ["Karnataka", "Tamil Nadu", "Kerala", "Telangana"],
           "north india": ["Delhi", "Haryana", "Uttar Pradesh", "Rajasthan"],
           "west india": ["Maharashtra", "Gujarat"]}


def empty_criteria() -> dict:
    return {"industries": [], "countries": [], "states": [], "cities": [], "exclude_cities": [],
            "exclude_industries": [], "employee_min": None, "employee_max": None, "revenue_min": None,
            "revenue_max": None, "technologies": [], "signals": [], "keywords": [], "business_model": [],
            "job_titles": [], "ai_criteria": [], "exclude_contacted": False}


def _find_places(text: str):
    countries, states, cities = [], [], []
    for alias, c in COUNTRY_ALIASES.items():
        if re.search(rf"\b{alias.strip()}\b", text) and c not in countries:
            countries.append(c)
    for region, sts in REGIONS.items():
        if region in text:
            states += [s for s in sts if s not in states]
            if "India" not in countries:
                countries.append("India")
    for k, s in STATES.items():
        if re.search(rf"\b{k}\b", text) and s not in states and s.lower() not in CITIES:
            states.append(s)
    for alias, c in {**{k: v for k, v in CITIES.items()}, **CITY_ALIASES}.items():
        if re.search(rf"\b{alias}\b", text) and c not in cities:
            cities.append(c)
    return countries, states, cities


def parse_search(query: str, base: dict | None = None) -> dict:
    """Converts NL into structured criteria. With `base`, applies a refinement
    ("Exclude companies in Mumbai", "only owners and CEOs") to existing criteria."""
    q = f" {query.lower()} "
    c = dict(base) if base else empty_criteria()
    for k, v in empty_criteria().items():
        c.setdefault(k, v)

    # exclusions first: "exclude/except/not in X"
    excl = re.findall(r"(?:exclude|excluding|except|not in|without|no)\s+(?:companies\s+)?(?:in\s+|from\s+)?([a-z &\-]+?)(?=[,.]| and | but |$| with )", q)
    excluded_text = " ".join(excl)
    for part in excl:
        _, _, cities = _find_places(f" {part} ")
        c["exclude_cities"] = sorted(set(c["exclude_cities"]) | set(cities))
        for ind, kws in INDUSTRY_KEYWORDS.items():
            if any(k in part for k in kws):
                c["exclude_industries"] = sorted(set(c["exclude_industries"]) | {ind})
        if "contacted" in part:
            c["exclude_contacted"] = True
    if "already contacted" in q:
        c["exclude_contacted"] = True
    pos = q
    for part in excl:
        pos = pos.replace(part, " ")

    countries, states, cities = _find_places(pos)
    if countries:
        c["countries"] = sorted(set(c["countries"]) | set(countries)) if base else countries
    if states:
        c["states"] = sorted(set(c["states"]) | set(states))
    if cities:
        c["cities"] = sorted(set(c["cities"]) | set(cities))
    c["cities"] = [x for x in c["cities"] if x not in c["exclude_cities"]]

    inds = [ind for ind, kws in INDUSTRY_KEYWORDS.items() if any(k in pos for k in kws)]
    if inds:
        c["industries"] = sorted(set(c["industries"]) | set(inds))
    c["industries"] = [i for i in c["industries"] if i not in c["exclude_industries"]]

    m = re.search(r"(\d[\d,]*)\s*(?:-|to|–)\s*(\d[\d,]*)\s*(?:employees|people|staff|emp)", q)
    if m:
        c["employee_min"], c["employee_max"] = int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))
    else:
        m = re.search(r"(\d[\d,]*)\s*\+\s*(?:employees|people|staff)|(?:more than|over|at least|above)\s*(\d[\d,]*)\s*(?:employees|people|staff)", q)
        if m:
            c["employee_min"] = int((m.group(1) or m.group(2)).replace(",", ""))
        m2 = re.search(r"(?:less than|under|below|fewer than|up to)\s*(\d[\d,]*)\s*(?:employees|people|staff)", q)
        if m2:
            c["employee_max"] = int(m2.group(1).replace(",", ""))
    if "mid-size" in q or "mid size" in q or "mid-market" in q:
        c["employee_min"], c["employee_max"] = c["employee_min"] or 100, c["employee_max"] or 1000
    if re.search(r"\bsmb|small business|sme\b", q):
        c["employee_max"] = c["employee_max"] or 500
    if "enterprise" in q and "enterprise it" not in q:
        c["employee_min"] = c["employee_min"] or 1000

    for sig, kws in SIGNAL_KEYWORDS.items():
        if sig == "technology":
            continue
        if any(k in pos for k in kws) and sig not in c["signals"]:
            c["signals"].append(sig)
    for t in TECHS:
        if t.lower() in pos and t not in c["technologies"]:
            c["technologies"].append(t)
    if " b2b " in q:
        c["business_model"] = ["B2B"]
    elif " b2c " in q:
        c["business_model"] = ["B2C"]

    titles = re.findall(r"\b(founders?|co-founders?|ceos?|coos?|ctos?|cmos?|cfos?|cros?|owners?|managing directors?|"
                        r"sales heads?|heads? of sales|vp sales|sales directors?|operations heads?|heads? of operations|"
                        r"heads? of marketing|heads? of it|it heads?|decision makers?)\b", pos)
    if titles:
        clean = []
        for t in titles:
            t = re.sub(r"s$", "", t).replace("heads of", "head of")
            clean.append("Founder" if t == "decision maker" else t.title() if len(t) > 4 else t.upper())
        if re.search(r"\bonly\b", q):
            c["job_titles"] = sorted(set(clean))
        else:
            c["job_titles"] = sorted(set(c["job_titles"]) | set(clean))
    m = re.search(r"likely to (?:need|have|buy|want) ([a-z \-/]+?)(?:[,.]|$| prioriti| and | with )", q)
    if m:
        crit = f"Likely to need {m.group(1).strip()}"
        if crit not in c["ai_criteria"]:
            c["ai_criteria"].append(crit)
    _ = excluded_text
    return c


def describe_criteria(c: dict) -> str:
    parts = []
    if c.get("industries"):
        parts.append(", ".join(c["industries"]))
    loc = c.get("cities") or c.get("states") or c.get("countries")
    if loc:
        parts.append("in " + ", ".join(loc))
    if c.get("employee_min") or c.get("employee_max"):
        parts.append(f"{c.get('employee_min') or 1}-{c.get('employee_max') or '∞'} employees")
    if c.get("signals"):
        parts.append("signals: " + ", ".join(c["signals"]))
    if c.get("exclude_cities"):
        parts.append("excluding " + ", ".join(c["exclude_cities"]))
    if c.get("job_titles"):
        parts.append("people: " + ", ".join(c["job_titles"]))
    return "; ".join(parts) or "all companies"


# ---------------------------------------------------------------- reply intent (PRD 71-72)
INTENT_RULES = [
    ("ANGRY", [r"\bspam(ming)?\b.*\b(stop|report)", r"how dare", r"harass", r"\bf\*+|\bwtf\b", r"report you",
               r"stop spamming", r"annoying", r"legal action", r"ridiculous"]),
    ("UNSUBSCRIBE", [r"\bstop\b", r"unsubscribe", r"remove me", r"take me off", r"do not (contact|email|message)",
                     r"don't (contact|email|message)", r"opt[ -]?out"]),
    ("OOO", [r"out of (the )?office", r"on (annual |parental )?leave", r"\booo\b", r"away until", r"back on",
             r"limited access to email"]),
    ("MEETING_CONFIRMED", [r"\b(confirmed|works for me|see you then|that works|booked)\b"]),
    ("MEETING_REQUEST", [r"\b(call|meet|meeting|chat|talk|demo|discuss)\b.*\b(monday|tuesday|wednesday|thursday|friday|saturday|tomorrow|next week|this week|today|\d{1,2}\s?(am|pm))\b",
                         r"(schedule|book|set up|arrange) (a |an )?(call|meeting|demo)", r"can we (talk|speak|meet|discuss)",
                         r"send (me )?(a |your )?(calendar|booking) link", r"let'?s (talk|chat|connect|meet)"]),
    ("PRICING", [r"pric(e|ing)", r"\bcost\b", r"how much", r"quote", r"budget", r"rates?\b"]),
    ("REFERRAL", [r"(speak|talk|reach out|contact|connect) (to|with) (\w+)", r"(\w+) (handles|owns|is responsible for) (this|that)",
                  r"right person (is|would be)", r"cc'?ing", r"loop(ing)? in"]),
    ("WRONG_PERSON", [r"wrong person", r"not the right person", r"not my (area|department)", r"no longer (work|with)"]),
    ("NOT_NOW", [r"not (right )?now", r"maybe later", r"next quarter", r"next year", r"few months", r"circle back",
                 r"not a priority", r"reach out (again )?(in|after)"]),
    ("NOT_INTERESTED", [r"not interested", r"no thanks", r"no, thank", r"we('| a)re (all )?(set|good)", r"not looking",
                        r"already (have|use|using)", r"pass on this"]),
    ("OBJECTION", [r"too expensive", r"we (already )?have (a|an) (vendor|partner|team)", r"in-?house", r"concern",
                   r"not sure (it|this)", r"doesn'?t (fit|work)", r"security", r"why should"]),
    ("VERY_INTERESTED", [r"(very|really) interested", r"evaluating", r"looking for (a|an|exactly)", r"need this",
                         r"timeline", r"how soon", r"implementation", r"we('| a)re (currently )?(looking|searching)"]),
    ("INTERESTED", [r"interest(ed|ing)", r"sounds good", r"tell me more", r"more (info|information|details)",
                    r"curious", r"keen", r"would like to (know|learn)", r"\byes\b", r"send (me )?(details|info|deck|case stud)"]),
    ("QUESTION", [r"\?", r"\b(what|how|which|who|when|where|do you|can you|is it)\b"]),
]

HOT = {"VERY_INTERESTED", "MEETING_REQUEST", "MEETING_CONFIRMED"}
WARM = {"INTERESTED", "PRICING", "QUESTION", "REFERRAL"}
NEGATIVE = {"NOT_INTERESTED", "UNSUBSCRIBE", "ANGRY", "SPAM"}
SAFE_AUTO = {"INTERESTED", "VERY_INTERESTED", "QUESTION", "MEETING_REQUEST", "MEETING_CONFIRMED", "OOO",
             "NOT_NOW", "NOT_INTERESTED", "UNSUBSCRIBE", "WRONG_PERSON", "REFERRAL"}


def classify_reply(text: str) -> dict:
    t = text.lower()
    matched = []
    for intent, pats in INTENT_RULES:
        for p in pats:
            if re.search(p, t):
                matched.append((intent, p))
                break
    intent = matched[0][0] if matched else "UNKNOWN"
    # priority adjustments: a meeting request with pricing is still a meeting request (hot)
    names = [x[0] for x in matched]
    if "MEETING_REQUEST" in names and intent in ("PRICING", "QUESTION", "INTERESTED"):
        intent = "MEETING_REQUEST"
    if intent == "QUESTION" and "INTERESTED" in names:
        intent = "INTERESTED"
    if intent == "INTERESTED" and "PRICING" in names:
        intent = "PRICING"
    sentiment = ("negative" if intent in NEGATIVE or intent == "OBJECTION" else
                 "positive" if intent in HOT | {"INTERESTED", "PRICING"} else "neutral")
    temperature = "hot" if intent in HOT else "warm" if intent in WARM else "cold"
    score = {"hot": 90, "warm": 65, "cold": 25}[temperature]
    if intent == "PRICING":
        score, temperature = 78, "hot"
    confidence = 0.55 if intent == "UNKNOWN" else min(0.95, 0.72 + 0.08 * len(matched))
    evidence = [f"matched '{p}' -> {i}" for i, p in matched[:3]]
    urgency = "high" if temperature == "hot" or intent == "ANGRY" else "normal"
    return {"intent": intent, "sentiment": sentiment, "temperature": temperature, "lead_score": score,
            "confidence": round(confidence, 2), "urgency": urgency, "evidence": evidence}


ESCALATION_PATTERNS = {
    "custom contract": r"contract|msa|nda|legal terms|terms and conditions",
    "legal question": r"\blegal\b|lawyer|gdpr|dpdp|compliance requirement",
    "security questionnaire": r"security questionnaire|soc ?2|iso ?27001|pen ?test|vapt",
    "pricing negotiation": r"discount|negotiat|lower (the )?price|best price",
    "complaint": r"complain|disappointed|unacceptable",
    "sensitive topic": r"lawsuit|death|medical emergency|harassment",
}


def escalation_reasons(text: str) -> list[str]:
    t = text.lower()
    return [k for k, p in ESCALATION_PATTERNS.items() if re.search(p, t)]


# ---------------------------------------------------------------- dates / OOO / referral
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]


def extract_day(text: str, now: datetime) -> tuple[datetime | None, str]:
    """Returns (date, part_of_day) for phrases like 'Thursday afternoon', 'tomorrow 3pm'."""
    t = text.lower()
    part = "afternoon" if "afternoon" in t else "morning" if "morning" in t else "evening" if "evening" in t else ""
    hm = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s?(am|pm)\b", t)
    day = None
    if "tomorrow" in t:
        day = now + timedelta(days=1)
    elif "today" in t:
        day = now
    else:
        for i, wd in enumerate(WEEKDAYS):
            if re.search(rf"\b{wd}\b", t):
                delta = (i - now.weekday()) % 7 or 7
                if "next week" in t and delta < 7:
                    delta += 7 if delta <= (6 - now.weekday()) else 0
                day = now + timedelta(days=delta)
                break
        if not day and "next week" in t:
            day = now + timedelta(days=(7 - now.weekday()))
    if day and hm:
        h = int(hm.group(1)) % 12 + (12 if hm.group(3) == "pm" else 0)
        day = day.replace(hour=h, minute=int(hm.group(2) or 0), second=0, microsecond=0)
        part = "exact"
    return day, part


def extract_return_date(text: str, now: datetime) -> datetime | None:
    t = text.lower()
    m = re.search(r"(?:until|till|back on|returning on|return on|back)\s+(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(MONTHS) + r")", t)
    m2 = re.search(r"(?:until|till|back on|returning on|return on|back)\s+(" + "|".join(MONTHS) + r")\s+(\d{1,2})", t)
    if m or m2:
        day, mon = (int(m.group(1)), m.group(2)) if m else (int(m2.group(2)), m2.group(1))
        year = now.year
        try:
            d = datetime(year, MONTHS.index(mon) + 1, day, 10, 0)
        except ValueError:
            return None
        if d < now:
            d = d.replace(year=year + 1)
        return d
    d, _ = extract_day(t.split("until")[-1] if "until" in t else t, now)
    return d


def extract_referral(text: str) -> dict | None:
    m = re.search(r"(?:[Ss]peak|[Tt]alk|[Rr]each out|[Cc]ontact|[Cc]onnect|[Ww]rite)\s+(?:to|with)\s+([A-Z][a-z]+)(?:\s+([A-Z][a-z]+))?"
                  r"(?:[^.]*?\b(?:in|from|on)\s+(?:our|the)\s+([a-z ]+?)\s+(?:team|department))?", text)
    if not m:
        m = re.search(r"([A-Z][a-z]+)(?:\s+([A-Z][a-z]+))?\s+(?:handles|owns|is responsible for)", text)
        if not m:
            return None
        return {"first_name": m.group(1), "last_name": m.group(2) or "", "department": ""}
    email = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    return {"first_name": m.group(1), "last_name": m.group(2) or "", "department": (m.group(3) or "").strip(),
            "email": email.group(0) if email else ""}
