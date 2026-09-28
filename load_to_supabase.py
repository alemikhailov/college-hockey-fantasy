#!/usr/bin/env python3
"""
College Hockey Fantasy - loader v3
==================================
Pulls men's D-I hockey and loads RAW STATS into Supabase.

WHAT CHANGED FROM v2
  * No fantasy points are calculated here any more. Points are computed in the
    database from your league settings, so changing a scoring weight re-scores
    every past week instantly. One rule, one place.
  * Stores every stat the feed provides - penalty minutes, game-winners,
    faceoffs, empty-net goals, and the goalie breakdowns - even the ones you
    don't score today. Turning a category on later then needs no re-loading.
  * Saves each game's exact START TIME, so lineups can lock per player.
  * Saves SCHEDULED games too, not just finished ones (you can't lock against
    a game you haven't stored).
  * Saves a season label, so multi-season play stays possible later.

SETUP (once)
    python3 -m pip install supabase

CREDENTIALS - in PowerShell, in the window you'll run from:
    $env:SUPABASE_URL="https://xxxxxxxx.supabase.co"
    $env:SUPABASE_KEY="your-sb_secret_-key"

RUN
    python3 load_to_supabase.py --selftest                    # offline check
    python3 load_to_supabase.py --start 2025-12-19 --end 2026-01-18
"""

import argparse, json, os, time, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

API_BASE = "https://ncaa-api.henrygd.me"
SPORT, DIVISION = "icehockey-men", "d1"
DEFAULT_START, DEFAULT_END = "2025-12-19", "2026-01-18"
CACHE_DIR = "cache"
REQUEST_PAUSE_SECONDS = 0.4

# --------------------------------------------------------------------------
# field maps: db column  ->  feed field name
# --------------------------------------------------------------------------
SKATER_FIELDS = {
    "goals": "goals", "assists": "assists", "shots": "shots",
    "plus_minus": "plusminus",
    "pim": "minutes",             # for skaters, `minutes` is PENALTY minutes
    "penalties": "count",
    "pp_goals": "powerPlayGoals", "sh_goals": "shortHandedGoals",
    "shootout_goals": "shootoutGoals",
    "gw_goals": "gameWinningGoals", "gt_goals": "gameTyingGoals",
    "ot_goals": "overtimeGoals", "en_goals": "emptyNetGoals",
    "first_goals": "firstGoals", "unassisted_goals": "unassistedGoals",
    "hattricks": "hattricks",
    "ps_goals": "penaltyShotGoals", "ps_attempts": "penaltyShotsAttempted",
    "blocks": "blk",
    "faceoffs_won": "facewon", "faceoffs_lost": "facelost",
}
GOALIE_FIELDS = {
    "minutes": "goalieMinutes", "saves": "saves",
    "goals_against": "goalsAllowed", "shutouts": "shutouts",
    "pp_goals_allowed": "powerPlayGoalsAllowed",
    "sh_goals_allowed": "shortHandedGoalsAllowed",
    "en_goals_allowed": "emptyNetGoalsAllowed",
    "ps_goals_allowed": "penaltyShotGoalsAllowed",
    "so_goals_allowed": "shootoutGoalsAllowed",
}

# --------------------------------------------------------------------------
# team name matching - the same folding the roster and schedule importers use.
# The scoreboard qualifies some names ("Maryville (MO)", "Union (NY)") where
# the boxscore feed that built your teams table does not, so exact matching
# alone makes real D-I programs look unknown and silently drops their games.
# --------------------------------------------------------------------------
import re, unicodedata

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
    idx, seen = {}, {}
    for t in db_teams:
        k = norm_team(t["name"])
        if k in seen and seen[k] != t["name"]:
            idx[k] = None                 # ambiguous - refuse rather than guess
        else:
            seen[k] = t["name"]; idx.setdefault(k, t)
    return idx

def match_team(name, idx):
    key = norm_team(name)
    if key in idx:
        return idx[key]
    hits = {k: v for k, v in idx.items()
            if v is not None and (k.startswith(key) or key.startswith(k))}
    return next(iter(hits.values())) if len(hits) == 1 else None


# --------------------------------------------------------------------------
# HTTP + cache
# --------------------------------------------------------------------------
def _cache_path(route):
    return os.path.join(CACHE_DIR, route.strip("/").replace("/", "_") + ".json")

def api_get(route, use_cache=True):
    os.makedirs(CACHE_DIR, exist_ok=True)
    cp = _cache_path(route)
    if use_cache and os.path.exists(cp):
        with open(cp, encoding="utf-8") as f:
            return json.load(f)
    req = urllib.request.Request(API_BASE + route, headers={"User-Agent": "hockey-loader/3.0"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
            with open(cp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            time.sleep(REQUEST_PAUSE_SECONDS)
            return data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 429:
                time.sleep(2 * (attempt + 1)); continue
            print(f"  ! HTTP {e.code} for {route}"); return None
        except (urllib.error.URLError, TimeoutError):
            time.sleep(1.5 * (attempt + 1))
    return None

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _norm(k):
    return str(k).lower().replace(" ", "").replace("_", "").replace("-", "")

def flatten(obj, out=None):
    if out is None:
        out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                flatten(v, out)
            else:
                out.setdefault(_norm(k), v)
    elif isinstance(obj, list):
        for item in obj:
            flatten(item, out)
    return out

def num(flat, name):
    v = flat.get(_norm(name), 0)
    try:
        return float(str(v).strip())
    except (ValueError, AttributeError, TypeError):
        return 0.0

def text(flat, name):
    v = flat.get(_norm(name), "")
    return "" if v is None else str(v).strip()

def truthy(flat, name):
    v = flat.get(_norm(name))
    return v is True or str(v).strip().lower() in ("true", "1", "yes")

def player_name(flat):
    n = (text(flat, "firstName") + " " + text(flat, "lastName")).strip()
    return n or text(flat, "name") or "(unknown)"

def is_goalie(flat):
    if text(flat, "position").upper() in ("G", "GK", "GOALIE", "GOALTENDER"):
        return True
    return num(flat, "goalieMinutes") > 0 or num(flat, "saves") > 0

def season_label(d):
    """A season spanning Oct-Apr is labelled by the year it started."""
    y = d.year if d.month >= 8 else d.year - 1
    return f"{y}-{str(y + 1)[2:]}"

def start_time_iso(g):
    """UTC timestamp for the game's scheduled start, from startTimeEpoch."""
    for key in ("startTimeEpoch", "startTimeepoch", "starttimeepoch"):
        v = g.get(key)
        if v:
            try:
                return datetime.fromtimestamp(int(str(v).strip()), tz=timezone.utc).isoformat()
            except (ValueError, OSError):
                pass
    return None

def scoreboard_teams(g):
    """(home, away) short names straight off the SCOREBOARD entry.

    A game that hasn't been played has no boxscore, so this is the only place
    the teams are available before puck drop. Without it a scheduled game
    lands in the database with no teams and nothing can lock against it.
    """
    def side(key):
        s = g.get(key) or {}
        n = s.get("names") or {}
        return (n.get("short") or n.get("full") or n.get("seo") or "").strip()
    return side("home"), side("away")


def team_directory(box):
    """NCAA teamId -> team info, read from the boxscore's `teams` section."""
    d = {}
    for t in (box.get("teams") or []):
        if not isinstance(t, dict):
            continue
        tid = str(t.get("teamId") or "").strip()
        name = (t.get("nameShort") or t.get("nameFull") or t.get("teamName") or "").strip()
        if tid and name:
            d[tid] = {"name": name, "is_home": bool(t.get("isHome"))}
    return d

# --------------------------------------------------------------------------
# assemble one game (pure -> testable offline)
# --------------------------------------------------------------------------
def assemble_game(meta, box):
    tdir = team_directory(box)
    teams, players, skaters, goalies = set(), {}, [], []
    team_goals, roster = {}, []
    home_team = away_team = None

    for info in tdir.values():
        teams.add(info["name"])
        team_goals[info["name"]] = 0.0
        if info["is_home"]:
            home_team = info["name"]
        else:
            away_team = info["name"]

    for tb in (box.get("teamBoxscore") or []):
        if not isinstance(tb, dict):
            continue
        info = tdir.get(str(tb.get("teamId") or "").strip())
        if not info:
            continue
        team = info["name"]
        for prow in (tb.get("playerStats") or []):
            flat = flatten(prow)
            gk = is_goalie(flat)
            nm = player_name(flat)
            players[(nm, team)] = {"full_name": nm, "team": team,
                                   "position": text(flat, "position"), "is_goalie": gk}
            team_goals[team] += num(flat, "goals")
            roster.append((team, flat, gk, nm))

    # decision: more goals wins, equal is a tie
    winner = loser = None
    tie = False
    if len(team_goals) == 2:
        (t1, g1), (t2, g2) = list(team_goals.items())
        if g1 > g2:   winner, loser = t1, t2
        elif g2 > g1: winner, loser = t2, t1
        else:         tie = True

    # goalie of record = most minutes for that team
    def keeper_of(team):
        cand = [(num(f, "goalieMinutes"), nm) for (tm, f, gk, nm) in roster if gk and tm == team]
        return max(cand)[1] if cand else None
    win_keeper  = keeper_of(winner) if winner else None
    lose_keeper = keeper_of(loser)  if loser  else None

    seen = set()
    for (team, flat, gk, nm) in roster:
        key = (meta["id"], team, nm)
        if key in seen:
            continue
        seen.add(key)
        if gk:
            row = {"game_id": meta["id"], "team": team, "player": nm,
                   "starter": truthy(flat, "starter"),
                   "wins":   1 if nm == win_keeper  and team == winner else 0,
                   "losses": 1 if nm == lose_keeper and team == loser  else 0,
                   "ties":   1 if tie and nm == keeper_of(team)        else 0}
            for col, field in GOALIE_FIELDS.items():
                row[col] = round(num(flat, field), 1) if col == "minutes" else int(num(flat, field))
            goalies.append(row)
        else:
            row = {"game_id": meta["id"], "team": team, "player": nm,
                   "starter": truthy(flat, "starter")}
            for col, field in SKATER_FIELDS.items():
                row[col] = int(num(flat, field))
            skaters.append(row)

    return {"teams": teams, "players": players, "skaters": skaters, "goalies": goalies,
            "home": home_team, "away": away_team, "team_goals": team_goals,
            "winner": winner, "tie": tie}

# --------------------------------------------------------------------------
# collect a date range
# --------------------------------------------------------------------------
def daterange(a, b):
    d = a
    while d <= b:
        yield d; d += timedelta(days=1)

def collect(start_s, end_s, refresh_today=True):
    start = datetime.strptime(start_s, "%Y-%m-%d").date()
    end   = datetime.strptime(end_s, "%Y-%m-%d").date()
    today = datetime.now(timezone.utc).date()
    print(f"Pulling men's D-I hockey {start} -> {end}")

    metas = []
    for d in daterange(start, end):
        route = f"/scoreboard/{SPORT}/{DIVISION}/{d.year}/{d.month:02d}/{d.day:02d}"
        # don't serve today's or future scoreboards from cache - they change
        data = api_get(route, use_cache=not (refresh_today and d >= today))

        # A scoreboard cached while games were still being played is frozen
        # mid-game: every later run would replay those non-final states and
        # the results would never load. If anything on this date is unfinished,
        # throw the cached copy away and fetch it again.
        if data is not None:
            unfinished = any(
                "final" not in str((w.get("game", w)).get("gameState", "")).lower()
                for w in (data.get("games") or []))
            if unfinished and d < today:
                data = api_get(route, use_cache=False)
        for wrapper in (data or {}).get("games", []):
            g = wrapper.get("game", wrapper)
            gid = str(g.get("gameID") or g.get("gameId") or "").strip()
            if not gid:
                continue
            state = str(g.get("gameState", "")).lower()
            home, away = scoreboard_teams(g)
            # An unknown/blank state means "not played yet", NOT final. Treating
            # blank as final made the loader chase boxscores for future games.
            metas.append({"id": gid, "date": d.isoformat(),
                          "state": state or "scheduled",
                          "start_time": start_time_iso(g),
                          "season": season_label(d),
                          "home": home, "away": away,
                          "final": "final" in state})

    finals = [m for m in metas if m["final"]]
    print(f"Found {len(metas)} games ({len(finals)} final). Downloading box scores...")

    teams, players, games, skaters, goalies = set(), {}, [], [], []
    for i, meta in enumerate(metas, 1):
        game = {"game_id": meta["id"], "game_date": meta["date"],
                "start_time": meta["start_time"], "season": meta["season"],
                "status": "final" if meta["final"] else (meta["state"] or "scheduled"),
                "home": meta.get("home") or None,
                "away": meta.get("away") or None, "team_goals": {}}
        if meta["home"]:
            teams.add(meta["home"])
        if meta["away"]:
            teams.add(meta["away"])
        if meta["final"]:
            box = api_get(f"/game/{meta['id']}/boxscore")
            if box:
                a = assemble_game(meta, box)
                teams |= a["teams"]; players.update(a["players"])
                skaters += a["skaters"]; goalies += a["goalies"]
                game.update(home=a["home"], away=a["away"], team_goals=a["team_goals"])
        games.append(game)
        if i % 25 == 0:
            print(f"  ...{i}/{len(metas)}")

    return {"teams": teams, "players": players, "games": games,
            "skaters": skaters, "goalies": goalies}

# --------------------------------------------------------------------------
# push
# --------------------------------------------------------------------------
def chunked(seq, n=500):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]

def dedupe(rows, keys):
    seen, out = set(), []
    for r in rows:
        k = tuple(r[x] for x in keys)
        if k in seen:
            continue
        seen.add(k); out.append(r)
    return out

def push(data, allow_new_teams=False, allow_unscheduled=False):
    from supabase import create_client
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY")
    if not url or not key:
        print("ERROR: set SUPABASE_URL and SUPABASE_KEY first.")
        raise SystemExit(1)          # fail loudly - a green tick must mean it worked
    client = create_client(url, key)

    # Existing teams first. Opening-night slates include EXHIBITIONS against
    # non-D-I opponents (Manitoba, Windsor, Concordia, US Under-18...). Left
    # alone, the loader would create those programs and every one of their
    # players, and they'd land in the draft pool as live, rosterable names.
    # So by default we only accept teams already in the database.
    db_teams = client.table("teams").select("team_id,name").execute().data
    idx = build_team_index(db_teams)
    team_id = {}
    for feed_name in sorted(data["teams"]):
        if not feed_name:
            continue
        hit = match_team(feed_name, idx)
        if hit:
            team_id[feed_name] = hit["team_id"]
    # every database team stays addressable under its own name too
    for t in db_teams:
        team_id.setdefault(t["name"], t["team_id"])

    unknown = sorted(t for t in data["teams"] if t and t not in team_id)
    if unknown:
        print(f"\n  {len(unknown)} team(s) in the feed are not in your database:")
        for t in unknown[:15]:
            print("   ", t)
        if len(unknown) > 15:
            print(f"    ...and {len(unknown)-15} more")
        if allow_new_teams:
            for batch in chunked([{"name": t} for t in unknown]):
                for r in client.table("teams").upsert(
                        batch, on_conflict="name").execute().data:
                    team_id[r["name"]] = r["team_id"]
            print(f"  -> created {len(unknown)} team(s) (--allow-new-teams)")
        else:
            print("  -> skipped. These are usually exhibition opponents, which")
            print("     should not enter the player pool. If one is a real D-I")
            print("     program, re-run with --allow-new-teams.")

    print(f"teams known: {len(team_id)}")
    if not team_id:
        print("  ! no teams resolved - stopping before anything else empties out.")
        raise SystemExit(1)

    player_rows = []
    for (nm, team), p in data["players"].items():
        tid = team_id.get(team)
        if tid is not None:
            player_rows.append({"full_name": nm, "team_id": tid,
                                "position": p["position"], "is_goalie": p["is_goalie"]})
    player_id = {}
    for batch in chunked(dedupe(player_rows, ("full_name", "team_id"))):
        for r in client.table("players").upsert(batch, on_conflict="full_name,team_id").execute().data:
            player_id[(r["full_name"], r["team_id"])] = r["player_id"]
    print(f"players: {len(player_id)}")

    # Games pre-loaded from the published schedule already occupy a row keyed
    # by date + the two teams. Reuse that row's id instead of inserting the
    # NCAA one, or every scheduled game would end up duplicated the moment it
    # is played and the scoring would split across two records.
    slot_id, _s = {}, 0
    while True:
        chunk = (client.table("games")
                 .select("game_id,game_date,home_team_id,away_team_id")
                 .range(_s, _s + 999).execute().data)
        for r in chunk:
            if r["home_team_id"] and r["away_team_id"]:
                slot_id[(str(r["game_date"]), r["home_team_id"],
                         r["away_team_id"])] = r["game_id"]
        if len(chunk) < 1000:
            break
        _s += 1000

    id_remap = {}
    game_rows, skipped, unscheduled = [], 0, []
    for g in data["games"]:
        if (g["home"] and g["home"] not in team_id) or \
           (g["away"] and g["away"] not in team_id):
            skipped += 1
            continue
        tg = g["team_goals"] or {}
        _h, _a = team_id.get(g["home"]), team_id.get(g["away"])
        # Exact date only. Teams routinely play back-to-back Friday/Saturday
        # series, so any day-of-slack here would merge the wrong two games.
        _pre = slot_id.get((g["game_date"], _h, _a))

        # The published schedule decides what counts. The NCAA D-I scoreboard
        # also carries EXHIBITIONS between two D-I teams (Merrimack at Sacred
        # Heart, Boston College at Michigan State...), which College Hockey
        # News deliberately leaves out. Those must not reach the database: they
        # would pad the schedule column and, once played, score fantasy points
        # for games that do not count. So a game with no scheduled row is left
        # alone and reported. If it is a genuine reschedule, the weekly CHN
        # refresh moves the row and the next run picks it up by itself.
        if _pre is None and slot_id and not allow_unscheduled:
            unscheduled.append((g["game_date"], g["away"], g["home"]))
            continue

        if _pre and _pre != g["game_id"]:
            id_remap[g["game_id"]] = _pre
        game_rows.append({
            "game_id": _pre or g["game_id"], "game_date": g["game_date"],
            "start_time": g["start_time"], "season": g["season"], "status": g["status"],
            "home_team_id": team_id.get(g["home"]), "away_team_id": team_id.get(g["away"]),
            "home_score": int(tg[g["home"]]) if g["home"] in tg else None,
            "away_score": int(tg[g["away"]]) if g["away"] in tg else None,
        })
    for batch in chunked(dedupe(game_rows, ("game_id",))):
        client.table("games").upsert(batch, on_conflict="game_id").execute()
    print(f"games: {len(game_rows)}" + (f"  ({skipped} skipped - unknown team)" if skipped else ""))
    if id_remap:
        print(f"  {len(id_remap)} matched to a pre-loaded scheduled game")
    if unscheduled:
        print(f"  {len(unscheduled)} not on the published schedule - left out:")
        for d, a, h in unscheduled[:12]:
            print(f"     {d}  {a} at {h}")
        if len(unscheduled) > 12:
            print(f"     ...and {len(unscheduled)-12} more")
        print("   Exhibitions look exactly like this and should stay out. If one")
        print("   is a real game that moved, it lands on the next run once the")
        print("   schedule refresh catches up, or use --allow-unscheduled now.")

    def stat_rows(lines):
        out = []
        for ln in lines:
            tid = team_id.get(ln["team"])
            pid = player_id.get((ln["player"], tid))
            if pid is None:
                continue
            row = {k: v for k, v in ln.items() if k not in ("team", "player")}
            row["game_id"] = id_remap.get(row["game_id"], row["game_id"])
            row["player_id"] = pid
            out.append(row)
        return dedupe(out, ("game_id", "player_id"))

    sk = stat_rows(data["skaters"])
    for batch in chunked(sk):
        client.table("skater_stats").upsert(batch, on_conflict="game_id,player_id").execute()
    print(f"skater_stats: {len(sk)}")

    gl = stat_rows(data["goalies"])
    for batch in chunked(gl):
        client.table("goalie_stats").upsert(batch, on_conflict="game_id,player_id").execute()
    print(f"goalie_stats: {len(gl)}")
    print("\nDone. Points are computed from league settings - nothing to re-score.")

# --------------------------------------------------------------------------
# offline self-test
# --------------------------------------------------------------------------
def selftest():
    print("SELF-TEST - fixture shaped like the real feed\n")
    meta = {"id": "T1", "date": "2026-01-16", "season": "2025-26",
            "start_time": "2026-01-17T01:07:00+00:00", "final": True}
    box = {"teams": [
             {"isHome": True,  "teamId": "1", "nameShort": "Penn St.", "nameFull": "Penn State"},
             {"isHome": False, "teamId": "2", "nameShort": "Harvard",  "nameFull": "Harvard"}],
           "teamBoxscore": [
             {"teamId": "1", "playerStats": [
               {"firstName":"TOP","lastName":"LINE","position":"C","starter":True,
                "goals":"2","assists":"1","shots":"6","plusminus":"2","minutes":"2","count":"1",
                "powerPlayGoals":"1","shortHandedGoals":"0","shootoutGoals":"0",
                "gameWinningGoals":"1","gameTyingGoals":"0","overtimeGoals":"0",
                "emptyNetGoals":"0","firstGoals":"1","unassistedGoals":"0","hattricks":"0",
                "penaltyShotGoals":"0","penaltyShotsAttempted":"0","blk":"0",
                "facewon":"12","facelost":"8","goalieMinutes":"0","saves":"0"},
               {"firstName":"LEFT","lastName":"DEE","position":"LD","starter":True,
                "goals":"0","assists":"2","shots":"3","plusminus":"1","minutes":"4","count":"2",
                "blk":"0","facewon":"0","facelost":"0","goalieMinutes":"0","saves":"0"},
               {"firstName":"HOME","lastName":"KEEPER","position":"G","starter":True,
                "goalieMinutes":"60","saves":"31","goalsAllowed":"1","shutouts":"0",
                "powerPlayGoalsAllowed":"0","shortHandedGoalsAllowed":"0",
                "emptyNetGoalsAllowed":"0","penaltyShotGoalsAllowed":"0",
                "shootoutGoalsAllowed":"0","goals":"0","assists":"0","shots":"0"}]},
             {"teamId": "2", "playerStats": [
               {"firstName":"AWAY","lastName":"KEEPER","position":"G","starter":True,
                "goalieMinutes":"60","saves":"20","goalsAllowed":"2","shutouts":"0",
                "goals":"0","assists":"0","shots":"0"}]}]}

    a = assemble_game(meta, box)
    print("teams  :", sorted(a["teams"]))
    print("home/away:", a["home"], "/", a["away"])
    print("goals  :", a["team_goals"], "-> winner:", a["winner"])
    for s in a["skaters"]:
        print(f"  SK {s['player']:<12} G{s['goals']} A{s['assists']} SOG{s['shots']} "
              f"+/-{s['plus_minus']} PIM{s['pim']} PPG{s['pp_goals']} GWG{s['gw_goals']} "
              f"FOW{s['faceoffs_won']}")
    for g in a["goalies"]:
        print(f"  G  {g['player']:<12} W{g['wins']} L{g['losses']} SV{g['saves']} "
              f"GA{g['goals_against']} MIN{g['minutes']}")

    ok = (a["home"] == "Penn St." and a["away"] == "Harvard"
          and a["winner"] == "Penn St."
          and len(a["skaters"]) == 2 and len(a["goalies"]) == 2
          and a["skaters"][0]["pim"] == 2          # `minutes` -> penalty minutes
          and a["skaters"][0]["faceoffs_won"] == 12
          and a["skaters"][1]["pim"] == 4
          and sum(g["wins"] for g in a["goalies"]) == 1
          and sum(g["losses"] for g in a["goalies"]) == 1
          and all("fantasy_points" not in r for r in a["skaters"] + a["goalies"]))
    print("\nseason label 2026-01-16 ->", season_label(datetime(2026,1,16).date()),
          "| 2025-12-19 ->", season_label(datetime(2025,12,19).date()))
    print("start_time from epoch 1766192820 ->",
          start_time_iso({"startTimeEpoch": "1766192820"}))
    print("\nRESULT:", "PASS" if ok else "FAIL")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=DEFAULT_END)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--allow-new-teams", action="store_true",
                    help="create teams the feed mentions but your database lacks")
    ap.add_argument("--allow-unscheduled", action="store_true",
                    help="also load games that aren't on the published schedule "
                         "(this lets EXHIBITIONS in - normally you don't want it)")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    else:
        push(collect(a.start, a.end), allow_new_teams=a.allow_new_teams,
             allow_unscheduled=a.allow_unscheduled)
