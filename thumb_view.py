"""
thumb_view.py — Virtualized thumbnail grid for ImageCaptionApp.

Class ``ThumbnailView`` encapsulates everything related to the thumbnail panel:
layout, scrolling, virtualization, keyboard/mouse navigation, selection, and
integration with ``ImageDB`` / ``ThumbWorker``.

Key properties:
    * Only cells inside the visible window (+ one-row buffer above/below) are
      mounted as real Tk widgets. Scrolling / resizing mount/unmount on demand.
    * Thumbnail generation is requested only for visible+buffer paths that are
      still missing in the DB (strict-visible policy).
    * Canvas resize is debounced — reflow happens once after ~150 ms of idle.
    * When the current (selected) thumbnail would drop out of view after a
      resize, it is pulled back into view automatically unless the user has
      manually scrolled since the last programmatic reposition.
    * Deletion of an image keeps keyboard/mouse navigation consistent: click
      handlers resolve by ``rel_path`` at call time, indices are recomputed.
"""

import io
import os
import queue
from collections import OrderedDict
from tkinter import (Frame, Canvas, Label, Scrollbar, VERTICAL, LEFT, RIGHT,
                     BOTH, X, Y)
from PIL import Image, ImageTk

from db import ImageDB, ThumbWorker, THUMB_SIZE


# ---------------------------------------------------------------------------
# Visual / behaviour constants
# ---------------------------------------------------------------------------

THUMB_PAD    = 6
LABEL_HEIGHT = 18
CELL_W       = THUMB_SIZE + THUMB_PAD * 2
CELL_H       = CELL_W + LABEL_HEIGHT + 2

RESIZE_DEBOUNCE_MS = 150
SCROLL_SYNC_MS     = 20
BUFFER_ROWS        = 1

PHOTO_CACHE_MIN        = 128
PHOTO_CACHE_EXTRA_ROWS = 4

SEL_BG   = "#005f87"
BG       = "#2b2b2b"
PH_BG    = "#444"
DOT_HAS  = "#4caf50"
DOT_NONE = "#f44336"
LBL_FG   = "#cccccc"


def _short_name(name: str, cell_w: int, font_px: int = 7) -> str:
    max_chars = max(4, cell_w // font_px)
    return name if len(name) <= max_chars else name[:max_chars - 1] + "…"


def _dot_color(has_caption: int) -> str:
    return DOT_HAS if has_caption else DOT_NONE


# ---------------------------------------------------------------------------
# ThumbnailView
# ---------------------------------------------------------------------------

class ThumbnailView:
    """Virtualized grid of image thumbnails with keyboard/mouse navigation."""

    def __init__(
        self,
        parent,
        db: ImageDB,
        *,
        on_select=None,
        on_open=None,
        on_progress=None,
        cell_w: int = CELL_W,
        cell_h: int = CELL_H,
    ):
        self._db = db
        self._on_select = on_select
        self._on_open = on_open
        self._on_progress = on_progress
        self._cell_w = cell_w
        self._cell_h = cell_h

        # --- data state ---
        self._files: list[str] = []
        self._current_idx: int = 0
        self._cols: int = 1
        self._rows: int = 0

        # idx -> cell dict
        self._cells: dict[int, dict] = {}
        self._mounted: set[int] = set()

        # rel_path -> PhotoImage (LRU ordering)
        self._photos: "OrderedDict[str, ImageTk.PhotoImage]" = OrderedDict()

        # Manual-scroll flag: True once user scrolled the viewport since the
        # last programmatic reposition. set_current(ensure_visible=True) and
        # set_images() reset it.
        self._user_scrolled: bool = False
        self._resize_anchor: float | None = None

        # --- worker ---
        self._queue: queue.Queue = queue.Queue()
        self._worker = ThumbWorker(db, self._queue)
        self._poll_after: str | None = None
        self._total_requested: int = 0
        self._remaining: int = 0

        # --- debounce ids ---
        self._resize_after: str | None = None
        self._scroll_after: str | None = None

        # --- UI ---
        self.frame = Frame(parent)
        self._canvas = Canvas(self.frame, bg=BG, highlightthickness=0,
                              takefocus=1)
        self._canvas.pack(side=LEFT, fill=BOTH, expand=True)
        self._scrollbar = Scrollbar(self.frame, orient=VERTICAL, width=18,
                                    command=self._on_scrollbar_command)
        self._scrollbar.pack(side=RIGHT, fill=Y)
        self._canvas.config(yscrollcommand=self._on_yview_set)

        self._canvas.bind("<Configure>", self._on_canvas_configure)

        for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self._canvas.bind(ev, self._on_mousewheel)

        self._canvas.bind("<Left>",    self._key_left)
        self._canvas.bind("<Right>",   self._key_right)
        self._canvas.bind("<Up>",      self._key_up)
        self._canvas.bind("<Down>",    self._key_down)
        self._canvas.bind("<Home>",    self._key_home)
        self._canvas.bind("<End>",     self._key_end)
        self._canvas.bind("<Prior>",   self._key_pageup)
        self._canvas.bind("<Next>",    self._key_pagedown)
        self._canvas.bind("<Button-1>", lambda e: self._canvas.focus_set(), add="+")

        self._worker.start()
        self._schedule_poll()

    # ------------------------------------------------------------------
    # Panel show / hide / destroy
    # ------------------------------------------------------------------

    def grid(self, **opts):
        self.frame.grid(**opts)

    def grid_remove(self):
        self.frame.grid_remove()

    def focus(self):
        self._canvas.focus_set()

    def destroy(self):
        if self._poll_after is not None:
            try:
                self._canvas.after_cancel(self._poll_after)
            except Exception:
                pass
            self._poll_after = None
        if self._resize_after is not None:
            try:
                self._canvas.after_cancel(self._resize_after)
            except Exception:
                pass
        if self._scroll_after is not None:
            try:
                self._canvas.after_cancel(self._scroll_after)
            except Exception:
                pass
        self._worker.stop()
        try:
            self.frame.destroy()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Public data API
    # ------------------------------------------------------------------

    def set_images(self, rel_paths: list[str], current_index: int = 0):
        """Replace the file set and re-render the visible window."""
        self._unmount_range(set(self._mounted))
        self._photos.clear()
        self._files = list(rel_paths)
        if self._files:
            self._current_idx = max(0, min(current_index, len(self._files) - 1))
        else:
            self._current_idx = 0
        self._user_scrolled = False

        # Reset scroll position — new data set always starts with current in view.
        self._canvas.yview_moveto(0.0)
        self._recompute_layout()
        self._sync_visible()
        if self._files:
            self._scroll_cell_into_view(self._current_idx)
            self._sync_visible()

    def set_current(self, index: int, ensure_visible: bool = True):
        """Select *index* and optionally scroll it into view."""
        if not self._files:
            return
        index = max(0, min(index, len(self._files) - 1))
        old = self._current_idx
        self._current_idx = index

        for i in (old, index):
            cell = self._cells.get(i)
            if cell is not None:
                self._paint_selection(cell, selected=(i == index))

        if ensure_visible:
            self._user_scrolled = False
            self._scroll_cell_into_view(index)
            self._sync_visible()

    def refresh_caption_dot(self, rel_path: str):
        """Re-query DB for caption state and repaint the dot of *rel_path*."""
        try:
            idx = self._files.index(rel_path)
        except ValueError:
            return
        cell = self._cells.get(idx)
        if cell is None:
            return
        row = self._db.get_by_rel(rel_path)
        has = row["has_caption"] if row else 0
        try:
            cell["dot"].config(fg=_dot_color(has))
        except Exception:
            pass

    def remove(self, rel_path: str):
        """Remove *rel_path* from the set, shifting indices and selection.

        Intended to be called AFTER the caller has updated its own lists.
        """
        try:
            del_idx = self._files.index(rel_path)
        except ValueError:
            return

        self._worker.cancel(rel_path)
        self._photos.pop(rel_path, None)

        # Unmount every cell from del_idx onward — they shift by one index.
        doomed = {i for i in self._mounted if i >= del_idx}
        self._unmount_range(doomed)

        self._files.pop(del_idx)

        if not self._files:
            self._current_idx = 0
            self._recompute_layout()
            self._sync_visible()
            return

        if del_idx < self._current_idx:
            self._current_idx -= 1
        elif del_idx == self._current_idx:
            self._current_idx = min(del_idx, len(self._files) - 1)

        self._recompute_layout()
        self._sync_visible()
        if not self._user_scrolled:
            self._scroll_cell_into_view(self._current_idx)
            self._sync_visible()

    def rename(self, old_rp: str, new_rp: str):
        """Update a rel_path in-place; keep caches aligned."""
        try:
            idx = self._files.index(old_rp)
        except ValueError:
            return
        self._files[idx] = new_rp
        if old_rp in self._photos:
            self._photos[new_rp] = self._photos.pop(old_rp)
        cell = self._cells.get(idx)
        if cell is not None:
            cell["rel_path"] = new_rp
            try:
                name = _short_name(os.path.basename(new_rp), self._cell_w)
                cell["name_lbl"].config(text=name)
            except Exception:
                pass
            # Re-bind click closures to the new rel_path so subsequent clicks
            # resolve the new index.
            self._rebind_cell(cell, new_rp)
        self._worker.cancel(old_rp)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _recompute_layout(self):
        w = max(self._canvas.winfo_width(), 1)
        self._cols = max(1, w // self._cell_w)
        n = len(self._files)
        self._rows = (n + self._cols - 1) // self._cols if n else 0
        total_h = self._rows * self._cell_h
        total_w = max(self._cols * self._cell_w, w)
        self._canvas.configure(scrollregion=(0, 0, total_w, total_h))

        # Reposition already-mounted cells; drop any that fell off the set.
        stale = []
        for idx, cell in self._cells.items():
            if idx >= n:
                stale.append(idx)
                continue
            self._place_cell(idx, cell)
        if stale:
            self._unmount_range(set(stale))

    def _place_cell(self, idx: int, cell: dict):
        x, y = self._idx_to_xy(idx)
        try:
            self._canvas.coords(cell["window"], x, y)
        except Exception:
            pass

    def _idx_to_xy(self, idx: int) -> tuple[int, int]:
        row, col = divmod(idx, self._cols)
        return col * self._cell_w, row * self._cell_h

    # ------------------------------------------------------------------
    # Virtualization
    # ------------------------------------------------------------------

    def _compute_visible_idx_range(self) -> tuple[int, int]:
        if not self._files or self._rows == 0:
            return (0, -1)
        ch = max(self._canvas.winfo_height(), 1)
        total_h = self._rows * self._cell_h
        if total_h <= 0:
            return (0, -1)
        frac_top, frac_bot = self._canvas.yview()
        vis_top = frac_top * total_h
        vis_bot = frac_bot * total_h
        start_row = max(0, int(vis_top // self._cell_h) - BUFFER_ROWS)
        end_row = min(
            self._rows - 1,
            int(max(vis_bot - 1, 0) // self._cell_h) + BUFFER_ROWS,
        )
        if end_row < start_row:
            end_row = start_row
        start_idx = start_row * self._cols
        end_idx = min(len(self._files) - 1, (end_row + 1) * self._cols - 1)
        return (start_idx, end_idx)

    def _sync_visible(self):
        # Skip early when the canvas is unmapped / size unknown — the first
        # real <Configure> event will re-trigger sync.
        if self._canvas.winfo_height() <= 10 or self._rows == 0:
            self._unmount_range(set(self._mounted))
            self._request_thumbs([])
            return

        start, end = self._compute_visible_idx_range()
        if end < start:
            self._unmount_range(set(self._mounted))
            self._request_thumbs([])
            return

        needed = set(range(start, end + 1))
        to_unmount = self._mounted - needed
        to_mount = sorted(needed - self._mounted)

        if to_unmount:
            self._unmount_range(to_unmount)

        if to_mount:
            new_paths = [self._files[i] for i in to_mount]
            rows = self._db.get_visible_rows_bulk(new_paths)
            for i in to_mount:
                rp = self._files[i]
                thumb_bytes, has_cap = rows.get(rp, (None, 0))
                self._mount_cell(i, rp, thumb_bytes, has_cap)

        self._evict_photos()

        # Strict-visible policy: request thumbs for every visible cell that
        # still doesn't have one. Worker queue is replaced every sync.
        missing = [
            self._files[i]
            for i in range(start, end + 1)
            if self._cells.get(i) is not None
               and self._cells[i].get("need_thumb", False)
        ]
        self._request_thumbs(missing)

    def _request_thumbs(self, rel_paths: list[str]):
        self._worker.request(rel_paths)
        self._total_requested = len(rel_paths)
        self._remaining = len(rel_paths)
        self._emit_progress()

    def _emit_progress(self):
        if self._on_progress is None:
            return
        try:
            done = max(0, self._total_requested - self._remaining)
            self._on_progress(done, self._total_requested)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Cell mount / unmount
    # ------------------------------------------------------------------

    def _mount_cell(self, idx: int, rel_path: str,
                    thumb_bytes: bytes | None, has_caption: int):
        cell_frame = Frame(self._canvas, bg=BG,
                           width=self._cell_w, height=self._cell_h)
        cell_frame.pack_propagate(False)
        cell_frame.grid_propagate(False)

        ph = Canvas(cell_frame, bg=PH_BG,
                    width=THUMB_SIZE, height=THUMB_SIZE,
                    highlightthickness=0)
        ph.place(x=THUMB_PAD, y=THUMB_PAD)

        lf = Frame(cell_frame, bg=BG)
        lf.place(x=0, y=THUMB_SIZE + THUMB_PAD * 2,
                 width=self._cell_w, height=LABEL_HEIGHT + 2)

        dot = Label(lf, text="●", fg=_dot_color(has_caption), bg=BG, font=("", 7))
        dot.pack(side=LEFT, padx=(2, 0))

        name = _short_name(os.path.basename(rel_path), self._cell_w)
        name_lbl = Label(lf, text=name, fg=LBL_FG, bg=BG, font=("", 7), anchor="w")
        name_lbl.pack(side=LEFT, fill=X, expand=True)

        x, y = self._idx_to_xy(idx)
        window_id = self._canvas.create_window(
            x, y, window=cell_frame, anchor="nw",
            width=self._cell_w, height=self._cell_h,
        )

        cell = {
            "rel_path":    rel_path,
            "frame":       cell_frame,
            "label_frame": lf,
            "placeholder": ph,
            "img_lbl":     None,
            "dot":         dot,
            "name_lbl":    name_lbl,
            "window":      window_id,
            "need_thumb":  thumb_bytes is None,
        }
        self._cells[idx] = cell
        self._mounted.add(idx)

        self._rebind_cell(cell, rel_path)

        if thumb_bytes is not None:
            self._apply_thumb(idx, rel_path, thumb_bytes)

        self._paint_selection(cell, selected=(idx == self._current_idx))

    def _rebind_cell(self, cell: dict, rel_path: str):
        """(Re-)bind mouse events on every sub-widget of the cell.

        Binding uses *rel_path* in a closure so click handlers survive
        index shifts from delete/rename — the index is resolved at click time.
        """
        widgets = [cell["frame"], cell["label_frame"], cell["dot"],
                   cell["name_lbl"]]
        if cell.get("placeholder") is not None:
            widgets.append(cell["placeholder"])
        if cell.get("img_lbl") is not None:
            widgets.append(cell["img_lbl"])

        for w in widgets:
            w.bind("<Button-1>",
                   lambda e, rp=rel_path: self._on_click_rp(rp))
            for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                w.bind(ev, self._on_mousewheel)
            if self._on_open is not None:
                w.bind("<Double-Button-1>",
                       lambda e, rp=rel_path: self._on_open(rp))

    def _unmount_range(self, indices: set[int]):
        if not indices:
            return
        for idx in list(indices):
            cell = self._cells.pop(idx, None)
            if cell is None:
                self._mounted.discard(idx)
                continue
            try:
                cell["frame"].destroy()
            except Exception:
                pass
            try:
                self._canvas.delete(cell["window"])
            except Exception:
                pass
            self._mounted.discard(idx)

    def _apply_thumb(self, idx: int, rel_path: str, jpeg_bytes: bytes):
        cell = self._cells.get(idx)
        if cell is None or cell.get("rel_path") != rel_path:
            return

        photo = self._photos.get(rel_path)
        if photo is None:
            try:
                pil = Image.open(io.BytesIO(jpeg_bytes))
            except Exception:
                return
            photo = ImageTk.PhotoImage(pil)
            self._photos[rel_path] = photo
        else:
            self._photos.move_to_end(rel_path)

        ph = cell.get("placeholder")
        if ph is not None:
            try:
                ph.destroy()
            except Exception:
                pass
            cell["placeholder"] = None

        bg = SEL_BG if idx == self._current_idx else BG
        tw = photo.width()
        th = photo.height()

        existing = cell.get("img_lbl")
        if existing is not None:
            try:
                existing.destroy()
            except Exception:
                pass
            cell["img_lbl"] = None

        img_lbl = Label(cell["frame"], image=photo, bg=bg, cursor="hand2")
        img_lbl.image = photo
        img_lbl.place(
            x=THUMB_PAD + (THUMB_SIZE - tw) // 2,
            y=THUMB_PAD + (THUMB_SIZE - th) // 2,
        )
        cell["img_lbl"] = img_lbl
        cell["need_thumb"] = False

        img_lbl.bind("<Button-1>",
                     lambda e, rp=rel_path: self._on_click_rp(rp))
        for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            img_lbl.bind(ev, self._on_mousewheel)
        if self._on_open is not None:
            img_lbl.bind("<Double-Button-1>",
                         lambda e, rp=rel_path: self._on_open(rp))

    def _paint_selection(self, cell: dict, selected: bool):
        bg = SEL_BG if selected else BG
        try:
            cell["frame"].config(bg=bg)
            if cell.get("img_lbl") is not None:
                cell["img_lbl"].config(bg=bg)
        except Exception:
            pass

    def _evict_photos(self):
        cap = max(
            self._cols * (self._visible_rows() + PHOTO_CACHE_EXTRA_ROWS),
            PHOTO_CACHE_MIN,
        )
        while len(self._photos) > cap:
            self._photos.popitem(last=False)

    def _visible_rows(self) -> int:
        ch = max(self._canvas.winfo_height(), 1)
        return max(1, ch // self._cell_h + 1)

    # ------------------------------------------------------------------
    # Scrolling
    # ------------------------------------------------------------------

    def _scroll_cell_into_view(self, idx: int, prefer_anchor: float | None = None):
        """Scroll so that cell *idx* is visible (minimally, unless anchor set)."""
        if self._rows == 0 or not self._files:
            return
        total_h = self._rows * self._cell_h
        ch = max(self._canvas.winfo_height(), 1)
        if total_h <= ch:
            self._canvas.yview_moveto(0.0)
            return
        _, y = self._idx_to_xy(idx)
        cell_top = y
        cell_bot = y + self._cell_h
        frac_top, frac_bot = self._canvas.yview()
        vis_top = frac_top * total_h
        vis_bot = frac_bot * total_h

        if prefer_anchor is not None and 0.0 <= prefer_anchor <= 1.0:
            target = cell_top - prefer_anchor * ch
        elif cell_top < vis_top:
            target = cell_top
        elif cell_bot > vis_bot:
            target = cell_bot - ch
        else:
            return  # already fully visible

        target = max(0.0, min(target, total_h - ch))
        self._canvas.yview_moveto(target / total_h)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_canvas_configure(self, event):
        if self._resize_after is not None:
            try:
                self._canvas.after_cancel(self._resize_after)
            except Exception:
                pass
        # Capture the anchor BEFORE debounce so we keep a stable reference
        # even if several Configure events arrive during the debounce window.
        self._resize_anchor = self._compute_current_anchor()
        self._resize_after = self._canvas.after(
            RESIZE_DEBOUNCE_MS, self._apply_resize
        )

    def _compute_current_anchor(self) -> float | None:
        if not self._files or self._rows == 0:
            return None
        total_h = self._rows * self._cell_h
        ch = max(self._canvas.winfo_height(), 1)
        if total_h <= ch:
            return None
        _, y = self._idx_to_xy(self._current_idx)
        frac_top, _ = self._canvas.yview()
        vis_top = frac_top * total_h
        return max(0.0, min(1.0, (y - vis_top) / ch))

    def _apply_resize(self):
        self._resize_after = None
        self._recompute_layout()

        if self._files:
            if self._user_scrolled:
                # Only rescue the current cell if it fell out of view.
                self._scroll_cell_into_view(self._current_idx)
            else:
                self._scroll_cell_into_view(
                    self._current_idx,
                    prefer_anchor=self._resize_anchor,
                )
        self._sync_visible()

    def _on_mousewheel(self, event):
        if not self._rows:
            return "break"
        self._user_scrolled = True
        if event.num == 4 or getattr(event, "delta", 0) > 0:
            self._canvas.yview_scroll(-3, "units")
        else:
            self._canvas.yview_scroll(3, "units")
        self._schedule_scroll_sync()
        return "break"

    def _on_scrollbar_command(self, *args):
        self._user_scrolled = True
        self._canvas.yview(*args)
        self._schedule_scroll_sync()

    def _on_yview_set(self, *args):
        # Fired on ANY scroll change (user or programmatic).
        self._scrollbar.set(*args)
        self._schedule_scroll_sync()

    def _schedule_scroll_sync(self):
        if self._scroll_after is not None:
            return
        self._scroll_after = self._canvas.after(
            SCROLL_SYNC_MS, self._do_scheduled_scroll_sync
        )

    def _do_scheduled_scroll_sync(self):
        self._scroll_after = None
        self._sync_visible()

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _on_click_rp(self, rel_path: str):
        try:
            idx = self._files.index(rel_path)
        except ValueError:
            return
        self._canvas.focus_set()
        if idx != self._current_idx:
            if self._on_select is not None:
                self._on_select(idx)
            else:
                self.set_current(idx)
        else:
            self.set_current(idx, ensure_visible=False)

    def _move_to(self, new_idx: int):
        if not self._files:
            return
        new_idx = max(0, min(new_idx, len(self._files) - 1))
        if new_idx == self._current_idx:
            return
        if self._on_select is not None:
            self._on_select(new_idx)
        else:
            self.set_current(new_idx)

    def _key_left(self, event=None):
        self._move_to(self._current_idx - 1)
        return "break"

    def _key_right(self, event=None):
        self._move_to(self._current_idx + 1)
        return "break"

    def _key_up(self, event=None):
        self._move_to(self._current_idx - self._cols)
        return "break"

    def _key_down(self, event=None):
        self._move_to(self._current_idx + self._cols)
        return "break"

    def _key_home(self, event=None):
        self._move_to(0)
        return "break"

    def _key_end(self, event=None):
        self._move_to(len(self._files) - 1)
        return "break"

    def _key_pageup(self, event=None):
        page = max(1, self._visible_rows() - 1) * self._cols
        self._move_to(self._current_idx - page)
        return "break"

    def _key_pagedown(self, event=None):
        page = max(1, self._visible_rows() - 1) * self._cols
        self._move_to(self._current_idx + page)
        return "break"

    # ------------------------------------------------------------------
    # Worker polling
    # ------------------------------------------------------------------

    def _schedule_poll(self):
        self._poll_after = self._canvas.after(40, self._poll_worker)

    def _poll_worker(self):
        self._poll_after = None
        processed = 0
        while processed < 32:
            try:
                msg = self._queue.get_nowait()
            except queue.Empty:
                break
            kind = msg[0]
            if kind == "idle":
                self._remaining = 0
                self._emit_progress()
            elif kind == "thumb":
                _, rel_path, jpeg_bytes, _, remaining = msg
                if jpeg_bytes:
                    self._on_thumb_ready(rel_path, jpeg_bytes)
                self._remaining = remaining
                self._emit_progress()
            processed += 1
        self._schedule_poll()

    def _on_thumb_ready(self, rel_path: str, jpeg_bytes: bytes):
        try:
            idx = self._files.index(rel_path)
        except ValueError:
            return
        if idx not in self._mounted:
            return  # scrolled out while worker was busy
        self._apply_thumb(idx, rel_path, jpeg_bytes)
