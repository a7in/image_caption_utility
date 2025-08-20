import os
from tkinter import *
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
        self.root.title("Image Caption Utility")

        b_frame = Frame(self.root)
        b_frame.pack(fill=X)

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

        main_frame = Frame(self.root)
        main_frame.pack(fill=X)

        img_frame = Frame(main_frame)
        img_frame.pack(side=LEFT, padx=2, pady=2)

        img_ctrl_frame = Frame(img_frame)
        img_ctrl_frame.pack(side=TOP, fill=X, padx=2, pady=2)

        self.prev_button = Button(img_ctrl_frame, text="Prev", command=lambda: self.select_image(-1))
        self.prev_button.pack(side=LEFT, padx=2, pady=2)

        self.next_button = Button(img_ctrl_frame, text="Next", command=lambda: self.select_image(1))
        self.next_button.pack(side=LEFT, padx=2, pady=2)

        self.index_label = Label(img_ctrl_frame, text="", fg="blue")
        self.index_label.pack(side=LEFT, padx=2, pady=2)

        self.file_entry = Entry(img_ctrl_frame, width=80)
        self.file_entry.pack(side=LEFT, padx=2, pady=2)
        self.file_entry.bind("<Return>", self.rename_file)
        self.file_entry.bind("<FocusOut>", self.rename_file)

        self.image_label = Label(img_frame, width=768, height=768)
        self.image_label.pack(side=TOP, fill=X, padx=2, pady=2)
        self.image_label.bind("<Double-1>", self.open_image)

        text_frame = Frame(main_frame)
        text_frame.pack(side=LEFT, fill=Y)

        self.text_area = Text(text_frame, wrap=WORD, width=40)
        self.text_area.pack(side=TOP, fill=Y, padx=2, pady=2)

        trans_frame = Frame(text_frame)
        trans_frame.pack(side=TOP, fill=X)

        self.trans_button = Button(trans_frame, text="Translate and add -^ from:", command=self.translate_text)
        self.trans_button.pack(side=LEFT, padx=2, pady=2)

        self.text_lang = Text(trans_frame, width=2, height=1)
        self.text_lang.pack(side=LEFT, padx=2, pady=2)
        self.text_lang.insert(END, 'ru')

        self.trans_text_area = Text(text_frame, wrap=WORD, width=40)
        self.trans_text_area.pack(side=TOP, fill=Y, padx=2, pady=2)

        self.file_list = Listbox(main_frame)
        self.file_list.pack(side=LEFT, fill=BOTH, expand=True, padx=2, pady=2)
        self.file_list.bind('<<ListboxSelect>>', self.on_file_select)

        # Создаем Scrollbar и связываем его с Listbox
        self.scrollbar = Scrollbar(main_frame, orient=VERTICAL, command=self.file_list.yview)
        self.scrollbar.pack(side=RIGHT, fill=Y)
        self.file_list.config(yscrollcommand=self.scrollbar.set)

        self.image_index = 0
        self.image_files = []
        self.current_image = None
        self.current_caption_file = None

        self.load_images()
        self.display_image()

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

        try:
            os.rename(old_image, new_image)
            if os.path.exists(old_txt):
                os.rename(old_txt, new_txt)
        except Exception as e:
            messagebox.showerror("Rename error", f"Rename error:\n{e}")
            # откатываем текст в Entry
            self.file_entry.delete(0, END)
            self.file_entry.insert(0, old_base)
            return

        # обновляем ссылки
        self.current_image = new_image
        self.current_caption_file = new_txt

        # также обновим список файлов
        self.image_files[self.image_index] = new_image
        self.file_list.delete(self.image_index)
        self.file_list.insert(self.image_index, new_image)

        self.file_entry.delete(0, END)
        self.file_entry.insert(0, os.path.basename(new_image))

        
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

    def display_image(self):
        if self.image_files:
            self.index_label.config(text=f"{self.image_index + 1} of {len(self.image_files)}")            
            image_path = os.path.join(self.image_directory, self.image_files[self.image_index])
            self.current_caption_file = os.path.splitext(image_path)[0] + '.txt'

            image = Image.open(image_path)
            image.thumbnail((400, 400))
            photo = ImageTk.PhotoImage(image)
            self.image_label.config(image=photo)
            self.image_label.image = photo

            # показываем имя файла в Entry
            self.file_entry.delete(0, END)
            self.file_entry.insert(0, os.path.basename(image_path))

            self.current_image = image_path
        
            self.load_caption()

            # Highlight the current file in file_list
            self.file_list.selection_clear(0, END)
            self.file_list.selection_set(self.image_index)
            self.file_list.see(self.image_index)  # Scroll to the active file
    
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
