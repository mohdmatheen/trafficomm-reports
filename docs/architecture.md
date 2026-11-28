# Architecture

```
Browser (Jinja pages + app.js / support.js, strict CSP)
   │  Zoho SSO session, X-Requested-With CSRF header on /api writes
   ▼
FastAPI web (app/web.py, app/support/web.py) ──► Postgres (SQLAlchemy 2, additive migrations in init_db)
   │                                                ▲
   ▼ enqueue                                         │ claim (FOR UPDATE SKIP LOCKED)
Worker subprocess (app/jobs.py) ─ pipeline / event_pipeline ─► app/engine/* ─► storage (Render disk /data)
   │  every 30 s: support.engineering.maybe_tick() → dispatch, GitHub status sync, e-mails
   ▼
External: Anthropic API (hybrid AI, Support Agent), Claude Code scheduled-task trigger, GitHub REST/GraphQL, Zoho SMTP
```

## Reports
- A **task** has versions. Each version is a full run: inspect → map → calculate → generate → validate.
- `app/engine/rules.py` holds agency-wide rules; `app/profiles.py` holds per-client profiles; event
  reports (`app/engine/event_report.py`) are recipes matched by `client_match`.
- Hybrid AI (`app/engine/ai.py`): built-in rules first; Claude for a client's first report or a new
  structure; the reading is saved per structure and reused on repeats.
- Validation results live in `TaskVersion.result["validation"]`. Blocking failures (`validation_blocked`,
  `sheet_mismatch`) open an AI Support issue for the task owner.

## AI Support (`app/support/`)
| Module | Role |
|---|---|
| `models.py` | SupportIssue, SupportMessage, SupportAttachment, SupportEvent (audit), EngineeringRun, Notification, ClientRule |
| `service.py` | create / dedup / access / attachments / resolve / reopen / validation hook |
| `agent.py` | classification, local diagnostics, readiness, Support Agent (rules, then gated Claude) |
| `engineering.py` | Engineering Agent bridge: payload, fire, retries, GitHub sync, approval, deploy check, rollback, callback |
| `knowledge.py` | Client Intelligence: versioned rules, approval, history, lookup, seed from code |
| `controls.py` | budgets, limits, concurrency, emergency stop, Claude/engineering gates |
| `notify.py` | portal notifications + optional Zoho e-mail |
| `sanitize.py` | redaction of identifiers and secrets |
| `fab_pilot.py` | FAB iOS All Events discrepancy pilot (required uploads and questions) |
| `dashboard.py` | admin control-centre data |

Issue statuses: open → awaiting_employee ↔ investigating → engineering_queued → engineering_running →
fix_proposed → awaiting_approval → awaiting_deploy → awaiting_validation → resolved (or reopened / unresolved).

See `docs/ai-support.md` for configuration and policy.
