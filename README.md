# Trafficomm Reporting Workspace

Internal web app for Trafficomm employees. You upload raw ad-platform exports, describe the report you need, and get back validated Excel and PowerPoint deliverables. You then refine them through feedback, and every round is kept as its own version. It is built for Around The Clock Media (ATC) first, and client-specific rules live in editable **client profiles** so more clients can be added.

> Status: a runnable private development build. It has not been deployed anywhere, and no money has been spent.
> No `ANTHROPIC_API_KEY` was available, so AI behaviour runs in the clearly labelled **"AI mocked (rule-based)"** mode. All figures are calculated by the deterministic engine in both modes.

## Quick start (local)

Requirements:
- Python 3.12 or newer
- PostgreSQL 14 or newer
- LibreOffice (`soffice`) and `pdftoppm` (poppler-utils), used for recalculation, PDF export and previews

```bash
pip install -r requirements.txt
createdb trafficomm                      # or point DATABASE_URL at any Postgres
cp .env.example .env && $EDITOR .env     # at minimum DATABASE_URL and SECRET_KEY
./run_dev.sh                             # seeds users/profiles, starts worker + web on http://127.0.0.1:8000
```

The development seed accounts are below. Change them on the Admin page, or set `SEED_*_PASSWORD` before seeding.

| Role | Email | Password |
|---|---|---|
| Admin | admin@trafficomm.local | admin-dev-password |
| Employee | employee@trafficomm.local | employee-dev-password |
| Employee | employee2@trafficomm.local | employee2-dev-password |

You can also run the two processes separately:
- Web: `PYTHONPATH=. uvicorn app.web:app`
- Worker: `PYTHONPATH=. python -m app.worker`

You can run several workers at once. They claim jobs with `FOR UPDATE SKIP LOCKED`.

## Using it

1. **New task.** Fill in the client and profile, title, brand, reporting period and an optional comparison period, the currency and the deliverables. Then paste the instructions.
2. **Upload files into the three drop areas:**
   - A: raw CSV/XLSX exports
   - B: an optional Excel template
   - C: an optional client PPTX, plus an optional ZIP of creative images

   Each file shows its upload progress and is inspected straight away. You see its sheets, rows, platform, currency, date range, and any total rows or pivot blocks that will be excluded. You can correct a file's role from the dropdown.
3. **Submit.** The workspace shows the real stages: Uploaded → Inspecting → Mapping → Awaiting clarification → Calculating → Generating → Validating → Ready for review. The page updates itself, and you can leave and come back.
4. **Answer questions.** When the data is ambiguous the app asks instead of guessing. Examples: which conversion CPA should use, whether Snapchat swipe-ups count as link clicks, an exchange rate, which of two conflicting exports to use, or which creative an ambiguous slide shows. Tick "Remember for this client profile" to save the answer for next time.
5. **Review.** The workspace shows:
   - key figures and commentary (every number is traced to a calculated value)
   - downloads (signed links that expire after 10 minutes)
   - rendered previews
   - the validation panel
   - findings: mapping, assumptions, exclusions and overlaps
   - the deck slide map and flags
   - the files
6. **Feedback.** Type what you want changed under "What would you like to change?". You can choose which version to base the revision on and attach extra files. The result is a new immutable version with a change summary. Earlier versions stay downloadable. Use **Approve** to mark the version you are happy with.

Nothing is ever sent to a client or published automatically.

## What works

**Inputs**
- Meta, TikTok and Snapchat exports are supported, with partial support for Google Ads.
- Headers are mapped through a synonym registry, with platform-specific rules and client-profile overrides.
- Encoding and delimiter are sniffed automatically.
- Metadata rows above the header are skipped.
- Platform total rows and pivot/summary blocks are excluded.
- Ambiguous date and number formats raise a question.
- Brand, market, country, language and format are derived from naming conventions. For ATC these are school codes such as BWA, BCD, BCAD and BCAA.

**Calculation rules (deterministic only)**
- Rates are recomputed from their components, never averaged. Each rate uses only rows that report both its numerator and its denominator.
- A zero denominator gives N/A. Missing is never treated as zero, and a sheet without a core column is flagged.
- Reach is never summed.
- Currencies are never combined without an approved, recorded rate.
- Click and conversion definitions are never combined without approval.
- Meta "Results" are split by result indicator. Lead-form results count as leads. Reach and link-click results are not treated as conversions.
- A clicks definition the data doesn't contain is not applied silently. The employee is told what was kept and why.

**Overlap handling**
- Identical files, duplicate rows across files and conflicting rows are detected. Conflicting rows raise a question.
- Comparison-period deltas are calculated.

**Excel output**
- Executive Summary, profile-ordered breakdown tabs, optional split tabs (per country, language or platform) and Period Comparison.
- Consolidated Data with per-row provenance, plus Insights, Data Quality and Methodology tabs.
- Read back after writing to confirm the values.

**Excel template**
- The template is copied first, then only its input ranges are filled. Formulas are never overwritten, and row formulas are extended down.
- Charts, images, pivots and external links are flagged as unsupported.
- The file is recalculated with LibreOffice and the formula results are reconciled against the engine.

**Client deck update**
- Metric slides are matched to creatives: exact, then fuzzy, then by brand hint, then by contiguity. When a match is ambiguous the employee is asked.
- Table cells are updated in the deck's own number style, along with range labels (ordinal or plain, following the deck), month labels and native charts (series data replaced).
- Slides are never flattened to images. Shapes and layouts are preserved.
- Flagged for review rather than changed: client-supplied content, grouped shapes with numbers, SmartArt, OLE objects, media, and creatives that have no slide.
- Speaker notes are checked.
- Optional extras on request: thumbnail replacement from the creatives ZIP, and cloning slides for new creatives.

**New deck**
- Trafficomm-branded: cover, KPIs, platform, brand, creative highlights (paginated), market, comparison, insights and methodology.
- Every figure is checked against the dataset.

**Rendering**
- PDF via LibreOffice and PNG previews via pdftoppm.
- A layout check for overflow, off-slide shapes and missing fonts.

**Validation panel**
- Checks covered:
  - required columns, source coverage and the reporting period
  - overlaps, currency and metric compatibility
  - reconciliation against an independent re-read of every file, and rate recomputation
  - Excel read-back, template formulas, deck read-back, Excel vs PowerPoint consistency and labels
  - deck flags, new-deck figures, open/render and layout
  - commentary traceability
- A check is only shown as passed if it actually ran. "Not run" is shown as such.
- Any failed check blocks the version.

**AI layer**
- Claude (`ANTHROPIC_MODEL`, default `claude-opus-5-5`) interprets the instructions and feedback into a structured plan and writes commentary. It never produces numbers.
- Every number in the commentary must match a calculated fact within display rounding, or the statement is dropped.
- Uploaded content is wrapped as untrusted, and instructions inside files are ignored.
- Without a key, a rule-based provider is used and labelled "AI mocked (rule-based)" across the UI.

**Jobs**
- Durable Postgres queue.
- Each job runs in a subprocess with memory and CPU limits and a wall-clock timeout.
- Heartbeats, recovery of stale jobs after a worker crash, and retries with exponential backoff.
- Idempotency keys, so the same inputs never produce a duplicate job.
- Cooperative cancellation backed by killing the subprocess. Retry is available after a failure or cancellation.

**Security**
- bcrypt passwords, signed session cookies, and employee and admin roles.
- An owner-or-admin check on every task, file and artifact route. Another user's task returns 404.
- A CSRF header is required on API mutations, plus CSP and security headers.
- Private storage, with originals stored read-only.
- Short-lived download links bound to the user.
- Limits on upload size, uncompressed size (zip-bomb guard), rows and columns. Only these extensions are accepted.
- Macros are never executed: workbooks are opened read-only with data only.
- Errors are recorded as type and location only, so report data stays out of the logs.
- An audit log, a retention policy that runs daily in the worker (also on demand from Admin), and task deletion.

**Admin**
- Users, client profile JSON (naming rules, metric labels, remembered answers), retention and recent jobs.

## Environment variables

See `.env.example`. The settings you must set are:
- `DATABASE_URL`
- `SECRET_KEY` (if unset in development, a key is generated into `var/.dev_secret`)
- `STORAGE_DIR`
- `COOKIE_SECURE=1` behind HTTPS

Optional settings:
- `ANTHROPIC_API_KEY` enables Claude. Without it the app runs in "AI mocked" mode.
- `ANTHROPIC_MODEL`
- The limit settings: `MAX_UPLOAD_MB`, `MAX_UNCOMPRESSED_MB`, `MAX_ROWS_PER_SHEET`, `MAX_COLS_PER_SHEET`, `DOWNLOAD_LINK_TTL_SECONDS`, `JOB_TIMEOUT_SECONDS`, `JOB_MEMORY_MB`, `JOB_MAX_ATTEMPTS`, `RETENTION_DAYS`
- `SOFFICE_BIN`

The API key is read only on the server, inside the job subprocess. It is never sent to the browser.

## Tests

```bash
createdb trafficomm_test
python -m pytest tests -v        # builds the synthetic fixtures automatically
```

- `tests/test_engine_acceptance.py`: **synthetic** fixtures (the fictional brand "Acme Dates") run through the full pipeline. It covers:
  - multi-platform headers, an Excel-only request, template population, deck update and a new deck
  - conflicting currencies, overlapping exports and identical files
  - missing columns, zero denominators and non-additive reach
  - unsupported PPTX elements and commentary traceability
  - metric-definition feedback, breakdown and tab-split feedback, and additional uploads
  - raw files left unchanged, macro workbooks, zip bombs, and instructions embedded in files
- `tests/test_web_acceptance.py`: end to end through FastAPI, Postgres and real worker subprocesses. It covers:
  - the full workflow: create, upload, submit, clarification, answer, ready, then download and preview
  - approval
  - a revision that changes the breakdown (one tab per country) and the click definition, with version retention
  - a revision with an additional upload, and feedback the app can't map to a change
  - cross-user denial, admin access, and CSRF and auth checks
  - a deck update with no deck, and upload validation
  - refreshing during processing, cancelling a running job, cancelling a queued job and retrying it
  - idempotent enqueueing and stale-job recovery, and retention
- `tests/test_real_samples.py`: **real-client** validation on the ATC exports and deck. It is skipped when those files are absent, because they are client data and are not bundled. It checks totals and that the deck update reproduces the employee-made deck, and it covers the all-clicks request on exports that contain only link clicks.

Last run: **38 passed** (21 engine, 14 web, 3 real-client).

## Known limitations

- **PowerPoint fidelity.**
  - Text is replaced run by run, keeping fonts and colours.
  - Tables and native charts are updated, and the embedded chart workbook is rewritten.
  - Not updated, only flagged: SmartArt, grouped shapes containing numbers, OLE or embedded Excel objects, numbers drawn inside images, and linked charts.
  - Previews are rendered by LibreOffice, which can differ slightly from PowerPoint in fonts and kerning. The layout check is a heuristic.
  - New slides for new creatives, and thumbnail replacement, are opt-in and should be eyeballed.
- **Excel templates.** openpyxl does not preserve charts, images or pivot caches in an existing workbook. These are flagged before you download. Pivot tables need a refresh in Excel.
- **Platforms.** Meta is the most thoroughly tested, including on the real ATC files. TikTok, Snapchat and Google Ads mappings are tested on synthetic files only. Real exports from other clients may need profile overrides, which can be edited on the Admin page.
- **Mocked AI.** The rule-based interpreter understands a fixed set of requests: clicks definition, CPA conversion, tab splits, breakdowns, removing a slide or section, a shorter summary, commentary on or off, and using new files. Anything else gets a clear "I couldn't map that" reply. With an API key, Claude produces the same structured plan from free text.
- **Data window.** When the data covers less than the reporting period (ATC September runs 1 to 21 Sep), the labels use the actual data window and a warning is shown.
- Each worker runs one job at a time. Scale by running more workers.

## Shortest path to a private production deployment

1. One small VM (2 vCPU, 8 GB) inside the company network or behind a VPN or identity-aware proxy. It needs Python, LibreOffice, poppler and Postgres, ideally managed Postgres.
2. Set `DATABASE_URL`, a strong `SECRET_KEY`, `STORAGE_DIR` on an encrypted private disk, `COOKIE_SECURE=1` and, optionally, `ANTHROPIC_API_KEY`.
3. Run `uvicorn app.web:app --workers 2` behind nginx or Caddy with TLS. Run one or more `python -m app.worker` processes under systemd with restart on failure.
4. Seed with strong passwords, create the real employee accounts, then disable the dev accounts.
5. Back up Postgres and `STORAGE_DIR` nightly, and set the retention period on the Admin page.

Optional: switch to SSO via the proxy, move storage to a private S3 bucket (the `LocalStorage` interface is the only place to change), and run workers in containers with a read-only filesystem.

## Deploying on Render (chosen for employee testing)

`render.yaml` is a Render Blueprint. It defines:
- one Docker web service (`deploy/Dockerfile` with `deploy/start.sh`), which runs the web app and the worker together and mounts a 10 GB persistent disk at `/data`
- a managed Postgres database

To deploy:
1. In Render, choose New → Blueprint, pick this repository, and enter `SEED_*_PASSWORD` and, optionally, `ANTHROPIC_API_KEY`. Then choose Apply.
2. Under Settings → Custom Domains, add `reports.trafficomm.com`.
3. At your DNS provider, create a CNAME record from `reports` to `<service>.onrender.com`. Render issues the HTTPS certificate automatically.

Approximate monthly cost:
- Standard instance (2 GB RAM, needed for LibreOffice): $25
- Postgres basic: about $6
- 10 GB disk: about $2.50

A self-hosted alternative is `deploy/docker-compose.yml` (Postgres, app, worker and Caddy) on any VPS.
