"""Plays a game's replays to their end in the original and in rebuilt
executables, and compares the runs calculation by calculation.

    replay.py fetch                    download the record replays, see below
    replay.py info RPY...              show what a replay holds
    replay.py run RPY [OPTIONS]        play a replay in one executable
    replay.py check [RPY...] [OPTIONS] play replays (default: the
                                       manifest's) in several executables,
                                       check and compare them
    replay.py compare TRACE TRACE      compare two traces

The game repository describes its game in the Python file replay.hook in
project.toml names (see HOOK below), and writes the tracer: a DLL, from
the hook's TRACER sources and this directory's trace.c and null_d3d8.c,
that plays one replay and writes the game's state (trace.h). A run
copies an executable into build/replay/, adds the tracer to its imports
and starts it under Wine, on an Xvfb display of its own and in a Wine
prefix of its own, with the game's files, the hook's setup (its
configuration and the replay) and no audio drivers. By default Direct3D
draws nothing and the clock runs as fast as the game does (trace.h).

A run passes when the replay ends by itself and the game gets back to
the title, plays the replay's stages in order, ends each with the score
the replay recorded and at its format's complete-input endpoint, with
the replay's frame counter going up by one at each calculation of a stage
and the game's own calculation counter at each calculation; plus the
hook's checks. `check` runs every executable of each replay at the
same time (the original twice, and the standalone exe, by default) and
compares every run's trace with the first one's, record by record.
Traces, hashes and results go to build/replay/<replay>/.

Options:

    --exe NAME     original, standalone (build/<name>.exe with its map
                   file), or PATH=MAP for another build; check takes it
                   more than once
    --speed N      0 as fast as possible; N times real time
    --render       draw the frames (llvmpipe, on the CPU: much slower)
    --show         use $DISPLAY instead of a display of its own (renders)
    --timeout S    give up on a run after S seconds (default 3600)
    --tail N       calculations to run on the title after the replay
    -j N           runs at the same time (check; default 3)

`fetch` downloads the record replays from the hook's WR_TABLE in Maribel
Hearn's world record table (https://maribelhearn.com/wr) into replays/
and writes replays/manifest.json: where each came from, by whom, and its
hash. A download the hook's Replay rejects (the server may serve an empty
file) is left out, and listed under "skipped" with why. The players made
the replays; keep their provenance.
"""
import argparse, csv, datetime, glob, hashlib, html, importlib.util, json, os, queue, re, shutil, struct
import subprocess, sys, time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Mapping

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import project  # noqa: E402

ROOT = project.ROOT
OUT = os.path.join(ROOT, "build", "replay")
REPLAYS = os.path.join(ROOT, "replays")
MANIFEST = os.path.join(REPLAYS, "manifest.json")
TRACER = os.path.join(OUT, f"{project.NAME}trace.dll")
RUNTIME = [os.path.join(HERE, "trace.c"), os.path.join(HERE, "null_d3d8.c")]
WR_PAGE = "https://maribelhearn.com/wr"

HOOK = """The game's replay hook, replay.hook in project.toml, defines:

    TRACER      the tracer's sources, from the repository's root
    FUNCTIONS   {name: symbol} of the functions the tracer patches, and
    GLOBALS     {name: symbol} of the data it reads: TRACE_ADDR gives it
                each name's address, from symbols.csv or the build's map; a
                tuple gives alternative mangled names, tried in order
    SIZED       names whose sizes TRACE_ADDR also gives, as <name>Size
    WR_TABLE    the game's table on Maribel Hearn's page ("EoSD_table")
    Replay      a class: Replay(path) reads a replay or raises
                ValueError; .label is "<shot> <difficulty>" as the table
                names them, .score the final score, .stages {stage:
                {"score": ..., "frames": ...}} in the order played;
                optional "frame_counts" lists the format's permitted
                playback endpoints when its final recorded input is
                redundant (defaults to the single "frames" value), and
                .describe() lines for `info`
    setup(game_dir, replay)
                writes the game's configuration and the replay into the
                run's directory; returns the tracer's own environment
    stage_failures(stage, recorded, last, end, get)
                optional: more checks of one stage, given the replay's
                record of it, the stage's last calculation record and its
                end record (get(record, field) reads them); returns
                failures
"""

# Trace record kinds (trace.h).
CALC, STAGE_END, STAGE_START, REPLAY_END, TITLE, LIMIT, CRASH = range(1, 8)
KIND_NAMES = {CALC: "calc", STAGE_END: "stage end", STAGE_START: "stage start", REPLAY_END: "replay end",
              TITLE: "title", LIMIT: "limit", CRASH: "crash"}
REQUIRED_FIELDS = ["kind", "seq", "passes", "calcs", "replayFrame", "stage", "flags", "score"]

_hook = None


def hook():
    global _hook
    if _hook is None:
        path = project.config["replay"]["hook"]
        if not path:
            sys.exit("project.toml has no replay.hook\n\n" + HOOK)
        spec = importlib.util.spec_from_file_location("replay_hook", os.path.join(ROOT, path))
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        # Parallel run() callers must never see a partially loaded hook.
        _hook = loaded
    return _hook


def sha1(path):
    with open(path, "rb") as f:
        return hashlib.sha1(f.read()).hexdigest()


def info(paths):
    for path in paths:
        r = hook().Replay(path)
        print(f"{os.path.relpath(path)}: {r.label}")
        for line in r.describe():
            print("  " + line)


# Fetching

def fetch():
    """Downloads the hook's replays of Maribel Hearn's world record table."""
    agent = {"User-Agent": "decomp-tools-replay"}
    page = urllib.request.urlopen(urllib.request.Request(WR_PAGE, headers=agent)).read().decode("utf-8")
    table = re.search(rf'<table id="{hook().WR_TABLE}".*?</table>', page, re.S)
    if not table:
        sys.exit(f"no {hook().WR_TABLE} on {WR_PAGE}")
    columns = re.findall(r"<th>(.*?)</th>", table.group(0))[1:]
    os.makedirs(REPLAYS, exist_ok=True)
    entries = []
    skipped = []
    # Shot types may have spaces in their names ("Border Team").
    for shot, cells in re.findall(r"<tr><td>([^<]+)</td>(.*?)</tr>", table.group(0), re.S):
        shot = html.unescape(shot).strip()
        for difficulty, cell in zip(columns, re.findall(r"<td[^>]*>(.*?)</td>", cells, re.S)):
            link = re.search(r'href="([^"]+\.rpy)"', cell)
            if not link:
                continue
            url = html.unescape(link.group(1))
            player = re.search(r"by <em>(.*?)</em>", cell)
            date = re.search(r'<span class="dimgrey"><br>([\d/]+)</span>', cell)
            name = os.path.basename(url)
            path = os.path.join(REPLAYS, name)
            if not os.path.exists(path):
                print("fetch", url)
                with urllib.request.urlopen(urllib.request.Request(url, headers=agent)) as r:
                    data = r.read()
                with open(path, "wb") as f:
                    f.write(data)
            try:
                replay = hook().Replay(path)
            except ValueError as exc:
                # The table may link a file the server no longer has, or
                # serves empty: keep where it came from, not the file.
                print(f"skip {url}: {exc}")
                os.remove(path)
                skipped.append(dict(file=name, shot=shot, difficulty=difficulty, url=url,
                                    reason=str(exc).removeprefix(path + ": ")))
                continue
            if replay.label != f"{shot} {difficulty}":
                sys.exit(f"{name} is {replay.label}, listed as {shot} {difficulty}")
            entries.append(dict(file=name, shot=shot, difficulty=difficulty, score=replay.score,
                                player=html.unescape(player.group(1)) if player else None,
                                date=date.group(1) if date else None, url=url, sha1=sha1(path)))
    with open(MANIFEST, "w") as f:
        json.dump(dict(source=WR_PAGE, fetched=datetime.date.today().isoformat(), replays=entries,
                       **(dict(skipped=skipped) if skipped else {})), f, indent=1)
        f.write("\n")
    print(f"{len(entries)} replays, {len(skipped)} skipped, {os.path.relpath(MANIFEST, ROOT)}")


# Executables

def symbols_csv():
    """symbol: (address, size) from config/symbols.csv, the original's."""
    out = {}
    with open(os.path.join(ROOT, "config", "symbols.csv")) as f:
        for row in csv.DictReader(f):
            for s in row["symbol"].split():
                out[s] = (int(row["address"], 16), int(row["size"]) if row["size"] else None)
    return out


def map_symbols(path):
    """symbol: (address, size) from an MSVC map file; a function's size is
    the distance to the next symbol after it."""
    addrs = {}
    for line in open(path, errors="replace"):
        m = re.match(r"\s*[0-9a-f]{4}:[0-9a-f]{8}\s+(\S+)\s+([0-9a-f]{8})\s", line)
        if m:
            addrs.setdefault(m.group(1), int(m.group(2), 16))
    ordered = sorted(set(addrs.values()))
    following = {a: b for a, b in zip(ordered, ordered[1:])}
    return {s: (a, following.get(a, a) - a) for s, a in addrs.items()}


def resolve(symbols: Mapping[str, tuple[int, int | None]]) -> str:
    """TRACE_ADDR's value, accepting ordered alternative symbol names."""
    parts = []
    h = hook()
    for name, spelling in {**h.FUNCTIONS, **h.GLOBALS}.items():
        alternatives = (spelling,) if isinstance(spelling, str) else spelling
        sym = next((candidate for candidate in alternatives if candidate in symbols), None)
        if sym is None:
            sys.exit(f"no {' or '.join(alternatives)} in the symbols for {name}")
        addr, size = symbols[sym]
        parts.append(f"{name}={addr:#x}")
        if name in h.SIZED:
            if not size:
                sys.exit(f"no size for {sym}")
            parts.append(f"{name}Size={size:#x}")
    return ",".join(parts)


def executable(spec):
    """(label, exe path, symbols) for an --exe argument."""
    if spec == "original":
        return spec, os.path.join(ROOT, project.ORIGINAL), symbols_csv()
    if spec == "standalone":
        return spec, os.path.join(ROOT, project.EXE), map_symbols(os.path.join(ROOT, project.EXE_MAP))
    exe, sep, mapfile = spec.partition("=")
    if not sep:
        sys.exit(f"--exe {spec}: give PATH=MAP")
    return os.path.splitext(os.path.basename(exe))[0], os.path.abspath(exe), map_symbols(mapfile)


# The tracer

def build_tracer():
    """Compiles the hook's TRACER sources and the runtime with the
    project's compiler into build/replay/<name>trace.dll, when it is out
    of date. The sources see Shift-JIS copies of src/'s headers, as the
    game's code does, and this directory's trace.h."""
    import build
    headers = glob.glob(os.path.join(ROOT, "src", "**", "*.h"), recursive=True)
    sources = [os.path.join(ROOT, p) for p in hook().TRACER] + RUNTIME
    inputs = headers + sources + [os.path.join(HERE, "trace.h")]
    if os.path.exists(TRACER) and os.path.getmtime(TRACER) > max(map(os.path.getmtime, inputs)):
        return
    include = os.path.join(OUT, "include")
    for path in headers:
        copy = os.path.join(include, os.path.relpath(path, os.path.join(ROOT, "src")))
        os.makedirs(os.path.dirname(copy), exist_ok=True)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        with open(copy, "wb") as f:
            f.write(build.to_sjis(text))
    objs = [os.path.join(OUT, os.path.splitext(os.path.basename(src))[0] + ".obj") for src in sources]
    rel = lambda p: build.win(os.path.relpath(p, ROOT))
    cwd = os.getcwd()
    os.chdir(ROOT)
    try:
        for src, obj in zip(sources, objs):
            run_tool(build, "cl", "/c", "/O2", "/Oi", "/W3", "/Zl", "/I" + rel(include), "/I" + rel(HERE),
                     "/Fo" + rel(obj), rel(src))
        run_tool(build, "link", "/DLL", "/NODEFAULTLIB", "/ENTRY:DllMain", "/OUT:" + rel(TRACER),
                 *map(rel, objs), "kernel32.lib", "user32.lib")
    finally:
        os.chdir(cwd)
    print("built", os.path.relpath(TRACER, ROOT))


def run_tool(build, name, *args):
    code, out = build.run_tool(name, *args)
    out = "\n".join(l for l in out.splitlines() if not l.endswith(".c") and "Creating library" not in l)
    if out:
        print(out)
    if code:
        sys.exit(f"{name} failed")


def patch_exe(src, dst):
    """A copy of `src` that imports Init from the tracer."""
    import patch_exe as patcher
    patcher.DLL = os.path.basename(TRACER).encode()
    patcher.patch(src, dst)


# Running

# Runs at the same time can't share a prefix: its one wineserver gives
# them one desktop with one active window, and a game may only run while
# its window is active, or only once (a named mutex). So each slot of
# `check -j` gets a prefix of its own, made on first use.
PREFIXES = os.path.join(OUT, "wine")


def wine_env(display, prefix):
    return dict(os.environ, DISPLAY=display, LANG="ja_JP.UTF-8", LC_ALL="ja_JP.UTF-8", WINEPREFIX=prefix,
                WINEDEBUG=os.environ.get("WINEDEBUG", "-all"),
                # No audio drivers: nothing reaches the speakers, and
                # DirectSound fails to start. (No driver in the registry
                # instead makes Wine's mmdevapi fail an assertion.)
                WINEDLLOVERRIDES="winealsa.drv,winepulse.drv,wineoss.drv,winecoreaudio.drv=",
                PULSE_SERVER="none")


def wine_prefix(slot):
    """The Wine prefix of `slot`, made on first use, without Mono or
    Gecko."""
    prefix = os.path.join(PREFIXES, str(slot))
    marker = os.path.join(prefix, ".replay-1")
    if os.path.exists(marker):
        return prefix
    print("making the Wine prefix", os.path.relpath(prefix, ROOT))
    os.makedirs(prefix, exist_ok=True)
    env = dict(os.environ, WINEPREFIX=prefix, WINEDEBUG="-all", WINEDLLOVERRIDES="mscoree,mshtml=", DISPLAY="")
    for cmd in (["wineboot", "-i"], ["wineserver", "-w"]):
        subprocess.run(cmd, env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    open(marker, "w").close()
    return prefix


def start_xvfb():
    """Starts Xvfb on a free display; returns the process and the display."""
    read, write = os.pipe()
    xvfb = subprocess.Popen(["Xvfb", "-displayfd", str(write), "-screen", "0", "640x480x24", "-nolisten", "tcp"],
                            pass_fds=[write], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.close(write)
    with os.fdopen(read) as f:
        number = f.readline().strip()
    if not number:
        sys.exit("Xvfb did not start")
    return xvfb, ":" + number


def game_files(dst):
    """Puts the game's files (but its executable) and the standalone exe's
    extras into `dst`: links to the archives and directories, which the
    game only reads, and copies of the rest, which it may write."""
    game = project.config["game"]
    src = os.path.join(ROOT, game["dir"])
    os.makedirs(dst, exist_ok=True)
    paths = [os.path.join(src, n) for n in os.listdir(src) if n != game["exe"]]
    for pattern in game["standalone_extras"]:
        paths += sorted(glob.glob(os.path.join(ROOT, pattern)))
    for path in paths:
        to = os.path.join(dst, os.path.basename(path))
        if os.path.lexists(to):
            continue
        if os.path.isdir(path) or path.lower().endswith(".dat"):
            os.symlink(os.path.abspath(path), to)
        else:
            shutil.copyfile(path, to)


# A game that has not started the replay by then is stuck (a message box
# under Wine, say).
START_TIMEOUT = 180


def started_replay(trace):
    """Whether a run's trace has a record yet: the first stage's start,
    which tracers write out at once."""
    try:
        with open(trace, "rb") as f:
            head = f.read(8192)
        count = struct.unpack_from("<I", head, 8)[0]
        at = 16
        for _ in range(count + 1):
            at = head.index(b"\n", at) + 1
    except (OSError, struct.error, ValueError):
        return False
    return os.path.getsize(trace) > at


def run(rpy, spec, out_dir, slot=0, speed=0, render=False, show=False, timeout=3600, tail=60, limit=None):
    """Plays `rpy` in the executable `spec`; returns the run's summary,
    which it also writes to <out_dir>/run.json."""
    replay = hook().Replay(rpy)
    label, exe, symbols = executable(spec)
    if not os.path.exists(exe):
        sys.exit(f"no {exe}")
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    game = os.path.join(out_dir, "game")
    game_files(game)
    env_game = hook().setup(game, replay)
    name = f"{project.NAME}.exe"
    patch_exe(exe, os.path.join(game, name))
    shutil.copyfile(TRACER, os.path.join(game, os.path.basename(TRACER)))
    trace = os.path.join(out_dir, "trace.bin")
    xvfb, display = (None, os.environ.get("DISPLAY", ":0")) if show else start_xvfb()
    env = wine_env(display, wine_prefix(slot))
    env.update(env_game, TRACE_ADDR=resolve(symbols), TRACE_OUT="..\\trace.bin", TRACE_SPEED=str(speed),
               TRACE_RENDER=str(int(render)), TRACE_TAIL=str(tail))
    if limit:
        env["TRACE_LIMIT"] = str(limit)
    started = time.time()
    stuck = None
    with open(os.path.join(out_dir, "wine.log"), "w") as log:
        proc = subprocess.Popen(["wine", name], cwd=game, env=env, stdout=log, stderr=log, start_new_session=True)
        while (code := proc.poll()) is None:
            time.sleep(1)
            elapsed = time.time() - started
            if elapsed > timeout:
                stuck = f"timed out after {timeout} s"
            elif elapsed > START_TIMEOUT and not started_replay(trace):
                stuck = f"the replay did not start in {START_TIMEOUT} s"
            if stuck:
                os.killpg(proc.pid, 9)
                code = proc.wait()
                break
    seconds = time.time() - started
    # Whatever is left in the slot's prefix could hold on to the game for
    # the next run.
    subprocess.run(["wineserver", "-k"], env=env, stderr=subprocess.DEVNULL)
    if xvfb:
        xvfb.terminate()
        xvfb.wait()
    summary = dict(replay=os.path.relpath(rpy, ROOT), replay_sha1=sha1(rpy), exe=label,
                   exe_path=os.path.relpath(exe, ROOT), exe_sha1=sha1(exe),
                   patched_sha1=sha1(os.path.join(game, name)),
                   tracer_sha1=sha1(os.path.join(game, os.path.basename(TRACER))), speed=speed,
                   render=render, exit_code=code, stuck=stuck, seconds=round(seconds, 1))
    if os.path.exists(trace):
        summary["trace_sha1"] = sha1(trace)
        summary.update(validate(replay, read_trace(trace)))
    else:
        summary.update(passed=False, failures=["no trace"])
    if stuck:
        summary["failures"].insert(0, stuck)
        summary["passed"] = False
    elif code != 0:
        summary["failures"].insert(0, f"exit code {code}")
        summary["passed"] = False
    with open(os.path.join(out_dir, "run.json"), "w") as f:
        json.dump(summary, f, indent=1)
        f.write("\n")
    return summary


# Traces

class Trace:
    def __init__(self, fields: list[str], info: str, records: list[tuple[int, ...]]) -> None:
        self.fields = fields
        self.info = info
        self.records = records
        self.index = {f: i for i, f in enumerate(fields)}

    def get(self, record: tuple[int, ...], field: str) -> int:
        return record[self.index[field]]

    def of(self, kind: int) -> list[tuple[int, ...]]:
        k = self.index["kind"]
        return [r for r in self.records if r[k] == kind]


def read_trace(path: str | os.PathLike[str]) -> Trace:
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 16:
        raise ValueError(f"{path}: truncated trace header")
    magic, version, count, size = struct.unpack_from("<4sIII", data)
    if magic != b"TRCE" or version != 1 or not count or size != count * 4:
        raise ValueError(f"{path}: not a version 1 trace")
    at = 16
    fields = []
    try:
        for _ in range(count + 1):
            end = data.index(b"\n", at)
            fields.append(data[at:end].decode("ascii"))
            at = end + 1
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"{path}: invalid trace field header") from exc
    info = fields.pop()
    if any(not field for field in fields) or len(set(fields)) != len(fields):
        raise ValueError(f"{path}: empty or duplicate trace fields")
    missing = [f for f in REQUIRED_FIELDS if f not in fields]
    if missing:
        raise ValueError(f"{path}: no {', '.join(missing)} fields")
    body = data[at:]
    if len(body) % size:
        raise ValueError(f"{path}: truncated trace record ({len(body) % size} trailing bytes)")
    return Trace(fields, info, list(struct.iter_unpack(f"<{count}I", body)))


def validate(replay, trace):
    """Checks a run's trace against its replay: see the module's
    description. Returns the results."""
    get = trace.get
    failures = []
    if "failed" in trace.info:
        failures.append("the tracer could not install: " + trace.info)
    calcs = trace.of(CALC)
    playing = [r for r in calcs if get(r, "flags") & 1]
    starts = trace.of(STAGE_START)
    ends = trace.of(STAGE_END) + trace.of(REPLAY_END)
    order = [get(r, "stage") for r in starts]
    if order != list(replay.stages):
        failures.append(f"stages played {order}, the replay has {list(replay.stages)}")
    if not trace.of(REPLAY_END):
        failures.append("the replay did not end")
    elif not trace.of(TITLE):
        failures.append("the game did not get back to the title")
    if trace.of(LIMIT):
        failures.append("ran out of calculations")
    for r in trace.of(CRASH):
        failures.append(f"crashed with exception {get(r, 'passes'):#010x} at {get(r, 'calcs'):#010x} "
                        f"in stage {get(r, 'stage')}")
    stage_failures = getattr(hook(), "stage_failures", None)
    stages = []
    for i, start in enumerate(starts):
        stage = get(start, "stage")
        recorded = replay.stages.get(stage)
        end = ends[i] if i < len(ends) else None
        in_stage = [r for r in playing if get(r, "seq") > get(start, "seq")
                    and (end is None or get(r, "seq") < get(end, "seq"))]
        frames = [get(r, "replayFrame") for r in in_stage]
        gaps = [(a, b) for a, b in zip(frames, frames[1:]) if b != a + 1]
        result = dict(stage=stage, calcs=len(in_stage), last_frame=frames[-1] if frames else None, gaps=len(gaps))
        if frames and frames[0] != 1:
            failures.append(f"stage {stage}: the first calculation is frame {frames[0]}, not 1")
        if gaps:
            failures.append(f"stage {stage}: {len(gaps)} gaps in the replay's frames, first {gaps[0]}")
        if end is None:
            failures.append(f"stage {stage} did not end")
        elif recorded:
            result.update(end_score=get(end, "score"), recorded_score=recorded["score"])
            if get(end, "score") != recorded["score"]:
                failures.append(f"stage {stage}: ended with {get(end, 'score')}, the replay recorded "
                                f"{recorded['score']}")
            frame_counts = recorded.get("frame_counts", [recorded["frames"]])
            if not frames or frames[-1] not in frame_counts:
                expected = " or ".join(map(str, frame_counts))
                failures.append(f"stage {stage}: played {frames[-1] if frames else 0} frames, the replay "
                                f"requires {expected}")
            elif stage_failures:
                failures += [f"stage {stage}: {f}" for f in stage_failures(stage, recorded, in_stage[-1], end, get)]
        stages.append(result)
    # A game that fast-forwards (dialogue in a replay) runs several
    # calculations in one pass of the main loop; it counts each one.
    passes = [get(r, "passes") for r in trace.records if get(r, "kind") != CRASH]
    if any(b < a for a, b in zip(passes, passes[1:])):
        failures.append("the main loop's counter went backwards")
    counted = [get(r, "calcs") for r in calcs]
    if any(b <= a for a, b in zip(counted, counted[1:])):
        failures.append("the game's calculation counter did not go up at each calculation")
    return dict(passed=not failures, failures=failures, info=trace.info, records=len(trace.records),
                calcs=len(calcs), playing_calcs=len(playing), stages=stages,
                final_score=get(ends[-1], "score") if ends else None)


def compare(a: Trace, b: Trace) -> dict[str, Any]:
    """Compares two traces record by record. Returns the results, with
    the first records that differ."""
    if a.fields != b.fields:
        return dict(identical=False, compared=0, difference="the traces have different fields")
    n = min(len(a.records), len(b.records))
    for i in range(n):
        if a.records[i] != b.records[i]:
            ra, rb = a.records[i], b.records[i]
            fields = {f: [ra[j], rb[j]] for j, f in enumerate(a.fields) if ra[j] != rb[j]}
            return dict(identical=False, compared=i, values=i * len(a.fields),
                        first_difference=dict(record=i, kind=KIND_NAMES.get(a.get(ra, "kind"), a.get(ra, "kind")),
                                              stage=a.get(ra, "stage"), replay_frame=a.get(ra, "replayFrame"),
                                              fields=fields))
    if len(a.records) != len(b.records):
        return dict(identical=False, compared=n, values=n * len(a.fields),
                    difference=f"{len(a.records)} records against {len(b.records)}")
    return dict(identical=True, compared=n, values=n * len(a.fields))


# Checking

def commit():
    """The checked-out commit, with "-dirty" for uncommitted changes."""
    r = subprocess.run(["git", "describe", "--always", "--dirty", "--abbrev=12"], cwd=ROOT,
                       capture_output=True, text=True)
    return r.stdout.strip() or None


def case_name(rpy):
    return os.path.splitext(os.path.basename(rpy))[0]


def describe(summary):
    state = "PASS" if summary["passed"] else "FAIL"
    text = (f"{state} {summary['exe']}: {summary.get('playing_calcs', 0)} calculations in "
            f"{len(summary.get('stages', []))} stages, final score {summary.get('final_score')}, "
            f"{summary['seconds']} s")
    for failure in summary["failures"]:
        text += "\n    " + failure
    return text


def check(rpys, specs, jobs=3, **kw):
    """Plays each replay in each executable and compares every run with
    the first. Returns whether all passed and matched."""
    build_tracer()
    runs = []
    for rpy in rpys:
        labels = []
        for spec in specs:
            label = executable(spec)[0]
            while label in labels:
                label += "+"
            labels.append(label)
            runs.append((rpy, spec, os.path.join(OUT, case_name(rpy), label)))
    slots = queue.Queue()
    for slot in range(jobs):
        wine_prefix(slot)
        slots.put(slot)

    def one(job):
        slot = slots.get()
        try:
            return run(*job, slot=slot, **kw)
        finally:
            slots.put(slot)

    with ThreadPoolExecutor(jobs) as pool:
        results = list(pool.map(one, runs))
    failed = False
    verdicts = []
    for rpy in rpys:
        case = case_name(rpy)
        mine = [(r, s) for r, s in zip(runs, results) if r[0] == rpy]
        print(f"{case}: {hook().Replay(rpy).label}")
        for _, summary in mine:
            print("  " + describe(summary).replace("\n", "\n  "))
            failed = failed or not summary["passed"]
        first = os.path.join(mine[0][0][2], "trace.bin")
        base = read_trace(first) if os.path.exists(first) else None
        comparisons = []
        for (_, _, out_dir), _ in mine[1:]:
            path = os.path.join(out_dir, "trace.bin")
            if base is None or not os.path.exists(path):
                result = dict(identical=False, compared=0, difference="a trace is missing")
            else:
                result = compare(base, read_trace(path))
            result.update(a=os.path.basename(mine[0][0][2]), b=os.path.basename(out_dir))
            comparisons.append(result)
            state = "IDENTICAL" if result["identical"] else "DIFFERENT"
            print(f"  {state} {result['a']} v {result['b']}: {result['compared']} records, "
                  f"{result.get('values', 0)} values")
            if not result["identical"]:
                failed = True
                print("    " + json.dumps(result.get("first_difference") or result.get("difference")))
        evidence = dict(replay=os.path.relpath(rpy, ROOT), label=hook().Replay(rpy).label,
                        date=datetime.datetime.now().isoformat(timespec="seconds"), commit=commit(),
                        runs=[s for _, s in mine], comparisons=comparisons)
        with open(os.path.join(OUT, case, "evidence.json"), "w") as f:
            json.dump(evidence, f, indent=1)
            f.write("\n")
        verdicts.append((all(s["passed"] for _, s in mine) and all(c["identical"] for c in comparisons),
                         mine[0][1].get("playing_calcs", 0)))
    if len(rpys) > 1:
        good = [calcs for ok, calcs in verdicts if ok]
        print(f"{len(good)} of {len(verdicts)} replays passed and matched, {sum(good)} calculations each")
    return not failed


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch")
    p = sub.add_parser("info")
    p.add_argument("rpy", nargs="+")
    for name in ("run", "check"):
        p = sub.add_parser(name)
        p.add_argument("rpy", nargs="*" if name == "check" else None)
        p.add_argument("--exe", action="append")
        p.add_argument("--speed", type=int, default=0)
        p.add_argument("--render", action="store_true")
        p.add_argument("--show", action="store_true")
        p.add_argument("--timeout", type=int, default=3600)
        p.add_argument("--tail", type=int, default=60)
        if name == "check":
            p.add_argument("-j", type=int, default=3)
    p = sub.add_parser("compare")
    p.add_argument("traces", nargs=2)
    args = parser.parse_args()

    if args.cmd == "fetch":
        fetch()
    elif args.cmd == "info":
        info(args.rpy)
    elif args.cmd == "compare":
        result = compare(*map(read_trace, args.traces))
        print(json.dumps(result, indent=1))
        sys.exit(0 if result["identical"] else 1)
    else:
        kw = dict(speed=args.speed, render=args.render or args.show, show=args.show, timeout=args.timeout,
                  tail=args.tail)
        if args.cmd == "run":
            build_tracer()
            spec = (args.exe or ["original"])[0]
            summary = run(args.rpy, spec, os.path.join(OUT, case_name(args.rpy), executable(spec)[0]), **kw)
            print(describe(summary))
            sys.exit(0 if summary["passed"] else 1)
        specs = args.exe or ["original", "original", "standalone"]
        rpys = args.rpy or [os.path.join(REPLAYS, r["file"]) for r in json.load(open(MANIFEST))["replays"]]
        sys.exit(0 if check(rpys, specs, jobs=args.j, **kw) else 1)


if __name__ == "__main__":
    main()
