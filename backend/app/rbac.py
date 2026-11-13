"""Role-based access control (PRD section 10, 143, 217, 309-310).

Roles are per organization. The Platform Super Admin is a platform flag on the
user and is deliberately NOT granted tenant conversation content (PRD 231).
All enforcement happens server-side; the frontend only hides what it can't use.
"""
ROLES = ["owner", "admin", "gtm_manager", "sales_user", "viewer"]

ROLE_LABELS = {
    "owner": "SaaS Owner",
    "admin": "Admin",
    "gtm_manager": "GTM Manager",
    "sales_user": "Sales User",
    "viewer": "Viewer",
    "platform_admin": "Platform Super Admin",
}

# Permission catalogue
P = {
    "dashboard.view": "View dashboard, command center and analytics",
    "leads.view": "View companies, people and leads",
    "conversations.view": "View inbox conversations",
    "conversations.reply": "Reply in conversations",
    "conversations.takeover": "Take over / return conversations to AI",
    "meetings.view": "View meetings",
    "meetings.book": "Book and manage meetings",
    "notes.add": "Add notes and lead feedback",
    "search.run": "Run company / people search (consumes credits)",
    "research.run": "Run AI research (consumes credits)",
    "enrich.run": "Run enrichment (consumes credits)",
    "missions.manage": "Create and edit GTM missions",
    "campaigns.manage": "Create, start, pause campaigns",
    "agents.monitor": "Monitor agents, AI decisions and jobs",
    "exceptions.approve": "Approve AI exceptions / human review drafts",
    "command.run": "Use the AI command bar",
    "data.import": "Import leads (CSV/XLSX)",
    "data.export": "Export leads",
    "data.delete": "Delete leads and data",
    "lists.manage": "Manage lists and saved searches",
    "suppression.manage": "Manage suppression lists",
    "offers.manage": "Manage offers and proof library",
    "workspace.manage": "Manage workspace settings, scoring and autonomy policy",
    "users.manage": "Invite users and change roles",
    "integrations.manage": "Manage integrations and API credentials (secrets)",
    "apikeys.manage": "Create / revoke public API keys",
    "webhooks.manage": "Manage webhooks",
    "billing.view": "View usage and billing",
    "billing.manage": "Change plan / buy credits",
    "autonomy.configure": "Configure autopilot and limits",
    "audit.view": "View audit log",
}

_viewer = {"dashboard.view", "leads.view", "conversations.view", "meetings.view", "billing.view"}
_sales = _viewer | {"conversations.reply", "conversations.takeover", "meetings.book", "notes.add"}
_gtm = _sales | {"search.run", "research.run", "enrich.run", "missions.manage", "campaigns.manage",
                 "agents.monitor", "exceptions.approve", "command.run", "data.import", "lists.manage",
                 "offers.manage", "data.export"}
_admin = _gtm | {"data.delete", "suppression.manage", "workspace.manage", "users.manage",
                 "integrations.manage", "apikeys.manage", "webhooks.manage", "audit.view",
                 "autonomy.configure"}
_owner = _admin | {"billing.manage"}

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "viewer": _viewer,
    "sales_user": _sales,
    "gtm_manager": _gtm,
    "admin": _admin,
    "owner": _owner,
}

# Roles an actor may assign (no privilege escalation above yourself).
ASSIGNABLE = {
    "owner": set(ROLES),
    "admin": {"admin", "gtm_manager", "sales_user", "viewer"},
}

# Platform super admin capabilities (separate from tenant permissions).
PLATFORM_PERMISSIONS = {"platform.tenants", "platform.providers", "platform.pricing", "platform.credits",
                        "platform.jobs", "platform.health", "platform.secrets", "platform.audit"}

# Agent tool permissions (PRD 217): what each autonomous agent may do.
AGENT_TOOLS = {
    "research": {"read": ["company", "people", "web"], "write": ["research", "scores"]},
    "prospecting": {"read": ["providers"], "write": ["companies", "people"]},
    "messaging": {"read": ["company", "person", "offer", "proof"], "write": ["drafts"]},
    "outreach": {"read": ["campaign"], "write": ["send_message"]},
    "reply": {"read": ["conversation"], "write": ["send_message", "suppress", "escalate"]},
    "booking": {"read": ["calendar"], "write": ["meetings", "crm"]},
    "optimization": {"read": ["analytics"], "write": ["campaign_status", "campaign_budget"]},
    "compliance": {"read": ["*"], "write": ["block"]},
}


def permissions_for(role: str) -> set[str]:
    return ROLE_PERMISSIONS.get(role, set())


def can_assign(actor_role: str, target_role: str) -> bool:
    return target_role in ASSIGNABLE.get(actor_role, set())
