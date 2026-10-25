#!/usr/bin/env python3
"""Melbourne obs gust alerts, end to end, in one pre-approved command.

Fetches BoM's Melbourne obs table, finds stations whose peak gust is at or
above THRESHOLD_KMH and higher than the last peak alerted for that station,
sends those to ntfy, and updates the state file. Doing it all here means the
scheduled run needs no Read/Write tool calls (which prompt for files under
~/.claude) and only this one command.

Mirrors parseTable() in the dashboard's app/api/melb-obs/route.ts: one
<tr class="rowleftcolumn"> per station, name in the <th>, and the <td> cells in
order, with cell 8 = current gust (km/h) and cell 16 = the day's maximum gust
(km/h) followed by a <small> time.

State: peaks.json in $WX_STATE_DIR (default: beside this script), {station: highest peak seen}. A lower
reading overwrites a higher one (BoM's daily maximum resets overnight), so the
next 75+ gust alerts as new. First run (no state file) only records a baseline.
If a send fails the station keeps its old stored value and is retried.

Exit codes: 0 ok, 1 fetch/parse failure (state untouched, nothing sent).
Flags (for manual use only): --baseline writes state, sends nothing;
--dry-run prints what would be sent, changes nothing.
"""
import html
import json
import os
import re
import sys
import urllib.request

THRESHOLD_KMH = 75
URL = "https://www.bom.gov.au/vic/observations/melbourne.shtml"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.6 Safari/605.1.15"
)
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("WX_STATE_DIR", HERE)
STATE = os.path.join(STATE_DIR, "peaks.json")
TOPIC_FILE = os.path.join(os.path.dirname(HERE), "ntfy-topic.txt")


def cell_text(raw: str) -> str:
    raw = re.sub(r"<small[^>]*>[\s\S]*?</small>", "", raw, flags=re.I)
    raw = re.sub(r"<[^>]+>", "", raw)
    return html.unescape(raw).replace("\xa0", " ").strip()


def small_text(raw: str):
    m = re.search(r"<small[^>]*>([\s\S]*?)</small>", raw, flags=re.I)
    if not m:
        return None
    t = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).replace("\xa0", " ").strip()
    return t or None


def num(s: str):
    m = re.match(r"^-?\d+(\.\d+)?$", s.strip())
    return float(m.group(0)) if m else None


def fetch_stations():
    req = urllib.request.Request(
        URL, headers={"User-Agent": UA, "Referer": "https://www.bom.gov.au/"}
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        page = r.read().decode("utf-8", errors="replace")
    stations = []
    for tr in re.finditer(
        r'<tr[^>]+class="rowleftcolumn"[^>]*>([\s\S]*?)</tr>', page, flags=re.I
    ):
        row = tr.group(1)
        th = re.search(r'<th[^>]+id="[^"]*-station-[^"]*"[^>]*>([\s\S]*?)</th>', row, flags=re.I)
        if not th:
            continue
        tds = re.findall(r"<td[^>]*>([\s\S]*?)</td>", row, flags=re.I)
        if len(tds) < 13:
            continue
        gust = num(cell_text(tds[8]))
        max_cell = tds[16] if len(tds) > 16 else ""
        max_gust = num(cell_text(max_cell))
        vals = [v for v in (gust, max_gust) if v is not None]
        if not vals:
            continue
        stations.append(
            {
                "station": cell_text(th.group(1)),
                "peak": max(vals),
                "time": small_text(max_cell) if max_gust is not None and max_gust >= max(vals) else None,
            }
        )
    return stations


def ntfy(message: str) -> bool:
    try:
        topic = os.environ.get("NTFY_TOPIC") or open(TOPIC_FILE).read().strip()
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={
                "Title": f"Melbourne gust {THRESHOLD_KMH}+",
                "Priority": "high",
                "Tags": "wind_face",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception as e:  # noqa: BLE001
        print(f"ntfy send failed: {type(e).__name__}", file=sys.stderr)
        return False


def fmt(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else str(n)


def main() -> int:
    baseline = "--baseline" in sys.argv
    dry = "--dry-run" in sys.argv
    try:
        stations = fetch_stations()
    except Exception as e:  # noqa: BLE001
        print(f"fetch failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    if not stations:
        print("no station rows found", file=sys.stderr)
        return 1

    first_run = not os.path.exists(STATE)
    try:
        stored = {} if first_run else json.load(open(STATE))
    except Exception:  # noqa: BLE001
        stored = {}
        first_run = True

    new_state = dict(stored)
    alerts = []  # (station, peak, time, prev)
    for s in stations:
        prev = float(stored.get(s["station"], 0))
        new_state[s["station"]] = s["peak"]
        if s["peak"] >= THRESHOLD_KMH and s["peak"] > prev:
            alerts.append((s["station"], s["peak"], s["time"], prev))

    sent = 0
    if first_run or baseline:
        alerts_to_send = []
    else:
        alerts_to_send = sorted(alerts, key=lambda a: -a[1])

    def one(a) -> str:
        name, peak, t, prev = a
        msg = f"Gust {fmt(peak)} km/h at {name}"
        if t:
            msg += f" (peak {t})"
        if prev >= THRESHOLD_KMH:
            msg += f", up from {fmt(prev)}"
        return msg

    if alerts_to_send:
        if len(alerts_to_send) <= 3:
            batches = [([a], one(a)) for a in alerts_to_send]
        else:
            top = alerts_to_send[:3]
            msg = f"{len(alerts_to_send)} stations at {THRESHOLD_KMH}+ km/h: " + "; ".join(
                f"{a[0]} {fmt(a[1])}" for a in top
            )
            batches = [(alerts_to_send, msg)]
        for group, msg in batches:
            msg = msg[:200]
            if dry:
                print(f"DRY-RUN would send: {msg}")
                ok = True
            else:
                ok = ntfy(msg)
            if ok:
                sent += len(group)
            else:
                for a in group:  # keep old value so it is retried next run
                    new_state[a[0]] = a[3]

    if not dry:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(new_state, f)
        os.replace(tmp, STATE)

    top = max(stations, key=lambda s: s["peak"])
    note = "baseline recorded, no alerts" if (first_run or baseline) else f"{sent} alert(s) sent"
    print(
        f"{len(stations)} stations, {len(alerts)} new at {THRESHOLD_KMH}+; {note}. "
        f"Strongest: {top['station']} {fmt(top['peak'])} km/h"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
