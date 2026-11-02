"""Runs permute.py over many functions at once, unattended, and keeps
the matches it finds.

    farm.py [-p PARALLEL] [-j JOBS] [-t SECONDS] [-r ROUNDS] [NAME...]
    farm.py --learn
    farm.py --apply [NAME...]

The functions default to project.toml's hooks.allow_diff, the ones in
src/ that don't match yet. Each round runs every function still
unmatched through a random search of -t seconds with a fresh seed,
starting from the best variant so far (build/permute/STEM.c, STEM being
the name with `::` as `-`), after `permute.py NAME --sweep` on that
variant whenever it changed since the last sweep, PARALLEL functions at
a time with JOBS compiles each, so PARALLEL x JOBS compiles at once. A
function whose sweep found nothing more is reported as "sweep
exhausted" and listed in build/farm/exhausted.txt: the rewrites can't
reach it, so it needs a person. Logs go to build/farm/STEM.log, and
every lower score is printed as it is found; a body that matches is
kept as build/farm/STEM.c. -r 0 runs rounds until every function
matches or the farm is stopped; a stopped farm keeps what it found.

src/ is never written while searching, so the farm can run on another
machine from a copy of the repository, such as a Windows one, where
build.py runs the compiler without wine (the copy needs src/, config/,
project.toml, tools/, the original exe at game.original and the
toolchain under .wine/drive_c): copy build/farm/ back.

Searches run with permute.py --file, so they may also reorder the
file's top-level declarations; such a match is kept whole as
build/farm/STEM.file.

--learn looks at how each best variant so far differs from src/ and
writes the counts of each kind of rewrite to build/permute/_prior.json,
which every search starts its rewrite weights from; the searches then
add what they learn about each function in build/permute/STEM.stats.json.

--apply writes each kept body over its function's definition in src/,
or the whole file when the match reordered its declarations, so apply
those one at a time. A body is cut down first: a search leaves many
rewrites that changed nothing, so each changed run of lines goes back
to src/'s as long as the function still matches, until none can (one
compile each). Build and compare after it, since a body can flip
other functions, then commit each one, taking it off hooks.allow_diff in
that commit.
"""
import argparse, glob, os, random, re, shutil, subprocess, sys
from concurrent.futures import ThreadPoolExecutor

import project
from permute import stem

ROOT = project.ROOT
os.chdir(ROOT)
OUT = "build/farm"
HERE = os.path.dirname(os.path.abspath(__file__))


def found(name):
    return os.path.join(OUT, f"{stem(name)}.c")


def search(name, args, seed):
    log = open(os.path.join(OUT, f"{stem(name)}.log"), "a")
    permute = [sys.executable, os.path.join(HERE, "permute.py"), name, "-j", str(args.j), "--file"]
    best = os.path.join("build/permute", f"{stem(name)}.c")
    marker = os.path.join(OUT, f"{stem(name)}.swept")
    body = open(best).read() if os.path.exists(best) else None
    steps = [["-t", str(args.t), "-s", str(seed), "--start", best]]
    swept = body is not None and os.path.exists(marker) and open(marker).read() == body
    if not swept:
        steps.insert(0, ["--sweep", "-t", str(args.t), "--start", best])
    exhausted = swept
    written = current = None
    for extra in steps:
        kind = "sweep" if extra[0] == "--sweep" else "random search"
        log.write(f"== {' '.join(extra)}\n")
        log.flush()
        run = subprocess.Popen(permute + extra, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, errors="replace")
        for line in run.stdout:
            log.write(line)
            log.flush()
            m = re.match(r".*, score (\d+) as written$", line)
            if m:
                written = int(m[1])
                current = current if current is not None else written
            m = re.match(r"starting from .*, score (\d+)$", line)
            if m:
                current = min(current, int(m[1])) if current is not None else int(m[1])
            if line.startswith("sweep exhausted"):
                exhausted = True
            m = re.match(r"\s+\d+ tries: score (\d+)$", line)
            if m and (current is None or int(m[1]) < current):
                print(f"{name}: {current} -> {m[1]} ({kind})", flush=True)
                current = int(m[1])
                if kind != "sweep":
                    exhausted = False
        done = subprocess.CompletedProcess(run.args, run.wait())
        if extra[0] == "--sweep" and exhausted and os.path.exists(best):
            open(marker, "w", encoding="utf-8", newline="\n").write(open(best).read())
        if done.returncode == 0:
            body = open(best).read()
            open(found(name), "w", encoding="utf-8", newline="\n").write(body)
            whole = os.path.splitext(best)[0] + ".file"
            if os.path.exists(whole):
                shutil.copy(whole, os.path.join(OUT, f"{stem(name)}.file"))
            print(f"{name}: MATCH ({written} in src/)", flush=True)
            return True
    print(f"{name}: no match this round, best {current} ({written} in src/)"
          f"{', sweep exhausted' if exhausted else ''}", flush=True)
    if exhausted:
        listed = os.path.join(OUT, "exhausted.txt")
        rows = dict(l.split() for l in open(listed)) if os.path.exists(listed) else {}
        rows[name] = str(current)
        open(listed, "w").write("".join(f"{k} {v}\n" for k, v in sorted(rows.items())))
    return False


def learn():
    """Writes build/permute/_prior.json: for each kind of rewrite, in how
    many functions the best variant so far differs from src/ by it, as
    the starting weights of every search."""
    import json
    sys.path.insert(0, HERE)
    import permute
    seen, total = {}, 0
    for name in sorted({r["name"] for r in permute.compare_rows()}):
        path = permute.out_path(name, ".c")
        if not os.path.exists(path):
            continue
        try:
            _, text, start, end = permute.find_function(name)
        except (Exception, SystemExit):
            continue
        if not text:
            continue
        best, ours = open(path, encoding="utf-8").read(), text[start:end]
        if best == ours:
            continue
        total += 1
        kinds = permute.classify(ours, best)
        for kind in kinds:
            seen[kind] = seen.get(kind, 0) + 1
        print(f"{name}: {', '.join(sorted(kinds)) or '?'}")
    prior = {kind: [5 * total, count] for kind, count in seen.items()}
    json.dump(prior, open("build/permute/_prior.json", "w"))
    print(f"{total} improved functions: " + ", ".join(f"{k} {v}" for k, v in sorted(seen.items(), key=lambda x: -x[1])))


def minimize(permute, name, src, text, start, end, body):
    """`body`, a match of `name`, with each run of changed lines put back
    as src/ has it while the function still matches."""
    import difflib
    s = permute.Scorer(name, src, text, start, end)
    old, new = text[start:end].split("\n"), body.split("\n")
    if s.score(body) != 0:
        print(f"{name}: the kept body doesn't match here; written as it is")
        return body
    reverted = True
    while reverted:
        reverted = False
        ops = [op for op in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes() if op[0] != "equal"]
        for _, i1, i2, j1, j2 in reversed(ops):
            trial = new[:j1] + old[i1:i2] + new[j2:]
            if s.score("\n".join(trial)) == 0:
                new, reverted = trial, True
    shutil.rmtree(s.tmp, ignore_errors=True)
    left = sum(op[0] != "equal" for op in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes())
    print(f"{name}: {left} changed runs of lines needed")
    return "\n".join(new)


def apply(names):
    sys.path.insert(0, HERE)
    import build, permute
    build.keep_wineserver()
    build.compile_all()
    for name in names:
        if not os.path.exists(found(name)):
            continue
        src, text, start, end = permute.find_function(name)
        body = open(found(name)).read()
        whole = os.path.join(OUT, f"{stem(name)}.file")
        if os.path.exists(whole):
            done = open(whole, encoding="utf-8").read()
            print(f"{name}: written to {src}, with its declarations reordered")
        else:
            done = text[:start] + minimize(permute, name, src, text, start, end, body) + text[end:]
            print(f"{name}: written to {src}")
        open(src, "w", encoding="utf-8", newline="").write(done)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*")
    ap.add_argument("-p", type=int, default=max(1, (os.cpu_count() or 2) // 4))
    ap.add_argument("-j", type=int, default=4)
    ap.add_argument("-t", type=float, default=1800)
    ap.add_argument("-r", type=int, default=1)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--learn", action="store_true")
    args = ap.parse_args()
    names = args.names or list(project.config["hooks"]["allow_diff"])
    os.makedirs(OUT, exist_ok=True)
    if args.apply:
        apply(names)
        return 0
    if args.learn:
        learn()
        return 0
    import build
    build.keep_wineserver()
    subprocess.run([sys.executable, os.path.join(HERE, "build.py")], check=True, stdout=subprocess.DEVNULL)
    rounds = 0
    while True:
        todo = [n for n in names if not os.path.exists(found(n))]
        if not todo or (args.r and rounds >= args.r):
            break
        rounds += 1
        print(f"round {rounds}: {len(todo)} functions", flush=True)
        with ThreadPoolExecutor(args.p) as pool:
            list(pool.map(lambda n: search(n, args, random.randrange(1 << 30)), todo))
    kept = [n for n in names if os.path.exists(found(n))]
    print(f"{len(kept)} matches kept in {OUT}: {' '.join(kept)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
