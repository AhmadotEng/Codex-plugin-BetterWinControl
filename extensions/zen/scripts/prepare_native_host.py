"""Stage a Firefox native-host manifest/launcher; never register or install it."""
from pathlib import Path
import argparse
import hashlib
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "native"))
from bridge import EXTENSION_ID


def batch_path(path):
    text = str(Path(path).resolve())
    # Quotes/percent/newlines change cmd.exe parsing even inside quoted arguments.
    if any(char in text for char in '"%\r\n\0'):
        raise ValueError("path_not_safe_for_batch_launcher")
    return text


def prepare(python, config, output):
    python, config = batch_path(python), batch_path(config)
    host = batch_path(ROOT / "native" / "native_host.py")
    if not Path(python).is_file():
        raise FileNotFoundError("python_executable_not_found")
    output = Path(output).resolve()
    batch_path(output)
    output.mkdir(parents=True, exist_ok=True)
    launcher = output / "betterwincontrol-zen-native.bat"
    manifest = output / "local.betterwincontrol.zen.json"
    launcher.write_bytes((f'@echo off\r\nsetlocal DisableDelayedExpansion\r\nset "BWC_ZEN_CONFIG={config}"\r\n"{python}" -u "{host}"\r\nexit /b %errorlevel%\r\n').encode("utf-8"))
    data = {"name":"local.betterwincontrol.zen", "description":"BetterWinControl optional Zen adapter", "path":str(launcher), "type":"stdio", "allowed_extensions":[EXTENSION_ID]}
    manifest.write_text(json.dumps(data,indent=2)+"\n",encoding="utf-8")
    hashes = {str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in [Path(python),Path(host),ROOT/"native"/"bridge.py",launcher,manifest]}
    metadata = {"registered":False,"configCreated":False,"manifest":str(manifest),"launcher":str(launcher),"python":python,"hostScript":host,"config":config,"sha256":hashes}
    (output/"staging.json").write_text(json.dumps(metadata,indent=2)+"\n",encoding="utf-8")
    return metadata


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python",required=True)
    parser.add_argument("--config",required=True)
    parser.add_argument("--output",default=str(ROOT/"dist"/"native-host"))
    args=parser.parse_args()
    print(json.dumps(prepare(args.python,args.config,args.output),indent=2))
