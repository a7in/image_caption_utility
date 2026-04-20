import os
import pytest
import sqlite3
from PIL import Image
import io
import queue

from db import ImageDB, ThumbWorker, THUMB_SIZE

@pytest.fixture
def temp_image_dir(tmp_path):
    """Fixture: creates a temporary directory with fake 'images' and captions."""
    # Create fake image files
    img1_path = tmp_path / "img1.png"
    # Create a valid minimal PNG or JPEG to avoid Pillow opening errors if possible, 
    # but for sync tests, just empty file is enough. For _generate test we need a real image.
    valid_img = Image.new('RGB', (10, 10), color = 'red')
    valid_img.save(img1_path, format="PNG")
    
    txt1_path = tmp_path / "img1.txt"
    txt1_path.write_text("Hello World", encoding="utf-8")
    
    img2_path = tmp_path / "img2.jpg"
    valid_img.save(img2_path, format="JPEG")
    # img2 has no text file
    
    return tmp_path

@pytest.fixture
def test_db(temp_image_dir):
    """Fixture: initializes the database in the temporary directory."""
    db = ImageDB()
    db.open(str(temp_image_dir))
    yield db
    db.close()

def test_db_open_and_close(temp_image_dir):
    db = ImageDB()
    assert db._conn is None
    db.open(str(temp_image_dir))
    assert db._conn is not None
    assert db.directory == str(temp_image_dir)
    db.close()
    assert db._conn is None

def test_sync_adds_new_images(test_db, temp_image_dir):
    abs_paths = [str(temp_image_dir / "img1.png"), str(temp_image_dir / "img2.jpg")]
    
    synced_rels = test_db.sync(abs_paths)
    
    assert len(synced_rels) == 2
    assert "img1.png" in synced_rels
    assert "img2.jpg" in synced_rels

def test_sync_removes_stale_images(test_db, temp_image_dir):
    abs_paths = [str(temp_image_dir / "img1.png"), str(temp_image_dir / "img2.jpg")]
    test_db.sync(abs_paths)
    
    # Keep only one image
    abs_paths_updated = [str(temp_image_dir / "img1.png")]
    synced_rels = test_db.sync(abs_paths_updated)
    
    assert len(synced_rels) == 1
    assert "img1.png" in synced_rels
    
    # Verify in DB
    rows = test_db.get_all()
    assert len(rows) == 1
    assert rows[0]['rel_path'] == "img1.png"

def test_get_all_filtering(test_db, temp_image_dir):
    abs_paths = [str(temp_image_dir / "img1.png"), str(temp_image_dir / "img2.jpg")]
    test_db.sync(abs_paths)
    
    # Note: img1.png has "Hello World", img2.jpg has no text
    
    # Filter by text
    results = test_db.get_all(filter_text="Hello")
    assert len(results) == 1
    assert results[0]['rel_path'] == "img1.png"
    
    # Filter by show_empty (show images without caption)
    results_empty = test_db.get_all(show_empty=True)
    assert len(results_empty) == 1
    assert results_empty[0]['rel_path'] == "img2.jpg"

def test_get_by_rel(test_db, temp_image_dir):
    abs_paths = [str(temp_image_dir / "img1.png")]
    test_db.sync(abs_paths)
    
    row = test_db.get_by_rel("img1.png")
    assert row is not None
    assert row['rel_path'] == "img1.png"
    assert row['has_caption'] == 1
    assert row['caption_text'] == "Hello World"
    
    row_none = test_db.get_by_rel("nonexistent.png")
    assert row_none is None

def test_update_caption(test_db, temp_image_dir):
    abs_paths = [str(temp_image_dir / "img1.png")]
    test_db.sync(abs_paths)
    
    test_db.update_caption("img1.png", "New Caption")
    
    row = test_db.get_by_rel("img1.png")
    assert row['caption_text'] == "New Caption"
    assert row['has_caption'] == 1
    
    # Empty caption
    test_db.update_caption("img1.png", "   ")
    row_empty = test_db.get_by_rel("img1.png")
    assert row_empty['has_caption'] == 0

def test_rename(test_db, temp_image_dir):
    abs_paths = [str(temp_image_dir / "img1.png")]
    test_db.sync(abs_paths)
    
    test_db.rename("img1.png", "img1_renamed.png")
    
    assert test_db.get_by_rel("img1.png") is None
    assert test_db.get_by_rel("img1_renamed.png") is not None

def test_delete(test_db, temp_image_dir):
    abs_paths = [str(temp_image_dir / "img1.png")]
    test_db.sync(abs_paths)
    
    test_db.delete("img1.png")
    assert test_db.get_by_rel("img1.png") is None

def test_thumb_management(test_db, temp_image_dir):
    abs_paths = [str(temp_image_dir / "img1.png")]
    test_db.sync(abs_paths)
    
    pending = test_db.get_pending_thumbs()
    assert "img1.png" in pending
    
    fake_thumb = b"fake_jpeg_data"
    test_db.set_thumb("img1.png", fake_thumb)
    
    pending_after = test_db.get_pending_thumbs()
    assert "img1.png" not in pending_after
    
    thumb = test_db.get_thumb("img1.png")
    assert thumb == fake_thumb
    
    test_db.invalidate_thumb("img1.png")
    assert test_db.get_thumb("img1.png") is None

def test_thumb_worker(test_db, temp_image_dir):
    # Prepare one file
    abs_paths = [str(temp_image_dir / "img1.png")]
    test_db.sync(abs_paths)

    q = queue.Queue()
    worker = ThumbWorker(test_db, q)
    worker.start()
    try:
        worker.request(["img1.png"])

        # Wait for the thumb message (long-running worker pushes it asynchronously).
        deadline_msgs = []
        for _ in range(5):
            msg = q.get(timeout=5)
            deadline_msgs.append(msg)
            if msg[0] == "thumb":
                break
        thumb_msgs = [m for m in deadline_msgs if m[0] == "thumb"]
        assert thumb_msgs, f"no thumb message received, got: {deadline_msgs}"

        msg = thumb_msgs[0]
        assert msg[1] == "img1.png"
        assert isinstance(msg[2], bytes)
    finally:
        worker.stop()

    # The DB should now have the thumb
    assert test_db.get_thumb("img1.png") is not None


def test_thumb_worker_start_stop(test_db):
    q = queue.Queue()
    worker = ThumbWorker(test_db, q)
    worker.start()
    assert worker.is_running()
    worker.stop()
    assert worker._stop_event.is_set()
    assert not worker.is_running()


def test_thumb_worker_cancel(test_db, temp_image_dir):
    abs_paths = [str(temp_image_dir / "img1.png"), str(temp_image_dir / "img2.jpg")]
    test_db.sync(abs_paths)

    q = queue.Queue()
    worker = ThumbWorker(test_db, q)
    # Don't start the thread — just manipulate pending queue to verify API.
    worker.request(["img1.png", "img2.jpg"])
    assert worker.pending_count() == 2
    worker.cancel("img1.png")
    assert worker.pending_count() == 1
    worker.cancel("nonexistent")  # no-op
    assert worker.pending_count() == 1


def test_get_thumbs_bulk(test_db, temp_image_dir):
    abs_paths = [str(temp_image_dir / "img1.png"), str(temp_image_dir / "img2.jpg")]
    test_db.sync(abs_paths)

    test_db.set_thumb("img1.png", b"thumb1")

    result = test_db.get_thumbs_bulk(["img1.png", "img2.jpg", "missing.png"])
    assert result["img1.png"] == b"thumb1"
    assert result["img2.jpg"] is None
    assert result["missing.png"] is None

    assert test_db.get_thumbs_bulk([]) == {}


def test_get_visible_rows_bulk(test_db, temp_image_dir):
    abs_paths = [str(temp_image_dir / "img1.png"), str(temp_image_dir / "img2.jpg")]
    test_db.sync(abs_paths)
    test_db.set_thumb("img1.png", b"thumb1")

    rows = test_db.get_visible_rows_bulk(["img1.png", "img2.jpg", "missing.png"])
    thumb1, has1 = rows["img1.png"]
    thumb2, has2 = rows["img2.jpg"]
    thumbm, hasm = rows["missing.png"]

    assert thumb1 == b"thumb1"
    assert has1 == 1  # img1 has caption "Hello World"
    assert thumb2 is None
    assert has2 == 0  # img2 has no caption
    assert thumbm is None
    assert hasm == 0
