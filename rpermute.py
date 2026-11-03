"""Run permute.py on the configured SSH machine and retrieve candidates.

Usage: rpermute.py NAME [permute.py options]
The remote root must be a dedicated working copy with its toolchain and
original executable installed. RPERMUTE_ROOT overrides that directory.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import project
from permute import stem

SSH = ["ssh", "-o", "BatchMode=yes", "-o", "LogLevel=ERROR"]


def main() -> int:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    os.chdir(project.ROOT)
    remote = project.config["remote"]
    root = os.environ.get("RPERMUTE_ROOT", remote["root"]).rstrip("/\\")
    host = remote["host"]
    if not host or not root:
        sys.exit("project.toml needs [remote] host and root")
    win = root.replace("/", "\\")
    paths = [Path("build/permute") / (stem(sys.argv[1]) + ext) for ext in (".c", ".file")]
    sent = ["src", "config", "project.toml", "tools"] + [str(p) for p in paths if p.exists()]
    with subprocess.Popen(["tar", "-cf", "-", "--exclude=.git", "--exclude=__pycache__", *sent],
                          stdout=subprocess.PIPE) as archive:
        subprocess.run(SSH + [host, f'rmdir /s /q "{win}\\src" 2>nul & tar -xf - -C "{root}"'],
                       stdin=archive.stdout, check=True)
        if archive.wait():
            sys.exit("source archive failed")
    args = subprocess.list2cmdline(sys.argv[1:])
    done = subprocess.run(SSH + [host, f'cd /d "{win}" && set PYTHONUTF8=1&& '
                                f'{remote["python"]} tools\\permute.py {args}'])
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=path.parent) as temporary:
            fetched = Path(temporary) / path.name
            copied = subprocess.run(["scp", "-q", "-o", "BatchMode=yes", "-o", "LogLevel=ERROR",
                                     f"{host}:{root}/{path.as_posix()}", str(fetched)],
                                    stderr=subprocess.DEVNULL)
            if copied.returncode == 0:
                os.replace(fetched, path)
    return done.returncode


if __name__ == "__main__":
    sys.exit(main())
