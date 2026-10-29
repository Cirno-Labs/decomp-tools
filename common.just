# The recipes every game shares. A game's justfile imports this file
# (`import 'tools/common.just'`) and adds its own. project.toml in the
# game's root says what differs between the games.

export WINEPREFIX := justfile_directory() / ".wine"
export WINEDEBUG := env("WINEDEBUG", "-all")

# The tools repository, checked out as a git submodule at tools/;
# pushes go over SSH
tools_push_url := "git@github.com:Cirno-Labs/decomp-tools.git"

# List recipes. [default] makes it the default although it is imported:
# just only takes the first recipe of the root justfile otherwise.
[default]
default:
    @just --list

# Compile src/ and link the hook DLL, build/<name>.dll
build:
    python3 tools/build.py

# Compare compiled functions with the original; show a diff for each SYMBOL
[positional-arguments]
compare *symbols: build _orig
    python3 tools/compare.py "$@"

# Set up build/game: game data, patched exe and the hook DLL
game: build _orig
    python3 tools/project.py game

# Run the patched game headless with STEPS (wait:S key:K hold:K:S shot:NAME)
[positional-arguments]
run *steps: game
    python3 tools/play.py "$@"

# Link the standalone build/<name>.exe (no DLL; FEAT=a,b adds features); with STEPS, run it like `run`
[positional-arguments]
exe *steps: game
    python3 tools/build.py exe
    python3 tools/project.py standalone
    if [ $# -gt 0 ]; then python3 tools/play.py --standalone "$@"; fi

# Compare the standalone exe with the original, section by section (-v: more)
[positional-arguments]
compare-exe *args: _orig
    python3 tools/compare_exe.py "$@"

# Extract VS.NET 2002's Platform SDK import libraries from its install ZIP
psdk-libs zip:
    python3 tools/psdk_libs.py {{ quote(zip) }}

# Analyze the original with headless Ghidra; add new functions to symbols.csv
analyze: _orig
    tools/ghidra/analyze.sh
    python3 tools/seed_symbols.py

# Decompile ADDRs (default: every function) with Ghidra into build/decomp/
decompile *addrs:
    tools/ghidra/decompile.sh {{ addrs }}

# Render docs/progress.svg from symbols.csv and the build
progress: build _orig
    python3 tools/render_progress.py

# Use tools/hooks/ here and in tools/: 50/72 messages, no trailers; commits redraw progress.svg if hooks.progress
hooks:
    git config core.hooksPath tools/hooks
    git -C tools config core.hooksPath hooks

# Remove build outputs (keeps the Ghidra project)
clean:
    python3 tools/project.py clean

# Verify the original and copy it to build/ under an ASCII name
_orig:
    python3 tools/project.py orig

# Add the decomp-tools git remote that tools-pull and tools-push use
# Check out tools/ at the commit the game records, on a main branch
# rather than a detached HEAD (after a clone or a pointer change)
tools-init:
    git submodule update --init tools
    git -C tools switch -q -C main "$(git rev-parse HEAD:tools)"

# Bring tools/ up to decomp-tools' main; commit the new pointer afterwards
tools-pull:
    git -C tools switch -q main
    git -C tools pull -q --ff-only
    git -C tools log -1 --oneline

# Push the commits made in tools/ to decomp-tools' main
tools-push:
    git -C tools push {{ tools_push_url }} main
