"""Automatic image-caption generation via an external LLM.

Self-contained module: holds the LLM connection settings (persisted as an
``.ini`` next to the program), a small settings dialog, and the logic that
sends the current image to an OpenAI-compatible vision endpoint and returns a
plain-text English description.

Borrowed in spirit from the Ideogram-Json-Captioner project, but trimmed down:
we only need a normal English caption (no JSON schema), and we talk to the
endpoint directly over HTTP so no extra dependency is required.

Public surface used by main.py::

    captioner = AutoCaptioner(app)
    captioner.generate_for_current()   # button command
    captioner.open_settings()          # button command
"""

from __future__ import annotations

import base64
import configparser
import io
import json
import mimetypes
import os
import threading
import urllib.request
import urllib.error
from dataclasses import dataclass, asdict, fields
from tkinter import (
    Toplevel, Frame, Label, Entry, Button, Text, StringVar,
    END, NORMAL, DISABLED, WORD, BOTH, X, LEFT, RIGHT,
)
from tkinter import ttk, messagebox

from PIL import Image


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

_INI_NAME = "auto_caption_settings.ini"
_SECTION = "llm"

DEFAULT_SYSTEM_PROMPT = (
    "You write factual image captions in English for image datasets. "
    "Return one polished plain-text caption only. No markdown, no JSON, no "
    "bullet points. Describe the main subjects, setting, style, lighting, "
    "camera/viewpoint, and notable objects without guessing identities."
)

DEFAULT_USER_PROMPT = (
    "Write a detailed but clean description of this image in English. Keep it "
    "useful for recreating the image, but avoid unsupported proper names or "
    "speculation."
)


def settings_path() -> str:
    """Path to the .ini stored in the program folder (next to this module)."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), _INI_NAME)


@dataclass
class LLMSettings:
    base_url: str = "http://127.0.0.1:8000/v1"
    api_key: str = ""
    model: str = ""
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    user_prompt: str = DEFAULT_USER_PROMPT
    max_tokens: int = 1000
    temperature: float = 0.2
    timeout: float = 180.0
    # Sampling sent on every request so the result does not depend on the
    # server's launch-time defaults (aggressive penalties can produce garbage).
    top_p: float = 0.9
    presence_penalty: float = 0.0
    repeat_penalty: float = 1.0
    # auto | original | png | jpeg : how the image is encoded before sending
    vision_image_format: str = "auto"

    @classmethod
    def load(cls, path: str | None = None) -> "LLMSettings":
        path = path or settings_path()
        defaults = cls()
        if not os.path.exists(path):
            return defaults
        parser = configparser.ConfigParser()
        try:
            parser.read(path, encoding="utf-8")
        except (OSError, configparser.Error):
            return defaults
        if not parser.has_section(_SECTION):
            return defaults

        sec = parser[_SECTION]
        values = asdict(defaults)
        for f in fields(cls):
            if f.name not in sec:
                continue
            raw = sec[f.name]
            try:
                if f.type == "int" or f.name == "max_tokens":
                    values[f.name] = int(raw)
                elif f.type == "float" or f.name in ("temperature", "timeout"):
                    values[f.name] = float(raw)
                else:
                    values[f.name] = raw
            except (TypeError, ValueError):
                pass  # keep default on malformed value
        return cls(**values)

    def save(self, path: str | None = None) -> str:
        path = path or settings_path()
        parser = configparser.ConfigParser()
        parser[_SECTION] = {k: str(v) for k, v in asdict(self).items()}
        with open(path, "w", encoding="utf-8") as f:
            parser.write(f)
        return path


# ---------------------------------------------------------------------------
# Image encoding + HTTP call
# ---------------------------------------------------------------------------

def _image_to_data_url(path: str, vision_image_format: str = "auto") -> str:
    """Encode an image file as a base64 ``data:`` URL for the chat payload."""
    fmt = (vision_image_format or "auto").lower().strip()
    suffix = os.path.splitext(path)[1].lower()

    convert_to: str | None = None
    if fmt == "png":
        convert_to = "PNG"
    elif fmt in ("jpeg", "jpg"):
        convert_to = "JPEG"
    elif fmt == "auto" and suffix == ".webp":
        # Many endpoints choke on webp; re-encode to PNG by default.
        convert_to = "PNG"

    if convert_to is None:
        mime, _ = mimetypes.guess_type(path)
        mime = mime or "application/octet-stream"
        with open(path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("utf-8")
        return f"data:{mime};base64,{b64}"

    with Image.open(path) as image:
        if convert_to == "JPEG":
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            mime = "image/jpeg"
        else:
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            mime = "image/png"
        buffer = io.BytesIO()
        image.save(buffer, format=convert_to)
        b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def _chat_completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def _models_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/models"):
        return base
    if base.endswith("/v1"):
        return base + "/models"
    return base + "/v1/models"


def list_models(settings: "LLMSettings", timeout: float = 5.0) -> list[str]:
    """Return model ids exposed by the endpoint (empty list on failure)."""
    request = urllib.request.Request(_models_url(settings.base_url))
    if settings.api_key.strip():
        request.add_header("Authorization", f"Bearer {settings.api_key.strip()}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return []
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    ids = []
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip():
            ids.append(item["id"].strip())
    return ids


def generate_caption(settings: LLMSettings, image_path: str) -> str:
    """Send the image to the LLM and return a plain-text English caption.

    Raises RuntimeError with a user-facing message on any failure.
    """
    model = settings.model.strip()
    if not model:
        # Servers like llama-server expose exactly one model; auto-pick it so
        # the user doesn't have to type the gguf filename by hand.
        available = list_models(settings)
        if available:
            model = available[0]
        else:
            raise RuntimeError(
                "No model name is configured and none could be fetched from the "
                "server. Open LLM settings and set the model (or check the URL)."
            )

    image_url = _image_to_data_url(image_path, settings.vision_image_format)
    payload = {
        "model": model,
        "temperature": settings.temperature,
        "top_p": settings.top_p,
        "presence_penalty": settings.presence_penalty,
        # llama.cpp's name; OpenAI-style servers ignore unknown keys.
        "repeat_penalty": settings.repeat_penalty,
        "max_tokens": settings.max_tokens,
        "messages": [
            {"role": "system", "content": settings.system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": settings.user_prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            },
        ],
    }
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        _chat_completions_url(settings.base_url),
        data=data,
        method="POST",
    )
    request.add_header("Content-Type", "application/json")
    if settings.api_key.strip():
        request.add_header("Authorization", f"Bearer {settings.api_key.strip()}")

    try:
        with urllib.request.urlopen(request, timeout=settings.timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        raise RuntimeError(f"LLM request failed: HTTP {exc.code}. {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Cannot reach LLM at {settings.base_url}: {exc.reason}"
        ) from exc

    try:
        parsed = json.loads(body)
        choice = parsed["choices"][0]
        message = choice["message"]
        content = message.get("content")
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected LLM response: {body[:500]}") from exc

    if isinstance(content, list):  # some servers return content parts
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    caption = (content or "").strip().strip('"').strip()
    if caption:
        return caption

    # Reasoning models may leave content empty and put text in reasoning_content.
    # That text is the model's thinking, not a finished caption, so we don't use
    # it — but we point the user at the real cause.
    if (message.get("reasoning_content") or "").strip():
        raise RuntimeError(
            "The model returned only reasoning/thinking text and no caption. "
            "This is usually a reasoning model or an aggressive sampling setup. "
            "Try a non-reasoning vision model, or relaunch the server without "
            "high presence/repeat penalties."
        )
    finish = choice.get("finish_reason")
    raise RuntimeError(
        f"LLM returned an empty caption (finish_reason={finish}). Check the model "
        "and the server's sampling settings."
    )


# ---------------------------------------------------------------------------
# Settings dialog
# ---------------------------------------------------------------------------

class _SettingsDialog(Toplevel):
    """Modal dialog for editing and saving LLM connection settings."""

    def __init__(self, parent, settings: LLMSettings, on_saved):
        super().__init__(parent)
        self.title("LLM Connection Settings")
        self.transient(parent)
        self.resizable(False, False)
        self._on_saved = on_saved

        self._base_url = StringVar(value=settings.base_url)
        self._api_key = StringVar(value=settings.api_key)
        self._model = StringVar(value=settings.model)
        self._max_tokens = StringVar(value=str(settings.max_tokens))
        self._temperature = StringVar(value=str(settings.temperature))
        self._top_p = StringVar(value=str(settings.top_p))
        self._presence_penalty = StringVar(value=str(settings.presence_penalty))
        self._repeat_penalty = StringVar(value=str(settings.repeat_penalty))
        self._timeout = StringVar(value=str(settings.timeout))
        self._fmt = StringVar(value=settings.vision_image_format)

        body = Frame(self)
        body.pack(fill=BOTH, expand=True, padx=10, pady=10)

        row = 0
        row = self._entry(body, row, "OpenAI-compatible base URL", self._base_url)
        row = self._entry(body, row, "API key", self._api_key, show="*")

        # Model: editable combobox + Refresh (leave blank to auto-detect).
        Label(body, text="Model name").grid(row=row, column=0, sticky="w", pady=3)
        model_row = Frame(body)
        model_row.grid(row=row, column=1, sticky="ew", pady=3)
        model_row.grid_columnconfigure(0, weight=1)
        self._model_combo = ttk.Combobox(model_row, textvariable=self._model)
        self._model_combo.grid(row=0, column=0, sticky="ew")
        Button(model_row, text="Refresh", command=self._refresh_models).grid(
            row=0, column=1, padx=(4, 0)
        )
        Label(body, text="(blank = auto-detect)", fg="gray").grid(
            row=row + 1, column=1, sticky="w"
        )
        row += 2

        row = self._entry(body, row, "Max tokens", self._max_tokens)
        row = self._entry(body, row, "Temperature", self._temperature)
        row = self._entry(body, row, "Top-p", self._top_p)
        row = self._entry(body, row, "Presence penalty", self._presence_penalty)
        row = self._entry(body, row, "Repeat penalty", self._repeat_penalty)
        row = self._entry(body, row, "Timeout (s)", self._timeout)

        Label(body, text="Image format").grid(row=row, column=0, sticky="w", pady=3)
        ttk.Combobox(
            body, textvariable=self._fmt, state="readonly", width=28,
            values=("auto", "original", "png", "jpeg"),
        ).grid(row=row, column=1, sticky="ew", pady=3)
        row += 1

        Label(body, text="System prompt").grid(row=row, column=0, sticky="nw", pady=3)
        self._system = Text(body, width=48, height=5, wrap=WORD)
        self._system.grid(row=row, column=1, sticky="ew", pady=3)
        self._system.insert("1.0", settings.system_prompt)
        row += 1

        Label(body, text="User prompt").grid(row=row, column=0, sticky="nw", pady=3)
        self._user = Text(body, width=48, height=4, wrap=WORD)
        self._user.grid(row=row, column=1, sticky="ew", pady=3)
        self._user.insert("1.0", settings.user_prompt)
        row += 1

        body.grid_columnconfigure(1, weight=1)

        btns = Frame(self)
        btns.pack(fill=X, padx=10, pady=(0, 10))
        Button(btns, text="Save", width=10, command=self._save).pack(side=RIGHT, padx=2)
        Button(btns, text="Cancel", width=10, command=self.destroy).pack(side=RIGHT, padx=2)
        Button(btns, text="Test connection", command=self._test).pack(side=LEFT, padx=2)

        self.grab_set()
        self._base_url_focus()

    def _base_url_focus(self):
        self.after(100, self.focus_force)

    def _current_settings(self) -> "LLMSettings":
        """Snapshot the connection fields (URL/key) for probing the server."""
        return LLMSettings(
            base_url=self._base_url.get().strip() or "http://127.0.0.1:8000/v1",
            api_key=self._api_key.get().strip(),
        )

    def _refresh_models(self):
        models = list_models(self._current_settings())
        if not models:
            messagebox.showwarning(
                "No models",
                "Could not fetch models. Check the base URL and that the server is running.",
                parent=self,
            )
            return
        self._model_combo["values"] = models
        if not self._model.get().strip():
            self._model.set(models[0])

    def _test(self):
        models = list_models(self._current_settings())
        if models:
            messagebox.showinfo(
                "Connection OK",
                "Server reachable. Available models:\n  " + "\n  ".join(models),
                parent=self,
            )
            self._model_combo["values"] = models
        else:
            messagebox.showerror(
                "Connection failed",
                "Could not reach the server or list its models.\n"
                "Check the base URL (e.g. http://127.0.0.1:8080/v1) and that the "
                "server is running.",
                parent=self,
            )

    def _entry(self, parent, row, label, var, show=None):
        Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        Entry(parent, textvariable=var, width=40, show=show).grid(
            row=row, column=1, sticky="ew", pady=3
        )
        return row + 1

    def _save(self):
        try:
            max_tokens = int(self._max_tokens.get().strip())
            temperature = float(self._temperature.get().strip())
            top_p = float(self._top_p.get().strip())
            presence_penalty = float(self._presence_penalty.get().strip())
            repeat_penalty = float(self._repeat_penalty.get().strip())
            timeout = float(self._timeout.get().strip())
        except ValueError:
            messagebox.showerror(
                "Invalid value",
                "Max tokens must be an integer; temperature, top-p, penalties and "
                "timeout must be numbers.",
                parent=self,
            )
            return

        settings = LLMSettings(
            base_url=self._base_url.get().strip() or "http://127.0.0.1:8000/v1",
            api_key=self._api_key.get().strip(),
            model=self._model.get().strip(),
            system_prompt=self._system.get("1.0", "end-1c").strip() or DEFAULT_SYSTEM_PROMPT,
            user_prompt=self._user.get("1.0", "end-1c").strip() or DEFAULT_USER_PROMPT,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            presence_penalty=presence_penalty,
            repeat_penalty=repeat_penalty,
            timeout=timeout,
            vision_image_format=self._fmt.get().strip() or "auto",
        )
        try:
            settings.save()
        except OSError as exc:
            messagebox.showerror("Save failed", str(exc), parent=self)
            return
        self._on_saved(settings)
        self.destroy()


# ---------------------------------------------------------------------------
# Controller wiring into the main app
# ---------------------------------------------------------------------------

class AutoCaptioner:
    """Glue between the main app and the captioning logic.

    Owns the loaded settings and provides the two button commands. Generation
    runs on a worker thread so the Tk UI stays responsive; the result is
    applied back on the main thread via ``root.after``.
    """

    def __init__(self, app):
        self.app = app
        self.settings = LLMSettings.load()
        self._busy = False
        self.button: Button | None = None  # "Auto-caption", set by main.py
        self.batch_button: Button | None = None  # "Caption all", set by main.py
        self._batch_running = False
        self._batch_cancel = False
        self._batch_errors_detail: list[str] = []

    # -- button commands ---------------------------------------------------

    def open_settings(self):
        _SettingsDialog(self.app.root, self.settings, self._apply_settings)

    def _apply_settings(self, settings: LLMSettings):
        self.settings = settings

    def generate_for_current(self):
        if self._busy:
            return
        if not getattr(self.app, "current_image", None):
            messagebox.showinfo("No image", "Open an image first.")
            return

        rel_path = self.app.current_image
        image_path = self.app.db._abs(rel_path)
        self._set_busy(True)
        threading.Thread(
            target=self._worker, args=(self.settings, rel_path, image_path), daemon=True
        ).start()

    # -- worker ------------------------------------------------------------

    def _worker(self, settings: LLMSettings, rel_path: str, image_path: str):
        try:
            caption = generate_caption(settings, image_path)
            self.app.root.after(0, self._on_success, rel_path, image_path, caption)
        except Exception as exc:  # surface any failure to the user
            self.app.root.after(0, self._on_error, str(exc))

    def _on_success(self, rel_path: str, image_path: str, caption: str):
        self._set_busy(False)
        # Still on the same image: show it in the editor for review/save.
        if self.app.current_image == rel_path:
            self.app.text_area.config(state=NORMAL)
            self.app.text_area.delete("1.0", END)
            self.app.text_area.insert("1.0", caption)
            return
        # User moved on: persist the caption to the original image directly,
        # so the result is never lost.
        self._persist_to_image(rel_path, image_path, caption)

    def _persist_to_image(self, rel_path: str, image_path: str, caption: str,
                          notify: bool = True) -> bool:
        """Write the caption to the original image's sidecar .txt and sync state.

        Returns True on success. With ``notify`` an info box is shown (used for
        single background generations); the batch run keeps it quiet.
        """
        app = self.app
        caption_file = os.path.splitext(image_path)[0] + ".txt"
        try:
            with open(caption_file, "w", encoding="utf-8") as f:
                f.write(caption)
        except OSError as exc:
            if notify:
                messagebox.showerror(
                    "Auto-caption save failed",
                    f"Could not write caption for {os.path.basename(image_path)}: {exc}",
                )
            return False

        # Keep the DB / list / thumbnail dot aligned with what's on disk.
        try:
            app.db.update_caption(rel_path, caption)
        except Exception:
            pass
        try:
            app.thumb_view.refresh_caption_dot(rel_path)
        except Exception:
            pass
        if app.file_list.exists(rel_path):
            vals = app.file_list.item(rel_path, "values")
            disp = vals[0] if vals else app._reldisp(rel_path)
            app.file_list.item(rel_path, values=(disp, len(caption)))
        if app._sort_state.get("col") == "len":
            app._apply_current_sort()
        # If this image happens to be the one currently open, refresh the editor.
        if app.current_image == rel_path:
            app.text_area.config(state=NORMAL)
            app.text_area.delete("1.0", END)
            app.text_area.insert("1.0", caption)

        if notify:
            messagebox.showinfo(
                "Auto-caption saved",
                f"Caption for {os.path.basename(image_path)} was generated and "
                "saved while you worked on another image.",
            )
        return True

    # -- batch: caption all images without a caption -----------------------

    def generate_for_all_missing(self):
        # If a batch is already running, this button acts as Stop.
        if self._batch_running:
            self._batch_cancel = True
            if self.batch_button is not None:
                self.batch_button.config(text="Stopping…", state=DISABLED)
            return
        if self._busy:
            return

        app = self.app
        if not app.all_image_files:
            messagebox.showinfo("No images", "Open a folder first.")
            return
        try:
            rows = app.db.get_all(show_empty=True)
            missing = {r["rel_path"] for r in rows}
        except Exception as exc:
            messagebox.showerror("Auto-caption", f"Could not query the database: {exc}")
            return
        targets = [rp for rp in app.all_image_files if rp in missing]
        if not targets:
            messagebox.showinfo("Auto-caption", "All images already have a caption.")
            return
        if not messagebox.askyesno(
            "Caption all missing",
            f"Generate captions for {len(targets)} image(s) without a caption?\n"
            "This may take a while. You can keep working; click the button again to stop.",
        ):
            return

        self._busy = True
        self._batch_running = True
        self._batch_cancel = False
        if self.button is not None:
            self.button.config(state=DISABLED)
        if self.batch_button is not None:
            self.batch_button.config(text="Stop", state=NORMAL)
        threading.Thread(
            target=self._batch_worker, args=(self.settings, targets), daemon=True
        ).start()

    def _batch_worker(self, settings: LLMSettings, targets: list[str]):
        total = len(targets)
        done = 0
        errors = 0
        for rp in targets:
            if self._batch_cancel:
                break
            image_path = self.app.db._abs(rp)
            try:
                caption = generate_caption(settings, image_path)
            except Exception as exc:
                errors += 1
                self.app.root.after(0, self._batch_note_error, os.path.basename(image_path), str(exc))
            else:
                self.app.root.after(0, self._persist_to_image, rp, image_path, caption, False)
            done += 1
            self.app.root.after(0, self._batch_progress, done, total)
        self.app.root.after(0, self._batch_done, done, total, errors, self._batch_cancel)

    def _batch_progress(self, done: int, total: int):
        # Reuse the thumbnail progress widgets; override the label wording.
        self.app._set_thumb_progress(done, total)
        if done < total:
            self.app.thumb_progress_label.config(
                text=f"Captioning {done}/{total} ({total - done} left)"
            )

    def _batch_note_error(self, name: str, message: str):
        # Keep a short rolling note in the label; full summary comes at the end.
        self._batch_errors_detail.append(f"{name}: {message}")

    def _batch_done(self, done: int, total: int, errors: int, cancelled: bool):
        self.app._set_thumb_progress(total, total)  # hide the bar/label
        self._busy = False
        self._batch_running = False
        if self.button is not None:
            self.button.config(state=NORMAL)
        if self.batch_button is not None:
            self.batch_button.config(text="Caption all", state=NORMAL)
        ok = done - errors
        summary = (
            f"{'Stopped. ' if cancelled else ''}Captioned {ok} image(s)"
            + (f", {errors} failed." if errors else ".")
        )
        if self._batch_errors_detail:
            summary += "\n\nFailures:\n" + "\n".join(self._batch_errors_detail[:10])
            if len(self._batch_errors_detail) > 10:
                summary += f"\n… and {len(self._batch_errors_detail) - 10} more."
        self._batch_errors_detail = []
        (messagebox.showwarning if errors else messagebox.showinfo)(
            "Auto-caption batch", summary
        )

    def _on_error(self, message: str):
        self._set_busy(False)
        messagebox.showerror("Auto-caption failed", message)

    def _set_busy(self, busy: bool):
        self._busy = busy
        if self.button is not None:
            self.button.config(
                state=DISABLED if busy else NORMAL,
                text="Generating…" if busy else "Auto-caption",
            )
