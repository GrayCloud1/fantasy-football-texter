#!/usr/bin/env python3
"""
Fantasy Football Breaking News Texter (Sleeper + ntfy.sh edition)
--------------------------------------------------------------------
Uses Sleeper's free, no-auth-required API for data, and ntfy.sh for
free push notifications (carrier email-to-SMS gateways were all
discontinued in 2025-2026).

Alerts on two signals:
  1. A fantasy-relevant player's injury/roster status changes
     (e.g. None -> "Questionable", "Questionable" -> "IR")
  2. A player has a sudden spike in league adds, with escalation-based
     re-alerting (only re-notifies on a big jump, not routine growth)

State is kept in state.json, committed back by the workflow each run.
Also records status_changed_at timestamps so daily_report.py can flag
players whose status changed the same day they're trending.
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

from ff_common import search_news_for_player

STATE_FILE = Path(__file__).parent / "state.json"
FANTASY_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DEF"}
TRENDING_ADD_THRESHOLD = 4000
TRENDING_LOOKBACK_HOURS = 1
TRENDING_REALERT_COOLDOWN_SECONDS = 60 * 60 * 24  # baseline quiet period if growth is minor
TRENDING_ESCALATION_MULTIPLIER = 10  # re-alert early only on a massive jump (e.g. 4,000 -> 40,000+)

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")

_full_players_cache = {}


def get_full_players():
    """Fallback lookup for players missing from the lightweight active=true
    list (e.g. just-signed/elevated players Sleeper hasn't flagged active
    yet - often exactly when they start trending). Fetched at most once
    per run, only if actually needed."""
    if not _full_players_cache:
        try:
            _full_players_cache.update(fetch_json("https://api.sleeper.app/v1/players/nfl"))
        except Exception as e:
            print(f"Fallback full player fetch failed: {e}")
    return _full_players_cache


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "fantasy-alert-script/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def load_state():
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
    else:
        state = {}
    state.setdefault("player_status", {})
    state.setdefault("trending_alerted", {})
    state.setdefault("status_changed_at", {})
    state.setdefault("depth_chart", {})
    return state


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


def player_name(p):
    return p.get("full_name") or f"{p.get('first_name','')} {p.get('last_name','')}".strip()


def check_status_changes(state, alerts_sent, players):
    prev_status = state["player_status"]
    status_changed_at = state["status_changed_at"]
    new_status = {}
    now = time.time()

    for pid, p in players.items():
        if p.get("position") not in FANTASY_POSITIONS:
            continue
        if not p.get("team"):  # skip free agents / retired
            continue

        status = p.get("injury_status")  # e.g. "Questionable", "Out", "IR", None
        new_status[pid] = status

        old = prev_status.get(pid, "__unseen__")
        if old == "__unseen__":
            continue  # first time seeing this player, don't alert on baseline
        if old != status:
            name = player_name(p)
            team = p.get("team", "")
            position = p.get("position", "")
            if status:
                body = f"{name} ({position}, {team}): status changed to {status}"
            else:
                body = f"{name} ({position}, {team}): status cleared (was {old})"

            headline = search_news_for_player(name, max_age_hours=48)
            if headline:
                source_note = f" — {headline['source']}" if headline.get("source") else ""
                body += f"\n{headline['title']}{source_note}"

            print("ALERT:", body)
            send_text("FF Status Change", body)
            alerts_sent[0] += 1
            status_changed_at[pid] = now

    state["player_status"] = new_status
    state["status_changed_at"] = status_changed_at


def check_depth_chart_changes(state, alerts_sent, players):
    """Alerts when a player moves into a starter or top-backup depth chart
    slot (order 1 or 2) - often the earliest concrete signal of an
    opportunity change, sometimes ahead of public injury news."""
    prev = state["depth_chart"]
    new_depth = {}

    for pid, p in players.items():
        if p.get("position") not in FANTASY_POSITIONS:
            continue
        if not p.get("team"):
            continue

        order = p.get("depth_chart_order")
        pos_label = p.get("depth_chart_position") or p.get("position", "")
        new_depth[pid] = {"position": pos_label, "order": order}

        if order is None:
            continue  # no depth chart data for this player right now

        prev_info = prev.get(pid)
        if prev_info is None:
            continue  # first time seeing depth chart data for this player - establish baseline only

        old_order = prev_info.get("order")
        if old_order == order:
            continue  # no change

        moved_up_to_key_slot = old_order is not None and order < old_order and order <= 2
        newly_earned_starter = old_order is None and order == 1

        if moved_up_to_key_slot or newly_earned_starter:
            name = player_name(p)
            team = p.get("team", "")
            slot = f"{pos_label}{order}"
            if moved_up_to_key_slot:
                body = f"{name} ({team}): moved up depth chart to {slot} (was {pos_label}{old_order})"
            else:
                body = f"{name} ({team}): now listed as {slot} on depth chart"

            headline = search_news_for_player(name, max_age_hours=48)
            if headline:
                source_note = f" — {headline['source']}" if headline.get("source") else ""
                body += f"\n{headline['title']}{source_note}"

            print("ALERT:", body)
            send_text("FF Depth Chart Move", body)
            alerts_sent[0] += 1

    state["depth_chart"] = new_depth


def check_trending_spikes(state, alerts_sent, players):
    trending = fetch_json(
        f"https://api.sleeper.app/v1/players/nfl/trending/add"
        f"?lookback_hours={TRENDING_LOOKBACK_HOURS}&limit=25"
    )
    alerted = state["trending_alerted"]  # pid -> {"ts": float, "count": int}
    now = time.time()

    # prune entries past the baseline cooldown (they're eligible for a fresh alert regardless of growth)
    alerted = {
        pid: info for pid, info in alerted.items()
        if now - info["ts"] < TRENDING_REALERT_COOLDOWN_SECONDS
    }

    for entry in trending:
        pid = entry.get("player_id")
        count = entry.get("count", 0)
        if count < TRENDING_ADD_THRESHOLD:
            continue

        prev = alerted.get(pid)
        if prev is not None and count < prev["count"] * TRENDING_ESCALATION_MULTIPLIER:
            continue  # already alerted recently and hasn't escalated enough to re-notify

        p = players.get(pid)
        if p is None:
            p = get_full_players().get(pid)
        name = player_name(p) if p else f"player {pid}"
        position = p.get("position", "") if p else ""
        team = p.get("team", "") if p else ""
        body = f"{name} ({position}, {team}): {count} adds in last {TRENDING_LOOKBACK_HOURS}h — likely breaking news"

        headline = search_news_for_player(name, max_age_hours=24)
        if headline:
            source_note = f" — {headline['source']}" if headline.get("source") else ""
            body += f"\n{headline['title']}{source_note}"

        print("ALERT:", body)
        send_text("FF Trending Spike", body)
        alerts_sent[0] += 1
        alerted[pid] = {"ts": now, "count": count}

    state["trending_alerted"] = alerted


def main():
    if os.environ.get("TEST_MODE") == "true":
        print("TEST_MODE is on — sending a test notification and exiting.")
        send_text("FF Alert Test", "This is a test notification from your fantasy football alert system. If you got this, it's working!")
        print("Test notification sent (if config was correct).")
        return

    state = load_state()
    alerts_sent = [0]

    # active=true filters out retired/inactive players, shrinking the payload
    # considerably vs. the full ~5MB unfiltered player map. Fetched once and
    # reused by both checks below.
    try:
        players = fetch_json("https://api.sleeper.app/v1/players/nfl?active=true")
    except Exception as e:
        print(f"Failed to fetch player list: {e}")
        players = {}

    if players:
        try:
            check_status_changes(state, alerts_sent, players)
        except Exception as e:
            print(f"Status check failed: {e}")

        try:
            check_depth_chart_changes(state, alerts_sent, players)
        except Exception as e:
            print(f"Depth chart check failed: {e}")

        try:
            check_trending_spikes(state, alerts_sent, players)
        except Exception as e:
            print(f"Trending check failed: {e}")

    save_state(state)
    print(f"Done. {alerts_sent[0]} alert(s) sent.")


if __name__ == "__main__":
    sys.exit(main())
