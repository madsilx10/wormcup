"""
WormCup Auto Bot
- Login pakai initData dari Pyrogram (RequestMainWebView)
- Auto sign-in (Solana SIWS flow) -> dapet access_token & refresh_token
- Token disimpan per akun di folder tokens/ (json), dipake ulang kalau masih valid
- Auto predict semua match UPCOMING+OPEN, auto tap, auto check-in, boost, cek hasil
"""

import argparse
import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse, parse_qs

import requests
from pyrogram import Client
from pyrogram.raw.functions.messages import RequestMainWebView

logging.getLogger("pyrogram").setLevel(logging.ERROR)

# ===================== CONFIG =====================
BOT_USERNAME = "wormcupbot"
START_PARAM = "PWJY9DP"  # invitation code / referral

SESSIONS_FILE = "sessions.txt"

def load_sessions():
    if not os.path.exists(SESSIONS_FILE):
        return []
    with open(SESSIONS_FILE) as f:
        return [line.strip() for line in f if line.strip()]

SESSIONS = load_sessions()

API_BASE = "https://api.worm.wtf/api"
WC_BASE  = "https://wc.worm.wtf/api"

HEADERS_COMMON = {
    "Origin":     "https://wormcup.vercel.app",
    "Referer":    "https://wormcup.vercel.app/",
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
}

TOKEN_DIR = "tokens"
os.makedirs(TOKEN_DIR, exist_ok=True)

# Mode predict: "random" | "simple" | "favored" | "manual"
PREDICT_MODE = "random"

# Buat mode manual: key = "HOME-AWAY" (code tim)
MANUAL_SCORES = {
    # "GER-CUW": (3, 0),
}

# Mapping points -> multiplier untuk boost
BOOST_MAP = {100: 2, 300: 3, 500: 5, 1000: 10}


# ===================== PREDICT LOGIC =====================
# Distribusi skor sepakbola natural: skor kecil lebih sering, besar jarang, max 4
_GOAL_WEIGHTS = [30, 28, 20, 13, 9]  # index 0-4, bobot kemunculan tiap gol

def _weighted_goal():
    return random.choices(range(5), weights=_GOAL_WEIGHTS, k=1)[0]

def decide_score(match):
    home_code = match["home"]["code"]
    away_code = match["away"]["code"]

    if PREDICT_MODE == "manual":
        key = f"{home_code}-{away_code}"
        return MANUAL_SCORES.get(key, (1, 0))

    if PREDICT_MODE == "simple":
        return 1, 0

    if PREDICT_MODE == "favored":
        dist = match["distribution"]
        return (1, 0) if dist["home_pct"] >= dist["away_pct"] else (0, 1)

    # random - weighted natural football scores
    return _weighted_goal(), _weighted_goal()


# ===================== INIT DATA (PYROGRAM) =====================
async def send_start(client: Client):
    await client.send_message(BOT_USERNAME, "/start")

async def get_init_data(client: Client) -> str:
    peer   = await client.resolve_peer(BOT_USERNAME)
    result = await client.invoke(
        RequestMainWebView(peer=peer, bot=peer, platform="android", start_param=START_PARAM)
    )
    params   = parse_qs(urlparse(result.url).fragment)
    raw_data = params["tgWebAppData"][0]
    return raw_data


# ===================== TOKEN STORAGE =====================
def token_path(user_id):
    return os.path.join(TOKEN_DIR, f"{user_id}.json")

def load_tokens(user_id):
    p = token_path(user_id)
    return json.load(open(p)) if os.path.exists(p) else None

def save_tokens(user_id, access_token, refresh_token):
    with open(token_path(user_id), "w") as f:
        json.dump({"access_token": access_token, "refresh_token": refresh_token}, f)

def is_token_valid(access_token):
    try:
        import base64
        b64 = access_token.split(".")[1]
        b64 += "=" * (-len(b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(b64))
        return payload["exp"] > time.time() + 60
    except Exception:
        return False


# ===================== SIGN-IN FLOW =====================
def login_with_init_data(init_data):
    headers_tma = {**HEADERS_COMMON, "Authorization": f"tma {init_data}"}

    me = requests.get(f"{WC_BASE}/users/me/", headers=headers_tma)
    me.raise_for_status()
    address          = me.json()["data"]["address"]
    telegram_user_id = me.json()["data"]["telegram_user_id"]

    si = requests.get(f"{API_BASE}/sign-in/", params={"address": address, "network_type": 2}, headers=headers_tma)
    si.raise_for_status()
    nonce = si.json()["result"]["data"]["nonce"]

    issued_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.now().microsecond // 1000:03d}Z"
    message = (
        f"www.worm.wtf wants you to sign in with your Solana account:\n{address}\n\n"
        f"Sign in with Solana to the app.\n\n"
        f"URI: https://www.worm.wtf\nVersion: 1\nChain ID: 1\nNonce: {nonce}\nIssued At: {issued_at}"
    )

    sign = requests.post(f"{WC_BASE}/signing/sign/", headers=headers_tma,
                         json={"kind": "worm_auth_message", "payload": message})
    sign.raise_for_status()
    signature = sign.json()["data"]["signed_payload"]

    final = requests.post(f"{API_BASE}/sign-in/", headers=HEADERS_COMMON,
                          json={"message": message, "signature": signature,
                                "address": address, "nonce": nonce, "invitation_code": START_PARAM})
    final.raise_for_status()
    data          = final.json()["result"]["data"]
    access_token  = data["access_token"]
    refresh_token = data["refresh_token"]
    save_tokens(str(telegram_user_id), access_token, refresh_token)

    requests.post(f"{API_BASE}/social/telegram/auth/miniapp/",
                  headers={**HEADERS_COMMON, "Authorization": f"Bearer {access_token}"},
                  json={"init_data": init_data})

    return access_token, telegram_user_id


# ===================== API HELPERS =====================
def ah(token):
    return {**HEADERS_COMMON, "Authorization": f"Bearer {token}"}

def get_dashboard(token):
    r = requests.get(f"{API_BASE}/worldcup/me/dashboard/", headers=ah(token))
    r.raise_for_status()
    return r.json()["result"]["data"]

def get_matches(token):
    r = requests.get(f"{API_BASE}/worldcup/matches/", params={"limit": 20, "offset": 0}, headers=ah(token))
    r.raise_for_status()
    return r.json()["result"]["data"]

def predict(token, condition_id, home_score, away_score):
    r = requests.post(f"{API_BASE}/worldcup/predictions/",
                      headers=ah(token),
                      json={"condition_id": condition_id, "home_score": home_score, "away_score": away_score})
    return r.json()

def boost_prediction(token, prediction_id, multiplier):
    r = requests.post(f"{API_BASE}/worldcup/predictions/{prediction_id}/boost/",
                      headers=ah(token), json={"multiplier": multiplier})
    return r.json()

def do_tap(token):
    r = requests.post(f"{API_BASE}/worldcup/game/play/", headers=ah(token))
    return r.json()["result"]["data"]

def check_in(token):
    r = requests.post(f"{API_BASE}/worldcup/streak/check-in/", headers=ah(token))
    return r.json()


# ===================== HELPERS GET TOKEN =====================
async def get_token(session_string, idx, total):
    client = Client(name=f"acc{idx}", session_string=session_string, in_memory=True, no_updates=True)
    await client.start()
    init_data = await get_init_data(client)
    await client.stop()

    user_part = unquote(init_data.split("user=")[1].split("&")[0])
    user_id   = str(json.loads(user_part)["id"])
    tag       = f"[Akun {idx}/{total} | {user_id}]"

    saved = load_tokens(user_id)
    if saved and is_token_valid(saved["access_token"]):
        return saved["access_token"], user_id, tag, init_data
    else:
        access_token, _ = login_with_init_data(init_data)
        print(f"{tag} login baru")
        return access_token, user_id, tag, init_data


# ===================== MODES =====================
async def run_normal(session_string, idx, total):
    tag = f"[Akun {idx}/{total}]"
    print(f"\n{'='*40}\n{tag} mulai")

    access_token, user_id, tag, _ = await get_token(session_string, idx, total)

    # Check-in
    ci = check_in(access_token)
    print(f"{tag} check-in: {'OK' if ci.get('success') else 'gagal'}")

    # Cek hasil predict
    matches = get_matches(access_token)
    for m in matches:
        p = m.get("my_prediction")
        if not p:
            continue
        status = p["status"]
        home, away = m["home"]["code"], m["away"]["code"]
        hs, as_ = p["home_score"], p["away_score"]
        if status == "WON":
            payout = p.get("payout_usdc") or "?"
            print(f"{tag} ✅ WON {home} {hs}-{as_} {away} | +${payout} USDC")
        elif status == "LOST":
            result = m.get("result")
            actual = f"{result['home_score']}-{result['away_score']}" if result else "?"
            print(f"{tag} ❌ LOST {home} {hs}-{as_} {away} | hasil: {actual}")

    # Predict UPCOMING+OPEN
    predicted = 0
    for m in matches:
        if m["status"] == "UPCOMING" and m["pool"]["status"] == "OPEN" and m["my_prediction"] is None:
            hs, as_ = decide_score(m)
            res  = predict(access_token, m["condition_id"], hs, as_)
            home, away = m["home"]["code"], m["away"]["code"]
            print(f"{tag} predict {home} {hs}-{as_} {away} -> {'OK' if res.get('success') else res}")
            predicted += 1
    if predicted == 0:
        print(f"{tag} gak ada match baru buat di-predict")

    # Tap
    dash      = get_dashboard(access_token)
    remaining = dash["game"]["plays_remaining"]
    if remaining > 0:
        for _ in range(remaining):
            do_tap(access_token)
            time.sleep(0.5)
        print(f"{tag} tap selesai ({remaining}x)")
    else:
        print(f"{tag} tap udah habis hari ini")


async def run_tap_only(session_string, idx, total):
    tag = f"[Akun {idx}/{total}]"
    print(f"\n{'='*40}\n{tag} tap only")
    access_token, user_id, tag, _ = await get_token(session_string, idx, total)

    dash      = get_dashboard(access_token)
    remaining = dash["game"]["plays_remaining"]
    if remaining > 0:
        for _ in range(remaining):
            do_tap(access_token)
            time.sleep(0.5)
        print(f"{tag} tap selesai ({remaining}x)")
    else:
        print(f"{tag} tap udah habis hari ini")


async def run_boost(session_string, idx, total, points):
    multiplier = BOOST_MAP[points]
    tag        = f"[Akun {idx}/{total}]"
    print(f"\n{'='*40}\n{tag} boost {points}pts (x{multiplier})")
    access_token, user_id, tag, _ = await get_token(session_string, idx, total)

    matches = get_matches(access_token)
    boosted = 0
    for m in matches:
        p = m.get("my_prediction")
        if not p or p["status"] != "PENDING" or p["boost_multiplier"] != 1:
            continue
        res  = boost_prediction(access_token, p["id"], multiplier)
        home, away = m["home"]["code"], m["away"]["code"]
        hs, as_    = p["home_score"], p["away_score"]
        bal  = res.get("result", {}).get("data", {}).get("points_balance", "?")
        print(f"{tag} boost {home} {hs}-{as_} {away} x{multiplier} -> {'OK' if res.get('success') else res} | sisa poin: {bal}")
        boosted += 1
    if boosted == 0:
        print(f"{tag} gak ada predict yang bisa di-boost")


async def run_start_bot(session_string, idx, total):
    client = Client(name=f"acc{idx}", session_string=session_string, in_memory=True, no_updates=True)
    await client.start()
    await send_start(client)
    await client.stop()
    print(f"[Akun {idx}/{total}] /start terkirim")


# ===================== MENU =====================
def select_mode_and_accounts():
    n = len(SESSIONS)
    print(f"\nTotal akun: {n}")
    print("Mode:")
    print("  1. Normal (check-in + predict + tap)")
    print("  2. Tap doang")
    print("  3. Boost predict")
    print("  4. Start bot doang")
    mode = input("Pilih mode (1/2/3/4): ").strip()

    boost_points = None
    if mode == "3":
        print("Boost berapa poin?")
        print("  1. 100 pts (x2)")
        print("  2. 300 pts (x3)")
        print("  3. 500 pts (x5)")
        print("  4. 1000 pts (x10)")
        bp_choice = input("Pilih (1/2/3/4): ").strip()
        boost_points = [100, 300, 500, 1000][int(bp_choice) - 1]

    print("\nAkun:")
    print("  1. Satu akun")
    print("  2. Semua akun")
    print("  3. Range")
    acc_choice = input("Pilih (1/2/3): ").strip()

    if acc_choice == "1":
        idx = int(input(f"Index akun (1-{n}): ").strip())
        indexed = [(idx, SESSIONS[idx - 1])]
    elif acc_choice == "3":
        start = int(input(f"Dari (1-{n}): ").strip())
        end   = int(input(f"Sampai (1-{n}): ").strip())
        indexed = [(i, SESSIONS[i - 1]) for i in range(start, end + 1)]
    else:
        indexed = list(enumerate(SESSIONS, start=1))

    return mode, boost_points, indexed


# ===================== MAIN =====================
async def main():
    if not SESSIONS:
        print("sessions.txt kosong / gak ketemu.")
        return

    mode, boost_points, indexed = select_mode_and_accounts()
    total = len(indexed)

    for idx, s in indexed:
        try:
            if mode == "1":
                await run_normal(s, idx, total)
            elif mode == "2":
                await run_tap_only(s, idx, total)
            elif mode == "3":
                await run_boost(s, idx, total, boost_points)
            elif mode == "4":
                await run_start_bot(s, idx, total)
        except Exception as e:
            print(f"[Akun {idx}] Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
