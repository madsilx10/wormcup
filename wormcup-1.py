"""
WormCup Auto Bot
- Login pakai initData dari Pyrogram (RequestMainWebView)
- Auto sign-in (Solana SIWS flow) -> dapet access_token & refresh_token
- Token disimpan per akun di folder tokens/ (json), dipake ulang kalau masih valid
- Auto predict semua match UPCOMING+OPEN, auto tap sampai plays_remaining = 0, auto check-in
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse, parse_qs

import requests
from pyrogram import Client
from pyrogram.raw.functions.messages import RequestMainWebView

# ===================== CONFIG =====================
BOT_USERNAME = "wormcupbot"
START_PARAM = "PWJY9DP"  # invitation code

# isi sessions.txt dengan 1 session string Pyrogram per baris
SESSIONS_FILE = "sessions.txt"


def load_sessions():
    if not os.path.exists(SESSIONS_FILE):
        return []
    with open(SESSIONS_FILE) as f:
        return [line.strip() for line in f if line.strip()]


SESSIONS = load_sessions()

API_BASE = "https://api.worm.wtf/api"
WC_BASE = "https://wc.worm.wtf/api"

HEADERS_COMMON = {
    "Origin": "https://wormcup.vercel.app",
    "Referer": "https://wormcup.vercel.app/",
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
}

TOKEN_DIR = "tokens"
os.makedirs(TOKEN_DIR, exist_ok=True)

# logic predict score: pilih salah satu
#   "simple"  -> selalu home 1 - away 0
#   "favored" -> pilih pemenang berdasarkan distribution_pct, skor 1-0/0-1
PREDICT_MODE = "favored"


# ===================== INIT DATA (PYROGRAM) =====================
async def get_init_data(client: Client) -> str:
    peer = await client.resolve_peer(BOT_USERNAME)
    result = await client.invoke(
        RequestMainWebView(
            peer=peer,
            bot=peer,
            platform="android",
            start_param=START_PARAM,
        )
    )
    url = result.url
    fragment = urlparse(url).fragment  # tgWebAppData=...&tgWebAppVersion=...
    params = parse_qs(fragment)
    raw_data = params["tgWebAppData"][0]
    return raw_data  # sudah dalam bentuk url-encoded (cocok buat header "tma <init_data>")


# ===================== TOKEN STORAGE =====================
def token_path(user_id: str) -> str:
    return os.path.join(TOKEN_DIR, f"{user_id}.json")


def load_tokens(user_id: str):
    p = token_path(user_id)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return None


def save_tokens(user_id: str, access_token: str, refresh_token: str):
    with open(token_path(user_id), "w") as f:
        json.dump({"access_token": access_token, "refresh_token": refresh_token}, f)


def is_token_valid(access_token: str) -> bool:
    """Cek expiry dari JWT (exp claim) tanpa verifikasi signature."""
    try:
        import base64

        payload_b64 = access_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload["exp"] > time.time() + 60  # buffer 60s
    except Exception:
        return False


# ===================== SIGN-IN FLOW =====================
def login_with_init_data(init_data: str):
    headers_tma = {**HEADERS_COMMON, "Authorization": f"tma {init_data}"}

    # 1. ambil address wallet
    me = requests.get(f"{WC_BASE}/users/me/", headers=headers_tma)
    me.raise_for_status()
    address = me.json()["data"]["address"]
    telegram_user_id = me.json()["data"]["telegram_user_id"]

    # 2. ambil nonce/message info
    si = requests.get(
        f"{API_BASE}/sign-in/",
        params={"address": address, "network_type": 2},
        headers=headers_tma,
    )
    si.raise_for_status()
    d = si.json()["result"]["data"]
    nonce = d["nonce"]

    # 3. construct message SIWS (format tetap, sesuai capture)
    issued_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.now().microsecond // 1000:03d}Z"
    message = (
        f"www.worm.wtf wants you to sign in with your Solana account:\n"
        f"{address}\n\n"
        f"Sign in with Solana to the app.\n\n"
        f"URI: https://www.worm.wtf\n"
        f"Version: 1\n"
        f"Chain ID: 1\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {issued_at}"
    )

    # 4. minta signature dari wc.worm.wtf
    sign = requests.post(
        f"{WC_BASE}/signing/sign/",
        headers=headers_tma,
        json={"kind": "worm_auth_message", "payload": message},
    )
    sign.raise_for_status()
    signature = sign.json()["data"]["signed_payload"]

    # 5. tukar ke access_token / refresh_token
    final = requests.post(
        f"{API_BASE}/sign-in/",
        headers=HEADERS_COMMON,
        json={
            "message": message,
            "signature": signature,
            "address": address,
            "nonce": nonce,
            "invitation_code": START_PARAM,
        },
    )
    final.raise_for_status()
    data = final.json()["result"]["data"]
    access_token = data["access_token"]
    refresh_token = data["refresh_token"]

    save_tokens(str(telegram_user_id), access_token, refresh_token)

    # 6. konfirmasi link telegram (init_data) ke akun ini
    requests.post(
        f"{API_BASE}/social/telegram/auth/miniapp/",
        headers={**HEADERS_COMMON, "Authorization": f"Bearer {access_token}"},
        json={"init_data": init_data},
    )

    return access_token, telegram_user_id


# ===================== API HELPERS =====================
def auth_headers(access_token: str):
    return {**HEADERS_COMMON, "Authorization": f"Bearer {access_token}"}


def get_dashboard(token):
    r = requests.get(f"{API_BASE}/worldcup/me/dashboard/", headers=auth_headers(token))
    r.raise_for_status()
    return r.json()["result"]["data"]


def get_matches(token):
    r = requests.get(
        f"{API_BASE}/worldcup/matches/",
        params={"limit": 20, "offset": 0},
        headers=auth_headers(token),
    )
    r.raise_for_status()
    return r.json()["result"]["data"]


def predict(token, condition_id, home_score, away_score):
    r = requests.post(
        f"{API_BASE}/worldcup/predictions/",
        headers=auth_headers(token),
        json={"condition_id": condition_id, "home_score": home_score, "away_score": away_score},
    )
    return r.json()


def tap(token):
    r = requests.post(f"{API_BASE}/worldcup/game/play/", headers=auth_headers(token))
    return r.json()["result"]["data"]


def check_in(token):
    r = requests.post(f"{API_BASE}/worldcup/streak/check-in/", headers=auth_headers(token))
    return r.json()


# Buat mode "manual": isi skor per pertandingan, key = "HOME-AWAY" (pakai code tim, misal "USA-PAR")
MANUAL_SCORES = {
    # "USA-PAR": (2, 1),
    # "QAT-SUI": (0, 3),
}


# ===================== PREDICT LOGIC =====================
def decide_score(match):
    home_code = match["home"]["code"]
    away_code = match["away"]["code"]

    if PREDICT_MODE == "manual":
        key = f"{home_code}-{away_code}"
        if key in MANUAL_SCORES:
            return MANUAL_SCORES[key]
        return 1, 0  # fallback kalau gak ada di MANUAL_SCORES

    if PREDICT_MODE == "random":
        import random

        dist = match["distribution"]
        # bobot skor lebih tinggi buat tim yg favorit, tapi tetep acak
        if dist["home_pct"] >= dist["away_pct"]:
            return random.randint(1, 3), random.randint(0, 2)
        return random.randint(0, 2), random.randint(1, 3)

    if PREDICT_MODE == "simple":
        return 1, 0

    dist = match["distribution"]
    if dist["home_pct"] >= dist["away_pct"]:
        return 1, 0
    return 0, 1


# ===================== MAIN PER ACCOUNT =====================
async def process_account(session_string: str):
    client = Client(name="acc", session_string=session_string, in_memory=True)
    await client.start()

    init_data = await get_init_data(client)

    # ambil telegram_user_id dari init_data buat cek token tersimpan
    user_part = unquote(init_data.split("user=")[1].split("&")[0])
    user_json = json.loads(user_part)
    user_id = str(user_json["id"])

    saved = load_tokens(user_id)
    if saved and is_token_valid(saved["access_token"]):
        access_token = saved["access_token"]
        print(f"[{user_id}] pakai token tersimpan")
    else:
        access_token, _ = login_with_init_data(init_data)
        print(f"[{user_id}] login baru, token disimpan")

    # ===== Check-in harian =====
    ci = check_in(access_token)
    print(f"[{user_id}] check-in: {ci.get('message') or ci.get('success')}")

    # ===== Predict semua match yang OPEN =====
    matches = get_matches(access_token)
    for m in matches["data"]:
        if m["status"] == "UPCOMING" and m["pool"]["status"] == "OPEN" and m["my_prediction"] is None:
            hs, as_ = decide_score(m)
            res = predict(access_token, m["condition_id"], hs, as_)
            home, away = m["home"]["code"], m["away"]["code"]
            print(f"[{user_id}] predict {home} {hs} - {as_} {away} -> {res.get('success')}")

    # ===== Tap sampai habis =====
    dash = get_dashboard(access_token)
    remaining = dash["game"]["plays_remaining"]
    print(f"[{user_id}] plays_remaining: {remaining}")
    for i in range(remaining):
        result = tap(access_token)
        time.sleep(0.5)  # delay biar gak rate-limit
    print(f"[{user_id}] tap selesai")

    await client.stop()


async def main():
    for s in SESSIONS:
        try:
            await process_account(s)
        except Exception as e:
            print("Error:", e)


if __name__ == "__main__":
    asyncio.run(main())
