#!/usr/bin/env python3
"""
Front Desk Shift Scheduler — Dartmouth Admissions
--------------------------------------------
Run:  python3 desk_scheduler.py
Then open http://localhost:8766 in your browser.

Upload a Google Form availability CSV to generate a fair weekly shift schedule.
Shifts: Mon–Fri (and optional Sat) in blocks:
  8:30–10:00 AM  |  10:00 AM–12:00 PM  |  12:00–2:00 PM  |  2:00–4:30 PM
Saturday (if enabled):
  8:30–10:00 AM  |  10:00 AM–1:00 PM
"""

import http.server
import socketserver
import os
import threading
import json
import csv
import io
import re
import random
from urllib.parse import urlparse
from collections import defaultdict

PORT = int(os.environ.get("PORT", 8766))

# ─────────────────────────────────────────────────────────────────────────────
# SHIFT DEFINITIONS
# ─────────────────────────────────────────────────────────────────────────────

WEEKDAY_SHIFTS = [
    {"id": "A", "label": "8:30–10:00 AM",  "start": 8*60+30,  "end": 10*60},
    {"id": "B", "label": "10:00 AM–12:00 PM","start": 10*60,  "end": 12*60},
    {"id": "C", "label": "12:00–2:00 PM",  "start": 12*60,    "end": 14*60},
    {"id": "D", "label": "2:00–4:30 PM",   "start": 14*60,    "end": 16*60+30},
]

SATURDAY_SHIFTS = [
    {"id": "A", "label": "8:30–10:00 AM",  "start": 8*60+30,  "end": 10*60},
    {"id": "B", "label": "10:00 AM–1:00 PM","start": 10*60,   "end": 13*60},
]

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
ALL_DAYS  = WEEKDAYS + ["Saturday"]

# ─────────────────────────────────────────────────────────────────────────────
# TIME PARSING  (handles "8:30-4:30", "8:30AM - 4:30 PM", "8-30-4:30", etc.)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_time(s):
    """Return minutes-since-midnight, or None."""
    s = s.strip().lower()
    s = re.sub(r"(\d)\s*-\s*(\d{2})\s*-\s*(\d)", r"\1:\2:\3", s)  # 8-30-4:30 → 8:30:4:30 ugh
    # normalise dashes used as range sep vs time sep
    # strategy: replace first isolated '-' that isn't preceded by a digit-colon pair
    m = re.search(r"(\d{1,2})[:\.](\d{2})\s*(am|pm)?", s)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        meridiem = m.group(3)
        if meridiem == "pm" and h != 12: h += 12
        if meridiem == "am" and h == 12: h = 0
        return h * 60 + mn
    m2 = re.search(r"(\d{1,2})\s*(am|pm)", s)
    if m2:
        h = int(m2.group(1))
        if m2.group(2) == "pm" and h != 12: h += 12
        if m2.group(2) == "am" and h == 12: h = 0
        return h * 60
    return None

def parse_availability_range(raw):
    """
    Given a free-text string like "8:30-4:30" or "10am-4:30pm",
    return (start_minutes, end_minutes) or None.
    """
    if not raw:
        return None
    raw = raw.strip().lower()
    not_free_patterns = ["not free", "not on", "n/a", "unavailable", "no", "none", "off"]
    if any(p in raw for p in not_free_patterns):
        return None

    # Normalise the weird "8-30-4:30" dash-as-colon format
    raw = re.sub(r"\b(\d)-(\d{2})\b", r"\1:\2", raw)

    # Split on range separator: ' - ', '–', '—', or bare '-' between time tokens
    # Use a separator that won't eat time colons
    parts = re.split(r"\s*[-–—]\s*(?=\d)", raw, maxsplit=1)
    if len(parts) == 2:
        start = _parse_time(parts[0])
        end   = _parse_time(parts[1])
        if start is not None and end is not None:
            # If end looks like it might be AM when it should be PM
            if end < start:
                end += 12 * 60
            return (start, end)

    # Fallback: try to extract two times with regex
    times = re.findall(r"\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)?", raw)
    if len(times) >= 2:
        start = _parse_time(times[0])
        end   = _parse_time(times[-1])
        if start is not None and end is not None:
            if end < start:
                end += 12 * 60
            return (start, end)

    return None

def avail_for_shifts(raw, shifts):
    """
    Return list of shift IDs that the person's availability covers.
    A shift is covered if ANY of the person's time ranges includes the full window.
    Handles multi-range cells separated by newlines or commas.
    """
    if not raw:
        return []
    # Split on newlines or semicolons to handle multiple ranges in one cell
    segments = re.split(r"[\n;]+", raw.strip())
    covered = set()
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        rng = parse_availability_range(seg)
        if rng is None:
            continue
        avail_start, avail_end = rng
        for sh in shifts:
            if avail_start <= sh["start"] and avail_end >= sh["end"]:
                covered.add(sh["id"])
    return list(covered)


# ─────────────────────────────────────────────────────────────────────────────
# GUIDE CSV PARSING
# ─────────────────────────────────────────────────────────────────────────────

def find_col(headers, keywords):
    for i, h in enumerate(headers):
        hl = h.lower()
        if any(k in hl for k in keywords):
            return i
    return None

def parse_worker_csv(raw_text, include_saturday=False):
    """
    Parse a Google Form availability CSV.
    Supports:
      Format A: First Name + Last Name columns + per-day availability cols (cols 49-53)
      Format B: email-derived name, same col layout
      Format C: Generic Name + Mon/Tue/Wed/Thu/Fri columns with time ranges or Yes/No
    Returns (workers list, error or None).
    Each worker: {name, email, availability: {day: [shift_ids]}}
    """
    reader = csv.reader(io.StringIO(raw_text))
    rows   = list(reader)
    if len(rows) < 2:
        return [], "File appears empty or has only a header row."

    headers = [h.strip() for h in rows[0]]

    # De-duplicate by email — prefer the submission that has availability data.
    # If someone resubmits with a different role (no availability cols filled),
    # keep the earlier submission that has the availability.
    seen = {}
    avail_col_range = range(40, 55)  # broad range covering availability cols
    for row in rows[1:]:
        if not any(c.strip() for c in row): continue
        key = row[1].strip().lower() if len(row) > 1 else str(id(row))
        has_avail = any(
            row[i].strip() for i in avail_col_range if i < len(row)
        )
        prev = seen.get(key)
        if prev is None:
            seen[key] = row
        else:
            prev_has_avail = any(
                prev[i].strip() for i in avail_col_range if i < len(prev)
            )
            # Only replace if new row has availability and old one doesn't
            if has_avail and not prev_has_avail:
                seen[key] = row
    data_rows = list(seen.values())

    workers = []

    # ── Detect availability columns ───────────────────────────────────────────
    day_keywords = {
        "Monday":    ["monday availability", "monday avail"],
        "Tuesday":   ["tuesday availability", "tuesday avail"],
        "Wednesday": ["wednesday availability", "wednesday avail"],
        "Thursday":  ["thursday availability", "thursday avail"],
        "Friday":    ["friday availability", "friday avail"],
        "Saturday":  ["saturday availability", "saturday avail"],
    }

    # Find ALL matching columns per day, then pick the one with the most data
    day_cols = {}
    for day, kws in day_keywords.items():
        candidates = [
            i for i, h in enumerate(headers)
            if any(k in h.lower() for k in kws)
        ]
        if not candidates:
            continue
        if len(candidates) == 1:
            day_cols[day] = candidates[0]
        else:
            # Pick the column that has the most non-empty, non-"not free" values
            def col_score(col_idx):
                count = 0
                for row in data_rows:
                    v = row[col_idx].strip().lower() if col_idx < len(row) else ""
                    if v and v not in ("not free", "not on", "n/a", ""):
                        count += 1
                return count
            day_cols[day] = max(candidates, key=col_score)

    # ── Name detection ────────────────────────────────────────────────────────
    has_fn = any("first name" in h.lower() for h in headers)
    has_ln = any("last name"  in h.lower() for h in headers)
    has_name = any(h.strip().lower() == "name" for h in headers)

    def email_to_name(email):
        local = email.split("@")[0]
        parts = local.split(".")
        if parts and len(parts[-1]) == 2 and parts[-1].isdigit():
            parts = parts[:-1]
        return " ".join(p.capitalize() for p in parts)

    days_to_use = WEEKDAYS + (["Saturday"] if include_saturday else [])

    if day_cols:
        # Format A / derived from the 25X-style survey
        fn_col = next((i for i,h in enumerate(headers) if "first name" in h.lower()), None)
        ln_col = next((i for i,h in enumerate(headers) if "last name"  in h.lower()), None)
        name_col = next((i for i,h in enumerate(headers) if h.strip().lower() == "name"), None)

        for row in data_rows:
            def cell(i):
                return row[i].strip() if i is not None and i < len(row) else ""

            if has_fn and has_ln:
                name = f"{cell(fn_col)} {cell(ln_col)}".strip()
            elif name_col is not None:
                name = cell(name_col)
            else:
                email = cell(1)
                name  = email_to_name(email) if email else "Unknown"

            if not name or name == " ":
                continue
            email = cell(1)

            availability = {}
            for day in days_to_use:
                col = day_cols.get(day)
                if col is None:
                    continue
                raw = cell(col)
                shifts = SATURDAY_SHIFTS if day == "Saturday" else WEEKDAY_SHIFTS
                availability[day] = avail_for_shifts(raw, shifts)

            workers.append({"name": name, "email": email, "availability": availability})
        if workers:
            return workers, None

    # ── Format C: Name + day columns with Yes/No or time range ───────────────
    if has_name:
        name_col = next(i for i,h in enumerate(headers) if h.strip().lower() == "name")
        col_map = {}
        for day in days_to_use:
            col = find_col(headers, [day.lower()])
            if col is not None:
                col_map[day] = col

        if col_map:
            for row in data_rows:
                name = row[name_col].strip() if name_col < len(row) else ""
                if not name: continue
                availability = {}
                for day, col in col_map.items():
                    raw = row[col].strip() if col < len(row) else ""
                    shifts = SATURDAY_SHIFTS if day == "Saturday" else WEEKDAY_SHIFTS
                    # Yes/No style
                    if raw.lower() in ("yes","true","1","available"):
                        availability[day] = [sh["id"] for sh in shifts]
                    elif raw.lower() in ("no","false","0",""):
                        availability[day] = []
                    else:
                        availability[day] = avail_for_shifts(raw, shifts)
                workers.append({"name": name, "email": "", "availability": availability})
            if workers:
                return workers, None

    return [], (
        "Could not detect availability columns. "
        "Expected columns like 'Monday availability (8:30-4:30)', "
        "'Tuesday availability', etc., or a Name column with day columns."
    )


# ─────────────────────────────────────────────────────────────────────────────
# SCHEDULING  — globally fair min-load assignment
# ─────────────────────────────────────────────────────────────────────────────

def generate_schedule(workers, include_saturday=False, max_per_worker=None):
    """
    Build a weekly front-desk schedule targeting 2 RAAs per shift.

    Strategy:
      1. Enumerate every (day, shift) slot.
      2. For each slot, build the eligible worker list.
      3. Sort slots by fewest eligible first (most-constrained first).
      4. Run TWO full passes, each assigning 1 worker per slot using the
         lowest-normalised-load eligible worker — so every slot gets 2 RAAs
         when possible, distributed as fairly as possible.

    Returns: {day: {shift_id: [worker_names]}}
    """
    days_to_use = WEEKDAYS + (["Saturday"] if include_saturday else [])

    # Build flat slot list
    slots = []
    for day in days_to_use:
        shifts = SATURDAY_SHIFTS if day == "Saturday" else WEEKDAY_SHIFTS
        for sh in shifts:
            slots.append((day, sh["id"]))

    # Build eligibility map: slot -> [worker names]
    def eligible_for(day, shift_id):
        return [
            w["name"]
            for w in workers
            if shift_id in w["availability"].get(day, [])
        ]

    slot_eligible = {s: eligible_for(*s) for s in slots}

    # Count total eligible slots per worker (for normalised load)
    eligibility_count = defaultdict(int)
    for names in slot_eligible.values():
        for n in names:
            eligibility_count[n] += 1

    count = {w["name"]: 0 for w in workers}

    def score(name):
        total = max(1, eligibility_count[name])
        return (count[name] / total, count[name], name)

    def pick_one(eligible, already_assigned):
        """Pick the lowest-load eligible worker not already on this slot.
        Pool is shuffled first so ties resolve randomly each run."""
        pool = [n for n in eligible if n not in already_assigned]
        if max_per_worker:
            pool = [n for n in pool if count.get(n, 0) < max_per_worker]
        if not pool:
            return None
        random.shuffle(pool)
        return min(pool, key=score)

    # Sort slots: fewest eligible first, shuffled within same-size groups
    random.shuffle(slots)
    sorted_slots = sorted(slots, key=lambda s: len(slot_eligible[s]))

    schedule = {day: {} for day in days_to_use}
    for day in days_to_use:
        for sh in (SATURDAY_SHIFTS if day == "Saturday" else WEEKDAY_SHIFTS):
            schedule[day][sh["id"]] = []

    # Two passes — each pass assigns 1 worker per slot
    for _pass in range(2):
        for day, shift_id in sorted_slots:
            eligible = slot_eligible[(day, shift_id)]
            already  = schedule[day][shift_id]
            picked   = pick_one(eligible, already)
            if picked:
                schedule[day][shift_id].append(picked)
                count[picked] += 1

    return schedule, count


# ─────────────────────────────────────────────────────────────────────────────
# HTML
# ─────────────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>CARS</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:ital,wght@0,300;0,400;0,500;1,300&family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;1,9..40,300&display=swap" rel="stylesheet"/>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}

:root{
  --bg:#0f1117;
  --surface:#181c27;
  --surface2:#1f2435;
  --border:#2a2f42;
  --accent:#4f7fff;
  --accent2:#7c5cfc;
  --green:#3ecf8e;
  --amber:#f59e0b;
  --red:#f87171;
  --text:#e8eaf2;
  --muted:#6b7280;
  --mono:'DM Mono',monospace;
  --sans:'DM Sans',system-ui,sans-serif;
}

body{font-family:var(--sans);background:var(--bg);color:var(--text);font-size:14px;line-height:1.6;min-height:100vh;}

/* ── Layout ── */
.app{display:grid;grid-template-columns:220px 1fr;min-height:100vh;}

.sidebar{background:var(--surface);border-right:1px solid var(--border);padding:28px 0;display:flex;flex-direction:column;position:sticky;top:0;height:100vh;overflow-y:auto;}

.logo{padding:0 24px 28px;border-bottom:1px solid var(--border);margin-bottom:20px;}
.logo-title{font-family:var(--mono);font-size:13px;font-weight:500;letter-spacing:.12em;text-transform:uppercase;color:var(--accent);}
.logo-sub{font-size:11px;color:var(--muted);margin-top:3px;font-family:var(--mono);}

.nav-item{display:flex;align-items:center;gap:10px;padding:9px 24px;font-size:13px;color:var(--muted);cursor:pointer;transition:all .12s;border-left:2px solid transparent;}
.nav-item:hover{color:var(--text);background:var(--surface2);}
.nav-item.active{color:var(--accent);border-left-color:var(--accent);background:rgba(79,127,255,.08);}
.nav-num{font-family:var(--mono);font-size:11px;width:18px;height:18px;border-radius:50%;background:var(--border);display:flex;align-items:center;justify-content:center;flex-shrink:0;}
.nav-item.active .nav-num{background:var(--accent);color:#fff;}
.nav-item.done .nav-num{background:var(--green);color:#000;}

.main{padding:40px 48px 80px;max-width:900px;}

/* ── Headings ── */
.page-head{margin-bottom:32px;}
.page-head h2{font-size:22px;font-weight:500;letter-spacing:-.01em;}
.page-head p{font-size:13px;color:var(--muted);margin-top:6px;}

/* ── Panes ── */
.pane{display:none;}.pane.active{display:block;}

/* ── Cards / sections ── */
.card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:24px;margin-bottom:20px;}
.card-title{font-family:var(--mono);font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-bottom:16px;}

/* ── Drop zone ── */
.dz{border:1px dashed var(--border);border-radius:6px;padding:36px 24px;text-align:center;cursor:pointer;transition:all .15s;background:transparent;}
.dz:hover{border-color:var(--accent);background:rgba(79,127,255,.04);}
.dz.over{border-color:var(--accent);background:rgba(79,127,255,.08);}
.dz.loaded{border-color:var(--green);border-style:solid;background:rgba(62,207,142,.05);}
.dz-icon{font-size:28px;margin-bottom:10px;display:block;}
.dz-label{font-size:13px;color:var(--muted);}
.dz.loaded .dz-label{color:var(--green);}
.dz-hint{font-size:11px;color:var(--border);margin-top:4px;font-family:var(--mono);}
.dz.loaded .dz-hint{color:rgba(62,207,142,.6);}

/* ── Buttons ── */
.btn{display:inline-flex;align-items:center;gap:8px;padding:9px 18px;border-radius:5px;font-size:12px;font-family:var(--mono);letter-spacing:.06em;text-transform:uppercase;cursor:pointer;border:none;transition:all .12s;}
.btn-primary{background:var(--accent);color:#fff;}
.btn-primary:hover{background:#3a6ee8;}
.btn-ghost{background:var(--surface2);color:var(--text);border:1px solid var(--border);}
.btn-ghost:hover{border-color:var(--accent);color:var(--accent);}
.btn-green{background:var(--green);color:#000;}
.btn-green:hover{background:#2db878;}

/* ── Toggle ── */
.toggle-row{display:flex;align-items:center;gap:12px;font-size:13px;}
.toggle{position:relative;width:36px;height:20px;cursor:pointer;}
.toggle input{opacity:0;width:0;height:0;}
.toggle-track{position:absolute;inset:0;background:var(--border);border-radius:20px;transition:.2s;}
.toggle input:checked+.toggle-track{background:var(--accent);}
.toggle-thumb{position:absolute;top:3px;left:3px;width:14px;height:14px;background:#fff;border-radius:50%;transition:.2s;}
.toggle input:checked~.toggle-thumb{left:19px;}

/* ── Number input ── */
.field{display:flex;flex-direction:column;gap:6px;}
.field label{font-size:10px;font-family:var(--mono);text-transform:uppercase;letter-spacing:.1em;color:var(--muted);}
input[type=number]{background:var(--surface2);border:1px solid var(--border);border-radius:4px;padding:7px 12px;font-size:13px;color:var(--text);outline:none;font-family:var(--mono);width:120px;}
input[type=number]:focus{border-color:var(--accent);}

/* ── Messages ── */
.msg{font-size:12px;font-family:var(--mono);margin-top:8px;min-height:15px;}
.msg-err{color:var(--red);}
.msg-ok{color:var(--green);}

/* ── Stats ── */
.stats-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--border);border:1px solid var(--border);border-radius:6px;overflow:hidden;margin-bottom:28px;}
.stat{background:var(--surface);padding:18px 20px;}
.stat-val{font-size:28px;font-family:var(--mono);font-weight:300;line-height:1;}
.stat-lbl{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;margin-top:6px;font-family:var(--mono);}
.stat-val.green{color:var(--green);}
.stat-val.red{color:var(--red);}
.stat-val.amber{color:var(--amber);}

/* ── Schedule grid ── */
.week-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:32px;}
.week-grid.with-sat{grid-template-columns:repeat(6,1fr);}
.day-col{}
.day-head{font-family:var(--mono);font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);padding:8px 0 10px;border-bottom:1px solid var(--border);margin-bottom:10px;}
.shift-block{background:var(--surface);border:1px solid var(--border);border-radius:5px;padding:10px 12px;margin-bottom:8px;border-left:3px solid var(--border);}
.shift-block.staffed{border-left-color:var(--green);}
.shift-block.partial{border-left-color:var(--amber);}
.shift-block.empty{border-left-color:var(--red);}
.shift-time{font-family:var(--mono);font-size:10px;color:var(--muted);margin-bottom:6px;}
.shift-workers{display:flex;flex-direction:column;gap:4px;}
.worker-chip{display:inline-block;font-size:11px;background:rgba(79,127,255,.15);color:var(--accent);border-radius:3px;padding:2px 7px;font-family:var(--mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%;}
.empty-slot{font-size:11px;color:var(--red);font-family:var(--mono);}

/* ── Distribution bars ── */
.dist-table{margin-top:8px;}
.dist-row{display:flex;align-items:center;gap:12px;padding:7px 0;border-bottom:1px solid var(--border);font-size:12px;}
.dist-row:last-child{border-bottom:none;}
.dist-name{min-width:160px;font-family:var(--mono);font-size:11px;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.dist-bar-bg{flex:1;background:var(--border);border-radius:2px;height:3px;}
.dist-bar-fill{background:var(--accent);border-radius:2px;height:3px;transition:width .3s;}
.dist-count{min-width:48px;text-align:right;font-family:var(--mono);font-size:11px;color:var(--muted);}

/* ── Review worker cards ── */
.worker-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px;}
.wcard{background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:12px 14px;}
.wcard-name{font-size:13px;font-weight:500;margin-bottom:8px;}
.wcard-slots{display:flex;flex-wrap:wrap;gap:3px;}
.slot-pill{font-size:10px;font-family:var(--mono);background:var(--surface2);color:var(--muted);border-radius:3px;padding:2px 6px;}
.slot-pill.has{background:rgba(79,127,255,.12);color:var(--accent);}

/* ── Actions bar ── */
.actions{display:flex;gap:10px;align-items:center;margin-bottom:24px;flex-wrap:wrap;}

/* ── Legend ── */
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:11px;font-family:var(--mono);color:var(--muted);margin-bottom:20px;}
.ldot{width:8px;height:8px;border-radius:1px;display:inline-block;margin-right:5px;}

/* ── Tabs (within schedule pane) ── */
.stabs{display:flex;gap:0;border-bottom:1px solid var(--border);margin-bottom:24px;}
.stab{padding:9px 16px;font-size:11px;font-family:var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--muted);cursor:pointer;border-bottom:2px solid transparent;transition:all .12s;}
.stab:hover{color:var(--text);}
.stab.active{color:var(--accent);border-bottom-color:var(--accent);}

.notebox{background:rgba(79,127,255,.08);border:1px solid rgba(79,127,255,.2);border-radius:5px;padding:12px 16px;font-size:12px;color:var(--muted);margin-bottom:20px;font-family:var(--mono);}
</style>
</head>
<body>
<div class="app">

  <!-- Sidebar -->
  <nav class="sidebar">
    <div class="logo">
      <div class="logo-title">CARS</div>
      <div class="logo-sub">Carolyn's Assignment & RAA System</div>
    </div>
    <div class="nav-item active" onclick="showPane('upload')" id="nav-upload">
      <span class="nav-num">1</span> Upload
    </div>
    <div class="nav-item" onclick="tryGoReview()" id="nav-review">
      <span class="nav-num">2</span> Review
    </div>
    <div class="nav-item" onclick="tryGoSchedule()" id="nav-schedule">
      <span class="nav-num">3</span> Schedule
    </div>
  </nav>

  <!-- Main -->
  <main class="main">

    <!-- ── Pane 1: Upload ── -->
    <div class="pane active" id="pane-upload">
      <div class="page-head">
        <h2>Upload availability</h2>
        <p>Drop in the Google Form responses CSV to get started.</p>
      </div>

      <div class="card">
        <div class="card-title">Availability CSV</div>
        <div class="dz" id="dz-workers">
          <span class="dz-icon">📋</span>
          <div class="dz-label" id="dz-label">Drop file here or click to browse</div>
          <div class="dz-hint" id="dz-hint">Google Form responses (.csv)</div>
        </div>
        <input type="file" id="fi-workers" accept=".csv" style="display:none;"/>
        <div class="msg msg-err" id="err-workers"></div>
        <div class="msg msg-ok"  id="ok-workers"></div>
      </div>

      <div class="card">
        <div class="card-title">Options</div>
        <div style="display:flex;flex-direction:column;gap:18px;">
          <div class="toggle-row">
            <label class="toggle">
              <input type="checkbox" id="inc-saturday"/>
              <div class="toggle-track"></div>
              <div class="toggle-thumb"></div>
            </label>
            <span>Include Saturday shifts (8:30 AM–1:00 PM)</span>
          </div>
          <div class="field">
            <label>Max shifts per worker (optional)</label>
            <input type="number" id="max-shifts" placeholder="No limit" min="1"/>
          </div>
        </div>
      </div>

      <div style="display:flex;gap:10px;align-items:center;">
        <button class="btn btn-primary" onclick="proceedToReview()">Continue to Review →</button>
      </div>
    </div>

    <!-- ── Pane 2: Review ── -->
    <div class="pane" id="pane-review">
      <div class="page-head">
        <h2>Review workers</h2>
        <p>Confirm the parsed availability before generating the schedule.</p>
      </div>
      <div class="notebox" id="review-summary">Loading…</div>
      <div class="worker-grid" id="worker-cards"></div>
      <div style="margin-top:24px;display:flex;gap:10px;">
        <button class="btn btn-primary" onclick="goSchedule()">Generate Schedule →</button>
        <button class="btn btn-ghost" onclick="showPane('upload')">← Back</button>
      </div>
    </div>

    <!-- ── Pane 3: Schedule ── -->
    <div class="pane" id="pane-schedule">
      <div class="page-head">
        <h2>Weekly schedule</h2>
        <p>Generated shift assignments. Optimised for fair distribution.</p>
      </div>

      <div class="actions">
        <button class="btn btn-primary" onclick="generateSchedule()">↻ Regenerate</button>
        <button class="btn btn-green"   onclick="exportCSV()">↓ Export CSV</button>
        <button class="btn btn-ghost"   onclick="showPane('review')">← Back</button>
      </div>

      <div class="msg msg-err" id="gen-err"></div>

      <div id="stats-area"></div>

      <div class="stabs">
        <div class="stab active" onclick="setSchedTab('grid')"  id="stab-grid">Schedule Grid</div>
        <div class="stab"        onclick="setSchedTab('dist')"  id="stab-dist">Distribution</div>
      </div>

      <div id="tab-grid">
        <div class="legend">
          <span><span class="ldot" style="background:var(--green)"></span>2 RAAs</span>
          <span><span class="ldot" style="background:var(--amber)"></span>1 RAA only</span>
          <span><span class="ldot" style="background:var(--red)"></span>Unassigned</span>
        </div>
        <div id="schedule-grid"></div>
      </div>
      <div id="tab-dist" style="display:none;">
        <div id="dist-out"></div>
      </div>
    </div>

  </main>
</div>

<script>
let workers=[], scheduleResult={}, countResult={};
const WEEKDAY_SHIFTS=[
  {id:"A",label:"8:30–10:00 AM"},
  {id:"B",label:"10:00 AM–12:00 PM"},
  {id:"C",label:"12:00–2:00 PM"},
  {id:"D",label:"2:00–4:30 PM"},
];
const SAT_SHIFTS=[
  {id:"A",label:"8:30–10:00 AM"},
  {id:"B",label:"10:00 AM–1:00 PM"},
];
const WEEKDAYS=["Monday","Tuesday","Wednesday","Thursday","Friday"];

function showPane(p){
  document.querySelectorAll('.pane').forEach(el=>el.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(el=>el.classList.remove('active'));
  document.getElementById('pane-'+p).classList.add('active');
  document.getElementById('nav-'+p).classList.add('active');
}
function tryGoReview(){if(!workers.length){alert('Upload a CSV first.');return;}renderReview();showPane('review');}
function tryGoSchedule(){if(!workers.length){alert('Upload a CSV first.');return;}generateSchedule();showPane('schedule');}

// ── File upload ──
const dz=document.getElementById('dz-workers');
const fi=document.getElementById('fi-workers');
dz.addEventListener('click',()=>fi.click());
dz.addEventListener('dragover',e=>{e.preventDefault();dz.classList.add('over');});
dz.addEventListener('dragleave',()=>dz.classList.remove('over'));
dz.addEventListener('drop',e=>{e.preventDefault();dz.classList.remove('over');if(e.dataTransfer.files[0])handleFile(e.dataTransfer.files[0]);});
fi.addEventListener('change',()=>{if(fi.files[0])handleFile(fi.files[0]);});

function handleFile(file){
  const r=new FileReader();
  r.onload=e=>{
    const incSat=document.getElementById('inc-saturday').checked;
    fetch('/parse_workers',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({csv:e.target.result,include_saturday:incSat})
    }).then(r=>r.json()).then(data=>{
      document.getElementById('err-workers').textContent=data.error||'';
      if(!data.error){
        workers=data.workers;
        document.getElementById('ok-workers').textContent=`${workers.length} workers loaded.`;
        dz.classList.add('loaded');
        document.getElementById('dz-label').textContent=file.name;
        document.getElementById('dz-hint').textContent='Loaded ✓';
        document.getElementById('nav-review').classList.add('done');
      }
    });
  };
  r.readAsText(file);
}

function proceedToReview(){
  if(!workers.length){alert('Upload a CSV first.');return;}
  renderReview();showPane('review');
}

function renderReview(){
  const incSat=document.getElementById('inc-saturday').checked;
  const days=incSat?[...WEEKDAYS,'Saturday']:WEEKDAYS;
  const with_avail=workers.filter(w=>days.some(d=>(w.availability[d]||[]).length>0));
  document.getElementById('review-summary').textContent=
    `${workers.length} workers parsed — ${with_avail.length} with at least one available shift.`;

  let html='';
  workers.forEach(w=>{
    let slots='';
    days.forEach(d=>{
      const shifts=d==='Saturday'?SAT_SHIFTS:WEEKDAY_SHIFTS;
      const avail=w.availability[d]||[];
      shifts.forEach(sh=>{
        const has=avail.includes(sh.id);
        if(has) slots+=`<span class="slot-pill has">${d.slice(0,3)} ${sh.label}</span>`;
      });
    });
    if(!slots) slots='<span class="slot-pill" style="color:var(--red)">No availability</span>';
    html+=`<div class="wcard"><div class="wcard-name">${w.name}</div><div class="wcard-slots">${slots}</div></div>`;
  });
  document.getElementById('worker-cards').innerHTML=html;
}

function goSchedule(){generateSchedule();showPane('schedule');}

function generateSchedule(){
  const err=document.getElementById('gen-err');
  if(!workers.length){err.textContent='Upload workers first.';return;}
  err.textContent='';
  const incSat=document.getElementById('inc-saturday').checked;
  const maxS=parseInt(document.getElementById('max-shifts').value)||null;
  fetch('/generate',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({workers,include_saturday:incSat,max_per_worker:maxS})
  }).then(r=>r.json()).then(data=>{
    if(data.error){err.textContent=data.error;return;}
    scheduleResult=data.schedule;
    countResult=data.count;
    renderSchedule(incSat);
    document.getElementById('nav-schedule').classList.add('done');
  });
}

function renderSchedule(incSat){
  const days=incSat?[...WEEKDAYS,'Saturday']:WEEKDAYS;
  let totalSlots=0,staffedSlots=0,partialSlots=0,emptySlots=0;

  // Stats
  days.forEach(d=>{
    const shifts=d==='Saturday'?SAT_SHIFTS:WEEKDAY_SHIFTS;
    shifts.forEach(sh=>{
      totalSlots++;
      const assigned=(scheduleResult[d]||{})[sh.id]||[];
      if(assigned.length>=2) staffedSlots++;
      else if(assigned.length===1) partialSlots++;
      else emptySlots++;
    });
  });
  const totalAssign=Object.values(countResult).reduce((a,b)=>a+b,0);
  document.getElementById('stats-area').innerHTML=`
    <div class="stats-grid">
      <div class="stat"><div class="stat-val">${totalSlots}</div><div class="stat-lbl">Total slots</div></div>
      <div class="stat"><div class="stat-val green">${staffedSlots}</div><div class="stat-lbl">Fully staffed (2)</div></div>
      <div class="stat"><div class="stat-val amber">${partialSlots}</div><div class="stat-lbl">1 RAA only</div></div>
      <div class="stat"><div class="stat-val ${emptySlots>0?'red':''}">${emptySlots}</div><div class="stat-lbl">Unassigned</div></div>
    </div>`;

  // Grid
  let gridCls='week-grid'+(incSat?' with-sat':'');
  let gridHtml=`<div class="${gridCls}">`;
  days.forEach(d=>{
    const shifts=d==='Saturday'?SAT_SHIFTS:WEEKDAY_SHIFTS;
    gridHtml+=`<div class="day-col"><div class="day-head">${d}</div>`;
    shifts.forEach(sh=>{
      const assigned=(scheduleResult[d]||{})[sh.id]||[];
      let cls=assigned.length>=2?'staffed':assigned.length===1?'partial':'empty';
      let chips=assigned.map(n=>`<div class="worker-chip" title="${n}">${n.split(' ')[0]}</div>`).join('');
      if(!chips) chips='<div class="empty-slot">unassigned</div>';
      gridHtml+=`<div class="shift-block ${cls}">
        <div class="shift-time">${sh.label}</div>
        <div class="shift-workers">${chips}</div>
      </div>`;
    });
    gridHtml+='</div>';
  });
  gridHtml+='</div>';
  document.getElementById('schedule-grid').innerHTML=gridHtml;

  // Distribution
  const sorted=Object.entries(countResult).sort((a,b)=>b[1]-a[1]);
  const maxC=sorted.length?sorted[0][1]:1;
  let distHtml='<div class="dist-table">';
  sorted.forEach(([name,c])=>{
    const pct=maxC?Math.round(c/maxC*100):0;
    distHtml+=`<div class="dist-row">
      <div class="dist-name">${name}</div>
      <div class="dist-bar-bg"><div class="dist-bar-fill" style="width:${pct}%"></div></div>
      <div class="dist-count">${c} shift${c!==1?'s':''}</div>
    </div>`;
  });
  const none=workers.filter(w=>!countResult[w.name]||countResult[w.name]===0).map(w=>w.name).sort();
  if(none.length){
    distHtml+=`<div style="font-size:10px;font-family:var(--mono);color:var(--muted);padding:10px 0 4px;text-transform:uppercase;letter-spacing:.08em;">Not assigned</div>`;
    none.forEach(n=>{distHtml+=`<div class="dist-row"><div class="dist-name" style="color:var(--muted)">${n}</div><div class="dist-bar-bg"></div><div class="dist-count" style="color:var(--muted)">0</div></div>`;});
  }
  distHtml+='</div>';
  document.getElementById('dist-out').innerHTML=distHtml;
}

function setSchedTab(t){
  ['grid','dist'].forEach(x=>{
    document.getElementById('tab-'+x).style.display=x===t?'block':'none';
    document.getElementById('stab-'+x).classList.toggle('active',x===t);
  });
}

function exportCSV(){
  if(!Object.keys(scheduleResult).length){document.getElementById('gen-err').textContent='Generate a schedule first.';return;}
  const incSat=document.getElementById('inc-saturday').checked;
  const days=incSat?[...WEEKDAYS,'Saturday']:WEEKDAYS;
  let csv='Day,Shift,Time,Assigned Workers\n';
  days.forEach(d=>{
    const shifts=d==='Saturday'?SAT_SHIFTS:WEEKDAY_SHIFTS;
    shifts.forEach(sh=>{
      const assigned=((scheduleResult[d]||{})[sh.id]||[]).join('; ');
      csv+=`"${d}","${sh.id}","${sh.label}","${assigned}"\n`;
    });
  });
  const a=document.createElement('a');
  a.href='data:text/csv;charset=utf-8,'+encodeURIComponent(csv);
  a.download='desk_schedule.csv';a.click();
}
</script>
</body>
</html>
"""

# ─────────────────────────────────────────────────────────────────────────────
# SERVER
# ─────────────────────────────────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(HTML.encode("utf-8"))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length)
        path   = urlparse(self.path).path

        def respond(obj):
            data = json.dumps(obj).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        if path == "/parse_workers":
            try:
                p = json.loads(body)
                workers, err = parse_worker_csv(
                    p["csv"],
                    include_saturday=p.get("include_saturday", False)
                )
                if err:
                    respond({"error": err, "workers": []})
                else:
                    respond({"workers": workers})
            except Exception as e:
                respond({"error": str(e), "workers": []})

        elif path == "/generate":
            try:
                p = json.loads(body)
                sched, count = generate_schedule(
                    p["workers"],
                    include_saturday=p.get("include_saturday", False),
                    max_per_worker=p.get("max_per_worker")
                )
                respond({"schedule": sched, "count": count})
            except Exception as e:
                respond({"error": str(e)})

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args): pass



if __name__ == "__main__":

    print(f"CARS  →  http://localhost:{PORT}")
    print("Press Ctrl+C to stop.\n")
    with socketserver.TCPServer(("0.0.0.0", PORT), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
