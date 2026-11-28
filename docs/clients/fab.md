# FAB (First Abu Dhabi Bank): client knowledge

**Confidential banking client.** External AI processing is off until an administrator enables it after
written client consent. Never commit FAB exports, workbooks or identifiers.

## Data sources
- AppsFlyer raw exports, one file per app × export type:
  - iOS app `id1383237548`, Android `com.fab.personalbanking`
  - export types: *in-app events* and *non-organic in-app events* (organic installs are not used)
- Columns used: `Event Time`, `Event Name`, `Campaign`, `Event Value` (JSON with `deviceId`).
  Identifier columns (AppsFlyer ID, IDFA/IDFV, Customer User ID, IP, Event Value) never leave the server.
- The team's Google Sheet ("Internal Report - FAB"): per-month tabs that track single campaigns
  (e.g. "Cards November 2026" = Amazon_Offer_2026 with `ntb_cc_card_created`,
  `cc_ntb_contactDetail_continue_click`). It is **not** the All Events report.

## Counting rules (built-in recipes in `app/engine/event_report.py`)
- Device id = `deviceId` from Event Value. Dedup on (device, event) keeping the first occurrence: per file,
  then per platform, then across platforms. Count by Event Time date.
- **CASA Always On**: iOS + Android; campaigns exactly Nine_Thousand_Cashback_PLT, Dining_PLT,
  Twenty_Percent_Cashback_PLT, Travel_PLT, Cinema_PLT; 4 CASA congratulation events. 25 Nov reproduced exactly (1388).
- **IOS-PLT All events**: iOS only; every campaign **except** the 5 Always On campaigns; tab name
  "FAB_IOS-PLT - <Mon>_All events" (month ignored). Several columns are constant zero by design.
- **IOS-PLT CASA**: iOS only; campaign contains "CASA"; 2 NTB congratulation events.
- Previous-day mismatches within AppsFlyer's revisions are warnings for built-in types.

## Open discrepancy (pilot, AI Support "FAB iOS All Events discrepancy")
Portal higher than the verified All Events report by 11 on 25 Nov 2026 and by 12 on 26 Nov 2026 (verified
26 Nov total 4191). The residual sits in the same cells both days, so a rule is missing. Unknown so far:
extra exclusions beyond the 5 Always On campaigns, dedup key, attribution window, date/timezone basis,
manual adjustments, and whether the sheet check should compare per-campaign tabs (option A) or not (B).
Do **not** subtract 11/12 or hardcode totals; any rule must reproduce several dates.
