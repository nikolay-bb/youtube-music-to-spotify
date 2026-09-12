"""Check that everything is configured, and say exactly what is not.

    npm run check

Written for someone setting this up for the first time. Every failure names the
file or the console page to go and fix, rather than reporting an error code.
"""

from __future__ import annotations

import asyncio
import base64
import sys

import httpx

from app.config import SPOTIFY_TOKEN_FILE, YTM_OAUTH_FILE, get_settings

OK, BAD, WARN = "  OK  ", " FAIL ", " WARN "


def line(state: str, label: str, detail: str = "") -> None:
    print(f"[{state}] {label}" + (f"\n         {detail}" if detail else ""))


async def main() -> int:
    settings = get_settings()
    failures = 0

    print("\nChecking your setup\n" + "-" * 60)

    # --- Spotify credentials ---
    if not settings.spotify_client_id or not settings.spotify_client_secret:
        line(BAD, "Spotify keys", "SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET are empty in .env")
        failures += 1
    else:
        auth = base64.b64encode(
            f"{settings.spotify_client_id}:{settings.spotify_client_secret}".encode()
        ).decode()
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                "https://accounts.spotify.com/api/token",
                headers={"Authorization": f"Basic {auth}"},
                data={"grant_type": "client_credentials"},
            )
        if r.status_code == 200:
            line(OK, "Spotify keys accepted")
        else:
            line(BAD, "Spotify keys rejected", f"HTTP {r.status_code}. Check them in the dashboard.")
            failures += 1

    if "127.0.0.1" not in settings.spotify_redirect_uri:
        line(BAD, "Spotify redirect URI", "Must use 127.0.0.1, not localhost. Spotify rejects localhost.")
        failures += 1
    else:
        line(OK, "Spotify redirect URI", settings.spotify_redirect_uri)

    line(
        OK if SPOTIFY_TOKEN_FILE.exists() else WARN,
        "Spotify connected" if SPOTIFY_TOKEN_FILE.exists() else "Spotify not connected yet",
        "" if SPOTIFY_TOKEN_FILE.exists() else "Press Connect Spotify in the app.",
    )

    # --- Google credentials ---
    if not settings.ytm_client_id or not settings.ytm_client_secret:
        line(BAD, "Google keys", "YTM_CLIENT_ID / YTM_CLIENT_SECRET are empty in .env")
        failures += 1
    else:
        line(OK, "Google keys present")
        if not settings.ytm_client_id.endswith(".apps.googleusercontent.com"):
            line(WARN, "Google client id looks unusual", "It normally ends .apps.googleusercontent.com")

    if YTM_OAUTH_FILE.exists():
        async with httpx.AsyncClient(timeout=20) as client:
            import json

            stored = json.loads(YTM_OAUTH_FILE.read_text())
            r = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": settings.ytm_client_id,
                    "client_secret": settings.ytm_client_secret,
                    "refresh_token": stored.get("refresh_token", ""),
                    "grant_type": "refresh_token",
                },
            )
        if r.status_code == 200:
            line(OK, "YouTube connected")
        else:
            line(
                BAD,
                "YouTube login no longer works",
                "Press Connect YouTube again. Logins expire after 7 days while the "
                "Google project is in Testing.",
            )
            failures += 1
    else:
        line(WARN, "YouTube not connected yet", "Press Connect YouTube in the app.")

    # --- Settings that affect how well it runs ---
    line(OK, "Match thresholds", f"add at {settings.match_threshold}, review at {settings.review_threshold}")
    line(
        OK,
        "Request pacing",
        f"{settings.spotify_max_concurrency} at a time, {settings.spotify_request_delay_ms}ms apart, "
        f"up to {settings.max_queries_per_song} searches per song",
    )

    print("-" * 60)
    if failures:
        print(f"\n{failures} thing(s) need fixing. See above.\n")
        return 1
    print("\nEverything checks out. Run 'npm run dev' and open http://127.0.0.1:3000\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
