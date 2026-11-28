# Engineering Agent: read test results without the Checks permission (2026-11-28)

Fine-grained GitHub tokens cannot be granted "Checks", so `GET /commits/{sha}/check-runs` can return 403 and
the portal would show every agent PR as "pending" forever. `engineering.checks_state` now falls back to the
Actions runs API (`actions/runs?head_sha=`, needs Actions: read) and then the combined commit status (Commit
statuses: read). Regression test: `test_checks_state_falls_back_when_check_runs_forbidden`.
