# decomp-tools

Tooling for matching decompilations of Touhou games built with MSVC 7
(Visual C++ .NET 2002) under Wine, on Linux. The goal of such a project
is C++ source that compiles, with the original toolchain, into code
byte-for-byte identical to the original executable.

The tools compile the game's source under Wine, compare every function
with the original byte for byte, hook the new code into the running game
through a DLL, link a standalone executable and compare it with the
original section by section, drive the game headless under Xvfb, and
analyze the original with headless Ghidra.

## Clean room

This repository holds tools only. It contains no game code or data, no
decompiled source, no symbol tables and nothing from Microsoft's
toolchain or SDKs. Each game repository brings its own source and
symbols, and each user brings their own copy of the game and the
compiler.

## Using it in a game repository

A game repository includes this repository as a git subtree at `tools/`
(not a submodule), describes itself in `project.toml` at its root, and
imports the shared recipes into its justfile:

```just
import 'tools/common.just'

# the game's own recipes follow
```

It expects this layout around it:

```
project.toml  the game's settings (see below)
justfile      imports tools/common.just
config/       symbols.csv: address, size, name and mangled symbol
src/          decompiled source; src/dll/ is the hook DLL's own code,
              src/exe/ the data and globals only the standalone exe builds
orig/         the user's game files (not tracked) and their checksums
.wine/        the Wine prefix with the toolchain (not tracked)
build/        outputs
tools/        this repository
```

`examples/` has the configuration of th06, th07 and th08, and th06's
build hook. To add the tools to a game repository:

```sh
git subtree add --prefix tools git@github.com:Cirno-Labs/decomp-tools.git main --squash
cp tools/examples/th07.toml project.toml   # then adapt it
echo "import 'tools/common.just'" > justfile
just tools-remote
```

## Recipes

`just` lists them. `common.just` has:

| Recipe | Does |
| --- | --- |
| `build` | compile `src/` and link the hook DLL, `build/<name>.dll` |
| `compare [SYMBOL...]` | compare every compiled function with the original; disassembly diffs for SYMBOLs |
| `game` | set up `build/game/`: the game's files, an exe patched to load the DLL, the DLL |
| `run [STEP...]` | run the patched game headless (`wait:S key:K hold:K:S shot:NAME`) |
| `exe [STEP...]` | link the standalone `build/<name>.exe`; `FEAT=a,b` turns on features |
| `compare-exe [-v]` | compare the standalone exe with the original, section by section |
| `psdk-libs ZIP` | extract VS.NET 2002's Platform SDK import libraries from its install media |
| `analyze` | analyze the original with headless Ghidra, seed `config/symbols.csv` |
| `decompile [ADDR...]` | write Ghidra's pseudocode to `build/decomp/` |
| `progress` | render `docs/progress.svg` |
| `hooks` | use `tools/hooks/`: 50/72 commit messages without type prefixes or trailers, and with `hooks.progress` a progress image in every commit |
| `clean` | remove build outputs (keeps the Ghidra project) |

Compiles run on half the cores; `JOBS` overrides it. Ghidra is found
through `GHIDRA`, `ghidra.path` or the newest `~/opt/ghidra_*`.

### The subtree

| Recipe | Runs |
| --- | --- |
| `tools-remote` | adds the `decomp-tools` remote, `git@github.com:Cirno-Labs/decomp-tools.git` |
| `tools-pull` | `git subtree pull --prefix tools decomp-tools main --squash` |
| `tools-push` | `git subtree push --prefix tools decomp-tools main` |

Each pull adds two commits to the game repository: a squash commit
holding the new state of the tools, and a merge commit that brings it
into `tools/`. Both carry git-subtree's own trailers
(`git-subtree-dir`, `git-subtree-split`), which it needs to find the
last pull.

Change the tools in any game repository and commit the change to
`tools/` on its own: `tools-push` splits the commits that touched
`tools/` out of the game's history and pushes them as decomp-tools
commits with the same messages, and a commit that also touched other
files would bring only its `tools/` part along. Then `just tools-pull`
in the other games.

The split history that `tools-push` sends also holds the merge commits
of the game's earlier pulls (`Merge commit '...' as 'tools'`), so
these show up in decomp-tools' history too.

## Tools

| File | Does |
| --- | --- |
| `project.py` | reads `project.toml`; the `orig`, `game`, `standalone` and `clean` steps |
| `build.py` | compiles `src/` (Shift-JIS, per-file flags, parallel), writes the hook table and thunks, links the DLL and the standalone exe |
| `compare.py` | compares compiled functions with the original, relocations resolved through `symbols.csv` |
| `compare_exe.py` | compares the standalone exe with the original: headers, Rich header, sections, imports, code and data by symbol |
| `patch_exe.py` | adds the hook DLL to a copy of the original's import table |
| `play.py` | runs the game under Xvfb, sends keys, takes screenshots |
| `seed_symbols.py` | adds Ghidra's functions and the imports to `symbols.csv` |
| `name.py` | names a row of `symbols.csv` |
| `rename.py` | renames functions in the working tree or throughout history |
| `render_progress.py` | draws the progress image |
| `locals.py` | predicts and picks local variable names for a wanted stack layout under `/Od` |
| `psdk_libs.py`, `msi_extract.py` | extract import libraries from the VS.NET 2002 media |
| `coff.py`, `rsrc.py`, `peinfo.py` | COFF objects, resources, PE and Rich header |
| `ghidra/` | headless Ghidra: analysis, function export, decompilation |
| `hooks/` | `commit-msg` and `pre-commit` |

## project.toml

Paths are relative to the game repository's root. Hex numbers are
written as such (`0x46004B`). `DECOMP_PROJECT` names another file.

### `[game]`

| Key | Default | Meaning |
| --- | --- | --- |
| `name` | required | short name: `build/<name>.dll`, `build/<name>.exe`, `<name>.exe` in `build/game/`, the hook log's prefix |
| `dir` | required | the original game's files, such as `orig/th07_100b` |
| `exe` | required | the original executable's file name in `dir` |
| `original` | required | where the verified executable is copied, an ASCII path such as `build/th07_100b.exe` |
| `game_end` | required | the address the game's code ends at; library code follows |
| `checksums` | none | a `sha1sum -c` file, run from its own directory |
| `title` | `name` | the progress image's title |
| `window_title` | `title` | the window `play.py` looks for |
| `standalone_extras` | `[]` | globs copied into `build/game/` with the standalone exe |

### `[ghidra]`

| Key | Default | Meaning |
| --- | --- | --- |
| `path` | none | the Ghidra installation; `GHIDRA` overrides it |
| `project` | `original`'s name without `.exe` | the Ghidra project in `build/ghidra/` |
| `functions` | `build/functions_<project>.csv` | the exported function list |

### `[build]`

| Key | Default | Meaning |
| --- | --- | --- |
| `cflags` | required | the compiler flags; a source file's first line `// cflags: /Os -/Oi` adds and removes some |
| `bin` | `C:\msvc7\bin` | where `cl.exe` and `link.exe` are, in the Wine prefix |
| `include` | `C:\dx8\include`, `C:\msvc7\include`, `C:\psdk\Include` | `INCLUDE` |
| `lib` | `C:\dx8\lib`, `C:\msvc7\lib`, `C:\msvc7\PlatformSDK\Lib`, `C:\psdk\Lib` | `LIB`, in search order |
| `timeout` | `60` | seconds before a compile counts as hung |
| `hook` | none | a Python file with the game's own build steps, see below |

### `[dll]`

| Key | Default | Meaning |
| --- | --- | --- |
| `ldflags` | `/DLL /NODEFAULTLIB /ENTRY:DllMain /BASE:0x30000000` | the hook DLL's linker flags |
| `libs` | `kernel32.lib user32.lib winmm.lib` | its libraries |

### `[exe]`

| Key | Default | Meaning |
| --- | --- | --- |
| `obj_dir` | `build/obj_exe` | the standalone exe's objects; `build/obj` shares the DLL's |
| `cflags` | `[]` | flags added to `build.cflags`, before a file's own; with `/GL` the exe is a second, `/LTCG` build and the plain objects give the link order |
| `defines` | `[]` | macros defined after a file's own flags (`TH06_EXE=1`) |
| `link_order` | `median` | object order: by the `median` or `first` original address of the functions only that object defines, or `source` order |
| `ldflags` | `/SUBSYSTEM:WINDOWS /FIXED /NODEFAULTLIB:libc.lib` | linker flags |
| `libs` | `libcmt.lib` | libraries, in the original's order |
| `features` | `{}` | `FEAT` names and their macros: `frame_cap = "FEAT_FRAME_CAP"` compiles `FEAT=frame_cap` into `build/obj_frame_cap` with `/DFEAT_FRAME_CAP=1` |

The exe's map is `build/<name>_exe.map`, the DLL's `build/<name>.map`.

### `[psdk]`, `[symbols]`, `[hooks]`

| Key | Default | Meaning |
| --- | --- | --- |
| `psdk.libs` | `[]` | the import libraries `psdk-libs` extracts, as named on the media (`User32.Lib`) |
| `symbols.import_libs` | `.wine/drive_c/psdk/Lib`, `.wine/drive_c/dx8/lib` | where `seed_symbols.py` looks up decorated import names |
| `hooks.progress` | `false` | the pre-commit hook redraws `docs/progress.svg` and adds it |

### The build hook

`build.hook` is for what the settings don't cover. The file may define:

- `exe_flags(feats)`: extra compiler flags for the standalone exe's
  objects, given the features asked for; it may also generate the files
  they need
- `headers()`: more headers whose changes rebuild everything
- `exe_resources(feats)`: the executable whose resources the standalone
  exe gets, instead of the original's

`examples/th06_build_hook.py` is th06's, for its English feature.

## Requirements

- Python 3.11 or later (for `tomllib`), [just](https://github.com/casey/just)
- Wine (32-bit, with its `msidb`), binutils (`objdump`), 7-Zip
- For `run`: Xvfb, xdotool, ImageMagick, 32-bit Mesa OpenGL and the
  `ja_JP.UTF-8` locale
- The original toolchain in the Wine prefix (bring your own copies)
- Ghidra, for analysis

## License

GNU General Public License, version 3 only (`GPL-3.0-only`), see
[`LICENSE`](LICENSE).
