from contextlib import asynccontextmanager
import asyncio
import logging

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.database import init_db
from app.dependencies import get_optional_user
from app.models import User
from app.routes.auth_routes import facebook_router, router as auth_router
from app.routes.setup_routes import router as setup_router
from app.routes.dashboard_routes import router as dashboard_router
from app.routes.automation_routes import router as automation_router
from app.routes.webhook_routes import router as webhook_router
from app.scheduler import automation_loop, process_due_follow_ups

logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app):
    await init_db()
    try:
        await process_due_follow_ups()
    except Exception:
        logger.exception("Startup follow-up check failed")
    scheduler_task = asyncio.create_task(automation_loop(interval_seconds=3600))
    yield
    scheduler_task.cancel()
    try:
        await scheduler_task
    except asyncio.CancelledError:
        pass


def create_app():
    app = FastAPI(title=settings.app_name, lifespan=lifespan)

    @app.get("/health")
    async def health():
        try:
            await process_due_follow_ups()
        except Exception:
            logger.exception("Follow-up check during health failed")
        return JSONResponse({"status": "ok"})

    @app.get("/internal/process-followups")
    async def cron_process_followups(key: str = ""):
        """Optional external cron ping (e.g. cron-job.org) when Render is asleep."""
        if key != settings.webhook_verify_token:
            return PlainTextResponse("Forbidden", status_code=403)
        count = await process_due_follow_ups()
        return JSONResponse({"processed": count})

    templates = Jinja2Templates(directory="app/templates")
    landing_router = APIRouter()

    @landing_router.get("/", response_class=HTMLResponse)
    async def landing(
        request: Request,
        user: User | None = Depends(get_optional_user),
    ):
        if user:
            return RedirectResponse("/dashboard", status_code=302)
        return templates.TemplateResponse(
            request,
            "landing.html",
            {"app_name": settings.app_name},
        )

    app.include_router(landing_router)
    app.include_router(auth_router)
    app.include_router(facebook_router)
    app.include_router(setup_router)
    app.include_router(dashboard_router)
    app.include_router(automation_router)
    app.include_router(webhook_router)

    app.mount("/static", StaticFiles(directory="app/static"), name="static")

    return app


app = create_app()
