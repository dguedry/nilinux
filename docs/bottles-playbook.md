# Legacy: Native Access in a Bottles bottle (manual playbook)

> **This is the manual route that preceded the app.** The `nilinux` app
> (see the top-level README) applies every fix below automatically in its
> own Wine environment and can import a bottle set up this way
> (*Import from Bottles…*). Keep reading only if you want to understand the
> fixes or run the scripts in `legacy/` against a Bottles bottle yourself.

Gets **Native Instruments Native Access 3.x** (the Electron-based version) running
in a [Bottles](https://usebottles.com/) Flatpak Wine bottle, including the NTK
Daemon helper service that Native Access needs to install products.

Validated 2026-08-23 with Native Access 3.23.0, Bottles 66.8 (Flatpak),
GE-Proton 10-26 runner, X11 session. The same fixes reproduce on a Soda 9.0
runner, so they are not runner-specific.

## TL;DR

```bash
# 0. In Bottles: create a bottle (application environment). Get Native Access
#    into it: NI's NSIS package ("Native-Access-latest.exe") does NOT run
#    under Wine — instead extract the app directly:
#      7z e Native-Access-latest.exe '$PLUGINSDIR/app-64.7z'
#      7z x app-64.7z -o"<bottle>/drive_c/Program Files/Native Instruments/Native Access"
#    The app will NOT launch yet — that's expected.
# 1. Host prerequisites:
sudo apt install p7zip-full cabextract curl python3   # (or distro equivalent)
# 2. Run the setup script against your bottle:
legacy/setup-native-access.sh <bottle-name>     # default: Music
# 3. Launch:
legacy/launch-native-access.sh
```

Log in when the window appears. The "grant permission to install
dependencies" prompt should not appear, because the helper service is already
installed and running.

## Why Native Access breaks under Wine (and what the script does)

Each of these was diagnosed from a real failure; all five are required.

### 1. V8 stack overflow → instant silent crash

Symptom: process exits before any window; Wine log shows
`err:virtual:virtual_setup_exception stack overflow`.

Wine sizes the Electron main thread from the exe's PE header
(`SizeOfStackReserve` = 8MB). Chromium's V8 needs more. **`ulimit -s` does
not help** — the PE header itself must be patched to 64MB. Native Access
self-updates replace the exe and revert this, so `launch-native-access.sh`
re-applies the patch on every launch.

> Validated end-to-end on a freshly created bottle (2026-08-23): create
> bottle → extract NA → `setup-native-access.sh` → window appears. Two
> gotchas the fresh-bottle test surfaced, both now handled by the script:
> the bottle's sync setting must match the script's `WINEFSYNC=1` (script
> normalizes the bottle to fsync), and the font kit must contain **only the
> four base faces per family** — copying DejaVu *Condensed*/*ExtraLight*
> into the bottle poisons Wine's font matching for the substituted UI font
> and reintroduces failure #2 below.

### 2. GDI handle exhaustion → window never appears

Symptom: `err:gdi:alloc_gdi_handle out of GDI object handles, expect a
crash`, no window.

A fresh bottle has ~12 fonts and no Segoe UI (Chromium's Windows UI font).
Chromium's font-fallback then re-enumerates every font for every text run —
we traced ~53,000 `HFONT` creations, blowing Wine's 65,536-entry GDI handle
table before the window mapped. Fix: install DejaVu / Liberation / Ubuntu /
Noto-Symbols fonts into `C:\windows\Fonts` and add registry
`FontSubstitutes` mapping Segoe UI, Tahoma, Verdana, Calibri, Consolas to
them, so lookups resolve immediately.

### 3. Software rendering makes #2 worse

NA's own settings JSON (hash-named file in
`AppData/Roaming/Native Instruments/Native Access/`) ships with
`"disableHardwareAcceleration": true`, forcing the GDI paint path. The
script flips it to `false`. **Edit only while NA is fully stopped** — it
rewrites the file on exit.

**NA ≥ 3.25 addendum — blank window:** newer NA ships a Chromium whose GPU
process crash-loops under Wine (`GPU process exited unexpectedly`,
`Failed to create shared context for virtualization` in the Chromium log),
so the app runs, the renderer draws, but the window stays blank. Launch
with `--disable-gpu` (the launch script does this) — Skia software
compositing paints correctly and does not re-trigger the font/GDI storm
from failure #2.

### 4. Wine's C runtime crashes NI's code

Symptom: `NTKDaemon.exe` (and some NI installers) segfault with a null
dereference in `msvcp140`.

Two problems: Wine's builtin `ucrtbase` misbehaves with NI code (the known
winetricks `ucrtbase2019` issue), and a **version-mismatched** `msvcp140.dll`
vs `vcruntime140`/`concrt140` set crashes on startup. Fix: install the real
`ucrtbase.dll` (extracted from the last VC2019 redist that ships it,
SHA-256 verified) plus a **matched** VC++ 2022 x64 runtime set into
`system32`, with `native,builtin` DLL overrides. Wine builtins are backed up
as `*.bak` alongside.

### 5. The NTK Daemon can't be installed the normal way

Symptom: clicking "grant permission to install dependencies" does nothing;
the bundled installer's MSI custom actions (`WixDependencyCheck`, `ACTAWE`)
crash with RPC error 1726 / return 1603.

Fix: skip the installer entirely. The daemon's files (`NTKDaemon.exe`,
`ni-platform.dll`, `aria2c.exe`, `crashpad_handler.exe`) are extractable
with 7z from `resources/daemon/win/NTKDaemon *Setup PC.exe` inside the NA
install dir. Copy them to
`C:\Program Files\Common Files\Native Instruments\NTKDaemon\` and register
the service: `wine sc create NTKDaemon binPath= "..." start= auto`.
Success looks like this in NA's log:
`(daemon startup) Valid running daemon found, version 1.29.0.0`.

## Operational notes

- **WINEFSYNC:** when invoking `wine64` directly against the prefix (outside
  `bottles-cli`), set `WINEFSYNC=1` or the process dies immediately with an
  fsync-mismatch error against the running wineserver.
- **Flatpak /tmp:** the Bottles sandbox cannot see host `/tmp` paths — put
  `.reg` files inside `drive_c/` before importing with `wine regedit`.
- **Logs to check when things go wrong:**
  - Wine/launch output: whatever you redirect the launch command to
  - NA's own log: `drive_c/users/Public/Documents/Native Instruments/Logs/Native Access/native-access.log`
  - Chromium internals: launch with `-- --enable-logging=file
    "--log-file=C:\users\Public\chrome_debug.log"`
- **NA self-updates (Bottles playbook):** clicking "restart to update" in NA
  leaves the app dead — its NSIS installer exits with code 2 under Wine.
  `./update-native-access.sh` extracts the new app from the downloaded
  updater, swaps the install dir, re-applies the stack patch and updates the
  NTK Daemon. **The `nilinux` package takes a different line:** it disables
  NA's self-updater outright (see the package section), because every NA
  release can break a fix (3.25 needed `--disable-gpu`; the dependency patch
  is tied to one renderer build) and NA must never be downloaded by the app.

## Desktop launcher

The setup script also installs an app-menu launcher ("Native Access", with the
real NI icon extracted from the exe via `wrestool`/`icotool`) if `icoutils` is
present on the host. It lands in `~/.local/share/applications/native-access.desktop`
and calls `launch-native-access.sh`, so every desktop launch gets the stack
re-patch too. Note: `StartupWMClass=steam_proton` matches how GE-Proton tags
Wine windows; if you run several bottles apps their windows may group under
one dock icon.

## Product installs from Native Access fail ("Setup has failed: FALSE")

Downloading works, but NI's InstallAware product installers fail under Wine:
the daemon log (`Logs/NTK/daemon.log`) shows the setup exe exiting with code
100, and running the installer by hand ends with *"Setup has failed:
'FALSE'"*. Root cause: the installer's runtime queries MSI's virtual
`_Property` table, which Wine's MSI SQL parser rejects, so the wizard's
install phase aborts. (Verified with Kontakt 8 8.12.1 on wine-staging 10
and 11.)

**Workaround — manual deployment** (validated with Kontakt 8, which then
runs in the bottle):

1. Grab the product installer. NA downloads it to
   `drive_c/users/steamuser/Documents/Downloads/<product>.zip` (deleted on
   failure — the signed CDN URL survives in
   `Logs/NTK/DownloadLogs/*.log`, grep for `GET /`).
2. `7z x "<Product> Setup PC.exe"` — the payload is a FileBag under
   `data/OFFLINE/<hash>/<hash>/`, plus the inner `Setup PC.msi`.
3. Run the setup **silently** under an MSI trace — no clicking required:
   ```bash
   WINEPREFIX=<bottle> WINEFSYNC=1 WINEDEBUG=+msi \
     wine "<Product> Setup PC.exe" /s 2> trace.log
   ```
   The trace contains `INSERT INTO Property … VALUES ('P<hash>_1', '<dest>')`
   lines that map every payload hash-dir to its Windows destination, plus
   `REGISTRY KEYS` entries naming the HKLM values the installer would write.
   (The installer also writes the same summary to
   `ProgramData\Native Instruments\Installer Log\<Product> Setup PC Log.ini`.)
   Verified 2026-09-05 with Kontakt 6.6.1: the silent run yields the complete
   mapping, and `TRACE=trace.log deploy-ni-payload.py` resolves every file
   (3567 files, 0 missing) with no hand-edited roots. The mapping is **not**
   in the MSI's Property table — it is assigned at runtime by the
   InstallAware script — so the trace run is the only automatic source.
4. `msidump.py <msi> Directory|Component|File` dumps the MSI tables
   (needs `pip install olefile`); `TRACE=trace.log deploy-ni-payload.py`
   joins File→Component→Directory against the roots parsed from the trace
   and copies everything into the bottle. It also prints the registry keys
   to apply. (Without `TRACE` it falls back to its hand-written `ROOTS` map.)
5. Import the product's registry keys (also visible in the trace:
   `SOFTWARE\Native Instruments\<Product>` — `InstallDir`, `ContentDir`,
   `InstallVST364Dir`), then run `sync-yabridge.sh`.

Once the registry keys are in place, the daemon's product scan recognizes
the install and Native Access lists it normally.

**Not every NI installer fails.** Kontakt 6.6.1's installer (2021,
InstallAware build from NI's legacy-installers support page) ran to
completion silently under Wine in the NATest bottle on 2026-09-05: app,
VST2, VST3, factory content and registry keys all landed, with no MSI
errors. The `_Property` failure is specific to newer InstallAware builds
such as Kontakt 8's. So the tool should always *try* a silent install
first and only fall back to the manual deploy when the setup exits
non-zero — and legacy NI apps may simply work.

**Licensing:** activation through NA works fine — the daemon writes a JWT
license to `Public Documents\Native Instruments\Native Access\ras3\<product-id>.jwt`.
Products check the license **only at startup**: if the app was open while
you activated, it keeps saying "no license" until you restart it. Content
libraries (Kontakt libraries etc.) download, install, and activate through
NA with no workaround — only application installers hit the InstallAware
failure above.

## "Requires Kontakt" modal when installing a library (Kontakt 8 Player is installed)

Clicking **Install** on a Kontakt library pops a modal saying the library
"requires at least Kontakt version X ... Currently no viable version of
Kontakt is installed", even though Kontakt 8 Player is installed, licensed,
and listed as installed by both the daemon and the Library tab.

This is Native Access's own dependency logic, not a Wine problem (verified
2026-09-05 by de-obfuscating NA 3.25.2's renderer bundle and reading its
Redux store over the Chromium DevTools protocol). For each library the
daemon supplies a list of "viable" Kontakt products (here: Kontakt 5,
Kontakt 6, Kontakt 7 Player, Kontakt 8 Player). NA takes the **first owned
entry that is not flagged `isPlayer`** and only falls back to a Player if
there is none. Kontakt 8 Player is flagged as a Player; "Kontakt 7 Player"
(owned via Komplete) is *not*, so NA picks it, finds it uninstalled, and
shows the modal offering to install it.

**Permanent fix:** `./patch-na-dependency-check.py [bottle]` patches the
renderer so an *installed* viable Kontakt/Reaktor wins over an uninstalled
owned one; the Install button then just installs the library. The script
rewrites `resources/app.asar` in place (only the renderer bundle entry
changes; offsets and per-file SHA-256 integrity are recomputed, unpacked
entries untouched) and updates the asar header hash in the exe's
`Integrity` resource, because NA ships with Electron's
`EnableEmbeddedAsarIntegrityValidation` fuse enabled. Backups:
`app.asar.orig` and `Native Access.exe.pre-deppatch`. `launch-native-access.sh`
re-applies it automatically when the marker is missing (i.e. after an NA
update). Stop NA before running it by hand. If a new NA version changes the
obfuscated variable names the script exits with "patch site not found" and
the manual workaround below still applies.

**Manual workaround: click "Only Install This Product"** in that modal. The library then
installs through the daemon's content-install path, which works under Wine,
and Kontakt 8 Player loads it (every library in the current product hints
file needs at most Kontakt 8.12.0; the deployed Player is 8.12.1). Do
**not** click "Install Both" — that queues the Kontakt 7 Player application
installer, which is an InstallAware setup and fails under Wine (see above).

## Library installs but doesn't show up in Kontakt

Native Access reports the library installed and activated, the files are in
`Public Documents\<Name> Library`, but Kontakt's browser never lists it
(seen with Butch Vig Drums, NTK Daemon 1.31.1, 2026-09-05).

Kontakt discovers libraries at startup from
`HKLM\SOFTWARE\Native Instruments\<Name>` (`ContentDir`, `HU`, `JDX`,
`Visibility`). The daemon's newer "Slim Deployment" wrote
`installed_products\<Name>.json` and the license, but under Wine it skipped
that registry key. (Kontakt itself notices the library and writes an HKCU
`UserRemoved=0` flag, which is a red herring.)

Fix: `./register-ni-library.py "<Library Name>" [bottle]` (or `--all`) rebuilds
the key from `installed_products\<Name>.json` (ContentDir/ContentVersion)
and `Service Center\NativeAccess.xml` (HU/JDX), then **restart Kontakt**.
Note the script invokes `C:\windows\system32\reg.exe` explicitly with
the bottle's own wine and `WINEFSYNC=1`; `bottles-cli reg add` reported
success but the key never appeared in either HKLM view.

## No sound from a standalone NI app

Kontakt (and likely other NI standalones) auto-selects the first WASAPI
device it sees — under Wine that's `WASAPI (Shared Mode) PulseAudio Input`,
i.e. the *capture* endpoint, so the engine runs silently. Fix it in the
app's audio options, or directly (app closed — it rewrites settings on
exit):

```bash
wine reg add "HKCU\Software\Native Instruments\Kontakt 8" \
  /v "AB2 AudioDevice" /d "WASAPI (Shared Mode) PulseAudio Output" /f
```

Verified: with the output endpoint selected, Kontakt's stream appears in
`pactl list sink-inputs` and notes are audible. For lower latency than
shared-mode WASAPI, the upgrade path is wineasio + JACK/PipeWire — not
needed for auditioning sounds, worth it for live playing. (When using the
plugin through yabridge in a DAW, the DAW owns the audio and none of this
applies.)

## Kontakt (or another NI app) freezes when Native Access opens

NI apps coordinate library scans through boost named mutexes in
`drive_c/ProgramData/boost_interprocess/`. Those mutexes are **not
crash-safe**: if an NI process dies while holding one (e.g. NA crashing on
launch while Kontakt runs), every other NI app blocks on it forever — the
frozen app sits at 0% CPU and ignores input. Recovery:

1. Kill the frozen app(s): `pkill -f "Kontakt 8.exe"` etc.
2. Delete the stale mutex files:
   `rm drive_c/ProgramData/boost_interprocess/*/*`
3. Relaunch.

`launch-native-access.sh` clears stale mutex files automatically when no NI
app is running. Launching NA *first* and Kontakt second has been reliable;
the crash happened launching NA while Kontakt was mid-scan.

## Bridging the VSTs to Linux DAWs (yabridge)

`./sync-yabridge.sh [bottle] [extra-dir ...]` installs
[yabridge](https://github.com/robbert-vdh/yabridge) if missing (latest GitHub
release into `~/.local/share/yabridge`), registers the bottle's plugin
directories, and runs `yabridgectl sync --prune`. Bridged plugins land in
`~/.vst/yabridge`, `~/.vst3/yabridge` and `~/.clap/yabridge` for Ardour /
REAPER / Bitwig etc. **Re-run it after every plugin install**, whether from
Native Access or a third-party installer.

Directories it registers:

- the standard 64-bit Windows locations, created if missing: NI's
  `VSTPlugins 64 bit`, `Program Files\VstPlugins`, `Steinberg\VstPlugins`,
  `Common Files\VST2`, `Common Files\Steinberg\VST2`, `Common Files\VST3`,
  `Common Files\CLAP`;
- the VST2 path recorded in the bottle registry
  (`HKLM\Software\VST\VSTPluginsPath`), which third-party installers honour;
- any directory under `Program Files` containing a `.vst3` or `.clap` bundle
  (those extensions are unambiguous, so vendor folders are discovered
  automatically — e.g. `Program Files\ACE Studio\vst`). VST2 `.dll`s cannot
  be told apart from ordinary DLLs, so VST2 in an unusual folder must be
  passed explicitly;
- extra directories given on the command line, absolute or relative to
  `drive_c`. It refuses over-broad roots such as `Program Files` itself.

It needs a host `wine` on PATH (separate from the Bottles runner) and warns
when that wine's major version is older than the runner's. If a plugin
refuses to load complaining the prefix was updated by a newer Wine, that
warning is the cause — install a newer host wine (e.g. wine-staging matching
the runner's major version).
