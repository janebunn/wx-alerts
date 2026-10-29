# wx-alerts

Phone alerts (via [ntfy](https://ntfy.sh)) for Victorian weather, run on GitHub Actions every 5 minutes (at :02, :07, :12, … to dodge GitHub's busiest slots) so they keep working when the computer is off.

- `check_warnings.py` — BoM's state warnings feeds, per region (see `REGIONS` at the top of the file):
  - **Victoria**: new, updated (reissued) or ended Severe Weather / Severe Thunderstorm warnings, and new or updated Fire Weather and Frost warnings.
  - **Sydney**: new, updated (reissued) and ended Severe Weather / Severe Thunderstorm warnings that name the Sydney Metropolitan forecast district.
  - **Brisbane**: the same, for the Southeast Coast forecast district.
  A region added later is baselined silently the first time it is seen.
- `check_gusts.py` — BoM's Melbourne observations table. Alerts the first time a station's peak gust reaches 75 km/h, and again each time its peak rises (`THRESHOLD_KMH` at the top of the file).
- `.github/workflows/alerts.yml` — the schedule. The "already alerted" state is kept between runs in the Actions cache.

## Setup

1. Create a GitHub repo and push this folder to it. Make it **public**: scheduled runs every 5 minutes use far more than the 2,000 free minutes a month that private repos get, and public repos are unlimited. The repo holds no secrets.
2. Repo → Settings → Secrets and variables → Actions → New repository secret: name `NTFY_TOPIC`, value your ntfy topic (the text after `https://ntfy.sh/`).
3. Actions tab → "alerts" → Run workflow once. The first run only records a baseline and sends nothing; the log should show "baseline recorded".

## Notes

- GitHub's scheduler can start runs several minutes late when it is busy, so an alert can arrive later than the warning.
- If the Actions cache is ever evicted, the next run silently records a new baseline, so a warning that was already active is not re-announced.
- Scripts need only Python 3 (standard library). Try one locally with `python3 check_gusts.py --dry-run` (set `NTFY_TOPIC` to really send; `--baseline` records state without sending).
