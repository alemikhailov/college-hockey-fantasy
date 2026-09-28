#!/usr/bin/env python3
"""
College Hockey Fantasy - schedule importer
==========================================

WHY THIS EXISTS
  The NCAA stats feed only publishes games at or near game day, so it cannot
  tell you who plays next week. This league is built around that question:
  fitting your roster to the calendar so you get more games than your opponent
  is the whole strategy. So the schedule is loaded up front from College
  Hockey News, which publishes the full season in advance.

  Games load with status 'scheduled' and no stats. When the NCAA feed later
  publishes a game, the stats loader finds the row already here (matching on
  date plus the two teams) and fills it in rather than creating a second copy.

WHAT IS SKIPPED
  Exhibitions and games against D-III opponents. They do not count in the
  NCAA record and they must not count here either - listing them would
  inflate the games-played count that roster planning depends on.

USAGE
    python3 import_schedule.py --selftest
    python3 import_schedule.py --file schedule.html --dry-run
    python3 import_schedule.py                       # fetch live + write
    python3 import_schedule.py --season 2026-27

CREDENTIALS - in the window you'll run from:
    $env:SUPABASE_URL="https://xxxxxxxx.supabase.co"
    $env:SUPABASE_KEY="your-service_role-key"
"""

import argparse, os, re, sys, unicodedata
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
except ImportError:
    print("Python 3.9+ required (zoneinfo)."); raise

CHN_SEASON_URL = "https://www.collegehockeynews.com/schedules/season/{season}"

# CHN's timezone abbreviations. 'AT' is Alaska - the only teams playing in it
# are Alaska and Alaska-Anchorage.
TZ = {
    "ET": "America/New_York",   "CT": "America/Chicago",
    "MT": "America/Denver",     "PT": "America/Los_Angeles",
    "AT": "America/Anchorage",  "AKT": "America/Anchorage",
    "HT": "Pacific/Honolulu",
}

# Sections whose games do not count toward anything.
SKIP_SECTION = re.compile(r"exhibition|v\.\s*d3|vs\.?\s*d3", re.I)

MONTHS = {m: i for i, m in enumerate(
    ["January","February","March","April","May","June",
     "July","August","September","October","November","December"], 1)}


# ---------------------------------------------------------------- parsing
def _txt(html):
    s = re.sub(r"<[^>]+>", "", html)
    s = s.replace("&nbsp;", " ").replace("\xa0", " ")
    s = s.replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
    return re.sub(r"\s+", " ", s).strip()


def parse_date_header(s):
    """'Friday, October 2, 2026' -> date"""
    m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", s)
    if not m or m.group(1) not in MONTHS:
        return None
    return datetime(int(m.group(3)), MONTHS[m.group(1)], int(m.group(2))).date()


def parse_start(day, status_text):
    """'6:00 ET' / '7:05 MT' -> (utc_iso, local_label). TBA -> (None, raw)."""
    m = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)?\s*([A-Z]{2,3})", status_text or "")
    if not m:
        return None, (status_text or "").strip()
    hh, mm, ampm, zone = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
    tzname = TZ.get(zone)
    if not tzname:
        return None, status_text.strip()
    if ampm:
        if ampm == "PM" and hh != 12: hh += 12
        if ampm == "AM" and hh == 12: hh = 0
    elif hh < 11:
        hh += 12                      # CHN drops AM/PM; evening is the default
    try:
        local = datetime(day.year, day.month, day.day, hh, mm,
                         tzinfo=ZoneInfo(tzname))
    except Exception:
        return None, status_text.strip()
    return local.astimezone(ZoneInfo("UTC")).isoformat(), f"{m.group(1)}:{m.group(2)} {zone}"


def parse_schedule(html):
    """-> (games, problems). One dict per countable game."""
    start = html.find("<table")
    if start < 0:
        return [], ["no <table> found - is this the schedule page?"]
    rows = re.findall(r"<tr[^>]*>.*?</tr>", html[start:], re.S)

    games, problems = [], []
    day, section, skipped = None, None, 0

    for r in rows:
        if 'class="stats-section"' in r:
            d = parse_date_header(_txt(r))
            if d:
                day, section = d, None
            continue
        if 'class="sked-header"' in r:
            section = _txt(r)
            continue
        if 'valign="top"' not in r:
            continue

        if day is None:
            continue
        if section and SKIP_SECTION.search(section):
            skipped += 1
            continue

        cells = [_txt(c) for c in re.findall(r"<td[^>]*>.*?</td>", r, re.S)]
        if len(cells) < 5:
            continue
        away, home = cells[0].strip(), cells[3].strip()
        if not away or not home or cells[2].strip().lower() not in ("at", "vs", "vs."):
            continue

        status = next((c for c in cells[5:8] if re.search(r"\d:\d\d", c)), "")
        utc, label = parse_start(day, status)

        games.append({
            "date": day.isoformat(), "away": away, "home": home,
            "start_utc": utc, "start_label": label,
            "neutral": cells[2].strip().lower().startswith("vs"),
            "section": section or "",
        })

    return games, problems, skipped


# ------------------------------------------------- team name reconciliation
TEAM_ALIASES = {
    "MASSACHUSETTS": "UMASS", "MASSLOWELL": "UMASSLOWELL",
    "MASSACHUSETTSLOWELL": "UMASSLOWELL", "LOWELL": "UMASSLOWELL",
    "CONNECTICUT": "UCONN", "NEBRASKAOMAHA": "OMAHA",
    "MINNESOTADULUTH": "MINNDULUTH", "MIAMIOH": "MIAMI",
    "ARMY": "ARMYWESTPOINT", "LONGISLAND": "LIU", "LIUBROOKLYN": "LIU",
    "STTHOMASMN": "STTHOMAS", "RENSSELAER": "RPI",
    "ALASANCHORAGE": "ALASKAANCHORAGE", "ANCHORAGE": "ALASKAANCHORAGE",
    "ALASFAIRBANKS": "ALASKA", "ALASKAFAIRBANKS": "ALASKA", "FAIRBANKS": "ALASKA",
}


def norm_team(name):
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).upper()
    s = s.replace("&", " AND ")
    s = re.sub(r"\bSAINT\b", "ST", s)
    s = re.sub(r"\bSTATE\b", "ST", s)
    # The stats feed shortens compass points and 'Michigan' inconsistently:
    # 'N. Michigan', 'Northern Mich.', 'Northern Michigan' are one program.
    s = re.sub(r"\bN\.?\s+(?=[A-Z])", "NORTHERN ", s)
    s = re.sub(r"\bS\.?\s+(?=[A-Z])", "SOUTHERN ", s)
    s = re.sub(r"\bE\.?\s+(?=[A-Z])", "EASTERN ", s)
    s = re.sub(r"\bW\.?\s+(?=[A-Z])", "WESTERN ", s)
    s = re.sub(r"\bMICH\b\.?", "MICHIGAN", s)
    s = re.sub(r"\bMINN\b\.?", "MINNESOTA", s)
    s = re.sub(r"\bCOLO\b\.?", "COLORADO", s)
    s = re.sub(r"\bUNIVERSITY\b", "U", s)
    s = re.sub(r"\bUNIV\b", "U", s)
    s = re.sub(r"\bCOLLEGE\b", "C", s)
    s = re.sub(r"\bCOLL\b", "C", s)
    s = re.sub(r"\bOF\b", "", s)
    s = re.sub(r"[^A-Z0-9]", "", s)
    return TEAM_ALIASES.get(s, s)


def build_team_index(db_teams):
    """Map fold -> team row. A fold two different teams both claim is poisoned
    so match_team refuses it rather than silently picking one."""
    idx, seen = {}, {}
    for t in db_teams:
        k = norm_team(t["name"])
        if k in seen and seen[k] != t["name"]:
            idx[k] = None
        else:
            seen[k] = t["name"]
            idx.setdefault(k, t)
    return idx


def match_team(name, idx):
    """Exact fold, then an unambiguous prefix match.

    The prefix half matters: the stats feed abbreviates ('N. Michigan',
    'Lake Superior St.', 'Union (NY)') where the schedule spells names out.
    Without it those programs look absent and their whole season is dropped.
    """
    key = norm_team(name)
    if key in idx:
        return idx[key]
    hits = {k: v for k, v in idx.items()
            if v is not None and (k.startswith(key) or key.startswith(k))}
    return next(iter(hits.values())) if len(hits) == 1 else None


def game_key(date_s, home_id, away_id):
    """Stable id for a game the NCAA feed hasn't published yet."""
    return f"chn-{date_s.replace('-','')}-{away_id}-{home_id}"


# ------------------------------------------------------------------- main
def selftest():
    print("SELF-TEST\n")
    html = """<table class="data schedule full"><tbody>
    <tr class="stats-section"><td colspan="99">Friday, October 2, 2026</td></tr>
    <tr class="sked-header"><td colspan="99">Exhibition</td></tr>
    <tr valign="top"><td>Manitoba&nbsp;</td><td class="center"></td><td>at</td>
      <td class="left">Minnesota State&nbsp;</td><td class="center"></td>
      <td>&nbsp;</td><td class="sked-status">&nbsp;11:30 CT&nbsp;</td></tr>
    <tr valign="top"><td>Boston College&nbsp;</td><td class="center"></td><td>at</td>
      <td class="left">Michigan State&nbsp;</td><td class="center"></td>
      <td>&nbsp;</td><td class="sked-status">&nbsp;5:00 ET&nbsp;</td></tr>
    <tr class="sked-header"><td colspan="99">Non-Conference</td></tr>
    <tr valign="top"><td>Maryville&nbsp;</td><td class="center"></td><td>at</td>
      <td class="left">St. Lawrence&nbsp;</td><td class="center"></td>
      <td>&nbsp;</td><td class="sked-status">&nbsp;6:00 ET&nbsp;</td></tr>
    <tr valign="top"><td>Denver&nbsp;</td><td class="center"></td><td>at</td>
      <td class="left">Alaska-Anchorage&nbsp;</td><td class="center"></td>
      <td>&nbsp;</td><td class="sked-status">&nbsp;7:00 AT&nbsp;</td></tr>
    </tbody></table>"""
    games, problems, skipped = parse_schedule(html)
    for g in games:
        print(f"  {g['date']}  {g['away']:<12} at {g['home']:<18} "
              f"{g['start_label']:<10} -> {g['start_utc']}")
    kept = {(g["away"], g["home"]) for g in games}
    # Boston College v Michigan State is D-I against D-I but still an
    # exhibition. If section tracking ever breaks, this is what catches it.
    ok = (len(games) == 2 and skipped == 2
          and ("Boston College", "Michigan State") not in kept
          and games[0]["home"] == "St. Lawrence"
          and games[0]["start_utc"].startswith("2026-10-02T22:00")     # 6pm EDT
          and games[1]["start_utc"].startswith("2026-10-03T03:00"))    # 7pm AKDT
    print(f"\n  exhibitions skipped: {skipped} "
          f"(incl. Boston College at Michigan State, D-I v D-I)")
    print("  aliases:", norm_team("Rensselaer") == norm_team("RPI"),
          norm_team("Alas. Anchorage") == norm_team("Alaska-Anchorage"))
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", default="2026-27")
    ap.add_argument("--file", help="a saved schedule page instead of fetching")
    ap.add_argument("--through", default="2027-03-06",
                    help="last date to import (default 2027-03-06: regular season "
                         "ends there, and postseason brackets are unseeded TBDs)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(0 if selftest() else 1)

    if args.file:
        html = open(args.file, encoding="utf-8", errors="replace").read()
    else:
        import urllib.request
        url = CHN_SEASON_URL.format(season=args.season)
        print(f"Fetching {url}")
        req = urllib.request.Request(url, headers={"User-Agent": "hockey-schedule/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            html = r.read().decode("utf-8", "replace")

    games, problems, skipped = parse_schedule(html)
    print(f"Parsed {len(games)} countable games "
          f"({skipped} exhibition/D-III skipped).")

    # Everything past the regular season is unseeded bracket placeholders
    # ("TBD-fr1 at TBD-fr4"), which name no real team and cannot be rostered.
    if args.through:
        before = len(games)
        games = [g for g in games if g["date"] <= args.through]
        if before != len(games):
            print(f"  {before - len(games)} postseason game(s) after "
                  f"{args.through} left out.")
    for p in problems:
        print("  !", p)
    if not games:
        return

    dates = sorted({g["date"] for g in games})
    no_time = [g for g in games if not g["start_utc"]]
    print(f"  {dates[0]} -> {dates[-1]} across {len(dates)} dates")
    if no_time:
        print(f"  {len(no_time)} game(s) have no start time yet (TBA)")

    from supabase import create_client
    url_, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY")
    if not url_ or not key:
        print("\nERROR: set SUPABASE_URL and SUPABASE_KEY first.")
        raise SystemExit(1)
    client = create_client(url_, key)

    db_teams = client.table("teams").select("team_id,name").execute().data
    idx = build_team_index(db_teams)

    rows, unknown, placeholders = [], {}, 0
    for g in games:
        # Bracket placeholders ("TBD-fr1 at TBD-fr4") name no real team.
        if g["home"].upper().startswith("TBD") or g["away"].upper().startswith("TBD"):
            placeholders += 1
            continue
        _h, _a = match_team(g["home"], idx), match_team(g["away"], idx)
        h = _h["team_id"] if _h else None
        a = _a["team_id"] if _a else None
        if h is None:
            unknown.setdefault(g["home"], 0); unknown[g["home"]] += 1
        if a is None:
            unknown.setdefault(g["away"], 0); unknown[g["away"]] += 1
        if h is None or a is None:
            continue
        rows.append({"game_id": game_key(g["date"], h, a), "game_date": g["date"],
                     "start_time": g["start_utc"], "season": args.season,
                     "status": "scheduled", "home_team_id": h, "away_team_id": a})

    if placeholders:
        print(f"  {placeholders} unseeded bracket placeholder(s) left out.")
    if unknown:
        print(f"\n  {len(unknown)} team name(s) not in your database:")
        for n, c in sorted(unknown.items(), key=lambda x: -x[1])[:20]:
            print(f"    {n}  ({c} games)")
        print("  Their games are skipped. Non-D-I opponents are expected here.")

    print(f"\n  {len(rows)} games ready to write.")
    if args.dry_run:
        print("\nDry run - nothing written. First 5:")
        for r in rows[:5]:
            print("   ", r["game_date"], r["away_team_id"], "at",
                  r["home_team_id"], r["start_time"])
        return

    existing = set()
    _s = 0
    while True:
        chunk = (client.table("games").select("game_id")
                 .range(_s, _s + 999).execute().data)
        existing |= {r["game_id"] for r in chunk}
        if len(chunk) < 1000:
            break
        _s += 1000

    fresh = [r for r in rows if r["game_id"] not in existing]
    for i in range(0, len(rows), 500):
        client.table("games").upsert(rows[i:i+500], on_conflict="game_id").execute()
    print(f"\nDone. {len(fresh)} new, {len(rows)-len(fresh)} already present.")
    print("  Results will fill these in as the stats loader runs.")


if __name__ == "__main__":
    main()
