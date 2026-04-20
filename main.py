import os
from tkinter import *
from tkinter import ttk
from tkinter import filedialog, messagebox
from idlelib.tooltip import Hovertip
from PIL import Image, ImageTk
import subprocess
import platform
from deep_translator import GoogleTranslator

from db import ImageDB
from thumb_view import ThumbnailView


class ImageCaptionApp:
    def __init__(self, root):
        self.root = root
        self.root.title("a7in image Caption Utility")

        # ---- DB / state ----
        self.db = ImageDB()

        # In-memory image list (list of rel paths, ordered by rel_path)
        self.image_files:     list[str] = []   # current (possibly filtered)
        self.all_image_files: list[str] = []   # full unfiltered list
        self.image_directory: str = ""
        self.image_index:     int = 0

        self.current_image:        str | None = None
        self.current_caption_file: str | None = None
        self.original_image:       Image.Image | None = None
        self.photo:                ImageTk.PhotoImage | None = None

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
        self.show_empty_var = BooleanVar()
        Checkbutton(filter_frame, text="Show empty", variable=self.show_empty_var, command=self.filter_files).pack(side=LEFT, padx=2)

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

        # Thumbnail panel — fully encapsulated in ThumbnailView.
        self.thumb_view = ThumbnailView(
            parent=nav_frame,
            db=self.db,
            on_select=lambda idx: self.select_image(index=idx),
            on_open=self._open_image_rp,
            on_progress=self._set_thumb_progress,
        )

    # ==================================================================
    # View-mode switching
    # ==================================================================

    def switch_to_list(self):
        if self.view_mode == "list":
            return
        self.view_mode = "list"
        self.list_mode_btn.config(relief=SUNKEN)
        self.thumb_mode_btn.config(relief=RAISED)
        self.thumb_view.grid_remove()
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
        self.thumb_view.grid(row=2, column=0, columnspan=2,
                             sticky="nsew", padx=2, pady=2)
        self.thumb_view.set_images(self.image_files, self.image_index)
        self.thumb_view.focus()

    # ==================================================================
    # Thumbnail progress bar
    # ==================================================================

    def _set_thumb_progress(self, done: int, total: int):
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

    def _open_image_rp(self, rel_path: str):
        """Open the image externally from a thumbnail double-click."""
        try:
            ap = self.db._abs(rel_path)
        except Exception:
            return
        try:
            if platform.system() == "Windows":
                os.startfile(ap)
            elif platform.system() == "Darwin":
                subprocess.call(("open", ap))
            else:
                subprocess.call(("xdg-open", ap))
        except Exception as e:
            messagebox.showerror("Error", str(e))

    # ==================================================================
    # File list helpers
    # ==================================================================

    def _rebuild_file_list(self):
        """Repopulate Listbox from self.image_files."""
        self.file_list.delete(0, END)
        for rp in self.image_files:
            self.file_list.insert(END, self._reldisp(rp))

    def _reldisp(self, rp: str) -> str:
        r"""Relative path for display in Listbox (backslash, root = \)."""
        if not rp or rp == ".": return "\\"
        return rp.replace("/", "\\")

    # ==================================================================
    # Image display
    # ==================================================================

    def display_image(self):
        if not self.image_files:
            return
        self.index_label.config(text=f"{self.image_index + 1} of {len(self.image_files)}")
        rp = self.image_files[self.image_index]
        abs_path = self.db._abs(rp)
        self.current_caption_file = os.path.splitext(abs_path)[0] + ".txt"

        try:
            self.original_image = Image.open(abs_path)
            self.resize_image()
        except Exception as e:
            messagebox.showerror("Error", f"Cannot open image: {e}")
            return

        self.file_entry.delete(0, END)
        self.file_entry.insert(0, os.path.basename(rp))
        self.current_image = rp
        dname = os.path.dirname(rp)
        self.dir_entry.set(self._reldisp(dname) if dname else "\\")

        self.text_area.delete("1.0", END)
        if os.path.exists(self.current_caption_file):
            with open(self.current_caption_file, "r", encoding="utf-8") as f:
                self.text_area.insert("1.0", f.read())

        self.file_list.selection_clear(0, END)
        self.file_list.selection_set(self.image_index)
        self.file_list.see(self.image_index)

        if self.view_mode == "thumbs":
            self.thumb_view.set_current(self.image_index)


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
        caption = self.text_area.get("1.0", "end-1c")
        with open(self.current_caption_file, "w", encoding="utf-8") as f:
            f.write(caption)
        if self.current_image:
            self.db.update_caption(self.current_image, caption.strip())
            self.thumb_view.refresh_caption_dot(self.current_image)

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

    # ==================================================================
    # Filter
    # ==================================================================

    def filter_files(self, event=None):
        text = self.filter_entry.get().strip()
        show_empty = self.show_empty_var.get()
        if not text and not show_empty:
            self.clear_filter()
            return

        rows = self.db.get_all(filter_text=text, show_empty=show_empty)
        rp_set = {r["rel_path"] for r in rows}
        self.image_files = [rp for rp in self.all_image_files
                            if rp in rp_set]
        self.image_index = 0
        self._rebuild_file_list()
        self._resolve_index_after_filter()

        if self.view_mode == "thumbs":
            self.thumb_view.set_images(self.image_files, self.image_index)

    def clear_filter(self):
        self.filter_entry.delete(0, END)
        self.show_empty_var.set(False)
        self.image_files = list(self.all_image_files)
        self._rebuild_file_list()
        self._resolve_index_after_filter()

        if self.view_mode == "thumbs":
            self.thumb_view.set_images(self.image_files, self.image_index)

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

        # Drain any in-flight thumbnail work before switching DB.
        self.thumb_view.set_images([], 0)

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
        synced_rps = self.db.sync(found)

        self.image_directory = directory
        self.all_image_files = synced_rps
        self.image_files     = list(synced_rps)
        self.image_index     = 0

        # populate directories combobox
        dirs = []
        for root, _, _ in os.walk(directory):
            r = os.path.relpath(root, directory)
            dirs.append("\\" if r == "." else r)
        dirs.sort()
        self.dir_entry["values"] = dirs

        self._rebuild_file_list()

        if self.view_mode == "thumbs":
            self.thumb_view.set_images(self.image_files, self.image_index)

    def open_folder(self):
        self.load_images()
        self.image_index = 0
        self.display_image()
        self.filter_entry.delete(0, END)
        if self.view_mode == "thumbs":
            self.thumb_view.set_images(self.image_files, self.image_index)

    # ==================================================================
    # Rename / move
    # ==================================================================

    def rename_file(self, event=None):
        if not self.current_image:
            return
        old_rp  = self.current_image
        old_ap  = self.db._abs(old_rp)
        old_txt = self.current_caption_file
        directory = os.path.dirname(old_ap)
        old_base  = os.path.basename(old_rp)
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
        self.db.rename(old_rp, new_rp)

        self._update_path_in_lists(old_rp, new_rp)
        self.current_image        = new_rp
        self.current_caption_file = new_txt

        self.file_entry.delete(0, END)
        self.file_entry.insert(0, os.path.basename(new_rp))
        self._rebuild_file_list()
        self.file_list.selection_clear(0, END)
        self.file_list.selection_set(self.image_index)
        self.file_list.see(self.image_index)
        self.file_entry.focus_set()

        self.thumb_view.rename(old_rp, new_rp)

    def on_dir_change(self, event=None):
        if not self.current_image:
            return
        rel_dir = self.dir_entry.get()
        new_dir = (self.image_directory if rel_dir == "\\"
                   else os.path.join(self.image_directory, rel_dir))
        if not os.path.isdir(new_dir):
            return

        old_rp   = self.current_image
        old_ap   = self.db._abs(old_rp)
        old_txt  = self.current_caption_file
        base     = os.path.basename(old_rp)
        stem     = os.path.splitext(base)[0].lower()
        for f in os.listdir(new_dir):
            fs, fe = os.path.splitext(f)
            if fs.lower() == stem and fe.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                messagebox.showerror("Move error",
                    f"File '{fs}{fe}' already exists in destination. Rename first.")
                db_dir = os.path.dirname(old_rp)
                self.dir_entry.set(self._reldisp(db_dir) if db_dir else "\\")
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
            db_dir = os.path.dirname(old_rp)
            self.dir_entry.set(self._reldisp(db_dir) if db_dir else "\\")
            return

        self.db.rename(old_rp, new_rp)
        self._update_path_in_lists(old_rp, new_rp)
        self.current_image        = new_rp
        self.current_caption_file = new_txt

        self.file_entry.delete(0, END)
        self.file_entry.insert(0, os.path.basename(new_rp))
        self._rebuild_file_list()
        self.file_list.selection_clear(0, END)
        self.file_list.selection_set(self.image_index)
        self.file_list.see(self.image_index)

        self.thumb_view.rename(old_rp, new_rp)

        self.dir_entry.set(self._reldisp(new_dir))
        
    def _update_path_in_lists(self, old_rp: str, new_rp: str):
        if old_rp in self.image_files:
            idx = self.image_files.index(old_rp)
            self.image_files[idx] = new_rp
        if old_rp in self.all_image_files:
            idx = self.all_image_files.index(old_rp)
            self.all_image_files[idx] = new_rp

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
        del_rp   = self.current_image
        del_path = self.db._abs(del_rp)
        del_txt  = os.path.splitext(del_path)[0] + ".txt"

        try:
            if os.path.exists(del_path):
                os.remove(del_path)
        except Exception as e:
            messagebox.showerror("Delete Error", str(e))
            return
        try:
            if del_txt and os.path.exists(del_txt):
                os.remove(del_txt)
        except Exception as e:
            messagebox.showerror("Delete Error", str(e))
            return

        self.db.delete(del_rp)

        # remove from lists
        self.image_files.pop(cur_idx)
        if del_rp in self.all_image_files:
            self.all_image_files.remove(del_rp)

        self.file_list.delete(cur_idx)
        self.thumb_view.remove(del_rp)

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
            for rp in self.image_files:
                ap = self.db._abs(rp)
                cap_file = os.path.splitext(ap)[0] + ".txt"
                if os.path.exists(cap_file):
                    with open(cap_file, "r", encoding="utf-8") as f:
                        content = f.read()
                    new_content = content.replace(find_text, replace_text)
                    if content != new_content:
                        count += content.count(find_text)
                        with open(cap_file, "w", encoding="utf-8") as f:
                            f.write(new_content)
                        self.db.update_caption(rp, new_content)
                        self.thumb_view.refresh_caption_dot(rp)

            messagebox.showinfo("Result", f"Replaced {count} instances.")

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
        s = self.trans_text_area.get(1.0, END).strip()
        if not s:
            return

        # Add comma if needed
        current_text = self.text_area.get(1.0, END).rstrip()
        if current_text and not current_text.endswith(","):
            self.text_area.insert(END, ",")

        src_lang = self.text_lang.get(1.0, END).strip() or "ru"
        try:
            translated = GoogleTranslator(source=src_lang, target='en').translate(s)
            t = " " + translated.strip()
            self.text_area.insert(END, t)
        except Exception as e:
            messagebox.showerror("Translation Error", str(e))

    # ==================================================================
    # Open in external viewer
    # ==================================================================

    def open_image(self, event):
        if not self.current_image:
            return
        ap = self.db._abs(self.current_image)
        try:
            if platform.system() == "Windows":
                os.startfile(ap)
            elif platform.system() == "Darwin":
                subprocess.call(("open", ap))
            else:
                subprocess.call(("xdg-open", ap))
        except Exception as e:
            messagebox.showerror("Error", str(e))


# ===========================================================================
# Helpers
# ===========================================================================

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
