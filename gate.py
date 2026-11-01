"""The commit rule the pre-commit hook checks when project.toml's
[hooks] turns it on.

    gate.py pre-commit    hooks.compare: every compiled function matches

hooks.compare builds src/ and fails the commit unless every function it
defines matches the original byte for byte (`just compare`), and unless
every function HEAD's docs/progress.svg shows as matching is still
compiled: a change to an inline function or a struct can make the
compiler inline a function everywhere, so that its out-of-line copy is
no longer emitted and silently drops out of the compare (that part
needs hooks.progress). src/ and config/ must have no unstaged changes,
so what is built is what is committed.

hooks.allow_diff lists functions, by their symbols.csv name, that are in
src/ though they don't match, such as ones that are logically equivalent
but allocate registers differently. They may differ in every commit. A
commit that makes one match takes it off the list.

A group of functions that only match together lands one function per
commit with DECOMP_ALLOW_DIFF=NAME[,NAME...] in the environment: the
named functions may differ in that commit (the neighbour the group
breaks until it is complete). `git commit --no-verify` skips the hook.
"""
import csv, os, subprocess, sys, tempfile

import project

ROOT = project.ROOT
HOOKS = project.config["hooks"]
SVG = "docs/progress.svg"


def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=check)


def rows():
    """symbol: (address, name) from config/symbols.csv."""
    with open(os.path.join(ROOT, "config/symbols.csv")) as f:
        return {s: (int(r["address"], 16), r["name"]) for r in csv.DictReader(f) for s in r["symbol"].split()}


def matched_at_head():
    """The addresses HEAD's progress image shows as matching, or None
    without one."""
    import render_progress
    shown = git("show", f"HEAD:{SVG}", check=False)
    if shown.returncode:
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".svg", delete=False) as f:
        f.write(shown.stdout)
    try:
        return {a for a, same in render_progress.built_from_svg(f.name).items() if same}
    finally:
        os.remove(f.name)


def pre_commit():
    if not HOOKS["compare"]:
        return 0
    if git("diff", "--quiet", "--", "src", "config", check=False).returncode:
        print("pre-commit: src/ or config/ has unstaged changes; stage them or stash them")
        return 1
    built = subprocess.run([sys.executable, os.path.join(ROOT, "tools/build.py")], cwd=ROOT,
                           capture_output=True, text=True)
    if built.returncode:
        print(built.stdout + built.stderr)
        print("pre-commit: the build failed")
        return 1
    import compare
    symbols, names = compare.read_symbols(), rows()
    differ, compiled = [], set()
    for name, same, *_ in compare.results(symbols):
        compiled.add(names[name][0])
        if not same:
            differ.append(name)
    allowed = set(HOOKS["allow_diff"]) | set(os.environ.get("DECOMP_ALLOW_DIFF", "").replace(",", " ").split())
    unknown = sorted(set(HOOKS["allow_diff"]) - {n for _, n in names.values()})
    if unknown:
        print("pre-commit: hooks.allow_diff names functions symbols.csv doesn't:", ", ".join(unknown))
        return 1
    excused = [name for name in differ if names[name][1] in allowed]
    differ = [name for name in differ if names[name][1] not in allowed]
    if excused and os.environ.get("DECOMP_ALLOW_DIFF"):
        print(f"pre-commit: {len(excused)} functions differ, allowed by hooks.allow_diff or DECOMP_ALLOW_DIFF")
    by_address = {a: n for a, n in names.values()}
    gone = sorted(a for a in (matched_at_head() or set()) - compiled if by_address.get(a) not in allowed)
    if gone:
        print("pre-commit: these functions matched at HEAD but aren't compiled any more")
        print("(inlined everywhere now, or their definition went missing):")
        for address in gone:
            print(f"  {address:#010x} {by_address.get(address, '?')}")
        return 1
    if differ:
        print("pre-commit: these functions don't match the original (just compare);")
        print("hooks.allow_diff in project.toml lists the ones that may differ:")
        for name in differ:
            print(f"  {names[name][0]:#010x} {names[name][1]}")
        return 1
    return 0


def main():
    os.chdir(ROOT)
    if sys.argv[1:] == ["pre-commit"]:
        return pre_commit()
    sys.exit(__doc__)


if __name__ == "__main__":
    sys.exit(main())
