import pytest
import tkinter as tk
from unittest.mock import MagicMock, patch

from thumb_view import ThumbnailView, _short_name, _dot_color, DOT_HAS, DOT_NONE

_root = None


@pytest.fixture(scope="module")
def tk_root():
    global _root
    _root = tk.Tk()
    _root.withdraw()
    yield _root
    _root.destroy()


@pytest.fixture
def view(tk_root):
    """ThumbnailView with a mocked DB and a real (hidden) Tk parent."""
    db = MagicMock()
    db.get_visible_rows_bulk.return_value = {}
    with patch.object(ThumbnailView, "_sync_visible"), \
         patch.object(ThumbnailView, "_recompute_layout"), \
         patch.object(ThumbnailView, "_schedule_poll"):
        tv = ThumbnailView(tk_root, db)
    yield tv
    try:
        tv._worker.stop()
        tv.frame.destroy()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_short_name_short_enough():
    assert _short_name("abc.png", 200) == "abc.png"


def test_short_name_truncates():
    name = "a" * 100 + ".png"
    result = _short_name(name, 70)
    assert result.endswith("…")
    assert len(result) < len(name)


def test_dot_color():
    assert _dot_color(1) == DOT_HAS
    assert _dot_color(0) == DOT_NONE


# ---------------------------------------------------------------------------
# Data-state mutations (set_images / set_current / remove / rename)
# ---------------------------------------------------------------------------

def test_set_images_replaces_file_list(view):
    files = ["a.png", "b.png", "c.png"]
    view.set_images(files, current_index=1)
    assert view._files == files
    assert view._current_idx == 1


def test_set_images_clamps_index(view):
    view.set_images(["a.png"], current_index=99)
    assert view._current_idx == 0


def test_set_current_updates_index(view):
    view.set_images(["a.png", "b.png", "c.png"])
    with patch.object(view, "_scroll_cell_into_view"), \
         patch.object(view, "_sync_visible"):
        view.set_current(2)
    assert view._current_idx == 2


def test_remove_shifts_selection_and_list(view):
    view.set_images(["a.png", "b.png", "c.png"], current_index=2)
    with patch.object(view, "_recompute_layout"), \
         patch.object(view, "_sync_visible"), \
         patch.object(view, "_scroll_cell_into_view"):
        view.remove("a.png")
    assert "a.png" not in view._files
    assert view._files == ["b.png", "c.png"]
    assert view._current_idx == 1  # was 2, shifted down by removal before it


def test_remove_unknown_path_is_noop(view):
    view.set_images(["a.png", "b.png"])
    view.remove("nonexistent.png")
    assert view._files == ["a.png", "b.png"]


def test_rename_updates_files_list(view):
    view.set_images(["a.png", "b.png"])
    view.rename("a.png", "renamed.png")
    assert view._files[0] == "renamed.png"
    assert "a.png" not in view._files
