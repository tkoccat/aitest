"""Zeynep demo API: görüşme jetonu, yönetim paneli uçları ve ajanın kullandığı iç uçlar."""
from __future__ import annotations

import hmac
import os
import secrets
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import db, logic
from .livekit_token import make_token

WEB_DIR = Path(os.environ.get("WEB_DIR", Path(__file__).resolve().parents[2] / "web"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    conn = db.connect()
    db.init_db(conn)
    if os.environ.get("SEED_DEMO", "1") != "0":
        logic.seed_demo(conn)
    conn.close()
    yield


app = FastAPI(title="Zeynep Demo API", lifespan=lifespan)


# ------------------------------------------------------------------ bağımlılıklar

def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def _same(expected: str, provided: str) -> bool:
    return bool(expected) and bool(provided) and hmac.compare_digest(expected.encode(), provided.encode())


def _check_key(env_name: str, provided: str) -> None:
    if not _same(os.environ.get(env_name, ""), provided):
        raise HTTPException(status_code=401, detail="Yetkisiz")


def admin_auth(x_admin_key: str = Header(default="")) -> None:
    _check_key("ADMIN_KEY", x_admin_key)


def internal_auth(x_internal_key: str = Header(default="")) -> None:
    _check_key("INTERNAL_KEY", x_internal_key)


# ------------------------------------------------------------------ sayfalar

@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def root():
    return RedirectResponse("/call")


@app.get("/call")
def call_page():
    return FileResponse(WEB_DIR / "call.html")


@app.get("/admin")
def admin_page():
    return FileResponse(WEB_DIR / "admin.html")


# ------------------------------------------------------------------ görüşme jetonu (herkese açık, anahtar korumalı)

_token_times: deque[float] = deque()
_token_lock = threading.Lock()


class TokenReq(BaseModel):
    key: str = ""


@app.get("/api/call/config")
def call_config():
    return {
        "needs_key": bool(os.environ.get("CALL_PAGE_KEY", "")),
        "agency_name": os.environ.get("AGENCY_NAME", "Demo Turizm"),
        "max_minutes": int(os.environ.get("MAX_CALL_MINUTES", "8")),
    }


@app.post("/api/call/token")
def call_token(req: TokenReq):
    required = os.environ.get("CALL_PAGE_KEY", "")
    if required and not _same(required, req.key or ""):
        raise HTTPException(status_code=401, detail="Geçersiz bağlantı anahtarı")

    limit = int(os.environ.get("MAX_CALLS_PER_HOUR", "30"))
    with _token_lock:
        now = time.time()
        while _token_times and now - _token_times[0] > 3600:
            _token_times.popleft()
        if len(_token_times) >= limit:
            raise HTTPException(status_code=429, detail="Saatlik görüşme sınırına ulaşıldı, daha sonra deneyin")
        _token_times.append(now)

    url = os.environ.get("LIVEKIT_URL", "")
    key = os.environ.get("LIVEKIT_API_KEY", "")
    secret = os.environ.get("LIVEKIT_API_SECRET", "")
    if not (url and key and secret):
        raise HTTPException(status_code=503, detail="LiveKit ayarları eksik")

    room = "call-" + secrets.token_hex(6)
    identity = "guest-" + secrets.token_hex(4)
    token = make_token(key, secret, identity, room, ttl_seconds=900, name="Misafir")
    return {"url": url, "token": token, "room": room}


# ------------------------------------------------------------------ ajanın kullandığı iç uçlar

class CheckReq(BaseModel):
    package_id: int
    rooms: int = Field(1, ge=1, le=50)


class HoldReq(BaseModel):
    package_id: int
    rooms: int = Field(1, ge=1, le=50)
    guest_name: str = Field("", max_length=120)
    phone: str = Field("", max_length=40)


class CallSaveReq(BaseModel):
    language: str | None = None
    guest_name: str | None = Field(None, max_length=120)
    phone: str | None = Field(None, max_length=40)
    outcome: str | None = None
    summary: str | None = Field(None, max_length=4000)
    notes: str | None = Field(None, max_length=4000)
    callback_time: str | None = Field(None, max_length=120)
    event_id: int | None = None
    package_id: int | None = None
    hold_id: int | None = None
    transcript: str | None = Field(None, max_length=100_000)
    ended_at: str | None = None


@app.get("/internal/events", dependencies=[Depends(internal_auth)])
def internal_events(conn=Depends(get_conn)):
    return logic.list_events(conn)


@app.post("/internal/check", dependencies=[Depends(internal_auth)])
def internal_check(req: CheckReq, conn=Depends(get_conn)):
    try:
        return logic.check_package(conn, req.package_id, req.rooms)
    except KeyError:
        raise HTTPException(status_code=404, detail="Paket bulunamadı")


@app.post("/internal/hold", dependencies=[Depends(internal_auth)])
def internal_hold(req: HoldReq, conn=Depends(get_conn)):
    try:
        return logic.hold_rooms(conn, req.package_id, req.rooms, req.guest_name, req.phone)
    except logic.NotEnoughRooms as e:
        raise HTTPException(status_code=409, detail=str(e))
    except KeyError:
        raise HTTPException(status_code=404, detail="Paket bulunamadı")


@app.put("/internal/calls/{call_id}", dependencies=[Depends(internal_auth)])
def internal_save_call(call_id: str, req: CallSaveReq, conn=Depends(get_conn)):
    return logic.save_call(conn, call_id, **req.model_dump(exclude_none=True))


# ------------------------------------------------------------------ yönetim paneli uçları

class EventReq(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    check_in: str
    check_out: str
    venue: str = ""
    description: str = ""


class PackageReq(BaseModel):
    event_id: int
    name: str = Field(min_length=1, max_length=200)
    price_usd: float = Field(gt=0)
    total_rooms: int = Field(ge=0)
    inclusions: str = ""
    max_discount_pct: float = Field(0, ge=0, le=100)


class PackagePatch(BaseModel):
    name: str | None = None
    price_usd: float | None = Field(None, gt=0)
    total_rooms: int | None = Field(None, ge=0)
    inclusions: str | None = None
    max_discount_pct: float | None = Field(None, ge=0, le=100)
    active: bool | None = None


@app.get("/api/admin/events", dependencies=[Depends(admin_auth)])
def admin_events(conn=Depends(get_conn)):
    return logic.list_events(conn, include_past=True, only_active=False)


@app.post("/api/admin/events", dependencies=[Depends(admin_auth)])
def admin_create_event(req: EventReq, conn=Depends(get_conn)):
    try:
        return {"event_id": logic.create_event(conn, req.name, req.check_in, req.check_out, req.venue, req.description)}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/api/admin/packages", dependencies=[Depends(admin_auth)])
def admin_create_package(req: PackageReq, conn=Depends(get_conn)):
    try:
        pid = logic.create_package(conn, req.event_id, req.name, req.price_usd, req.total_rooms,
                                   req.inclusions, req.max_discount_pct)
    except KeyError:
        raise HTTPException(status_code=404, detail="Etkinlik bulunamadı")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"package_id": pid}


@app.patch("/api/admin/packages/{package_id}", dependencies=[Depends(admin_auth)])
def admin_patch_package(package_id: int, req: PackagePatch, conn=Depends(get_conn)):
    logic.update_package(conn, package_id, **req.model_dump(exclude_none=True))
    return {"ok": True}


@app.get("/api/admin/holds", dependencies=[Depends(admin_auth)])
def admin_holds(conn=Depends(get_conn)):
    return logic.list_holds(conn)


@app.post("/api/admin/holds/{hold_id}/{action}", dependencies=[Depends(admin_auth)])
def admin_hold_action(hold_id: int, action: str, conn=Depends(get_conn)):
    status = {"confirm": "confirmed", "release": "released"}.get(action)
    if status is None:
        raise HTTPException(status_code=404, detail="Bilinmeyen işlem")
    try:
        logic.set_hold_status(conn, hold_id, status)
    except KeyError:
        raise HTTPException(status_code=404, detail="Kayıt bulunamadı")
    return {"ok": True}


@app.get("/api/admin/calls", dependencies=[Depends(admin_auth)])
def admin_calls(conn=Depends(get_conn)):
    return logic.list_calls(conn)


@app.get("/api/admin/calls/{call_id}", dependencies=[Depends(admin_auth)])
def admin_call(call_id: str, conn=Depends(get_conn)):
    call = logic.get_call(conn, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Görüşme bulunamadı")
    return call
