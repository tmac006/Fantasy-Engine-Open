"""FastAPI application entry point."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from api.config import get_settings
from api.routers.admin import router as admin_router
from api.routers.draft import router as draft_router
from api.routers.leagues import router as leagues_router
from api.routers.news import router as news_router
from api.routers.weekly import router as weekly_router
from api.scheduler import build_scheduler, run_due_jobs

logging.basicConfig(level=get_settings().log_level)
log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Start the ingest scheduler and catch up on anything stale.

    Catch-up runs off the event loop so a cold start with a week of missed
    ingests still serves requests immediately.
    """
    scheduler = build_scheduler()
    scheduler.start()
    catch_up = asyncio.create_task(asyncio.to_thread(run_due_jobs))
    try:
        yield
    finally:
        catch_up.cancel()
        scheduler.shutdown(wait=False)


app = FastAPI(title="fantasy", version="0.1.0", lifespan=lifespan)
app.include_router(leagues_router)
app.include_router(draft_router)
app.include_router(admin_router)
app.include_router(news_router)
app.include_router(weekly_router)


class _WebFiles(StaticFiles):
    """Static files with a cache policy that survives a rebuild.

    Angular fingerprints its bundles, so those are safe to cache indefinitely.
    `index.html` names them, so caching it hands back a page pointing at bundles
    that no longer exist -- the app silently keeps running the previous build
    until someone thinks to hard-refresh.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if path in ("", ".", "index.html") or path.endswith("/index.html"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        elif "-" in Path(path).stem:  # fingerprinted bundle
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


# The in-season web app is served by the API itself: one process, one command.
_WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist" / "browser"
if _WEB_DIST.is_dir():
    app.mount("/app", _WebFiles(directory=_WEB_DIST, html=True), name="web")
else:  # not built yet — say so instead of 404ing mysteriously
    log.warning("web app not built; run `npx ng build` in web/")


@app.get("/")
def index() -> RedirectResponse:
    return RedirectResponse(url="/app/")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
