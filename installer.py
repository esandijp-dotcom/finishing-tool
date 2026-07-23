"""
Finishing Tool Installer
Downloads source files from GitHub and builds the app locally.
"""
import tkinter as tk
import threading, subprocess, sys, os, shutil, tempfile, time

ACCENT       = "#E8A838"
ACCENT_HOVER = "#f0c060"
ACCENT_PRESS = "#a06010"
BG_DARK      = "#141414"
TEXT         = "#FFFFFF"
TEXT_MUTED   = "#888888"
SUCCESS      = "#4CAF50"
ERROR        = "#E05555"

GITHUB_BASE  = "https://raw.githubusercontent.com/esandijp-dotcom/finishing-tool/main"
FILES        = ["main.py", "thinking.gif", "icon.png", "bug_icon.png", "version.json",
                "build_and_install.sh", "setup.py", "build_icon.py",
                "pymiere_link.zxp"]
# Bundled into the app by setup.py's DATA_FILES — must exist in the build
# dir before `setup.py py2app` runs, or the build fails outright.
AME_PRESET_FILES = ["LIVE.epr", "MARKETING.epr", "SOCIAL MEDIA.epr", "LIVE WITH SRTs.epr"]
# Installed straight into DaVinci Resolve's own preset folder — main.py
# calls LoadRenderPreset("02_COLORED VFX 4444 XQ") by name and expects
# Resolve to already know about it.
RENDER_PRESET_FILES = ["01_STRINGOUT Render.xml", "02_COLORED VFX 4444 XQ Render.xml",
                        "03_PREMIERE XML Render.xml"]

STEPS = [
    "Checking for Homebrew...",
    "Installing Tesseract OCR...",
    "Downloading files & installing presets...",
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
            env["PATH"] = ("/Library/Frameworks/Python.framework/Versions/3.13/bin:"
                           "/opt/homebrew/bin:/usr/local/bin:" + env.get("PATH", ""))
            # This app is normally launched by double-clicking it in Finder,
            # not from Terminal — GUI-launched apps get a much sparser
            # environment from launchd than a login shell does, and USER/
            # LOGNAME are often simply absent from it (they're populated by
            # shell startup files, not by the OS itself). Homebrew's own
            # installer checks $USER against the admin group's member list
            # to decide whether to proceed — if $USER is empty, that check
            # can never match, and Homebrew wrongly reports the account
            # isn't an administrator even when it genuinely is. Derive the
            # username from the process's real UID instead (a syscall via
            # pwd, independent of any environment variable) and force it
            # into the env every subprocess call below inherits.
            import pwd
            current_user = pwd.getpwuid(os.getuid()).pw_name
            env["USER"] = current_user
            env["LOGNAME"] = current_user

            # Pre-flight: Python 3.13, Xcode CLT, Homebrew — all three
            # installed sequentially inside ONE bash script (not as
            # separate Python-spawned subprocess calls each doing their
            # own "sudo -A"). Earlier versions authenticated separately
            # per step (or tried to share one authorization across
            # separately-spawned subprocess calls via a background
            # keepalive) and still re-prompted — relying on macOS's sudo
            # ticket cache to persist ACROSS separate process invocations
            # with no controlling terminal turned out to be unreliable.
            # A single continuous script making sequential `sudo -A`
            # calls is unambiguous: that's the same process re-invoking
            # sudo, not separate ones hoping to share a cached ticket.
            python = "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3"
            need_python = not os.path.exists(python)
            xcode_check = subprocess.run(["xcode-select", "-p"], capture_output=True,
                                         text=True, env=env)
            need_clt = xcode_check.returncode != 0
            brew = ("/opt/homebrew/bin/brew" if os.path.exists("/opt/homebrew/bin/brew")
                    else "/usr/local/bin/brew" if os.path.exists("/usr/local/bin/brew")
                    else shutil.which("brew"))
            need_brew = not brew or not os.path.exists(brew)

            if need_python or need_clt or need_brew:
                self._set_status("Checking system requirements...")
                askpass_script = os.path.join(tempfile.gettempdir(), "ft_sudo_askpass.sh")
                with open(askpass_script, "w") as f:
                    f.write(
                        "#!/bin/bash\n"
                        "osascript -e 'display dialog "
                        "\"Finishing Tool needs your password to continue "
                        "installing:\" default answer \"\" with hidden answer "
                        "with title \"Finishing Tool Installer\" buttons {\"OK\"} "
                        "default button \"OK\"' "
                        "-e 'text returned of result' 2>/dev/null\n"
                    )
                os.chmod(askpass_script, 0o755)
                preflight_env = env.copy()
                preflight_env["SUDO_ASKPASS"] = askpass_script

                script_body = (
                    "#!/bin/bash\n"
                    "set -e\n"
                    # One shared authorization for every sudo call below —
                    # a single script making sequential `sudo -A` calls
                    # always reuses the same ticket within itself.
                    "sudo -A -v\n"
                    "( while true; do sudo -n -v; sleep 60; done ) &\n"
                    "KEEPALIVE_PID=$!\n"
                    'trap "kill $KEEPALIVE_PID 2>/dev/null" EXIT\n'
                )
                if need_python:
                    script_body += (
                        "echo FT_STAGE:python_installing\n"
                        "curl -fsSL -o /tmp/ft_python-3.13.0.pkg "
                        "\"https://www.python.org/ftp/python/3.13.0/python-3.13.0-macos11.pkg\"\n"
                        "sudo -A installer -pkg /tmp/ft_python-3.13.0.pkg -target /\n"
                        "echo FT_STAGE:python_done\n"
                    )
                if need_clt:
                    script_body += (
                        "echo FT_STAGE:clt_installing\n"
                        "touch /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress\n"
                        "CLT_LABEL=$(softwareupdate -l 2>/dev/null | "
                        "grep -B 1 -E 'Command Line Tools' | "
                        "awk -F'*' '/^ *\\*/ {print $2}' | "
                        "sed -e 's/^ *Label: //' -e 's/^ *//' | sort -V | tail -n1)\n"
                        "if [ -n \"$CLT_LABEL\" ]; then sudo -A softwareupdate -i \"$CLT_LABEL\"; fi\n"
                        "rm -f /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress\n"
                        "echo FT_STAGE:clt_done\n"
                    )
                if need_brew:
                    script_body += (
                        "echo FT_STAGE:brew_installing\n"
                        # Must be run via `bash -c "<script text>"` — a bare
                        # `"$(curl ...)"` on its own line treats the ENTIRE
                        # downloaded script as a single command/path to
                        # execute instead of running it, which fails with
                        # "File name too long" (it contains "/" characters
                        # throughout, so bash treats it as a literal path
                        # rather than searching $PATH for it).
                        'NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL '
                        'https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"\n'
                        "echo FT_STAGE:brew_done\n"
                    )

                preflight_script = os.path.join(tempfile.gettempdir(), "ft_preflight.sh")
                with open(preflight_script, "w") as f:
                    f.write(script_body)
                os.chmod(preflight_script, 0o755)

                proc = subprocess.Popen(
                    [preflight_script],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, env=preflight_env
                )
                start_time = time.time()
                timed_out = False
                for line in iter(proc.stdout.readline, ""):
                    stripped = line.rstrip()
                    if stripped == "FT_STAGE:python_installing":
                        self._set_status("Installing Python 3.13 (this may take a minute)...")
                        self._log("Installing Python 3.13...")
                    elif stripped == "FT_STAGE:python_done":
                        self._log("Python 3.13 installed ✓")
                    elif stripped == "FT_STAGE:clt_installing":
                        self._set_status("Installing Xcode Command Line Tools...")
                        self._log("Installing Xcode Command Line Tools "
                                  "(this can take several minutes)...")
                    elif stripped == "FT_STAGE:clt_done":
                        self._log("Xcode Command Line Tools installed ✓")
                    elif stripped == "FT_STAGE:brew_installing":
                        self._set_status("Installing Homebrew...")
                        self._log("Installing Homebrew...")
                    elif stripped == "FT_STAGE:brew_done":
                        self._log("Homebrew installed ✓")
                    elif stripped:
                        self._log(stripped)
                    if time.time() - start_time > 1800:
                        proc.terminate()
                        timed_out = True
                        break
                proc.stdout.close()
                proc.wait()

                if timed_out:
                    self._log("Pre-flight install timed out after 30 minutes.")
                    self._set_step(0, "error")
                    self._set_status("Install timed out — check your network and retry.")
                    return
                if proc.returncode != 0:
                    self._log(f"Pre-flight install failed (exit {proc.returncode}) — see log above.")
                    self._set_step(0, "error")
                    self._set_status("Install failed — check log.")
                    return
                if need_python and not os.path.exists(python):
                    self._log("  ✗ Python 3.13 still not found after install.")
                    self._set_status("Python 3.13 install failed — check log.")
                    return
                if need_clt and not os.path.exists("/Library/Developer/CommandLineTools"):
                    self._log("⚠ Xcode Command Line Tools still not found — continuing anyway.")
            else:
                self._log("Python 3.13 found ✓")
                self._log("Xcode Command Line Tools found ✓")

            # Step 0: Homebrew
            self._set_step(0, "active")
            self._set_status("Checking Homebrew...")
            brew = ("/opt/homebrew/bin/brew" if os.path.exists("/opt/homebrew/bin/brew")
                    else "/usr/local/bin/brew" if os.path.exists("/usr/local/bin/brew")
                    else shutil.which("brew"))
            if not brew or not os.path.exists(brew):
                self._set_step(0, "error")
                self._set_status("Homebrew install failed.")
                return
            self._log("Homebrew found ✓")
            brew_dir = os.path.dirname(brew)
            env["PATH"] = brew_dir + ":" + env["PATH"]
            self._set_step(0, "done")
            self._set_progress(1/total)

            # Step 1: Tesseract
            self._set_step(1, "active")
            self._set_status("Checking Tesseract...")
            tess = ("/opt/homebrew/bin/tesseract" if os.path.exists("/opt/homebrew/bin/tesseract")
                    else "/usr/local/bin/tesseract" if os.path.exists("/usr/local/bin/tesseract")
                    else shutil.which("tesseract"))
            if not tess or not os.path.exists(tess):
                self._log("Installing Tesseract OCR...")
                r = subprocess.run([brew, "install", "tesseract"],
                                   capture_output=True, text=True, env=env)
                if r.returncode != 0:
                    self._set_step(1, "error")
                    self._set_status("Tesseract install failed.")
                    self._log(f"  ✗ {r.stderr.strip()}")
                    return
            else:
                self._log("Tesseract found ✓")
            self._set_step(1, "done")
            self._set_progress(2/total)

            # Step 2: Download all source files from GitHub
            self._set_step(2, "active")
            self._set_status("Downloading source files...")
            import urllib.request, urllib.parse, ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            build_dir = os.path.expanduser("~/Applications/FinishingToolBuild")
            os.makedirs(build_dir, exist_ok=True)

            for fname in FILES + AME_PRESET_FILES + RENDER_PRESET_FILES:
                url = f"{GITHUB_BASE}/{urllib.parse.quote(fname)}"
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
            self._log("All files downloaded ✓")

            # DaVinci render presets install straight into Resolve's preset
            # folder here — Resolve doesn't need to be running, and this is
            # non-fatal if the folder can't be created (e.g. Resolve isn't
            # installed on this machine at all; the VFX tab just won't work).
            # Each file is copied and verified independently — previously
            # this was one try/except around the whole loop, so if the
            # FIRST file failed (permissions, a locked file, etc.) the
            # exception aborted the loop and silently skipped the rest,
            # with only one easy-to-miss warning logged for it.
            self._set_status("Installing DaVinci render presets...")
            resolve_presets_dir = os.path.expanduser(
                "~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Presets/Render")
            preset_failures = []
            try:
                os.makedirs(resolve_presets_dir, exist_ok=True)
            except Exception as e:
                self._log(f"⚠ Could not create DaVinci render presets folder: {e}")
                preset_failures = list(RENDER_PRESET_FILES)
            else:
                for fname in RENDER_PRESET_FILES:
                    src = os.path.join(build_dir, fname)
                    dst = os.path.join(resolve_presets_dir, fname)
                    try:
                        shutil.copy(src, dst)
                        if os.path.getsize(dst) != os.path.getsize(src):
                            raise IOError("copied file size does not match source")
                        self._log(f"  ✓ {fname}")
                    except Exception as e:
                        preset_failures.append(fname)
                        self._log(f"  ✗ {fname}: {e}")

            if preset_failures:
                self._log(f"⚠ {len(preset_failures)} DaVinci render preset(s) failed to install: "
                           f"{', '.join(preset_failures)}")
            else:
                self._log("DaVinci render presets installed ✓")
                self._log("  Note: if DaVinci Resolve is already open, restart it so it "
                           "picks up the new presets — it only reads this folder on launch.")

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
                + build_env.get("PATH", "")
            )
            proc = subprocess.Popen(
                ["/bin/bash", build_script],
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
                self._set_status(f"Build failed (exit {proc.returncode}) — check log.")
                self._log(f"✗ Build script exited with code {proc.returncode}")
                return

            app_bundle = "/Applications/Finishing Tool.app"
            if not os.path.exists(app_bundle):
                self._set_step(3, "error")
                self._set_status("App not found after build.")
                return

            self._log("Finishing Tool installed ✓")
            self._set_step(3, "done")
            self._set_progress(1.0)
            if preset_failures:
                self._set_status(f"Installed, but {len(preset_failures)} DaVinci render preset(s) failed — see log.")
            else:
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
