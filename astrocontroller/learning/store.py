"""
The learning store: what worked, under what conditions, on which rig.

The central design decision is that **the primary unit of evidence is the
epoch, not the change**. A/B deltas around a parameter change are sparse (a
dozen a night at most), noisy, and confounded by seeing that drifts underneath
the measurement. But every night also produces long stretches where the
parameters sit still -- "these settings held for 34 minutes in this seeing and
delivered 0.61 arcsec RMS". That evidence is dense, cheap, and directly
reusable.

So: `setting_epoch` rows are what the deterministic baseline and retrieval are
built on, and `param_change` rows supply the complementary ledger of what was
tried and what happened -- especially the failures, which are what stop the
model re-exploring the same dead end every night.

Everything is keyed by `rig_id`. `minMove` is in pixels and `aggression`
depends on mount mechanics, so transferring knowledge between rigs is not
merely useless but actively harmful.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from ..metrics.conditions import ConditionVector, bucket_key

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS rig (
          rig_id            INTEGER PRIMARY KEY,
          fingerprint       TEXT UNIQUE NOT NULL,
          mount_name        TEXT,
          guide_camera      TEXT,
          guide_pixel_scale REAL,
          main_camera       TEXT,
          focal_length_mm   REAL,
          phd2_profile      TEXT,
          created_utc       TEXT NOT NULL,
          last_seen_utc     TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS session (
          session_id           INTEGER PRIMARY KEY,
          rig_id               INTEGER NOT NULL REFERENCES rig(rig_id),
          started_utc          TEXT NOT NULL,
          ended_utc            TEXT,
          site_lat             REAL,
          site_lon             REAL,
          nina_profile         TEXT,
          baseline_params_json TEXT,
          app_version          TEXT
        );

        CREATE TABLE IF NOT EXISTS condition_sample (
          sample_id     INTEGER PRIMARY KEY,
          session_id    INTEGER NOT NULL REFERENCES session(session_id),
          t_utc         TEXT NOT NULL,
          bucket_key    TEXT,
          cond_json     TEXT NOT NULL,
          rms_total     REAL,
          rms_ra        REAL,
          rms_dec       REAL,
          n_samples     INTEGER,
          guide_hfd_px  REAL,
          guide_snr     REAL,
          nina_hfr      REAL,
          nina_stars    INTEGER,
          app_state     TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_cond_session
          ON condition_sample(session_id, t_utc);

        CREATE TABLE IF NOT EXISTS setting_epoch (
          epoch_id       INTEGER PRIMARY KEY,
          session_id     INTEGER NOT NULL REFERENCES session(session_id),
          rig_id         INTEGER NOT NULL REFERENCES rig(rig_id),
          start_utc      TEXT NOT NULL,
          end_utc        TEXT,
          params_json    TEXT NOT NULL,
          params_hash    TEXT NOT NULL,
          bucket_key     TEXT NOT NULL,
          close_reason   TEXT,
          usable_seconds REAL NOT NULL,
          n_samples      INTEGER NOT NULL,
          rms_total      REAL NOT NULL,
          rms_ra         REAL,
          rms_dec        REAL,
          guide_hfd_med  REAL,
          altitude_med   REAL,
          cond_json      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_epoch_lookup
          ON setting_epoch(rig_id, bucket_key, rms_total);
        CREATE INDEX IF NOT EXISTS ix_epoch_hash
          ON setting_epoch(rig_id, params_hash);

        CREATE TABLE IF NOT EXISTS param_change (
          change_id        INTEGER PRIMARY KEY,
          session_id       INTEGER NOT NULL REFERENCES session(session_id),
          rig_id           INTEGER NOT NULL REFERENCES rig(rig_id),
          t_utc            TEXT NOT NULL,
          source           TEXT NOT NULL,
          model_str        TEXT,
          axis             TEXT NOT NULL,
          param            TEXT NOT NULL,
          before_value     REAL NOT NULL,
          requested_value  REAL NOT NULL,
          applied_value    REAL NOT NULL,
          rationale        TEXT,
          bucket_key       TEXT NOT NULL,
          cond_json        TEXT NOT NULL,
          outcome          TEXT,
          before_rms_total REAL,
          before_n         INTEGER,
          after_rms_total  REAL,
          after_n          INTEGER,
          delta_rms_total  REAL,
          effect_sigma     REAL,
          confounded       INTEGER NOT NULL DEFAULT 0,
          reverted         INTEGER NOT NULL DEFAULT 0,
          revert_reason    TEXT,
          closed_at_utc    TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_change_lookup
          ON param_change(rig_id, bucket_key, axis, param);
        CREATE INDEX IF NOT EXISTS ix_change_recent
          ON param_change(rig_id, t_utc DESC);

        CREATE TABLE IF NOT EXISTS advice (
          advice_id     INTEGER PRIMARY KEY,
          session_id    INTEGER REFERENCES session(session_id),
          t_utc         TEXT NOT NULL,
          model_str     TEXT,
          latency_ms    INTEGER,
          prompt_chars  INTEGER,
          raw_response  TEXT,
          parsed_json   TEXT,
          verdict       TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS frame (
          frame_id      INTEGER PRIMARY KEY,
          session_id    INTEGER NOT NULL REFERENCES session(session_id),
          t_utc         TEXT NOT NULL,
          filename      TEXT,
          target        TEXT,
          filter        TEXT,
          exposure_s    REAL,
          gain          INTEGER,
          hfr           REAL,
          hfr_stdev     REAL,
          stars         INTEGER,
          mean          REAL,
          median        REAL,
          min_adu       REAL,
          max_adu       REAL,
          rms_text      TEXT,
          saturated     INTEGER NOT NULL DEFAULT 0,
          cloud_suspect INTEGER NOT NULL DEFAULT 0,
          flags_json    TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_frame_session
          ON frame(session_id, t_utc);
        """,
    ),
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def params_hash(params: dict) -> str:
    """Stable hash of a parameter set, rounded so float noise does not split it."""
    normalised = {str(k): round(float(v), 4) for k, v in sorted(params.items())}
    blob = json.dumps(normalised, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def rig_fingerprint(
    mount: Optional[str],
    guide_camera: Optional[str],
    main_camera: Optional[str],
    focal_length_mm: Optional[float],
    pixel_scale: Optional[float],
) -> str:
    parts = [
        (mount or "?").strip().lower(),
        (guide_camera or "?").strip().lower(),
        (main_camera or "?").strip().lower(),
        f"{focal_length_mm or 0:.0f}",
        # Bucket the scale so tiny reported differences do not fork the rig.
        f"{round(float(pixel_scale or 0) * 20) / 20:.2f}",
    ]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


@dataclass
class EpochRecord:
    session_id: int
    rig_id: int
    start_utc: str
    end_utc: str
    params: dict
    bucket_key: str
    close_reason: str
    usable_seconds: float
    n_samples: int
    rms_total: float
    rms_ra: Optional[float]
    rms_dec: Optional[float]
    guide_hfd_med: Optional[float]
    altitude_med: Optional[float]
    conditions: dict


class LearningStore:
    """
    Synchronous SQLite access.

    Intended to be driven from a single writer task (via `asyncio.to_thread`)
    so the event loop is never blocked and writes are naturally serialised.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        self.db.close()

    def _migrate(self) -> None:
        current = self.db.execute("PRAGMA user_version").fetchone()[0]
        for version, ddl in MIGRATIONS:
            if version > current:
                log.info("applying learning-store migration %d", version)
                self.db.executescript(ddl)
                self.db.execute(f"PRAGMA user_version = {version}")
                self.db.commit()

    # ── rig / session ──────────────────────────────────────────────────

    def ensure_rig(
        self,
        *,
        mount: Optional[str] = None,
        guide_camera: Optional[str] = None,
        main_camera: Optional[str] = None,
        focal_length_mm: Optional[float] = None,
        pixel_scale: Optional[float] = None,
        phd2_profile: Optional[str] = None,
    ) -> int:
        fp = rig_fingerprint(mount, guide_camera, main_camera, focal_length_mm, pixel_scale)
        now = utcnow()
        row = self.db.execute(
            "SELECT rig_id FROM rig WHERE fingerprint = ?", (fp,)
        ).fetchone()
        if row:
            self.db.execute(
                "UPDATE rig SET last_seen_utc = ? WHERE rig_id = ?", (now, row["rig_id"])
            )
            self.db.commit()
            return int(row["rig_id"])

        cur = self.db.execute(
            "INSERT INTO rig (fingerprint, mount_name, guide_camera, guide_pixel_scale,"
            " main_camera, focal_length_mm, phd2_profile, created_utc, last_seen_utc)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (fp, mount, guide_camera, pixel_scale, main_camera,
             focal_length_mm, phd2_profile, now, now),
        )
        self.db.commit()
        log.info("registered new rig %s (id=%s)", fp, cur.lastrowid)
        return int(cur.lastrowid)

    def start_session(
        self,
        rig_id: int,
        *,
        site_lat: Optional[float] = None,
        site_lon: Optional[float] = None,
        nina_profile: Optional[str] = None,
        baseline_params: Optional[dict] = None,
        app_version: str = "0.1.0",
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO session (rig_id, started_utc, site_lat, site_lon,"
            " nina_profile, baseline_params_json, app_version) VALUES (?,?,?,?,?,?,?)",
            (rig_id, utcnow(), site_lat, site_lon, nina_profile,
             json.dumps(baseline_params or {}), app_version),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def end_session(self, session_id: int) -> None:
        self.db.execute(
            "UPDATE session SET ended_utc = ? WHERE session_id = ?",
            (utcnow(), session_id),
        )
        self.db.commit()

    def baseline_params(self, session_id: int) -> dict:
        row = self.db.execute(
            "SELECT baseline_params_json FROM session WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not row or not row["baseline_params_json"]:
            return {}
        return json.loads(row["baseline_params_json"])

    # ── samples ────────────────────────────────────────────────────────

    def add_condition_sample(
        self,
        session_id: int,
        conditions: ConditionVector,
        *,
        n_samples: Optional[int] = None,
        guide_hfd_px: Optional[float] = None,
        nina_hfr: Optional[float] = None,
        nina_stars: Optional[int] = None,
        app_state: Optional[str] = None,
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO condition_sample (session_id, t_utc, bucket_key, cond_json,"
            " rms_total, rms_ra, rms_dec, n_samples, guide_hfd_px, guide_snr,"
            " nina_hfr, nina_stars, app_state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, utcnow(), bucket_key(conditions),
             json.dumps(conditions.as_dict()), conditions.rms_total,
             conditions.rms_ra, conditions.rms_dec, n_samples, guide_hfd_px,
             conditions.guide_snr, nina_hfr, nina_stars, app_state),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def add_frame(self, session_id: int, stats: Any, flags: Optional[dict] = None) -> int:
        flags = flags or {}
        cur = self.db.execute(
            "INSERT INTO frame (session_id, t_utc, filename, target, filter,"
            " exposure_s, gain, hfr, hfr_stdev, stars, mean, median, min_adu,"
            " max_adu, rms_text, saturated, cloud_suspect, flags_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, stats.date or utcnow(), stats.filename, stats.target,
             stats.filter, stats.exposure_s, stats.gain, stats.hfr,
             stats.hfr_stdev, stats.stars, stats.mean, stats.median, stats.min,
             stats.max, stats.rms_text,
             int(bool(flags.get("saturated"))),
             int(bool(flags.get("cloud_suspect"))),
             json.dumps(flags)),
        )
        self.db.commit()
        return int(cur.lastrowid)

    # ── epochs ─────────────────────────────────────────────────────────

    def add_epoch(self, record: EpochRecord) -> int:
        cur = self.db.execute(
            "INSERT INTO setting_epoch (session_id, rig_id, start_utc, end_utc,"
            " params_json, params_hash, bucket_key, close_reason, usable_seconds,"
            " n_samples, rms_total, rms_ra, rms_dec, guide_hfd_med, altitude_med,"
            " cond_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (record.session_id, record.rig_id, record.start_utc, record.end_utc,
             json.dumps(record.params), params_hash(record.params),
             record.bucket_key, record.close_reason, record.usable_seconds,
             record.n_samples, record.rms_total, record.rms_ra, record.rms_dec,
             record.guide_hfd_med, record.altitude_med,
             json.dumps(record.conditions)),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def epochs_for(
        self,
        rig_id: int,
        bucket: Optional[str] = None,
        *,
        limit: int = 500,
    ) -> list[sqlite3.Row]:
        if bucket:
            sql = ("SELECT * FROM setting_epoch WHERE rig_id = ? AND bucket_key = ?"
                   " ORDER BY start_utc DESC LIMIT ?")
            args: tuple = (rig_id, bucket, limit)
        else:
            sql = ("SELECT * FROM setting_epoch WHERE rig_id = ?"
                   " ORDER BY start_utc DESC LIMIT ?")
            args = (rig_id, limit)
        return list(self.db.execute(sql, args).fetchall())

    def global_median_rms(self, rig_id: int) -> Optional[float]:
        """Rig-wide median epoch RMS: the shrinkage prior for scoring."""
        rows = self.db.execute(
            "SELECT rms_total FROM setting_epoch WHERE rig_id = ? ORDER BY rms_total",
            (rig_id,),
        ).fetchall()
        if not rows:
            return None
        values = [r["rms_total"] for r in rows]
        mid = len(values) // 2
        if len(values) % 2:
            return float(values[mid])
        return float(0.5 * (values[mid - 1] + values[mid]))

    # ── changes ────────────────────────────────────────────────────────

    def add_change(
        self,
        *,
        session_id: int,
        rig_id: int,
        source: str,
        axis: str,
        param: str,
        before_value: float,
        requested_value: float,
        applied_value: float,
        conditions: ConditionVector,
        rationale: Optional[str] = None,
        model_str: Optional[str] = None,
        before_rms: Optional[float] = None,
        before_n: Optional[int] = None,
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO param_change (session_id, rig_id, t_utc, source, model_str,"
            " axis, param, before_value, requested_value, applied_value, rationale,"
            " bucket_key, cond_json, outcome, before_rms_total, before_n)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, rig_id, utcnow(), source, model_str, axis, param,
             before_value, requested_value, applied_value, rationale,
             bucket_key(conditions), json.dumps(conditions.as_dict()),
             "pending", before_rms, before_n),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def close_change(
        self,
        change_id: int,
        *,
        outcome: str,
        after_rms: Optional[float],
        after_n: Optional[int],
        delta: Optional[float],
        effect_sigma: Optional[float],
        confounded: bool,
    ) -> None:
        self.db.execute(
            "UPDATE param_change SET outcome = ?, after_rms_total = ?, after_n = ?,"
            " delta_rms_total = ?, effect_sigma = ?, confounded = ?, closed_at_utc = ?"
            " WHERE change_id = ?",
            (outcome, after_rms, after_n, delta, effect_sigma,
             int(confounded), utcnow(), change_id),
        )
        self.db.commit()

    def mark_reverted(self, change_id: int, reason: str) -> None:
        self.db.execute(
            "UPDATE param_change SET reverted = 1, revert_reason = ? WHERE change_id = ?",
            (reason, change_id),
        )
        self.db.commit()

    def changes_for(
        self,
        rig_id: int,
        *,
        bucket: Optional[str] = None,
        axis: Optional[str] = None,
        param: Optional[str] = None,
        limit: int = 200,
        closed_only: bool = True,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM param_change WHERE rig_id = ?"
        args: list[Any] = [rig_id]
        if bucket:
            sql += " AND bucket_key = ?"
            args.append(bucket)
        if axis:
            sql += " AND axis = ?"
            args.append(axis)
        if param:
            sql += " AND param = ?"
            args.append(param)
        if closed_only:
            sql += " AND outcome IS NOT NULL AND outcome != 'pending'"
        sql += " ORDER BY t_utc DESC LIMIT ?"
        args.append(limit)
        return list(self.db.execute(sql, args).fetchall())

    def recent_changes(self, session_id: int, limit: int = 50) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM param_change WHERE session_id = ? ORDER BY t_utc DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_changes_since(self, session_id: int, since_utc: str) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM param_change WHERE session_id = ? AND t_utc >= ?",
            (session_id, since_utc),
        ).fetchone()
        return int(row["n"]) if row else 0

    def count_session_changes(self, session_id: int) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM param_change WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row["n"]) if row else 0

    # ── advice audit ───────────────────────────────────────────────────

    def add_advice(
        self,
        session_id: Optional[int],
        *,
        model_str: Optional[str],
        verdict: str,
        raw_response: Optional[str] = None,
        parsed: Optional[dict] = None,
        latency_ms: Optional[int] = None,
        prompt_chars: Optional[int] = None,
    ) -> int:
        cur = self.db.execute(
            "INSERT INTO advice (session_id, t_utc, model_str, latency_ms,"
            " prompt_chars, raw_response, parsed_json, verdict)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (session_id, utcnow(), model_str, latency_ms, prompt_chars,
             raw_response, json.dumps(parsed) if parsed else None, verdict),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def recent_advice(self, limit: int = 20) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM advice ORDER BY t_utc DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── maintenance ────────────────────────────────────────────────────

    def prune(self, older_than_days: int = 730) -> int:
        """
        Drop ancient per-minute samples.

        Epochs, changes and advice are never pruned -- they *are* the
        accumulated knowledge, and they are small.
        """
        cutoff = datetime.fromtimestamp(
            time.time() - older_than_days * 86400, tz=timezone.utc
        ).isoformat(timespec="seconds")
        cur = self.db.execute(
            "DELETE FROM condition_sample WHERE t_utc < ?", (cutoff,)
        )
        self.db.commit()
        return cur.rowcount

    def stats(self) -> dict:
        def count(table: str) -> int:
            return int(self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

        return {
            "rigs": count("rig"),
            "sessions": count("session"),
            "epochs": count("setting_epoch"),
            "changes": count("param_change"),
            "frames": count("frame"),
            "condition_samples": count("condition_sample"),
        }
