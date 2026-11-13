"""Canonical data model (PRD sections 11, 22, 41, 51, 130-136).

Every business record carries organization_id; operational records also carry
workspace_id. All tenant queries go through services.tenancy helpers.
"""
import secrets
from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text,
                        UniqueConstraint, Index)
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id(prefix: str):
    return lambda: f"{prefix}_{secrets.token_hex(6)}"


def _id(prefix):
    return mapped_column(String(32), primary_key=True, default=new_id(prefix))


def _org():
    return mapped_column(String(32), ForeignKey("organizations.id", ondelete="CASCADE"), index=True)


def _ws():
    return mapped_column(String(32), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=True)


class Plan(Base):
    __tablename__ = "plans"
    id: Mapped[str] = _id("pln")
    code: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    price_usd: Mapped[float] = mapped_column(Float, default=0)
    monthly_gtu: Mapped[int] = mapped_column(Integer, default=0)
    entitlements: Mapped[dict] = mapped_column(JSON, default=dict)  # feature -> bool
    limits: Mapped[dict] = mapped_column(JSON, default=dict)        # users, workspaces, api_rpm...


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[str] = _id("org")
    name: Mapped[str] = mapped_column(String(160))
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    plan_code: Mapped[str] = mapped_column(String(40), default="starter")
    status: Mapped[str] = mapped_column(String(20), default="active")
    website: Mapped[str] = mapped_column(String(255), default="")
    industry: Mapped[str] = mapped_column(String(120), default="")
    country: Mapped[str] = mapped_column(String(80), default="")
    timezone: Mapped[str] = mapped_column(String(60), default="Asia/Kolkata")
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    profile: Mapped[dict] = mapped_column(JSON, default=dict)  # AI business understanding
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[str] = _id("wsp")
    organization_id: Mapped[str] = _org()
    name: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(60), default="Asia/Kolkata")
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = _id("usr")
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(200))
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "organization_id"),)
    id: Mapped[str] = _id("mem")
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    organization_id: Mapped[str] = _org()
    role: Mapped[str] = mapped_column(String(30))  # owner|admin|gtm_manager|sales_user|viewer
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CreditAccount(Base):
    __tablename__ = "credit_accounts"
    organization_id: Mapped[str] = mapped_column(String(32), ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True)
    balance: Mapped[float] = mapped_column(Float, default=0)
    monthly_allowance: Mapped[float] = mapped_column(Float, default=0)
    hard_limit_usd: Mapped[float] = mapped_column(Float, default=0)  # 0 = plan default
    period_start: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CreditTransaction(Base):
    __tablename__ = "credit_transactions"
    id: Mapped[str] = _id("ctx")
    organization_id: Mapped[str] = _org()
    amount: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(String(200))
    meter_type: Mapped[str] = mapped_column(String(60), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MeterPrice(Base):
    __tablename__ = "meter_prices"
    meter_type: Mapped[str] = mapped_column(String(60), primary_key=True)
    gtu_cost: Mapped[float] = mapped_column(Float)
    provider_cost_usd: Mapped[float] = mapped_column(Float, default=0)
    category: Mapped[str] = mapped_column(String(30), default="data")


class UsageEvent(Base):
    __tablename__ = "usage_events"
    id: Mapped[str] = _id("use")
    organization_id: Mapped[str] = _org()
    workspace_id: Mapped[str | None] = _ws()
    meter_type: Mapped[str] = mapped_column(String(60), index=True)
    category: Mapped[str] = mapped_column(String(30), default="data")
    quantity: Mapped[float] = mapped_column(Float, default=1)
    provider_cost: Mapped[float] = mapped_column(Float, default=0)
    customer_cost: Mapped[float] = mapped_column(Float, default=0)  # GTU
    ref: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Secret(Base):
    """Admin-held credentials. Value is Fernet-encrypted; only last4 is ever returned."""
    __tablename__ = "secrets"
    __table_args__ = (UniqueConstraint("scope", "organization_id", "key_name"),)
    id: Mapped[str] = _id("sec")
    scope: Mapped[str] = mapped_column(String(20))  # platform | org
    organization_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True)
    key_name: Mapped[str] = mapped_column(String(80))
    ciphertext: Mapped[str] = mapped_column(Text)
    last4: Mapped[str] = mapped_column(String(8), default="")
    updated_by: Mapped[str] = mapped_column(String(32), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = _id("key")
    organization_id: Mapped[str] = _org()
    workspace_id: Mapped[str | None] = _ws()
    name: Mapped[str] = mapped_column(String(120))
    prefix: Mapped[str] = mapped_column(String(20), index=True)
    key_hash: Mapped[str] = mapped_column(String(128), unique=True)
    role: Mapped[str] = mapped_column(String(30), default="gtm_manager")  # permissions the key acts with
    created_by: Mapped[str] = mapped_column(String(32))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = _id("aud")
    organization_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    actor_id: Mapped[str] = mapped_column(String(32), default="")
    actor_type: Mapped[str] = mapped_column(String(20), default="user")  # user|api_key|agent|system
    action: Mapped[str] = mapped_column(String(80), index=True)
    entity_type: Mapped[str] = mapped_column(String(40), default="")
    entity_id: Mapped[str] = mapped_column(String(40), default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    ip: Mapped[str] = mapped_column(String(64), default="")
    request_id: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Provider(Base):
    """Platform provider registry (PRD 23-24, 165)."""
    __tablename__ = "providers"
    id: Mapped[str] = mapped_column(String(60), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(40), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    cost_usd: Mapped[float] = mapped_column(Float, default=0)
    region: Mapped[str] = mapped_column(String(40), default="global")
    calls: Mapped[int] = mapped_column(Integer, default=0)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    avg_latency_ms: Mapped[float] = mapped_column(Float, default=0)
    requires_secret: Mapped[str] = mapped_column(String(80), default="")
    license: Mapped[dict] = mapped_column(JSON, default=dict)  # storage/export/retention rules
    config: Mapped[dict] = mapped_column(JSON, default=dict)   # e.g. endpoint URL (no secrets)
    description: Mapped[str] = mapped_column(Text, default="")


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (Index("ix_company_ws_domain", "workspace_id", "domain"),)
    id: Mapped[str] = _id("cmp")
    organization_id: Mapped[str] = _org()
    workspace_id: Mapped[str | None] = _ws()
    name: Mapped[str] = mapped_column(String(200))
    domain: Mapped[str] = mapped_column(String(200), default="")
    website: Mapped[str] = mapped_column(String(255), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    industry: Mapped[str] = mapped_column(String(120), default="")
    sub_industry: Mapped[str] = mapped_column(String(120), default="")
    country: Mapped[str] = mapped_column(String(80), default="")
    state: Mapped[str] = mapped_column(String(80), default="")
    city: Mapped[str] = mapped_column(String(80), default="")
    employee_count: Mapped[int] = mapped_column(Integer, default=0)
    revenue_estimate: Mapped[float] = mapped_column(Float, default=0)
    founded_year: Mapped[int] = mapped_column(Integer, default=0)
    business_model: Mapped[str] = mapped_column(String(40), default="")
    linkedin_url: Mapped[str] = mapped_column(String(255), default="")
    technologies: Mapped[list] = mapped_column(JSON, default=list)
    signals: Mapped[list] = mapped_column(JSON, default=list)
    scores: Mapped[dict] = mapped_column(JSON, default=dict)
    score_explanation: Mapped[dict] = mapped_column(JSON, default=dict)
    custom_criteria: Mapped[dict] = mapped_column(JSON, default=dict)
    research: Mapped[dict] = mapped_column(JSON, default=dict)
    data_confidence: Mapped[float] = mapped_column(Float, default=0.7)
    provider_ref: Mapped[str] = mapped_column(String(80), default="")
    source: Mapped[str] = mapped_column(String(60), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class FieldObservation(Base):
    """Provenance for every enriched field (PRD 26, 132)."""
    __tablename__ = "field_observations"
    id: Mapped[str] = _id("obs")
    organization_id: Mapped[str] = _org()
    entity_type: Mapped[str] = mapped_column(String(20))
    entity_id: Mapped[str] = mapped_column(String(32), index=True)
    field_name: Mapped[str] = mapped_column(String(60))
    field_value: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(60))
    source_url: Mapped[str] = mapped_column(String(255), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.7)
    license: Mapped[str] = mapped_column(String(60), default="")
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Person(Base):
    __tablename__ = "people"
    __table_args__ = (Index("ix_person_ws_email", "workspace_id", "email"),)
    id: Mapped[str] = _id("per")
    organization_id: Mapped[str] = _org()
    workspace_id: Mapped[str | None] = _ws()
    company_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("companies.id", ondelete="SET NULL"), nullable=True, index=True)
    first_name: Mapped[str] = mapped_column(String(80), default="")
    last_name: Mapped[str] = mapped_column(String(80), default="")
    full_name: Mapped[str] = mapped_column(String(160), default="")
    title: Mapped[str] = mapped_column(String(160), default="")
    normalized_title: Mapped[str] = mapped_column(String(80), default="")
    seniority: Mapped[str] = mapped_column(String(40), default="")
    department: Mapped[str] = mapped_column(String(60), default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    email_status: Mapped[str] = mapped_column(String(20), default="UNKNOWN")
    phone: Mapped[str] = mapped_column(String(40), default="")
    phone_status: Mapped[str] = mapped_column(String(20), default="UNKNOWN")
    linkedin_url: Mapped[str] = mapped_column(String(255), default="")
    country: Mapped[str] = mapped_column(String(80), default="")
    city: Mapped[str] = mapped_column(String(80), default="")
    timezone: Mapped[str] = mapped_column(String(60), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.7)
    stage: Mapped[str] = mapped_column(String(30), default="New")  # PRD 181 pipeline
    temperature: Mapped[str] = mapped_column(String(10), default="cold")
    icp_score: Mapped[int] = mapped_column(Integer, default=0)
    intent_score: Mapped[int] = mapped_column(Integer, default=0)
    owner_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    feedback: Mapped[str] = mapped_column(String(10), default="")
    source: Mapped[str] = mapped_column(String(60), default="")
    provider_ref: Mapped[str] = mapped_column(String(80), default="")
    custom_fields: Mapped[dict] = mapped_column(JSON, default=dict)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Note(Base):
    __tablename__ = "notes"
    id: Mapped[str] = _id("not")
    organization_id: Mapped[str] = _org()
    person_id: Mapped[str] = mapped_column(String(32), ForeignKey("people.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(String(32))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LeadList(Base):
    __tablename__ = "lists"
    id: Mapped[str] = _id("lst")
    organization_id: Mapped[str] = _org()
    workspace_id: Mapped[str | None] = _ws()
    name: Mapped[str] = mapped_column(String(160))
    type: Mapped[str] = mapped_column(String(30), default="static")  # static|saved_search|imported|campaign_audience
    definition: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ListMember(Base):
    __tablename__ = "list_members"
    list_id: Mapped[str] = mapped_column(String(32), ForeignKey("lists.id", ondelete="CASCADE"), primary_key=True)
    person_id: Mapped[str] = mapped_column(String(32), ForeignKey("people.id", ondelete="CASCADE"), primary_key=True)


class SuppressionEntry(Base):
    __tablename__ = "suppression_entries"
    id: Mapped[str] = _id("sup")
    organization_id: Mapped[str] = _org()
    workspace_id: Mapped[str | None] = _ws()  # null = organization-wide
    campaign_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    kind: Mapped[str] = mapped_column(String(20))  # email|phone|domain|person|linkedin
    value: Mapped[str] = mapped_column(String(200), index=True)
    channel: Mapped[str] = mapped_column(String(20), default="all")
    reason: Mapped[str] = mapped_column(String(200), default="")
    created_by: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Offer(Base):
    __tablename__ = "offers"
    id: Mapped[str] = _id("off")
    organization_id: Mapped[str] = _org()
    workspace_id: Mapped[str | None] = _ws()
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    problems: Mapped[list] = mapped_column(JSON, default=list)
    benefits: Mapped[list] = mapped_column(JSON, default=list)
    cta: Mapped[str] = mapped_column(String(200), default="Open to a 20-minute call?")
    pricing: Mapped[str] = mapped_column(String(200), default="")
    industries: Mapped[list] = mapped_column(JSON, default=list)  # offer routing (PRD 261)


class ProofItem(Base):
    """Claim governance (PRD 259-260): agents may only cite approved proof."""
    __tablename__ = "proof_items"
    id: Mapped[str] = _id("prf")
    organization_id: Mapped[str] = _org()
    claim: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(200), default="")
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Mission(Base):
    __tablename__ = "missions"
    id: Mapped[str] = _id("msn")
    organization_id: Mapped[str] = _org()
    workspace_id: Mapped[str | None] = _ws()
    name: Mapped[str] = mapped_column(String(160))
    objective: Mapped[str] = mapped_column(Text, default="")
    instruction: Mapped[str] = mapped_column(Text, default="")
    offer_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    icp: Mapped[dict] = mapped_column(JSON, default=dict)
    personas: Mapped[list] = mapped_column(JSON, default=list)
    signals: Mapped[list] = mapped_column(JSON, default=list)
    channels: Mapped[list] = mapped_column(JSON, default=list)
    budget_monthly: Mapped[float] = mapped_column(Float, default=0)  # GTU
    meeting_goal: Mapped[int] = mapped_column(Integer, default=10)
    autonomy_mode: Mapped[str] = mapped_column(String(30), default="full_autonomous")
    policy: Mapped[dict] = mapped_column(JSON, default=dict)
    memory: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_by: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[str] = _id("cmpn")
    organization_id: Mapped[str] = _org()
    workspace_id: Mapped[str | None] = _ws()
    mission_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("missions.id", ondelete="SET NULL"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    objective: Mapped[str] = mapped_column(String(200), default="Book qualified meetings")
    offer_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    audience: Mapped[dict] = mapped_column(JSON, default=dict)
    target_roles: Mapped[list] = mapped_column(JSON, default=list)
    negative_criteria: Mapped[list] = mapped_column(JSON, default=list)
    channels: Mapped[list] = mapped_column(JSON, default=list)
    messaging_strategy: Mapped[dict] = mapped_column(JSON, default=dict)
    sequence: Mapped[list] = mapped_column(JSON, default=list)
    language: Mapped[str] = mapped_column(String(20), default="en")
    budget_daily: Mapped[float] = mapped_column(Float, default=0)
    budget_total: Mapped[float] = mapped_column(Float, default=0)
    spent: Mapped[float] = mapped_column(Float, default=0)
    daily_send_limit: Mapped[int] = mapped_column(Integer, default=50)
    autonomy_mode: Mapped[str] = mapped_column(String(30), default="full_autonomous")
    status: Mapped[str] = mapped_column(String(30), default="DRAFT")
    agent_state: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CampaignLead(Base):
    __tablename__ = "campaign_leads"
    __table_args__ = (UniqueConstraint("campaign_id", "person_id"),)
    id: Mapped[str] = _id("cld")
    organization_id: Mapped[str] = _org()
    campaign_id: Mapped[str] = mapped_column(String(32), ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    person_id: Mapped[str] = mapped_column(String(32), ForeignKey("people.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|active|replied|stopped|completed|blocked
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    stop_reason: Mapped[str] = mapped_column(String(80), default="")
    variant: Mapped[str] = mapped_column(String(4), default="A")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Mailbox(Base):
    __tablename__ = "mailboxes"
    id: Mapped[str] = _id("mbx")
    organization_id: Mapped[str] = _org()
    address: Mapped[str] = mapped_column(String(200))
    provider: Mapped[str] = mapped_column(String(40), default="sandbox")  # sandbox|smtp
    status: Mapped[str] = mapped_column(String(20), default="HEALTHY")
    health_score: Mapped[float] = mapped_column(Float, default=100)
    daily_limit: Mapped[int] = mapped_column(Integer, default=50)
    sent_today: Mapped[int] = mapped_column(Integer, default=0)
    sent_total: Mapped[int] = mapped_column(Integer, default=0)
    bounces: Mapped[int] = mapped_column(Integer, default=0)
    complaints: Mapped[int] = mapped_column(Integer, default=0)
    replies: Mapped[int] = mapped_column(Integer, default=0)
    counter_date: Mapped[str] = mapped_column(String(10), default="")


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = _id("cnv")
    organization_id: Mapped[str] = _org()
    workspace_id: Mapped[str | None] = _ws()
    person_id: Mapped[str] = mapped_column(String(32), ForeignKey("people.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    campaign_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    channel: Mapped[str] = mapped_column(String(20), default="email")
    status: Mapped[str] = mapped_column(String(20), default="open")
    intent: Mapped[str] = mapped_column(String(30), default="")
    sentiment: Mapped[str] = mapped_column(String(20), default="neutral")
    urgency: Mapped[str] = mapped_column(String(20), default="normal")
    temperature: Mapped[str] = mapped_column(String(10), default="cold")
    lead_score: Mapped[int] = mapped_column(Integer, default=0)
    ai_mode: Mapped[str] = mapped_column(String(10), default="ai")  # ai|human
    assigned_to: Mapped[str | None] = mapped_column(String(32), nullable=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    review_reason: Mapped[str] = mapped_column(String(200), default="")
    draft_reply: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    next_action: Mapped[str] = mapped_column(String(200), default="")
    memory: Mapped[dict] = mapped_column(JSON, default=dict)
    last_message_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = _id("msg")
    organization_id: Mapped[str] = _org()
    conversation_id: Mapped[str] = mapped_column(String(32), ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    campaign_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    channel: Mapped[str] = mapped_column(String(20), default="email")
    direction: Mapped[str] = mapped_column(String(10))  # outbound|inbound
    sender: Mapped[str] = mapped_column(String(200), default="")
    recipient: Mapped[str] = mapped_column(String(200), default="")
    subject: Mapped[str] = mapped_column(String(255), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued|sent|delivered|failed|blocked|received
    error: Mapped[str] = mapped_column(String(255), default="")
    provider_message_id: Mapped[str] = mapped_column(String(120), default="")
    idempotency_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    step: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    ai_generated: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_model: Mapped[str] = mapped_column(String(60), default="")
    ai_confidence: Mapped[float] = mapped_column(Float, default=0)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Meeting(Base):
    __tablename__ = "meetings"
    id: Mapped[str] = _id("mtg")
    organization_id: Mapped[str] = _org()
    workspace_id: Mapped[str | None] = _ws()
    person_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    campaign_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mission_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    title: Mapped[str] = mapped_column(String(200))
    start_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime)
    timezone: Mapped[str] = mapped_column(String(60), default="Asia/Kolkata")
    status: Mapped[str] = mapped_column(String(20), default="booked")  # proposed|booked|completed|no_show|cancelled
    location: Mapped[str] = mapped_column(String(255), default="Video call")
    booked_by: Mapped[str] = mapped_column(String(32), default="agent")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LinkedInTask(Base):
    __tablename__ = "linkedin_tasks"
    id: Mapped[str] = _id("lit")
    organization_id: Mapped[str] = _org()
    workspace_id: Mapped[str | None] = _ws()
    person_id: Mapped[str] = mapped_column(String(32), ForeignKey("people.id", ondelete="CASCADE"))
    campaign_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action: Mapped[str] = mapped_column(String(40), default="connect")
    reason: Mapped[str] = mapped_column(String(255), default="")
    suggested_message: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="open")  # open|completed|skipped
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AIDecision(Base):
    __tablename__ = "ai_decisions"
    id: Mapped[str] = _id("dec")
    organization_id: Mapped[str] = _org()
    agent: Mapped[str] = mapped_column(String(40))
    entity_type: Mapped[str] = mapped_column(String(30), default="")
    entity_id: Mapped[str] = mapped_column(String(32), default="")
    action: Mapped[str] = mapped_column(String(80))
    reason: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0)
    policy_result: Mapped[str] = mapped_column(String(20), default="ALLOW")
    decision: Mapped[str] = mapped_column(String(20), default="executed")  # executed|blocked|needs_approval|proposed
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    model: Mapped[str] = mapped_column(String(60), default="rules")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[str] = _id("run")
    organization_id: Mapped[str] = _org()
    agent: Mapped[str] = mapped_column(String(40))
    mission_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tools_used: Mapped[list] = mapped_column(JSON, default=list)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0)
    result: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = _id("job")
    organization_id: Mapped[str] = _org()
    workspace_id: Mapped[str | None] = _ws()
    type: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="QUEUED")
    priority: Mapped[int] = mapped_column(Integer, default=4)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str] = mapped_column(String(32), default="")
    request_id: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class WebhookEndpoint(Base):
    __tablename__ = "webhook_endpoints"
    id: Mapped[str] = _id("whk")
    organization_id: Mapped[str] = _org()
    url: Mapped[str] = mapped_column(String(500))
    events: Mapped[list] = mapped_column(JSON, default=list)
    secret_enc: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    id: Mapped[str] = _id("whd")
    organization_id: Mapped[str] = _org()
    endpoint_id: Mapped[str] = mapped_column(String(32), ForeignKey("webhook_endpoints.id", ondelete="CASCADE"))
    event: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    response_code: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DomainEvent(Base):
    """Internal event log / analytics stream (PRD 125, 206)."""
    __tablename__ = "domain_events"
    id: Mapped[str] = _id("evt")
    organization_id: Mapped[str] = _org()
    workspace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    type: Mapped[str] = mapped_column(String(60), index=True)
    entity_id: Mapped[str] = mapped_column(String(32), default="")
    campaign_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[str] = _id("ntf")
    organization_id: Mapped[str] = _org()
    kind: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    link: Mapped[str] = mapped_column(String(200), default="")
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(32))
    endpoint: Mapped[str] = mapped_column(String(200))
    status_code: Mapped[int] = mapped_column(Integer, default=200)
    response: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CrmRecord(Base):
    """Internal CRM mirror + external sync status (PRD 82-83)."""
    __tablename__ = "crm_records"
    id: Mapped[str] = _id("crm")
    organization_id: Mapped[str] = _org()
    person_id: Mapped[str] = mapped_column(String(32), index=True)
    deal_stage: Mapped[str] = mapped_column(String(40), default="Hot lead")
    external_system: Mapped[str] = mapped_column(String(30), default="internal")
    external_id: Mapped[str] = mapped_column(String(80), default="")
    sync_status: Mapped[str] = mapped_column(String(20), default="synced")
    last_error: Mapped[str] = mapped_column(String(255), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
