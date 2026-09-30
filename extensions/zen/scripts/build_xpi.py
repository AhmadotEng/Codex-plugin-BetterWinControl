"""Reproducible unsigned XPI: stable entry order, timestamp, permissions and compression."""
from pathlib import Path
import hashlib
import json
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "extension"


def build(destination=None):
    manifest = json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))
    destination = Path(destination or ROOT / "dist" / f"betterwincontrol-zen-{manifest['version']}-unsigned.xpi")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for file in sorted(SOURCE.rglob("*")):
            if not file.is_file() or file.is_symlink():
                continue
            entry = zipfile.ZipInfo(file.relative_to(SOURCE).as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, file.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return {"path":str(destination),"sha256":hashlib.sha256(destination.read_bytes()).hexdigest(),"bytes":destination.stat().st_size,"signed":False}


if __name__ == "__main__":
    result = build()
    (ROOT / "dist" / "build.json").write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(result))
