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
| Plugins refuse to load in a DAW ("prefix updated by a newer Wine") | Host wine older than the prefix's wine | One wine binary for both; *Make DAWs use this wine* sets `WINELOADER` |

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

**Flatpak notes.** Wine is downloaded at first run rather than bundled;
Native Access is neither bundled nor downloaded. Permissions: network,
X11/Wayland/PulseAudio, DRI, `--allow=devel` (wine), `--filesystem=home`
(yabridge dirs, installers) and `xdg-run/wine`
(share the prefix's wineserver with a host-side yabridge, which works because
both run the identical wine binary). `yabridgectl` runs with the host's
`XDG_CONFIG_HOME`/`XDG_DATA_HOME` because the sandbox remaps them. The app id
`io.github.dguedry.nilinux` is a placeholder; Flathub needs an id under a domain
or GitHub account you control, changed consistently in the manifest, desktop
file, metainfo, icon and `gui.APP_ID`.

## Submitting to Flathub

The committed manifest already passes `flatpak-builder-lint` (only the
"app id URL not reachable" error remains while the repository is private).
The submission is a pull request carrying the manifest, not a bundle:

1. Make this repository public (the manifest builds from the `v0.1.0` tag,
   and the metainfo's screenshot URLs point at `main`).
2. Fork <https://github.com/flathub/flathub>, then:
   ```bash
   git clone --branch new-pr git@github.com:<you>/flathub.git flathub-pr
   cd flathub-pr && git checkout -b io.github.dguedry.nilinux
   cp ../nilinux/flatpak/io.github.dguedry.nilinux.yml ../nilinux/flatpak/flathub.json .
   git add . && git commit -m "Add io.github.dguedry.nilinux" && git push -u origin HEAD
   ```
   Open the PR against `flathub:new-pr`. Its template asks whether you are
   the developer (yes), where the app is otherwise published (this repo),
   and about permissions — the manifest's comments give the reasons.
3. The Flathub bot posts a test-build link; reviewers then look at
   permissions, metainfo and licensing. Points to expect: Wine and yabridge
   are downloaded at run time (Bottles is the precedent), Native Access is
   user-supplied, `--allow=devel` is for Wine's exception handling, and the
   name/icon must not suggest NI affiliation (the metainfo disclaims it).
4. After the merge you get a `flathub/io.github.dguedry.nilinux` repository;
   future releases are PRs there bumping the tag/commit.

Lint locally before every submission (this is what Flathub's bot runs):
```bash
flatpak install flathub org.flatpak.Builder
L="flatpak run --command=flatpak-builder-lint org.flatpak.Builder"
$L manifest flatpak/io.github.dguedry.nilinux.yml
$L appstream data/io.github.dguedry.nilinux.metainfo.xml
# repo check: build with Flathub's own builder, cache disabled (the cached
# finish stage ignores the mirroring flags), mirroring screenshots
cd flatpak && flatpak run --command=flatpak-builder org.flatpak.Builder --user --force-clean \
  --disable-cache --ccache --repo=repo-flathub --compose-url-policy=full \
  --mirror-screenshots-url=https://dl.flathub.org/media/ build-flathub io.github.dguedry.nilinux.yml
ostree commit --repo=repo-flathub --canonical-permissions --branch=screenshots/x86_64 \
  build-flathub/files/share/app-info/media
$L repo repo-flathub
```

Submission: <https://github.com/flathub/flathub/pull/10121> (2026-09-07).

## Tested

- The author's machine (Ubuntu-based).
- A fresh Fedora 43 Workstation VM: no Steam, no wine, GNOME on Wayland —
  environment setup in 97 s, Native Access and the GUI render, sync works.
- Native Access 3.23.0 and 3.25.2 (`KNOWN_GOOD` in `native_access.py`);
  Kontakt 8 Player 8.12.1, Kontakt 6.6.1 (its installer runs unmodified),
  Scarbee Mark I, Butch Vig Drums.
