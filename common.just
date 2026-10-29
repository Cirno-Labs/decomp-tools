# The recipes every game shares. A game's justfile imports this file
# (`import 'tools/common.just'`) and adds its own. project.toml in the
# game's root says what differs between the games.

export WINEPREFIX := justfile_directory() / ".wine"
export WINEDEBUG := env("WINEDEBUG", "-all")

# The tools repository, pulled in as a git subtree at tools/
tools_remote := "decomp-tools"
tools_url := "git@github.com:Cirno-Labs/decomp-tools.git"

# List recipes
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

# Use tools/hooks/: 50/72 messages, no trailers; commits redraw progress.svg if hooks.progress
hooks:
    git config core.hooksPath tools/hooks

# Remove build outputs (keeps the Ghidra project)
clean:
    python3 tools/project.py clean

# Verify the original and copy it to build/ under an ASCII name
_orig:
    python3 tools/project.py orig

# Add the decomp-tools git remote that tools-pull and tools-push use
tools-remote:
    git remote add {{ tools_remote }} {{ tools_url }} || git remote set-url {{ tools_remote }} {{ tools_url }}

# Merge the tools repository's main into tools/ as one squashed commit
tools-pull:
    git subtree pull --prefix tools {{ tools_remote }} main --squash

# Push the commits that changed tools/ to the tools repository's main
tools-push:
    git subtree push --prefix tools {{ tools_remote }} main
