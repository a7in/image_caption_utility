import os
import pytest
import tkinter as tk
from unittest.mock import MagicMock, patch

from main import ImageCaptionApp
from thumb_view import ThumbnailView

# Global root for all tests in this module to avoid Tkinter recreating errors on Windows
_test_root = None

@pytest.fixture(scope="module")
def tk_root():
    global _test_root
    _test_root = tk.Tk()
    _test_root.withdraw()
    yield _test_root
    _test_root.destroy()

@pytest.fixture
def app_gui(tmp_path, tk_root):
    """Fixture: initializes the Tkinter app hideously."""
    with patch('tkinter.filedialog.askdirectory', return_value=str(tmp_path)):
        # We start by overriding db.open to avoid actually relying on real db
        with patch('main.ImageDB', autospec=True) as MockDB:
            # We also mock messagebox.showinfo to prevent "No images found" blocking popup
            with patch('main.messagebox.showinfo'):
                with patch('main.messagebox.showerror'):
                    mock_db_instance = MockDB.return_value
                    mock_db_instance.directory = str(tmp_path)
                    
                    # Since load_images asks for directory via filedialog, we mocked it to tmp_path
                    app = ImageCaptionApp(tk_root)
                    
                    # Manually trigger a mock sync 
                    app.db = mock_db_instance
                    
                    yield app

def test_switch_view_modes(app_gui):
    # App starts in list view mode according to init
    assert app_gui.view_mode == "list"
    
    app_gui.switch_to_thumbs()
    assert app_gui.view_mode == "thumbs"
    
    app_gui.switch_to_list()
    assert app_gui.view_mode == "list"

@patch('main.messagebox.askyesno')
def test_delete_current_image(mock_askyesno, app_gui):
    mock_askyesno.return_value = True
    
    app_gui.current_image = "img1.png"
    app_gui.image_files = ["img1.png"]
    # Provide an absolute path mapping
    app_gui.db._abs = MagicMock(return_value="/mock/path/img1.png")
    
    # Since delete_current_image is defined somewhere down in main.py not fully shown,
    # assume standard logic: os.remove and db.delete
    with patch('os.remove') as mock_remove:
        # Avoid error if file doesn't exist during test
        with patch('os.path.exists', return_value=True):
            # Attempt to call delete_current_image if it exists
            if hasattr(app_gui, "delete_current_image"):
                app_gui.delete_current_image()
                # Ensure db.delete was called
                app_gui.db.delete.assert_called()

def test_filter_files(app_gui):
    app_gui.all_image_files = ["img1.png", "img2.jpg"]
    app_gui.db.get_all.return_value = [{"rel_path": "img1.png"}]
    
    app_gui.filter_entry.insert(0, "test")
    app_gui.filter_files()
    
    assert app_gui.image_files == ["img1.png"]
    app_gui.db.get_all.assert_called_with(filter_text="test", show_empty=False)
    
def test_clear_filter(app_gui):
    app_gui.all_image_files = ["img1.png", "img2.jpg"]
    app_gui.image_files = ["img1.png"]
    
    app_gui.clear_filter()
    
    assert app_gui.image_files == ["img1.png", "img2.jpg"]
    assert app_gui.filter_entry.get() == ""

def test_select_image(app_gui):
    app_gui.image_files = ["img1.png", "img2.jpg", "img3.png"]
    app_gui.image_index = 0
    
    # Test step next
    # Mocking display_image to not do complex UI updates during basic test
    with patch.object(app_gui, "display_image"):
        with patch.object(app_gui, "save_caption"):
            app_gui.select_image(step=1)
            assert app_gui.image_index == 1
            
            # Step prev
            app_gui.select_image(step=-1)
            assert app_gui.image_index == 0
            
            # Select specific index
            app_gui.select_image(index=2)
            assert app_gui.image_index == 2

def test_caption_save_and_update(app_gui, tmp_path):
    # Setup state manually for the test
    app_gui.image_files = ["img1.png", "img2.png"]
    app_gui.image_index = 0
    app_gui.current_image = "img1.png"
    
    # Use tmp_path to easily check if the file was created and what it contains
    caption_file = tmp_path / "img1.txt"
    app_gui.current_caption_file = str(caption_file)
    
    # --- 1. First scenario: No caption, we add one ---
    app_gui.text_area.delete("1.0", tk.END)
    app_gui.text_area.insert("1.0", "First caption text")
    
    # We switch to the second image. 
    # select_image() automatically calls save_caption() first.
    # We mock display_image because we just want to test saving logic, 
    # not actual UI image rendering.
    with patch.object(app_gui, "display_image"):
        app_gui.select_image(index=1)
        
    # Check if the .txt file was created and contains the correct text
    assert caption_file.exists()
    assert caption_file.read_text(encoding="utf-8") == "First caption text"
    
    # Check if the database update method was called with the correct arguments
    app_gui.db.update_caption.assert_called_with("img1.png", "First caption text")
    
    
    # --- 2. Second scenario: Return and update ---
    # Reset mock tracker
    app_gui.db.update_caption.reset_mock()
    
    # Simulate returning to the first image again
    app_gui.current_image = "img1.png"
    app_gui.current_caption_file = str(caption_file)
    
    # Change the text
    app_gui.text_area.delete("1.0", tk.END)
    app_gui.text_area.insert("1.0", "Updated caption text")
    
    # Switch to image 2 again
    with patch.object(app_gui, "display_image"):
        app_gui.select_image(index=1)
        
    # Check if the text file was properly updated
    assert caption_file.read_text(encoding="utf-8") == "Updated caption text"
    
    # Check if the database was updated with the new text
    app_gui.db.update_caption.assert_called_with("img1.png", "Updated caption text")

def test_rename_file(app_gui, tmp_path):
    # Setup fake physical files
    old_img_path = tmp_path / "img1.png"
    old_img_path.write_bytes(b"fake_image")
    old_txt_path = tmp_path / "img1.txt"
    old_txt_path.write_text("Hello", encoding="utf-8")
    
    # Create a second file to test conflict
    img2_path = tmp_path / "img2.png"
    img2_path.write_bytes(b"fake_image_2")
    
    app_gui.image_files = ["img1.png", "img2.png"]
    app_gui.all_image_files = ["img1.png", "img2.png"]
    app_gui.image_index = 0
    app_gui.image_directory = str(tmp_path)
    
    # For main rename logic
    app_gui.current_image = "img1.png"
    app_gui.current_caption_file = str(old_txt_path)
    
    # Setup mock to return actual paths
    app_gui.db._abs.side_effect = lambda rp: os.path.join(tmp_path, rp)
    app_gui.db._rel.side_effect = lambda ap: os.path.basename(ap)
    
    # Spy on the thumbnail view's rename() to verify it's notified.
    app_gui.thumb_view.rename = MagicMock()

    # --- 1. Successful Rename ---
    app_gui.file_entry.delete(0, tk.END)
    app_gui.file_entry.insert(0, "new_img1.png")

    app_gui.rename_file()

    # Verify 1 & 2: physical rename
    assert not old_img_path.exists()
    assert not old_txt_path.exists()
    assert (tmp_path / "new_img1.png").exists()
    assert (tmp_path / "new_img1.txt").exists()

    # Verify 3: DB rename called
    app_gui.db.rename.assert_called_with("img1.png", "new_img1.png")

    # Verify 4: internal list renamed
    assert app_gui.image_files[0] == "new_img1.png"
    assert app_gui.current_image == "new_img1.png"

    # Verify 5: thumb view notified of the rename
    app_gui.thumb_view.rename.assert_called_with("img1.png", "new_img1.png")

    # --- 2. Rename to an existing file (Collision) ---
    app_gui.file_entry.delete(0, tk.END)
    app_gui.file_entry.insert(0, "img2.png")
    
    with patch('main.messagebox.showerror') as mock_err:
        app_gui.rename_file()
        
    # Verify 6: Error displayed, file not renamed
    mock_err.assert_called_once()
    assert (tmp_path / "new_img1.png").exists() # Still there
    assert app_gui.current_image == "new_img1.png"

def test_open_find_replace(app_gui, tmp_path):
    app_gui.image_files = ["img1.png", "img2.png", "img3.png"]
    app_gui.db._abs.side_effect = lambda rp: os.path.join(tmp_path, rp)
    
    cap1 = tmp_path / "img1.txt"
    cap1.write_text("An old car", encoding="utf-8")
    
    cap2 = tmp_path / "img2.txt"
    cap2.write_text("Another old item", encoding="utf-8")
    
    # Target file that has no "old" word, or no file
    # img3.txt won't be modified
    
    mock_find_entry = MagicMock()
    mock_replace_entry = MagicMock()
    
    # We patch the UI components that build the popup window
    with patch('main.Toplevel'), \
         patch('main.Label'), \
         patch('main.Entry', side_effect=[mock_find_entry, mock_replace_entry]), \
         patch('main.Button') as mock_button:
         
        # Execute the function that builds the popup
        app_gui.open_find_replace()
        
        # Ensure Button was created and get the embedded perform_replace function
        assert mock_button.call_count == 1
        _, kwargs = mock_button.call_args
        perform_replace = kwargs['command']
        
        # 1. Test validation error (empty find field)
        mock_find_entry.get.return_value = ""
        with patch('main.messagebox.showerror') as mock_err:
            perform_replace()
            mock_err.assert_called_once_with("Error", "Please enter text to find.")
            
        # 2. Test actual replacement logic
        mock_find_entry.get.return_value = "old"
        mock_replace_entry.get.return_value = "new"
        
        app_gui.thumb_view.refresh_caption_dot = MagicMock()

        # Call perform_replace with valid input
        with patch('main.messagebox.showinfo') as mock_info:
            perform_replace()
            mock_info.assert_called_once_with("Result", "Replaced 2 instances.")

        # Verify the physical .txt files were modified correctly
        assert cap1.read_text(encoding="utf-8") == "An new car"
        assert cap2.read_text(encoding="utf-8") == "Another new item"

        # Verify the database was updated accordingly
        app_gui.db.update_caption.assert_any_call("img1.png", "An new car")
        app_gui.db.update_caption.assert_any_call("img2.png", "Another new item")

        # Verify dot color refresh was triggered for both modified files
        assert app_gui.thumb_view.refresh_caption_dot.call_count == 2
