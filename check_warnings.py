#!/usr/bin/env python3
"""BoM warning alerts, end to end, in one command.

Fetches BoM's state warnings RSS feeds and tells the user's phone (via ntfy)
about the warnings they follow. Each region below has its own feed, its own
set of warning types, and its own idea of which events are worth an alert:

  VIC  - all of Victoria. Severe Weather, Severe Thunderstorm, Fire Weather and
         Frost warnings. Alerts on NEW, UPDATED (BoM reissued it: the pubDate
         moved on or the headline changed) and, for Severe Weather and Severe
         Thunderstorm, ENDED (cancelled, finalised or lapsed).
  SYD  - Sydney only: warnings naming the Sydney Metropolitan forecast
         district, in the NSW feed. Severe Weather and Severe Thunderstorm,
         NEW, UPDATED and ENDED.
  BNE  - Brisbane only: warnings naming the Southeast Coast forecast district
         (the district Brisbane sits in), in the QLD feed. Severe Weather and
         Severe Thunderstorm, NEW, UPDATED and ENDED.

Each warning is tracked by BoM's product link plus warning type, not by its
text, so a headline that changes (districts added or dropped) is one UPDATED
alert, never a cancel plus a new warning. A headline that bundles several
warnings joined by " / " is split into clauses and any clause mentioning
"final" or "cancel" is dropped one at a time, never the whole headline (same
rule as activeWarningTitle in the dashboard's lib/warning-titles.ts).

If a warning of a type ends in the same run that a new one of that type
appears under a different product, it is treated as replaced and no ENDED
alert is sent, since the new one alerts as NEW.

Regions are independent: if one feed fails to load, only that region is
skipped (its saved state is left alone, so nothing is wrongly "ended") and
the others carry on. A region added to this file later is baselined silently
the first time it is seen, so the warnings already in force are not announced.

State: seen.json in $WX_STATE_DIR (default: beside this script),
{key: {"title", "pub", "r"}, "_regions": [codes already baselined]}. Keys for
VIC are the bare product link (as they always were); other regions prefix
their code. The older list format is migrated in place without alerting.
First run (no state file) only records a baseline. If a send fails, that change
is left out of the saved state so it is retried next run.

Exit codes: 0 all feeds ok, 1 at least one feed failed. State for the feeds
that did load is still saved.
Flags (manual use only): --baseline writes state, sends nothing; --dry-run
prints what would be sent and changes nothing.
"""
import html
import json
import os
import re
import sys
import urllib.request

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.6 Safari/605.1.15"
)
SEVERE = ["severe weather warning", "severe thunderstorm warning"]
ALL_TYPES = SEVERE + ["fire weather warning", "frost warning"]


def _norm(s: str) -> str:
    """Letters only, lower case: 'South East Coast' == 'Southeast Coast'."""
    return re.sub(r"[^a-z]", "", s.lower())


REGIONS = [
    {
        "code": "VIC",
        "label": "VIC",
        "header": "Victorian warning",
        "url": "https://www.bom.gov.au/fwo/IDZ00059.warnings_vic.xml",
        "types": ALL_TYPES,
        "end_types": SEVERE,
        "updated": True,
        "match": lambda clause: True,
    },
    {
        "code": "SYD",
        "label": "Sydney",
        "header": "Sydney warning",
        "url": "https://www.bom.gov.au/fwo/IDZ00054.warnings_nsw.xml",
        "types": SEVERE,
        "end_types": SEVERE,
        "updated": True,
        "match": lambda clause: "sydneymetro" in _norm(clause),
    },
    {
        "code": "BNE",
        "label": "Brisbane",
        "header": "Brisbane warning",
        "url": "https://www.bom.gov.au/fwo/IDZ00056.warnings_qld.xml",
        "types": SEVERE,
        "end_types": SEVERE,
        "updated": True,
        "match": lambda clause: "southeastcoast" in _norm(clause),
    },
]
BY_CODE = {r["code"]: r for r in REGIONS}
MAX_INDIVIDUAL = 5
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("WX_STATE_DIR", HERE)
STATE = os.path.join(STATE_DIR, "seen.json")
TOPIC_FILE = os.path.join(os.path.dirname(HERE), "ntfy-topic.txt")


def fetch_region(cfg: dict) -> dict:
    """{key: {"title", "pub", "r"}} for every active matching warning."""
    req = urllib.request.Request(
        cfg["url"],
        headers={
            "User-Agent": UA,
            "Accept": "application/xml,text/xml,*/*",
            "Referer": "https://www.bom.gov.au/",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        xml = r.read().decode("utf-8", errors="replace")
    if "<channel" not in xml:
        raise ValueError("not a BoM RSS feed")
    prefix = "" if cfg["code"] == "VIC" else cfg["code"] + ":"
    out: dict = {}
    for item in re.findall(r"<item>([\s\S]*?)</item>", xml):
        m = re.search(r"<title>([\s\S]*?)</title>", item)
        if not m:
            continue
        link = re.search(r"<link>([\s\S]*?)</link>", item)
        guid = re.search(r"<guid[^>]*>([\s\S]*?)</guid>", item)
        pub = re.search(r"<pubDate>([\s\S]*?)</pubDate>", item)
        ident = (link or guid).group(1).strip() if (link or guid) else None
        pub_s = pub.group(1).strip() if pub else ""
        t = re.sub(r"^<!\[CDATA\[|\]\]>$", "", m.group(1).strip())
        t = html.unescape(t)
        t = re.sub(r"^\d{1,2}/\d{2}:\d{2}\s+[A-Z]+\s+", "", t)
        t = re.sub(r"\s+", " ", t).strip()
        for clause in t.split(" / "):
            clause = clause.strip().rstrip(".").strip()
            if not clause or re.search(r"final|cancel", clause, re.I):
                continue
            kind = next((k for k in cfg["types"] if k in clause.lower()), None)
            if not kind or not cfg["match"](clause):
                continue
            key = f"{prefix}{ident or clause}|{kind}"
            if key in out:
                if clause not in out[key]["title"]:
                    out[key]["title"] += " / " + clause
            else:
                out[key] = {"title": clause, "pub": pub_s, "r": cfg["code"]}
    return out


def kind_of(entry: dict) -> str:
    title = entry["title"].lower()
    return next((k for k in ALL_TYPES if k in title), "")


def region_of(entry: dict) -> str:
    return entry.get("r", "VIC")


def ntfy(message: str, title: str) -> bool:
    try:
        topic = os.environ.get("NTFY_TOPIC") or open(TOPIC_FILE).read().strip()
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": "high", "Tags": "warning"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception as e:  # noqa: BLE001
        print(f"ntfy send failed: {type(e).__name__}", file=sys.stderr)
        return False


def main() -> int:
    baseline = "--baseline" in sys.argv
    dry = "--dry-run" in sys.argv

    current: dict = {}
    ok_regions = set()
    failed = False
    for cfg in REGIONS:
        try:
            current.update(fetch_region(cfg))
            ok_regions.add(cfg["code"])
        except Exception as e:  # noqa: BLE001
            failed = True
            print(f"{cfg['code']} fetch failed: {type(e).__name__}: {e}", file=sys.stderr)
    if not ok_regions:
        return 1

    first_run = not os.path.exists(STATE)
    stored: dict = {}
    baselined = set()
    if not first_run:
        try:
            raw = json.load(open(STATE))
            if isinstance(raw, list):
                # Oldest format: just VIC titles. Adopt them at their current
                # pubDate so the upgrade itself does not alert.
                stored = {k: dict(v) for k, v in current.items() if v["title"] in raw}
                baselined = {"VIC"}
            else:
                baselined = set(raw.get("_regions", ["VIC"]))
                stored = {k: v for k, v in raw.items() if isinstance(v, dict)}
        except Exception:  # noqa: BLE001
            stored, baselined, first_run = {}, set(), True

    # Regions announced silently this run: all of them on a first run or with
    # --baseline, and any region not yet baselined (a newly added one).
    quiet = set(ok_regions) if (first_run or baseline) else {c for c in ok_regions if c not in baselined}

    events = []  # (verb, key, text, region)
    for code in sorted(ok_regions):
        cfg = BY_CODE[code]
        cur = {k: v for k, v in current.items() if region_of(v) == code}
        old_all = {k: v for k, v in stored.items() if region_of(v) == code}
        for key, c in cur.items():
            old = old_all.get(key)
            if old is None:
                events.append(("New", key, c["title"], code))
            elif cfg["updated"] and (old.get("pub") != c["pub"] or old.get("title") != c["title"]):
                events.append(("Updated", key, c["title"], code))
        new_kinds = {kind_of(cur[k]) for v, k, _, r in events if v == "New" and r == code}
        for key, old in old_all.items():
            if key in cur:
                continue
            kind = kind_of(old)
            if kind in cfg["end_types"] and kind not in new_kinds:
                events.append(("Ended", key, old["title"], code))

    to_save = {k: v for k, v in stored.items() if region_of(v) not in ok_regions}
    to_save.update(current)

    loud = [e for e in events if e[3] not in quiet]
    sent = 0
    if loud:
        if len(loud) <= MAX_INDIVIDUAL:
            batches = [
                ([e], f"{e[0]} {BY_CODE[e[3]]['label']} {e[2]}"[:200], BY_CODE[e[3]]["header"])
                for e in loud
            ]
        else:
            msg = f"{len(loud)} warning changes: " + "; ".join(
                f"{v} {BY_CODE[r]['label']} {t}" for v, _, t, r in loud
            )
            batches = [(loud, msg[:200], "Weather warnings")]
        for group, msg, header in batches:
            ok = True
            if dry:
                print(f"DRY-RUN would send: {msg}")
            else:
                ok = ntfy(msg, header)
            if ok:
                sent += len(group)
                continue
            for verb, key, _, _ in group:  # retry next run
                if verb == "New":
                    to_save.pop(key, None)
                else:  # Updated -> keep old record; Ended -> keep it active
                    to_save[key] = stored[key]

    if not dry:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE + ".tmp"
        payload = dict(to_save)
        payload["_regions"] = sorted(baselined | ok_regions)
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, STATE)

    per = ", ".join(
        f"{c} {sum(1 for v in current.values() if region_of(v) == c)}" for c in sorted(ok_regions)
    )
    note = f"baseline recorded for {', '.join(sorted(quiet))}; " if quiet else ""
    print(f"{per} matching; {len(events)} change(s); {note}{sent} alert(s) sent")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
