#!/usr/bin/env python3
"""
Fantasy Football Weekly Movers Report
-----------------------------------------
Sunday-night rollup of the week's biggest add/drop movers (7-day lookback
via Sleeper's trending endpoints). No repeat-suppression here by design -
it's a point-in-time weekly snapshot, not a running daily feed.

Data sources (all free, no auth required):
  - Sleeper API: trending adds/drops (7 days) + player names/positions
  - Google News RSS search: headline lookup for the top "up" movers
"""

import sys

from ff_common import fetch_json, player_name, search_news_for_player, send_notification

FANTASY_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DEF"}
POSITION_ORDER = ["QB", "RB", "WR", "TE", "K", "DEF"]
TOP_N_UP = 10
TOP_N_DOWN = 8
LOOKBACK_HOURS = 24 * 7
HEADLINE_MAX_AGE_HOURS = 24 * 7


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
    return results


def group_by_position(entries):
    grouped = {pos: [] for pos in POSITION_ORDER}
    for e in entries:
        grouped.setdefault(e["position"], []).append(e)
    return grouped


def format_group(grouped, verb):
    lines = []
    for pos in POSITION_ORDER:
        entries = grouped.get(pos)
        if not entries:
            continue
        lines.append(f"[{pos}]")
        for e in entries:
            line = f"{e['name']} ({e['team']}) — {e['count']} {verb} this week"
            if e.get("headline"):
                source_note = f" — {e['headline']['source']}" if e["headline"].get("source") else ""
                line += f"\n  {e['headline']['title']}{source_note}"
            lines.append(line)
    return "\n".join(lines)


def main():
    try:
        up = get_trending("add", TOP_N_UP)
    except Exception as e:
        print(f"Failed to fetch weekly trending-up players: {e}")
        up = []

    try:
        down = get_trending("drop", TOP_N_DOWN)
    except Exception as e:
        print(f"Failed to fetch weekly trending-down players: {e}")
        down = []

    if not up and not down:
        print("No weekly trending data available.")
        return

    for e in up:
        e["headline"] = search_news_for_player(e["name"], max_age_hours=HEADLINE_MAX_AGE_HOURS)

    sections = []
    if up:
        sections.append("\U0001F4C8 BIGGEST ADDS THIS WEEK\n" + format_group(group_by_position(up), "adds"))
    if down:
        sections.append("\U0001F4C9 BIGGEST DROPS THIS WEEK\n" + format_group(group_by_position(down), "drops"))

    body = "\n\n".join(sections)

    print(body)
    send_notification("FF Weekly Movers", body)
    print(f"Done. Reported {len(up)} up / {len(down)} down for the week.")


if __name__ == "__main__":
    sys.exit(main())
