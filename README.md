# NI on Linux (`nilinux`)

[![CI](https://github.com/dguedry/nilinux/actions/workflows/ci.yml/badge.svg)](https://github.com/dguedry/nilinux/actions/workflows/ci.yml)

Run Native Instruments' **Native Access**, the products it installs, and any
Windows VST2 / VST3 / CLAP plugin on Linux — and use them in Linux DAWs —
without ever touching Wine yourself.

The app owns a private Wine environment, applies every fix Native Access
needs to run under Wine, installs NI applications whose installers do not
work under Wine, runs third-party plugin installers, and bridges the
resulting plugins to Linux DAWs with [yabridge](https://github.com/robbert-vdh/yabridge).

*Not affiliated with or endorsed by Native Instruments GmbH. Native Access is
not included: you download it from NI and hand the file to the app.*

## Install

**Flatpak (recommended).** **[Download nilinux.flatpak (latest release)](https://github.com/dguedry/nilinux/releases/latest/download/nilinux.flatpak)**
— or pick a version on the [releases page](https://github.com/dguedry/nilinux/releases).
Then install it for your user; or from a terminal:

```bash
curl -LO https://github.com/dguedry/nilinux/releases/latest/download/nilinux.flatpak
flatpak install --user nilinux.flatpak     # needs the flathub remote for the GNOME 50 runtime
```

Or build it yourself:

```bash
flatpak/build.sh                      # builds against org.gnome.Platform 50, installs for the user
flatpak run io.github.dguedry.nilinux       # GUI
flatpak run --command=nilinux io.github.dguedry.nilinux doctor   # CLI
```

Per-commit builds are attached to each [CI run](https://github.com/dguedry/nilinux/actions/workflows/ci.yml)
as the `nilinux-flatpak` artifact.

**From source (no Flatpak).** Needs Python ≥ 3.10, GTK4 + libadwaita bindings
(`python3-gi`), `7z`, `cabextract`, and `pip install olefile`:

```bash
python3 -m nilinux.gui                # GUI
python3 -m nilinux --help             # CLI
```

The app keeps everything under `~/.local/share/nilinux` (Flatpak:
`~/.var/app/io.github.dguedry.nilinux/data/nilinux`): a pinned portable Wine build,
the prefix, logs. Nothing is installed system-wide.

## First run

1. The app prepares its environment: downloads the pinned Wine build
   (SHA-256 checked), creates the prefix, installs fonts, the real Microsoft
   C runtime, registry fixes, and yabridge. About two minutes.
2. It then shows **Get Native Access** with two buttons: one opens
   <https://www.native-instruments.com/pages/native-access>, the other picks
   the downloaded `Native-Access-latest.exe` from your Downloads folder.
3. The app installs Native Access from that file, applies the NA-side fixes,
   and you sign in to your NI account inside Native Access as usual.

## Using it

- **Plugins** — installed NI products with registration and license state,
  and the plugins bridged to your DAWs.
- **Install**
  - *Open Native Access* — install or update products. When you close it,
    libraries are registered for Kontakt and plugins are bridged
    automatically.
  - *Install an NI application from its installer* — for Kontakt, Reaktor,
    Massive… when NI's own installer fails under Wine (see below). Pick the
    `.zip` Native Access downloaded or the `Setup PC.exe`.
  - *Run a plugin installer* — any third-party Windows plugin installer;
    its own window opens, and the plugins are bridged when it finishes.
  - *Add a plugin folder* / *Bridge plugins now*.
- **Health** — every fix and prerequisite with a repair hint. The menu has
  *Re-run setup / repair* and *Make DAWs use this wine*.

**DAWs must run plugins with the app's wine.** Setup installs a small
`~/.local/bin/wine` shim that execs the app's wine (with `WINEFSYNC=1`), and
writes `~/.config/environment.d/50-nilinux.conf` (`WINELOADER`) for desktops that
import it. yabridge resolves `wine` through PATH, and `~/.local/bin` precedes
`/usr/bin` on Debian, Ubuntu, Mint and Fedora desktops, so the shim takes effect
for every wine started afterwards, with no re-login. Until one of these is
active the app does not bridge plugins, and Health and the Plugins tab say so: a
DAW using the host's own wine would run *its* prefix update on the app's prefix
and replace the DLLs with another version's. If you already have your own
`~/.local/bin/wine`, setup leaves it alone and tells you.
- **Progress** — step list, download bar and log for long tasks.

Bridged plugins land in `~/.vst/yabridge`, `~/.vst3/yabridge`,
`~/.clap/yabridge`; point your DAW there if it does not scan them already.

CLI equivalents: `setup`, `install-na <file>`, `launch [--wait]`,
`products`, `register`, `install <installer> [--third-party]`, `sync
[dirs…] [--daw-env]`, `doctor`, `status`.

## Native Access updates

Native Access never updates itself under this app: its build default for
auto-updates is flipped inside `app.asar`, so it neither downloads nor
prompts. The app reads NI's release feed for the version *number* only and
shows "3.xx available — validated / not yet validated on this stack" in
Health and on the Install tab. To update, download the new installer from NI
and pick it in the app; every fix is re-applied and the previous install is
kept as `Native Access.prev`. Each NA release can break a fix (3.25 needed a
GPU workaround 3.23 did not; the dependency patch is tied to one renderer
build), which is why updates are deliberate.

## What the app fixes, and why

Each of these was diagnosed from a real failure.

| Symptom under Wine | Cause | What the app does |
|---|---|---|
| NA exits silently before any window | Wine sizes the main thread from the exe's PE header (8 MB); V8 overflows it | Patches the header's stack reserve to 64 MB (re-applied on every launch) |
| `out of GDI object handles`, no window | No Segoe UI etc.; Chromium's font fallback re-enumerates fonts ~53 000 times | Installs DejaVu/Liberation/Noto and maps Segoe UI, Tahoma, Calibri… to them |
| Blank window (NA ≥ 3.25) | Chromium GPU process crash-loops | Launches with `--disable-gpu` |
| NTK Daemon / NI apps crash in `msvcp140` | Wine's `ucrtbase` and a mismatched VC runtime | Installs the real `ucrtbase.dll` and a matched VC++ 2022 set with native overrides |
| "Grant permission to install dependencies" does nothing | The daemon's MSI custom actions crash | Extracts the daemon from NA's own resources and registers it as a service |
| NA's installer/updater exits with code 2 | NSIS package does not run under Wine | Extracts `app-64.7z` from the installer instead |
| "Requires Kontakt … no viable version installed" although Kontakt Player is installed | NA prefers an *owned* full Kontakt over the installed Player | Patches the renderer's dependency selector so an installed viable product wins |
| NI app installers fail ("Setup has failed: FALSE") | InstallAware queries an MSI virtual table Wine's SQL parser rejects | Runs the installer silently under an MSI trace; if it fails, deploys the payload from the trace's destination map and writes the registry keys |
| Library installed but invisible in Kontakt | Daemon skipped the HKLM key Kontakt scans | Writes `ContentDir`/`HU`/`JDX` from the daemon's record and NI's catalogue |
| NI apps freeze each other | Boost named mutexes are not crash-safe | Clears stale mutex files when no NI app runs |
| Every NI application install fails instantly in the Flatpak (daemon error 731; any 32-bit program: "Application could not be started") | Flatpak's seccomp filter blocks `modify_ldt`, which Wine's WoW64 layer needs for 32-bit code; NI's InstallAware setup exes are 32-bit | Manifest grants `--allow=multiarch`; Health checks that a 32-bit program runs |
| Every download fails ("Download folder does not exist", "could not create new file") | A fresh prefix has no download location; Wine's `Downloads` folder is a symlink to the host's, which the sandbox mounts read-only | Points NA at `C:\users\Public\Downloads` inside the prefix whenever the configured location is unset or not writable (checked at setup and on every launch; a working custom location is kept) |
| Plugins refuse to load in a DAW ("prefix updated by a newer Wine") | Host wine older than the prefix's wine | One wine binary for both: setup installs a `~/.local/bin/wine` shim (and `WINELOADER` via environment.d); plugins are not bridged until one is active |
| After a DAW session, NI installers die instantly ("Cannot create temporary folder" style dialogs, daemon error 731) | A DAW's yabridge ran the *host* wine on the prefix, whose prefix update replaced the built-in DLLs with another version's | Health compares built-in DLLs with the app's wine; setup re-runs the prefix update with the right wine (native C runtime files are kept) |

The `app.asar` patches recompute the archive's per-file integrity and the
header hash stored in the exe (NA ships with Electron's asar-integrity fuse
on); originals are kept next to the patched files.

## How it is built

```
nilinux/
  wine.py            portable wine provisioning; Prefix: env, run, 64-bit registry,
                     per-prefix process scan, wineserver lock detection
  native_access.py   install from a user-supplied installer, every NA fix, launch,
                     asar patcher (dependency check, auto-update off)
  products.py        NI catalogue, installed products, library registration,
                     silent-install-or-trace-deploy for NI apps, third-party installers
  yabridge.py        install, plugin-dir discovery, sync, DAW environment
  doctor.py, cli.py, gui.py (GTK4 + libadwaita), msi.py, download.py, progress.py
flatpak/             manifest (GNOME 50 runtime; bundles 7-Zip, cabextract, olefile), build.sh
data/                desktop entry, AppStream metainfo, icon
```

Every operation reports through `progress.Reporter`; the CLI prints steps,
the GUI maps them onto rows. Wine is Kron4ek's portable
`wine-11.17-staging-amd64-wow64`, pinned by hash in `wine.py`.

**CI.** Every push and pull request runs Flathub's linter on the manifest
and metainfo, a Python syntax/import check, and a full Flatpak build of the
working tree in Flathub's GNOME 50 container; the built bundle
(`nilinux.flatpak`) is attached to the run as an artifact.

**Releasing — automatic.** Every push to `main` publishes a release: CI's
`auto-tag` job bumps the patch version (`0.1.2` → `0.1.3` → …), adds a metainfo
`<release>` entry whose note is the push's commit subject, commits and tags it,
and the same run builds `nilinux.flatpak` and attaches it to a GitHub release.
The `releases/latest` download link then serves it, no manual step. So write a
clear commit subject — it becomes the changelog. To push *without* releasing
(docs, WIP), put `[skip release]` in the commit message. The bot's own bump
commit carries that marker, so there is no release-of-a-release loop.

**Releasing — manual.** To choose the version and notes yourself (e.g. a minor
or major bump, not the next patch), run `scripts/release.sh 0.2.0 "notes"` — it
bumps, lints the metainfo, commits, tags and pushes; add `--dry-run` to preview.
Both paths share `scripts/bump.py`, which edits the package, `pyproject.toml`
and the metainfo. Flathub is separate either way: after a release, re-pin the
manifest's `tag`/`commit` to the new version and refresh your Flathub fork by
hand.

**Flatpak notes.** Wine is downloaded at first run rather than bundled;
Native Access is neither bundled nor downloaded. Permissions: network,
X11/PulseAudio, DRI, `--allow=devel` (wine), `--allow=multiarch` (32-bit
installers), the yabridge directories under `~`, and `/tmp` plus `xdg-run/wine`
(share the prefix's wineserver socket with a host-side yabridge so a DAW's
plugins and the app's NTK daemon use one wineserver: this wine puts the socket
under `/tmp/.wine-<uid>` when `/tmp` is real, and the sandbox's private `/tmp`
would otherwise split the two). Installers
arrive through the file-chooser portal, so the app has no grant on
`~/Downloads`: granting it would make Wine symlink the prefix's Downloads
folder to the read-only host folder, and NI's daemon would have no writable
default download location. `yabridgectl` runs with the host's
`XDG_CONFIG_HOME`/`XDG_DATA_HOME` because the sandbox remaps them. The app id
`io.github.dguedry.nilinux` is a placeholder; Flathub needs an id under a domain
or GitHub account you control, changed consistently in the manifest, desktop
file, metainfo, icon and `gui.APP_ID`.

## Tested

- The author's machine (Ubuntu-based).
- A fresh Fedora 43 Workstation VM: no Steam, no wine, GNOME on Wayland —
  environment setup in 97 s, Native Access and the GUI render, sync works.
- Native Access 3.23.0 and 3.25.2 (`KNOWN_GOOD` in `native_access.py`);
  Kontakt 8 Player 8.12.1, Kontakt 6.6.1 (its installer runs unmodified),
  Scarbee Mark I, Butch Vig Drums.
