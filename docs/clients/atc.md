# ATC (Around The Clock Media): client knowledge

Brands are schools: BWA (Bloom World Academy, Dubai), BCD (Brighton College Dubai), BCAD (Brighton College
Abu Dhabi), BCAA (Brighton College Al Ain). Profile in `app/profiles.py`.

## Sources
- Meta Ads Manager ad-level XLSX in AED ("Reporting starts/ends", "Ad name", "Objective", "Brand" = creative,
  "Results" + "Result indicator", "Amount spent (AED)", "Link clicks", "3-second video plays", Reach, Frequency).
- Employees sometimes append a pivot block ("Values" / "Sum of ..." / "Grand Total") under the rows: excluded.
- Snapchat / TikTok exports are USD → AED (task rate, default 3.6725).
- Ad name: `FB+IG_<targeting>_<school>_<objective>_<market>_<VID|Static>_<EN|AR>_<creative>_Ad#n_(date)`.
- File naming: `Client_Platform_Market_Period` (e.g. `BWA_Meta_UAE_Oct2026.xlsx`; markets joined `UAE-KSA`).

## Metrics
Views = Meta 3s / Snapchat 2s / TikTok 2s plays; VTR = views / impressions; Clicks = link clicks;
CTR = link clicks / impressions; reach above ad level is summed (agency convention), frequency =
impressions / summed reach; "Results" depend on the result indicator and are never summed blindly.

## Deck
BWA deck slides 7–28: one slide per creative (Range | Audience Size | Reach | Views | Impressions |
Frequency | VTR | Clicks | CTR, '-' for N/A). Slides with "Kyra to update" or "to be shared by the client"
are never touched. Screenshots get a black border. Professional language only.
