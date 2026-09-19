"""Expand the original dissertation handover without changing frozen source bytes."""
from pathlib import Path, PurePosixPath
import hashlib, json, shutil, stat, zipfile
ROOT = Path.cwd()
ARCHIVE = ROOT / '2026_SiqiZhang_ProjectMemoryAgentCoordination_CODE_HANDOVER.zip'
assert hashlib.sha256(ARCHIVE.read_bytes()).hexdigest() == '29f0e8ea07a8431077eaae2a091905190823952f5f92c9b9546ec26ed3a3f69f'
assert not (ROOT / 'code').exists(), 'Refusing to overwrite existing code'
with zipfile.ZipFile(ARCHIVE) as z:
    for item in z.infolist():
        p = PurePosixPath(item.filename)
        assert not p.is_absolute() and '..' not in p.parts
        assert not stat.S_ISLNK(item.external_attr >> 16)
        if item.is_dir(): continue
        relative = Path(*p.parts[1:])
        target = ROOT / ('HANDOVER_README.md' if str(relative) == 'README.md' else relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(z.read(item))
code = ROOT / 'code'
manifest = json.loads((code / 'SOURCE_MANIFEST.json').read_text())
count = 0
for snap in manifest['snapshots']:
    archive = code / snap['archive']
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == snap['archive_sha256']
    with zipfile.ZipFile(archive) as z:
        expected = {snap['id'] + '/' + f['path']: f for f in snap['files']}
        assert set(z.namelist()) == set(expected)
        for name, f in expected.items():
            p = PurePosixPath(name)
            assert not p.is_absolute() and '..' not in p.parts
            item = z.getinfo(name)
            assert not stat.S_ISLNK(item.external_attr >> 16)
            data = z.read(name)
            assert len(data) == f['bytes'] and hashlib.sha256(data).hexdigest() == f['sha256']
            target = code / 'source_snapshots' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod((item.external_attr >> 16) & 0o777)
            count += 1
    archive.unlink()
    snap['directory'] = 'source_snapshots/' + snap['id']
(code / 'source_archives').rmdir()
(code / 'SOURCE_MANIFEST.json').write_text(json.dumps(manifest, indent=2) + '\n')
original = code / 'docs' / 'original-packaging-tools'
original.mkdir()
for name in ('verify_and_extract.py', 'run_offline_tests.py'):
    shutil.copy2(code / 'tools' / name, original / (name + '.txt'))
shutil.copy2(code / 'PACKAGE_SHA256.json', code / 'docs' / 'ORIGINAL_PACKAGE_SHA256.json')
(code / 'tools' / 'verify_and_extract.py').write_text('''#!/usr/bin/env python3
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
''')
tests=(code/'tools/run_offline_tests.py').read_text().replace('tempfile,zipfile','tempfile,shutil')
tests=tests.replace("with zipfile.ZipFile(ROOT/'source_archives/e4v3_runtime.zip') as z:z.extractall(tmp)","shutil.copytree(ROOT/'source_snapshots/e4v3_runtime',Path(tmp)/'e4v3_runtime')")
(code/'tools/run_offline_tests.py').write_text(tests)
notice='''# Expanded source layout

All six original source archives are expanded in `code/source_snapshots/`.
Every frozen source file is unchanged and verified against SOURCE_MANIFEST.json.
Archive names and checksums in that manifest document the original handover;
the directory fields identify the current, browsable locations.

The root README contains current usage instructions. Historical handover documents
may describe ZIP extraction. The two packaging tools were adapted to directories;
their original text is retained in docs/original-packaging-tools/.
ORIGINAL_PACKAGE_SHA256.json preserves the original package inventory, while
PACKAGE_SHA256.json verifies the current metadata, analysis and packaging tools.
No research source, analysis input, attribution or license was changed.
'''
(code/'docs/EXPANDED_LAYOUT.md').write_text(notice)
readme=(ROOT/'README.md').read_text()
a=readme.index('## Download the complete project')
b=readme.index('## What I worked on')
readme=readme[:a]+'''## Browse the source

[**Main runtime — e4v3_runtime**](code/source_snapshots/e4v3_runtime) · [**All six snapshots**](code/source_snapshots) · [**Analysis**](code/analysis) · [**Documentation**](code/docs)

The complete code is expanded into ordinary files and directories. Start with `e4v3_runtime` for the main portfolio-analysis experiment. Earlier snapshots and separately versioned controls remain available for traceability.

'''+readme[b:]
readme=readme.replace('Download and unzip the package, then open a terminal in its `code` directory:', 'Clone this repository, then open a terminal in its `code` directory:')
readme=readme.replace('unzip 2026_SiqiZhang_ProjectMemoryAgentCoordination_CODE_HANDOVER.zip\ncd 2026_SiqiZhang_ProjectMemoryAgentCoordination/code', 'git clone https://github.com/SiqiZhang77/Agentic-Quantitative-Development-Loop.git\ncd Agentic-Quantitative-Development-Loop/code')
readme=readme.replace('# Verify source archives, source files and packaged analytical inputs.', '# Verify expanded source files and packaged analytical inputs.')
readme=readme.replace('# Extract the main frozen runtime to inspect its implementation.\npython3 tools/verify_and_extract.py --snapshot e4v3_runtime --destination extracted', '# Browse the main runtime directly.\ncd source_snapshots/e4v3_runtime')
readme=readme.replace("The package's `code/README.md` explains", "The historical handover's [code README](code/README.md), together with the [expanded-layout notes](code/docs/EXPANDED_LAYOUT.md), explains")
readme=readme.replace('2026_SiqiZhang_ProjectMemoryAgentCoordination/\n├── README.md', 'Agentic-Quantitative-Development-Loop/\n├── README.md\n├── HANDOVER_README.md')
readme=readme.replace('source_archives/       # Six separately identified frozen source snapshots', 'source_snapshots/      # Six expanded, separately identified source snapshots')
readme=readme.replace('Main runtime entry points after extraction:', 'Main runtime entry points in `code/source_snapshots/e4v3_runtime/`:')
readme=readme.replace('The original handover files are preserved byte-for-byte inside the downloadable package.', 'All frozen source files are preserved byte-for-byte in the expanded snapshot directories. Packaging tools and instructions have been adapted to this layout; original packaging-tool text and checksums are retained for provenance.')
(ROOT/'README.md').write_text(readme)
files={p.relative_to(code).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(code.rglob('*')) if p.is_file() and 'source_snapshots' not in p.parts and p.name!='PACKAGE_SHA256.json'}
(code/'PACKAGE_SHA256.json').write_text(json.dumps({'files':files},indent=2)+'\n')
assert count==3929,count
ARCHIVE.unlink()
assert not list(code.rglob('*.zip')), 'Unexpected remaining nested ZIP'
print(f'Expanded and hash-verified {count} source files across six snapshots.')
