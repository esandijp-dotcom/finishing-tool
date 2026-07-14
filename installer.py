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
FILES        = ["main.py", "thinking.gif", "icon.png", "version.json",
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

    def _ensure_sudo_authenticated(self, env):
        """Lazily authenticates sudo ONCE (via a GUI password prompt,
        since this process has no real terminal for sudo to prompt on
        directly) and starts a background keepalive that silently
        refreshes that authorization every 60s for the rest of the
        install. Every privileged step below (Python 3.13, Xcode CLT,
        Homebrew) calls this before doing anything that needs sudo —
        it's idempotent, so only the very first call actually prompts;
        every later call just confirms the existing authorization is
        still being kept warm and returns immediately. This is what
        makes the whole install a single password prompt instead of
        one per privileged step.

        Returns True if sudo is authenticated and usable, False if the
        user failed to authenticate (wrong password, cancelled, etc).
        """
        if getattr(self, "_sudo_authenticated", False):
            return True
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
        self._sudo_env = env.copy()
        self._sudo_env["SUDO_ASKPASS"] = askpass_script
        try:
            r = subprocess.run(["sudo", "-A", "-v"], env=self._sudo_env,
                               capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                self._log(f"⚠ Password authentication failed: {r.stderr.strip()}")
                return False
        except subprocess.TimeoutExpired:
            self._log("⚠ Password prompt timed out.")
            return False
        keepalive_script = os.path.join(tempfile.gettempdir(), "ft_sudo_keepalive.sh")
        with open(keepalive_script, "w") as f:
            f.write("#!/bin/bash\nwhile true; do sudo -n -v; sleep 60; done\n")
        os.chmod(keepalive_script, 0o755)
        self._sudo_keepalive_proc = subprocess.Popen([keepalive_script], env=self._sudo_env)
        self._sudo_authenticated = True
        return True

    def _install(self):
        self._sudo_authenticated = False
        self._sudo_keepalive_proc = None
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

            # Pre-flight: Python 3.13
            python = "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3"
            if not os.path.exists(python):
                self._log("Python 3.13 not found — downloading and installing...")
                self._set_status("Installing Python 3.13 (this may take a minute)...")
                # tempfile is already imported at module level (line 6) —
                # do NOT re-import it locally here. Python scopes a name as
                # local to the whole function the moment it's imported
                # anywhere inside it, even conditionally, which would
                # shadow the module-level import for the rest of _install()
                # and break every other tempfile.* call below whenever this
                # branch (Python 3.13 already installed) is skipped.
                import urllib.request, ssl
                ctx_py = ssl.create_default_context()
                ctx_py.check_hostname = False
                ctx_py.verify_mode = ssl.CERT_NONE
                py_pkg_url = ("https://www.python.org/ftp/python/3.13.0/"
                              "python-3.13.0-macos11.pkg")
                py_pkg = os.path.join(tempfile.gettempdir(), "python-3.13.0.pkg")
                try:
                    self._log("  Downloading Python 3.13 installer (~45MB)...")
                    req = urllib.request.urlopen(py_pkg_url, context=ctx_py, timeout=120)
                    with open(py_pkg, "wb") as f:
                        f.write(req.read())
                    self._log("  Installing Python 3.13 (requires admin)...")
                    if not self._ensure_sudo_authenticated(env):
                        self._set_status("Authentication failed — check log.")
                        return
                    r = subprocess.run(["sudo", "-A", "installer", "-pkg", py_pkg, "-target", "/"],
                                       env=self._sudo_env, capture_output=True, text=True, timeout=300)
                    if r.returncode != 0 or not os.path.exists(python):
                        self._log(f"  ✗ Python install failed: {r.stderr.strip()}")
                        self._set_status("Python 3.13 install failed — check log.")
                        return
                    self._log("Python 3.13 installed ✓")
                except Exception as e:
                    self._log(f"  ✗ Could not install Python 3.13: {e}")
                    self._set_status("Python 3.13 install failed — check log.")
                    return
            else:
                self._log("Python 3.13 found ✓")

            # Pre-flight: Xcode Command Line Tools. None of this app's own
            # pip packages need a compiler (verified — they're all prebuilt
            # wheels or pure Python), but Homebrew's own installer needs
            # CLT for git, so this is a real transitive requirement via the
            # Homebrew step below.
            #
            # `xcode-select --install` pops a separate system GUI dialog
            # ("Finding Software...") that this process can't track,
            # can't time out, and can't report progress on — installing
            # via `softwareupdate` directly instead (the same mechanism
            # Homebrew's own bootstrap script uses internally) keeps
            # everything in this process's own log, with a real timeout.
            xcode_check = subprocess.run(
                ["xcode-select", "-p"],
                capture_output=True, text=True, env=env
            )
            if xcode_check.returncode != 0:
                self._log("Xcode Command Line Tools not found — installing "
                          "(this can take several minutes)...")
                self._set_status("Installing Xcode Command Line Tools...")
                clt_script = os.path.join(tempfile.gettempdir(), "ft_install_clt.sh")
                with open(clt_script, "w") as f:
                    f.write(
                        "#!/bin/bash\n"
                        "touch /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress\n"
                        "CLT_LABEL=$(softwareupdate -l 2>/dev/null | "
                        "grep -B 1 -E 'Command Line Tools' | "
                        "awk -F'*' '/^ *\\*/ {print $2}' | "
                        "sed -e 's/^ *Label: //' -e 's/^ *//' | sort -V | tail -n1)\n"
                        "if [ -n \"$CLT_LABEL\" ]; then\n"
                        "  softwareupdate -i \"$CLT_LABEL\"\n"
                        "fi\n"
                        "rm -f /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress\n"
                    )
                os.chmod(clt_script, 0o755)
                if not self._ensure_sudo_authenticated(env):
                    self._log("⚠ Could not authenticate for Xcode Command Line Tools "
                              "install — continuing anyway.")
                else:
                    try:
                        r = subprocess.run(["sudo", "-A", "bash", clt_script],
                                           env=self._sudo_env, capture_output=True,
                                           text=True, timeout=900)
                        if os.path.exists("/Library/Developer/CommandLineTools"):
                            self._log("Xcode Command Line Tools installed ✓")
                        else:
                            self._log(f"⚠ CLT install may not have finished: {r.stderr.strip()} "
                                      f"— continuing anyway.")
                    except subprocess.TimeoutExpired:
                        self._log("⚠ Xcode Command Line Tools install timed out after 15 "
                                  "minutes — continuing anyway.")
            else:
                self._log("Xcode Command Line Tools found ✓")

            # Step 0: Homebrew
            self._set_step(0, "active")
            self._set_status("Checking Homebrew...")
            # Note: `if/else` binds looser than `or` in Python, so a naive
            # "shutil.which(...) or X if cond else Y" would silently drop
            # the shutil.which() result whenever cond is False. Mirror the
            # same explicit path-first pattern used after a fresh install
            # below instead of chaining `or` with an untested ternary.
            brew = ("/opt/homebrew/bin/brew" if os.path.exists("/opt/homebrew/bin/brew")
                    else "/usr/local/bin/brew" if os.path.exists("/usr/local/bin/brew")
                    else shutil.which("brew"))
            if not brew or not os.path.exists(brew):
                self._log("Homebrew not found — installing...")
                self._set_status("Installing Homebrew...")

                # Homebrew's own installer has a hard-coded safety check
                # that REFUSES to run as root — so it can't be wrapped in
                # "with administrator privileges" (that runs everything as
                # root) like the other pre-flight installs. It needs sudo
                # for SEVERAL of its own internal steps (prefix, cache, and
                # repository directories, not just one) — verified by
                # reading Homebrew's actual current install.sh, not
                # assumed. It finds sudo access via SUDO_ASKPASS, same as
                # Python 3.13/Xcode CLT above — _ensure_sudo_authenticated
                # is idempotent, so if either of those already prompted
                # this run, this reuses that same authorization instead of
                # prompting a third time.
                if not self._ensure_sudo_authenticated(env):
                    self._log("Homebrew install failed: could not authenticate.")
                    self._set_step(0, "error")
                    self._set_status("Authentication failed — check log.")
                    return
                brew_env = self._sudo_env.copy()
                brew_env["NONINTERACTIVE"] = "1"

                brew_script = os.path.join(tempfile.gettempdir(), "ft_install_brew.sh")
                with open(brew_script, "w") as f:
                    f.write(
                        "#!/bin/bash\n"
                        # Must be run via `bash -c "<script text>"` — a bare
                        # `"$(curl ...)"` on its own line treats the ENTIRE
                        # downloaded script as a single command/path to
                        # execute instead of running it, which fails with
                        # "File name too long" once bash tries to resolve
                        # that giant string as a path (it contains "/"
                        # characters throughout, so bash treats it as a
                        # literal path rather than searching $PATH for it).
                        '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"\n'
                    )
                os.chmod(brew_script, 0o755)
                # Stream output live instead of capturing it and dumping
                # the whole thing as one blob on failure — Homebrew's own
                # script already prints clean, readable status lines as it
                # runs, so this shows real progress instead of a raw text
                # dump at the end (or nothing at all, on success).
                # Run as the current user, NOT admin-privileged — see note
                # above about Homebrew's own root check.
                proc = subprocess.Popen(
                    [brew_script],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, env=brew_env
                )
                start_time = time.time()
                timed_out = False
                for line in iter(proc.stdout.readline, ""):
                    stripped = line.rstrip()
                    if stripped:
                        self._log(stripped)
                    if time.time() - start_time > 600:
                        proc.kill()
                        timed_out = True
                        break
                proc.stdout.close()
                proc.wait()

                if timed_out:
                    self._log("Homebrew install timed out after 10 minutes.")
                    self._set_step(0, "error")
                    self._set_status("Homebrew install timed out — check your network and retry.")
                    return
                if proc.returncode != 0:
                    self._log(f"Homebrew install failed (exit {proc.returncode}) — see log above.")
                    self._set_step(0, "error")
                    self._set_status("Homebrew install failed — check log.")
                    return
                brew = ("/opt/homebrew/bin/brew" if os.path.exists("/opt/homebrew/bin/brew")
                        else "/usr/local/bin/brew" if os.path.exists("/usr/local/bin/brew")
                        else shutil.which("brew"))
                if not brew:
                    self._set_step(0, "error")
                    self._set_status("Homebrew install failed.")
                    return
                # Update PATH with new brew location
                brew_dir = os.path.dirname(brew)
                env["PATH"] = brew_dir + ":" + env["PATH"]
            else:
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
            self._set_status("Installing DaVinci render presets...")
            resolve_presets_dir = os.path.expanduser(
                "~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Presets/Render")
            try:
                os.makedirs(resolve_presets_dir, exist_ok=True)
                for fname in RENDER_PRESET_FILES:
                    shutil.copy(os.path.join(build_dir, fname),
                                os.path.join(resolve_presets_dir, fname))
                self._log("DaVinci render presets installed ✓")
            except Exception as e:
                self._log(f"⚠ Could not install DaVinci render presets: {e}")

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
            self._set_status("Installation complete!")
            self.after(0, self._show_done)

        except Exception as e:
            self._log(f"Error: {e}")
            self._set_status(f"Installation failed: {e}")
        finally:
            # Stop the sudo keepalive loop (if it was ever started) no
            # matter how _install() exits — success, an early return, or
            # an exception — so it doesn't linger as an orphaned
            # background process after the installer is done.
            if getattr(self, "_sudo_keepalive_proc", None) is not None:
                self._sudo_keepalive_proc.kill()

    def _show_done(self):
        self._install_btn.place_forget()
        self._launch_btn.place(relx=0.5, rely=0.5, anchor="center")

    def _launch_app(self):
        subprocess.run(["open", "/Applications/Finishing Tool.app"], capture_output=True)
        self.after(2000, self.destroy)


if __name__ == "__main__":
    app = InstallerApp()
    app.mainloop()
