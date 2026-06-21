"""
Finishing Tool Installer
"""
import tkinter as tk
from tkinter import messagebox
import threading, subprocess, sys, os, shutil

ACCENT   = "#E8A838"
BG_DARK  = "#141414"
TEXT     = "#FFFFFF"
TEXT_MUTED = "#888888"
SUCCESS  = "#4CAF50"
ERROR    = "#E05555"

STEPS = [
    "Checking Python version...",
    "Installing Homebrew (if needed)...",
    "Installing Tesseract OCR...",
    "Installing Python packages...",
    "Installing the app...",
]

PIP_PACKAGES = ["pillow", "opencv-python", "pytesseract", "openpyxl", "numpy"]

class InstallerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Finishing Tool Installer")
        self.configure(bg=BG_DARK)
        self.resizable(False, False)
        self.geometry("520x600")
        self._build_ui()

    def _build_ui(self):
        # Header
        header = tk.Frame(self, bg=BG_DARK)
        header.pack(fill="x", pady=(32, 0))
        tk.Label(header, text="Finishing Tool", font=("SF Pro Display", 26, "bold"),
                 bg=BG_DARK, fg=ACCENT).pack()
        tk.Label(header, text="v1.0 Installer", font=("SF Pro Display", 13),
                 bg=BG_DARK, fg=TEXT_MUTED).pack(pady=(2, 0))

        tk.Frame(self, bg="#2a2a2a", height=1).pack(fill="x", padx=40, pady=24)

        # Steps
        self._step_icons  = []
        self._step_labels = []
        steps_frame = tk.Frame(self, bg=BG_DARK)
        steps_frame.pack(fill="x", padx=40)
        for label in STEPS:
            row = tk.Frame(steps_frame, bg=BG_DARK)
            row.pack(fill="x", pady=5)
            icon = tk.Label(row, text="○", font=("SF Pro Display", 14),
                            bg=BG_DARK, fg=TEXT_MUTED, width=2)
            icon.pack(side="left")
            lbl = tk.Label(row, text=label, font=("SF Pro Display", 12),
                           bg=BG_DARK, fg=TEXT_MUTED, anchor="w")
            lbl.pack(side="left", padx=8)
            self._step_icons.append(icon)
            self._step_labels.append(lbl)

        tk.Frame(self, bg="#2a2a2a", height=1).pack(fill="x", padx=40, pady=20)

        # Progress bar
        pb_frame = tk.Frame(self, bg=BG_DARK)
        pb_frame.pack(fill="x", padx=40)
        self._pb = tk.Canvas(pb_frame, height=8, bg="#2a2a2a", highlightthickness=0)
        self._pb.pack(fill="x")

        # Status
        self._status = tk.StringVar(value="Ready to install.")
        tk.Label(self, textvariable=self._status, font=("SF Pro Display", 11),
                 bg=BG_DARK, fg=TEXT_MUTED).pack(pady=(10, 0))

        # Log
        log_frame = tk.Frame(self, bg=BG_DARK)
        log_frame.pack(fill="both", expand=True, padx=40, pady=10)
        self._log_box = tk.Text(log_frame, font=("SF Mono", 10), bg="#111111",
                                fg=TEXT_MUTED, relief="flat", bd=0, height=5,
                                state="disabled", wrap="word")
        self._log_box.pack(fill="both", expand=True)

        # Install button
        self._install_btn = tk.Button(self, text="Install",
                                      font=("SF Pro Display", 14, "bold"),
                                      bg=ACCENT, fg="#000000",
                                      relief="flat", bd=0, padx=24, pady=10,
                                      cursor="", command=self._start_install)
        self._install_btn.pack(pady=16)

        # Launch button (hidden until done)
        self._launch_btn = tk.Button(self, text="Launch Finishing Tool",
                                     font=("SF Pro Display", 14, "bold"),
                                     bg=SUCCESS, fg="#000000",
                                     relief="flat", bd=0, padx=24, pady=10,
                                     cursor="", command=self._launch_app)

    def _log(self, msg):
        def _do():
            self._log_box.config(state="normal")
            self._log_box.insert("end", msg + "\n")
            self._log_box.see("end")
            self._log_box.config(state="disabled")
        self.after(0, _do)

    def _set_progress(self, pct):
        def _do():
            w = self._pb.winfo_width() or 440
            self._pb.delete("all")
            self._pb.create_rectangle(0, 0, int(w * pct), 8, fill=ACCENT, outline="")
        self.after(0, _do)

    def _set_step(self, i, state):
        def _do():
            if state == "active":
                self._step_icons[i].config(text="●", fg=ACCENT)
                self._step_labels[i].config(fg=TEXT)
            elif state == "done":
                self._step_icons[i].config(text="✓", fg=SUCCESS)
                self._step_labels[i].config(fg=SUCCESS)
            elif state == "error":
                self._step_icons[i].config(text="✗", fg=ERROR)
                self._step_labels[i].config(fg=ERROR)
        self.after(0, _do)

    def _set_status(self, msg):
        self.after(0, lambda: self._status.set(msg))

    def _run(self, cmd, env=None):
        e = os.environ.copy()
        if env:
            e.update(env)
        result = subprocess.run(cmd, capture_output=True, text=True, env=e)
        if result.stdout.strip(): self._log(result.stdout.strip())
        if result.stderr.strip(): self._log(result.stderr.strip())
        return result

    def _ask_admin_credentials(self):
        """Show a modal dialog asking for admin username and password."""
        import getpass
        result = {"ok": False, "user": "", "pwd": ""}
        dialog = tk.Toplevel(self)
        dialog.title("Administrator Required")
        dialog.configure(bg=BG_DARK)
        dialog.resizable(False, False)
        dialog.geometry("380x280")
        dialog.transient(self)
        dialog.grab_set()

        tk.Label(dialog, text="Administrator Required",
                 font=("SF Pro Display", 15, "bold"),
                 bg=BG_DARK, fg=TEXT).pack(pady=(24, 4))
        tk.Label(dialog, text="Homebrew requires an administrator account to install.",
                 font=("SF Pro Display", 11), bg=BG_DARK, fg=TEXT_MUTED,
                 wraplength=320).pack(pady=(0, 20))

        form = tk.Frame(dialog, bg=BG_DARK)
        form.pack(padx=32, fill="x")

        tk.Label(form, text="Username", font=("SF Pro Display", 11),
                 bg=BG_DARK, fg=TEXT_MUTED, anchor="w").pack(fill="x")
        user_var = tk.StringVar(value=getpass.getuser())
        user_entry = tk.Entry(form, textvariable=user_var,
                              font=("SF Pro Display", 12), bg="#2a2a2a",
                              fg=TEXT, relief="flat", bd=0,
                              insertbackground=TEXT)
        user_entry.pack(fill="x", ipady=6, pady=(2, 12))

        tk.Label(form, text="Password", font=("SF Pro Display", 11),
                 bg=BG_DARK, fg=TEXT_MUTED, anchor="w").pack(fill="x")
        pwd_var = tk.StringVar()
        pwd_entry = tk.Entry(form, textvariable=pwd_var, show="●",
                             font=("SF Pro Display", 12), bg="#2a2a2a",
                             fg=TEXT, relief="flat", bd=0,
                             insertbackground=TEXT)
        pwd_entry.pack(fill="x", ipady=6, pady=(2, 0))
        pwd_entry.focus()

        btn_row = tk.Frame(dialog, bg=BG_DARK)
        btn_row.pack(pady=20)

        def _cancel():
            dialog.destroy()

        def _ok():
            result["ok"] = True
            result["user"] = user_var.get().strip()
            result["pwd"] = pwd_var.get()
            dialog.destroy()

        tk.Button(btn_row, text="Cancel", font=("SF Pro Display", 12),
                  bg="#2a2a2a", fg=TEXT_MUTED, relief="flat", bd=0,
                  padx=16, pady=6, command=_cancel).pack(side="left", padx=8)
        tk.Button(btn_row, text="OK", font=("SF Pro Display", 12, "bold"),
                  bg=ACCENT, fg="#000000", relief="flat", bd=0,
                  padx=16, pady=6, command=_ok).pack(side="left", padx=8)

        pwd_entry.bind("<Return>", lambda e: _ok())
        self.wait_window(dialog)
        return (result["user"], result["pwd"]) if result["ok"] else None

    def _start_install(self):
        self._install_btn.config(state="disabled", bg="#555555", fg="#888888")
        self._set_status("Installing...")
        threading.Thread(target=self._install, daemon=True).start()

    def _install(self):
        try:
            total = len(STEPS)

            # Step 0: Python
            self._set_step(0, "active")
            v = sys.version_info
            if v.major < 3 or (v.major == 3 and v.minor < 10):
                self._set_step(0, "error")
                self._set_status("Python 3.10+ required. Install from python.org.")
                return
            self._log(f"Python {v.major}.{v.minor}.{v.micro} ✓")
            self._set_step(0, "done")
            self._set_progress(1/total)

            # Step 1: Homebrew
            self._set_step(1, "active")
            # Search common locations
            brew = (shutil.which("brew") or
                    "/opt/homebrew/bin/brew" if os.path.exists("/opt/homebrew/bin/brew") else
                    "/usr/local/bin/brew" if os.path.exists("/usr/local/bin/brew") else None)
            if not brew or not os.path.exists(brew):
                self._log("Homebrew not found — installing...")
                self._set_status("Installing Homebrew — you may be asked for your password in Terminal...")
                # Use osascript to run with admin privileges in a visible Terminal window
                script = (
                    'tell application "Terminal"\n'
                    '  activate\n'
                    '  do script "NONINTERACTIVE=1 /bin/bash -c \\"$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\\""\n'
                    'end tell'
                )
                subprocess.run(["osascript", "-e", script])
                # Wait for brew to appear
                import time
                for _ in range(120):
                    time.sleep(2)
                    brew = (shutil.which("brew") or
                            "/opt/homebrew/bin/brew" if os.path.exists("/opt/homebrew/bin/brew") else
                            "/usr/local/bin/brew" if os.path.exists("/usr/local/bin/brew") else None)
                    if brew and os.path.exists(brew):
                        break
                if not brew or not os.path.exists(brew):
                    self._set_step(1, "error")
                    self._set_status("Homebrew install failed or timed out.")
                    return
            self._log(f"Homebrew found ✓")
            self._set_step(1, "done")
            self._set_progress(2/total)

            # Step 2: Tesseract
            self._set_step(2, "active")
            tess = shutil.which("tesseract") or "/opt/homebrew/bin/tesseract"
            if not os.path.exists(tess):
                self._log("Installing tesseract...")
                env = {"PATH": "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")}
                r = self._run([brew, "install", "tesseract"], env=env)
                if r.returncode != 0:
                    self._set_step(2, "error")
                    self._set_status("Tesseract install failed.")
                    return
            else:
                self._log(f"Tesseract found ✓")
            self._set_step(2, "done")
            self._set_progress(3/total)

            # Step 3: pip packages
            self._set_step(3, "active")
            # Find real Python — not the PyInstaller bundle
            python = (shutil.which("python3") or
                      "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3" if
                      os.path.exists("/Library/Frameworks/Python.framework/Versions/3.13/bin/python3")
                      else shutil.which("python"))
            if not python:
                self._set_step(3, "error")
                self._set_status("Could not find Python. Please install from python.org.")
                return
            self._log(f"Using Python: {python}")
            for pkg in PIP_PACKAGES:
                self._log(f"Installing {pkg}...")
                r = self._run([python, "-m", "pip", "install",
                               pkg, "--break-system-packages", "-q"])
                if r.returncode != 0:
                    self._set_step(3, "error")
                    self._set_status(f"Failed to install {pkg}.")
                    return
            self._log("All packages installed ✓")
            self._set_step(3, "done")
            self._set_progress(4/total)

            # Step 4: Install app
            self._set_step(4, "active")

            # Find source files — inside PyInstaller bundle or next to script
            if getattr(sys, 'frozen', False):
                src_dir = sys._MEIPASS
            else:
                src_dir = os.path.dirname(os.path.abspath(__file__))
            self._log(f"Source dir: {src_dir}")
            self._log(f"Files there: {os.listdir(src_dir)}")

            # Install location
            app_dir = os.path.expanduser("~/Applications/FinishingTool")
            os.makedirs(app_dir, exist_ok=True)
            self._log(f"App dir: {app_dir}")

            # Copy files
            for fname in ["main.py", "thinking.gif", "icon.png"]:
                src = os.path.join(src_dir, fname)
                dst = os.path.join(app_dir, fname)
                if os.path.exists(src):
                    shutil.copy2(src, dst)
                    self._log(f"  Copied {fname} ✓")
                else:
                    self._log(f"  ⚠ {fname} not found at {src}")

            # Verify main.py copied
            main_path = os.path.join(app_dir, "main.py")
            if not os.path.exists(main_path):
                self._set_step(4, "error")
                self._set_status("Failed to copy app files.")
                return

            # Build .app bundle
            app_bundle = os.path.expanduser("~/Applications/Finishing Tool.app")
            macos_dir  = os.path.join(app_bundle, "Contents", "MacOS")
            res_dir    = os.path.join(app_bundle, "Contents", "Resources")
            os.makedirs(macos_dir, exist_ok=True)
            os.makedirs(res_dir, exist_ok=True)

            # Launcher
            launcher = os.path.join(macos_dir, "FinishingTool")
            with open(launcher, "w") as f:
                f.write("#!/bin/bash\n")
                f.write('export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"\n')
                f.write(f'cd "{app_dir}"\n')
                f.write(f'exec "{python}" "{main_path}"\n')
            os.chmod(launcher, 0o755)
            self._log(f"  Launcher: {launcher} ✓")

            # Info.plist
            plist = "\n".join([
                '<?xml version="1.0" encoding="UTF-8"?>',
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
                '<plist version="1.0"><dict>',
                '<key>CFBundleName</key><string>Finishing Tool</string>',
                '<key>CFBundleDisplayName</key><string>Finishing Tool</string>',
                '<key>CFBundleIdentifier</key><string>com.finishingtool.app</string>',
                '<key>CFBundleVersion</key><string>1.0</string>',
                '<key>CFBundleExecutable</key><string>FinishingTool</string>',
                '<key>NSHighResolutionCapable</key><true/>',
                '</dict></plist>',
            ])
            with open(os.path.join(app_bundle, "Contents", "Info.plist"), "w") as f:
                f.write(plist)

            # Icon
            icon_src = os.path.join(src_dir, "icon.png")
            if os.path.exists(icon_src):
                shutil.copy2(icon_src, os.path.join(res_dir, "icon.png"))
                iconset = os.path.join(res_dir, "icon.iconset")
                os.makedirs(iconset, exist_ok=True)
                for size in [16, 32, 64, 128, 256, 512]:
                    subprocess.run(["sips", "-z", str(size), str(size),
                                    icon_src, "--out",
                                    os.path.join(iconset, f"icon_{size}x{size}.png")],
                                   capture_output=True)
                subprocess.run(["iconutil", "-c", "icns", iconset,
                                "-o", os.path.join(res_dir, "icon.icns")],
                               capture_output=True)
                shutil.rmtree(iconset, ignore_errors=True)
                self._log("  Icon ✓")

            self._log(f"Installed: {app_bundle} ✓")
            self._set_step(4, "done")
            self._set_progress(1.0)
            self._set_status("Installation complete!")
            self.after(0, lambda b=app_bundle: self._show_done(b))

        except Exception as e:
            self._log(f"Error: {e}")
            self._set_status(f"Installation failed: {e}")

    def _show_done(self, app_bundle):
        self._app_bundle = app_bundle
        self._install_btn.pack_forget()
        self._launch_btn.pack(pady=16)

    def _launch_app(self):
        bundle = getattr(self, '_app_bundle',
                         '/Applications/Finishing Tool.app')
        self._log(f"Opening {bundle}...")
        result = subprocess.run(["open", bundle], capture_output=True, text=True)
        if result.returncode != 0:
            self._log(f"✗ Could not open: {result.stderr}")
        self.after(2000, self.destroy)


if __name__ == "__main__":
    app = InstallerApp()
    app.mainloop()
