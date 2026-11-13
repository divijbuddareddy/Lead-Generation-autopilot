import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import seed
from .config import FRONTEND_DIR
from .database import Base, SessionLocal, engine
from .deps import APIError
from .routers import admin, auth, data, gtm, platform, public_api, webhooks_in
from .worker import worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("gtm")


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        seed.run(db)
    finally:
        db.close()
    worker.start()
    log.info("GTM platform ready")
    yield
    worker.stop()


app = FastAPI(title="Autonomous GTM Platform", version="1.0.0", lifespan=lifespan,
              description="AI Automation Labs - autonomous GTM: research, prospecting, outreach, replies, meetings.")


@app.middleware("http")
async def request_id_mw(request: Request, call_next):
    rid = request.headers.get("X-Request-Id") or "req_" + secrets.token_hex(8)
    request.state.request_id = rid
    response = await call_next(request)
    response.headers["X-Request-Id"] = rid
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        # Always revalidate the UI so a new deploy is picked up without a hard refresh.
        response.headers["Cache-Control"] = "no-cache"
    return response


def _err(request: Request, status: int, code: str, message: str, headers=None):
    return JSONResponse({"error": {"code": code, "message": message,
                                   "request_id": getattr(request.state, "request_id", "")}},
                        status_code=status, headers=headers)


@app.exception_handler(APIError)
async def api_error(request: Request, exc: APIError):
    return _err(request, exc.status, exc.code, exc.message, exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    loc = ".".join(str(x) for x in first.get("loc", []) if x != "body")
    return _err(request, 422, "VALIDATION_ERROR", f"{loc}: {first.get('msg', 'invalid input')}")


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled error")
    return _err(request, 500, "INTERNAL_ERROR", "Something went wrong. Quote the request id to support.")


for r in (auth.router, admin.router, data.router, gtm.router, platform.router, public_api.router, webhooks_in.router):
    app.include_router(r)


@app.get("/health")
def health():
    return {"status": "ok", "worker_last_tick": worker.last_tick.isoformat() if worker.last_tick else None}


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")
