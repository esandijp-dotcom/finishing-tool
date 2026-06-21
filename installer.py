"""
Finishing Tool Installer
Downloads source files from GitHub and builds the app locally.
"""
import tkinter as tk
import threading, subprocess, sys, os, shutil, tempfile

ACCENT       = "#E8A838"
ACCENT_HOVER = "#f0c060"
ACCENT_PRESS = "#a06010"
BG_DARK      = "#141414"
TEXT         = "#FFFFFF"
TEXT_MUTED   = "#888888"
SUCCESS      = "#4CAF50"
ERROR        = "#E05555"

GITHUB_BASE  = "https://raw.githubusercontent.com/esandijp-dotcom/finishing-tool/main"
FILES        = ["main.py", "thinking.gif", "icon.png", "version.json",
                "build_and_install.sh", "setup.py", "build_icon.py",
                "01_STRINGOUT_Render.xml",
                "02_COLORED_VFX_4444_XQ_Render.xml",
                "03_PREMIERE_XML_Render.xml"]

STEPS = [
    "Checking for Homebrew...",
    "Installing Tesseract OCR...",
    "Downloading source files...",
    "Building & installing app...",
]


def _rounded_btn(parent, text, command, bg=ACCENT, fg="#000000", width=160, height=36,
                 radius=8, hover=None, press=None):
    c = tk.Canvas(parent, width=width, height=height,
                  bg=BG_DARK, highlightthickness=0, cursor="")
    c._bg = bg
    c._text = text
    _hover = hover or ACCENT_HOVER
    _press = press or ACCENT_PRESS

    def _draw(color):
        c.delete("all")
        r = radius
        c.create_arc(0, 0, r*2, r*2, start=90, extent=90, fill=color, outline=color)
        c.create_arc(width-r*2, 0, width, r*2, start=0, extent=90, fill=color, outline=color)
        c.create_arc(0, height-r*2, r*2, height, start=180, extent=90, fill=color, outline=color)
        c.create_arc(width-r*2, height-r*2, width, height, start=270, extent=90, fill=color, outline=color)
        c.create_rectangle(r, 0, width-r, height, fill=color, outline=color)
        c.create_rectangle(0, r, width, height-r, fill=color, outline=color)
        c.create_text(width//2, height//2, text=c._text,
                      font=("SF Pro Display", 13, "bold"), fill=fg)

    c._draw = _draw
    _draw(bg)
    c.bind("<Enter>",           lambda e: _draw(_hover))
    c.bind("<Leave>",           lambda e: _draw(c._bg))
    c.bind("<ButtonPress-1>",   lambda e: _draw(_press))
    c.bind("<ButtonRelease-1>", lambda e: (_draw(_hover), command()))
    return c


class InstallerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Finishing Tool Installer")
        self.configure(bg=BG_DARK)
        self.resizable(False, False)
        self.attributes("-alpha", 0)
        self.withdraw()
        self._build_ui()
        self.update_idletasks()
        w, h = 520, 580
        x = (self.winfo_screenwidth() // 2) - (w // 2)
        y = (self.winfo_screenheight() // 2) - (h // 2)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.update_idletasks()
        self.deiconify()
        self.attributes("-alpha", 1)
        self.lift()
        self.focus_force()

    def _build_ui(self):
        tk.Label(self, text="Finishing Tool", font=("SF Pro Display", 26, "bold"),
                 bg=BG_DARK, fg=ACCENT).pack(pady=(32, 0))
        tk.Label(self, text="Installer by Juan Esandi", font=("SF Pro Display", 13),
                 bg=BG_DARK, fg=TEXT_MUTED).pack(pady=(2, 0))

        tk.Frame(self, bg="#2a2a2a", height=1).pack(fill="x", padx=40, pady=24)

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

        pb_frame = tk.Frame(self, bg=BG_DARK)
        pb_frame.pack(fill="x", padx=40)
        self._pb = tk.Canvas(pb_frame, height=8, bg="#2a2a2a", highlightthickness=0)
        self._pb.pack(fill="x")

        self._status = tk.StringVar(value="Ready to install.")
        tk.Label(self, textvariable=self._status, font=("SF Pro Display", 11),
                 bg=BG_DARK, fg=TEXT_MUTED).pack(pady=(10, 0))

        log_frame = tk.Frame(self, bg=BG_DARK, height=100)
        log_frame.pack(fill="x", padx=40, pady=10)
        log_frame.pack_propagate(False)
        self._log_box = tk.Text(log_frame, font=("SF Mono", 10), bg="#111111",
                                fg=TEXT_MUTED, relief="flat", bd=0,
                                state="disabled", wrap="word",
                                highlightthickness=0)
        self._log_box.pack(fill="both", expand=True)
        self._log_box.configure(selectbackground="#333333", selectforeground="#ffffff")

        # Single button area — Install becomes Launch when done
        self._btn_frame = tk.Frame(self, bg=BG_DARK, height=60)
        self._btn_frame.pack(fill="x", pady=12)
        self._btn_frame.pack_propagate(False)

        self._install_btn = _rounded_btn(self._btn_frame, "Install", self._start_install,
                                          width=140, height=34)
        self._install_btn.place(relx=0.5, rely=0.5, anchor="center")

        self._launch_btn = _rounded_btn(self._btn_frame, "Launch Finishing Tool",
                                         self._launch_app, bg=SUCCESS, fg="#000000",
                                         width=200, height=34,
                                         hover="#6fcf6f", press="#2d8a2d")

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

    def _start_install(self):
        self._install_btn._bg = "#555555"
        self._install_btn._draw("#555555")
        self._install_btn.unbind("<Enter>")
        self._install_btn.unbind("<Leave>")
        self._install_btn.unbind("<ButtonPress-1>")
        self._install_btn.unbind("<ButtonRelease-1>")
        self._set_status("Installing...")
        threading.Thread(target=self._install, daemon=True).start()

    def _install(self):
        try:
            total = len(STEPS)
            env = os.environ.copy()
            env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + env.get("PATH", "")

            # Step 0: Homebrew
            self._set_step(0, "active")
            self._set_status("Checking Homebrew...")
            brew = (shutil.which("brew") or
                    "/opt/homebrew/bin/brew" if os.path.exists("/opt/homebrew/bin/brew") else
                    "/usr/local/bin/brew" if os.path.exists("/usr/local/bin/brew") else None)
            if not brew or not os.path.exists(brew):
                self._log("Homebrew not found — installing...")
                self._set_status("Installing Homebrew...")
                import getpass
                default_user = getpass.getuser()
                user_script = (
                    f'set uname to text returned of (display dialog '
                    f'"Finishing Tool needs admin access to install Homebrew.\\nAdmin username:" '
                    f'default answer "{default_user}" with title "Finishing Tool Installer" '
                    f'buttons {{"Cancel","Next"}} default button "Next")'
                )
                user_result = subprocess.run(["osascript", "-e", user_script],
                                             capture_output=True, text=True)
                if user_result.returncode != 0:
                    self._set_step(0, "error")
                    self._set_status("Cancelled.")
                    return
                username = user_result.stdout.strip()
                pw_script = (
                    f'set pwd to text returned of (display dialog '
                    f'"Password for \\"{username}\\":" '
                    f'default answer "" with hidden answer '
                    f'with title "Finishing Tool Installer" '
                    f'buttons {{"Cancel","Install"}} default button "Install")'
                )
                pw_result = subprocess.run(["osascript", "-e", pw_script],
                                           capture_output=True, text=True)
                if pw_result.returncode != 0:
                    self._set_step(0, "error")
                    self._set_status("Cancelled.")
                    return
                password = pw_result.stdout.strip()
                install_cmd = ('NONINTERACTIVE=1 /bin/bash -c '
                               '"$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"')
                proc = subprocess.Popen(
                    ["sudo", "-S", "-u", username, "/bin/bash", "-c", install_cmd],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True)
                proc.stdin.write(password + "\n")
                proc.stdin.flush()
                proc.stdin.close()
                for line in proc.stdout:
                    self._log(line.rstrip())
                proc.wait()
                brew = ("/opt/homebrew/bin/brew" if os.path.exists("/opt/homebrew/bin/brew")
                        else "/usr/local/bin/brew" if os.path.exists("/usr/local/bin/brew")
                        else shutil.which("brew"))
                if not brew:
                    self._set_step(0, "error")
                    self._set_status("Homebrew install failed.")
                    return
            else:
                self._log("Homebrew found ✓")
            self._set_step(0, "done")
            self._set_progress(1/total)

            # Step 1: Tesseract
            self._set_step(1, "active")
            self._set_status("Checking Tesseract...")
            tess = shutil.which("tesseract") or "/opt/homebrew/bin/tesseract"
            if not os.path.exists(tess):
                self._log("Installing Tesseract OCR...")
                r = subprocess.run([brew, "install", "tesseract"],
                                   capture_output=True, text=True, env=env)
                if r.returncode != 0:
                    self._set_step(1, "error")
                    self._set_status("Tesseract install failed.")
                    return
            else:
                self._log("Tesseract found ✓")
            self._set_step(1, "done")
            self._set_progress(2/total)

            # Step 2: Download all source files from GitHub
            self._set_step(2, "active")
            self._set_status("Downloading source files...")
            import urllib.request, ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            REQUIRED_FILES = ["main.py", "thinking.gif", "icon.png", "version.json",
                              "build_and_install.sh", "setup.py", "build_icon.py"]
            PRESET_FILES   = ["01_STRINGOUT Render.xml",
                              "02_COLORED VFX 4444 XQ Render.xml",
                              "03_PREMIERE XML Render.xml"]

            build_dir = os.path.expanduser("~/Applications/FinishingToolBuild")
            os.makedirs(build_dir, exist_ok=True)

            # Download required files — fail hard if any missing
            for fname in REQUIRED_FILES:
                url = f"{GITHUB_BASE}/{fname}"
                dst = os.path.join(build_dir, fname)
                self._log(f"  Downloading {fname}...")
                try:
                    req = urllib.request.urlopen(url, context=ctx, timeout=30)
                    with open(dst, "wb") as f:
                        f.write(req.read())
                except Exception as e:
                    self._set_step(2, "error")
                    self._set_status(f"Download failed: {fname}")
                    self._log(f"  ✗ {e}")
                    return

            # Download preset files — warn but continue if missing
            for fname in PRESET_FILES:
                url = f"{GITHUB_BASE}/{fname.replace(' ', '%20')}"
                dst = os.path.join(build_dir, fname)
                self._log(f"  Downloading {fname}...")
                try:
                    req = urllib.request.urlopen(url, context=ctx, timeout=30)
                    with open(dst, "wb") as f:
                        f.write(req.read())
                except Exception as e:
                    self._log(f"  ⚠ Preset not found, skipping: {fname}")
            self._log("All files downloaded ✓")

            # Install DaVinci Resolve render presets
            preset_dst_dir = os.path.expanduser(
                "~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Presets/Render")
            os.makedirs(preset_dst_dir, exist_ok=True)
            for preset_name in ["01_STRINGOUT Render.xml",
                                 "02_COLORED VFX 4444 XQ Render.xml",
                                 "03_PREMIERE XML Render.xml"]:
                preset_src = os.path.join(build_dir, preset_name)
                preset_dst = os.path.join(preset_dst_dir, preset_name)
                try:
                    shutil.copy2(preset_src, preset_dst)
                    self._log(f"  {preset_name} ✓")
                except Exception as e:
                    self._log(f"  ⚠ Could not install {preset_name}: {e}")
            self._log("Render presets installed ✓")

            self._set_step(2, "done")
            self._set_progress(3/total)

            # Step 3: Run build_and_install.sh
            self._set_step(3, "active")
            self._set_status("Building app — this may take a few minutes...")
            self._log("Running build script...")

            build_script = os.path.join(build_dir, "build_and_install.sh")
            os.chmod(build_script, 0o755)

            build_env = env.copy()
            build_env["PATH"] = (
                "/Library/Frameworks/Python.framework/Versions/3.13/bin:"
                "/opt/homebrew/bin:/usr/local/bin:" + build_env.get("PATH", "")
            )
            proc = subprocess.Popen(
                ["/bin/bash", "-u", build_script],
                cwd=build_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=build_env
            )
            for line in iter(proc.stdout.readline, ""):
                stripped = line.rstrip()
                if stripped:
                    self._log(stripped)
            proc.stdout.close()
            proc.wait()

            if proc.returncode != 0:
                self._set_step(3, "error")
                self._set_status(f"Build failed (exit code {proc.returncode}) — check log.")
                return

            app_bundle = "/Applications/Finishing Tool.app"
            if not os.path.exists(app_bundle):
                self._set_step(3, "error")
                self._set_status("App not found after build.")
                return

            self._log("Finishing Tool installed ✓")
            self._set_step(3, "done")
            self._set_progress(1.0)
            self._set_status("Installation complete!")
            self.after(0, self._show_done)

        except Exception as e:
            self._log(f"Error: {e}")
            self._set_status(f"Installation failed: {e}")

    def _show_done(self):
        self._install_btn.place_forget()
        self._launch_btn.place(relx=0.5, rely=0.5, anchor="center")

    def _launch_app(self):
        subprocess.run(["open", "/Applications/Finishing Tool.app"], capture_output=True)
        self.after(2000, self.destroy)


if __name__ == "__main__":
    app = InstallerApp()
    app.mainloop()
