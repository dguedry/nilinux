#!/usr/bin/env python3
"""Patch Native Access so an *installed* Kontakt/Reaktor satisfies a library's
dependency check.

NA's renderer picks the first OWNED, non-Player product from a library's
"viable" dependency list and only falls back to a Player when none is owned.
Owning e.g. Kontakt 7 via Komplete while only Kontakt 8 Player is installed
therefore triggers the "Requires Kontakt ... no viable version installed"
modal (see README). This patch makes an installed viable product win.

It rewrites resources/app.asar in place (index.js entry replaced, offsets and
per-file integrity recomputed, unpacked entries untouched) and updates the
asar header hash stored in the exe's "Integrity" resource, since NA ships
with Electron's EnableEmbeddedAsarIntegrityValidation fuse on.

Usage: patch-na-dependency-check.py [bottle] [--dry-run]
Backups: resources/app.asar.orig, Native Access.exe.pre-deppatch
"""
import hashlib, json, os, re, shutil, struct, sys

MARK = '/*NA_DEP_PATCH*/'
BLOCK = 4 * 1024 * 1024

args = [a for a in sys.argv[1:] if not a.startswith('--')]
dry = '--dry-run' in sys.argv
bottle = args[0] if args else 'Music'
nadir = os.path.expanduser(f'~/.var/app/com.usebottles.bottles/data/bottles/bottles/{bottle}/drive_c/Program Files/Native Instruments/Native Access')
asar = os.path.join(nadir, 'resources', 'app.asar')
exe = os.path.join(nadir, 'Native Access.exe')

# ---- parse asar -----------------------------------------------------------
with open(asar, 'rb') as f:
    hdr = f.read(16)
    json_len = struct.unpack_from('<I', hdr, 12)[0]
    hjson = f.read(json_len)
    header = json.loads(hjson.rstrip(b'\0'))
    aligned = (json_len + 3) & ~3
    base = 16 + aligned
    f.seek(0)
    data = f.read()
old_hdr_hash = hashlib.sha256(hjson).hexdigest()

entries = []  # (path, node) for packed files
def walk(node, path):
    for k, v in node.get('files', {}).items():
        p = f'{path}/{k}' if path else k
        if 'files' in v:
            walk(v, p)
        elif not v.get('unpacked'):
            entries.append((p, v))
walk(header, '')

targets = [e for e in entries if re.fullmatch(r'out/renderer/assets/index-[\w-]+\.js', e[0])]
if len(targets) != 1:
    sys.exit(f'expected one renderer index.js, found {[t[0] for t in targets]}')
tpath, tnode = targets[0]
off, size = int(tnode['offset']), tnode['size']
js = data[base + off: base + off + size].decode('utf-8')

if MARK in js:
    print('already patched:', tpath); sys.exit(0)

# owned[0] ?? players[0]  ->  prefer an installed one from either list
pat = re.compile(r'(_0x[0-9a-f]+)=(_0x[0-9a-f]+)\[0x0\]\?\?(_0x[0-9a-f]+)\[0x0\],(_0x[0-9a-f]+)=\2\[_0x[0-9a-f]+\(0x[0-9a-f]+\)\]\((_0x[0-9a-f]+)=>\5\[')
ms = list(pat.finditer(js))
if len(ms) != 1:
    sys.exit(f'patch site not found exactly once ({len(ms)} matches) — NA version changed, re-derive the pattern')
m = ms[0]
cand, owned, players = m.group(1), m.group(2), m.group(3)
new = f'{cand}={MARK}{owned}.find(p=>p.isInstalled)??{players}.find(p=>p.isInstalled)??{owned}[0x0]??{players}[0x0]'
old = f'{cand}={owned}[0x0]??{players}[0x0]'
print(f'{tpath}: {old}  ->  {new}')
js2 = js.replace(old, new, 1).encode('utf-8')
if dry:
    print('dry run, nothing written'); sys.exit(0)

# ---- rebuild archive --------------------------------------------------------
def integrity(b):
    return {'algorithm': 'SHA256', 'hash': hashlib.sha256(b).hexdigest(), 'blockSize': BLOCK,
            'blocks': [hashlib.sha256(b[i:i + BLOCK]).hexdigest() for i in range(0, len(b), BLOCK)]}

entries.sort(key=lambda e: int(e[1]['offset']))
out_chunks, cur = [], 0
for p, node in entries:
    if p == tpath:
        blob = js2
        node['integrity'] = integrity(blob)
    else:
        o = int(node['offset']); blob = data[base + o: base + o + node['size']]
    node['offset'] = str(cur); node['size'] = len(blob)
    out_chunks.append(blob); cur += len(blob)

hj = json.dumps(header, separators=(',', ':')).encode('utf-8')
new_hdr_hash = hashlib.sha256(hj).hexdigest()
pad = ((len(hj) + 3) & ~3) - len(hj)
head = struct.pack('<IIII', 4, len(hj) + pad + 8, len(hj) + pad + 4, len(hj))

orig = asar + '.orig'
if not os.path.exists(orig):
    shutil.copy2(asar, orig)
tmp = asar + '.tmp'
with open(tmp, 'wb') as f:
    f.write(head); f.write(hj); f.write(b'\0' * pad)
    for c in out_chunks: f.write(c)
os.replace(tmp, asar)
print(f'wrote {asar} ({cur/1e6:.1f} MB payload); backup at {orig}')

# ---- update exe Integrity resource -------------------------------------------
with open(exe, 'rb') as f: ed = f.read()
needle = old_hdr_hash.encode()
n = ed.count(needle)
if n < 1:
    sys.exit(f'old header hash {old_hdr_hash} not found in exe — asar/exe mismatch, restore app.asar.orig')
exb = exe + '.pre-deppatch'
if not os.path.exists(exb):
    shutil.copy2(exe, exb)
with open(exe, 'wb') as f: f.write(ed.replace(needle, new_hdr_hash.encode()))
print(f'exe Integrity resource: {old_hdr_hash[:12]}… -> {new_hdr_hash[:12]}… ({n} occurrence{"s" if n>1 else ""}); backup at {exb}')
