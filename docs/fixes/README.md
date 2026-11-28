# Fix history

One file per fix from now on (`docs/fixes/AIS-xxxx.md` for AI Support issues). Earlier fixes, summarised:

| PR | Date | Root cause → fix |
|---|---|---|
| #12 | 2026-11-07 | Multi-month template fill wrote only one month; TikTok files not merged; diagnostics hid real errors. Reach is summed across campaigns (Matheen). |
| #13 | 2026-11-07 | Upload guidelines panel and file-name check (`Client_Platform_Market_Period`). |
| #15 | 2026-11-17 | Platform detection: trust the file name when it names one platform; columns override only with 2+ distinctive headers. |
| #16 | 2026-11-19 | ZSP/Mayfair decks returned unchanged: updater handled one creative per slide → RowTableUpdater + "Deck figures updated" check. Claude instructions calls all failed (optional/nullable schema fields) → fully required PlanOut. X "Total Reach & Frequency" tab doubled impressions → excluded when a keyed tab exists. |
| #17 | 2026-11-19 | YouTube "TrueView views" mapped to Views; "Unique users" to reach. |
| #18 | 2026-11-19 | Claude's instruction reading saved per structure and reused; rolling decks no longer count as new structures. |
| #19 | 2026-11-24 | Media plan upload: booked vs delivered and pacing. |
| #20 | 2026-11-24 | ZSP 78-file job ran out of memory (decks opened in full, full-size renders) → light deck open / render copies (2.7 GB → 1.05 GB). |
| #21 | 2026-11-25 | FAB CASA Always On daily report automated; Google Sheet verification gate. |
| #22 | 2026-11-25 | Event reports restricted to FAB; other clients unchanged. |
| #24 | 2026-11-26 | IOS-PLT All events / CASA built in. Live block: Claude counted all campaigns including Always On (doubling 01–02 Nov) → `campaign_match: exclude`. |
