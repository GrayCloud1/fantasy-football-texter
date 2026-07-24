#!/usr/bin/env python3
"""
Fantasy Football Breaking News Texter (Sleeper edition)
---------------------------------------------------------
Uses Sleeper's free, no-auth-required API instead of Reddit (no approval
process, no registration, works immediately).

Alerts on two signals:
  1. A fantasy-relevant player's injury/roster status changes
     (e.g. None -> "Questionable", "Questionable" -> "IR")
  2. A player has a sudden spike in league adds (proxy for breaking news
     causing a waiver-wire run: trade, breakout, injury return, etc.)

State is kept in state.json, which this script updates each run. Your
workflow should commit that file back to the repo after each run (see
.github/workflows/alert.yml).
"""

import json
import os
import smtplib
import sys
import time
import urllib.request
from email.mime.text import MIMEText
from pathlib import Path

# ---------- Config ----------

STATE_FILE = Path(__file__).parent / "state.json"
FANTASY_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DEF"}
TRENDING_ADD_THRESHOLD = 4000   # add-count to trigger a "spike" alert
TRENDING_LOOKBACK_HOURS = 1
TRENDING_REALERT_COOLDOWN_SECONDS = 60 * 60 * 6  # don't re-alert same player within 6h

# ---------- Env vars (set these as GitHub Actions secrets) ----------

GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
SMS_GATEWAY_ADDRESS = os.environ.get("SMS_GATEWAY_ADDRESS")


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
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and SMS_GATEWAY_ADDRESS):
        print("Missing email/SMS config, skipping send. (Set GMAIL_USER, "
              "GMAIL_APP_PASSWORD, SMS_GATEWAY_ADDRESS as secrets.)")
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = SMS_GATEWAY_ADDRESS
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, [SMS_GATEWAY_ADDRESS], msg.as_string())


def check_status_changes(state, alerts_sent):
    # active=true filters out retired/inactive players, shrinking the payload
    # considerably vs. the full ~5MB unfiltered player map
    players = fetch_json("https://api.sleeper.app/v1/players/nfl?active=true")
    prev_status = state["player_status"]
    new_status = {}

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

    # prune old cooldown entries
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
        print("TEST_MODE is on — sending a test text and exiting.")
        send_text("FF Alert Test", "This is a test text from your fantasy football alert system. If you got this, it's working!")
        print("Test text sent (if config was correct).")
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
