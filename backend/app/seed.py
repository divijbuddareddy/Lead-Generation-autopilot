"""First-run bootstrap: plans, prices, providers, platform super admin, and the
internal AI Automation Labs tenant (PRD 191, 254-256)."""
import logging
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from . import models as m
from .security import hash_password
from .services import core
from .services.providers import ensure_registry

log = logging.getLogger("gtm.seed")

PLANS = [
    ("starter", "Starter", 99, 3000,
     {"email_outreach": True, "linkedin_assistant": True, "whatsapp_outreach": False, "sms_outreach": False,
      "voice_outreach": False, "phone_enrichment": False, "deep_research": False, "api_access": True, "autopilot": True},
     {"users": 2, "workspaces": 1, "api_rpm": 60}),
    ("growth", "Growth", 299, 12000,
     {"email_outreach": True, "linkedin_assistant": True, "whatsapp_outreach": True, "sms_outreach": True,
      "voice_outreach": True, "phone_enrichment": True, "deep_research": True, "api_access": True, "autopilot": True},
     {"users": 10, "workspaces": 3, "api_rpm": 300}),
    ("enterprise", "Enterprise", 0, 100000,
     {"email_outreach": True, "linkedin_assistant": True, "whatsapp_outreach": True, "sms_outreach": True,
      "voice_outreach": True, "phone_enrichment": True, "deep_research": True, "api_access": True, "autopilot": True},
     {"users": 1000, "workspaces": 100, "api_rpm": 3000}),
    ("internal", "Internal (AI Automation Labs)", 0, 1000000,
     {"email_outreach": True, "linkedin_assistant": True, "whatsapp_outreach": True, "sms_outreach": True,
      "voice_outreach": True, "phone_enrichment": True, "deep_research": True, "api_access": True, "autopilot": True},
     {"users": 1000, "workspaces": 100, "api_rpm": 3000}),
]

INTERNAL_OFFERS = [
    ("AI Process Automation", "Automating repetitive operational workflows with AI agents.",
     ["manual, repetitive back-office work", "slow response to customer enquiries"],
     ["free up 20+ team hours a week", "respond to every lead in minutes"], ["Professional Services", "Logistics", "Manufacturing"]),
    ("CRM Automation", "CRM implementation, lead routing and WhatsApp-to-CRM automation.",
     ["leads lost across WhatsApp and spreadsheets", "no visibility into sales pipeline"],
     ["every lead captured and followed up", "a clean, automated pipeline"], ["Real Estate", "Financial Services", "Software & SaaS"]),
    ("Admissions Automation", "AI-driven enquiry handling and admissions follow-up for education.",
     ["admission enquiries going cold", "counsellors overloaded"], ["faster enquiry response", "higher conversion"],
     ["Education"]),
    ("AI Appointment Automation", "AI booking and reminders for hospitals and clinics.",
     ["missed appointments and no-shows", "front-desk overload"], ["fewer no-shows", "24/7 booking"], ["Healthcare"]),
]


def run(db: Session):
    for code, name, price, gtu, ent, lim in PLANS:
        if not db.scalars(select(m.Plan).where(m.Plan.code == code)).first():
            db.add(m.Plan(code=code, name=name, price_usd=price, monthly_gtu=gtu, entitlements=ent, limits=lim))
    for meter in core.DEFAULT_PRICES:
        core.price(db, meter)
    ensure_registry(db)
    db.flush()

    created: list[tuple[str, str, str]] = []   # (role, email, one-time password) shown once on the console

    def new_user(email: str, name: str, role_label: str, env_password: str | None = None, **kw) -> m.User:
        # No password lives in code: use the operator-supplied env value, else a random one-time password.
        pw = env_password or secrets.token_urlsafe(12)
        u = m.User(email=email, name=name, password_hash=hash_password(pw),
                   must_change_password=env_password is None, **kw)
        db.add(u)
        db.flush()
        if env_password is None:
            created.append((role_label, email, pw))
        return u

    if not db.scalars(select(m.User).where(m.User.email == config.SUPERADMIN_EMAIL)).first():
        new_user(config.SUPERADMIN_EMAIL, "Platform Super Admin", "Platform Super Admin",
                 config.SUPERADMIN_PASSWORD, is_platform_admin=True)

    if not db.scalars(select(m.Organization).where(m.Organization.slug == "ai-automation-labs")).first():
        org = m.Organization(name="AI Automation Labs LLP", slug="ai-automation-labs", plan_code="internal",
                             website="", industry="IT Services", country="India", timezone="Asia/Kolkata",
                             settings={"internal_mode": True, "sender_name": "Ajay"})
        db.add(org)
        db.flush()
        ws = m.Workspace(organization_id=org.id, name="Main", timezone="Asia/Kolkata",
                         settings={"icp": {"industries": ["Professional Services", "Software & SaaS", "Manufacturing",
                                                          "Education", "Healthcare", "Real Estate"],
                                           "countries": ["India"], "employee_min": 20, "employee_max": 500,
                                           "signals": ["hiring"], "job_titles": ["Founder", "CEO", "COO", "Head of Sales"]}})
        db.add(ws)
        acc = core.credit_account(db, org.id)
        acc.monthly_allowance = 1_000_000
        core.grant(db, org.id, 1_000_000, "Internal plan allowance")
        if config.DEMO_DATA:   # fake mailboxes exist only in demo/test mode
            db.add(m.Mailbox(organization_id=org.id, address="ajay@outreach.aiautomationlabs.local",
                             provider="sandbox", daily_limit=200))
        for name, desc, problems, benefits, inds in INTERNAL_OFFERS:
            db.add(m.Offer(organization_id=org.id, workspace_id=ws.id, name=name, description=desc, problems=problems,
                           benefits=benefits, industries=inds, cta="Open to a 20-minute call next week?"))
        owner = new_user(config.OWNER_EMAIL, "Ajay (Owner)", "SaaS Owner", config.OWNER_PASSWORD)
        db.add(m.Membership(user_id=owner.id, organization_id=org.id, role="owner"))
        if config.SEED_DEMO_USERS:
            for email, name, role in [("admin@aiautomationlabs.local", "Admin User", "admin"),
                                      ("manager@aiautomationlabs.local", "GTM Manager", "gtm_manager"),
                                      ("sales@aiautomationlabs.local", "Sales Rep", "sales_user"),
                                      ("viewer@aiautomationlabs.local", "Read-only Viewer", "viewer")]:
                u = new_user(email, name, name)
                db.add(m.Membership(user_id=u.id, organization_id=org.id, role=role))
        core.audit(db, org.id, "org.bootstrap", actor_type="system", details={"plan": "internal"})
    # Demo role accounts: also created on an existing installation when demo sign-in is switched on.
    if config.SEED_DEMO_USERS or config.DEMO_LOGIN:
        org = db.scalars(select(m.Organization).where(m.Organization.slug == "ai-automation-labs")).first()
        if org:
            for email, name, role in [("admin@aiautomationlabs.local", "Admin User", "admin"),
                                      ("manager@aiautomationlabs.local", "GTM Manager", "gtm_manager"),
                                      ("sales@aiautomationlabs.local", "Sales Rep", "sales_user"),
                                      ("viewer@aiautomationlabs.local", "Read-only Viewer", "viewer")]:
                if not db.scalars(select(m.User).where(m.User.email == email)).first():
                    u = new_user(email, name, name)
                    db.add(m.Membership(user_id=u.id, organization_id=org.id, role=role))
    if not config.DEMO_DATA:
        # Remove unused fake mailboxes left from earlier versions (real leads must use Resend/SMTP).
        for b in db.scalars(select(m.Mailbox).where(m.Mailbox.provider == "sandbox", m.Mailbox.sent_total == 0)):
            db.delete(b)
    db.commit()
    if created:
        lines = ["", "=" * 78, " FIRST-RUN ACCOUNTS - one-time passwords (shown only now; change at first login)",
                 "=" * 78]
        lines += [f"  {r:<22} {e:<40} {p}" for r, e, p in created]
        lines += [" Lost one? Run:  python -m app.reset_password <email>", "=" * 78, ""]
        print("\n".join(lines), flush=True)
