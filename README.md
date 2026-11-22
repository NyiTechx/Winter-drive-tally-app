# Winter Great Drive — Vercel + Python Deployment Guide

## 1. Project Structure
- `api/index.py`: Python (FastAPI) serverless backend handling phone verification, duplicate blocking, family slots (up to 2 parents + 3 kids), 2-for-1 referral ticket math, raffle draws, totals aggregation, and CSV export.
- `public/index.html`: Responsive frontend connected to `/api/*`.
- `vercel.json`: Routes `/api/*` to `api/index.py`.
- `requirements.txt`: Python dependencies (`fastapi`, `pydantic`, `psycopg2-binary`, `twilio`).

## 2. Required Environment Variables for Live Production on Vercel
In your Vercel Project Settings -> Environment Variables, add:

1. **Database (Required for persistent data across users/devices):**
   - `DATABASE_URL`: Your PostgreSQL connection string (e.g., from Neon Postgres or Supabase Postgres via Vercel Marketplace).
   - *Note:* If `DATABASE_URL` is omitted, the app falls back to `/tmp/winter_drive.db` (ephemeral SQLite), which resets when serverless functions restart.

2. **Real SMS Verification (Required to send real text messages):**
   - `TWILIO_ACCOUNT_SID`
   - `TWILIO_AUTH_TOKEN`
   - `TWILIO_VERIFY_SERVICE_SID`
   - *Note:* If Twilio variables are omitted, the app runs in **Demo OTP Mode** and displays the 6-digit code on screen for testing.

3. **Organizer Dashboard PIN:**
   - `ADMIN_PIN`: Set a strong PIN (defaults to `1234` if unset).
