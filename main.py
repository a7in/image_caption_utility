import os
import queue
import io
from tkinter import *
from tkinter import ttk
from tkinter import filedialog, messagebox
from idlelib.tooltip import Hovertip
from PIL import Image, ImageTk
import subprocess
import platform
from googletrans import Translator

from db import ImageDB, ThumbWorker, THUMB_SIZE

translator = Translator()

THUMB_PAD    = 6
LABEL_HEIGHT = 18   # px for filename label row under each thumb cell
CELL_SIZE    = THUMB_SIZE + THUMB_PAD * 2


class ImageCaptionApp:
    def __init__(self, root):
        self.root = root
        self.root.title("a7in image Caption Utility")

        # ---- DB / state ----
        self.db = ImageDB()
        self._thumb_queue: queue.Queue = queue.Queue()
        self._thumb_worker: ThumbWorker | None = None
        self._thumb_built  = False   # True after first thumb-mode activation
        self._thumb_cols   = 0

        # In-memory image list (list of abs paths, ordered by rel_path)
        self.image_files:     list[str] = []   # current (possibly filtered)
        self.all_image_files: list[str] = []   # full unfiltered list
        self.image_directory: str = ""
        self.image_index:     int = 0

        self.current_image:        str | None = None
        self.current_caption_file: str | None = None
        self.original_image:       Image.Image | None = None
        self.photo:                ImageTk.PhotoImage | None = None

        # PhotoImage refs keyed by rel_path (prevent GC)
        self._thumb_photos: dict[str, ImageTk.PhotoImage] = {}
        # cell widget info keyed by abs_path
        self._thumb_widgets: dict[str, dict] = {}

        self.view_mode = "list"

        # ---- UI build ----
        self._build_ui()

        self.load_images()
        self.display_image()
        root.state("zoomed")
        root.after(200, root.focus_force)

    # ==================================================================
    # UI construction
    # ==================================================================

    def _build_ui(self):
        root = self.root
        container = Frame(root)
        container.pack(fill=BOTH, expand=True)
        container.grid_rowconfigure(1, weight=1)
        container.grid_columnconfigure(0, weight=1)

        # ---- top button bar ----
        b_frame = Frame(container)
        b_frame.grid(row=0, column=0, sticky="ew")

        for text, cmd, tip in [
            ("Reopen folder",  self.open_folder,          None),
            ("Search empty",   self.search_empty_caption, None),
            ("Find-Replace",   self.open_find_replace,    None),
            ("Save",           self.save_caption,         "Save current edited prompt"),
            ("Cancel",         self.load_caption,         "Return to original prompt"),
            ("Delete",         self.delete_current_image, "Delete current image and caption file"),
        ]:
            btn = Button(b_frame, text=text, command=cmd)
            btn.pack(side=LEFT, padx=2, pady=2)
            if tip:
                Hovertip(btn, text=tip)
            if text == "Save":
                self.save_button = btn
            elif text == "Cancel":
                self.cancel_button = btn
            elif text == "Delete":
                self.delete_button = btn

        # ---- main split frame ----
        main_frame = Frame(container)
        main_frame.grid(row=1, column=0, sticky="nsew")
        main_frame.grid_rowconfigure(0, weight=1)
        main_frame.grid_columnconfigure(0, weight=1, uniform="half")
        main_frame.grid_columnconfigure(1, weight=1, uniform="half")

        # ---- left: image + text ----
        text_frame_height = root.winfo_screenheight() / 4

        img_frame = Frame(main_frame)
        img_frame.grid(row=0, column=0, sticky="nsew")
        img_frame.grid_rowconfigure(0, weight=0)
        img_frame.grid_rowconfigure(1, weight=7, minsize=200)
        img_frame.grid_rowconfigure(2, weight=0, minsize=text_frame_height)
        img_frame.grid_columnconfigure(0, weight=1)

        img_ctrl_frame = Frame(img_frame)
        img_ctrl_frame.grid(row=0, column=0, sticky="ew", padx=2, pady=2)

        self.prev_button  = Button(img_ctrl_frame, text="Prev", command=lambda: self.select_image(-1))
        self.next_button  = Button(img_ctrl_frame, text="Next", command=lambda: self.select_image(1))
        self.index_label  = Label(img_ctrl_frame, text="", fg="blue")
        self.dir_entry    = ttk.Combobox(img_ctrl_frame, state="readonly", width=32)
        self.file_entry   = Entry(img_ctrl_frame)

        self.prev_button.pack(side=LEFT, padx=2, pady=2)
        self.next_button.pack(side=LEFT, padx=2, pady=2)
        self.index_label.pack(side=LEFT, padx=2, pady=2)
        self.dir_entry.pack(side=LEFT, padx=2, pady=2)
        self.dir_entry.bind("<<ComboboxSelected>>", self.on_dir_change)
        self.file_entry.pack(side=LEFT, fill=X, expand=True, padx=2, pady=2)
        self.file_entry.bind("<Return>", self.rename_file)

        self.image_label = Label(img_frame)
        self.image_label.grid(row=1, column=0, sticky="nsew", padx=2, pady=2)
        self.image_label.bind("<Configure>", self.resize_image)
        self.image_label.bind("<Double-Button-1>", self.open_image)

        text_frame = Frame(img_frame, height=text_frame_height)
        text_frame.grid(row=2, column=0, sticky="nsew")
        text_frame.grid_rowconfigure(0, weight=1)
        text_frame.grid_propagate(False)
        text_frame.grid_columnconfigure(0, weight=1, uniform="txt")
        text_frame.grid_columnconfigure(1, weight=1, uniform="txt")

        self.text_area = Text(text_frame, wrap=WORD, width=1)
        self.text_area.grid(row=0, column=0, sticky="nsew", padx=2, pady=2)
        for ev in ("<Button-1>", "<ButtonRelease-1>", "<FocusIn>", "<KeyPress>", "<KeyRelease>"):
            self.text_area.bind(ev, lambda e: self.root.after_idle(self.restore_listbox_selection))

        trans_frame = Frame(text_frame)
        trans_frame.grid(row=0, column=1, sticky="nsew", padx=2, pady=2)
        trans_frame.grid_rowconfigure(2, weight=1)
        trans_frame.grid_columnconfigure(0, weight=1)

        self.trans_button = Button(trans_frame, text="Translate and add -^ from:", command=self.translate_text)
        self.trans_button.grid(row=0, column=0, sticky="w", padx=2, pady=2)

        self.text_lang = Text(trans_frame, width=2, height=1)
        self.text_lang.grid(row=1, column=0, sticky="w", padx=2, pady=2)
        self.text_lang.insert(END, "ru")

        self.trans_text_area = Text(trans_frame, wrap=WORD, width=1)
        self.trans_text_area.grid(row=2, column=0, sticky="nsew", padx=2, pady=2)
        for ev in ("<Button-1>", "<ButtonRelease-1>", "<FocusIn>", "<KeyPress>"):
            self.trans_text_area.bind(ev, lambda e: self.root.after_idle(self.restore_listbox_selection))
        for ev in ("<Button-1>", "<FocusIn>"):
            self.text_lang.bind(ev, lambda e: self.root.after_idle(self.restore_listbox_selection))

        # ---- right: nav panel ----
        nav_frame = Frame(main_frame)
        nav_frame.grid(row=0, column=1, sticky="nsew")
        nav_frame.grid_rowconfigure(0, weight=0)   # filter
        nav_frame.grid_rowconfigure(1, weight=0)   # mode buttons
        nav_frame.grid_rowconfigure(2, weight=1)   # list / thumbs
        nav_frame.grid_columnconfigure(0, weight=1)
        nav_frame.grid_columnconfigure(1, weight=0)

        # filter bar
        filter_frame = Frame(nav_frame)
        filter_frame.grid(row=0, column=0, columnspan=2, sticky="ew", padx=2, pady=2)
        filter_frame.grid_columnconfigure(0, weight=1)

        Label(filter_frame, text="Filter:").pack(side=LEFT, padx=(0, 2))
        self.filter_entry = Entry(filter_frame)
        self.filter_entry.pack(side=LEFT, fill=X, expand=True, padx=2)
        self.filter_entry.bind("<Return>", self.filter_files)
        Hovertip(self.filter_entry, text="Enter text and press Enter to filter by caption content")
        Button(filter_frame, text="Clear", command=self.clear_filter).pack(side=LEFT, padx=2)

        # mode toggle bar
        mode_frame = Frame(nav_frame)
        mode_frame.grid(row=1, column=0, columnspan=2, sticky="ew", padx=2, pady=(0, 2))

        self.list_mode_btn  = Button(mode_frame, text="☰ List",   width=8, relief=SUNKEN,
                                     command=self.switch_to_list)
        self.thumb_mode_btn = Button(mode_frame, text="⊞ Thumbs", width=9, relief=RAISED,
                                     command=self.switch_to_thumbs)
        self.list_mode_btn.pack(side=LEFT, padx=2)
        self.thumb_mode_btn.pack(side=LEFT, padx=2)

        self.thumb_progress_bar = ttk.Progressbar(
            mode_frame, orient=HORIZONTAL, mode="determinate", length=120
        )
        self.thumb_progress_label = Label(mode_frame, text="", fg="gray", font=("", 8))
        # hidden until generation starts

        # list box
        self.file_list = Listbox(nav_frame)
        self.file_list.grid(row=2, column=0, sticky="nsew", padx=(2, 0), pady=2)
        self.file_list.bind("<<ListboxSelect>>", self.on_file_select)
        self.file_list.bind("<FocusOut>", lambda e: self.root.after_idle(self.restore_listbox_selection))

        self.scrollbar = Scrollbar(nav_frame, orient=VERTICAL, command=self.file_list.yview, width=18)
        self.scrollbar.grid(row=2, column=1, sticky="ns", padx=(0, 2), pady=2)
        self.file_list.config(yscrollcommand=self.scrollbar.set)

        # thumb canvas (hidden until first activation)
        self.thumb_outer = Frame(nav_frame)

        self.thumb_canvas = Canvas(self.thumb_outer, bg="#2b2b2b", highlightthickness=0)
        self.thumb_canvas.pack(side=LEFT, fill=BOTH, expand=True)

        self.thumb_scrollbar = Scrollbar(self.thumb_outer, orient=VERTICAL,
                                         command=self.thumb_canvas.yview, width=18)
        self.thumb_scrollbar.pack(side=RIGHT, fill=Y)
        self.thumb_canvas.config(yscrollcommand=self.thumb_scrollbar.set)

        self.thumb_inner = Frame(self.thumb_canvas, bg="#2b2b2b")
        self._thumb_window = self.thumb_canvas.create_window((0, 0), window=self.thumb_inner, anchor="nw")

        self.thumb_inner.bind("<Configure>", self._on_thumb_inner_configure)
        self.thumb_canvas.bind("<Configure>", self._on_thumb_canvas_configure)
        for widget in (self.thumb_canvas, self.thumb_inner):
            for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                widget.bind(ev, self._on_thumb_scroll)

    # ==================================================================
    # View-mode switching
    # ==================================================================

    def switch_to_list(self):
        if self.view_mode == "list":
            return
        self.view_mode = "list"
        self.list_mode_btn.config(relief=SUNKEN)
        self.thumb_mode_btn.config(relief=RAISED)
        self.thumb_outer.grid_remove()
        self.file_list.grid(row=2, column=0, sticky="nsew", padx=(2, 0), pady=2)
        self.scrollbar.grid(row=2, column=1, sticky="ns", padx=(0, 2), pady=2)
        self.restore_listbox_selection()

    def switch_to_thumbs(self):
        if self.view_mode == "thumbs":
            return
        self.view_mode = "thumbs"
        self.thumb_mode_btn.config(relief=SUNKEN)
        self.list_mode_btn.config(relief=RAISED)
        self.file_list.grid_remove()
        self.scrollbar.grid_remove()
        self.thumb_outer.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=2, pady=2)

        # bind keyboard navigation for thumb mode
        self.thumb_canvas.focus_set()
        self.thumb_canvas.bind("<Left>",  self._thumb_key_left)
        self.thumb_canvas.bind("<Right>", self._thumb_key_right)
        self.thumb_canvas.bind("<Up>",    self._thumb_key_up)
        self.thumb_canvas.bind("<Down>",  self._thumb_key_down)

        if not self._thumb_built:
            self._thumb_built = True
            self._build_thumb_grid()
        else:
            self._highlight_current_thumb()
            # если воркер ещё работает — прогресс-бар уже виден (poll продолжает работу)

    # ==================================================================
    # Thumbnail grid
    # ==================================================================

    def _build_thumb_grid(self):
        """Rebuild the full grid from scratch using image_files list."""
        self._stop_thumb_worker()
        for w in self.thumb_inner.winfo_children():
            w.destroy()
        self._thumb_widgets.clear()
        self._thumb_photos.clear()

        if not self.image_files:
            return

        self._thumb_cols = self._calc_thumb_cols()
        self._place_placeholders()

        # load already-cached thumbs instantly, queue the rest
        pending = []
        for ap in self.image_files:
            rp = self.db._rel(ap)
            jpeg = self.db.get_thumb(rp)
            if jpeg:
                self._apply_jpeg(ap, jpeg)
            else:
                pending.append((rp, ap))

        if pending:
            self._start_thumb_worker(pending)
        else:
            self._set_progress(0, 0)

    def _calc_thumb_cols(self) -> int:
        self.thumb_canvas.update_idletasks()
        w = self.thumb_canvas.winfo_width()
        if w < 10:
            w = 400
        return max(1, w // CELL_SIZE)

    def _place_placeholders(self):
        cols = self._thumb_cols
        for idx, ap in enumerate(self.image_files):
            row, col = divmod(idx, cols)
            cf = Frame(self.thumb_inner, bg="#2b2b2b",
                       width=CELL_SIZE, height=CELL_SIZE + LABEL_HEIGHT + 2)
            cf.grid(row=row, column=col, padx=0, pady=0)
            cf.grid_propagate(False)

            # grey placeholder box
            ph = Canvas(cf, bg="#444", width=THUMB_SIZE, height=THUMB_SIZE, highlightthickness=0)
            ph.place(x=THUMB_PAD, y=THUMB_PAD)

            # label row
            rp = self.db._rel(ap)
            row_info = self.db.get_by_rel(rp)
            has_cap = row_info["has_caption"] if row_info else 0
            name = _short_name(os.path.basename(ap), CELL_SIZE)

            lf = Frame(cf, bg="#2b2b2b")
            lf.place(x=0, y=CELL_SIZE, width=CELL_SIZE, height=LABEL_HEIGHT + 2)
            dot = Label(lf, text="●", fg=_dot_color(has_cap), bg="#2b2b2b", font=("", 7))
            dot.pack(side=LEFT, padx=(2, 0))
            lbl = Label(lf, text=name, fg="#cccccc", bg="#2b2b2b", font=("", 7), anchor="w")
            lbl.pack(side=LEFT, fill=X, expand=True)

            for w in (cf, ph, lf, dot, lbl):
                w.bind("<Button-1>", lambda e, ap=ap: self._on_thumb_click_by_path(ap))
            self._bind_scroll(cf)

            self._thumb_widgets[ap] = {"frame": cf, "placeholder": ph,
                                       "img_lbl": None, "dot": dot}

        self._highlight_current_thumb()

    def _set_progress(self, done: int, total: int):
        """Show / hide the inline progress bar + label."""
        if total <= 0 or done >= total:
            self.thumb_progress_bar.pack_forget()
            self.thumb_progress_label.pack_forget()
            self.thumb_progress_label.config(text="")
        else:
            pct = int(done / total * 100)
            self.thumb_progress_bar["value"] = pct
            self.thumb_progress_label.config(text=f"{done}/{total}")
            if not self.thumb_progress_bar.winfo_ismapped():
                self.thumb_progress_bar.pack(side=LEFT, padx=(6, 2))
                self.thumb_progress_label.pack(side=LEFT, padx=(0, 4))

    def _start_thumb_worker(self, pending: list[tuple[str, str]]):
        self._set_progress(0, len(pending))
        self._thumb_worker = ThumbWorker(self.db, self._thumb_queue)
        self._thumb_worker.start(pending)
        self.root.after(50, self._poll_thumb_queue)

    def _stop_thumb_worker(self):
        if self._thumb_worker:
            self._thumb_worker.stop()
            self._thumb_worker = None
            self._set_progress(0, 0)

    def _poll_thumb_queue(self):
        processed = 0
        while processed < 20:
            try:
                msg = self._thumb_queue.get_nowait()
            except queue.Empty:
                break
            kind, rel_path, jpeg_bytes, done, total = msg
            if kind == "done":
                self._set_progress(total, total)
                return
            if kind == "abort":
                self._set_progress(0, 0)
                return
            if kind == "thumb" and rel_path:
                ap = self.db._abs(rel_path)
                if jpeg_bytes:
                    self._apply_jpeg(ap, jpeg_bytes)
                self._set_progress(done, total)
            processed += 1

        self.root.after(30, self._poll_thumb_queue)

    def _apply_jpeg(self, abs_path: str, jpeg_bytes: bytes):
        if abs_path not in self._thumb_widgets:
            return
        info = self._thumb_widgets[abs_path]
        cf = info["frame"]

        try:
            pil = Image.open(io.BytesIO(jpeg_bytes))
        except Exception:
            return

        photo = ImageTk.PhotoImage(pil)
        rp = self.db._rel(abs_path)
        self._thumb_photos[rp] = photo   # prevent GC

        ph = info.get("placeholder")
        if ph:
            try:
                ph.destroy()
            except Exception:
                pass
            info["placeholder"] = None

        tw, th = pil.size
        x = THUMB_PAD + (THUMB_SIZE - tw) // 2
        y = THUMB_PAD + (THUMB_SIZE - th) // 2
        img_lbl = Label(cf, image=photo, bg="#2b2b2b", cursor="hand2")
        img_lbl.place(x=x, y=y)

        img_lbl.bind("<Button-1>", lambda e, ap=abs_path: self._on_thumb_click_by_path(ap))
        self._bind_scroll(img_lbl)
        info["img_lbl"] = img_lbl

        self._highlight_current_thumb()

    def _highlight_current_thumb(self):
        current = self.image_files[self.image_index] if self.image_files else None
        for ap, info in self._thumb_widgets.items():
            sel = (ap == current)
            bg = "#005f87" if sel else "#2b2b2b"
            try:
                info["frame"].config(bg=bg)
                if info.get("img_lbl"):
                    info["img_lbl"].config(bg=bg)
            except Exception:
                pass
        if current:
            self._scroll_to_thumb(current)

    def _scroll_to_thumb(self, abs_path: str):
        """Scroll only if the thumb cell is not fully visible in the canvas."""
        info = self._thumb_widgets.get(abs_path)
        if not info:
            return
        cf = info["frame"]
        self.thumb_inner.update_idletasks()
        cell_top    = cf.winfo_y()
        cell_height = cf.winfo_height()
        cell_bottom = cell_top + cell_height
        total       = self.thumb_inner.winfo_height()
        ch          = self.thumb_canvas.winfo_height()
        if total <= 0 or ch <= 0:
            return

        # current visible range in pixels
        frac_top, frac_bot = self.thumb_canvas.yview()
        vis_top    = frac_top * total
        vis_bottom = frac_bot * total

        # fully visible → do nothing
        if cell_top >= vis_top and cell_bottom <= vis_bottom:
            return

        # scroll minimally: bring into view
        if cell_top < vis_top:
            target = cell_top
        else:
            target = cell_bottom - ch
        target = max(0, min(target, total - ch))
        self.thumb_canvas.yview_moveto(target / total)

    def _reflow_thumb_grid(self):
        cols = self._thumb_cols
        for idx, ap in enumerate(self.image_files):
            info = self._thumb_widgets.get(ap)
            if info:
                r, c = divmod(idx, cols)
                info["frame"].grid(row=r, column=c)

    def _remove_thumb_cell(self, abs_path: str):
        info = self._thumb_widgets.pop(abs_path, None)
        if info:
            info["frame"].destroy()
        rp = self.db._rel(abs_path)
        self._thumb_photos.pop(rp, None)
        self._reflow_thumb_grid()

    def _refresh_dot(self, abs_path: str):
        info = self._thumb_widgets.get(abs_path)
        if not info:
            return
        rp = self.db._rel(abs_path)
        row = self.db.get_by_rel(rp)
        has = row["has_caption"] if row else 0
        try:
            info["dot"].config(fg=_dot_color(has))
        except Exception:
            pass

    def _bind_scroll(self, widget):
        """Recursively bind mouse-wheel events to widget and all children."""
        for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            widget.bind(ev, self._on_thumb_scroll)
        for child in widget.winfo_children():
            self._bind_scroll(child)

    def _on_thumb_click(self, idx: int):
        if idx != self.image_index:
            self.select_image(index=idx)
        self.thumb_canvas.focus_set()
        self._highlight_current_thumb()

    def _on_thumb_click_by_path(self, abs_path: str):
        """Click handler that resolves index at call time — survives deletions/reorders."""
        try:
            idx = self.image_files.index(abs_path)
        except ValueError:
            return
        self._on_thumb_click(idx)

    # ------------------------------------------------------------------
    # Thumb keyboard navigation
    # ------------------------------------------------------------------

    def _thumb_key_left(self, event=None):
        if not self.image_files:
            return
        new_idx = max(0, self.image_index - 1)
        if new_idx != self.image_index:
            self.select_image(index=new_idx)

    def _thumb_key_right(self, event=None):
        if not self.image_files:
            return
        new_idx = min(len(self.image_files) - 1, self.image_index + 1)
        if new_idx != self.image_index:
            self.select_image(index=new_idx)

    def _thumb_key_up(self, event=None):
        if not self.image_files or self._thumb_cols < 1:
            return
        new_idx = max(0, self.image_index - self._thumb_cols)
        if new_idx != self.image_index:
            self.select_image(index=new_idx)

    def _thumb_key_down(self, event=None):
        if not self.image_files or self._thumb_cols < 1:
            return
        new_idx = min(len(self.image_files) - 1, self.image_index + self._thumb_cols)
        if new_idx != self.image_index:
            self.select_image(index=new_idx)

    def _on_thumb_inner_configure(self, event):
        self.thumb_canvas.configure(scrollregion=self.thumb_canvas.bbox("all"))

    def _on_thumb_canvas_configure(self, event):
        self.thumb_canvas.itemconfig(self._thumb_window, width=event.width)
        new_cols = max(1, event.width // CELL_SIZE)
        if new_cols != self._thumb_cols and self._thumb_widgets:
            self._thumb_cols = new_cols
            self._reflow_thumb_grid()

    def _on_thumb_scroll(self, event):
        if event.num == 4 or getattr(event, "delta", 0) > 0:
            self.thumb_canvas.yview_scroll(-3, "units")
        else:
            self.thumb_canvas.yview_scroll(3, "units")

    # ==================================================================
    # File list helpers
    # ==================================================================

    def _rebuild_file_list(self):
        """Repopulate Listbox from self.image_files."""
        self.file_list.delete(0, END)
        for ap in self.image_files:
            self.file_list.insert(END, self._reldisp(ap))

    def _reldisp(self, abs_path: str) -> str:
        """Relative path for display in Listbox (backslash, root = \\)."""
        r = os.path.relpath(abs_path, self.image_directory)
        return r if r != "." else "\\"

    # ==================================================================
    # Image display
    # ==================================================================

    def display_image(self):
        if not self.image_files:
            return
        self.index_label.config(text=f"{self.image_index + 1} of {len(self.image_files)}")
        abs_path = self.image_files[self.image_index]
        self.current_caption_file = os.path.splitext(abs_path)[0] + ".txt"

        try:
            self.original_image = Image.open(abs_path)
            self.resize_image()
        except Exception as e:
            messagebox.showerror("Error", f"Cannot open image: {e}")
            return

        self.file_entry.delete(0, END)
        self.file_entry.insert(0, os.path.basename(abs_path))
        self.current_image = abs_path

        self.load_caption()

        self.file_list.selection_clear(0, END)
        self.file_list.selection_set(self.image_index)
        self.file_list.see(self.image_index)

        if self.view_mode == "thumbs":
            self._highlight_current_thumb()

        self.dir_entry.set(self._reldisp(os.path.dirname(abs_path)))

    def restore_listbox_selection(self):
        if self.image_files and 0 <= self.image_index < len(self.image_files):
            self.file_list.selection_clear(0, END)
            self.file_list.selection_set(self.image_index)
            self.file_list.see(self.image_index)

    def resize_image(self, event=None):
        if not self.original_image:
            return
        w = (event.width  if event else self.image_label.winfo_width())  - 4
        h = (event.height if event else self.image_label.winfo_height()) - 4
        if w <= 0 or h <= 0:
            return
        ow, oh = self.original_image.size
        ratio = min(w / ow, h / oh)
        nw, nh = int(ow * ratio), int(oh * ratio)
        resized = self.original_image.resize((nw, nh), Image.LANCZOS)
        self.photo = ImageTk.PhotoImage(resized)
        self.image_label.config(image=self.photo)
        self.image_label.image = self.photo

    # ==================================================================
    # Caption load / save
    # ==================================================================

    def load_caption(self):
        if os.path.exists(self.current_caption_file):
            with open(self.current_caption_file, "r", encoding="utf-8") as f:
                caption = f.read()
        else:
            caption = ""
            with open(self.current_caption_file, "w", encoding="utf-8") as f:
                f.write(caption)
        self.text_area.config(state=NORMAL)
        self.text_area.delete(1.0, END)
        self.text_area.insert(END, caption)

    def save_caption(self):
        if not self.current_caption_file:
            return
        caption = self.text_area.get(1.0, END).strip()
        with open(self.current_caption_file, "w", encoding="utf-8") as f:
            f.write(caption)
        if self.current_image:
            rp = self.db._rel(self.current_image)
            self.db.update_caption(rp, caption)
            self._refresh_dot(self.current_image)

    # ==================================================================
    # Navigation
    # ==================================================================

    def select_image(self, step=0, index=None):
        self.save_caption()
        if index is not None:
            self.image_index = index
        else:
            self.image_index = (self.image_index + step) % len(self.image_files)
        self.display_image()

    def on_file_select(self, event):
        try:
            sel = event.widget.curselection()
            if sel:
                idx = sel[0]
                if idx != self.image_index:
                    self.select_image(index=idx)
        except IndexError:
            pass

    def search_empty_caption(self):
        start = self.image_index
        while True:
            self.image_index = (self.image_index + 1) % len(self.image_files)
            self.display_image()
            if self.text_area.get(1.0, END).strip() == "":
                return
            if self.image_index == start:
                messagebox.showinfo("Not found", "No image with an empty or missing caption found.")
                break

    # ==================================================================
    # Filter
    # ==================================================================

    def filter_files(self, event=None):
        text = self.filter_entry.get().strip()
        if not text:
            self.clear_filter()
            return

        rows = self.db.get_all(filter_text=text)
        rp_set = {r["rel_path"] for r in rows}
        self.image_files = [ap for ap in self.all_image_files
                            if self.db._rel(ap) in rp_set]

        self._rebuild_file_list()
        self._resolve_index_after_filter()

        if self.view_mode == "thumbs":
            self._build_thumb_grid()

    def clear_filter(self):
        self.filter_entry.delete(0, END)
        self.image_files = list(self.all_image_files)
        self._rebuild_file_list()
        self._resolve_index_after_filter()

        if self.view_mode == "thumbs":
            self._build_thumb_grid()

    def _resolve_index_after_filter(self):
        if not self.image_files:
            self.current_image = None
            self.current_caption_file = None
            self.original_image = None
            self.image_label.config(image="")
            self.text_area.delete(1.0, END)
            self.file_entry.delete(0, END)
            self.index_label.config(text="0 of 0")
            return
        if self.current_image and self.current_image in self.image_files:
            self.image_index = self.image_files.index(self.current_image)
        else:
            self.image_index = 0
        self.display_image()

    # ==================================================================
    # Load images / open folder
    # ==================================================================

    def load_images(self):
        directory = filedialog.askdirectory(title="Select Image Directory")
        if not directory:
            if not self.image_files:
                messagebox.showinfo("No Images", "No directory selected.")
                self.root.quit()
            return

        self._stop_thumb_worker()

        # scan disk
        found: list[str] = []
        for root, _, files in os.walk(directory):
            for f in files:
                if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                    found.append(os.path.join(root, f))
        found.sort()

        if not found:
            messagebox.showinfo("No Images", "No images found in the selected directory.")
            self.root.quit()
            return

        # open DB and sync
        self.db.open(directory)
        self.db.sync(found)

        self.image_directory = directory
        self.all_image_files = found
        self.image_files     = list(found)
        self.image_index     = 0

        # populate directories combobox
        dirs = []
        for root, _, _ in os.walk(directory):
            r = os.path.relpath(root, directory)
            dirs.append("\\" if r == "." else r)
        dirs.sort()
        self.dir_entry["values"] = dirs

        self._thumb_built = False
        for w in self.thumb_inner.winfo_children():
            w.destroy()
        self._thumb_widgets.clear()
        self._thumb_photos.clear()

        self._rebuild_file_list()

    def open_folder(self):
        self.load_images()
        self.image_index = 0
        self.display_image()
        self.filter_entry.delete(0, END)
        if self.view_mode == "thumbs":
            self._build_thumb_grid()

    # ==================================================================
    # Rename / move
    # ==================================================================

    def rename_file(self, event=None):
        if not self.current_image:
            return
        old_ap  = self.current_image
        old_txt = self.current_caption_file
        old_rp  = self.db._rel(old_ap)
        directory = os.path.dirname(old_ap)
        old_base  = os.path.basename(old_ap)
        new_base  = self.file_entry.get().strip()

        if not new_base:
            self.file_entry.delete(0, END)
            self.file_entry.insert(0, old_base)
            return
        if not os.path.splitext(new_base)[1]:
            new_base += os.path.splitext(old_base)[1]

        new_ap  = os.path.join(directory, new_base)
        new_txt = os.path.splitext(new_ap)[0] + ".txt"

        if new_ap == old_ap:
            return

        stem = os.path.splitext(new_base)[0].lower()
        for f in os.listdir(directory):
            fs, fe = os.path.splitext(f)
            if fs.lower() == stem and fe.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                if os.path.join(directory, f) != old_ap:
                    messagebox.showerror("Rename error",
                        f"File '{f}' already exists. Enter another name.")
                    self.file_entry.delete(0, END)
                    self.file_entry.insert(0, old_base)
                    return

        try:
            os.rename(old_ap, new_ap)
            if os.path.exists(old_txt):
                os.rename(old_txt, new_txt)
        except Exception as e:
            messagebox.showerror("Rename error", str(e))
            self.file_entry.delete(0, END)
            self.file_entry.insert(0, old_base)
            return

        new_rp = self.db._rel(new_ap)
        self.db.rename(old_rp, new_rp, new_ap)

        self._update_path_in_lists(old_ap, new_ap)
        self.current_image        = new_ap
        self.current_caption_file = new_txt

        self.file_entry.delete(0, END)
        self.file_entry.insert(0, os.path.basename(new_ap))
        self._rebuild_file_list()
        self.file_list.selection_clear(0, END)
        self.file_list.selection_set(self.image_index)
        self.file_list.see(self.image_index)
        self.file_entry.focus_set()

        if old_ap in self._thumb_widgets:
            self._thumb_widgets[new_ap] = self._thumb_widgets.pop(old_ap)
        if old_rp in self._thumb_photos:
            self._thumb_photos[new_rp] = self._thumb_photos.pop(old_rp)

    def on_dir_change(self, event=None):
        if not self.current_image:
            return
        rel_dir = self.dir_entry.get()
        new_dir = (self.image_directory if rel_dir == "\\"
                   else os.path.join(self.image_directory, rel_dir))
        if not os.path.isdir(new_dir):
            return

        old_ap   = self.current_image
        old_txt  = self.current_caption_file
        old_rp   = self.db._rel(old_ap)
        base     = os.path.basename(old_ap)
        stem     = os.path.splitext(base)[0].lower()

        for f in os.listdir(new_dir):
            fs, fe = os.path.splitext(f)
            if fs.lower() == stem and fe.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                messagebox.showerror("Move error",
                    f"File '{fs}{fe}' already exists in destination. Rename first.")
                self.dir_entry.set(self._reldisp(os.path.dirname(old_ap)))
                return

        new_ap  = os.path.join(new_dir, base)
        new_txt = os.path.splitext(new_ap)[0] + ".txt"
        new_rp  = self.db._rel(new_ap)

        try:
            os.rename(old_ap, new_ap)
            if os.path.exists(old_txt):
                os.rename(old_txt, new_txt)
        except Exception as e:
            messagebox.showerror("Move error", str(e))
            self.dir_entry.set(self._reldisp(os.path.dirname(old_ap)))
            return

        self.db.rename(old_rp, new_rp, new_ap)
        self._update_path_in_lists(old_ap, new_ap)
        self.current_image        = new_ap
        self.current_caption_file = new_txt

        self.file_entry.delete(0, END)
        self.file_entry.insert(0, os.path.basename(new_ap))
        self._rebuild_file_list()
        self.file_list.selection_clear(0, END)
        self.file_list.selection_set(self.image_index)
        self.file_list.see(self.image_index)

        if old_ap in self._thumb_widgets:
            self._thumb_widgets[new_ap] = self._thumb_widgets.pop(old_ap)
        if old_rp in self._thumb_photos:
            self._thumb_photos[new_rp] = self._thumb_photos.pop(old_rp)

        self.dir_entry.set(self._reldisp(new_dir))

    def _update_path_in_lists(self, old_ap: str, new_ap: str):
        if old_ap in self.image_files:
            idx = self.image_files.index(old_ap)
            self.image_files[idx] = new_ap
        if old_ap in self.all_image_files:
            idx = self.all_image_files.index(old_ap)
            self.all_image_files[idx] = new_ap

    # ==================================================================
    # Delete
    # ==================================================================

    def delete_current_image(self):
        if not self.current_image or not self.image_files:
            return
        if not messagebox.askyesno("Confirm Delete",
                f"Delete permanently:\n{os.path.basename(self.current_image)}\n"
                "\nThis will delete the image and caption file.", icon="warning"):
            return

        cur_idx  = self.image_index
        del_path = self.current_image
        del_rp   = self.db._rel(del_path)

        try:
            if os.path.exists(del_path):
                os.remove(del_path)
        except Exception as e:
            messagebox.showerror("Delete Error", str(e))
            return
        try:
            if self.current_caption_file and os.path.exists(self.current_caption_file):
                os.remove(self.current_caption_file)
        except Exception as e:
            messagebox.showerror("Delete Error", str(e))
            return

        self.db.delete(del_rp)

        self.image_files.pop(cur_idx)
        if del_path in self.all_image_files:
            self.all_image_files.remove(del_path)

        self.file_list.delete(cur_idx)
        self._remove_thumb_cell(del_path)

        if not self.image_files:
            self.current_image = None
            self.current_caption_file = None
            self.original_image = None
            self.image_label.config(image="")
            self.text_area.delete(1.0, END)
            self.file_entry.delete(0, END)
            self.index_label.config(text="0 of 0")
            messagebox.showinfo("All Deleted", "All images have been deleted.")
            return

        self.image_index = min(cur_idx, len(self.image_files) - 1)
        self.display_image()

    # ==================================================================
    # Find & Replace
    # ==================================================================

    def open_find_replace(self):
        def perform_replace():
            find_text    = find_entry.get()
            replace_text = replace_entry.get()
            if not find_text:
                messagebox.showerror("Error", "Please enter text to find.")
                return
            btn.config(state=DISABLED)
            count = 0
            updated_rps = []
            for ap in self.image_files:
                cap_file = os.path.splitext(ap)[0] + ".txt"
                if os.path.exists(cap_file):
                    with open(cap_file, "r", encoding="utf-8") as f:
                        content = f.read()
                    new_content = content.replace(find_text, replace_text)
                    if content != new_content:
                        count += content.count(find_text)
                        with open(cap_file, "w", encoding="utf-8") as f:
                            f.write(new_content)
                        rp = self.db._rel(ap)
                        self.db.update_caption(rp, new_content)
                        self._refresh_dot(ap)
                        updated_rps.append(rp)
            messagebox.showinfo("Done", f"Replaced: {count}")

        win = Toplevel(self.root)
        win.title("Find and Replace")
        Label(win, text="Find:").grid(row=0, column=0, padx=5, pady=5)
        find_entry = Entry(win, width=30)
        find_entry.grid(row=0, column=1, padx=5, pady=5)
        Label(win, text="Replace:").grid(row=1, column=0, padx=5, pady=5)
        replace_entry = Entry(win, width=30)
        replace_entry.grid(row=1, column=1, padx=5, pady=5)
        btn = Button(win, text="Replace", command=perform_replace)
        btn.grid(row=2, column=0, columnspan=2, pady=10)

    # ==================================================================
    # Translate
    # ==================================================================

    def translate_text(self):
        if not self.text_area.get(1.0, END).rstrip().endswith(","):
            self.text_area.insert(END, ",")
        s = self.trans_text_area.get(1.0, END).strip()
        t = " " + translator.translate(s, src="ru", dest="en").text.strip()
        self.text_area.insert(END, t)

    # ==================================================================
    # Open in external viewer
    # ==================================================================

    def open_image(self, event):
        if not self.current_image:
            return
        try:
            if platform.system() == "Windows":
                os.startfile(self.current_image)
            elif platform.system() == "Darwin":
                subprocess.call(("open", self.current_image))
            else:
                subprocess.call(("xdg-open", self.current_image))
        except Exception as e:
            messagebox.showerror("Error", str(e))


# ===========================================================================
# Helpers
# ===========================================================================

def _short_name(name: str, cell_w: int, font_px: int = 7) -> str:
    max_chars = max(4, cell_w // font_px)
    return name if len(name) <= max_chars else name[:max_chars - 1] + "…"


def _dot_color(has_caption: int) -> str:
    return "#4caf50" if has_caption else "#f44336"


# workaround for ctrl+c ctrl+v on non-Latin keyboard layouts
def keypress(e):
    if e.keycode == 86 and e.keysym != "v" and e.char != "м":
        e.widget.event_generate("<<Paste>>")
    elif e.keycode == 67 and e.keysym != "c" and e.char != "с":
        e.widget.event_generate("<<Copy>>")


if __name__ == "__main__":
    root = Tk()
    root.bind_all("<KeyPress>", keypress)
    app = ImageCaptionApp(root)
    root.mainloop()
