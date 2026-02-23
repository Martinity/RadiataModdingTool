### this GUI is ai generated


import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import os
import threading
from pathlib import Path

from RadiataCompressionTool import (
    MODE_DISPLAY, COMPRESSION_MODES,
    start_compression, start_decompression
)

def launch_gui():
    root = tk.Tk()
    root.title("RADIATA STORIES - SLZ/SLE Compression Tool")
    root.geometry("980x740")
    root.minsize(860, 620)
    root.configure(bg="#C0C0C0")
    root.resizable(True, True)

    # Classic Windows 98/2000 look
    style = ttk.Style(root)
    try:
        style.theme_use('classic')
    except:
        style.theme_use('default')

    style.configure(".", background="#C0C0C0", foreground="#000000", font=("Courier New", 10))
    style.configure("TNotebook", background="#C0C0C0")
    style.configure("TNotebook.Tab", padding=[12, 5], font=("Courier New", 10, "bold"))
    style.map("TNotebook.Tab", background=[("selected", "#A0A0A0")])

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True, padx=8, pady=8)

    # Retro header function
    def create_retro_header(parent, text):
        header = tk.Label(parent, text=text, font=("Courier New", 14, "bold"),
                          bg="#000080", fg="#FFFFFF", relief="raised", bd=3, pady=8)
        header.pack(fill="x", pady=(0, 10))
    
    # ====================== RETRO STATS POPUP ======================
    def show_completion_dialog(title: str, content: str):
        dialog = tk.Toplevel(root)
        dialog.title(title)
        dialog.geometry("860x520")
        dialog.configure(bg="#C0C0C0")
        dialog.resizable(False, False)
        dialog.transient(root)          # stay on top of main window
        dialog.grab_set()               # modal

        # Retro header
        tk.Label(dialog, text=title, font=("Courier New", 14, "bold"),
                 bg="#000080", fg="#FFFFFF", relief="raised", bd=3, pady=8).pack(fill="x")

        # Scrollable stats
        frame = tk.Frame(dialog, bg="#C0C0C0")
        frame.pack(fill="both", expand=True, padx=12, pady=12)

        text = tk.Text(frame, bg="#001100", fg="#00FF80", font=("Courier New", 10),
                       relief="sunken", bd=3, wrap="none")
        text.pack(side="left", fill="both", expand=True)

        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        scroll.pack(side="right", fill="y")
        text.config(yscrollcommand=scroll.set)

        text.insert("1.0", content)
        text.config(state="disabled")   # read-only

        # OK button
        tk.Button(dialog, text="OK", command=dialog.destroy, relief="raised", bd=3,
                  bg="#C0C0C0", activebackground="#A0A0A0", font=("Courier New", 10, "bold"),
                  width=12, height=1).pack(pady=12)
        
    # ====================== COMPRESS TAB ======================
    compress_tab = ttk.Frame(notebook)
    notebook.add(compress_tab, text=" COMPRESS ")

    create_retro_header(compress_tab, "   RADIATA COMPRESSION TOOL v1.0   ")

    top_content = tk.Frame(compress_tab, bg="#C0C0C0")
    top_content.pack(fill="both", expand=True)

    ttk.Label(top_content, text="Input Raw Files:", font=("Courier New", 10, "bold"), background="#C0C0C0").pack(anchor="w", padx=12, pady=(5, 2))

    files_frame = ttk.Frame(top_content)
    files_frame.pack(fill="both", expand=True, padx=12, pady=2)

    input_listbox = tk.Listbox(files_frame, selectmode="extended", height=6,   # ← reduced from 8
                               bg="#FFFFFF", fg="#000000", font=("Courier New", 10), relief="sunken", bd=3)
    input_listbox.pack(side="left", fill="both", expand=True)

    scrollbar = ttk.Scrollbar(files_frame, orient="vertical", command=input_listbox.yview)
    scrollbar.pack(side="right", fill="y")
    input_listbox.config(yscrollcommand=scrollbar.set)

    btn_frame = tk.Frame(top_content, bg="#C0C0C0")
    btn_frame.pack(fill="x", padx=12, pady=6)

    def add_input_files():
        files = filedialog.askopenfilenames(title="Select Raw Files to Compress")
        for f in files:
            if f not in input_listbox.get(0, "end"):
                input_listbox.insert("end", f)

    def remove_input_files():
        for i in reversed(input_listbox.curselection()):
            input_listbox.delete(i)

    tk.Button(btn_frame, text="➕ Add Files", command=add_input_files, relief="raised", bd=3,
              bg="#C0C0C0", activebackground="#A0A0A0", font=("Courier New", 10)).pack(side="left", padx=4)
    tk.Button(btn_frame, text="🗑 Remove Selected", command=remove_input_files, relief="raised", bd=3,
              bg="#C0C0C0", activebackground="#A0A0A0", font=("Courier New", 10)).pack(side="left", padx=4)
    tk.Button(btn_frame, text="Clear All", command=lambda: input_listbox.delete(0, "end"), relief="raised", bd=3,
              bg="#C0C0C0", activebackground="#A0A0A0", font=("Courier New", 10)).pack(side="left", padx=4)

    ttk.Label(top_content, text="Compression Mode (for all files):", font=("Courier New", 10, "bold"), background="#C0C0C0").pack(anchor="w", padx=12, pady=(10, 2))
    mode_var = tk.StringVar(value=MODE_DISPLAY[3])
    mode_combo = ttk.Combobox(top_content, textvariable=mode_var, values=list(MODE_DISPLAY.values()),
                              state="readonly", font=("Courier New", 10), width=50)
    mode_combo.pack(fill="x", padx=12)

    ttk.Label(top_content, text="Output Options:", font=("Courier New", 10, "bold"), background="#C0C0C0").pack(anchor="w", padx=12, pady=(12, 5))

    chain_var = tk.BooleanVar(value=False)
    bank_var = tk.BooleanVar(value=False)

    def on_chain_toggle():
        if chain_var.get(): bank_var.set(False)
        output_path_var.set("")
    def on_bank_toggle():
        if bank_var.get(): chain_var.set(False)
        output_path_var.set("")

    tk.Checkbutton(top_content, text="Chained archive", variable=chain_var, command=on_chain_toggle,
                   bg="#C0C0C0", font=("Courier New", 10)).pack(anchor="w", padx=20, pady=3)
    tk.Checkbutton(top_content, text="Bank archive (sector-aligned)", variable=bank_var, command=on_bank_toggle,
                   bg="#C0C0C0", font=("Courier New", 10)).pack(anchor="w", padx=20, pady=3)

    sle_var = tk.BooleanVar(value=False)
    tk.Checkbutton(top_content, text="Encrypt as .SLE", variable=sle_var,
                   bg="#C0C0C0", font=("Courier New", 10)).pack(anchor="w", padx=20, pady=3)

    output_frame = tk.Frame(top_content, bg="#C0C0C0")
    output_frame.pack(fill="x", padx=12, pady=8)

    output_path_var = tk.StringVar()
    tk.Label(output_frame, text="Output Path:", bg="#C0C0C0", font=("Courier New", 10)).pack(side="left")
    tk.Entry(output_frame, textvariable=output_path_var, font=("Courier New", 10), width=65, relief="sunken", bd=3).pack(side="left", padx=8, fill="x", expand=True)

    def browse_output():
        if chain_var.get() or bank_var.get():
            ext = ".sle" if sle_var.get() else ".slz"
            label = "Save Bank Archive" if bank_var.get() else "Save Chained Archive"
            f = filedialog.asksaveasfilename(title=label, defaultextension=ext)
            if f: output_path_var.set(f)
        else:
            d = filedialog.askdirectory(title="Select Output Folder")
            if d: output_path_var.set(d)

    tk.Button(output_frame, text="Browse...", command=browse_output, relief="raised", bd=3,
              bg="#C0C0C0", activebackground="#A0A0A0", font=("Courier New", 10)).pack(side="left", padx=5)

    # Live Progress
    ttk.Label(top_content, text="Live Progress:", font=("Courier New", 10, "bold"), background="#C0C0C0").pack(anchor="w", padx=12, pady=(8,2))
    current_status = tk.StringVar(value="Idle - Ready to compress")
    tk.Label(top_content, textvariable=current_status, font=("Courier New", 10), bg="#C0C0C0", fg="#006400",
             relief="sunken", bd=2, anchor="w", padx=8).pack(fill="x", padx=12, pady=2)
    
    progress_bar = ttk.Progressbar(top_content, length=720, mode='determinate')
    progress_bar.pack(fill="x", padx=12, pady=4)

    # ====================== RUN COMPRESSION ======================
    _comp_progress = {"percent": 0, "msg": "", "done": False, "error": None, "log": []}

    def run_compression():
        files = list(input_listbox.get(0, "end"))
        if not files:
            messagebox.showerror("Error", "Add at least one input file")
            return
        out_path = output_path_var.get().strip()
        if not out_path:
            messagebox.showerror("Error", "Choose an output path")
            return

        try:
            mode_name = mode_var.get()
            mode = next(k for k, v in MODE_DISPLAY.items() if v == mode_name)
        except StopIteration:
            messagebox.showerror("Error", "Invalid mode")
            return

        modes = [mode] * len(files)
        chain = chain_var.get()
        bank = bank_var.get()

        if chain or bank:
            output_paths = [out_path]
        else:
            ext = ".sle" if sle_var.get() else ".slz"
            output_paths = [os.path.join(out_path, Path(f).stem + ext) for f in files]

        progress_bar.configure(value=0)
        current_status.set("Starting compression...")
        compress_btn.configure(state="disabled")

        _comp_progress["percent"] = 0
        _comp_progress["msg"] = "Starting compression..."
        _comp_progress["done"] = False
        _comp_progress["error"] = None
        _comp_progress["log"] = []

        def gui_log(text):
            _comp_progress["log"].append(text)
            print(text)

        def gui_progress(percent, msg):
            _comp_progress["percent"] = percent
            _comp_progress["msg"] = msg[:85]

        def thread_target():
            try:
                start_compression(files, modes, output_paths, chain, bank=bank,
                                  log_func=gui_log, progress_callback=gui_progress)
                _comp_progress["percent"] = 100
                _comp_progress["msg"] = "All done!"
                _comp_progress["done"] = True
            except Exception as e:
                _comp_progress["error"] = str(e)
                _comp_progress["done"] = True

        def poll_progress():
            progress_bar.configure(value=_comp_progress["percent"])
            current_status.set(_comp_progress["msg"])
            if _comp_progress["done"]:
                compress_btn.configure(state="normal")
                if _comp_progress["error"]:
                    current_status.set("ERROR: " + _comp_progress["error"][:60])
                    messagebox.showerror("Error", _comp_progress["error"])
                else:
                    full_log = "\n".join(_comp_progress["log"])
                    show_completion_dialog("COMPRESSION COMPLETE", full_log)
            else:
                root.after(100, poll_progress)

        threading.Thread(target=thread_target, daemon=True).start()
        root.after(100, poll_progress)

    # Big button always at bottom
    button_frame = tk.Frame(compress_tab, bg="#C0C0C0")
    button_frame.pack(side="bottom", fill="x", padx=12, pady=12)

    compress_btn = tk.Button(button_frame, text="START COMPRESSION", command=run_compression,
              relief="raised", bd=5, bg="#C0C0C0", activebackground="#000080",
              font=("Courier New", 12, "bold"), height=2)
    compress_btn.pack(fill="x")
        
    # ====================== DECOMPRESS TAB ======================
    decompress_tab = ttk.Frame(notebook)
    notebook.add(decompress_tab, text=" DECOMPRESS ")

    create_retro_header(decompress_tab, "   RADIATA DECOMPRESSION TOOL v1.0   ")

    ttk.Label(decompress_tab, text="Input SLZ / SLE Files:", font=("Courier New", 10, "bold"), background="#C0C0C0").pack(anchor="w", padx=12, pady=(5, 2))

    decomp_listbox = tk.Listbox(decompress_tab, selectmode="extended", height=10,
                                bg="#FFFFFF", fg="#000000", font=("Courier New", 10), relief="sunken", bd=3)
    decomp_listbox.pack(fill="both", expand=True, padx=12, pady=5)

    d_btn_frame = tk.Frame(decompress_tab, bg="#C0C0C0")
    d_btn_frame.pack(fill="x", padx=12, pady=6)

    def add_decomp_files():
        files = filedialog.askopenfilenames(title="Select SLZ/SLE Files", filetypes=[("SLZ/SLE", "*.slz *.sle"), ("All", "*.*")])
        for f in files:
            if f not in decomp_listbox.get(0, "end"):
                decomp_listbox.insert("end", f)

    tk.Button(d_btn_frame, text="➕ Add Files", command=add_decomp_files, relief="raised", bd=3,
              bg="#C0C0C0", font=("Courier New", 10)).pack(side="left", padx=4)
    tk.Button(d_btn_frame, text="🗑 Remove Selected", command=lambda: [decomp_listbox.delete(i) for i in reversed(decomp_listbox.curselection())],
              relief="raised", bd=3, bg="#C0C0C0", font=("Courier New", 10)).pack(side="left", padx=4)
    tk.Button(d_btn_frame, text="Clear All", command=lambda: decomp_listbox.delete(0, "end"), relief="raised", bd=3,
              bg="#C0C0C0", font=("Courier New", 10)).pack(side="left", padx=4)

    ttk.Label(decompress_tab, text="Output Directory:", font=("Courier New", 10, "bold"), background="#C0C0C0").pack(anchor="w", padx=12, pady=(12, 2))

    decomp_out_var = tk.StringVar()
    decomp_out_frame = tk.Frame(decompress_tab, bg="#C0C0C0")
    decomp_out_frame.pack(fill="x", padx=12)

    tk.Entry(decomp_out_frame, textvariable=decomp_out_var, font=("Courier New", 10), relief="sunken", bd=3).pack(side="left", fill="x", expand=True, padx=(0, 8))
    tk.Button(decomp_out_frame, text="Browse Folder", command=lambda: decomp_out_var.set(filedialog.askdirectory() or decomp_out_var.get()),
              relief="raised", bd=3, bg="#C0C0C0", font=("Courier New", 10)).pack(side="left")

    # Live Progress
    ttk.Label(decompress_tab, text="Live Progress:", font=("Courier New", 10, "bold"), background="#C0C0C0").pack(anchor="w", padx=12, pady=(8,2))
    decomp_status = tk.StringVar(value="Idle - Ready to decompress")
    tk.Label(decompress_tab, textvariable=decomp_status, font=("Courier New", 10), bg="#C0C0C0", fg="#006400",
             relief="sunken", bd=2, anchor="w", padx=8).pack(fill="x", padx=12, pady=2)

    decomp_progress = ttk.Progressbar(decompress_tab, length=720, mode='determinate')
    decomp_progress.pack(fill="x", padx=12, pady=4)

    # ──────── DECOMPRESSION RUN FUNCTION ────────
    _decomp_progress = {"percent": 0, "msg": "", "done": False, "error": None, "log": []}

    def run_decompression():
        files = list(decomp_listbox.get(0, "end"))
        if not files:
            messagebox.showerror("Error", "Add at least one SLZ/SLE file")
            return
        out_dir = decomp_out_var.get().strip()
        if not out_dir:
            messagebox.showerror("Error", "Select an output directory")
            return

        decomp_progress.configure(value=0)
        decomp_status.set("Starting decompression...")
        decomp_btn.configure(state="disabled")

        _decomp_progress["percent"] = 0
        _decomp_progress["msg"] = "Starting decompression..."
        _decomp_progress["done"] = False
        _decomp_progress["error"] = None
        _decomp_progress["log"] = []

        def gui_log(text):
            _decomp_progress["log"].append(text)
            print(text)

        def gui_progress(percent, msg):
            _decomp_progress["percent"] = percent
            _decomp_progress["msg"] = msg[:85]

        def thread_target():
            try:
                start_decompression(files, out_dir, log_func=gui_log, progress_callback=gui_progress)
                _decomp_progress["percent"] = 100
                _decomp_progress["msg"] = "All done!"
                _decomp_progress["done"] = True
            except Exception as e:
                _decomp_progress["error"] = str(e)
                _decomp_progress["done"] = True

        def poll_progress():
            decomp_progress.configure(value=_decomp_progress["percent"])
            decomp_status.set(_decomp_progress["msg"])
            if _decomp_progress["done"]:
                decomp_btn.configure(state="normal")
                if _decomp_progress["error"]:
                    decomp_status.set("ERROR: " + _decomp_progress["error"][:60])
                    messagebox.showerror("Error", _decomp_progress["error"])
                else:
                    full_log = "\n".join(_decomp_progress["log"])
                    show_completion_dialog("DECOMPRESSION COMPLETE", full_log)
            else:
                root.after(100, poll_progress)

        threading.Thread(target=thread_target, daemon=True).start()
        root.after(100, poll_progress)

    decomp_btn_frame = tk.Frame(decompress_tab, bg="#C0C0C0")
    decomp_btn_frame.pack(side="bottom", fill="x", padx=12, pady=12)

    decomp_btn = tk.Button(decomp_btn_frame, text="START DECOMPRESSION", command=run_decompression, relief="raised", bd=5,
              bg="#C0C0C0", activebackground="#00FF00", font=("Courier New", 12, "bold"), height=2)
    decomp_btn.pack(fill="x")

    root.mainloop()

if __name__ == "__main__":
    launch_gui()
