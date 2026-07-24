#!/usr/bin/env python3
"""
Fantasy Football Breaking News Texter (Sleeper + ntfy.sh edition)
--------------------------------------------------------------------
Uses Sleeper's free, no-auth-required API for data, and ntfy.sh for
free push notifications (carrier email-to-SMS gateways were all
discontinued in 2025-2026).

Alerts on two signals:
  1. A fantasy-relevant player's injury/roster status changes
  2. A player has a sudden spike in league adds

State is kept in state.json, committed back by the workflow each run.
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

STATE_FILE = Path(__file__).parent / "state.json"
FANTASY_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DEF"}
TRENDING_ADD_THRESHOLD = 4000
TRENDING_LOOKBACK_HOURS = 1
TRENDING_REALERT_COOLDOWN_SECONDS = 60 * 60 * 6

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "fantasy-alert-script/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"player_status": {}, "trending_alerted": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


def send_text(subject, body):
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


def check_status_changes(state, alerts_sent):
    players = fetch_json("https://api.sleeper.app/v1/players/nfl?active=true")
    prev_status = state["player_status"]
    new_status = {}

    for pid, p in players.items():
        if p.get("position") not in FANTASY_POSITIONS:
            continue
        if not p.get("team"):
            continue

        status = p.get("injury_status")
        new_status[pid] = status

        old = prev_status.get(pid, "__unseen__")
        if old == "__unseen__":
            continue
        if old != status:
            name = p.get("full_name") or f"{p.get('first_name','')} {p.get('last_name','')}"
            team = p.get("team", "")
            if status:
                body = f"{name} ({team}): status changed to {status}"
            else:
                body = f"{name} ({team}): status cleared (was {old})"
            print("ALERT:", body)
            send_text("FF Status Change", body)
            alerts_sent[0] += 1

    state["player_status"] = new_status


def check_trending_spikes(state, alerts_sent):
    trending = fetch_json(
        f"https://api.sleeper.app/v1/players/nfl/trending/add"
        f"?lookback_hours={TRENDING_LOOKBACK_HOURS}&limit=25"
    )
    alerted = state["trending_alerted"]
    now = time.time()
    alerted = {pid: ts for pid, ts in alerted.items() if now - ts < TRENDING_REALERT_COOLDOWN_SECONDS}

    for entry in trending:
        pid = entry.get("player_id")
        count = entry.get("count", 0)
        if count < TRENDING_ADD_THRESHOLD:
            continue
        if pid in alerted:
            continue
        try:
            player = fetch_json(f"https://api.sleeper.app/v1/players/nfl/{pid}")
        except Exception:
            player = {}
        name = player.get("full_name", f"player {pid}") if isinstance(player, dict) else f"player {pid}"
        body = f"{name}: {count} adds in last {TRENDING_LOOKBACK_HOURS}h — likely breaking news"
        print("ALERT:", body)
        send_text("FF Trending Spike", body)
        alerts_sent[0] += 1
        alerted[pid] = now

    state["trending_alerted"] = alerted


def main():
    if os.environ.get("TEST_MODE") == "true":
        print("TEST_MODE is on — sending a test notification and exiting.")
        send_text("FF Alert Test", "This is a test notification from your fantasy football alert system. If you got this, it's working!")
        print("Test notification sent (if config was correct).")
        return

    state = load_state()
    alerts_sent = [0]

    try:
        check_status_changes(state, alerts_sent)
    except Exception as e:
        print(f"Status check failed: {e}")

    try:
        check_trending_spikes(state, alerts_sent)
    except Exception as e:
        print(f"Trending check failed: {e}")

    save_state(state)
    print(f"Done. {alerts_sent[0]} alert(s) sent.")


if __name__ == "__main__":
    sys.exit(main())
