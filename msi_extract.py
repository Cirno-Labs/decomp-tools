"""Extracts an MSI's files to their install paths without running it.

The tables are exported with Wine's msidb, the cabinets unpacked with 7z.

    msi_extract.py TABLES CABDIR OUT [PREFIX...]

TABLES holds File.idt, Component.idt and Directory.idt. Only files whose
install path (relative to the MSI root) starts with one of PREFIX are
extracted.
"""
import os, shutil, subprocess, sys, tempfile


def read_idt(path):
    with open(path, encoding="cp1252") as f:
        lines = f.read().splitlines()
    cols = lines[0].split("\t")
    return [dict(zip(cols, l.split("\t"))) for l in lines[3:] if l]


def long_name(name):
    return name.split("|")[-1]


def main():
    tables, cabdir, out, *prefixes = sys.argv[1:]
    dirs = {d["Directory"]: d for d in read_idt(os.path.join(tables, "Directory.idt"))}
    comps = {c["Component"]: c for c in read_idt(os.path.join(tables, "Component.idt"))}
    files = read_idt(os.path.join(tables, "File.idt"))

    def dir_path(key):
        d = dirs[key]
        target = d["DefaultDir"].split(":")[0]
        name = "" if target in (".", "SOURCEDIR") else long_name(target)
        parent = d["Directory_Parent"]
        if not parent or parent == key:
            return ""
        return os.path.join(dir_path(parent), name)

    wanted = {}
    for f in files:
        path = os.path.join(dir_path(comps[f["Component_"]]["Directory_"]), long_name(f["FileName"]))
        if any(path.lower().startswith(p.lower()) for p in prefixes):
            wanted[f["File"]] = path

    with tempfile.TemporaryDirectory() as tmp:
        for cab in sorted(os.listdir(cabdir)):
            if cab.lower().endswith(".cab"):
                subprocess.run(["7z", "x", "-y", "-o" + tmp, os.path.join(cabdir, cab)],
                               check=True, stdout=subprocess.DEVNULL)
        missing = 0
        for key, path in sorted(wanted.items(), key=lambda kv: kv[1]):
            src = os.path.join(tmp, key)
            if not os.path.exists(src):
                missing += 1
                print("missing", path, file=sys.stderr)
                continue
            dst = os.path.join(out, path)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
    print(f"{len(wanted) - missing} files extracted, {missing} missing")


if __name__ == "__main__":
    main()
