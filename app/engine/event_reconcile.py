"""Local day x event reconciliation of an AppsFlyer event report against a verified (hand-checked) report.

Used by AI Support to investigate discrepancies such as the FAB iOS All Events case. It never adjusts a
total: it recomputes the report under the current recipe and under candidate rule changes generated from
the data (extra campaign exclusions, media sources, retargeting, timezone basis, dedup key and scope,
export type), and scores every candidate on every verified date. A candidate is only offered as a rule
when it reproduces the verified figures exactly on at least two dates; nothing is applied automatically.

Runs entirely on the portal server. The result holds aggregates only (counts, campaign and media source
names); device and AppsFlyer identifiers never leave this module.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from . import event_report as ER

MIN_DATES_FOR_RULE = 2
MAX_CAMPAIGN_CANDIDATES = 15


@dataclass
class Row:
    campaign: str
    event: str
    device: str
    af_id: str
    time: dt.datetime | None
    day: dt.date | None
    media_source: str
    retargeting: bool


@dataclass
class Variant:
    key: str
    label: str
    exclude_campaigns: frozenset = frozenset()
    exclude_media_sources: frozenset = frozenset()
    exclude_retargeting: bool = False
    sources: tuple | None = None          # restrict export types
    id_key: str = "device"                # device | appsflyer | device_or_appsflyer
    dedup_scope: str = "month"            # month | day
    tz_shift_hours: int = 0
    spec: dict = field(default_factory=dict)


def _parse_time(s) -> dt.datetime | None:
    s = str(s or "").strip()
    try:
        return dt.datetime.fromisoformat(s[:19])
    except ValueError:
        return None


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("true", "1", "yes")


def load_rows(recipe: dict, raw_files: list[dict]) -> dict[tuple, list[Row]]:
    """{(platform, source): [Row]} for the exports the recipe needs, in file order (same order as build_counts)."""
    need = {(p, s) for p in recipe["platforms"] for s in recipe["sources"]}
    ev_set = {c["event"] for c in recipe["columns"] if c.get("event")}
    out: dict[tuple, list[Row]] = {}
    for f in raw_files:
        head, rows = ER._read_table(f["path"])
        info = ER._describe(f["name"], head, rows)
        key = (info["platform"], info["source"])
        if key not in need or key in out:
            continue
        idx = {h: i for i, h in enumerate(head)}

        def g(r, col):
            i = idx.get(col)
            return r[i] if i is not None and i < len(r) else ""
        lst = []
        for r in rows:
            ev = str(g(r, "Event Name") or "").strip()
            if ev not in ev_set:
                continue
            rec = {"Event Value": g(r, "Event Value")}
            t = _parse_time(g(r, "Event Time"))
            lst.append(Row(campaign=str(g(r, "Campaign") or "").strip(), event=ev, device=ER._device_id(rec, recipe["id_field"]),
                           af_id=str(g(r, "AppsFlyer ID") or "").strip(), time=t, day=ER._parse_day(g(r, "Event Time")),
                           media_source=str(g(r, "Media Source") or "").strip(), retargeting=_truthy(g(r, "Is Retargeting"))))
        out[key] = lst
    return out


def count(recipe: dict, slots: dict[tuple, list[Row]], v: Variant, keep_rows: bool = False):
    """Counts per (day, event) under a variant, deduplicating like build_counts (per file, per platform, across platforms)."""
    sources = [s for s in recipe["sources"] if v.sources is None or s in v.sources]

    def ident(r: Row) -> str:
        if v.id_key == "appsflyer":
            return r.af_id
        if v.id_key == "device_or_appsflyer":
            return r.device or (("af:" + r.af_id) if r.af_id else "")
        return r.device

    per_slot = {}
    for p in recipe["platforms"]:
        for s in sources:
            seen, kept = set(), []
            for r in slots.get((p, s), []):
                if not ER._campaign_ok(r.campaign, recipe) or r.campaign in v.exclude_campaigns:
                    continue
                if r.media_source in v.exclude_media_sources or (v.exclude_retargeting and r.retargeting):
                    continue
                i = ident(r)
                if not i:
                    continue
                day = r.day
                if v.tz_shift_hours:
                    if not r.time:
                        continue
                    day = (r.time + dt.timedelta(hours=v.tz_shift_hours)).date()
                if not day:
                    continue
                k = (i, r.event, day) if v.dedup_scope == "day" else (i, r.event)
                if k in seen:
                    continue
                seen.add(k)
                kept.append((k, day, r))
            per_slot[(p, s)] = kept

    def merge(lists):
        seen, out = set(), []
        for lst in lists:
            for k, day, r in lst:
                if k not in seen:
                    seen.add(k)
                    out.append((k, day, r))
        return out
    final = merge([merge([per_slot[(p, s)] for s in sources]) for p in recipe["platforms"]])
    c = Counter((day, r.event) for _, day, r in final)
    return (c, final) if keep_rows else c


def verified_cells(prev: dict, recipe: dict) -> tuple[dict, list[str]]:
    """{(day, event): count} from a report read by event_report.read_previous; plus block labels that map to no event."""
    by_header = {c["header"]: c["event"] for c in recipe["columns"] if c.get("event")}
    events = set(by_header.values())
    cells, unknown = {}, []
    for label, blk in (prev.get("blocks") or {}).items():
        ev = label if label in events else by_header.get(label)
        if not ev:
            unknown.append(label)
            continue
        for d, n in (blk.get("days") or {}).items():
            cells[(d, ev)] = int(n or 0)
    return cells, unknown


def _score(c: Counter, target: dict, dates: list[dt.date], events: list[str]) -> dict:
    per_date, total_err = [], 0
    for d in dates:
        err = sum(abs(c.get((d, e), 0) - target.get((d, e), 0)) for e in events)
        total_err += err
        per_date.append({"date": d.isoformat(), "portal": sum(c.get((d, e), 0) for e in events),
                         "verified": sum(target.get((d, e), 0) for e in events), "cell_error": err})
    return {"total_error": total_err, "exact_dates": sum(1 for x in per_date if x["cell_error"] == 0), "per_date": per_date}


def candidates(recipe: dict, slots, base_final, target: dict, dates: list[dt.date], extra_exclusions: list[str] | None = None) -> list[Variant]:
    """Candidate rule changes suggested by the data in the cells where the portal is higher."""
    surplus = {(d, e) for (d, e) in {(d, e) for d in dates for e in {e for (_, e) in target}}
               if sum(1 for _, day, r in base_final if day == d and r.event == e) > target.get((d, e), 0)}
    camp, media = Counter(), Counter()
    for _, day, r in base_final:
        if (day, r.event) in surplus:
            camp[r.campaign] += 1
            media[r.media_source] += 1
    has_rt = any(r.retargeting for rows in slots.values() for r in rows)
    has_af = any(r.af_id for rows in slots.values() for r in rows)
    out = [Variant("baseline", "Current rule (no change)")]
    for c, _ in camp.most_common(MAX_CAMPAIGN_CANDIDATES):
        out.append(Variant(f"exclude_campaign:{c}", f"Also exclude campaign {c}", exclude_campaigns=frozenset([c]),
                           spec={"campaign_match": recipe.get("campaign_match"), "add_campaign_exclusions": [c]}))
    if extra_exclusions:
        ex = frozenset(x for x in extra_exclusions if x)
        out.append(Variant("employee_exclusions", "Exclude the campaigns the employee listed", exclude_campaigns=ex,
                           spec={"add_campaign_exclusions": sorted(ex)}))
    for m, _ in media.most_common(5):
        if m:
            out.append(Variant(f"exclude_media:{m}", f"Exclude media source {m}", exclude_media_sources=frozenset([m]),
                               spec={"exclude_media_sources": [m]}))
    if has_rt:
        out.append(Variant("exclude_retargeting", "Exclude retargeting conversions", exclude_retargeting=True, spec={"exclude_retargeting": True}))
    if has_af:
        out.append(Variant("id_appsflyer", "Deduplicate on AppsFlyer ID instead of deviceId", id_key="appsflyer", spec={"id_field": "AppsFlyer ID"}))
        out.append(Variant("id_device_or_af", "Use AppsFlyer ID when deviceId is missing", id_key="device_or_appsflyer",
                           spec={"id_fallback": "AppsFlyer ID"}))
    out.append(Variant("dedup_per_day", "Deduplicate per day instead of per month", dedup_scope="day", spec={"dedup_scope": "day"}))
    for h in (-4, 4):
        out.append(Variant(f"tz_{h:+d}", f"Count days {abs(h)} hours {'earlier' if h < 0 else 'later'} (timezone basis)", tz_shift_hours=h,
                           spec={"tz_shift_hours": h}))
    for s in recipe["sources"]:
        out.append(Variant(f"only_{s}", f"Use only the {ER.SOURCE_LABEL.get(s, s)} export", sources=(s,), spec={"sources": [s]}))
    return out


def reconcile(recipe: dict, raw_files: list[dict], target: dict, extra_exclusions: list[str] | None = None) -> dict:
    """Compare the recipe's counts with verified cells and rank candidate rule changes. Aggregates only."""
    ER.build_counts(recipe, raw_files)  # same validation and error messages as a normal run
    slots = load_rows(recipe, raw_files)
    base, base_final = count(recipe, slots, Variant("baseline", ""), keep_rows=True)
    last_data_day = max((day for _, day, _ in base_final), default=None)
    dates = sorted({d for (d, _) in target if last_data_day is None or d <= last_data_day})
    events = [c["event"] for c in recipe["columns"] if c.get("event")]
    cells = []
    for d in dates:
        for e in events:
            p, t = base.get((d, e), 0), target.get((d, e), 0)
            if p != t:
                rows = [r for _, day, r in base_final if day == d and r.event == e]
                cells.append({"date": d.isoformat(), "event": e, "portal": p, "verified": t, "difference": p - t,
                              "campaigns": Counter(r.campaign for r in rows).most_common(6),
                              "media_sources": Counter(r.media_source for r in rows).most_common(4)})
    devices_by_day = defaultdict(set)
    for (i, *_), day, _ in base_final:
        devices_by_day[day].add(i)
    results = []
    for v in candidates(recipe, slots, base_final, target, dates, extra_exclusions):
        sc = _score(count(recipe, slots, v), target, dates, events)
        results.append({"key": v.key, "label": v.label, "spec": v.spec, **sc})
    baseline = next(r for r in results if r["key"] == "baseline")
    for r in results:
        r["improves"] = r["total_error"] < baseline["total_error"]
        r["reproduces_all"] = bool(dates) and r["exact_dates"] == len(dates)
    ranked = sorted(results, key=lambda r: (r["key"] != "baseline" and not r["improves"], r["total_error"], r["key"] != "baseline"))
    best = [r for r in ranked if r["key"] != "baseline" and r["reproduces_all"]]
    if baseline["reproduces_all"]:
        verdict = "The current rule already reproduces every verified date."
    elif best and len(dates) >= MIN_DATES_FOR_RULE:
        verdict = f"“{best[0]['label']}” reproduces all {len(dates)} verified dates exactly. It is proposed for approval, not applied."
    elif best:
        verdict = (f"“{best[0]['label']}” reproduces the only verified date. Upload a verified report covering at least "
                   f"{MIN_DATES_FOR_RULE} dates before it can become a rule.")
    else:
        verdict = "No single candidate explains the difference. The answers about the manual procedure are needed."
    return {"recipe": recipe.get("key"), "dates": [d.isoformat() for d in dates], "baseline": baseline,
            "mismatched_cells": cells[:60], "candidates": ranked[:25], "verdict": verdict,
            "rule_candidate": best[0] if best and len(dates) >= MIN_DATES_FOR_RULE and not baseline["reproduces_all"] else None,
            "unique_devices_by_day": {d.isoformat(): len(s) for d, s in sorted(devices_by_day.items()) if d in set(dates)}}
