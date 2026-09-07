#!/usr/bin/env python3
"""Dump a table from an MSI database as TSV (no Windows/msitools needed).

Usage: python3 msidump.py <package.msi> <TableName>     (requires: pip install olefile)
Common tables: Directory, Component, File, Registry, Property, CreateFolder.
Used with deploy-ni-payload.py to manually install NI products whose
InstallAware installers fail under Wine — see README.
"""
import olefile, struct, sys

def mschar(x):
    if x < 10: return chr(x + ord('0'))
    if x < 36: return chr(x - 10 + ord('A'))
    if x < 62: return chr(x - 36 + ord('a'))
    return '.' if x == 62 else '_'

def decode_name(name):
    out = []
    for ch in name:
        c = ord(ch)
        if 0x3800 <= c < 0x4800:
            c -= 0x3800
            out.append(mschar(c & 0x3f) + mschar((c >> 6) & 0x3f))
        elif 0x4800 <= c < 0x4840:
            out.append(mschar(c - 0x4800))
        elif c == 0x4840:
            out.append('!')
        else:
            out.append(ch)
    return ''.join(out)

ole = olefile.OleFileIO(sys.argv[1])
streams = {}
for entry in ole.listdir():
    streams[decode_name(entry[0])] = entry[0]

def read(n): return ole.openstream(streams[n]).read()

pool = read('!_StringPool'); data = read('!_StringData')
strings = ['']; off = 0; i = 4
while i < len(pool):
    size, refs = struct.unpack_from('<HH', pool, i); i += 4
    if size == 0 and refs != 0:
        size, = struct.unpack_from('<I', pool, i); i += 4
    strings.append(data[off:off+size].decode('utf-8', 'replace')); off += size

# schema from _Columns: Table(str), Number(int16), Name(str), Type(int16)
cols_raw = read('!_Columns')
n = len(cols_raw) // 8  # 4 columns x 2 bytes
schema = {}
for r in range(n):
    t, = struct.unpack_from('<H', cols_raw, 2*(0*n + r))
    num, = struct.unpack_from('<H', cols_raw, 2*(1*n + r))
    name, = struct.unpack_from('<H', cols_raw, 2*(2*n + r))
    typ, = struct.unpack_from('<H', cols_raw, 2*(3*n + r))
    schema.setdefault(strings[t], []).append((num & 0x7fff, strings[name], typ))

def dump(table):
    cols = sorted(schema[table])
    d = read('!' + table)
    # compute row size: string/short=2, int32=4 (type & 0x0800? MSI: type bit 0x0800 = string; width in low bits)
    widths = []
    for _, name, typ in cols:
        if typ & 0x0800: widths.append(2)          # string ref
        elif (typ & 0xff) == 4: widths.append(4)    # int32
        else: widths.append(2)                      # int16
    rowsz = sum(widths)
    rows = len(d) // rowsz
    out = []
    offc = 0
    colvals = []
    for w in widths:
        vals = []
        for r in range(rows):
            if w == 2:
                v, = struct.unpack_from('<H', d, offc + 2*r)
            else:
                v, = struct.unpack_from('<I', d, offc + 4*r)
            vals.append(v)
        offc += w * rows
        colvals.append(vals)
    for r in range(rows):
        row = []
        for ci, (_, name, typ) in enumerate(cols):
            v = colvals[ci][r]
            if typ & 0x0800:
                row.append(strings[v] if v < len(strings) else f'#{v}')
            else:
                row.append(v)
        out.append(row)
    return [name for _, name, _ in cols], out

table = sys.argv[2]
hdr, rows = dump(table)
print('\t'.join(hdr))
for row in rows:
    print('\t'.join(str(x) for x in row))
