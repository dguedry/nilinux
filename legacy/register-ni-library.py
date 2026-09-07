#!/usr/bin/env python3
r"""Register an installed NI content library in HKLM so Kontakt shows it.

Native Access's content install (NTK Daemon 1.31.x, "Slim Deployment") copies
the library and writes installed_products/<Name>.json, but under Wine it does
not always write the HKLM\SOFTWARE\Native Instruments\<Name> key that Kontakt
scans at startup (Butch Vig Drums, 2026-09-05). Kontakt then knows the library
exists (writes its own HKCU "UserRemoved" flag) but never lists it.

This script rebuilds that key from the two files NA does write:
  ContentDir / ContentVersion  <- Public Documents\Native Instruments\installed_products\<Name>.json
  HU / JDX                     <- Service Center\NativeAccess.xml (<ProductSpecific>)

Usage: register-ni-library.py "<Library Name>" [bottle]        (default bottle: Music)
       register-ni-library.py --all [bottle]   # every installed_products entry lacking a key
Restart Kontakt afterwards — it scans libraries only at startup.
"""
import json, os, subprocess, sys, xml.etree.ElementTree as ET

args = [a for a in sys.argv[1:] if not a.startswith('--')]
do_all = '--all' in sys.argv
if not args and not do_all:
    sys.exit(__doc__)
if do_all:
    bottle, name = (args[0] if args else 'Music'), None
else:
    name, bottle = args[0], (args[1] if len(args) > 1 else 'Music')

home = os.path.expanduser('~')
B = f'{home}/.var/app/com.usebottles.bottles/data/bottles/bottles/{bottle}'
IP = f'{B}/drive_c/users/Public/Documents/Native Instruments/installed_products'
XML = f'{B}/drive_c/Program Files/Common Files/Native Instruments/Service Center/NativeAccess.xml'
RUN = f'{home}/.local/share/Steam/ubuntu12_32/steam-runtime/run.sh'
REG = r'C:\windows\system32\reg.exe'   # explicit 64-bit reg.exe -> 64-bit HKLM view

def runner():
    for line in open(f'{B}/bottle.yml'):
        if line.startswith('Runner:'):
            r = line.split(':', 1)[1].strip()
            for w in ('wine64', 'wine'):
                p = f'{home}/.var/app/com.usebottles.bottles/data/bottles/runners/{r}/files/bin/{w}'
                if os.path.exists(p): return p
    sys.exit('runner wine not found')
WINE = runner()
env = dict(os.environ, WINEPREFIX=B, WINEFSYNC='1', WINEDEBUG='-all')

def wine(*a):
    return subprocess.run([RUN, WINE, REG, *a], env=env, capture_output=True, text=True).stdout

hints = {p.findtext('Name'): p for p in ET.parse(XML).getroot().findall('Product')}

def register(n):
    key = rf'HKLM\Software\Native Instruments\{n}'
    if 'ContentDir' in wine('query', key):
        print(f'{n}: already registered'); return
    info = json.load(open(f'{IP}/{n}.json'))
    p = hints.get(n)
    if p is None: sys.exit(f'{n}: not in NativeAccess.xml')
    vals = {'ContentDir': info['ContentDir'], 'ContentVersion': info.get('ContentVersion', '1.0.0')}
    for k in ('HU', 'JDX'):
        v = p.findtext(f'ProductSpecific/{k}')
        if v: vals[k] = v
    for k, v in vals.items():
        wine('add', key, '/v', k, '/t', 'REG_SZ', '/d', v, '/f')
    wine('add', key, '/v', 'Visibility', '/t', 'REG_DWORD', '/d', '3', '/f')
    print(f'{n}: registered ->', wine('query', key).strip().replace('\n', '\n  '))

names = [name] if name else [f[:-5] for f in os.listdir(IP) if f.endswith('.json')
         and hints.get(f[:-5]) is not None and hints[f[:-5]].findtext('Type') == 'Content']
for n in names: register(n)
