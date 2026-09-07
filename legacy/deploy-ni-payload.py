#!/usr/bin/env python3
"""Deploy an NI product's extracted FileBag payload into a Wine bottle.

NI's InstallAware installers fail under Wine (see README). This tool does
the file-copy phase manually, joining the inner MSI's File -> Component ->
Directory tables against the destination roots the wizard would have used.

Workflow (per product):
  1. 7z x "<Product> Setup PC.exe"        -> gives "<Product> Setup PC.msi"
                                             and the payload under data/
  2. Run the setup SILENTLY under an MSI trace — no clicking needed:
       WINEPREFIX=<bottle> WINEFSYNC=1 WINEDEBUG=+msi wine "<Product> Setup PC.exe" /s 2> trace.log
     The trace contains INSERT INTO Property ... VALUES ('P<hash>_1', '<dest>')
     lines mapping every payload bag to its destination, plus the registry
     keys the installer would write. Pass it as TRACE=trace.log and ROOTS is
     derived automatically (the hand-written ROOTS below is only a fallback).
  3. Dump the MSI tables next to this script:
       python3 msidump.py "<msi>" Directory > tbl_Directory.tsv
       python3 msidump.py "<msi>" Component > tbl_Component.tsv
       python3 msidump.py "<msi>" File      > tbl_File.tsv
  4. (Only without TRACE) edit the fallback ROOTS below with the P<hash>_1
     pairs you care about (skip AAX, installer-log and shortcut roots).
  5. Dry run, inspect, then deploy:
       BAG=/path/to/extracted/data DRIVE_C=/path/to/bottle/drive_c \
         python3 deploy-ni-payload.py          # dry run
       ... deploy-ni-payload.py run            # copy

Also import the product's registry keys afterwards (InstallDir, ContentDir,
InstallVST364Dir under HKLM\\SOFTWARE\\Native Instruments\\<Product>) —
they're in the same trace — then run sync-yabridge.sh.

Validated with Kontakt 8 8.12.1 (2026-08-23); the ROOTS below are Kontakt's
as a worked example.
"""
import csv, os, sys, shutil
from functools import lru_cache

S = os.path.dirname(os.path.abspath(__file__))
BAG = os.environ.get('BAG')          # extracted payload's data/ dir
DRIVE_C = os.environ.get('DRIVE_C')  # bottle's drive_c
DO = len(sys.argv) > 1 and sys.argv[1] == 'run'

if not BAG or not DRIVE_C:
    sys.exit("set BAG (extracted data/ dir) and DRIVE_C (bottle drive_c) env vars — see docstring")

TRACE = os.environ.get('TRACE')      # optional: +msi trace of a silent (/s) setup run

SKIP_ROOTS = ('Installer Log', 'Start Menu', '\\Desktop', 'Avid\\Audio', 'ProgramData\\Native Instruments')

def roots_from_trace(path):
    import re
    props = {}
    for m in re.finditer(r"VALUES \( '(P[0-9A-F]+)_(\d+)' , '([^']*)'", open(path, errors='ignore').read()):
        props.setdefault(m.group(1), {})[int(m.group(2))] = m.group(3).replace('\\\\', '\\')
    roots, regkeys = {}, {}
    for bag, v in props.items():
        if v.get(2) == 'REGISTRY KEYS' and 3 in v and 4 in v:
            regkeys[v[3]] = v[4]
        dest = v.get(1, '')
        if dest[:3].upper() == 'C:\\' and not any(x in dest for x in SKIP_ROOTS):
            roots[bag + '_1'] = dest[3:].rstrip('\\').replace('\\', '/')
    return roots, regkeys

REGKEYS = {}
if TRACE:
    ROOTS, REGKEYS = roots_from_trace(TRACE)
    print(f'roots from trace: {len(ROOTS)}')
    for k, v in sorted(ROOTS.items()): print(f'  {k} -> {v}')
    if REGKEYS:
        print('registry keys to apply after deploy (HKLM):')
        for k, v in sorted(REGKEYS.items()): print(f'  {k} = {v}')
else:
  ROOTS = {  # fallback: hand-written roots for P<bag>_1 (from a +msi trace)
   'P622D08AE_1': r'Program Files/Native Instruments/Kontakt 8',
   'P231061AC_1': r'Program Files/Common Files/Native Instruments',
   'P3297BA97_1': r'Program Files/Common Files/VST3',
   'P7E4823C1_1': r'Program Files/Common Files/Native Instruments',
   'P9BC80AE4_1': r'Program Files/Native Instruments/Kontakt 8',
  }

def rows(t):
    with open(f'{S}/tbl_{t}.tsv') as f:
        r = csv.reader(f, delimiter='\t')
        hdr = next(r)
        for row in r:
            yield dict(zip(hdr, row))

def longname(part):
    return part.split('|', 1)[-1]

dirs = {}
for d in rows('Directory'):
    dirs[d['Directory']] = (d['Directory_Parent'], d['DefaultDir'])

@lru_cache(maxsize=None)
def resolve(key):
    """returns (targetpath, sourcepath) relative, or None if outside our roots"""
    if key in ROOTS:
        return (ROOTS[key], '')
    if key not in dirs:
        return None
    parent, dd = dirs[key]
    if not parent or key == 'TARGETDIR':
        return None
    base = resolve(parent)
    if base is None:
        return None
    if ':' in dd:
        tpart, spart = dd.split(':', 1)
    else:
        tpart = spart = dd
    t, s = longname(tpart), longname(spart)
    tp = base[0] if t == '.' else os.path.join(base[0], t)
    sp = base[1] if s == '.' else os.path.join(base[1], s)
    return (tp, sp)

comp_dir = {c['Component']: c['Directory_'] for c in rows('Component')}

copied = skipped = missing = 0
total = 0
samples = []
for f in rows('File'):
    d = comp_dir.get(f['Component_'])
    if not d: skipped += 1; continue
    r = resolve(d)
    if r is None: skipped += 1; continue
    tdir, sdir = r
    name = longname(f['FileName'])
    src = os.path.join(BAG, sdir, name)
    dst = os.path.join(DRIVE_C, tdir, name)
    if not os.path.exists(src):
        missing += 1
        if missing <= 5: print('MISSING:', src)
        continue
    total += os.path.getsize(src)
    if len(samples) < 8: samples.append((src.replace(BAG,''), dst.replace(DRIVE_C,'')))
    copied += 1
    if DO:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if not os.path.exists(dst):
            shutil.copy2(src, dst)

print(f'files={copied} skipped={skipped} missing={missing} bytes={total/1e9:.2f}GB mode={"RUN" if DO else "dry"}')
for s in samples: print(' ', s[0], '->', s[1])
