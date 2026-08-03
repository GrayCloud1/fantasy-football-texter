#!/usr/bin/env python3
"""
Fantasy Football Daily Standouts Report
------------------------------------------
Once-daily digest of players trending up in adds, cross-referenced against
ESPN's official NFL news RSS feed for a short "why" when a matching
headline can be found.

Data sources (both free, no auth required):
  - Sleeper API: trending adds (last 24h) + player names
  - ESPN NFL RSS feed: recent headlines (official, meant for aggregators)

Sent as a single digest notification via ntfy.sh.
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

FANTASY_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DEF"}
TOP_N_TRENDING = 10
LOOKBACK_HOURS = 24

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "fantasy-alert-script/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def fetch_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "fantasy-alert-script/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


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


def get_trending_players():
    players = fetch_json("https://api.sleeper.app/v1/players/nfl?active=true")
    trending = fetch_json(
        f"https://api.sleeper.app/v1/players/nfl/trending/add"
        f"?lookback_hours={LOOKBACK_HOURS}&limit={TOP_N_TRENDING}"
    )
    results = []
    for entry in trending:
        pid = entry.get("player_id")
        p = players.get(pid)
        if not p or p.get("position") not in FANTASY_POSITIONS:
            continue
        results.append({
            "name": player_name(p),
            "last_name": p.get("last_name", ""),
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
    link = item.findtext("link", default="")
    source_el = item.find("source")
    source = source_el.text if source_el is not None else None
    return {"title": title, "link": link, "source": source}


def main():
    try:
        trending = get_trending_players()
    except Exception as e:
        print(f"Failed to fetch trending players: {e}")
        return

    if not trending:
        print("No trending players found, nothing to report.")
        return

    lines = []
    for p in trending:
        match = search_news_for_player(p["name"])
        header = f"{p['name']} ({p['team']}) — {p['count']} adds/{LOOKBACK_HOURS}h"
        if match:
            source_note = f" — {match['source']}" if match.get("source") else ""
            lines.append(f"{header}\n{match['title']}{source_note}\n{match['link']}")
        else:
            lines.append(f"{header}\nNo matching headline found yet — worth a manual check.")

    body = "\n\n".join(lines)

    print(body)
    send_notification("FF Daily Standouts", body)
    print(f"Done. Reported on {len(trending)} trending player(s).")


if __name__ == "__main__":
    sys.exit(main())
