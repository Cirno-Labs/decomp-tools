"""Fails if the README names a repository path that doesn't exist.

    readme_paths.py [README]

A path is a `backticked` token or a link target containing a slash, or an
entry of the Layout section's code block. Only paths inside the
repository's tracked tree are checked: the first component must be a
tracked top-level entry, and gitignored locations (build/, the game files
under orig/, the Wine prefix) are skipped, so build outputs, paths inside
install media and the user's own files never count. The pre-commit hook
runs it with hooks.readme in project.toml.
"""
import os
import re
import subprocess
import sys

TOKEN = re.compile(r"`([^`\s]+)`|\]\(([^)\s]+)\)")
LAYOUT = re.compile(r"^## Layout\s*\n```\n(.*?)\n```", re.M | re.S)
ENTRY = re.compile(r"(?<![\w./-])((?:[\w.-]+/)+[\w.-]*)")


def git(root, *args):
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True).stdout


def candidates(text):
    """The path-like strings the README names, with their line numbers."""
    found = []
    for n, line in enumerate(text.split("\n"), 1):
        for m in TOKEN.finditer(line):
            found.append((n, m.group(1) or m.group(2)))
    layout = LAYOUT.search(text)
    if layout:
        first = text[:layout.start(1)].count("\n") + 1
        for n, line in enumerate(layout.group(1).split("\n"), first):
            found += [(n, m.group(1)) for m in ENTRY.finditer(line)]
    out = []
    for n, path in found:
        path = path.split("#")[0].rstrip(".,:;")
        if ("/" not in path or "://" in path or path.startswith(("/", "~", "$", "-"))
                or any(c in path for c in "<>*{}\\|?=")):
            continue
        out.append((n, path))
    return out


def missing(root, readme):
    tracked = set(git(root, "ls-files").split("\n")) - {""}
    tops = {p.split("/")[0] for p in tracked}
    tops |= {line.split()[1] for line in git(root, "submodule", "status").split("\n") if line.strip()}
    out = []
    for n, path in dict.fromkeys(candidates(open(readme, encoding="utf-8").read())):
        rel = os.path.normpath(path)
        if rel.startswith("..") or rel.split(os.sep)[0] not in tops:
            continue
        if subprocess.run(["git", "-C", root, "check-ignore", "-q", rel],
                          stderr=subprocess.DEVNULL).returncode == 0:
            continue
        if not os.path.exists(os.path.join(root, rel)):
            out.append((n, path))
    return out


def main():
    root = git(".", "rev-parse", "--show-toplevel").strip() or "."
    readme = sys.argv[1] if len(sys.argv) > 1 else os.path.join(root, "README.md")
    if not os.path.exists(readme):
        return
    bad = missing(root, readme)
    for n, path in bad:
        print(f"{os.path.relpath(readme)}:{n}: names {path}, which doesn't exist")
    if bad:
        sys.exit(1)


if __name__ == "__main__":
    main()
