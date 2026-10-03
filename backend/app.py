import asyncio
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from routes_auth import router as auth_router
from routes_cases import router as cases_router
from routes_osint import router as osint_router
from routes_run import router as run_router
from routes_graph import router as graph_router
from routes_timeline import router as timeline_router
from routes_reports import router as reports_router
from routes_monitor import router as monitor_router
from auth import get_current_user
from collector.base import collect_async

load_dotenv()


def _ensure_monitor_tables():
    """Create base schema and monitoring tables if they don't exist (safe on existing DBs)."""
    import os, logging
    from auth import get_db_conn
    log = logging.getLogger("aria.migrate")
    try:
        conn = get_db_conn()
    except Exception as e:
        log.warning(f"Database connection not available on startup: {e}")
        return

    try:
        cur = conn.cursor()
        # Ensure base schema is present
        cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'users'")
        if not cur.fetchone():
            log.info("Base schema missing, running schema.sql...")
            schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
            if os.path.exists(schema_path):
                with open(schema_path, "r", encoding="utf-8") as f:
                    cur.execute(f.read())
                conn.commit()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS monitor_targets (
                id                SERIAL PRIMARY KEY,
                case_id           INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
                created_by        INTEGER NOT NULL REFERENCES users(id),
                account_id        INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
                identifier_id     INTEGER REFERENCES case_identifiers(id) ON DELETE CASCADE,
                status            TEXT NOT NULL DEFAULT 'pending_baseline'
                                  CHECK (status IN (
                                      'pending_baseline', 'active', 'paused',
                                      'degraded', 'expired', 'revoked'
                                  )),
                reason            TEXT NOT NULL,
                permitted_sources JSONB NOT NULL DEFAULT '["platform_recollect", "maigret", "dorking", "breach"]',
                interval_seconds  INTEGER NOT NULL DEFAULT 3600,
                next_check_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_checked_at   TIMESTAMPTZ,
                expires_at        TIMESTAMPTZ NOT NULL,
                created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT monitor_targets_one_target
                    CHECK (
                        (account_id IS NOT NULL AND identifier_id IS NULL) OR
                        (account_id IS NULL AND identifier_id IS NOT NULL)
                    )
            );
            CREATE INDEX IF NOT EXISTS monitor_targets_case_id_idx ON monitor_targets(case_id);
            CREATE INDEX IF NOT EXISTS monitor_targets_next_check_idx
                ON monitor_targets(next_check_at) WHERE status IN ('active', 'pending_baseline');

            CREATE TABLE IF NOT EXISTS monitor_snapshots (
                id            SERIAL PRIMARY KEY,
                target_id     INTEGER NOT NULL REFERENCES monitor_targets(id) ON DELETE CASCADE,
                source        TEXT NOT NULL,
                snapshot_json JSONB NOT NULL,
                content_hash  TEXT NOT NULL,
                is_baseline   BOOLEAN NOT NULL DEFAULT false,
                observed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS monitor_snapshots_target_id_idx ON monitor_snapshots(target_id);

            CREATE TABLE IF NOT EXISTS monitor_events (
                id                SERIAL PRIMARY KEY,
                target_id         INTEGER NOT NULL REFERENCES monitor_targets(id) ON DELETE CASCADE,
                event_type        TEXT NOT NULL
                                  CHECK (event_type IN (
                                      'profile_changed', 'new_posts', 'network_changed',
                                      'account_discovered', 'account_disappeared',
                                      'breach_detected', 'correlation_drift',
                                      'new_web_mention', 'hard_link_found'
                                  )),
                source            TEXT NOT NULL,
                title             TEXT NOT NULL,
                previous_json     JSONB,
                current_json      JSONB,
                evidence_json     JSONB,
                source_url        TEXT,
                confidence        TEXT CHECK (confidence IN ('high', 'medium', 'low')),
                priority          TEXT NOT NULL DEFAULT 'normal'
                                  CHECK (priority IN ('low', 'normal', 'high', 'critical')),
                fingerprint       TEXT NOT NULL,
                observed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
                created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS monitor_events_target_id_idx ON monitor_events(target_id);
            CREATE UNIQUE INDEX IF NOT EXISTS monitor_events_dedup_idx
                ON monitor_events(target_id, source, fingerprint);

            CREATE TABLE IF NOT EXISTS alerts (
                id               SERIAL PRIMARY KEY,
                case_id          INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
                monitor_event_id INTEGER REFERENCES monitor_events(id) ON DELETE SET NULL,
                investigator_id  INTEGER NOT NULL REFERENCES users(id),
                status           TEXT NOT NULL DEFAULT 'unread'
                                 CHECK (status IN ('unread', 'read', 'acknowledged', 'dismissed')),
                title            TEXT NOT NULL,
                message          TEXT NOT NULL,
                priority         TEXT NOT NULL DEFAULT 'normal'
                                 CHECK (priority IN ('low', 'normal', 'high', 'critical')),
                created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
                read_at          TIMESTAMPTZ,
                acknowledged_at  TIMESTAMPTZ,
                dismissed_at     TIMESTAMPTZ,
                dismiss_reason   TEXT
            );
            CREATE INDEX IF NOT EXISTS alerts_investigator_status_idx ON alerts(investigator_id, status);
            CREATE INDEX IF NOT EXISTS alerts_case_id_idx ON alerts(case_id);
        """)
        conn.commit()
        log.info("Monitoring tables verified/created")
    except Exception:
        conn.rollback()
        log.exception("Failed to create monitoring tables")
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _ensure_monitor_tables()
    from monitor_engine import start_scheduler
    task = asyncio.create_task(start_scheduler())
    yield
    task.cancel()


app = FastAPI(title="ARIA API", lifespan=lifespan)

# ── CORS ──────────────────────────────────────────────────────────────────────
# IMPORTANT: allow_credentials=True requires an explicit origin list — NOT "*".
# Using "*" with credentials silently breaks cookie auth.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost",
        "http://localhost:80",
        "http://localhost:5173",
    ],
    allow_credentials=True,  # Required for HttpOnly cookies
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(auth_router)
app.include_router(cases_router)
app.include_router(osint_router)
app.include_router(run_router)
app.include_router(graph_router)
app.include_router(timeline_router)
app.include_router(reports_router)
app.include_router(monitor_router)


# ── Existing routes ───────────────────────────────────────────────────────────
@app.get("/collect/{platform}/{username}")
async def collect(
    platform: str,
    username: str,
    limit: int = 50,
    current_user: dict = Depends(get_current_user),
):
    """Collect public profile data. Requires authentication."""
    try:
        profile = await collect_async(platform, username, limit=limit)
        return profile.to_dict()
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
