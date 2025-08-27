import os
from tkinter import *
from tkinter import ttk
from tkinter import filedialog, messagebox
from idlelib.tooltip import Hovertip # for tooltips
from PIL import Image, ImageTk
import subprocess
import platform
from googletrans import Translator

translator = Translator()

class ImageCaptionApp:
    def __init__(self, root):
        self.root = root
        self.root.title("a7in image Caption Utility")
        
        container = Frame(self.root)
        container.pack(fill=BOTH, expand=True)

        # buttons frame (top)
        b_frame = Frame(container)
        b_frame.grid(row=0, column=0, sticky="ew")

        self.open_folder_button = Button(b_frame, text="Reopen folder", command=self.open_folder)
        self.open_folder_button.pack(side=LEFT, padx=2, pady=2)

        self.search_empty_button = Button(b_frame, text="Search empty", command=self.search_empty_caption)
        self.search_empty_button.pack(side=LEFT, padx=2, pady=2)
        
        self.find_replace_button = Button(b_frame, text="Find-Replace", command=self.open_find_replace)
        self.find_replace_button.pack(side=LEFT, padx=2, pady=2)        

        self.save_button = Button(b_frame, text="Save", command=self.save_caption)
        Hovertip(self.save_button, text="Save current edited prompt")
        self.save_button.pack(side=LEFT, padx=2, pady=2)

        self.cancel_button = Button(b_frame, text="Cancel", command=self.load_caption)
        Hovertip(self.cancel_button, text="Return to original prompt")
        self.cancel_button.pack(side=LEFT, padx=2, pady=2)

        main_frame = Frame(container)
        main_frame.grid(row=1, column=0, sticky="nsew")
        
        container.grid_rowconfigure(1, weight=1)
        container.grid_columnconfigure(0, weight=1)
        
        main_frame.grid_rowconfigure(0, weight=1)
        main_frame.grid_columnconfigure(0, weight=1, uniform="half")
        main_frame.grid_columnconfigure(1, weight=1, uniform="half")

        text_frame_height = root.winfo_screenheight() / 4
        # --- img_frame (левая часть, 50%) ---
        img_frame = Frame(main_frame)
        img_frame.grid(row=0, column=0, sticky="nsew")
        # Разделяем на 3 ряда: controls (фиксированный), image (70%), text (30%)
        img_frame.grid_rowconfigure(0, weight=0)  # controls, natural height
        img_frame.grid_rowconfigure(1, weight=7, minsize=200)  # image
        img_frame.grid_rowconfigure(2, weight=0, minsize=text_frame_height)  # text
        img_frame.grid_columnconfigure(0, weight=1)

        # --- nav_frame (правая часть, 50%) ---
        nav_frame = Frame(main_frame)
        nav_frame.grid(row=0, column=1, sticky="nsew")
        nav_frame.grid_rowconfigure(0, weight=1)
        nav_frame.grid_columnconfigure(0, weight=1)
        nav_frame.grid_columnconfigure(1, weight=0)

        # --- img_frame ---
        img_ctrl_frame = Frame(img_frame)
        img_ctrl_frame.grid(row=0, column=0, sticky="ew", padx=2, pady=2)

        self.prev_button = Button(img_ctrl_frame, text="Prev", command=lambda: self.select_image(-1))
        self.prev_button.pack(side=LEFT, padx=2, pady=2)

        self.next_button = Button(img_ctrl_frame, text="Next", command=lambda: self.select_image(1))
        self.next_button.pack(side=LEFT, padx=2, pady=2)

        self.index_label = Label(img_ctrl_frame, text="", fg="blue")
        self.index_label.pack(side=LEFT, padx=2, pady=2)

        self.dir_entry = ttk.Combobox(img_ctrl_frame, state="readonly", width=32)
        self.dir_entry.pack(side=LEFT, padx=2, pady=2)
        self.dir_entry.bind("<<ComboboxSelected>>", self.on_dir_change)

        self.file_entry = Entry(img_ctrl_frame)
        self.file_entry.pack(side=LEFT, fill=X, expand=True, padx=2, pady=2)
        self.file_entry.bind("<Return>", self.rename_file)

        # Image label без фиксированного размера
        self.image_label = Label(img_frame)
        self.image_label.grid(row=1, column=0, sticky="nsew", padx=2, pady=2)
        self.image_label.bind("<Configure>", self.resize_image)
        self.image_label.bind("<Double-Button-1>", self.open_image)

        # --- text_frame (bottom 1/4 of screen height) ---
        text_frame = Frame(img_frame, height=text_frame_height)
        text_frame.grid(row=2, column=0, sticky="nsew")
        text_frame.grid_rowconfigure(0, weight=1)
        text_frame.grid_propagate(False)
        text_frame.grid_columnconfigure(0, weight=1, uniform="txt")
        text_frame.grid_columnconfigure(1, weight=1, uniform="txt")

        self.text_area = Text(text_frame, wrap=WORD, width=1)
        self.text_area.grid(row=0, column=0, sticky="nsew", padx=2, pady=2)

        trans_frame = Frame(text_frame)
        trans_frame.grid(row=0, column=1, sticky="nsew", padx=2, pady=2)
        trans_frame.grid_rowconfigure(2, weight=1)
        trans_frame.grid_columnconfigure(0, weight=1)

        self.trans_button = Button(trans_frame, text="Translate and add -^ from:", command=self.translate_text)
        self.trans_button.grid(row=0, column=0, sticky="w", padx=2, pady=2)

        self.text_lang = Text(trans_frame, width=2, height=1)
        self.text_lang.grid(row=1, column=0, sticky="w", padx=2, pady=2)
        self.text_lang.insert(END, 'ru')

        self.trans_text_area = Text(trans_frame, wrap=WORD, width=1)
        self.trans_text_area.grid(row=2, column=0, sticky="nsew", padx=2, pady=2)

        # --- nav_frame ---
        self.file_list = Listbox(nav_frame)
        self.file_list.grid(row=0, column=0, sticky="nsew", padx=(2,0), pady=2)
        self.file_list.bind('<<ListboxSelect>>', self.on_file_select)

        self.scrollbar = Scrollbar(nav_frame, orient=VERTICAL, command=self.file_list.yview, width=18)
        self.scrollbar.grid(row=0, column=1, sticky="ns", padx=(0,2), pady=2)
        self.file_list.config(yscrollcommand=self.scrollbar.set)

        self.image_index = 0
        self.image_files = []
        self.current_image = None
        self.current_caption_file = None
        self.original_image = None
        self.photo = None

        self.load_images()
        self.display_image()
        root.state('zoomed') # maximize window

        root.after(200, root.focus_force) # fix focus issue

    def on_dir_change(self, event=None):
        if not self.current_image:
            return
        rel_dir = self.dir_entry.get()
        if rel_dir == "\\":
            new_dir = self.image_directory
        else:
            new_dir = os.path.join(self.image_directory, rel_dir)
        if not os.path.isdir(new_dir):
            return

        old_image = self.current_image
        old_txt = self.current_caption_file
        base_name = os.path.basename(old_image)
        name_no_ext, _ = os.path.splitext(base_name)

        # check name conflict
        for f in os.listdir(new_dir):
            f_name, f_ext = os.path.splitext(f)
            if f_name.lower() == name_no_ext.lower() and f.lower().endswith(('.png', '.jpg', '.jpeg')):
                messagebox.showerror(
                    "Move error",
                    f"In dst dir filename already exists '{f_name}' (file {f}). "
                    "Please rename first."
                )
                # restore
                old_dir = os.path.dirname(old_image)
                rel_old = os.path.relpath(old_dir, self.image_directory)
                if rel_old == ".":
                    rel_old = "\\"
                self.dir_entry.set(rel_old)
                return        

        new_image = os.path.join(new_dir, base_name)
        new_txt = os.path.splitext(new_image)[0] + ".txt"

        try:
            os.rename(old_image, new_image)
            if os.path.exists(old_txt):
                os.rename(old_txt, new_txt)
        except Exception as e:
            messagebox.showerror("Move error", f"Could not move:\n{e}")
            self.dir_entry.set(os.path.dirname(old_image))  # restore
            return

        # обновляем пути
        self.current_image = new_image
        self.current_caption_file = new_txt
        self.image_files[self.image_index] = new_image

        # обновляем в списке файлов
        self.file_list.delete(self.image_index)
        self.file_list.insert(self.image_index, new_image)

        # оставить фокус на строке
        self.file_list.selection_clear(0, END)
        self.file_list.selection_set(self.image_index)
        self.file_list.see(self.image_index)

        # обновить input
        self.file_entry.delete(0, END)
        self.file_entry.insert(0, os.path.basename(new_image))

    def resize_image(self, event=None):
        if not self.original_image:
            return
        # Use event dimensions if available, otherwise use current label size
        if event:
            label_width = event.width - 4
            label_height = event.height - 4
        else:
            label_width = self.image_label.winfo_width() - 4
            label_height = self.image_label.winfo_height() - 4

        if label_width <= 0 or label_height <= 0:
            return
        # Scale image with aspect ratio
        orig_width, orig_height = self.original_image.size
        ratio = min(label_width / orig_width, label_height / orig_height)
        new_width = int(orig_width * ratio)
        new_height = int(orig_height * ratio)
        resized = self.original_image.resize((new_width, new_height), Image.LANCZOS)
        self.photo = ImageTk.PhotoImage(resized)
        self.image_label.config(image=self.photo)
        self.image_label.image = self.photo  # Keep a reference to prevent garbage collection

    def display_image(self):
        if self.image_files:
            self.index_label.config(text=f"{self.image_index + 1} of {len(self.image_files)}")            
            image_path = os.path.join(self.image_directory, self.image_files[self.image_index])
            self.current_caption_file = os.path.splitext(image_path)[0] + '.txt'

            try:
                self.original_image = Image.open(image_path)
                # Call resize_image directly to update the display immediately
                self.resize_image()  # No event, uses current label size
            except Exception as e:
                messagebox.showerror("Error", f"Cannot open image: {e}")
                return

            # Show file name in Entry
            self.file_entry.delete(0, END)
            self.file_entry.insert(0, os.path.basename(image_path))

            self.current_image = image_path
        
            self.load_caption()

            # Highlight the current file in file_list
            self.file_list.selection_clear(0, END)
            self.file_list.selection_set(self.image_index)
            self.file_list.see(self.image_index)  # Scroll to the active file
            
            current_dir = os.path.dirname(self.current_image)
            rel_dir = os.path.relpath(current_dir, self.image_directory)
            if rel_dir == ".":
                rel_dir = "\\"
            self.dir_entry.set(rel_dir)


    def rename_file(self, event=None):
        if not self.current_image:
            return

        old_image = self.current_image
        old_txt = self.current_caption_file

        directory = os.path.dirname(old_image)
        old_base = os.path.basename(old_image)
        new_base = self.file_entry.get().strip()

        if not new_base:
            self.file_entry.delete(0, END)
            self.file_entry.insert(0, old_base)
            return

        # ext
        if not os.path.splitext(new_base)[1]:
            new_base += os.path.splitext(old_base)[1]

        new_image = os.path.join(directory, new_base)
        new_txt = os.path.splitext(new_image)[0] + ".txt"

        # not changed
        if new_image == old_image:
            return

        # check
        name_no_ext, _ = os.path.splitext(new_base)
        for f in os.listdir(directory):
            f_name, f_ext = os.path.splitext(f)
            if f_name.lower() == name_no_ext.lower() and f.lower().endswith(('.png', '.jpg', '.jpeg')):
                # если это не сам файл, который мы сейчас переименовываем
                if os.path.join(directory, f) != old_image:
                    messagebox.showerror(
                        "Rename error",
                        f"Filename with same name exists '{f_name}' (file {f}). "
                        "Enter another name."
                    )
                    # restore old name
                    self.file_entry.delete(0, END)
                    self.file_entry.insert(0, old_base)
                    return            

        try:
            os.rename(old_image, new_image)
            if os.path.exists(old_txt):
                os.rename(old_txt, new_txt)
        except Exception as e:
            messagebox.showerror("Rename error", f"Rename error:\n{e}")
            # restore old name
            self.file_entry.delete(0, END)
            self.file_entry.insert(0, old_base)
            return

        self.current_image = new_image
        self.current_caption_file = new_txt

        self.image_files[self.image_index] = new_image
        self.file_list.delete(self.image_index)
        self.file_list.insert(self.image_index, new_image)

        self.file_entry.delete(0, END)
        self.file_entry.insert(0, os.path.basename(new_image))
        # line selection
        self.file_list.selection_clear(0, END)
        self.file_list.selection_set(self.image_index)
        self.file_list.see(self.image_index)
        self.file_entry.focus_set()

        
    def open_find_replace(self):
        def perform_replace():
            find_text = find_entry.get()
            replace_text = replace_entry.get()
            if not find_text:
                messagebox.showerror("Error", "Please enter text to find.")
                return

            find_replace_button.config(state=DISABLED)
            count = 0

            for i, image_path in enumerate(self.image_files):
                caption_file = os.path.splitext(image_path)[0] + '.txt'
                if os.path.exists(caption_file):
                    with open(caption_file, 'r', encoding='utf-8') as f:
                        content = f.read()
                    new_content = content.replace(find_text, replace_text)
                    if content != new_content:
                        count += content.count(find_text)
                        with open(caption_file, 'w', encoding='utf-8') as f:
                            f.write(new_content)

            messagebox.showinfo("Done", f"Replaced: {count}")

        find_replace_window = Toplevel(self.root)
        find_replace_window.title("Find and Replace")

        Label(find_replace_window, text="Find:").grid(row=0, column=0, padx=5, pady=5)
        find_entry = Entry(find_replace_window, width=30)
        find_entry.grid(row=0, column=1, padx=5, pady=5)

        Label(find_replace_window, text="Replace:").grid(row=1, column=0, padx=5, pady=5)
        replace_entry = Entry(find_replace_window, width=30)
        replace_entry.grid(row=1, column=1, padx=5, pady=5)

        find_replace_button = Button(find_replace_window, text="Replace", command=perform_replace)
        find_replace_button.grid(row=2, column=0, columnspan=2, pady=10)

    def translate_text(self):
        if not self.text_area.get(1.0, END).rstrip().endswith(','):
            self.text_area.insert(END, ',')
        s = self.trans_text_area.get(1.0, END).strip()
        t = ' ' + translator.translate(s, src='ru', dest='en').text.strip()
        self.text_area.insert(END, t)

    def open_folder(self):
        self.load_images()
        self.image_index = 0
        self.display_image()

    def load_images(self):
        directory = filedialog.askdirectory(title="Select Image Directory")
        if directory:
            self.image_files = []
            # Recursively walk through the directory and subdirectories
            for root, _, files in os.walk(directory):
                for file in files:
                    if file.lower().endswith(('.png', '.jpg', '.jpeg')):
                        self.image_files.append(os.path.join(root, file))
            self.image_files.sort()
            self.image_directory = directory
            
            self.directories = []
            for root, dirs, _ in os.walk(self.image_directory):
                rel_path = os.path.relpath(root, self.image_directory)
                if rel_path == ".":
                    rel_path = "\\"
                self.directories.append(rel_path)
            self.directories.sort()
            self.dir_entry["values"] = self.directories          

            # Populate file_list with the image files
            self.file_list.delete(0, END)
            for file in self.image_files:
                self.file_list.insert(END, file)            

        if not self.image_files:
            messagebox.showinfo("No Images", "No images found in the selected directory.")
            self.root.quit()

    # load caption from current_caption_file
    def load_caption(self):
        if os.path.exists(self.current_caption_file):
            with open(self.current_caption_file, 'r', encoding='utf-8') as f:
                caption = f.read()
        else:
            caption = ""
            with open(self.current_caption_file, 'w', encoding='utf-8') as f:
                f.write(caption)

        self.text_area.config(state=NORMAL)
        self.text_area.delete(1.0, END)
        self.text_area.insert(END, caption)
    
    def save_caption(self):
        if self.current_caption_file:
            caption = self.text_area.get(1.0, END).strip()
            with open(self.current_caption_file, 'w', encoding='utf-8') as f:
                f.write(caption)

    # step or index
    def select_image(self, step=0, index=None):
        self.save_caption()
        if index != None:
            self.image_index = index
        else:    
            self.image_index = (self.image_index + step) % len(self.image_files)
        self.display_image()

    def search_empty_caption(self):
        start_index = self.image_index
        while True:
            self.image_index = (self.image_index + 1) % len(self.image_files)
            self.display_image()
            if self.text_area.get(1.0, END).strip() == "":
                return  # found
            if self.image_index == start_index:
                messagebox.showinfo("Not found", "No image with an empty or missing caption found.")
                break        

    def open_image(self, event):
        if self.current_image:
            try:
                if platform.system() == "Windows":
                    os.startfile(self.current_image)
                elif platform.system() == "Darwin":  # macOS
                    subprocess.call(("open", self.current_image))
                else:  # Linux and others
                    subprocess.call(("xdg-open", self.current_image))
            except Exception as e:
                messagebox.showerror("Error", f"Could not open image: {e}")

    def on_file_select(self, event):
        try:
            selection = event.widget.curselection()
            if selection:
                index = selection[0]
                if index != self.image_index:  # Avoid reloading if it's already selected
                    self.select_image(index=index)
        except IndexError:
            pass  # Handle empty selection if needed                

# workaround for ctrl+c ctrl+v on other locals
def keypress(e):
    if e.keycode == 86 and e.keysym != 'v' and e.char != 'м':
        e.widget.event_generate("<<Paste>>")
    elif e.keycode == 67 and e.keysym != 'c' and e.char != 'с':
        e.widget.event_generate("<<Copy>>")

if __name__ == "__main__":
    root = Tk()
    root.bind_all("<KeyPress>", keypress)
    app = ImageCaptionApp(root)
    root.mainloop()
