#!/usr/bin/env python3
"""Verify expanded source snapshots and current package metadata, offline."""
import argparse, hashlib, json, shutil
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def verify():
 for name,expected in json.loads((ROOT/'PACKAGE_SHA256.json').read_text())['files'].items():
  assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==expected,name
 manifest=json.loads((ROOT/'SOURCE_MANIFEST.json').read_text())
 for snap in manifest['snapshots']:
  folder=ROOT/snap['directory']
  expected={f['path']:f for f in snap['files']}
  actual={p.relative_to(folder).as_posix() for p in folder.rglob('*') if p.is_file()}
  assert actual==set(expected),(snap['id'],'file inventory mismatch')
  for name,f in expected.items():
   b=(folder/name).read_bytes()
   assert len(b)==f['bytes'] and hashlib.sha256(b).hexdigest()==f['sha256'],name
  print(f"Verified {snap['id']}: {len(expected)} unchanged source files")
 return manifest

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--snapshot');p.add_argument('--destination',type=Path,default=Path('extracted'));a=p.parse_args()
 manifest=verify()
 if a.snapshot:
  snap=next((s for s in manifest['snapshots'] if s['id']==a.snapshot),None)
  if snap is None:p.error('Unknown snapshot')
  target=a.destination.resolve()/a.snapshot
  if target.exists():p.error(f'Refusing to overwrite {target}')
  shutil.copytree(ROOT/snap['directory'],target)
  print(f'Copied {target}')
if __name__=='__main__':main()
