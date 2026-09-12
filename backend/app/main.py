"""FastAPI application entry point.

Run it with:
    cd backend && ./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

It must be 127.0.0.1 rather than localhost, because that is what the Spotify
redirect URI has to be, and mixing the two breaks the login.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import ensure_data_dirs, get_settings
from .errors import describe_transport_error
from .routers import auth, library, transfer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

app = FastAPI(
    title="YouTube Music to Spotify",
    description="Move playlists and liked songs from YouTube Music to Spotify.",
    version="1.0.0",
)

settings = get_settings()
ensure_data_dirs()


# Anything a route did not catch would otherwise come back as a bare
# "Internal Server Error" with no CORS headers, which the browser reports as
# "cannot reach the backend" - the one thing it is not. This turns it into the
# same {"detail": "..."} shape as every other error, carrying the real reason.
#
# It is registered before CORS on purpose. Middleware added later wraps
# middleware added earlier, so CORS ends up on the outside and stamps its
# headers on this reply too.
@app.middleware("http")
async def explain_unexpected_errors(request: Request, call_next):  # type: ignore[no-untyped-def]
    try:
        return await call_next(request)
    except Exception as exc:  # noqa: BLE001 - the UI must show why, whatever it was
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        if isinstance(exc, httpx.HTTPError):
            return JSONResponse(
                status_code=502,
                content={"detail": describe_transport_error(exc, "the music service")},
            )
        return JSONResponse(
            status_code=500,
            content={
                "detail": (
                    f"The backend hit an unexpected error: {type(exc).__name__}: {exc}. "
                    "The full traceback is in the terminal running npm run dev."
                )
            },
        )


# The UI runs on a different port, so the browser treats it as another origin.
# Both spellings of the loopback address are allowed because Next.js prints
# "localhost" in the terminal even when it is serving 127.0.0.1.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        settings.frontend_url,
        "http://127.0.0.1:3000",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def explain_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """A 422 whose detail is a sentence, not a list of dictionaries.

    FastAPI's default puts a JSON array in ``detail``. The UI shows ``detail``
    as text, so without this it would print "Request failed with status 422"
    and nothing else.
    """
    problems = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
        problems.append(f"{location or 'request'}: {error.get('msg', 'invalid')}")
    return JSONResponse(
        status_code=422,
        content={
            "detail": (
                "The request was not in the shape the backend expects ("
                + "; ".join(problems)
                + "). This is a fault in the app rather than in your setup."
            )
        },
    )


app.include_router(auth.router)
app.include_router(library.router)
app.include_router(transfer.router)


@app.get("/api/health")
async def health() -> dict[str, object]:
    """A quick way to check the server is up and what it thinks is configured."""
    return {
        "ok": True,
        "spotify_configured": settings.spotify_configured,
        "youtube_configured": settings.ytm_configured,
        "match_threshold": settings.match_threshold,
        "review_threshold": settings.review_threshold,
    }
