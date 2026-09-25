"""Durable, transactional SQLite state.

One ``control`` row holds the purchase state machine. All state changes are
compare-and-set inside ``BEGIN IMMEDIATE`` transactions so competing events, processes,
or restarts cannot both "win". Events are deduplicated by event id *and* by
(channel id, message id) so bounded retries and re-detections collapse to one attempt.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .models import BUSY_STATES, STICKY_STATES, PurchaseState, TriggerEvent

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS control (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT NOT NULL,
    armed_until_ms INTEGER,
    purchase_disabled INTEGER NOT NULL DEFAULT 0,
    disabled_reason TEXT,
    active_event_id TEXT,
    note TEXT,
    updated_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    guild_id TEXT NOT NULL,
    matched_target TEXT NOT NULL,
    message_ts_ms INTEGER NOT NULL,
    sent_at_ms INTEGER NOT NULL,
    received_at_ms INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    source TEXT NOT NULL,
    disposition TEXT NOT NULL,
    UNIQUE (channel_id, message_id)
);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    started_at_ms INTEGER NOT NULL,
    intent_persisted_at_ms INTEGER,
    submitted_at_ms INTEGER,
    finished_at_ms INTEGER,
    final_state TEXT,
    reason TEXT,
    order_id TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at_ms INTEGER NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    event_id TEXT,
    reason TEXT
);
"""


class StateError(RuntimeError):
    pass


@dataclass(frozen=True)
class ControlRow:
    state: PurchaseState
    armed_until_ms: int | None
    purchase_disabled: bool
    disabled_reason: str | None
    active_event_id: str | None
    note: str | None
    updated_at_ms: int

    def is_armed(self, now_ms: int) -> bool:
        return (
            self.state == PurchaseState.ARMED
            and not self.purchase_disabled
            and self.armed_until_ms is not None
            and self.armed_until_ms > now_ms
        )


@dataclass(frozen=True)
class AttemptRow:
    id: int
    event_id: str
    started_at_ms: int
    intent_persisted_at_ms: int | None
    submitted_at_ms: int | None
    finished_at_ms: int | None
    final_state: str | None
    reason: str | None
    order_id: str | None
    dry_run: bool


def now_ms() -> int:
    return int(time.time() * 1000)


class StateStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.path), isolation_level=None, check_same_thread=False, timeout=5.0
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()

    # ------------------------------------------------------------------ setup
    def _init_schema(self) -> None:
        # DDL is idempotent; executescript() would implicitly COMMIT, so keep it outside _tx().
        self._conn.executescript(_SCHEMA)
        with self._tx():
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            row = self._conn.execute("SELECT id FROM control WHERE id = 1").fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO control(id, state, updated_at_ms) VALUES (1, ?, ?)",
                    (PurchaseState.DISARMED.value, now_ms()),
                )

    def close(self) -> None:
        self._conn.close()

    class _Tx:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self.conn = conn

        def __enter__(self):
            self.conn.execute("BEGIN IMMEDIATE")
            return self.conn

        def __exit__(self, exc_type, exc, tb):
            if exc_type is None:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute("ROLLBACK")
            return False

    def _tx(self) -> _Tx:
        return StateStore._Tx(self._conn)

    # ---------------------------------------------------------------- control
    def get_control(self) -> ControlRow:
        row = self._conn.execute(
            "SELECT state, armed_until_ms, purchase_disabled, disabled_reason, active_event_id,"
            " note, updated_at_ms FROM control WHERE id = 1"
        ).fetchone()
        return ControlRow(
            state=PurchaseState(row[0]),
            armed_until_ms=row[1],
            purchase_disabled=bool(row[2]),
            disabled_reason=row[3],
            active_event_id=row[4],
            note=row[5],
            updated_at_ms=row[6],
        )

    def _set_control(
        self,
        conn: sqlite3.Connection,
        *,
        state: PurchaseState,
        from_state: PurchaseState | None,
        at_ms: int,
        event_id: str | None,
        reason: str,
        armed_until_ms: int | None | object = ...,
        purchase_disabled: bool | None = None,
        disabled_reason: str | None | object = ...,
        active_event_id: str | None | object = ...,
        note: str | None | object = ...,
    ) -> None:
        sets = ["state = ?", "updated_at_ms = ?"]
        params: list[object] = [state.value, at_ms]
        if armed_until_ms is not ...:
            sets.append("armed_until_ms = ?")
            params.append(armed_until_ms)
        if purchase_disabled is not None:
            sets.append("purchase_disabled = ?")
            params.append(1 if purchase_disabled else 0)
        if disabled_reason is not ...:
            sets.append("disabled_reason = ?")
            params.append(disabled_reason)
        if active_event_id is not ...:
            sets.append("active_event_id = ?")
            params.append(active_event_id)
        if note is not ...:
            sets.append("note = ?")
            params.append(note)
        conn.execute(f"UPDATE control SET {', '.join(sets)} WHERE id = 1", params)
        conn.execute(
            "INSERT INTO transitions(at_ms, from_state, to_state, event_id, reason) VALUES (?, ?, ?, ?, ?)",
            (at_ms, from_state.value if from_state else None, state.value, event_id, reason),
        )

    def arm(self, until_ms: int, at_ms: int | None = None) -> ControlRow:
        at_ms = at_ms or now_ms()
        if until_ms <= at_ms:
            raise StateError("armed_until must be in the future")
        with self._tx() as conn:
            cur = self.get_control()
            if cur.purchase_disabled:
                raise StateError(f"purchases are disabled ({cur.disabled_reason}); run `reset` first")
            if cur.state in BUSY_STATES:
                raise StateError(f"cannot arm while {cur.state.value}")
            if cur.state in STICKY_STATES:
                raise StateError(f"state {cur.state.value} requires explicit `reset` before arming")
            self._set_control(
                conn,
                state=PurchaseState.ARMED,
                from_state=cur.state,
                at_ms=at_ms,
                event_id=None,
                reason="armed by operator",
                armed_until_ms=until_ms,
                active_event_id=None,
                note=None,
            )
        return self.get_control()

    def disarm(self, reason: str = "disarmed by operator", at_ms: int | None = None) -> ControlRow:
        at_ms = at_ms or now_ms()
        with self._tx() as conn:
            cur = self.get_control()
            if cur.state in STICKY_STATES:
                # Sticky states already block purchases; keep them visible for the operator.
                self._set_control(
                    conn,
                    state=cur.state,
                    from_state=cur.state,
                    at_ms=at_ms,
                    event_id=None,
                    reason=f"{reason} (state retained)",
                    armed_until_ms=None,
                )
            else:
                self._set_control(
                    conn,
                    state=PurchaseState.DISARMED,
                    from_state=cur.state,
                    at_ms=at_ms,
                    event_id=None,
                    reason=reason,
                    armed_until_ms=None,
                )
        return self.get_control()

    def reset(self, reason: str = "explicit reset", at_ms: int | None = None) -> ControlRow:
        """Clear PURCHASED/UNKNOWN/NEEDS_ATTENTION and re-enable purchasing (DISARMED)."""
        at_ms = at_ms or now_ms()
        with self._tx() as conn:
            cur = self.get_control()
            if cur.state in BUSY_STATES:
                raise StateError(f"cannot reset while {cur.state.value}; stop the runner first")
            self._set_control(
                conn,
                state=PurchaseState.DISARMED,
                from_state=cur.state,
                at_ms=at_ms,
                event_id=None,
                reason=reason,
                armed_until_ms=None,
                purchase_disabled=False,
                disabled_reason=None,
                active_event_id=None,
                note=None,
            )
        return self.get_control()

    def recover_on_startup(self, at_ms: int | None = None) -> ControlRow:
        """Apply restart rules: never stay armed; crash after intent => UNKNOWN."""
        at_ms = at_ms or now_ms()
        with self._tx() as conn:
            cur = self.get_control()
            if cur.state == PurchaseState.ARMED:
                self._set_control(
                    conn,
                    state=PurchaseState.DISARMED,
                    from_state=cur.state,
                    at_ms=at_ms,
                    event_id=None,
                    reason="restart: explicit re-arm required",
                    armed_until_ms=None,
                )
            elif cur.state == PurchaseState.SUBMITTING:
                self._set_control(
                    conn,
                    state=PurchaseState.UNKNOWN,
                    from_state=cur.state,
                    at_ms=at_ms,
                    event_id=cur.active_event_id,
                    reason="restart during SUBMITTING: submission outcome unknown",
                    armed_until_ms=None,
                    purchase_disabled=True,
                    disabled_reason="ambiguous submission; reconcile order history then `reset`",
                )
                conn.execute(
                    "UPDATE attempts SET finished_at_ms = ?, final_state = ?, reason = ?"
                    " WHERE event_id = ? AND finished_at_ms IS NULL",
                    (
                        at_ms,
                        PurchaseState.UNKNOWN.value,
                        "process restarted after intent",
                        cur.active_event_id,
                    ),
                )
            elif cur.state in {PurchaseState.VERIFYING, PurchaseState.CHECKOUT_READY}:
                self._set_control(
                    conn,
                    state=PurchaseState.DISARMED,
                    from_state=cur.state,
                    at_ms=at_ms,
                    event_id=cur.active_event_id,
                    reason="restart before submission intent: attempt abandoned",
                    armed_until_ms=None,
                    active_event_id=None,
                )
                conn.execute(
                    "UPDATE attempts SET finished_at_ms = ?, final_state = ?, reason = ?"
                    " WHERE event_id = ? AND finished_at_ms IS NULL",
                    (
                        at_ms,
                        PurchaseState.DISARMED.value,
                        "process restarted before intent",
                        cur.active_event_id,
                    ),
                )
        return self.get_control()

    # ----------------------------------------------------------------- events
    def record_event(self, event: TriggerEvent, received_at_ms: int | None = None) -> str:
        """Insert an event; returns 'new', 'duplicate_event', or 'duplicate_message'."""
        received_at_ms = received_at_ms or now_ms()
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT event_id FROM events WHERE event_id = ?", (event.event_id,)
            ).fetchone()
            if existing:
                return "duplicate_event"
            existing_msg = conn.execute(
                "SELECT event_id FROM events WHERE channel_id = ? AND message_id = ?",
                (event.channel_id, event.message_id),
            ).fetchone()
            if existing_msg:
                return "duplicate_message"
            conn.execute(
                "INSERT INTO events(event_id, message_id, channel_id, guild_id, matched_target,"
                " message_ts_ms, sent_at_ms, received_at_ms, attempt, source, disposition)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'accepted')",
                (
                    event.event_id,
                    event.message_id,
                    event.channel_id,
                    event.guild_id,
                    event.matched_target,
                    event.message_ts_ms,
                    event.sent_at_ms,
                    received_at_ms,
                    event.attempt,
                    event.source,
                ),
            )
            return "new"

    def set_event_disposition(self, event_id: str, disposition: str) -> None:
        with self._tx() as conn:
            conn.execute("UPDATE events SET disposition = ? WHERE event_id = ?", (disposition, event_id))

    def recent_events(self, limit: int = 10) -> list[dict]:
        rows = self._conn.execute(
            "SELECT event_id, message_id, channel_id, matched_target, message_ts_ms,"
            " received_at_ms, disposition FROM events ORDER BY received_at_ms DESC LIMIT ?",
            (limit,),
        ).fetchall()
        keys = [
            "event_id",
            "message_id",
            "channel_id",
            "matched_target",
            "message_ts_ms",
            "received_at_ms",
            "disposition",
        ]
        return [dict(zip(keys, r, strict=True)) for r in rows]

    # --------------------------------------------------------------- attempts
    def begin_attempt(self, event_id: str, at_ms: int | None = None) -> bool:
        """ARMED (unexpired, enabled) -> VERIFYING for this event. False if not permitted."""
        at_ms = at_ms or now_ms()
        with self._tx() as conn:
            cur = self.get_control()
            if not cur.is_armed(at_ms):
                return False
            self._set_control(
                conn,
                state=PurchaseState.VERIFYING,
                from_state=cur.state,
                at_ms=at_ms,
                event_id=event_id,
                reason="trigger accepted",
                active_event_id=event_id,
            )
            conn.execute("INSERT INTO attempts(event_id, started_at_ms) VALUES (?, ?)", (event_id, at_ms))
            return True

    def transition(
        self,
        event_id: str,
        from_states: Iterable[PurchaseState],
        to_state: PurchaseState,
        reason: str,
        at_ms: int | None = None,
    ) -> bool:
        """Compare-and-set for the active event. False when preconditions fail."""
        at_ms = at_ms or now_ms()
        allowed = set(from_states)
        with self._tx() as conn:
            cur = self.get_control()
            if cur.active_event_id != event_id or cur.state not in allowed:
                return False
            self._set_control(
                conn,
                state=to_state,
                from_state=cur.state,
                at_ms=at_ms,
                event_id=event_id,
                reason=reason,
            )
            return True

    def persist_submission_intent(self, event_id: str, at_ms: int | None = None) -> bool:
        """CHECKOUT_READY -> SUBMITTING, recording intent *before* the final click."""
        at_ms = at_ms or now_ms()
        with self._tx() as conn:
            cur = self.get_control()
            if (
                cur.active_event_id != event_id
                or cur.state != PurchaseState.CHECKOUT_READY
                or cur.purchase_disabled
                or cur.armed_until_ms is None
                or cur.armed_until_ms <= at_ms
            ):
                return False
            self._set_control(
                conn,
                state=PurchaseState.SUBMITTING,
                from_state=cur.state,
                at_ms=at_ms,
                event_id=event_id,
                reason="submission intent persisted",
            )
            conn.execute(
                "UPDATE attempts SET intent_persisted_at_ms = ? WHERE event_id = ?"
                " AND finished_at_ms IS NULL",
                (at_ms, event_id),
            )
            return True

    def mark_submitted(self, event_id: str, at_ms: int | None = None) -> None:
        at_ms = at_ms or now_ms()
        with self._tx() as conn:
            conn.execute(
                "UPDATE attempts SET submitted_at_ms = ? WHERE event_id = ? AND finished_at_ms IS NULL",
                (at_ms, event_id),
            )

    def finish_attempt(
        self,
        event_id: str,
        final_state: PurchaseState,
        reason: str,
        *,
        order_id: str | None = None,
        dry_run: bool = False,
        at_ms: int | None = None,
    ) -> ControlRow:
        """Close the active attempt and move control to ``final_state``.

        PURCHASED and UNKNOWN durably disable further purchases until explicit reset.
        ARMED is used when a pre-intent failure should leave the session armed.
        """
        at_ms = at_ms or now_ms()
        with self._tx() as conn:
            cur = self.get_control()
            if cur.active_event_id != event_id:
                raise StateError("finish_attempt for an event that is not active")
            if final_state == PurchaseState.ARMED and (
                cur.state == PurchaseState.DISARMED
                or cur.armed_until_ms is None
                or cur.armed_until_ms <= at_ms
                or cur.purchase_disabled
            ):
                # Operator disarmed (or the session expired) mid-attempt: do not re-arm.
                final_state = PurchaseState.DISARMED
            disable = final_state in {PurchaseState.PURCHASED, PurchaseState.UNKNOWN}
            self._set_control(
                conn,
                state=final_state,
                from_state=cur.state,
                at_ms=at_ms,
                event_id=event_id,
                reason=reason,
                purchase_disabled=True if disable else None,
                disabled_reason=(reason if disable else ...),
                active_event_id=None if final_state not in BUSY_STATES else event_id,
                armed_until_ms=(cur.armed_until_ms if final_state == PurchaseState.ARMED else None),
            )
            conn.execute(
                "UPDATE attempts SET finished_at_ms = ?, final_state = ?, reason = ?, order_id = ?,"
                " dry_run = ? WHERE event_id = ? AND finished_at_ms IS NULL",
                (at_ms, final_state.value, reason, order_id, 1 if dry_run else 0, event_id),
            )
        return self.get_control()

    def attempts_for(self, event_id: str) -> list[AttemptRow]:
        rows = self._conn.execute(
            "SELECT id, event_id, started_at_ms, intent_persisted_at_ms, submitted_at_ms,"
            " finished_at_ms, final_state, reason, order_id, dry_run FROM attempts"
            " WHERE event_id = ? ORDER BY id",
            (event_id,),
        ).fetchall()
        return [AttemptRow(*r[:9], dry_run=bool(r[9])) for r in rows]

    def last_attempt(self) -> AttemptRow | None:
        r = self._conn.execute(
            "SELECT id, event_id, started_at_ms, intent_persisted_at_ms, submitted_at_ms,"
            " finished_at_ms, final_state, reason, order_id, dry_run FROM attempts"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return AttemptRow(*r[:9], dry_run=bool(r[9])) if r else None

    def transitions(self, limit: int = 20) -> list[tuple]:
        return self._conn.execute(
            "SELECT at_ms, from_state, to_state, event_id, reason FROM transitions ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
