#!/usr/bin/env python3
"""Victorian warning alerts, end to end, in one command.

Fetches BoM's Victorian warnings RSS and tells the user's phone (via ntfy)
when a Severe Weather, Severe Thunderstorm, Fire Weather or Frost warning is

  * NEW      - not seen on the previous run,
  * UPDATED  - BoM reissued it (the item's pubDate moved on, or its headline
               changed, e.g. districts added or dropped), or
  * ENDED    - a Severe Weather or Severe Thunderstorm warning that was active
               and is no longer: BoM cancelled/finalised it or let it lapse.

Each warning is tracked by BoM's product link plus warning type, not by its
text, so an update that changes the districts in the headline is one UPDATED
alert rather than a cancel plus a new warning. The same holds for a headline
that bundles several warnings joined by " / ": clauses mentioning "final" or
"cancel" are dropped one at a time, never the whole headline (same rule as
activeWarningTitle in the dashboard's lib/warning-titles.ts).

If a warning of a type ends in the same run that a new one of that type
appears under a different product, it is treated as replaced and no ENDED
alert is sent, since the new one alerts as NEW.

State: seen.json in $WX_STATE_DIR (default: beside this script), {key: {"title", "pub"}}. An older list
format is migrated in place without alerting. First run (no state file) only
records a baseline. If a send fails, that change is left out of the saved
state so it is retried next run.

Exit codes: 0 ok, 1 fetch/parse failure (state untouched, nothing sent).
Flags (manual use only): --baseline writes state, sends nothing; --dry-run
prints what would be sent and changes nothing.
"""
import html
import json
import os
import re
import sys
import urllib.request

URL = "https://www.bom.gov.au/fwo/IDZ00059.warnings_vic.xml"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.6 Safari/605.1.15"
)
TYPES = [
    "severe weather warning",
    "severe thunderstorm warning",
    "fire weather warning",
    "frost warning",
]
# Types whose ending is worth an alert of its own.
ALERT_ON_END = {"severe weather warning", "severe thunderstorm warning"}
MAX_INDIVIDUAL = 5
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("WX_STATE_DIR", HERE)
STATE = os.path.join(STATE_DIR, "seen.json")
TOPIC_FILE = os.path.join(os.path.dirname(HERE), "ntfy-topic.txt")


def fetch_current() -> dict:
    """{key: {"title", "pub"}} for every active matching warning."""
    req = urllib.request.Request(
        URL,
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
            kind = next((k for k in TYPES if k in clause.lower()), None)
            if not kind:
                continue
            key = f"{ident or clause}|{kind}"
            if key in out:
                if clause not in out[key]["title"]:
                    out[key]["title"] += " / " + clause
            else:
                out[key] = {"title": clause, "pub": pub_s}
    return out


def kind_of(entry: dict) -> str:
    title = entry["title"].lower()
    return next((k for k in TYPES if k in title), "")


def ntfy(message: str) -> bool:
    try:
        topic = os.environ.get("NTFY_TOPIC") or open(TOPIC_FILE).read().strip()
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": "Victorian warning", "Priority": "high", "Tags": "warning"},
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
    try:
        current = fetch_current()
    except Exception as e:  # noqa: BLE001
        print(f"fetch failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    first_run = not os.path.exists(STATE)
    stored: dict = {}
    if not first_run:
        try:
            raw = json.load(open(STATE))
            if isinstance(raw, list):
                # Older format: just titles. Adopt them at their current pubDate
                # so the upgrade itself does not alert.
                stored = {k: dict(v) for k, v in current.items() if v["title"] in raw}
            else:
                stored = {k: v for k, v in raw.items() if isinstance(v, dict)}
        except Exception:  # noqa: BLE001
            stored, first_run = {}, True

    events = []  # (verb, key, text)
    for key, cur in current.items():
        old = stored.get(key)
        if old is None:
            events.append(("New", key, cur["title"]))
        elif old.get("pub") != cur["pub"] or old.get("title") != cur["title"]:
            events.append(("Updated", key, cur["title"]))

    new_kinds = {kind_of(current[k]) for v, k, _ in events if v == "New"}
    for key, old in stored.items():
        if key in current:
            continue
        kind = kind_of(old)
        if kind in ALERT_ON_END and kind not in new_kinds:
            events.append(("Ended", key, old["title"]))

    to_save = dict(current)
    sent = 0
    if events and not (first_run or baseline):
        if len(events) <= MAX_INDIVIDUAL:
            batches = [([e], f"{e[0]} VIC {e[2]}") for e in events]
        else:
            batches = [(events, f"{len(events)} VIC warning changes: " + "; ".join(f"{v} {t}" for v, _, t in events))]
        for group, msg in batches:
            msg = msg[:200]
            ok = True
            if dry:
                print(f"DRY-RUN would send: {msg}")
            else:
                ok = ntfy(msg)
            if ok:
                sent += len(group)
                continue
            for verb, key, _ in group:  # retry next run
                if verb == "New":
                    to_save.pop(key, None)
                else:  # Updated -> keep old record; Ended -> keep it active
                    to_save[key] = stored[key]

    if not dry:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(to_save, f)
        os.replace(tmp, STATE)

    note = "baseline recorded, no alerts" if (first_run or baseline) else f"{sent} alert(s) sent"
    print(f"{len(current)} matching, {len(events)} change(s); {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
