"""Deterministic SYNTHETIC B2B dataset used by the built-in "Demo Data Provider".

These companies and people are fictional. Domains use the non-routable `.demo`
TLD and phone numbers are placeholders, and the channel layer refuses to send
real messages to demo records - they only flow through the sandbox.
Replace or supplement with a licensed provider in Data Sources (PRD 8, 23).
"""
import random
from functools import lru_cache

LOCATIONS = [
    # (country, state, city, timezone, weight)
    ("India", "Karnataka", "Bangalore", "Asia/Kolkata", 14), ("India", "Maharashtra", "Mumbai", "Asia/Kolkata", 10),
    ("India", "Maharashtra", "Pune", "Asia/Kolkata", 7), ("India", "Delhi", "New Delhi", "Asia/Kolkata", 7),
    ("India", "Haryana", "Gurgaon", "Asia/Kolkata", 6), ("India", "Uttar Pradesh", "Noida", "Asia/Kolkata", 4),
    ("India", "Telangana", "Hyderabad", "Asia/Kolkata", 8), ("India", "Tamil Nadu", "Chennai", "Asia/Kolkata", 8),
    ("India", "Tamil Nadu", "Coimbatore", "Asia/Kolkata", 4), ("India", "Kerala", "Kochi", "Asia/Kolkata", 3),
    ("India", "Gujarat", "Ahmedabad", "Asia/Kolkata", 4), ("India", "West Bengal", "Kolkata", "Asia/Kolkata", 3),
    ("India", "Rajasthan", "Jaipur", "Asia/Kolkata", 2),
    ("United States", "California", "San Francisco", "America/Los_Angeles", 4),
    ("United States", "New York", "New York", "America/New_York", 3), ("United States", "Texas", "Austin", "America/Chicago", 2),
    ("United Kingdom", "England", "London", "Europe/London", 3), ("Singapore", "Singapore", "Singapore", "Asia/Singapore", 3),
    ("United Arab Emirates", "Dubai", "Dubai", "Asia/Dubai", 3), ("Germany", "Berlin", "Berlin", "Europe/Berlin", 2),
    ("Australia", "New South Wales", "Sydney", "Australia/Sydney", 2),
]

INDUSTRIES = {
    "Software & SaaS": {"subs": ["B2B SaaS", "Fintech SaaS", "HR Tech", "Martech", "AI Platform"], "model": "B2B",
                        "suffix": ["Technologies", "Labs", "Software", "Cloud", "AI"], "w": 14,
                        "tech": ["HubSpot", "Salesforce", "Intercom", "AWS", "Stripe", "Zendesk", "Slack"]},
    "IT Services": {"subs": ["Custom Software", "IT Consulting", "Managed Services", "Staff Augmentation"], "model": "B2B",
                    "suffix": ["Infotech", "Solutions", "Systems", "Technologies", "Consulting"], "w": 12,
                    "tech": ["Zoho CRM", "Jira", "Microsoft 365", "Freshdesk", "Azure"]},
    "Manufacturing": {"subs": ["Auto Components", "Industrial Machinery", "Packaging", "Textiles", "Electronics"], "model": "B2B",
                      "suffix": ["Industries", "Manufacturing Pvt Ltd", "Engineering", "Polymers", "Works"], "w": 10,
                      "tech": ["Tally ERP", "SAP Business One", "Excel", "WhatsApp Business", "Zoho Books"]},
    "Healthcare": {"subs": ["Hospitals", "Diagnostics", "Clinics", "Healthtech", "Pharma Distribution"], "model": "B2C",
                   "suffix": ["Hospitals", "Healthcare", "Diagnostics", "Clinics", "Medical"], "w": 9,
                   "tech": ["Practo", "WhatsApp Business", "Excel", "Freshdesk", "Custom HIS"]},
    "Education": {"subs": ["K-12 Schools", "EdTech", "Higher Education", "Coaching Institutes", "Skilling"], "model": "B2C",
                  "suffix": ["Academy", "Learning", "Institute", "Education", "EdTech"], "w": 8,
                  "tech": ["WhatsApp Business", "LeadSquared", "Google Workspace", "Zoom", "Excel"]},
    "Real Estate": {"subs": ["Residential Developers", "Commercial Real Estate", "Property Brokerage", "Co-working"], "model": "B2C",
                    "suffix": ["Realty", "Developers", "Properties", "Estates", "Spaces"], "w": 7,
                    "tech": ["WhatsApp Business", "Sell.Do", "Excel", "Facebook Lead Ads", "Salesforce"]},
    "Professional Services": {"subs": ["Accounting", "Legal", "Management Consulting", "Recruitment", "Architecture"], "model": "B2B",
                              "suffix": ["Advisors", "Partners", "Associates", "Consultants", "& Co"], "w": 8,
                              "tech": ["Zoho CRM", "Microsoft 365", "Excel", "Tally ERP", "Calendly"]},
    "Logistics": {"subs": ["Freight Forwarding", "Last-mile Delivery", "Warehousing", "Cold Chain"], "model": "B2B",
                  "suffix": ["Logistics", "Express", "Cargo", "Supply Chain", "Movers"], "w": 6,
                  "tech": ["SAP", "Excel", "WhatsApp Business", "Custom TMS", "Zoho CRM"]},
    "Retail & E-commerce": {"subs": ["D2C Brands", "Marketplace Sellers", "Omnichannel Retail", "Grocery"], "model": "B2C",
                            "suffix": ["Retail", "Stores", "Brands", "Mart", "Commerce"], "w": 7,
                            "tech": ["Shopify", "WooCommerce", "WhatsApp Business", "Freshdesk", "Razorpay"]},
    "Financial Services": {"subs": ["NBFC", "Insurance Brokerage", "Wealth Management", "Lending"], "model": "B2C",
                           "suffix": ["Finance", "Capital", "Fincorp", "Wealth", "Insurance Brokers"], "w": 6,
                           "tech": ["Salesforce", "LeadSquared", "Excel", "Freshdesk", "Custom LOS"]},
    "Construction": {"subs": ["Civil Contractors", "Interior Design", "Infrastructure", "Building Materials"], "model": "B2B",
                     "suffix": ["Constructions", "Builders", "Infra", "Interiors", "Projects"], "w": 5,
                     "tech": ["Excel", "Tally ERP", "WhatsApp Business", "AutoCAD", "Procore"]},
    "Marketing Agency": {"subs": ["Digital Marketing", "Performance Marketing", "Creative Agency", "PR"], "model": "B2B",
                         "suffix": ["Media", "Digital", "Creative", "Studio", "Marketing"], "w": 5,
                         "tech": ["HubSpot", "Google Ads", "Meta Ads", "Semrush", "Slack"]},
    "Hospitality": {"subs": ["Hotels", "Restaurants", "Travel Agencies", "Event Management"], "model": "B2C",
                    "suffix": ["Hospitality", "Hotels", "Travels", "Events", "Resorts"], "w": 4,
                    "tech": ["WhatsApp Business", "Excel", "Booking Engine", "Petpooja", "Zoho CRM"]},
}

PREFIXES = ["Nimbus", "Vertex", "Aarav", "Kaveri", "Zenith", "Indus", "Sahyadri", "Orbit", "Lumen", "Pragati", "Nova",
            "Tejas", "Arka", "Vayu", "Quanta", "Saffron", "Helix", "Meridian", "Sarvam", "Ananta", "Cobalt", "Prism",
            "Vistara", "Ojas", "Kinetic", "Banyan", "Lotus", "Everest", "Garuda", "Pinnacle", "Northstar", "Bluepeak",
            "Coral", "Deccan", "Ember", "Falcon", "Granite", "Horizon", "Ivory", "Jasmine", "Krishna", "Lighthouse",
            "Monsoon", "Neem", "Opal", "Peacock", "Quill", "Riverstone", "Sterling", "Tulsi", "Umang", "Vriksha",
            "Westwind", "Yamuna", "Zephyr", "Agni", "Bodhi", "Chakra", "Dhruv", "Ekam", "Firefly", "Godavari", "Hamsa"]

FIRST = ["Aarav", "Priya", "Rohan", "Ananya", "Vikram", "Sneha", "Arjun", "Kavya", "Rahul", "Meera", "Karthik", "Divya",
         "Siddharth", "Pooja", "Aditya", "Neha", "Rajesh", "Lakshmi", "Suresh", "Anjali", "Varun", "Ishita", "Manish",
         "Revathi", "Nikhil", "Shreya", "Harish", "Deepa", "Gaurav", "Swati", "Arun", "Nandini", "Amit", "Bhavna",
         "Sanjay", "Farah", "Imran", "Zoya", "Joseph", "Maria", "David", "Sarah", "Michael", "Emma", "James", "Olivia",
         "Daniel", "Sophie", "Liam", "Chloe", "Wei", "Mei", "Omar", "Layla", "Lukas", "Hannah"]
LAST = ["Sharma", "Iyer", "Reddy", "Nair", "Patel", "Gupta", "Menon", "Rao", "Kumar", "Singh", "Mehta", "Joshi", "Pillai",
        "Krishnan", "Agarwal", "Bose", "Chatterjee", "Desai", "Kapoor", "Malhotra", "Subramanian", "Verma", "Shetty",
        "Naidu", "Khan", "Fernandes", "D'Souza", "Banerjee", "Chopra", "Bhat", "Smith", "Johnson", "Brown", "Taylor",
        "Wilson", "Clarke", "Tan", "Lim", "Haddad", "Schmidt", "Fischer", "Murphy"]

# (title, normalized family, seniority, department, min_employees)
TITLES = [
    ("Founder & CEO", "Founder/CEO", "C-Level", "Executive", 0), ("Co-Founder", "Founder/CEO", "C-Level", "Executive", 0),
    ("Managing Director", "Founder/CEO", "C-Level", "Executive", 0), ("Chief Operating Officer", "COO / Operations Leadership", "C-Level", "Operations", 40),
    ("Head of Operations", "COO / Operations Leadership", "Head", "Operations", 30), ("VP Sales", "Sales Leadership", "VP", "Sales", 50),
    ("Head of Sales", "Sales Leadership", "Head", "Sales", 20), ("Sales Director", "Sales Leadership", "Director", "Sales", 60),
    ("Chief Revenue Officer", "Sales Leadership", "C-Level", "Sales", 150), ("Business Development Manager", "Sales Management", "Manager", "Sales", 10),
    ("CTO", "Technology Leadership", "C-Level", "Engineering", 20), ("Head of IT", "Technology Leadership", "Head", "IT", 60),
    ("VP Engineering", "Technology Leadership", "VP", "Engineering", 80), ("CMO", "Marketing Leadership", "C-Level", "Marketing", 80),
    ("Head of Marketing", "Marketing Leadership", "Head", "Marketing", 25), ("Head of Customer Support", "Customer Support Leadership", "Head", "Support", 60),
    ("CFO", "Finance Leadership", "C-Level", "Finance", 100), ("HR Director", "HR Leadership", "Director", "HR", 120),
    ("Admissions Director", "Operations Leadership", "Director", "Operations", 40), ("Medical Director", "Executive", "Director", "Executive", 60),
]

SIGNAL_TEMPLATES = [
    ("hiring", "Hiring {n} sales roles in the last 90 days", "careers page"),
    ("hiring", "Hiring {n} customer support executives", "careers page"),
    ("hiring", "Hiring a CRM / Salesforce administrator", "job board"),
    ("employee_growth", "Headcount grew {p}% in the last 6 months", "professional network"),
    ("funding", "Raised a {round} round", "press release"),
    ("expansion", "Opening a new branch in {city}", "news"),
    ("expansion", "Expanding into international markets", "website"),
    ("technology", "Recently adopted {tech}", "technology scan"),
    ("process", "Handles leads manually over WhatsApp and spreadsheets", "website"),
    ("process", "Multiple WhatsApp numbers listed for sales enquiries", "website"),
    ("launch", "Launched a new product line", "news"),
    ("website_change", "Pricing page updated recently", "website monitor"),
]


def _weighted(rng, items, key):
    total = sum(key(i) for i in items)
    r = rng.uniform(0, total)
    acc = 0
    for i in items:
        acc += key(i)
        if r <= acc:
            return i
    return items[-1]


@lru_cache(maxsize=1)
def dataset() -> tuple[list[dict], list[dict]]:
    rng = random.Random(20261113)
    companies, people, used = [], [], set()
    industries = list(INDUSTRIES.items())
    for i in range(720):
        ind_name, ind = _weighted(rng, industries, lambda x: x[1]["w"])
        for _ in range(20):
            name = f"{rng.choice(PREFIXES)} {rng.choice(ind['suffix'])}"
            if name not in used:
                break
        else:
            name = f"{rng.choice(PREFIXES)} {rng.choice(ind['suffix'])} {i}"
        used.add(name)
        country, state, city, tz, _ = _weighted(rng, LOCATIONS, lambda x: x[4])
        size = int(rng.choice([rng.randint(8, 50), rng.randint(20, 200), rng.randint(50, 500), rng.randint(200, 2000)]))
        slug = "".join(ch for ch in name.lower().replace("&", "and") if ch.isalnum())[:24]
        domain = f"{slug}.demo"
        rev_per_emp = rng.uniform(15_000, 60_000)
        techs = rng.sample(ind["tech"], k=min(len(ind["tech"]), rng.randint(2, 4)))
        sigs = []
        for tpl in rng.sample(SIGNAL_TEMPLATES, k=rng.randint(0, 4)):
            text = tpl[1].format(n=rng.randint(3, 25), p=rng.randint(10, 60), round=rng.choice(["Seed", "Series A", "Series B"]),
                                 city=rng.choice([l[2] for l in LOCATIONS if l[0] == country] or [city]),
                                 tech=rng.choice(ind["tech"]))
            sigs.append({"type": tpl[0], "text": text, "source": tpl[2], "observed_days_ago": rng.randint(1, 160)})
        sub = rng.choice(ind["subs"])
        cid = f"dp_c_{i:04d}"
        desc = (f"{name} is a {city}-based {sub.lower()} company in the {ind_name.lower()} sector "
                f"with roughly {size} employees, serving {'businesses' if ind['model'] == 'B2B' else 'consumers'}"
                f" across {country if country != 'India' else 'India'}.")
        companies.append({
            "provider_ref": cid, "name": name, "domain": domain, "website": f"https://{domain}",
            "description": desc, "industry": ind_name, "sub_industry": sub, "country": country, "state": state,
            "city": city, "timezone": tz, "employee_count": size, "revenue_estimate": round(size * rev_per_emp, -3),
            "founded_year": rng.randint(1995, 2023), "business_model": ind["model"],
            "linkedin_url": f"demo://linkedin/company/{slug}", "technologies": techs, "signals": sigs,
        })
        eligible = [t for t in TITLES if t[4] <= size]
        for j, t in enumerate(rng.sample(eligible, k=min(len(eligible), rng.randint(3, 6)))):
            fn, ln = rng.choice(FIRST), rng.choice(LAST)
            email = f"{fn.lower()}.{ln.lower().replace(chr(39), '')}@{domain}"
            people.append({
                "provider_ref": f"dp_p_{i:04d}_{j}", "company_ref": cid, "first_name": fn, "last_name": ln,
                "full_name": f"{fn} {ln}", "title": t[0], "normalized_title": t[1], "seniority": t[2],
                "department": t[3], "email": email if rng.random() < 0.85 else "",
                "phone": f"+00-DEMO-{i:04d}{j}" if rng.random() < 0.55 else "",
                "linkedin_url": f"demo://linkedin/in/{fn.lower()}-{ln.lower()}-{i}{j}",
                "country": country, "city": city, "timezone": tz,
            })
    return companies, people


def companies_by_ref() -> dict[str, dict]:
    return {c["provider_ref"]: c for c in dataset()[0]}
