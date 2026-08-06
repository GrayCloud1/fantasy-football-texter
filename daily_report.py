#!/usr/bin/env python3
"""
Fantasy Football Daily Standouts Report
------------------------------------------
Once-daily digest with:
  - Trending UP: players getting added in bulk, grouped by position,
    each with a recent news headline when one can be found
  - Trending DOWN: players getting dropped in bulk (often just as
    newsworthy - injury, benching, poor camp performance)
  - Repeat suppression: a player reported in the last 24h won't repeat
    the next day, so each report surfaces new names
  - Cross-reference: flags if a trending player's roster/injury status
    also changed today, per the real-time alert script's state.json

Data sources (all free, no auth required):
  - Sleeper API: trending adds/drops (24h) + player names/positions
  - Google News RSS search: per-player headline lookup (via ff_common)
  - state.json: written by fantasy_alert.py, read here (not modified)
"""

import json
import sys
import time
from pathlib import Path

from ff_common import fetch_json, player_name, search_news_for_player, send_notification

FANTASY_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DEF"}
POSITION_ORDER = ["QB", "RB", "WR", "TE", "K", "DEF"]
TOP_N_UP = 10
TOP_N_DOWN = 8
CANDIDATE_POOL_SIZE = 30
LOOKBACK_HOURS = 24
REPEAT_SUPPRESSION_HOURS = 24
HEADLINE_MAX_AGE_HOURS = 48
STATUS_CHANGE_WINDOW_HOURS = 24

DAILY_STATE_FILE = Path(__file__).parent / "daily_state.json"
ALERT_STATE_FILE = Path(__file__).parent / "state.json"  # written by fantasy_alert.py


def load_daily_state():
    if DAILY_STATE_FILE.exists():
        state = json.loads(DAILY_STATE_FILE.read_text())
    else:
        state = {}
    state.setdefault("reported_up", {})
    state.setdefault("reported_down", {})
    return state


def save_daily_state(state):
    DAILY_STATE_FILE.write_text(json.dumps(state))


def load_alert_state():
    """Read the real-time alert script's state, if it exists. Never written
    to from here - this script only reads it for cross-referencing."""
    if ALERT_STATE_FILE.exists():
        try:
            return json.loads(ALERT_STATE_FILE.read_text())
        except Exception as e:
            print(f"Could not parse state.json: {e}")
    return {"player_status": {}, "status_changed_at": {}}


def get_trending(direction, limit):
    players = fetch_json("https://api.sleeper.app/v1/players/nfl?active=true")
    trending = fetch_json(
        f"https://api.sleeper.app/v1/players/nfl/trending/{direction}"
        f"?lookback_hours={LOOKBACK_HOURS}&limit={limit}"
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
            "position": p.get("position", ""),
            "count": entry.get("count", 0),
        })
    return results, players


def filter_unreported(candidates, reported, now):
    return [
        c for c in candidates
        if now - reported.get(c["pid"], 0) >= REPEAT_SUPPRESSION_HOURS * 3600
    ]


def group_by_position(entries):
    grouped = {pos: [] for pos in POSITION_ORDER}
    for e in entries:
        grouped.setdefault(e["position"], []).append(e)
    return grouped


def format_group(grouped, verb, status_lookup=None):
    lines = []
    for pos in POSITION_ORDER:
        entries = grouped.get(pos)
        if not entries:
            continue
        lines.append(f"[{pos}]")
        for e in entries:
            line = f"{e['name']} ({e['team']}) — {e['count']} {verb}/{LOOKBACK_HOURS}h"
            if e.get("headline"):
                source_note = f" — {e['headline']['source']}" if e["headline"].get("source") else ""
                line += f"\n  {e['headline']['title']}{source_note}"
            if status_lookup and e["pid"] in status_lookup:
                line += f"\n  \u26a0\ufe0f Status also changed today \u2192 {status_lookup[e['pid']]}"
            lines.append(line)
    return "\n".join(lines)


def main():
    daily_state = load_daily_state()
    now = time.time()

    try:
        up_candidates, players = get_trending("add", CANDIDATE_POOL_SIZE)
    except Exception as e:
        print(f"Failed to fetch trending-up players: {e}")
        return

    try:
        down_candidates, _ = get_trending("drop", CANDIDATE_POOL_SIZE)
    except Exception as e:
        print(f"Failed to fetch trending-down players: {e}")
        down_candidates = []

    alert_state = load_alert_state()
    status_changed_at = alert_state.get("status_changed_at", {})
    player_status = alert_state.get("player_status", {})
    recently_changed_status = {
        pid: player_status.get(pid)
        for pid, ts in status_changed_at.items()
        if now - ts < STATUS_CHANGE_WINDOW_HOURS * 3600
    }

    fresh_up = filter_unreported(up_candidates, daily_state["reported_up"], now)[:TOP_N_UP]
    fresh_down = filter_unreported(down_candidates, daily_state["reported_down"], now)[:TOP_N_DOWN]

    if not fresh_up and not fresh_down:
        print("No new trending players today (all candidates were reported recently).")
        save_daily_state(daily_state)
        return

    # attach news headlines for the "up" list only (drops don't usually have
    # a positive story to surface, and it keeps the call volume reasonable)
    for e in fresh_up:
        e["headline"] = search_news_for_player(e["name"], max_age_hours=HEADLINE_MAX_AGE_HOURS)
        daily_state["reported_up"][e["pid"]] = now

    for e in fresh_down:
        daily_state["reported_down"][e["pid"]] = now

    sections = []
    if fresh_up:
        grouped_up = group_by_position(fresh_up)
        sections.append("\U0001F4C8 TRENDING UP\n" + format_group(grouped_up, "adds", recently_changed_status))
    if fresh_down:
        grouped_down = group_by_position(fresh_down)
        sections.append("\U0001F4C9 TRENDING DOWN\n" + format_group(grouped_down, "drops", recently_changed_status))

    body = "\n\n".join(sections)

    print(body)
    send_notification("FF Daily Standouts", body)
    print(f"Done. Reported {len(fresh_up)} up / {len(fresh_down)} down.")

    save_daily_state(daily_state)


if __name__ == "__main__":
    sys.exit(main())
