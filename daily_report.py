#!/usr/bin/env python3
"""
Fantasy Football Daily Standouts Report
------------------------------------------
Once-daily digest of players trending up in adds, cross-referenced against
a per-player Google News search for a short "why" when a matching
headline can be found.

Players reported in the last 24h are suppressed from re-appearing, so each
day's report surfaces new names rather than repeating yesterday's list.
State is kept in daily_state.json, committed back by the workflow each run.

Data sources (both free, no auth required):
  - Sleeper API: trending adds (last 24h) + player names
  - Google News RSS search: per-player headline lookup
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

FANTASY_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DEF"}
TOP_N_TRENDING = 10          # how many players to actually report on
CANDIDATE_POOL_SIZE = 30     # how many trending players to pull before filtering repeats
LOOKBACK_HOURS = 24
REPEAT_SUPPRESSION_HOURS = 24  # don't re-report the same player within this window

STATE_FILE = Path(__file__).parent / "daily_state.json"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "fantasy-alert-script/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def fetch_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "fantasy-alert-script/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"reported": {}}  # pid -> last-reported unix timestamp


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


def send_notification(subject, body):
    if not NTFY_TOPIC:
        print("Missing NTFY_TOPIC secret, skipping send.")
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={"Title": subject},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def player_name(p):
    return p.get("full_name") or f"{p.get('first_name','')} {p.get('last_name','')}".strip()


def get_trending_candidates():
    players = fetch_json("https://api.sleeper.app/v1/players/nfl?active=true")
    trending = fetch_json(
        f"https://api.sleeper.app/v1/players/nfl/trending/add"
        f"?lookback_hours={LOOKBACK_HOURS}&limit={CANDIDATE_POOL_SIZE}"
    )
    results = []
    for entry in trending:
        pid = entry.get("player_id")
        p = players.get(pid)
        if not p or p.get("position") not in FANTASY_POSITIONS:
            continue
        results.append({
            "pid": pid,
            "name": player_name(p),
            "team": p.get("team", ""),
            "count": entry.get("count", 0),
        })
    return results


def search_news_for_player(name):
    query = urllib.parse.quote(f"{name} NFL")
    url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
    try:
        raw = fetch_text(url)
        root = ET.fromstring(raw)
    except Exception as e:
        print(f"News search failed for {name}: {e}")
        return None

    item = root.find(".//item")
    if item is None:
        return None

    title = item.findtext("title", default="")
    source_el = item.find("source")
    source = source_el.text if source_el is not None else None
    return {"title": title, "source": source}


def main():
    state = load_state()
    now = time.time()
    reported = state.get("reported", {})

    try:
        candidates = get_trending_candidates()
    except Exception as e:
        print(f"Failed to fetch trending players: {e}")
        return

    # filter out anyone reported within the suppression window
    fresh = [
        c for c in candidates
        if now - reported.get(c["pid"], 0) >= REPEAT_SUPPRESSION_HOURS * 3600
    ]
    selected = fresh[:TOP_N_TRENDING]

    if not selected:
        print("No new trending players today (all candidates were reported recently).")
        save_state(state)
        return

    lines = []
    for p in selected:
        match = search_news_for_player(p["name"])
        header = f"{p['name']} ({p['team']}) — {p['count']} adds/{LOOKBACK_HOURS}h"
        if match:
            source_note = f" — {match['source']}" if match.get("source") else ""
            lines.append(f"{header}\n{match['title']}{source_note}")
        else:
            lines.append(f"{header}\nNo matching headline found yet — worth a manual check.")
        reported[p["pid"]] = now

    body = "\n\n".join(lines)

    print(body)
    send_notification("FF Daily Standouts", body)
    print(f"Done. Reported on {len(selected)} new trending player(s).")

    state["reported"] = reported
    save_state(state)


if __name__ == "__main__":
    sys.exit(main())
