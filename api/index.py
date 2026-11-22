import csv
import io
import json
import os
import random
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

app = FastAPI(title="Winter Great Drive API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
SQLITE_PATH = os.environ.get("WINTER_DRIVE_DB_PATH", "/tmp/winter_drive.db")
ADMIN_PIN = os.environ.get("ADMIN_PIN", "1234").strip() or "1234"

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_VERIFY_SERVICE_SID = os.environ.get("TWILIO_VERIFY_SERVICE_SID", "").strip()


def use_postgres() -> bool:
    return bool(DATABASE_URL)


def use_twilio() -> bool:
    return bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_VERIFY_SERVICE_SID)


def get_conn():
    if use_postgres():
        import psycopg2
        from psycopg2.extras import RealDictCursor
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        return conn, True
    conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn, False


def init_db():
    conn, is_pg = get_conn()
    try:
        cur = conn.cursor()
        if is_pg:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS families (
                    id SERIAL PRIMARY KEY,
                    phone TEXT UNIQUE NOT NULL,
                    share_code TEXT UNIQUE NOT NULL,
                    referred_by TEXT,
                    referral_count INTEGER NOT NULL DEFAULT 0,
                    raffle_opt_in BOOLEAN NOT NULL DEFAULT TRUE,
                    parents_json TEXT NOT NULL,
                    kids_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS otp_codes (
                    phone TEXT PRIMARY KEY,
                    code TEXT NOT NULL,
                    expires_at DOUBLE PRECISION NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS verification_tokens (
                    token TEXT PRIMARY KEY,
                    phone TEXT UNIQUE NOT NULL,
                    expires_at DOUBLE PRECISION NOT NULL
                )
                """
            )
        else:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS families (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT UNIQUE NOT NULL,
                    share_code TEXT UNIQUE NOT NULL,
                    referred_by TEXT,
                    referral_count INTEGER NOT NULL DEFAULT 0,
                    raffle_opt_in INTEGER NOT NULL DEFAULT 1,
                    parents_json TEXT NOT NULL,
                    kids_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS otp_codes (
                    phone TEXT PRIMARY KEY,
                    code TEXT NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS verification_tokens (
                    token TEXT PRIMARY KEY,
                    phone TEXT UNIQUE NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
        conn.commit()
    finally:
        conn.close()


def p_sql(query: str, is_pg: bool) -> str:
    return query.replace("?", "%s") if is_pg else query


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        raise HTTPException(status_code=400, detail="Please enter a valid 10-digit US phone number.")
    return digits


def format_phone(digits: str) -> str:
    if len(digits) != 10:
        return digits
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


def calc_tickets(raffle_opt_in: bool, referral_count: int) -> int:
    if not raffle_opt_in:
        return 0
    return 1 + (max(0, referral_count) // 2)


def require_admin(x_admin_pin: Optional[str]):
    if not x_admin_pin or x_admin_pin.strip() != ADMIN_PIN:
        raise HTTPException(status_code=401, detail="Invalid or missing Admin PIN.")


class PersonGear(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    coat: str = Field(..., min_length=1, max_length=30)
    glove: str = Field(..., min_length=1, max_length=30)
    hat: str = Field(..., min_length=1, max_length=30)


class SendOtpRequest(BaseModel):
    phone: str


class VerifyOtpRequest(BaseModel):
    phone: str
    code: str


class RegisterRequest(BaseModel):
    phone: str
    verification_token: str
    referred_by: Optional[str] = None
    raffle_opt_in: bool = True
    parents: List[PersonGear]
    kids: List[PersonGear] = Field(default_factory=list)


class DrawRequest(BaseModel):
    prize: str = Field(..., pattern="^(ps5|tv)$")



@app.get("/", response_class=HTMLResponse)
def serve_home():
    index_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "public", "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), status_code=200)
    return HTMLResponse(content="<h1>Winter Great Drive API is running</h1><p>public/index.html not found at root.</p>", status_code=200)


@app.get("/api/health")
def health():
    init_db()
    return {
        "status": "ok",
        "database_mode": "postgres" if use_postgres() else "sqlite_ephemeral_demo",
        "sms_mode": "twilio_verify" if use_twilio() else "demo_simulated_otp",
        "admin_pin_is_default": ADMIN_PIN == "1234",
    }


@app.post("/api/send-otp")
def send_otp(payload: SendOtpRequest):
    init_db()
    phone = normalize_phone(payload.phone)
    conn, is_pg = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(p_sql("SELECT share_code FROM families WHERE phone = ?", is_pg), (phone,))
        existing = cur.fetchone()
        if existing:
            code = existing["share_code"] if isinstance(existing, dict) or hasattr(existing, "keys") else existing[0]
            raise HTTPException(
                status_code=409,
                detail=f"This phone number is already registered (Share Code: {code}). Duplicate signups are blocked.",
            )

        if use_twilio():
            from twilio.rest import Client
            client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            client.verify.v2.services(TWILIO_VERIFY_SERVICE_SID).verifications.create(
                to=f"+1{phone}", channel="sms"
            )
            return {"sent": True, "demo_mode": False, "phone": phone}

        code = f"{random.randint(100000, 999999)}"
        expires_at = time.time() + 600
        if is_pg:
            cur.execute(
                """
                INSERT INTO otp_codes (phone, code, expires_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (phone) DO UPDATE SET code = EXCLUDED.code, expires_at = EXCLUDED.expires_at
                """,
                (phone, code, expires_at),
            )
        else:
            cur.execute(
                "INSERT OR REPLACE INTO otp_codes (phone, code, expires_at) VALUES (?, ?, ?)",
                (phone, code, expires_at),
            )
        conn.commit()
        return {"sent": True, "demo_mode": True, "demo_code": code, "phone": phone}
    finally:
        conn.close()


@app.post("/api/verify-otp")
def verify_otp(payload: VerifyOtpRequest):
    init_db()
    phone = normalize_phone(payload.phone)
    code = (payload.code or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        raise HTTPException(status_code=400, detail="Enter a 6-digit verification code.")

    conn, is_pg = get_conn()
    try:
        cur = conn.cursor()
        if use_twilio():
            from twilio.rest import Client
            client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            check = client.verify.v2.services(TWILIO_VERIFY_SERVICE_SID).verification_checks.create(
                to=f"+1{phone}", code=code
            )
            if check.status != "approved":
                raise HTTPException(status_code=400, detail="Invalid or expired verification code.")
        else:
            cur.execute(p_sql("SELECT code, expires_at FROM otp_codes WHERE phone = ?", is_pg), (phone,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=400, detail="No verification code requested for this number.")
            stored_code = row["code"]
            expires_at = float(row["expires_at"])
            if time.time() > expires_at:
                raise HTTPException(status_code=400, detail="Verification code expired. Please request a new one.")
            if code != stored_code:
                raise HTTPException(status_code=400, detail="Incorrect verification code.")
            cur.execute(p_sql("DELETE FROM otp_codes WHERE phone = ?", is_pg), (phone,))

        token = secrets.token_urlsafe(24)
        token_exp = time.time() + 1800
        if is_pg:
            cur.execute(
                """
                INSERT INTO verification_tokens (token, phone, expires_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (phone) DO UPDATE SET token = EXCLUDED.token, expires_at = EXCLUDED.expires_at
                """,
                (token, phone, token_exp),
            )
        else:
            cur.execute(
                "INSERT OR REPLACE INTO verification_tokens (token, phone, expires_at) VALUES (?, ?, ?)",
                (token, phone, token_exp),
            )
        conn.commit()
        return {"verified": True, "phone": phone, "verification_token": token}
    finally:
        conn.close()


@app.post("/api/register")
def register_family(payload: RegisterRequest):
    if len(payload.parents) < 1 or len(payload.parents) > 2:
        raise HTTPException(status_code=422, detail="Must include 1 or 2 parents/guardians.")
    if len(payload.kids) > 3:
        raise HTTPException(status_code=422, detail="Maximum of 3 kids allowed per household.")
    for person in payload.parents + payload.kids:
        if not person.name.strip() or not person.coat.strip() or not person.glove.strip() or not person.hat.strip():
            raise HTTPException(status_code=422, detail="All name and size fields must be non-empty.")
    init_db()
    phone = normalize_phone(payload.phone)
    conn, is_pg = get_conn()
    try:
        cur = conn.cursor()

        # Verify token
        cur.execute(
            p_sql("SELECT phone, expires_at FROM verification_tokens WHERE token = ?", is_pg),
            (payload.verification_token,),
        )
        tok_row = cur.fetchone()
        if not tok_row or tok_row["phone"] != phone or time.time() > float(tok_row["expires_at"]):
            raise HTTPException(status_code=403, detail="Phone verification expired or invalid. Please verify again.")

        # Block duplicate phone
        cur.execute(p_sql("SELECT share_code FROM families WHERE phone = ?", is_pg), (phone,))
        if cur.fetchone():
            raise HTTPException(status_code=409, detail="This phone number is already registered.")

        # Check referral code if provided
        ref_code = (payload.referred_by or "").strip().upper() or None
        valid_ref_code = None
        if ref_code:
            cur.execute(p_sql("SELECT share_code FROM families WHERE share_code = ?", is_pg), (ref_code,))
            if cur.fetchone():
                valid_ref_code = ref_code

        # Generate unique share code
        share_code = None
        for _ in range(25):
            candidate = f"WGD-{random.randint(1000, 9999)}"
            cur.execute(p_sql("SELECT 1 FROM families WHERE share_code = ?", is_pg), (candidate,))
            if not cur.fetchone():
                share_code = candidate
                break
        if not share_code:
            share_code = f"WGD-{secrets.token_hex(3).upper()}"

        now_iso = datetime.now(timezone.utc).isoformat()
        parents_json = json.dumps([p.dict() for p in payload.parents])
        kids_json = json.dumps([k.dict() for k in payload.kids])
        opt_val = payload.raffle_opt_in if is_pg else (1 if payload.raffle_opt_in else 0)

        cur.execute(
            p_sql(
                """
                INSERT INTO families (phone, share_code, referred_by, referral_count, raffle_opt_in, parents_json, kids_json, created_at)
                VALUES (?, ?, ?, 0, ?, ?, ?, ?)
                """,
                is_pg,
            ),
            (phone, share_code, valid_ref_code, opt_val, parents_json, kids_json, now_iso),
        )

        if valid_ref_code:
            cur.execute(
                p_sql("UPDATE families SET referral_count = referral_count + 1 WHERE share_code = ?", is_pg),
                (valid_ref_code,),
            )

        cur.execute(p_sql("DELETE FROM verification_tokens WHERE token = ?", is_pg), (payload.verification_token,))
        conn.commit()

        return {
            "registered": True,
            "phone": phone,
            "formatted_phone": format_phone(phone),
            "share_code": share_code,
            "referred_by": valid_ref_code,
            "raffle_opt_in": payload.raffle_opt_in,
            "raffle_tickets": calc_tickets(payload.raffle_opt_in, 0),
            "parents_count": len(payload.parents),
            "kids_count": len(payload.kids),
        }
    finally:
        conn.close()


@app.get("/api/lookup")
def lookup_family(q: str = Query(..., min_length=3)):
    init_db()
    cleaned = q.strip()
    upper_code = cleaned.upper()
    digits = re.sub(r"\D", "", cleaned)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]

    conn, is_pg = get_conn()
    try:
        cur = conn.cursor()
        if len(digits) == 10:
            cur.execute(p_sql("SELECT * FROM families WHERE phone = ?", is_pg), (digits,))
        else:
            cur.execute(p_sql("SELECT * FROM families WHERE share_code = ?", is_pg), (upper_code,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="No registration found matching that phone number or share code.")

        parents = json.loads(row["parents_json"])
        kids = json.loads(row["kids_json"])
        opt_in = bool(row["raffle_opt_in"])
        ref_count = int(row["referral_count"])
        return {
            "phone": format_phone(row["phone"]),
            "share_code": row["share_code"],
            "referral_count": ref_count,
            "bonus_tickets": ref_count // 2,
            "raffle_opt_in": opt_in,
            "raffle_tickets": calc_tickets(opt_in, ref_count),
            "parents": parents,
            "kids": kids,
            "created_at": row["created_at"],
        }
    finally:
        conn.close()


def _fetch_all_families():
    init_db()
    conn, _ = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM families ORDER BY id DESC")
        rows = cur.fetchall()
        result = []
        for r in rows:
            parents = json.loads(r["parents_json"])
            kids = json.loads(r["kids_json"])
            opt_in = bool(r["raffle_opt_in"])
            ref_count = int(r["referral_count"])
            result.append({
                "id": r["id"],
                "phone": r["phone"],
                "formatted_phone": format_phone(r["phone"]),
                "share_code": r["share_code"],
                "referred_by": r["referred_by"],
                "referral_count": ref_count,
                "raffle_opt_in": opt_in,
                "raffle_tickets": calc_tickets(opt_in, ref_count),
                "parents": parents,
                "kids": kids,
                "created_at": r["created_at"],
            })
        return result
    finally:
        conn.close()


@app.get("/api/admin/totals")
def admin_totals(x_admin_pin: Optional[str] = Header(default=None)):
    require_admin(x_admin_pin)
    families = _fetch_all_families()

    counts = {"coats": {}, "gloves": {}, "hats": {}}
    parent_count = 0
    kid_count = 0
    total_gear = 0
    opted_in_count = 0
    total_tickets = 0
    bonus_tickets = 0

    for fam in families:
        parent_count += len(fam["parents"])
        kid_count += len(fam["kids"])
        if fam["raffle_opt_in"]:
            opted_in_count += 1
            b = fam["referral_count"] // 2
            bonus_tickets += b
            total_tickets += 1 + b
        for person in fam["parents"] + fam["kids"]:
            for gear_key, size_key in [("coat", "coats"), ("glove", "gloves"), ("hat", "hats")]:
                sz = person.get(gear_key, "")
                if sz and sz != "None needed":
                    counts[size_key][sz] = counts[size_key].get(sz, 0) + 1
                    total_gear += 1

    return {
        "total_families": len(families),
        "parent_count": parent_count,
        "kid_count": kid_count,
        "total_people": parent_count + kid_count,
        "total_gear": total_gear,
        "opted_in_count": opted_in_count,
        "total_tickets": total_tickets,
        "bonus_tickets": bonus_tickets,
        "size_counts": counts,
        "families": families,
    }


@app.post("/api/admin/draw")
def admin_draw(payload: DrawRequest, x_admin_pin: Optional[str] = Header(default=None)):
    require_admin(x_admin_pin)
    families = _fetch_all_families()
    pool = []
    for fam in families:
        for _ in range(fam["raffle_tickets"]):
            pool.append(fam)

    if not pool:
        raise HTTPException(status_code=400, detail="No eligible raffle tickets in the pool yet.")

    winner = random.choice(pool)
    return {
        "prize": payload.prize,
        "winner_name": winner["parents"][0]["name"] if winner["parents"] else "Participant",
        "winner_phone": winner["formatted_phone"],
        "share_code": winner["share_code"],
        "tickets_held": winner["raffle_tickets"],
        "pool_size": len(pool),
    }


@app.get("/api/admin/export-csv")
def admin_export_csv(x_admin_pin: Optional[str] = Header(default=None)):
    require_admin(x_admin_pin)
    families = _fetch_all_families()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Primary Contact", "Phone", "Parents", "Kids", "Share Code",
        "Referred By", "Referrals", "Raffle Opt-In", "Raffle Tickets", "Sizes Detail"
    ])
    for fam in families:
        detail = " | ".join(
            [f"P{i+1}:{p['name']}(Coat:{p['coat']}/Glove:{p['glove']}/Hat:{p['hat']})" for i, p in enumerate(fam["parents"])]
            + [f"K{i+1}:{k['name']}(Coat:{k['coat']}/Glove:{k['glove']}/Hat:{k['hat']})" for i, k in enumerate(fam["kids"])]
        )
        writer.writerow([
            fam["parents"][0]["name"] if fam["parents"] else "",
            fam["formatted_phone"],
            len(fam["parents"]),
            len(fam["kids"]),
            fam["share_code"],
            fam["referred_by"] or "",
            fam["referral_count"],
            "Yes" if fam["raffle_opt_in"] else "No",
            fam["raffle_tickets"],
            detail,
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="winter_great_drive_signups.csv"'},
    )
