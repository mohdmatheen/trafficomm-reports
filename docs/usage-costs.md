# Usage & costs (admin only)

Page: `/admin/usage`. Every route checks `require_admin` on the server and is sent with `no-store`.

## What it shows

| Figure | Source | Notes |
|---|---|---|
| Live AI activity per employee | This app's own ledger (`ai_requests`) | Written by the server around every Claude call, including retries. Attributed to the signed-in employee who started the task or revision. Exact. |
| Confirmed Claude cost | Anthropic Usage and Cost Analytics API (beta) | `GET /v1/organizations/analytics/cost` and `/usage`, header `anthropic-beta: analytics-usage-cost-2026-08-24`. Daily buckets, at most 31 days per request. Amounts are decimal strings in USD cents. |
| Render cost | **Not supported** | Render's public API has no billing, invoice or charge endpoint. The page links to dashboard.render.com/billing and never shows a Render amount. |
| INR | Frankfurter (`api.frankfurter.dev`, ECB-based daily rate) | Display conversion only. USD stays the record. Rounded once, half-up. |

Employee money is shown as **Unavailable**. All requests use one shared server key, and the provider reports
cost per workspace, key and model, not per person. Costs are never split proportionally.

Missing data is never shown as $0. The combined figure is always a "Confirmed subtotal", marked as incomplete
coverage, because Render billing can't be read.

## Server environment variables

| Variable | Required | Purpose |
|---|---|---|
| `ANTHROPIC_ADMIN_API_KEY` | For cost data | Admin API key (`sk-ant-admin...`), created by an org admin in the Claude Console. Separate from `ANTHROPIC_API_KEY`. |
| `ANTHROPIC_WORKSPACE_IDS` and/or `ANTHROPIC_API_KEY_IDS` | For cost data | Comma-separated ids that belong to this app. Rows outside them are stored as excluded. Without a mapping the cost is shown as unmapped, not as this app's cost. |
| `RENDER_API_KEY`, `RENDER_SERVICE_IDS` | Optional | Service metadata only (no money). |
| `USAGE_FX` | Optional, default on | Set to `0` to turn off the INR conversion. |
| `USAGE_SYNC_MINUTES` | Optional, default 60 | How often the worker syncs. "Sync now" is debounced to once per 5 minutes. |
| `DISPLAY_TZ` | Optional, default `Asia/Kolkata` | Display timezone. Everything is stored in UTC. |

## Sync and freshness

- The worker calls the sync on a schedule. Each run refetches a rolling window: the current month, plus the
  previous month until day 8, because Anthropic can revise values until about 7 days after month end.
- Rows in the window are replaced in one transaction (an advisory lock stops two syncs running at once), so
  re-running is idempotent and late corrections replace older values.
- Rate limits and server errors back off (honouring `retry-after`). An auth error is shown as its own status.
- Provider cost usually appears within about 4 hours and can take up to 24. The page shows the provider's
  `data_refreshed_at` separately from the time of the last successful fetch.
- Live activity is pushed to the open page over server-sent events (Postgres `LISTEN ai_usage`). It carries
  no cost data.

## Reconciliation

After a mapped sync, the page compares this app's own request counts and tokens with the provider's usage
rows for the same days and scope, and reports the match rate.

## Verification status

- Covered by tests with fixtures (`tests/test_usage.py`): admin-only access, retry linking, attribution
  through revisions and jobs, history kept after rename or delete, idempotent sync with late corrections,
  excluded workspaces, pagination, cents parsing, INR rounding, incomplete subtotal, backoff, debounce, NOTIFY.
- **Not yet verified against the live Anthropic API**: that needs `ANTHROPIC_ADMIN_API_KEY` and the
  workspace or key ids set on the server. The FX source and Render API were not reachable from the build
  environment either.
