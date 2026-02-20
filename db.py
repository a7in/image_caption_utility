"""
db.py — Database layer for ImageCaptionApp.

Schema (table: images):
    id           INTEGER PRIMARY KEY AUTOINCREMENT
    rel_path     TEXT UNIQUE NOT NULL   -- relative to image_directory (as shown in listbox)
    abs_path     TEXT NOT NULL
    mtime        REAL NOT NULL          -- os.path.getmtime at last sync
    has_caption  INTEGER NOT NULL DEFAULT 0
    caption_text TEXT NOT NULL DEFAULT ''
    thumb        BLOB                   -- JPEG bytes, NULL = not yet generated

All public methods are safe to call from the main thread.
Thumbnail generation runs in a background thread managed by ThumbWorker.
"""

import os
import io
import sqlite3
import threading
import queue
from PIL import Image

THUMB_SIZE = 128
DB_FILENAME = "thumbs.sqlite"


class ImageDB:
    """Manages the SQLite database for image metadata and thumbnails."""

    def __init__(self):
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self.directory: str = ""

    # ------------------------------------------------------------------
    # Open / close
    # ------------------------------------------------------------------

    def open(self, directory: str):
        """Open (or create) the database in *directory*. Sync filesystem state."""
        self.close()
        self.directory = directory
        db_path = os.path.join(directory, DB_FILENAME)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create_schema()

    def close(self):
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        self.directory = ""

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _create_schema(self):
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS images (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    rel_path     TEXT UNIQUE NOT NULL,
                    abs_path     TEXT NOT NULL,
                    mtime        REAL NOT NULL,
                    has_caption  INTEGER NOT NULL DEFAULT 0,
                    caption_text TEXT NOT NULL DEFAULT '',
                    thumb        BLOB
                );
                CREATE INDEX IF NOT EXISTS idx_rel_path ON images (rel_path);
                CREATE INDEX IF NOT EXISTS idx_has_caption ON images (has_caption);
            """)
            self._conn.commit()

    # ------------------------------------------------------------------
    # Filesystem sync
    # ------------------------------------------------------------------

    def sync(self, abs_paths: list[str]) -> list[str]:
        """
        Synchronise DB with the current list of image files on disk.

        - Rows whose rel_path is no longer on disk are deleted.
        - New files get an INSERT (thumb=NULL).
        - Existing files whose mtime changed get mtime reset and thumb=NULL
          (thumbnail will be regenerated).
        - caption_text / has_caption are refreshed for new rows.

        Returns the ordered list of rel_paths after sync (same order as
        abs_paths that still exist).
        """
        rel_to_abs = {}
        for ap in abs_paths:
            rp = self._rel(ap)
            rel_to_abs[rp] = ap

        disk_set = set(rel_to_abs)

        with self._lock:
            cur = self._conn.execute("SELECT rel_path, mtime FROM images")
            db_rows = {row["rel_path"]: row["mtime"] for row in cur}
            db_set = set(db_rows)

            # --- delete stale rows ---
            stale = db_set - disk_set
            if stale:
                self._conn.executemany(
                    "DELETE FROM images WHERE rel_path = ?",
                    [(rp,) for rp in stale]
                )

            # --- insert new rows ---
            new_paths = disk_set - db_set
            rows_to_insert = []
            for rp in new_paths:
                ap = rel_to_abs[rp]
                try:
                    mtime = os.path.getmtime(ap)
                except OSError:
                    mtime = 0.0
                cap_text, has_cap = self._read_caption(ap)
                rows_to_insert.append((rp, ap, mtime, has_cap, cap_text))

            if rows_to_insert:
                self._conn.executemany(
                    """INSERT OR IGNORE INTO images
                       (rel_path, abs_path, mtime, has_caption, caption_text, thumb)
                       VALUES (?, ?, ?, ?, ?, NULL)""",
                    rows_to_insert
                )

            # --- invalidate changed mtimes ---
            for rp, ap in rel_to_abs.items():
                if rp in db_set:
                    try:
                        mtime = os.path.getmtime(ap)
                    except OSError:
                        mtime = 0.0
                    if abs(mtime - db_rows[rp]) > 0.5:
                        self._conn.execute(
                            "UPDATE images SET mtime=?, thumb=NULL WHERE rel_path=?",
                            (mtime, rp)
                        )

            self._conn.commit()

        # Return rel_paths in original sort order
        return [self._rel(ap) for ap in abs_paths if self._rel(ap) in disk_set]

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_all(self, filter_text: str = "") -> list[sqlite3.Row]:
        """Return all rows matching *filter_text* in caption_text or rel_path."""
        with self._lock:
            if filter_text:
                pattern = f"%{filter_text}%"
                cur = self._conn.execute(
                    """SELECT id, rel_path, abs_path, has_caption, caption_text
                       FROM images
                       WHERE caption_text LIKE ? OR rel_path LIKE ?
                       ORDER BY rel_path""",
                    (pattern, pattern)
                )
            else:
                cur = self._conn.execute(
                    """SELECT id, rel_path, abs_path, has_caption, caption_text
                       FROM images ORDER BY rel_path"""
                )
            return cur.fetchall()

    def get_by_rel(self, rel_path: str) -> sqlite3.Row | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM images WHERE rel_path = ?", (rel_path,)
            )
            return cur.fetchone()

    def get_thumb(self, rel_path: str) -> bytes | None:
        """Return raw JPEG thumb bytes or None."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT thumb FROM images WHERE rel_path = ?", (rel_path,)
            )
            row = cur.fetchone()
            return row["thumb"] if row else None

    def get_pending_thumbs(self) -> list[tuple[str, str]]:
        """Return (rel_path, abs_path) for rows where thumb IS NULL."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT rel_path, abs_path FROM images WHERE thumb IS NULL ORDER BY rel_path"
            )
            return [(r["rel_path"], r["abs_path"]) for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Update caption
    # ------------------------------------------------------------------

    def update_caption(self, rel_path: str, caption_text: str):
        has = 1 if caption_text.strip() else 0
        with self._lock:
            self._conn.execute(
                """UPDATE images
                   SET caption_text=?, has_caption=?
                   WHERE rel_path=?""",
                (caption_text, has, rel_path)
            )
            self._conn.commit()

    def update_all_captions(self, rel_paths: list[str]):
        """Re-read caption files from disk for a list of rel_paths."""
        rows = []
        for rp in rel_paths:
            ap = self._abs(rp)
            cap_text, has_cap = self._read_caption(ap)
            rows.append((cap_text, has_cap, rp))
        with self._lock:
            self._conn.executemany(
                "UPDATE images SET caption_text=?, has_caption=? WHERE rel_path=?",
                rows
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Update thumb
    # ------------------------------------------------------------------

    def set_thumb(self, rel_path: str, jpeg_bytes: bytes):
        with self._lock:
            self._conn.execute(
                "UPDATE images SET thumb=? WHERE rel_path=?",
                (jpeg_bytes, rel_path)
            )
            self._conn.commit()

    def invalidate_thumb(self, rel_path: str):
        """Force thumb regeneration on next thumb-mode activation."""
        with self._lock:
            self._conn.execute(
                "UPDATE images SET thumb=NULL WHERE rel_path=?", (rel_path,)
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Rename / move
    # ------------------------------------------------------------------

    def rename(self, old_rel: str, new_rel: str, new_abs: str):
        """
        Update rel_path and abs_path keeping all other fields (including thumb).
        """
        with self._lock:
            self._conn.execute(
                "UPDATE images SET rel_path=?, abs_path=? WHERE rel_path=?",
                (new_rel, new_abs, old_rel)
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete(self, rel_path: str):
        with self._lock:
            self._conn.execute(
                "DELETE FROM images WHERE rel_path=?", (rel_path,)
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _rel(self, abs_path: str) -> str:
        r = os.path.relpath(abs_path, self.directory)
        return r.replace("\\", "/")   # normalise to forward slashes in DB

    def _abs(self, rel_path: str) -> str:
        return os.path.join(self.directory, rel_path.replace("/", os.sep))

    @staticmethod
    def _read_caption(abs_image_path: str) -> tuple[str, int]:
        txt_path = os.path.splitext(abs_image_path)[0] + ".txt"
        if os.path.exists(txt_path):
            try:
                with open(txt_path, "r", encoding="utf-8") as f:
                    text = f.read()
                return text, (1 if text.strip() else 0)
            except OSError:
                pass
        return "", 0


# ---------------------------------------------------------------------------
# Background thumbnail generator
# ---------------------------------------------------------------------------

class ThumbWorker:
    """
    Generates thumbnails in a background thread and communicates results
    via a queue.  UI polls with root.after().

    Usage:
        worker = ThumbWorker(db, on_done_callback)
        worker.start(pending_list)   # list of (rel_path, abs_path)
        worker.stop()
    """

    def __init__(self, db: ImageDB, result_queue: queue.Queue):
        self._db = db
        self._queue = result_queue
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self, pending: list[tuple[str, str]]):
        self.stop()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, args=(pending,), daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        # drain queue so poll doesn't keep firing
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def _run(self, pending: list[tuple[str, str]]):
        total = len(pending)
        for i, (rel_path, abs_path) in enumerate(pending):
            if self._stop_event.is_set():
                self._queue.put(("abort", None, None, 0, 0))
                return
            jpeg_bytes = self._generate(abs_path)
            if jpeg_bytes:
                self._db.set_thumb(rel_path, jpeg_bytes)
            self._queue.put(("thumb", rel_path, jpeg_bytes, i + 1, total))
        self._queue.put(("done", None, None, total, total))

    @staticmethod
    def _generate(abs_path: str) -> bytes | None:
        try:
            img = Image.open(abs_path)
            img.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "JPEG", quality=80)
            return buf.getvalue()
        except Exception:
            return None
