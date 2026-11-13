# GTM Autopilot: Autonomous GTM Platform (Internal V1, SaaS-ready)

Built from *Autonomous GTM Platform – PRD (Internal V1 → SaaS Scale)*.

This build covers the PRD's "first business loop" (§334):

**website → AI learns business → ICP → companies → people → research → personalised outreach → reply handling → meeting booked → CRM → learning**

It also includes role-based access and multi-tenancy. Credentials are kept by admins in an encrypted vault.

## Quick start (Windows)

Double-click `run.bat`, or run these commands:

```powershell
cd backend
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --port 8000
```

Then open <http://localhost:8000>. Interactive API docs are at <http://localhost:8000/docs>.

### First-run accounts (no passwords in code)

There are no passwords in the code, the UI or this README. On first start the server creates the accounts below. Each gets a **random one-time password**, printed **once** in the server console. Each user must set their own password at first sign-in, and the server blocks every other action until they do.

| Role | Email |
|---|---|
| Platform Super Admin | superadmin@aiautomationlabs.local |
| SaaS Owner | owner@aiautomationlabs.local |
| Admin / GTM Manager / Sales User / Viewer | admin@ / manager@ / sales@ / viewer@aiautomationlabs.local |

- **Lost a one-time password?** From the `backend` folder, run `python -m app.reset_password <email>`.
- **Supply your own initial passwords** with the env vars `GTM_SUPERADMIN_PASSWORD` and `GTM_OWNER_PASSWORD`. Change the emails with `GTM_SUPERADMIN_EMAIL` and `GTM_OWNER_EMAIL`.
- **Skip the four demo role accounts** with `GTM_SEED_DEMO_USERS=0`.
- **Invited users** get a temporary password and must also change it at first login.

New companies can self-sign-up at `#/signup`; each gets its own tenant on the Starter plan.

## Role-based access (PRD §10)

The server checks permissions on every request. The UI only hides what a role can't use.

| Capability | Owner | Admin | GTM Manager | Sales User | Viewer | Platform Super Admin |
|---|:-:|:-:|:-:|:-:|:-:|:-:|
| View dashboard, leads, inbox, meetings | ✓ | ✓ | ✓ | ✓ | ✓ (read-only) | – |
| Reply, take over conversations, book meetings, notes | ✓ | ✓ | ✓ | ✓ | – | – |
| Search, research, enrich, missions, campaigns, approve AI drafts | ✓ | ✓ | ✓ | – | – | – |
| Users & roles, **integrations / API credentials**, API keys, webhooks, suppression, policy, audit | ✓ | ✓ | – | – | – | – |
| Change plan / buy credits | ✓ | – | – | – | – | – |
| Tenants, providers, pricing, platform credentials, health, jobs | – | – | – | – | – | ✓ |

- No privilege escalation. An Admin can't grant Owner, and nobody can change their own role. Every organization keeps at least one Owner.
- The Platform Super Admin can't read tenant conversations (PRD §231).
- Settings → Users & roles shows the full permission matrix.

## How API keys are held by the admin

- **Third-party credentials** are held in Integrations → Credential vault (Admin/Owner only). This covers Anthropic/OpenAI, Resend (email), SMTP (alternative), WhatsApp Cloud API, Twilio, HubSpot, Slack and data providers.
  - Values are encrypted at rest with Fernet; the master key is in `data/.gtm_master_key` or the `GTM_MASTER_KEY` env var.
  - Once saved, a value is never returned to any browser, only a mask like `••••abcd`.
  - Values are never placed in an AI prompt.
  - Changes are audit-logged.
- **Platform defaults:** the Super Admin can set shared keys under Platform Admin → Platform credentials, such as one AI key for all tenants. A tenant's own key overrides the platform key.
- **No key is ever hardcoded, and none is read from source files or environment variables.** The admin vault is the only place provider keys come from. The only server-side secrets are the vault's encryption key and the session-signing key:
  - In production, set them as `GTM_MASTER_KEY` and `GTM_JWT_SECRET`, ideally from a secret manager or KMS.
  - Locally, they are generated randomly on first run into `data/`. Keep that folder out of source control and cloud sync.
- **Public API keys:** Admins create them in Integrations → Public API keys.
  - Each key acts with a chosen role (Admin, GTM Manager, Sales User or Viewer).
  - Keys are stored as SHA-256 hashes and shown only once.
  - They are rate-limited per plan, and the Admin can revoke them.

```bash
curl -X POST http://localhost:8000/api/v1/search/companies \
  -H "X-API-Key: gtm_live_..." -H "Content-Type: application/json" \
  -d '{"query": "B2B SaaS companies in India with 50-500 employees", "limit": 20}'
```

Write endpoints accept an `Idempotency-Key` header. Errors use `{"error": {"code", "message", "request_id"}}`.

## Real data only (no demo data)

The platform runs on real data only. There are no fictional companies, fake mailboxes or simulated replies.

- **Leads:** from **LeadOcean** (`LEADOCEAN_API_KEY` in the vault), or your own CSV/XLSX import. Until one is connected, company and people search show a clear "connect LeadOcean" message.
- **Email:** sent only through a real Resend (or SMTP) mailbox on your verified domain. If none is set up, messages wait as *deferred*; they are never marked as sent.
- **WhatsApp / SMS:** sent only when their credentials are in the vault, otherwise the step fails with a clear reason.
- **Voice:** not built yet. Voice steps fail visibly rather than pretending a call happened.
- **AI:** optional. With an Anthropic or OpenAI key it writes and replies better. Without one, it uses built-in rules.
- **Timing:** sequences run in real days. Developers can set `GTM_DEMO_DATA=1` and `GTM_TIME_COMPRESSION=1` for automated tests only.

## Email via Resend (recommended)

Outbound email goes through [Resend](https://resend.com). SMTP remains available as an alternative. Integrations → **Email (Resend)** shows a live setup checklist:

1. **API key:** paste it into the Credential vault as `RESEND_API_KEY` (admins only, encrypted).
2. **Verified domain:** add your sending domain in Resend and set the SPF/DKIM DNS records. The checklist shows each domain's verification status.
3. **Sending mailbox:** add an address on a verified domain with provider `resend`. The app refuses unverified or unknown domains. New mailboxes start in WARMING with a conservative daily cap.
4. **Webhook:** in Resend → Webhooks, add `https://<your-server>/webhooks/resend/<org-id>` (the exact URL is shown in the app). Subscribe to `email.sent`, `email.delivered`, `email.bounced`, `email.complained` and `email.received`. Paste the signing secret into the vault as `RESEND_WEBHOOK_SECRET`.
5. **Replies:** enable receiving in Resend (your domain, or the free `<id>.resend.app` address). Set it as `RESEND_REPLY_TO` so prospect replies flow back to the AI reply agent.
6. **Test:** click **Send test email**.

**How it behaves**
- **No duplicate sends.** Every message's idempotency key is passed to Resend as the `Idempotency-Key` header. A retry after a timeout or crash reuses the same message and key, so a prospect can never get the same email twice.
- **Retries.** Network errors, 429s and 5xx responses are temporary: they are retried with backoff, up to 5 attempts. A 4xx response (bad address, unverified domain) is permanent and stops that lead.
- **Webhook security.** Every webhook is verified with Resend's Svix signature (HMAC-SHA256 with a 5-minute replay window) before anything happens. Each event is stored raw and de-duplicated before processing.
- **What each event does:**
  - `delivered` marks the message delivered.
  - `bounced` marks the email invalid, suppresses it and lowers mailbox health.
  - `complained` suppresses the contact on all channels and alerts admins.
  - `received` fetches the reply body from Resend's Received Emails API, strips quoted history, and hands it to the reply agent (intent, auto-reply, booking, STOP handling).
- **Mailbox health.** A mailbox that degrades from bounces or complaints automatically stops getting new work. Real leads wait for a healthy mailbox and are never "fake-sent" through the sandbox.
- **Demo records stay inside.** Messages to demo (`.demo`) records are never sent to Resend.

The webhook needs a public URL. For local testing, use a tunnel (for example `cloudflared tunnel --url http://localhost:8000`) and register that URL in Resend.

## A 5-minute tour

1. Sign in as **Owner** (using the one-time password from the console), set your password, add your LeadOcean key, then open **Find Companies** and search "Indian SaaS companies with 20-200 employees hiring salespeople". Then refine: "Exclude companies in Mumbai", then "Only show founders and CEOs".
2. Open **GTM Missions → New mission** and paste a natural-language instruction. Preview the plan, then create it with "Build audiences now".
3. Open a campaign and click **Review before launch** to see counts, sample messages, estimated spend and risks. Then click **Start campaign** and **Run scheduler now**.
4. Open **Inbox**. Sandbox replies arrive within a minute, or use **Simulate prospect reply**. Watch intent classification, auto-replies, slot proposals, booking, STOP suppression and escalation.
5. Check **AI Command Center** for the decision trace, **Meetings**, **Analytics** and **Usage & Billing**.
6. As Owner, invite users or sign in as the seeded Sales User or Viewer to see role restrictions. Sign in as **Super Admin** for the platform console.

## Architecture

```
backend/app
  main.py            FastAPI app, request IDs, standard error format, static UI
  models.py          Multi-tenant schema (organization_id on every business record)
  rbac.py / deps.py  Roles, permissions, JWT + API-key auth, tenant isolation, rate limits
  security.py        bcrypt, JWT, Fernet credential encryption, API-key hashing
  services/core.py   Audit log, credential vault, GTU credits & metering, events/webhooks, AI decisions
  services/llm.py    Vendor-neutral LLM gateway (Anthropic/OpenAI), prompt versions, injection guard
  services/providers.py  Provider interfaces, router with priority/failover/health, website crawler
  services/intelligence.py  Scoring + explanations, provenance, research, ICP, missions, messaging
  services/engine.py Compliance checks, channel layer, scheduler, reply agent, booking, CRM, optimizer
  worker.py          Durable DB-backed job queue + campaign scheduler + webhook delivery
frontend/            Single-page app (no build step)
```

**Storage.** The default database is SQLite at `data/gtm.db`. For production, set `DATABASE_URL` to PostgreSQL and install `psycopg[binary]`. The modules map one-to-one to the PRD's modular monolith (§202), so the worker can be swapped for Temporal or Redis later (§200–201).

### Key PRD rules enforced in code
- Every outbound message goes through `engine.deliver`, which checks, in order:
  1. suppression (organization, workspace and campaign level; per channel)
  2. plan entitlement
  3. email verification status
  4. WhatsApp/SMS consent
  5. jurisdiction policy
  6. re-contact window
  7. campaign budget and credits
  8. mailbox health and capacity
- **Idempotency:** each message records its idempotency key *before* the provider call, so a retry can't send twice.
- **STOP:** an opt-out reply suppresses the contact immediately and stops all their sequences.
- **Budgets:** AI agents can't raise hard budgets. Budget shifts above the autonomous limit need human approval.
- **Audit:** every autonomous action writes an `ai_decisions` row with its reason, confidence and policy result.
- **Untrusted content:** website, email and CSV content is passed to the AI only as delimited untrusted data. CSV formula injection is neutralised.

### Not yet built (later phases in the PRD)
Real Google/Microsoft calendar OAuth, Salesforce/Zoho/Pipedrive sync (HubSpot is implemented), real voice telephony, payment-provider checkout, a ClickHouse analytics store and Temporal workflows. The extension points for each are in place: the channel and provider interfaces, the CRM sync job and the billing endpoints.
