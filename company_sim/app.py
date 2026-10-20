"""FastAPI application: landing page + mail, portal, ERP apps + admin endpoints, for any number of tenants."""
from __future__ import annotations

import asyncio
import os
import random
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import chaos, config, db, seed
from .apps import admin, erp, mail, portal
from .apps.common import render
from .auth import LoginRequired
from .tenant_web import TenantMiddleware
from .tenant_web import router as tenant_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    chaos.load_from_env()
    # Reseed when the DB is missing, or when SIM_CHAOS was given (data flags must be applied).
    if not db.is_seeded() or os.environ.get("SIM_CHAOS") is not None:
        seed.seed_database()
    seed.write_workspace(overwrite=False)
    yield


app = FastAPI(title="Acme company simulator", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")
for module_router in (mail.router, portal.router, erp.router, erp.api, admin.router, tenant_router):
    app.include_router(module_router)


@app.exception_handler(LoginRequired)
async def login_required_handler(_: Request, exc: LoginRequired) -> RedirectResponse:
    return RedirectResponse(exc.url, status_code=303)


@app.middleware("http")
async def slow_pages(request: Request, call_next):
    """Chaos: add 1-3 s latency to page loads."""
    path = request.url.path
    if chaos.is_on("slow_pages") and not path.startswith(("/static", "/admin", "/t/", "/erp/api")):
        await asyncio.sleep(random.uniform(1, 3))
    return await call_next(request)


app.add_middleware(TenantMiddleware)  # added last = outermost: every other middleware and handler sees the tenant


@app.get("/", include_in_schema=False)
async def landing(request: Request):
    return render(request, "landing", "landing.html")


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    """Liveness plus the identity of this process (see ``config.INSTANCE_ID``): which simulator, which date, which data."""
    return {"status": "ok", "instance_id": config.INSTANCE_ID, "sim_today": config.SIM_TODAY.isoformat(),
            "data_dir": str(config.DATA_DIR), "pid": os.getpid(), "started_at": config.STARTED_AT}
