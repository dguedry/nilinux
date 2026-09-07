"""nilinux — run Native Instruments' Native Access and its products on Linux.

Owns a Wine prefix (no Bottles dependency), applies every fix documented in
the project README, installs NI products and third-party VSTs, and bridges
them to Linux DAWs with yabridge. The CLI (nilinux.cli) and the GUI are thin
layers over the functions in this package; every function reports progress
through a nilinux.progress.Reporter and returns plain data.
"""
__version__ = "0.1.0"
APP_NAME = "nilinux"
