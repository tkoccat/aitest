"""İş mantığı: müsaitlik, oda tutma, yukarı satış önerisi, görüşme kaydı."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

HOLD_HOURS = 24
OUTCOMES = {
    "reserved_hold",   # oda geçici tutuldu
    "positive",        # olumlu baktı
    "callback",        # sonra aranmak istiyor
    "thinking",        # düşünüyor
    "declined",        # istemedi
    "info_only",       # sadece bilgi aldı
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- yardımcılar

def _package_dict(row: sqlite3.Row, available: int) -> dict:
    return {
        "package_id": row["id"],
        "event_id": row["event_id"],
        "name": row["name"],
        "price_usd": row["price_usd"],
        "inclusions": [x for x in row["inclusions"].split("\n") if x.strip()],
        "total_rooms": row["total_rooms"],
        "available_rooms": available,
        "max_discount_pct": row["max_discount_pct"],
        "active": bool(row["active"]),
    }


def available_rooms(conn: sqlite3.Connection, package_id: int, now: datetime | None = None) -> int:
    now = now or utcnow()
    row = conn.execute(
        """
        SELECT p.total_rooms - COALESCE(SUM(h.rooms), 0) AS free
        FROM packages p
        LEFT JOIN holds h
          ON h.package_id = p.id
         AND (h.status = 'confirmed' OR (h.status = 'held' AND h.expires_at > ?))
        WHERE p.id = ?
        """,
        (iso(now), package_id),
    ).fetchone()
    if row is None:
        raise KeyError(f"package {package_id} not found")
    return max(int(row["free"]), 0)


def _packages_of_event(conn, event_id: int, now: datetime | None, only_active=True) -> list[dict]:
    q = "SELECT * FROM packages WHERE event_id = ?"
    if only_active:
        q += " AND active = 1"
    q += " ORDER BY price_usd ASC"
    return [_package_dict(r, available_rooms(conn, r["id"], now)) for r in conn.execute(q, (event_id,))]


# ---------------------------------------------------------------- etkinlikler

def list_events(conn, now: datetime | None = None, today: date | None = None,
                include_past: bool = False, only_active: bool = True) -> list[dict]:
    today = today or (now or utcnow()).date()
    q = "SELECT * FROM events"
    conds, params = [], []
    if only_active:
        conds.append("active = 1")
    if not include_past:
        conds.append("check_out >= ?")
        params.append(today.isoformat())
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY check_in ASC"
    out = []
    for e in conn.execute(q, params):
        out.append({
            "event_id": e["id"],
            "name": e["name"],
            "venue": e["venue"],
            "description": e["description"],
            "check_in": e["check_in"],
            "check_out": e["check_out"],
            "active": bool(e["active"]),
            "packages": _packages_of_event(conn, e["id"], now, only_active=only_active),
        })
    return out


def create_event(conn, name: str, check_in: str, check_out: str,
                 venue: str = "", description: str = "") -> int:
    if date.fromisoformat(check_out) < date.fromisoformat(check_in):
        raise ValueError("check_out, check_in tarihinden önce olamaz")
    cur = conn.execute(
        "INSERT INTO events (name, venue, description, check_in, check_out) VALUES (?,?,?,?,?)",
        (name, venue, description, check_in, check_out),
    )
    return cur.lastrowid


def create_package(conn, event_id: int, name: str, price_usd: float, total_rooms: int,
                   inclusions: str = "", max_discount_pct: float = 0) -> int:
    if price_usd <= 0 or total_rooms < 0:
        raise ValueError("fiyat pozitif, oda sayısı negatif olmayan bir sayı olmalı")
    if not 0 <= max_discount_pct <= 100:
        raise ValueError("max_discount_pct 0-100 arasında olmalı")
    if conn.execute("SELECT 1 FROM events WHERE id = ?", (event_id,)).fetchone() is None:
        raise KeyError(f"event {event_id} not found")
    cur = conn.execute(
        "INSERT INTO packages (event_id, name, price_usd, inclusions, total_rooms, max_discount_pct) "
        "VALUES (?,?,?,?,?,?)",
        (event_id, name, price_usd, inclusions, total_rooms, max_discount_pct),
    )
    return cur.lastrowid


def update_package(conn, package_id: int, **fields) -> None:
    allowed = {"name", "price_usd", "inclusions", "total_rooms", "max_discount_pct", "active"}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            sets.append(f"{k} = ?")
            params.append(int(v) if k == "active" else v)
    if not sets:
        return
    params.append(package_id)
    conn.execute(f"UPDATE packages SET {', '.join(sets)} WHERE id = ?", params)


# ---------------------------------------------------------------- müsaitlik ve yukarı satış

def check_package(conn, package_id: int, rooms: int = 1, now: datetime | None = None) -> dict:
    """İstenen paketin durumunu ve satış önerisini döner.

    recommendation:
      book_requested   -> istenen paket müsait (üst paket opsiyonel olarak bir kez anılabilir)
      offer_upgrade    -> istenen dolu/yetersiz, bir üst paket müsait: onu öner
      offer_alternative-> üst paket yok ama daha ucuz paket müsait
      sold_out         -> bu etkinlikte uygun paket kalmadı
    """
    if rooms < 1:
        raise ValueError("rooms en az 1 olmalı")
    row = conn.execute("SELECT * FROM packages WHERE id = ? AND active = 1", (package_id,)).fetchone()
    if row is None:
        raise KeyError(f"package {package_id} not found")
    free = available_rooms(conn, package_id, now)
    requested = _package_dict(row, free)
    siblings = [p for p in _packages_of_event(conn, row["event_id"], now) if p["package_id"] != package_id]
    higher = [p for p in siblings if p["price_usd"] > row["price_usd"] and p["available_rooms"] >= rooms]
    lower = [p for p in siblings if p["price_usd"] < row["price_usd"] and p["available_rooms"] >= rooms]
    upgrade = higher[0] if higher else None  # fiyata göre artan sıralı, ilk = en yakın üst paket

    if free >= rooms:
        return {"recommendation": "book_requested", "requested_rooms": rooms, "package": requested,
                "optional_upgrade": upgrade, "alternatives": []}
    if upgrade:
        return {"recommendation": "offer_upgrade", "requested_rooms": rooms, "package": requested,
                "optional_upgrade": None, "alternatives": [upgrade]}
    if lower:
        return {"recommendation": "offer_alternative", "requested_rooms": rooms, "package": requested,
                "optional_upgrade": None, "alternatives": list(reversed(lower))}
    return {"recommendation": "sold_out", "requested_rooms": rooms, "package": requested,
            "optional_upgrade": None, "alternatives": []}


# ---------------------------------------------------------------- oda tutma

class NotEnoughRooms(Exception):
    pass


def hold_rooms(conn, package_id: int, rooms: int, guest_name: str, phone: str,
               now: datetime | None = None, hours: int = HOLD_HOURS) -> dict:
    now = now or utcnow()
    if rooms < 1:
        raise ValueError("rooms en az 1 olmalı")
    conn.execute("BEGIN IMMEDIATE")  # eşzamanlı çift satışı engeller
    try:
        pkg = conn.execute("SELECT * FROM packages WHERE id = ? AND active = 1", (package_id,)).fetchone()
        if pkg is None:
            raise KeyError(f"package {package_id} not found")
        free = available_rooms(conn, package_id, now)
        if free < rooms:
            raise NotEnoughRooms(f"{free} oda kaldı, {rooms} oda istendi")
        expires = now + timedelta(hours=hours)
        cur = conn.execute(
            "INSERT INTO holds (package_id, rooms, guest_name, phone, status, created_at, expires_at) "
            "VALUES (?,?,?,?, 'held', ?, ?)",
            (package_id, rooms, guest_name, phone, iso(now), iso(expires)),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"hold_id": cur.lastrowid, "package_id": package_id, "rooms": rooms,
            "expires_at": iso(expires), "price_usd": pkg["price_usd"]}


def set_hold_status(conn, hold_id: int, status: str) -> None:
    if status not in {"confirmed", "released"}:
        raise ValueError("status confirmed veya released olmalı")
    cur = conn.execute("UPDATE holds SET status = ? WHERE id = ? AND status != 'released'", (status, hold_id))
    if cur.rowcount == 0:
        raise KeyError(f"hold {hold_id} not found or already released")


def list_holds(conn, now: datetime | None = None) -> list[dict]:
    now = now or utcnow()
    rows = conn.execute(
        """
        SELECT h.*, p.name AS package_name, p.price_usd, e.name AS event_name
        FROM holds h JOIN packages p ON p.id = h.package_id JOIN events e ON e.id = p.event_id
        ORDER BY h.id DESC
        """
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d["status"] == "held" and d["expires_at"] <= iso(now):
            d["status"] = "expired"
        out.append(d)
    return out


# ---------------------------------------------------------------- görüşme kayıtları

def save_call(conn, call_id: str, now: datetime | None = None, **fields) -> dict:
    now = now or utcnow()
    allowed = {"language", "guest_name", "phone", "outcome", "summary", "notes", "callback_time",
               "event_id", "package_id", "hold_id", "transcript", "ended_at"}
    data = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if "outcome" in data and data["outcome"] not in OUTCOMES:
        data["outcome"] = "info_only"
    existing = conn.execute("SELECT 1 FROM calls WHERE call_id = ?", (call_id,)).fetchone()
    if existing is None:
        conn.execute("INSERT INTO calls (call_id, started_at) VALUES (?, ?)", (call_id, iso(now)))
    if data:
        sets = ", ".join(f"{k} = ?" for k in data)
        conn.execute(f"UPDATE calls SET {sets} WHERE call_id = ?", [*data.values(), call_id])
    return dict(conn.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,)).fetchone())


def list_calls(conn, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT call_id, started_at, ended_at, language, guest_name, phone, outcome, summary, callback_time "
        "FROM calls ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_call(conn, call_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------- demo verisi

def next_weekday(today: date, weekday: int) -> date:
    """Bugünden sonraki ilk `weekday` (0=Pzt ... 4=Cum). Bugün de o gün ise 7 gün sonrası."""
    delta = (weekday - today.weekday()) % 7 or 7
    return today + timedelta(days=delta)


def seed_demo(conn, today: date | None = None) -> bool:
    """Veritabanı boşsa örnek (kurgusal) veri ekler. Gerçek kişi/otel adı kullanılmaz."""
    if conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] > 0:
        return False
    today = today or utcnow().date()
    fri = next_weekday(today, 4)
    ev1 = create_event(
        conn, "Yaz Finali Konseri (Demo)", fri.isoformat(), (fri + timedelta(days=2)).isoformat(),
        venue="Demo Resort Otel, Kıbrıs",
        description="Kurgusal sanatçı 'Demo Sanatçı' ile cumartesi gecesi büyük konser. "
                    "Cuma giriş, pazar gece çıkış.",
    )
    create_package(conn, ev1, "Standart Konser Paketi", 5000, 20,
                   "Cuma giriş - Pazar gece çıkış\nHer şey dahil konaklama\nKonser girişi\nHavalimanı transferi",
                   max_discount_pct=0)
    create_package(conn, ev1, "Deluxe Sahne Önü Paketi", 7000, 8,
                   "Cuma giriş - Pazar gece çıkış\nHer şey dahil konaklama\nSahne önü VIP konser girişi\n"
                   "Deniz manzaralı geniş oda\nÖzel transfer ve karşılama", max_discount_pct=5)
    sat = fri + timedelta(days=7)
    ev2 = create_event(
        conn, "Sonbahar Gala Gecesi (Demo)", sat.isoformat(), (sat + timedelta(days=2)).isoformat(),
        venue="Demo Resort Otel, Kıbrıs",
        description="Davetli misafirler için gala yemeği ve canlı performans gecesi.",
    )
    create_package(conn, ev2, "Gala Paketi", 4200, 15,
                   "Cuma giriş - Pazar çıkış\nHer şey dahil konaklama\nGala yemeği ve gösteri")
    create_package(conn, ev2, "Gala VIP Paketi", 6200, 6,
                   "Cuma giriş - Pazar çıkış\nHer şey dahil konaklama\nVIP masa\nÖzel transfer")
    return True
